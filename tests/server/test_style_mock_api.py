"""API del simulacro con el formato de la cátedra: 409 mientras faltan preguntas."""
import json

import pytest
from fastapi.testclient import TestClient

from estudia.config import data_dir
from estudia.db import connect
from estudia.server.app import create_app
from estudia.study.exam_style import ExamStyle, StyleGroup, save_style
from estudia.study.exams import create_manual_exam
from test_app import BASE, PORT, FakeMoodle, make_provider

NOW = 1_790_000_000


def seed(c):
    c.execute("INSERT INTO course(id, site_id, fullname, shortname) VALUES (10, 's', 'Análisis', 'AM')")
    c.execute("INSERT INTO section(id, course_id, num, name, visible) VALUES (1, 10, 1, 'S', 1)")
    c.execute("INSERT INTO material(id, section_id, cmid, kind, title, ref, status)"
              " VALUES (1, 1, 101, 'youtube', 'V', 'yt1', 'done')")
    c.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (1, 10, 't1', 'Función lineal')")
    c.execute("INSERT INTO material_topic(material_id, topic_id) VALUES (1, 1)")
    c.commit()


def make_exam(conn, groups):
    exam_id = create_manual_exam(conn, 10, "Primer parcial", NOW + 86400, [1], None, now=NOW)
    save_style(conn, exam_id, ExamStyle(
        groups=[StyleGroup(kind=k, count=n, points_each=p) for k, n, p in groups], topic_ids=[1]))
    return exam_id


def add_campus(conn, qid):
    conn.execute(
        "INSERT INTO question(id, material_id, topic_id, stem, options_json, correct_index, explanation,"
        " common_error, verified, retired, dedupe_key) VALUES (?, 1, 1, ?, ?, 0, 'e', 'c', 1, 0, ?)",
        (qid, f"campus {qid}", json.dumps(["9", "6", "3", "27"]), f"c{qid}"))
    conn.commit()


def add_style(conn, qid, exam_id, kind):
    conn.execute(
        "INSERT INTO question(id, topic_id, stem, options_json, correct_index, explanation, common_error,"
        " verified, retired, origin, style_exam_id, style_kind, dedupe_key)"
        " VALUES (?, 1, ?, ?, 0, 'e', 'c', 1, 0, 'parcial_propio', ?, ?, ?)",
        (qid, f"estilo {qid}", json.dumps(["9", "6", "3", "27"]), exam_id, kind, f"s{qid}"))
    conn.commit()


@pytest.fixture
def client():
    base = data_dir()
    db_path = base / "estudia.db"
    app = create_app(db_path=db_path, moodle_client_factory=lambda url, token: FakeMoodle(url, token),
                     ai_provider_factory=make_provider, open_browser=lambda url: None,
                     web_dist=base / "no-existe", port=PORT)
    c = connect(db_path)
    seed(c)
    yield TestClient(app, base_url=BASE), c
    c.close()


def test_style_mock_not_ready_409(client):
    http, conn = client
    exam_id = make_exam(conn, [("opcion_multiple", 7, 1)])
    for qid in range(1, 6):
        add_campus(conn, qid)
    r = http.post("/api/mock/start", json={"exam_id": exam_id, "style": True})
    assert r.status_code == 409 and "Todavía faltan 2 preguntas" in r.json()["detail"]
    # El simulacro de siempre sigue andando.
    assert http.post("/api/mock/start", json={"exam_id": exam_id}).status_code == 200
    add_style(conn, 50, exam_id, "opcion_multiple")
    add_style(conn, 51, exam_id, "opcion_multiple")
    r = http.post("/api/mock/start", json={"exam_id": exam_id, "style": True})
    assert r.status_code == 200
    body = r.json()
    assert body["style"] is True and len(body["items"]) == 7
    assert all(item["points"] == 1.0 for item in body["items"])
    finished = http.post(f"/api/mock/{body['attempt_id']}/finish",
                         json={"answers": {str(i["question_id"]): 0 for i in body["items"]}})
    assert finished.status_code == 200 and finished.json()["grade"] == 10.0
    r = http.post("/api/mock/start", json={"exam_id": exam_id, "style": True, "short": True})
    assert r.status_code == 400


def test_style_mock_start_rejects_foreign_origin(client):
    http, conn = client
    exam_id = make_exam(conn, [("opcion_multiple", 1, 1)])
    r = http.post("/api/mock/start", json={"exam_id": exam_id, "style": True},
                  headers={"origin": "https://evil.com"})
    assert r.status_code == 403
    assert conn.execute("SELECT COUNT(*) FROM mock_attempt").fetchone()[0] == 0
