"""Puerta de edad: con "minor" (o sin responder) nada sale hacia el proveedor de IA."""
import pytest
from fastapi.testclient import TestClient

from estudia.db import connect
from estudia.secrets import set_secret
from estudia.server.app import AGE_KEY, Jobs, _kv_set, create_app
from fakes import FakeProvider
from test_app import BASE, PORT, SITE_ID, FakeMoodle
from test_retry import ScriptedProvider, analysis


class Spy(FakeProvider):
    """Anota cada llamada que saldría hacia el proveedor."""

    def __init__(self):
        super().__init__({}, lambda s, o: 1)
        self.calls = []

    def analyze_video(self, youtube_id, title):
        self.calls.append("analyze")
        return analysis()

    def answer_blind_batch(self, items, exclude_models=()):
        self.calls.append("blind")
        return super().answer_blind_batch(items, exclude_models)

    def check_key(self):
        self.calls.append("check_key")


@pytest.fixture
def env(tmp_path):
    spy = Spy()
    factory_calls = []

    def factory(key):
        factory_calls.append(key)
        return spy

    db_path = tmp_path / "estudia.db"
    app = create_app(
        db_path=db_path,
        moodle_client_factory=lambda url, token: FakeMoodle(url, token),
        ai_provider_factory=factory,
        open_browser=lambda url: None,
        web_dist=tmp_path / "no-existe",
        port=PORT,
    )
    conn = connect(db_path)
    conn.execute("INSERT INTO section(id, course_id, num, name, visible) VALUES (1, 10, 1, 'S', 1)")
    conn.execute("INSERT INTO material(id, section_id, cmid, kind, title, ref, status)"
                 " VALUES (1, 1, 101, 'youtube', 'V', 'abcdefghijk', 'pending')")
    conn.commit()
    set_secret("gemini_key", "clave-guardada")
    client = TestClient(app, base_url=BASE)
    yield {"client": client, "conn": conn, "spy": spy, "factory_calls": factory_calls,
           "jobs": app.state.jobs, "db_path": db_path}
    conn.close()


def set_age(env, age):
    _kv_set(env["conn"], AGE_KEY, age)
    env["conn"].commit()


def material_status(env):
    return env["conn"].execute("SELECT status FROM material WHERE id=1").fetchone()[0]


def test_api_ai_rejects_with_minor_age(env):
    set_age(env, "minor")
    r = env["client"].post("/api/ai", json={"provider": "gemini", "key": "otra"})
    assert r.status_code == 403
    assert "18" in r.json()["detail"]
    assert env["spy"].calls == [] and env["factory_calls"] == []


def test_api_ai_rejects_until_age_is_answered(env):
    r = env["client"].post("/api/ai", json={"provider": "gemini", "key": "otra"})
    assert r.status_code == 403
    assert env["spy"].calls == [] and env["factory_calls"] == []


def test_api_ai_works_for_adult(env):
    set_age(env, "adult")
    assert env["client"].post("/api/ai", json={"provider": "gemini", "key": "otra"}).status_code == 200
    assert env["spy"].calls[0] == "check_key"


@pytest.mark.parametrize("age", ["minor", None])
def test_background_processing_never_calls_provider(env, age):
    if age:
        set_age(env, age)
    assert env["jobs"].run_now()
    assert env["spy"].calls == [] and env["factory_calls"] == []
    assert material_status(env) == "pending"
    assert env["jobs"].run_process_now()
    assert env["spy"].calls == [] and env["factory_calls"] == []


@pytest.mark.parametrize("age", ["minor", None])
def test_sync_triggered_and_retry_paths_do_not_call_provider(env, age):
    if age:
        set_age(env, age)
    c = env["client"]
    c.post("/api/site", json={"site_id": SITE_ID})
    set_secret(f"moodle_token:{SITE_ID}", "tok")
    assert c.post("/api/sync").status_code in (202, 409)
    env["conn"].execute("UPDATE material SET status='failed', fail_kind='error'")
    env["conn"].commit()
    assert c.post("/api/materials/retry-failed").status_code == 200
    # Las tareas de fondo del TestClient ya corrieron al responder.
    assert env["spy"].calls == [] and env["factory_calls"] == []


def test_quiz_and_practice_do_not_call_provider(env):
    set_age(env, "minor")
    c = env["client"]
    assert c.post("/api/quiz", json={}).status_code == 200
    assert c.get("/api/home").status_code == 200
    assert c.get("/api/agenda").status_code == 200
    assert c.get("/api/materials").status_code == 200
    assert env["spy"].calls == [] and env["factory_calls"] == []


def test_adult_processes_material(env):
    set_age(env, "adult")
    assert env["jobs"].run_now()
    assert env["spy"].calls[0] == "analyze"
    assert material_status(env) == "done"


def test_switching_to_minor_mid_run_stops_provider_calls(env):
    set_age(env, "adult")
    env["conn"].execute("INSERT INTO material(id, section_id, cmid, kind, title, ref, status)"
                        " VALUES (2, 1, 102, 'youtube', 'W', 'bbbbbbbbbbb', 'pending')")
    env["conn"].commit()
    spy = env["spy"]
    original = spy.analyze_video

    def analyze_then_turn_minor(youtube_id, title):
        result = original(youtube_id, title)
        _kv_set(env["conn"], AGE_KEY, "minor")
        env["conn"].commit()
        return result

    spy.analyze_video = analyze_then_turn_minor
    assert env["jobs"].run_now()
    assert spy.calls.count("analyze") == 1
    assert "blind" not in spy.calls
    statuses = [r[0] for r in env["conn"].execute("SELECT status FROM material ORDER BY id")]
    assert "failed" not in statuses


def test_provider_helper_refuses_directly(env):
    from estudia.ai.gate import AIBlocked
    set_age(env, "minor")
    with pytest.raises(AIBlocked):
        env["jobs"].provider("clave")
    assert env["factory_calls"] == []


# --- estado y ajustes ----------------------------------------------------------------------------


def logged_in(env):
    c = env["client"]
    c.post("/api/site", json={"site_id": SITE_ID})
    set_secret(f"moodle_token:{SITE_ID}", "tok")
    return c


def test_state_exposes_age_and_skips_key_step_for_minor(env):
    c = logged_in(env)
    from estudia.secrets import delete_secret
    delete_secret("gemini_key")
    s = c.get("/api/state").json()
    assert (s["age"], s["step"]) == (None, "edad")
    assert c.put("/api/settings", json={"age": "adult"}).status_code == 200
    s = c.get("/api/state").json()
    assert (s["age"], s["step"]) == ("adult", "ia")
    assert c.put("/api/settings", json={"age": "minor"}).status_code == 200
    s = c.get("/api/state").json()
    assert (s["age"], s["step"]) == ("minor", "listo")


def test_existing_install_with_key_and_no_answer_stays_listo(env):
    c = logged_in(env)
    s = c.get("/api/state").json()
    assert (s["age"], s["step"]) == (None, "listo")


def test_put_settings_validates_age(env):
    c = env["client"]
    assert c.put("/api/settings", json={"age": "joven"}).status_code == 400
    assert c.put("/api/settings", json={"age": 17}).status_code == 422
    assert c.put("/api/settings", json={}).status_code == 400
    assert c.get("/api/settings").json()["age"] is None
    assert c.put("/api/settings", json={"age": "minor"}).status_code == 200
    assert c.get("/api/settings").json() == {"reminders": True, "age": "minor", "allow_download": False}


def test_settings_change_keeps_stored_key_untouched(env):
    from estudia.secrets import get_secret
    assert env["client"].put("/api/settings", json={"age": "minor"}).status_code == 200
    assert get_secret("gemini_key") == "clave-guardada"


def test_minor_hides_stop_reason(env):
    c = logged_in(env)
    env["conn"].execute("INSERT OR REPLACE INTO kv(key, value) VALUES ('ai_stop_reason', 'quota')")
    env["conn"].commit()
    set_age(env, "minor")
    assert c.get("/api/state").json()["ai_stop_reason"] is None


def test_resuming_after_answer_adult_runs_pending(env):
    c = logged_in(env)
    assert c.put("/api/settings", json={"age": "adult"}).status_code == 200
    assert env["spy"].calls[:1] == ["analyze"]


def test_api_ai_race_to_minor_returns_403(env, monkeypatch):
    """La edad pasa a menor entre la revisión de la ruta y `jobs.provider`."""
    from estudia.server import app as app_module
    set_age(env, "minor")  # lo que ve `jobs.provider`...
    monkeypatch.setattr(app_module, "ai_age", lambda conn: "adult")  # ...y lo que vio la ruta
    r = env["client"].post("/api/ai", json={"provider": "gemini", "key": "otra"})
    assert r.status_code == 403
    assert env["spy"].calls == []


def test_aiblocked_does_not_mark_failed_and_periodic_sync_survives(env):
    import threading
    from estudia.ai.gate import AIBlocked
    from estudia.server.app import periodic_sync
    set_age(env, "adult")
    jobs = env["jobs"]
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise AIBlocked("x")

    jobs.run_now = boom
    stop = threading.Event()
    jobs.seconds_until_due = lambda: 0
    periodic_sync(jobs, stop, sleep=lambda s: stop.set() or True)
    assert calls
    assert material_status(env) == "pending"


def test_gate_read_does_not_open_a_full_connection(env, monkeypatch):
    set_age(env, "adult")
    monkeypatch.setattr(env["jobs"], "connect", lambda: (_ for _ in ()).throw(AssertionError("conexión completa")))
    assert env["jobs"].ai_allowed() is True


@pytest.mark.parametrize("bad_path", ["/tmp/estudia\0roto.db", 3, object()])
def test_age_is_adult_with_a_bad_path_is_not_adult(bad_path):
    """Cualquier error, no solo de SQLite, cuenta como "no es adulto"."""
    from estudia.ai.gate import age_is_adult

    assert age_is_adult(bad_path) is False


def test_minor_never_sends_document():
    from estudia.ai.base import DocumentInput
    from estudia.ai.gate import AIBlocked, GatedProvider

    fake = FakeProvider({}, lambda s, o: 1)
    doc = DocumentInput(kind="pdf", title="Apunte 3", pages=3, data=b"%PDF-1.4")
    with pytest.raises(AIBlocked):
        GatedProvider(fake, lambda: False).analyze_document(doc)
    assert fake.analyzed_docs == []
    assert fake.requests["pdf"] == 0


def test_gated_provider_passes_link_settings_through():
    """El envoltorio deja leer y escribir la marca del enlace y la descarga del proveedor."""
    from estudia.ai.gate import GatedProvider
    from estudia.ai.gemini import GeminiProvider

    inner = GeminiProvider("clave-falsa", client=object(), clock=lambda: 1000.0)
    gated = GatedProvider(inner, lambda: True)
    assert gated.youtube_url_denied is False and gated.allow_download is False
    gated.youtube_url_denied_until = 5000.0
    gated.allow_download = True
    assert inner.youtube_url_denied_until == 5000.0 and inner.allow_download is True
    assert gated.youtube_url_denied is True
    gated.youtube_url_denied = False
    assert gated.youtube_url_denied_until == 0.0
    assert gated.last_video_seconds is None
