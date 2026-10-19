"""API de "3 preguntas para arrancar"."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from estudia.db import connect
from estudia.server.app import create_app
from test_app import BASE, PORT, SITE_ID, FakeMoodle, make_provider

EVIL = {"origin": "https://evil.com"}


@pytest.fixture
def env(tmp_path):
    db_path = tmp_path / "estudia.db"
    app = create_app(
        db_path=db_path,
        moodle_client_factory=lambda url, token: FakeMoodle(url, token),
        ai_provider_factory=make_provider,
        open_browser=lambda url: None,
        web_dist=tmp_path / "no-existe",
        port=PORT,
    )
    conn = connect(db_path)
    conn.execute("INSERT INTO course(id, site_id, fullname, shortname) VALUES (10, ?, 'Álgebra', 'ALG')", (SITE_ID,))
    conn.execute("INSERT INTO section(id, course_id, num, name, visible) VALUES (1001, 10, 1, 's', 1)")
    conn.execute("INSERT INTO material(id, section_id, cmid, kind, title, ref, status, updated_at) "
                 "VALUES (1, 1001, 1, 'youtube', 'Rectas', 'r', 'done', ?)", (int(time.time()) - 3600,))
    conn.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (1, 10, 'rectas', 'Rectas')")
    conn.execute("INSERT INTO material_topic(material_id, topic_id) VALUES (1, 1)")
    for qid in range(1, 5):
        conn.execute(
            "INSERT INTO question(id, material_id, topic_id, stem, options_json, correct_index, explanation,"
            " source_seconds, verified, retired) VALUES (?, 1, 1, ?, ?, 0, 'porque', ?, 1, 0)",
            (qid, f"Pregunta {qid}", json.dumps(["a", "b", "c"]), qid * 60))
    conn.commit()
    yield {"client": TestClient(app, base_url=BASE), "conn": conn}
    conn.close()


def test_pretest_flow(env):
    c, conn = env["client"], env["conn"]
    offer = c.get("/api/home").json()["pretest"]
    assert offer == {"material_id": 1, "title": "Rectas", "course": "Álgebra", "kind": "youtube"}

    items = c.post("/api/pretest/1/start").json()
    assert [i["question_id"] for i in items] == [1, 3, 4]
    assert all(i["format"] == "mcq" for i in items)

    r = c.post("/api/pretest/answer", json={"question_id": 1, "chosen_index": 2, "shown_order": [2, 1, 0]})
    assert r.status_code == 200, r.text
    assert (r.json()["correct"], r.json()["chosen_index"]) == (True, 0)
    row = conn.execute("SELECT mode, confidence FROM answer").fetchone()
    assert tuple(row) == ("pretest", None)
    assert conn.execute("SELECT COUNT(*) FROM review_state").fetchone()[0] == 0

    assert c.post("/api/pretest/1/close").json() == {"ok": True}
    assert c.get("/api/home").json()["pretest"] is None


def test_pretest_close_404_for_unknown_class(env):
    c, conn = env["client"], env["conn"]
    assert c.post("/api/pretest/99/close").status_code == 404
    assert conn.execute("SELECT COUNT(*) FROM kv WHERE key = 'pretest_closed'").fetchone()[0] == 0


def test_pretest_start_404_without_three(env):
    c, conn = env["client"], env["conn"]
    conn.execute("UPDATE question SET verified = 0 WHERE id IN (1, 2)")
    conn.commit()
    assert c.post("/api/pretest/1/start").status_code == 404
    assert c.post("/api/pretest/99/start").status_code == 404


def test_pretest_answer_route_rejects_unverified(env):
    c, conn = env["client"], env["conn"]
    conn.execute("UPDATE question SET verified = 0 WHERE id = 2")
    conn.execute("UPDATE question SET retired = 1 WHERE id = 3")
    conn.commit()
    for qid in (2, 3, 99):
        r = c.post("/api/pretest/answer", json={"question_id": qid, "chosen_index": 0, "shown_order": None})
        assert r.status_code == 404, qid
    assert c.post("/api/pretest/answer", json={"question_id": 1, "chosen_index": 7}).status_code == 400
    assert conn.execute("SELECT COUNT(*) FROM answer").fetchone()[0] == 0


def test_pretest_routes_guarded(env):
    c = env["client"]
    assert c.post("/api/pretest/1/start", headers=EVIL).status_code == 403
    assert c.post("/api/pretest/1/close", headers=EVIL).status_code == 403
    r = c.post("/api/pretest/answer", headers=EVIL, json={"question_id": 1, "chosen_index": 0})
    assert r.status_code == 403


@pytest.mark.parametrize("change", [
    "UPDATE material SET status = 'pending' WHERE id = 1",
    "UPDATE section SET visible = 0 WHERE id = 1001",
])
def test_pretest_start_404_for_a_class_that_is_not_offered(env, change):
    c, conn = env["client"], env["conn"]
    assert c.post("/api/pretest/1/start").status_code == 200
    conn.execute(change)
    conn.commit()
    assert c.post("/api/pretest/1/start").status_code == 404
