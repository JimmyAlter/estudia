"""El modo demo: la materia de ejemplo vive en su propia base (`demo.db`) y nunca se mezcla
con la real (`estudia.db`), salvo la marca `demo_active`."""
import io
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

from estudia import demo
from estudia.db import connect
from estudia.demo import DEMO_CONNECTED_TEXT, DEMO_ROUTES, load_demo_subject
from estudia.server.app import create_app
from estudia.study.exams import create_manual_exam
from test_app import PORT, callback_url, env, logged_in, make_provider, start_login  # noqa: F401 - `env` es el fixture

COURSE = "Matemática de ejemplo"
# Todo lo que la demo puede mostrar: las preguntas y las variantes de su grupo.
STEMS = [stem for q in load_demo_subject().questions for stem in (q.stem, *(v.stem for v in q.variants))]
DEMO_FILES = ("demo.db", "demo.db-journal", "demo.db-wal", "demo.db-shm",
              "demo.db.tmp", "demo.db.tmp-journal", "demo.db.tmp-wal", "demo.db.tmp-shm")


@pytest.fixture
def denv(env, monkeypatch):
    """`env` (que ya cuenta las llamadas al campus en `moodle_tokens`) con un contador en la fábrica de la IA, y sin red para el aviso de versión."""
    jobs = env["app"].state.jobs
    env["ai_keys"] = []

    def ai_factory(key):
        env["ai_keys"].append(key)
        return make_provider(key)

    jobs.ai_provider_factory = ai_factory

    def no_network():
        raise OSError("sin red en los tests")

    monkeypatch.setattr("estudia.server.app.default_http", no_network)
    return env


def dump(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return list(conn.iterdump())
    finally:
        conn.close()


def demo_file(env) -> Path:
    return env["db_path"].with_name("demo.db")


def answer_some(c, n):
    items = c.post("/api/quiz", json={}).json()
    assert len(items) >= n
    for item in items[:n]:
        r = c.post("/api/answer", json={"question_id": item["question_id"], "chosen_index": 0})
        assert r.status_code == 200, r.text
    return items


def seed_real_questions(db_path: Path) -> str:
    """Una materia real con preguntas exportables, cargada a mano en la base real."""
    conn = connect(db_path)
    conn.execute("INSERT INTO course(id, site_id, fullname, shortname) VALUES (10, 'utn-frba', 'Álgebra real', 'ALG')")
    conn.execute("INSERT INTO section(id, course_id, num, name, visible) VALUES (1, 10, 1, 'S1', 1)")
    conn.execute("INSERT INTO material(id, section_id, cmid, kind, title, ref, status, summary_md)"
                 " VALUES (1, 1, 1001, 'youtube', 'Video real', 'abcdefghijk', 'done', '# Real')")
    conn.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (1, 10, 'rectas', 'Rectas')")
    conn.execute("INSERT INTO material_topic(material_id, topic_id) VALUES (1, 1)")
    for qid in range(1, 4):
        conn.execute(
            "INSERT INTO question(id, material_id, topic_id, stem, options_json, correct_index,"
            " explanation, common_error, verified, retired, dedupe_key) VALUES (?, 1, 1, ?, ?, 0, 'e', 'c', 1, 0, ?)",
            (qid, f"Pregunta real {qid}", json.dumps(["1", "2", "3", "4"]), f"real{qid}"))
    conn.commit()
    create_manual_exam(conn, 10, "Parcial real", 1_900_000_000, [1], None)
    conn.close()
    return "Pregunta real 1"


def assert_no_demo(text: str, where: str):
    assert COURSE not in text, where
    for stem in STEMS:
        assert stem not in text, where


def test_demo_starts_without_network_or_key(denv):
    c = denv["client"]
    # Sin la edad ni la clave: la demo no las necesita.
    conn = connect(denv["db_path"])
    conn.execute("DELETE FROM kv WHERE key = 'age_gate'")
    conn.commit()
    conn.close()

    r = c.post("/api/demo")
    assert r.status_code == 201
    assert r.json() == {"ok": True}
    state = c.get("/api/state").json()
    assert state["demo"] is True
    assert state["step"] == "facultad"
    assert denv["moodle_tokens"] == []
    assert denv["ai_keys"] == []

    items = c.post("/api/quiz", json={}).json()
    assert len(items) == 10
    assert all(i["stem"] in STEMS for i in items)
    assert denv["moodle_tokens"] == [] and denv["ai_keys"] == []


def test_demo_answers_never_touch_real_db(denv):
    c = denv["client"]
    assert c.post("/api/demo").status_code == 201
    before = dump(denv["db_path"])

    answer_some(c, 3)
    exam_id = c.get("/api/exams").json()[0]["id"]
    start = c.post("/api/mock/start", json={"exam_id": exam_id, "short": True})
    assert start.status_code == 200, start.text
    assert len(start.json()["items"]) == 6
    attempt = start.json()["attempt_id"]
    answers = {str(i["question_id"]): 0 for i in start.json()["items"]}
    assert c.post(f"/api/mock/{attempt}/finish", json={"answers": answers}).status_code == 200
    progress = c.get("/api/progress")
    assert progress.status_code == 200

    assert dump(denv["db_path"]) == before
    # Las respuestas quedaron en la base de ejemplo.
    conn = connect(demo_file(denv))
    assert conn.execute("SELECT COUNT(*) FROM answer").fetchone()[0] >= 3
    conn.close()


def test_demo_never_reaches_non_study_routes(denv, tmp_path):
    c = denv["client"]
    assert c.post("/api/demo").status_code == 201
    answer_some(c, 5)
    # Lo real se carga después (con la demo prendida), así las exportaciones tienen qué mostrar.
    real_stem = seed_real_questions(denv["db_path"])

    paths = sorted(
        route.path for route in denv["app"].routes
        if isinstance(route, APIRoute) and "GET" in route.methods and "{" not in route.path
        and ("GET", route.path) not in DEMO_ROUTES
    )
    for required in ("/api/export/anki.apkg", "/api/export/anki-semana.apkg", "/api/calendar.ics",
                     "/api/home", "/api/materials"):
        assert required in paths
    seen_real = False
    for path in paths:
        r = c.get(path)
        assert r.status_code < 500, path
        if path.endswith(".apkg"):
            assert r.status_code == 200, path
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                z.extract("collection.anki2", tmp_path)
            anki = sqlite3.connect(tmp_path / "collection.anki2")
            notes = " ".join(row[0] for row in anki.execute("SELECT flds FROM notes"))
            anki.close()
            (tmp_path / "collection.anki2").unlink()
            assert_no_demo(notes, path)
            seen_real = seen_real or real_stem in notes
        else:
            assert_no_demo(r.content.decode("utf-8", "replace"), path)
    # Las exportaciones leyeron la base real (el test no pasa en vacío).
    assert seen_real


def _uses_study_conn(dependant) -> bool:
    return any(getattr(d.call, "__name__", "") == "get_study_conn" or _uses_study_conn(d)
               for d in dependant.dependencies)


def test_demo_routes_are_exactly_the_study_list(denv):
    found = set()
    for route in denv["app"].routes:
        if isinstance(route, APIRoute) and _uses_study_conn(route.dependant):
            found.update((method, route.path) for method in route.methods)
    assert found == DEMO_ROUTES
    for _, path in found:
        for word in ("export", ".ics", "calendar", "sync", "login", "settings"):
            assert word not in path
        assert "ai" not in path.strip("/").split("/")


def test_login_removes_demo_and_its_answers(denv):
    c = denv["client"]
    assert c.post("/api/demo").status_code == 201
    answer_some(c, 2)
    assert c.post("/api/site", json={"site_id": "utn-frba"}).status_code == 200
    passport = start_login(c)
    assert c.post("/api/login/callback", json={"url": callback_url(passport)}).status_code == 200

    assert c.get("/api/state").json()["demo"] is False
    assert not demo_file(denv).exists()
    assert not denv["db_path"].with_name("demo.db-journal").exists()
    items = c.post("/api/quiz", json={}).json()
    assert all(i["stem"] not in STEMS for i in items)
    conn = connect(denv["db_path"])
    assert conn.execute("SELECT COUNT(*) FROM course WHERE site_id = 'demo'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM kv WHERE key = 'demo_active'").fetchone()[0] == 0
    conn.close()


def test_demo_off_even_if_file_cannot_be_deleted(denv, monkeypatch):
    c = denv["client"]
    assert c.post("/api/demo").status_code == 201
    real_unlink = Path.unlink

    def stubborn_unlink(self, *args, **kwargs):
        if self.name.startswith("demo.db"):
            raise PermissionError("archivo en uso")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", stubborn_unlink)
    assert c.post("/api/site", json={"site_id": "utn-frba"}).status_code == 200
    passport = start_login(c)
    assert c.post("/api/login/callback", json={"url": callback_url(passport)}).status_code == 200

    assert demo_file(denv).exists()   # no se pudo borrar...
    assert c.get("/api/state").json()["demo"] is False   # ...pero la demo está apagada
    # La práctica lee la base real: sin preguntas todavía (ninguna de ejemplo).
    assert c.post("/api/quiz", json={}).json() == []


def test_leftover_demo_file_removed_at_startup(tmp_path):
    db_path = tmp_path / "estudia.db"
    connect(db_path).close()
    for name in DEMO_FILES:
        (tmp_path / name).write_bytes(b"resto")
    create_app(db_path=db_path, moodle_client_factory=lambda *a: None, ai_provider_factory=make_provider,
               open_browser=lambda url: None, web_dist=tmp_path / "no-existe", port=PORT)
    for name in DEMO_FILES:
        assert not (tmp_path / name).exists(), name
    assert db_path.exists()


def test_demo_refused_once_campus_connected(denv):
    c = logged_in(denv)
    r = c.post("/api/demo")
    assert r.status_code == 409
    assert r.json()["detail"] == DEMO_CONNECTED_TEXT
    assert not demo_file(denv).exists()
    assert c.get("/api/state").json()["demo"] is False


def test_demo_start_is_idempotent(denv):
    c = denv["client"]
    assert c.post("/api/demo").status_code == 201
    answer_some(c, 2)
    assert c.post("/api/demo").status_code == 201
    conn = connect(demo_file(denv))
    assert conn.execute("SELECT COUNT(*) FROM answer").fetchone()[0] == 2
    conn.close()


def test_demo_requires_local_origin(denv):
    c = denv["client"]
    r = c.post("/api/demo", headers={"origin": "https://evil.com"})
    assert r.status_code == 403
    assert not demo_file(denv).exists()
    assert c.post("/api/demo", headers={"origin": f"http://127.0.0.1:{PORT}"}).status_code == 201


def test_real_db_flag_is_the_only_trace(denv):
    """Prender y apagar la demo deja la base real igual, byte a byte en su volcado."""
    c = denv["client"]
    before = dump(denv["db_path"])
    assert c.post("/api/demo").status_code == 201
    answer_some(c, 3)
    after_start = dump(denv["db_path"])
    assert sorted(after_start) == sorted(before + ["INSERT INTO \"kv\" VALUES('demo_active','1');"])
    conn = connect(denv["db_path"])
    demo.remove_demo(conn, denv["db_path"])
    conn.close()
    assert dump(denv["db_path"]) == before
    for name in DEMO_FILES:
        assert not denv["db_path"].with_name(name).exists()


def test_demo_removed_between_check_and_open_is_never_recreated(denv, monkeypatch):
    """La carrera: un pedido de estudio ve la demo prendida, el login la borra, y recién ahí
    el pedido abre demo.db. No se crea una demo.db vacía: el pedido lee la base real."""
    import estudia.server.app as server_app

    c = denv["client"]
    assert c.post("/api/demo").status_code == 201
    real_check = server_app.demo_active

    def check_then_login_removes(conn, db_path):
        active = real_check(conn, db_path)
        other = connect(db_path)
        demo.remove_demo(other, db_path)
        other.close()
        return active

    monkeypatch.setattr(server_app, "demo_active", check_then_login_removes)
    r = c.post("/api/quiz", json={})
    assert r.status_code == 200
    assert r.json() == []   # la base real, sin preguntas de ejemplo
    for name in DEMO_FILES:
        assert not denv["db_path"].with_name(name).exists(), name


def test_corrupt_demo_db_turns_the_demo_off_and_uses_the_real_db(denv, caplog):
    """Una demo.db rota (no es SQLite) no traba la práctica: la demo se apaga y se sigue con la real."""
    c = denv["client"]
    assert c.post("/api/demo").status_code == 201
    garbage = b"basura que no es una base de SQLite " * 200
    demo_file(denv).write_bytes(garbage)

    with caplog.at_level("INFO"):
        r = c.post("/api/quiz", json={})
    assert r.status_code == 200
    assert r.json() == []                       # la base real, vacía
    assert c.get("/api/state").json()["demo"] is False
    for name in DEMO_FILES:
        assert not denv["db_path"].with_name(name).exists(), name
    # El log dice el tipo del error, nunca el contenido del archivo.
    assert "DatabaseError" in caplog.text
    assert "basura" not in caplog.text


def test_empty_demo_db_turns_the_demo_off_and_uses_the_real_db(denv, caplog):
    """Una demo.db vacía (0 bytes) es SQLite válido pero sin preguntas: la demo se apaga como con
    una rota, y se sigue con la real (no una práctica vacía con la demo prendida)."""
    c = denv["client"]
    assert c.post("/api/demo").status_code == 201
    demo_file(denv).write_bytes(b"")

    with caplog.at_level("INFO"):
        r = c.post("/api/quiz", json={})
    assert r.status_code == 200
    assert r.json() == []                       # la base real, vacía
    assert c.get("/api/state").json()["demo"] is False
    for name in DEMO_FILES:
        assert not denv["db_path"].with_name(name).exists(), name


class _Raising:
    def __init__(self, exc):
        self.exc = exc

    def execute(self, *args):
        raise self.exc


def test_has_questions_only_treats_missing_table_and_garbage_as_empty():
    from estudia.server.app import _has_questions

    assert _has_questions(_Raising(sqlite3.OperationalError("no such table: question"))) is False
    assert _has_questions(_Raising(sqlite3.DatabaseError("file is not a database"))) is False
    with pytest.raises(sqlite3.OperationalError):
        _has_questions(_Raising(sqlite3.OperationalError("database is locked")))


def test_busy_demo_db_is_not_removed(denv, monkeypatch):
    """Una demo.db bloqueada no es una vacía: el pedido falla, pero la demo sigue ahí."""
    import estudia.server.app as server_app

    c = denv["client"]
    assert c.post("/api/demo").status_code == 201

    def locked(conn):
        raise sqlite3.OperationalError("database is locked")

    real = server_app._has_questions
    monkeypatch.setattr(server_app, "_has_questions", locked)
    with pytest.raises(sqlite3.OperationalError):
        c.post("/api/quiz", json={})
    monkeypatch.setattr(server_app, "_has_questions", real)
    assert demo_file(denv).exists()
    assert c.get("/api/state").json()["demo"] is True
