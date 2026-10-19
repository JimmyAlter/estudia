"""Lo propio (origin='parcial_propio') no sale de la compu: ninguna exportación lo lleva."""
import inspect
import json
import re
import sqlite3
import time
import zipfile
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

from estudia.config import data_dir
from estudia.db import connect
from estudia.server.app import create_app
from estudia.study.anki import EXPORTABLE, build_apkg, weekly_question_ids
from estudia.study.exams import create_manual_exam

PORT = 8123
BASE = f"http://127.0.0.1:{PORT}"


def _no_network(*args, **kwargs):
    raise AssertionError("una exportación no debería tocar el campus ni la IA")

SECRET_STEM = "ENUNCIADO-SECRETO"
SECRET_NAME = "PARCIAL-SECRETO"
STYLE_IDS = (101, 102, 103)
REGULAR_IDS = (1, 2, 3, 4, 5)


@pytest.fixture
def env():
    base = data_dir()
    db_path = base / "estudia.db"
    app = create_app(
        db_path=db_path,
        moodle_client_factory=_no_network,
        ai_provider_factory=_no_network,
        open_browser=lambda url: None,
        web_dist=base / "no-existe",
        port=PORT,
    )
    conn = connect(db_path)
    now = int(time.time())
    conn.execute("INSERT INTO course(id, site_id, fullname, shortname) VALUES (10, 's', 'Análisis', 'AM')")
    conn.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (1, 10, 'lim', 'Límites')")
    conn.execute("INSERT INTO section(id, course_id, num, name, visible) VALUES (1, 10, 1, 'S1', 1)")
    conn.execute("INSERT INTO material(id, section_id, cmid, kind, title, ref, status)"
                 " VALUES (1, 1, 1, 'youtube', 'Video 1', 'abcdefghijk', 'done')")
    exam_id = create_manual_exam(conn, 10, "Primer parcial", now + 3 * 86400, [1], None, now=now)
    conn.execute(
        "INSERT INTO past_exam(exam_id, name, kind, path, size, sha256, status, created_at)"
        " VALUES (?, ?, 'pdf', 'parciales/1.pdf', 10, 'abc', 'done', ?)",
        (exam_id, f"{SECRET_NAME}-2024.pdf", now))
    options = json.dumps(["1", "2", "3", "4"])
    for qid in REGULAR_IDS:
        conn.execute(
            "INSERT INTO question(id, material_id, topic_id, stem, options_json, correct_index, explanation,"
            " verified, retired, dedupe_key) VALUES (?, 1, 1, ?, ?, 0, 'Porque sí', 1, 0, ?)",
            (qid, f"Pregunta normal {qid}", options, f"k{qid}"))
    for qid in STYLE_IDS:
        # Con material a propósito: el filtro no puede depender de que falte el join.
        conn.execute(
            "INSERT INTO question(id, material_id, topic_id, stem, options_json, correct_index, explanation,"
            " verified, retired, dedupe_key, origin) VALUES (?, 1, 1, ?, ?, 0, 'Porque sí', 1, 0, ?,"
            " 'parcial_propio')",
            (qid, f"{SECRET_STEM} {qid}", options, f"k{qid}"))
    for qid in REGULAR_IDS + STYLE_IDS:
        conn.execute("INSERT INTO review_state(card_key, stability, difficulty, state, due, last_review)"
            " VALUES (?, 3.0, 5.0, 2, ?, ?)", (f"q:{qid}", now - 60, now - 86400))
    conn.commit()
    yield {"client": TestClient(app, base_url=BASE), "conn": conn, "app": app, "now": now}
    conn.close()


def _texts(data: bytes, tmp_path) -> str:
    """Lo legible de una respuesta: un .apkg se descomprime y se lee la tabla notes."""
    if data[:2] != b"PK":
        return data.decode("utf-8", "replace")
    with zipfile.ZipFile(BytesIO(data)) as z:
        z.extract("collection.anki2", tmp_path)
    col = sqlite3.connect(tmp_path / "collection.anki2")
    try:
        return "\n".join(f"{flds} {tags}" for flds, tags in col.execute("SELECT flds, tags FROM notes"))
    finally:
        col.close()


def _export_paths(app) -> list[str]:
    """Rutas GET sin parámetros de ruta que exportan o descargan algo."""
    paths = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if "GET" not in (getattr(route, "methods", None) or ()) or "{" in path:
            continue
        if "export" in path or path.endswith((".ics", ".apkg")):
            paths.append(path)
    return sorted(paths)


def test_parcial_propio_never_exported(env, tmp_path):
    walked = _export_paths(env["app"])
    for expected in ("/api/export/anki.apkg", "/api/export/anki-semana.apkg", "/api/calendar.ics"):
        assert expected in walked
    for path in walked:
        r = env["client"].get(path)
        assert r.status_code == 200, path
        text = _texts(r.content, tmp_path)
        assert SECRET_STEM not in text and SECRET_NAME not in text, path
    # El .apkg completo lleva las preguntas normales: el test no pasa en vacío.
    full = _texts(env["client"].get("/api/export/anki.apkg").content, tmp_path)
    assert "Pregunta normal 1" in full


def test_every_download_route_is_classified(env):
    """Una ruta GET nueva que devuelve un archivo tiene que quedar a la vista de este test."""
    covered = set(_export_paths(env["app"]))
    downloads = set()
    for route in env["app"].routes:
        if "GET" not in (getattr(route, "methods", None) or ()):
            continue
        endpoint = getattr(route, "endpoint", None)
        source = ""
        try:
            source = inspect.getsource(endpoint)
        except (OSError, TypeError):
            pass
        if re.search(r"FileResponse|StreamingResponse|Content-Disposition|media_type=", source):
            downloads.add(route.path)
    assert downloads <= covered, f"rutas de descarga sin cubrir: {sorted(downloads - covered)}"


def test_weekly_deck_excludes_style_questions_even_on_exam_topics(env, tmp_path):
    ids = weekly_question_ids(env["conn"], env["now"])
    assert ids == set(REGULAR_IDS)
    text = _texts(build_apkg(env["conn"], None, ids), tmp_path)
    assert SECRET_STEM not in text


def test_exportable_filter_does_not_depend_on_material_join(env, tmp_path):
    assert EXPORTABLE == "q.origin IS NULL"
    # Aun pidiéndolas por id, las de estilo no salen.
    text = _texts(build_apkg(env["conn"], None, set(STYLE_IDS) | {1}), tmp_path)
    assert SECRET_STEM not in text and "Pregunta normal 1" in text
    with pytest.raises(LookupError):
        build_apkg(env["conn"], None, set(STYLE_IDS))


def test_past_exam_names_not_in_get_responses(env):
    for path in ("/api/exams", "/api/agenda", "/api/home", "/api/materials", "/api/state"):
        r = env["client"].get(path)
        assert r.status_code == 200, path
        assert SECRET_NAME not in r.text and SECRET_STEM not in r.text, path
