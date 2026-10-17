import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from estudia.ai.base import AIAuthError, AIFormatError, AIQuotaError
from estudia.ai.gemini import GeminiProvider
from estudia.db import connect
from estudia.moodle.client import TokenInvalid
from estudia.secrets import get_secret, set_secret
from estudia.server.app import create_app
from fakes import DeniedYoutubeUrlClient, FakeProvider

FIXTURES = Path(__file__).parent.parent / "fixtures" / "moodle"
SITE_ID = "utn-frba"
SITE_URL = "https://aulasvirtuales.frba.utn.edu.ar"
CONTENTS = json.loads((FIXTURES / "core_course_get_contents_4242.json").read_text("utf-8"))
QUIZZES = json.loads((FIXTURES / "mod_quiz_get_quizzes_by_courses_4242.json").read_text("utf-8"))
PAGE = (FIXTURES / "page_video_clase_1.html").read_text("utf-8")
GOOD_KEY = "clave-buena-123"
PORT = 8123
BASE = f"http://127.0.0.1:{PORT}"
BAD_KEY = "clave-mala-456"


class FakeMoodle:
    """MoodleClient falso con respuestas sintéticas con la forma de las de FRBA."""

    def __init__(self, site_url, token, fail_with=None):
        self.site_url = site_url
        self.token = token
        self.fail_with = fail_with

    def call(self, fn, **params):
        if self.fail_with is not None:
            raise self.fail_with
        if fn == "core_webservice_get_site_info":
            return {"userid": 7, "fullname": "Ana Alumna"}
        if fn == "core_enrol_get_users_courses":
            return [{"id": 4242, "fullname": "Matemática - Ejemplo", "shortname": "MAT-EJ"}]
        if fn == "core_course_get_contents":
            return CONTENTS
        if fn == "mod_quiz_get_quizzes_by_courses":
            return QUIZZES
        raise AssertionError(f"función inesperada: {fn}")

    def fetch_file(self, fileurl):
        return PAGE.encode()


class AuthFailingProvider(FakeProvider):
    def check_key(self):
        raise AIAuthError("Gemini rechazó la clave (código 400)")

    def answer_blind(self, stem, options):
        raise AIAuthError("Gemini rechazó la clave (código 401)")


class CheckFailsProvider(FakeProvider):
    """Proveedor cuya verificación de clave lanza la excepción indicada."""

    def __init__(self, exc):
        super().__init__({}, lambda s, o: 1)
        self.exc = exc

    def check_key(self):
        raise self.exc


def make_provider(key):
    if key == BAD_KEY:
        return AuthFailingProvider({}, lambda s, o: 0)
    return FakeProvider({}, lambda s, o: 1)


@pytest.fixture
def env(tmp_path):
    state = {"opened": [], "moodle_tokens": [], "moodle_error": None}

    def moodle_factory(site_url, token):
        state["moodle_tokens"].append(token)
        return FakeMoodle(site_url, token, state["moodle_error"])

    app = create_app(
        db_path=tmp_path / "estudia.db",
        moodle_client_factory=moodle_factory,
        ai_provider_factory=make_provider,
        open_browser=state["opened"].append,
        web_dist=tmp_path / "no-existe",
        port=PORT,
    )
    state["app"] = app
    state["client"] = TestClient(app, base_url=BASE)
    state["db_path"] = tmp_path / "estudia.db"
    return state


def step(client):
    return client.get("/api/state").json()["step"]


def callback_url(passport, token="tok-moodle", private="tok-privado"):
    firma = hashlib.md5((SITE_URL + passport).encode()).hexdigest()
    payload = base64.b64encode(f"{firma}:::{token}:::{private}".encode()).decode()
    return f"estudia://token={payload}"


def start_login(client):
    r = client.post("/api/login/start")
    assert r.status_code == 200
    return parse_qs(urlparse(r.json()["url"]).query)["passport"][0]


def logged_in(env):
    """Atajo: facultad elegida, token y clave guardados."""
    c = env["client"]
    assert c.post("/api/site", json={"site_id": SITE_ID}).status_code == 200
    set_secret(f"moodle_token:{SITE_ID}", "tok-moodle")
    set_secret("gemini_key", GOOD_KEY)
    return c


def test_state_flow(env):
    c = env["client"]
    assert step(c) == "facultad"

    assert c.post("/api/site", json={"site_id": "no-existe"}).status_code == 404
    assert c.post("/api/site", json={"site_id": SITE_ID}).status_code == 200
    assert step(c) == "login"

    passport = start_login(c)
    assert len(env["opened"]) == 1
    assert env["opened"][0].startswith(f"{SITE_URL}/admin/tool/mobile/launch.php")

    r = c.post("/api/login/callback", json={"url": callback_url(passport)})
    assert r.status_code == 200
    assert get_secret(f"moodle_token:{SITE_ID}") == "tok-moodle"
    assert get_secret(f"moodle_private:{SITE_ID}") == "tok-privado"
    # El login dispara la sincronización en segundo plano con el token nuevo.
    assert env["moodle_tokens"] == ["tok-moodle"]
    conn = connect(env["db_path"])
    assert conn.execute("SELECT COUNT(*) FROM material").fetchone()[0] == 8
    assert step(c) == "ia"

    r = c.post("/api/ai", json={"provider": "gemini", "key": BAD_KEY})
    assert r.status_code == 400
    assert BAD_KEY not in r.text
    assert get_secret("gemini_key") is None
    assert step(c) == "ia"

    assert c.post("/api/ai", json={"provider": "gemini", "key": GOOD_KEY}).status_code == 200
    assert get_secret("gemini_key") == GOOD_KEY
    assert step(c) == "listo"


def test_callback_rejects_unknown_passport(env):
    c = env["client"]
    c.post("/api/site", json={"site_id": SITE_ID})
    start_login(c)
    url = callback_url("otro-passport")
    r = c.post("/api/login/callback", json={"url": url})
    assert r.status_code == 400
    assert "tok-moodle" not in r.text and url not in r.text
    assert get_secret(f"moodle_token:{SITE_ID}") is None
    assert env["moodle_tokens"] == []
    assert step(c) == "login"


def test_callback_without_login_start_is_rejected(env):
    c = env["client"]
    c.post("/api/site", json={"site_id": SITE_ID})
    r = c.post("/api/login/callback", json={"url": callback_url("cualquiera")})
    assert r.status_code == 400
    assert get_secret(f"moodle_token:{SITE_ID}") is None


def test_validation_errors_do_not_echo_secrets(env):
    c = env["client"]
    r = c.post("/api/ai", json={"provider": "gemini", "key": ["secreto-xyz"]})
    assert r.status_code == 422
    assert "secreto-xyz" not in r.text
    r = c.post("/api/login/callback", json={"url": 12345678901, "extra": "secreto-xyz"})
    assert r.status_code == 422
    assert "secreto-xyz" not in r.text and "12345678901" not in r.text
    r = c.post("/api/ai", content=b'{"provider": "gemini", "key": "secreto-xyz"',
               headers={"content-type": "application/json"})
    assert r.status_code == 422
    assert "secreto-xyz" not in r.text


def test_token_invalid_sends_state_to_login(env):
    c = logged_in(env)
    assert step(c) == "listo"
    env["moodle_error"] = TokenInvalid("invalidtoken")

    # La sincronización corre en segundo plano; el token inválido se detecta ahí.
    assert c.post("/api/sync").status_code == 202
    assert get_secret(f"moodle_token:{SITE_ID}") is None
    assert c.get("/api/home").status_code == 200
    assert step(c) == "login"


def test_sync_busy_returns_409(env):
    c = logged_in(env)
    jobs = env["app"].state.jobs
    assert jobs.try_start()
    try:
        assert c.post("/api/sync").status_code == 409
    finally:
        jobs.finish()
    assert c.post("/api/sync").status_code == 202


def test_ai_auth_stop_sends_state_back_to_ia(env):
    c = logged_in(env)
    set_secret("gemini_key", BAD_KEY)
    conn = connect(env["db_path"])
    conn.execute("INSERT INTO section(id, course_id, num, name, visible) VALUES (1, 10, 1, 'S1', 1)")
    conn.execute(
        "INSERT INTO material(id, section_id, cmid, kind, title, ref, status)"
        " VALUES (1, 1, 1001, 'youtube', 'Video', 'abcdefghijk', 'pending')"
    )
    conn.commit()

    class AnalyzeAuthFails(FakeProvider):
        def analyze_video(self, youtube_id, title):
            raise AIAuthError("Gemini rechazó la clave (código 401)")

    env["app"].state.jobs.ai_provider_factory = lambda key: AnalyzeAuthFails({}, lambda s, o: 0)
    assert c.post("/api/sync").status_code == 202
    state = c.get("/api/state").json()
    assert state["ai_stop_reason"] == "auth"
    assert state["step"] == "ia"


def test_video_403_after_saving_the_key_does_not_send_back_to_ia(env, tmp_path):
    """Caso real: la clave anda para texto pero Gemini no acepta enlaces de YouTube (403)."""
    c = logged_in(env)
    conn = connect(env["db_path"])
    conn.execute("INSERT INTO section(id, course_id, num, name, visible) VALUES (1, 10, 1, 'S1', 1)")
    conn.execute(
        "INSERT INTO material(id, section_id, cmid, kind, title, ref, status)"
        " VALUES (1, 1, 1001, 'youtube', 'Video', 'abcdefghijk', 'pending')"
    )
    # Lo que quedó de antes del arreglo: un 403 de video se tomó como clave mala.
    conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES ('ai_stop_reason', 'auth')")
    conn.commit()
    conn.close()

    def no_download(youtube_id, dest_dir):
        raise OSError("sin red en los tests")

    env["app"].state.jobs.ai_provider_factory = lambda key: GeminiProvider(
        key, analyze_models=["m"], blind_models=["b"], client=DeniedYoutubeUrlClient(),
        sleep=lambda s: None,
        download=no_download, video_dir=tmp_path / "videos")
    assert step(c) == "ia"
    assert c.post("/api/ai", json={"provider": "gemini", "key": GOOD_KEY}).status_code == 200
    state = c.get("/api/state").json()
    assert state["ai_stop_reason"] is None
    assert state["step"] == "listo"
    assert material_row(env["db_path"])[0] == "failed"


def test_new_ai_key_clears_the_youtube_url_denial(env):
    c = env["client"]
    conn = connect(env["db_path"])
    conn.execute("INSERT INTO kv(key, value) VALUES ('youtube_url_input', 'denied')")
    conn.commit()
    conn.close()
    assert c.post("/api/ai", json={"provider": "gemini", "key": GOOD_KEY}).status_code == 200
    conn = connect(env["db_path"])
    try:
        assert conn.execute("SELECT 1 FROM kv WHERE key='youtube_url_input'").fetchone() is None
    finally:
        conn.close()


def seed_question(db_path):
    conn = connect(db_path)
    conn.execute("INSERT INTO course(id, site_id, fullname, shortname) VALUES (10, ?, 'Álgebra', 'ALG')", (SITE_ID,))
    conn.execute("INSERT INTO section(id, course_id, num, name, visible) VALUES (1, 10, 1, 'S1', 1)")
    conn.execute(
        "INSERT INTO material(id, section_id, cmid, kind, title, ref, status, summary_md)"
        " VALUES (1, 1, 1001, 'youtube', 'Video 1', 'abcdefghijk', 'done', '# Resumen')"
    )
    conn.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (1, 10, 'rectas', 'Rectas')")
    conn.execute("INSERT INTO material_topic(material_id, topic_id) VALUES (1, 1)")
    conn.execute(
        "INSERT INTO question(id, material_id, topic_id, stem, options_json, correct_index,"
        " explanation, common_error, source_seconds, verified, retired, dedupe_key)"
        " VALUES (1, 1, 1, '¿Pendiente?', ?, 2, 'Porque m = 3', 'Confundir m con b', 42, 1, 0, 'k1')",
        (json.dumps(["1", "2", "3", "4"]),),
    )
    conn.commit()


def test_answer_flow(env):
    c = logged_in(env)
    seed_question(env["db_path"])

    r = c.post("/api/quiz", json={})
    assert r.status_code == 200
    items = r.json()
    assert items == [{"question_id": 1, "stem": "¿Pendiente?", "options": ["1", "2", "3", "4"], "topic": "Rectas"}]

    r = c.post("/api/answer", json={"question_id": 1, "chosen_index": 0})
    assert r.status_code == 200
    fb = r.json()
    assert fb["correct"] is False
    assert fb["correct_index"] == 2
    assert fb["explanation"] == "Porque m = 3"
    assert fb["youtube_id"] == "abcdefghijk"

    assert c.post("/api/answer", json={"question_id": 1, "chosen_index": 9}).status_code == 400
    assert c.post("/api/question/1/retire").status_code == 200
    assert c.post("/api/quiz", json={"topic_id": 1}).json() == []
    assert c.post("/api/answer", json={"question_id": 1, "chosen_index": 2}).status_code == 404


def test_home_and_materials(env):
    c = logged_in(env)
    seed_question(env["db_path"])
    home = c.get("/api/home").json()
    assert [t["name"] for t in home["topics"]] == ["Rectas"]
    assert home["processing"]["done"] == 1

    sections = c.get("/api/materials").json()
    assert sections[0]["name"] == "S1"
    assert sections[0]["course"] == "Álgebra"
    assert sections[0]["materials"][0] == {
        "id": 1, "title": "Video 1", "kind": "youtube", "status": "done",
        "summary_md": "# Resumen", "error": None, "youtube_id": "abcdefghijk",
        "campus_url": f"{SITE_URL}/mod/page/view.php?id=1001",
    }


def test_root_placeholder_without_web_build(env):
    r = env["client"].get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")


def test_root_serves_web_build(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<p>Estudia</p>", "utf-8")
    app = create_app(
        db_path=tmp_path / "e.db",
        moodle_client_factory=lambda u, t: FakeMoodle(u, t),
        ai_provider_factory=make_provider,
        open_browser=lambda url: None,
        web_dist=dist,
        port=PORT,
    )
    c = TestClient(app, base_url=BASE)
    assert "<p>Estudia</p>" in c.get("/").text
    assert c.get("/api/state").json()["step"] == "facultad"


def test_binds_localhost_only():
    from estudia.__main__ import uvicorn_config

    config = uvicorn_config(object(), 8123)
    assert config.host == "127.0.0.1"
    assert config.port == 8123


def test_concurrent_requests(env):
    logged_in(env)
    seed_question(env["db_path"])
    paths = ["/api/state", "/api/home", "/api/materials"]
    # Dentro del `with` todos los pedidos comparten un event loop y su threadpool,
    # como en uvicorn: la dependencia abre y cierra la conexión en hilos distintos.
    with TestClient(env["app"], base_url=BASE, raise_server_exceptions=False) as c:
        with ThreadPoolExecutor(8) as pool:
            codes = list(pool.map(lambda i: c.get(paths[i % 3]).status_code, range(100)))
    assert codes == [200] * 100


def test_local_study_works_after_token_invalid(env):
    c = logged_in(env)
    seed_question(env["db_path"])
    env["moodle_error"] = TokenInvalid("invalidtoken")
    assert c.post("/api/sync").status_code == 202
    state = c.get("/api/state").json()
    assert state["step"] == "login"
    assert state["last_sync_error"] == "token"
    assert c.get("/api/home").status_code == 200

    assert c.get("/api/materials").status_code == 200
    assert len(c.post("/api/quiz", json={}).json()) == 1
    assert c.post("/api/answer", json={"question_id": 1, "chosen_index": 2}).status_code == 200
    assert c.post("/api/question/1/retire").status_code == 200


def test_sync_error_is_recorded_and_cleared(env):
    from estudia.moodle.client import MoodleError

    c = logged_in(env)
    env["moodle_error"] = MoodleError("HTTP 503 en https://aulasvirtuales.frba.utn.edu.ar/x")
    c.post("/api/sync")
    assert c.get("/api/state").json()["last_sync_error"].startswith("HTTP 503")
    env["moodle_error"] = None
    c.post("/api/sync")
    assert c.get("/api/state").json()["last_sync_error"] is None


def test_rejects_foreign_host(env):
    r = env["client"].get("/api/state", headers={"host": "evil.com"})
    assert r.status_code == 400
    assert env["client"].get("/api/state", headers={"host": f"localhost:{PORT}"}).status_code == 200


def test_rejects_foreign_origin_on_writes(env):
    c = logged_in(env)
    seed_question(env["db_path"])
    r = c.post("/api/question/1/retire", headers={"origin": "https://evil.com"})
    assert r.status_code == 403
    r = c.post("/api/login/start", headers={"origin": f"http://127.0.0.1:{PORT + 1}"})
    assert r.status_code == 403
    assert env["opened"] == []
    r = c.post("/api/question/1/retire", headers={"origin": f"http://localhost:{PORT}"})
    assert r.status_code == 200
    # El handler de estudia:// no manda Origin.
    assert c.post("/api/login/start").status_code == 200


def test_catalog_lists_sites_without_urls(env):
    r = env["client"].get("/api/catalog")
    assert r.status_code == 200
    assert r.json() == [
        {"id": SITE_ID, "name": "UTN · Facultad Regional Buenos Aires", "university": "UTN"}
    ]


def test_unretire_undoes_a_report(env):
    c = logged_in(env)
    seed_question(env["db_path"])
    assert c.post("/api/question/1/retire").status_code == 200
    assert c.post("/api/quiz", json={}).json() == []

    assert c.post("/api/question/1/unretire").status_code == 200
    assert [q["question_id"] for q in c.post("/api/quiz", json={}).json()] == [1]
    assert c.post("/api/question/99/unretire").status_code == 404
    assert c.post("/api/question/99/retire").status_code == 404


def test_unretire_rejects_foreign_origin(env):
    c = logged_in(env)
    seed_question(env["db_path"])
    c.post("/api/question/1/retire")
    r = c.post("/api/question/1/unretire", headers={"origin": "https://evil.com"})
    assert r.status_code == 403
    assert c.post("/api/quiz", json={}).json() == []


def test_materials_campus_url_only_for_web_sites(env, monkeypatch):
    c = logged_in(env)
    seed_question(env["db_path"])
    import estudia.server.app as app_module
    from estudia.catalog import Site

    monkeypatch.setattr(
        app_module, "get_site",
        lambda site_id: Site(site_id, "X", "X", "javascript:alert(1)", "browser"),
    )
    assert c.get("/api/materials").json()[0]["materials"][0]["campus_url"] is None


def add_failed_material(db_path, mid=1, status="failed", fail_kind="format"):
    conn = connect(db_path)
    conn.execute("INSERT OR IGNORE INTO section(id, course_id, num, name, visible) VALUES (1, 10, 1, 'S1', 1)")
    conn.execute(
        "INSERT INTO material(id, section_id, cmid, kind, title, ref, status, error, fail_kind)"
        " VALUES (?, 1, ?, 'youtube', 'Video', 'abcdefghijk', ?, 'La IA devolvió una respuesta con formato inválido.', ?)",
        (mid, 1000 + mid, status, fail_kind),
    )
    conn.commit()
    conn.close()


def material_row(db_path, mid=1):
    conn = connect(db_path)
    try:
        return tuple(conn.execute("SELECT status, error FROM material WHERE id=?", (mid,)).fetchone())
    finally:
        conn.close()


def test_ai_key_rejected_when_check_is_format_error(env):
    c = env["client"]
    env["app"].state.jobs.ai_provider_factory = lambda key: CheckFailsProvider(AIFormatError("x"))
    r = c.post("/api/ai", json={"provider": "gemini", "key": "clave-rara-789"})
    assert r.status_code == 400
    assert "clave-rara-789" not in r.text
    assert get_secret("gemini_key") is None


def test_ai_key_saved_on_quota(env):
    c = env["client"]
    env["app"].state.jobs.ai_provider_factory = lambda key: CheckFailsProvider(AIQuotaError("x"))
    assert c.post("/api/ai", json={"provider": "gemini", "key": GOOD_KEY}).status_code == 200
    assert get_secret("gemini_key") == GOOD_KEY


def test_new_ai_key_retries_failed_materials(env):
    c = env["client"]
    add_failed_material(env["db_path"])
    # Sin facultad ni token no corre nada en segundo plano: el estado queda a la vista.
    env["app"].state.jobs.ai_provider_factory = lambda key: CheckFailsProvider(AIAuthError("x"))
    assert c.post("/api/ai", json={"provider": "gemini", "key": BAD_KEY}).status_code == 400
    assert material_row(env["db_path"])[0] == "failed"

    env["app"].state.jobs.ai_provider_factory = make_provider
    jobs = env["app"].state.jobs
    assert jobs.try_start()  # que no procese en segundo plano durante el test
    try:
        assert c.post("/api/ai", json={"provider": "gemini", "key": GOOD_KEY}).status_code == 200
    finally:
        jobs.finish_all()
    assert material_row(env["db_path"]) == ("pending", None)


def test_new_ai_key_does_not_retry_unavailable_videos(env):
    c = env["client"]
    add_failed_material(env["db_path"], 1, fail_kind="video")
    add_failed_material(env["db_path"], 2, fail_kind="error")
    jobs = env["app"].state.jobs
    assert jobs.try_start()
    try:
        assert c.post("/api/ai", json={"provider": "gemini", "key": GOOD_KEY}).status_code == 200
    finally:
        jobs.finish_all()
    assert material_row(env["db_path"], 1)[0] == "failed"
    assert material_row(env["db_path"], 2) == ("pending", None)


def test_retry_failed_endpoint(env):
    c = env["client"]
    add_failed_material(env["db_path"], 1)
    add_failed_material(env["db_path"], 2, status="done")
    add_failed_material(env["db_path"], 3, fail_kind="video")
    jobs = env["app"].state.jobs
    assert jobs.try_start()
    try:
        r = c.post("/api/materials/retry-failed")
    finally:
        jobs.finish_all()
    assert r.status_code == 200
    assert r.json() == {"reset": 2}
    assert material_row(env["db_path"], 1) == ("pending", None)
    assert material_row(env["db_path"], 2)[0] == "done"
    assert material_row(env["db_path"], 3) == ("pending", None)


def test_retry_failed_rejects_foreign_origin(env):
    c = env["client"]
    add_failed_material(env["db_path"])
    r = c.post("/api/materials/retry-failed", headers={"origin": "https://evil.com"})
    assert r.status_code == 403
    assert material_row(env["db_path"])[0] == "failed"


def test_callback_accepts_pasted_moodlemobile_url(env):
    c = env["client"]
    c.post("/api/site", json={"site_id": SITE_ID})
    passport = start_login(c)
    url = callback_url(passport).replace("estudia://", "moodlemobile://", 1)
    # Pegado a mano desde la interfaz: llega con el Origin de la app.
    r = c.post("/api/login/callback", json={"url": url}, headers={"origin": BASE})
    assert r.status_code == 200
    assert url not in r.text
    assert get_secret(f"moodle_token:{SITE_ID}") == "tok-moodle"


def test_state_says_when_the_gemini_quota_comes_back(env):
    c = env["client"]
    conn = connect(env["db_path"])
    conn.execute("INSERT INTO kv(key, value) VALUES ('ai_stop_reason', 'quota')")
    conn.execute("INSERT INTO kv(key, value) VALUES ('ai_quota_resets_at', '1800000000')")
    conn.commit()
    assert c.get("/api/state").json()["ai_quota_resets_at"] == 1800000000
    conn.execute("UPDATE kv SET value='unavailable' WHERE key='ai_stop_reason'")
    conn.commit()
    conn.close()
    assert c.get("/api/state").json()["ai_quota_resets_at"] is None


def test_new_ai_key_forgets_exhausted_models(env):
    c = env["client"]
    conn = connect(env["db_path"])
    conn.execute("INSERT INTO kv(key, value) VALUES ('model_exhausted:abc123abc123:gemini-2.5-flash', '4000000000')")
    conn.execute("INSERT INTO kv(key, value) VALUES ('ai_quota_resets_at', '4000000000')")
    conn.commit()
    jobs = env["app"].state.jobs
    assert jobs.try_start()
    try:
        assert c.post("/api/ai", json={"provider": "gemini", "key": GOOD_KEY}).status_code == 200
    finally:
        jobs.finish_all()
    keys = {r[0] for r in conn.execute("SELECT key FROM kv")}
    conn.close()
    assert "model_exhausted:abc123abc123:gemini-2.5-flash" not in keys
    assert "ai_quota_resets_at" not in keys


def test_api_version_route(tmp_path):
    import httpx
    import respx

    from estudia.updates import RELEASE_API
    from estudia.version import __version__

    url = "https://github.com/JimmyAlter/estudia/releases/tag/v9.9.9"
    app = create_app(
        db_path=tmp_path / "e.db",
        web_dist=tmp_path / "no-existe",
        port=PORT,
        update_http_factory=httpx.Client,
    )
    c = TestClient(app, base_url=BASE)
    with respx.mock:
        route = respx.get(RELEASE_API).mock(
            return_value=httpx.Response(200, json={"tag_name": "v9.9.9", "html_url": url})
        )
        assert c.get("/api/version").json() == {
            "current": __version__,
            "latest": {"version": "9.9.9", "url": url},
        }
        c.get("/api/version")
        assert route.call_count == 1
        route.mock(side_effect=httpx.ConnectError("sin internet"))
        assert c.get("/api/version").status_code == 200


def test_api_version_without_internet(tmp_path):
    import httpx
    import respx

    from estudia.updates import RELEASE_API

    app = create_app(
        db_path=tmp_path / "e.db", web_dist=tmp_path / "no-existe", port=PORT,
        update_http_factory=httpx.Client,
    )
    with respx.mock:
        respx.get(RELEASE_API).mock(side_effect=httpx.ConnectError("sin internet"))
        r = TestClient(app, base_url=BASE).get("/api/version")
    assert r.status_code == 200
    assert r.json()["latest"] is None
