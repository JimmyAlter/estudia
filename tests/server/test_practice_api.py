"""API de práctica, versión 2: formato por ítem, sesión del plan, confianza y respuesta escrita."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from estudia.db import connect
from estudia.generation import verify
from estudia.server.app import create_app
from estudia.study.practice import record_answer
from test_app import BASE, PORT, SITE_ID, FakeMoodle, make_provider

DAY = 86400
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
    conn.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (1, 10, 'rectas', 'Rectas')")
    conn.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (2, 10, 'planos', 'Planos')")
    conn.commit()
    yield {"client": TestClient(app, base_url=BASE), "conn": conn}
    conn.close()


def add_question(conn, qid, topic_id=1, check_expr=None, options=("a", "b", "c"), correct=0):
    conn.execute(
        "INSERT INTO question(id, topic_id, stem, options_json, correct_index, explanation, common_error,"
        " check_expr, verified, retired, dedupe_key) VALUES (?, ?, ?, ?, ?, 'porque', 'el error', ?, 1, 0, ?)",
        (qid, topic_id, f"Pregunta {qid}", json.dumps(list(options)), correct, check_expr, f"k{qid}"),
    )
    conn.commit()


def answers(conn):
    return [dict(r) for r in conn.execute(
        "SELECT question_id, chosen_index, correct, confidence, mode, option_order_json, typed_text,"
        " latency_ms, options_revealed_ms FROM answer ORDER BY id")]


def typed(client, text, qid=1, confidence=2):
    return client.post("/api/answer", json={
        "question_id": qid, "typed_text": text, "confidence": confidence, "mode": "typed",
        "latency_ms": 5000, "shown_order": []})


def test_typed_equivalent_forms_accepted(env):
    c, conn = env["client"], env["conn"]
    add_question(conn, 1, check_expr="2*sqrt(2)", options=("2√2", "√2", "4", "2"))
    for text in ["2√2", "2*sqrt(2)", "2 raiz 2", "2.828", "2.83"]:
        r = typed(c, text)
        assert r.status_code == 200, (text, r.text)
        fb = r.json()
        assert (fb["typed_ok"], fb["correct"], fb["chosen_index"]) == (True, True, None), text
    for text in ["3", "2.82", "2.8"]:
        wrong = typed(c, text).json()
        assert (wrong["typed_ok"], wrong["correct"], wrong["correct_index"]) == (False, False, 0), text
    rows = answers(conn)
    assert [r["typed_text"] for r in rows] == ["2√2", "2*sqrt(2)", "2 raiz 2", "2.828", "2.83", "3", "2.82", "2.8"]
    assert {(r["mode"], r["chosen_index"], r["confidence"]) for r in rows} == {("typed", None, 2)}
    assert [r["correct"] for r in rows] == [1, 1, 1, 1, 1, 0, 0, 0]


def test_typed_near_misses_rejected(env):
    c, conn = env["client"], env["conn"]
    add_question(conn, 1, check_expr="100")
    add_question(conn, 2, check_expr="1")
    for qid, text in [(1, "99.99"), (1, "99.9"), (1, "100.1"), (2, "1+10^(-30)")]:
        assert typed(c, text, qid=qid).json()["typed_ok"] is False, text
    assert typed(c, "100.0", qid=1).json()["typed_ok"] is True


def test_typed_grading_unavailable_503_no_write(env, monkeypatch):
    c, conn = env["client"], env["conn"]
    add_question(conn, 1, check_expr="2")
    # Sin worker y sin que arranque: determinista. Con `EVAL_TIMEOUT_S = 0` el worker (tibio de
    # otro test) podía contestar entre el envío y el `poll(0)` y la respuesta salía 200.
    verify._stop_worker()
    monkeypatch.setattr(verify, "_start_worker", lambda: False)
    r = typed(c, "2")
    assert r.status_code == 503
    assert r.json()["detail"] == "No pude corregir ahora, probá de nuevo"
    assert answers(conn) == []


def test_typed_text_max_length(env):
    c, conn = env["client"], env["conn"]
    add_question(conn, 1, check_expr="2")
    assert typed(c, "2" + " " * 199).status_code == 200
    assert typed(c, "2" + " " * 200).status_code == 422
    assert len(answers(conn)) == 1


def test_typed_unparseable_400_no_write(env):
    c, conn = env["client"], env["conn"]
    add_question(conn, 1, check_expr="2")
    for text in ["dos", "", "2√", "x + 1"]:
        r = typed(c, text)
        assert r.status_code == 400, text
        assert r.json()["detail"] == "No pude leer la expresión"
    assert answers(conn) == []


def test_typed_without_check_expr_400_no_write(env):
    c, conn = env["client"], env["conn"]
    add_question(conn, 1)
    r = typed(c, "2")
    assert r.status_code == 400
    assert r.json()["detail"] == "Esta pregunta no admite respuesta escrita"
    # Tampoco con una expresión de control que no se puede usar (infinita o con letras).
    add_question(conn, 2, check_expr="1/0")
    add_question(conn, 3, check_expr="x+1")
    for qid in (2, 3):
        r = typed(c, "2", qid=qid)
        assert (r.status_code, r.json()["detail"]) == (400, "Esta pregunta no admite respuesta escrita")
    assert answers(conn) == []


def test_shown_order_maps_to_original(env):
    c, conn = env["client"], env["conn"]
    add_question(conn, 1, options=("a", "b", "c"), correct=0)
    body = {"question_id": 1, "confidence": 2, "mode": "mcq", "latency_ms": 3000, "shown_order": [2, 0, 1]}
    right = c.post("/api/answer", json={**body, "chosen_index": 1}).json()   # la 2.ª mostrada es "a"
    assert (right["correct"], right["chosen_index"], right["correct_index"]) == (True, 0, 0)
    wrong = c.post("/api/answer", json={**body, "chosen_index": 0}).json()   # la 1.ª mostrada es "c"
    assert (wrong["correct"], wrong["chosen_index"]) == (False, 2)
    rows = answers(conn)
    assert [(r["chosen_index"], r["correct"], json.loads(r["option_order_json"])) for r in rows] == [
        (0, 1, [2, 0, 1]), (2, 0, [2, 0, 1])]
    # Un orden que no es una permutación de las opciones no se registra.
    for order in ([0, 1], [0, 0, 1], [0, 1, 7]):
        assert c.post("/api/answer", json={**body, "chosen_index": 0, "shown_order": order}).status_code == 400
    assert len(answers(conn)) == 2


def test_session_quiz_review_then_focus(env):
    c, conn = env["client"], env["conn"]
    now = int(time.time())
    for qid in (1, 2, 3, 4, 5):
        add_question(conn, qid, topic_id=1)
    for qid in (11, 12):
        add_question(conn, qid, topic_id=2)
    record_answer(conn, 11, 0, now - 30 * DAY)   # vencida, tema 2
    record_answer(conn, 5, 0, now - 25 * DAY)    # vencida, tema 1 (el foco)
    conn.execute(
        "INSERT INTO plan_session(id, course_id, topic_id, exam_id, starts_at, minutes, done, kind, topics_json)"
        " VALUES (1, 10, 1, NULL, ?, 50, 0, 'mixed_focus', '[1, 2]')",
        (now,),
    )
    conn.commit()

    r = c.post("/api/quiz", json={"session_id": 1, "size": 10})
    assert r.status_code == 200
    items = r.json()
    # 10 de 50 minutos de repaso: 2 ítems de repaso mezclado, después el foco (sin la 12, del otro tema).
    assert [i["phase"] for i in items] == ["review"] * 2 + ["focus"] * 4
    assert [i["question_id"] for i in items[:2]] == [11, 5]
    assert {i["topic"] for i in items[:2]} == {"Planos", "Rectas"}
    assert sorted(i["question_id"] for i in items[2:]) == [1, 2, 3, 4]
    assert {i["format"] for i in items[:2]} == {"think_first"}   # ya vistas, sin check_expr
    assert {i["format"] for i in items[2:]} == {"mcq"}           # nuevas
    assert c.post("/api/quiz", json={"session_id": 99}).status_code == 404


def test_quiz_topic_ids_and_size(env):
    c, conn = env["client"], env["conn"]
    for qid in (1, 2, 3):
        add_question(conn, qid, topic_id=1)
    add_question(conn, 11, topic_id=2)
    r = c.post("/api/quiz", json={"topic_ids": [1, 2], "size": 50})
    assert sorted(i["question_id"] for i in r.json()) == [1, 2, 3, 11]
    assert len(c.post("/api/quiz", json={"topic_ids": [1, 2], "size": 2}).json()) == 2
    assert sorted(i["question_id"] for i in c.post("/api/quiz", json={"topic_ids": [2]}).json()) == [11]
    item = c.post("/api/quiz", json={"topic_id": 2}).json()[0]
    assert (item["format"], item["phase"], item["relearning"]) == ("mcq", "focus", None)
    assert c.post("/api/quiz", json={"size": 0}).status_code == 422
    assert c.post("/api/quiz", json={"topic_ids": [999]}).status_code == 404
    assert c.post("/api/quiz", json={"topic_ids": [1, 999]}).status_code == 404
    assert c.post("/api/quiz", json={"topic_ids": [1] * 51}).status_code == 422
    assert c.post("/api/quiz", json={"topic_ids": [1] * 50}).status_code == 200
    conn.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (3, 20, 'otra', 'Otra materia')")
    conn.commit()
    add_question(conn, 21, topic_id=3)
    r = c.post("/api/quiz", json={"topic_ids": [1, 3]})
    assert r.status_code == 400
    assert r.json()["detail"] == "Elegí temas de una sola materia"


@pytest.mark.parametrize("correct, confidence, style, requeue", [
    (False, 2, "alarm", True),
    (False, 1, "full", True),
    (False, 0, "full", True),
    (True, 2, "short", False),
    (True, 1, "doubt", False),
    (True, 0, "doubt", True),    # acierto de suerte: vuelve en la sesión
])
def test_feedback_style_matrix(env, correct, confidence, style, requeue):
    c, conn = env["client"], env["conn"]
    add_question(conn, 1, correct=0)
    r = c.post("/api/answer", json={"question_id": 1, "chosen_index": 0 if correct else 1,
                                    "confidence": confidence, "mode": "mcq", "latency_ms": 4000,
                                    "shown_order": [0, 1, 2]})
    fb = r.json()
    assert (fb["correct"], fb["feedback_style"], fb["requeue"]) == (correct, style, requeue)
    if requeue:
        assert (fb["next_label"], fb["relearn_progress"]) == ("Vuelve al final de la sesión", "0 de 3 días")
    else:
        assert fb["next_label"].startswith(("Vuelve ", "La sabés: vuelve "))
    assert fb["typed_ok"] is None


def test_legacy_answer_body_still_works(env):
    c, conn = env["client"], env["conn"]
    add_question(conn, 1, correct=0)
    right = c.post("/api/answer", json={"question_id": 1, "chosen_index": 0})
    assert right.status_code == 200
    assert (right.json()["correct"], right.json()["feedback_style"]) == (True, "doubt")   # como "Me parece"
    wrong = c.post("/api/answer", json={"question_id": 1, "chosen_index": 2}).json()
    assert (wrong["correct"], wrong["feedback_style"], wrong["chosen_index"]) == (False, "full", 2)
    rows = answers(conn)
    assert [(r["confidence"], r["mode"], r["option_order_json"], r["latency_ms"]) for r in rows] == [
        (1, "mcq", None, None), (1, "mcq", None, None)]
    assert c.post("/api/answer", json={"question_id": 1, "chosen_index": 9}).status_code == 400


def test_answer_payload_is_validated(env):
    c, conn = env["client"], env["conn"]
    add_question(conn, 1, check_expr="2")
    base = {"question_id": 1, "chosen_index": 0, "confidence": 2, "mode": "mcq"}
    for bad in ({"confidence": 3}, {"mode": "mock"}, {"mode": "pretest"}, {"latency_ms": -1},
                {"options_revealed_ms": -5}, {"chosen_index": None},
                {"mode": "typed", "typed_text": "2"}):   # escrita con opción elegida
        assert c.post("/api/answer", json={**base, **bad}).status_code == 400, bad
    ok = c.post("/api/answer", json={**base, "mode": "mcq_think_first", "options_revealed_ms": 1200,
                                     "latency_ms": 6000})
    assert ok.status_code == 200
    then = c.post("/api/answer", json={**base, "mode": "typed_then_mcq", "typed_text": "no sé"})
    assert then.status_code == 200
    rows = answers(conn)
    assert [(r["mode"], r["options_revealed_ms"], r["typed_text"]) for r in rows] == [
        ("mcq_think_first", 1200, None), ("typed_then_mcq", None, "no sé")]


def test_writes_need_same_origin(env):
    c, conn = env["client"], env["conn"]
    add_question(conn, 1, check_expr="2")
    body = {"question_id": 1, "chosen_index": 0, "confidence": 2, "mode": "mcq"}
    assert c.post("/api/quiz", json={}, headers=EVIL).status_code == 403
    assert c.post("/api/answer", json=body, headers=EVIL).status_code == 403
    assert c.post("/api/answer", json={"question_id": 1, "typed_text": "2", "confidence": 2, "mode": "typed"},
                  headers=EVIL).status_code == 403
    assert answers(conn) == []
    assert c.post("/api/quiz", json={}, headers={"origin": BASE}).status_code == 200
    assert c.post("/api/answer", json=body, headers={"origin": BASE}).status_code == 200
    assert len(answers(conn)) == 1


def test_typed_to_a_flagged_question_400_points_to_the_options(env):
    c, conn = env["client"], env["conn"]
    add_question(conn, 1, check_expr="sqrt(2)", options=("√2", "1.41", "2"))
    conn.execute("UPDATE question SET typed_safe = 0 WHERE id = 1")
    conn.commit()
    r = typed(c, "1.41")
    assert (r.status_code, r.json()["detail"]) == (
        400, "Esta pregunta va con opciones. Tocá «Mostrame opciones».")
    assert answers(conn) == []


def add_summary(conn, material_id=1, summary="# Funciones lineales\n## Raíces\n## Pendiente\n"):
    conn.execute("INSERT INTO section(id, course_id, num, name, visible) VALUES (1001, 10, 1, 's', 1)")
    conn.execute("INSERT INTO material(id, section_id, cmid, kind, title, ref, status, summary_md, updated_at) "
                 "VALUES (?, 1001, 1, 'youtube', 'Rectas', 'r', 'done', ?, 0)", (material_id, summary))
    conn.commit()


def test_recall_route_marks_missing_headings(env):
    c, conn = env["client"], env["conn"]
    add_summary(conn)
    r = c.post("/api/materials/1/recall", json={"text": "la funcion lineal y sus raices"})
    assert r.status_code == 200, r.text
    assert r.json() == {"headings": [{"title": "Funciones lineales", "mentioned": True},
                                     {"title": "Raíces", "mentioned": True},
                                     {"title": "Pendiente", "mentioned": False}]}


def test_recall_route_does_not_store_text(env):
    c, conn = env["client"], env["conn"]
    add_summary(conn)
    secret = "mi recuerdo secreto zanahoria"
    assert c.post("/api/materials/1/recall", json={"text": secret}).status_code == 200
    assert secret not in "\n".join(conn.iterdump())


def test_recall_route_404_without_summary(env):
    c, conn = env["client"], env["conn"]
    assert c.post("/api/materials/1/recall", json={"text": "x"}).status_code == 404
    add_summary(conn, summary="   ")
    assert c.post("/api/materials/1/recall", json={"text": "x"}).status_code == 404


def test_recall_route_guarded_and_422_does_not_echo(env):
    c, conn = env["client"], env["conn"]
    add_summary(conn)
    assert c.post("/api/materials/1/recall", json={"text": "x"}, headers=EVIL).status_code == 403
    long_text = "zanahoria " * 600
    r = c.post("/api/materials/1/recall", json={"text": long_text})
    assert r.status_code == 422
    assert "zanahoria" not in r.text
    assert c.post("/api/materials/1/recall", json={"text": "a" * 5000}).status_code == 200


def test_explain_route_saves_match_and_rejects_repeat(env):
    c, conn = env["client"], env["conn"]
    add_question(conn, 1, correct=0)
    r = c.post("/api/answer", json={"question_id": 1, "chosen_index": 0, "confidence": 1})
    fb = r.json()
    assert fb["explain_prompt"] is True and isinstance(fb["answer_id"], int)
    url = f"/api/answers/{fb['answer_id']}/explain"
    assert c.post(url, json={"same": True}, headers=EVIL).status_code == 403
    assert c.post(url, json={"same": "x"}).status_code == 422
    assert c.post(url, json={"same": False}).json() == {"ok": True}
    assert conn.execute("SELECT explain_match FROM answer").fetchone()[0] == 0
    assert c.post(url, json={"same": True}).status_code == 400
    assert c.post("/api/answers/999/explain", json={"same": True}).status_code == 404
