"""API de agenda, exámenes, horarios, plan, simulacro, calendario y ajustes."""
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from estudia.db import connect
from estudia.server import app as app_module
from estudia.server.app import create_app
from test_app import BASE, PORT, SITE_ID, FakeMoodle, make_provider

TZ = ZoneInfo("America/Argentina/Buenos_Aires")
EVIL = {"origin": "https://evil.com"}


def ts(y, mo, d, h=0, mi=0) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp())


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
    conn.execute("INSERT INTO course(id, site_id, fullname, shortname) VALUES (20, ?, 'Física', 'FIS')", (SITE_ID,))
    conn.execute("INSERT INTO section(id, course_id, num, name, visible) VALUES (1, 10, 1, 'S1', 1)")
    conn.execute(
        "INSERT INTO material(id, section_id, cmid, kind, title, ref, status) "
        "VALUES (1, 1, 1001, 'youtube', 'Video 1', 'abcdefghijk', 'done')"
    )
    conn.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (1, 10, 'rectas', 'Rectas')")
    conn.execute("INSERT INTO material_topic(material_id, topic_id) VALUES (1, 1)")
    conn.commit()
    yield {"app": app, "client": TestClient(app, base_url=BASE), "conn": conn, "db_path": db_path}
    conn.close()


def add_questions(conn, n, start=1):
    for i in range(start, start + n):
        conn.execute(
            "INSERT INTO question(id, material_id, topic_id, stem, options_json, correct_index,"
            " explanation, verified, retired, dedupe_key) VALUES (?, 1, 1, ?, ?, 0, 'porque', 1, 0, ?)",
            (i, f"Pregunta {i}", json.dumps(["a", "b", "c"]), f"k{i}"),
        )
    conn.commit()


def add_exam(conn, at, name="Parcial", course_id=10, source="manual", max_grade=None):
    cur = conn.execute(
        "INSERT INTO exam(course_id, name, opens_at, source, max_grade) VALUES (?, ?, ?, ?, ?)",
        (course_id, name, at, source, max_grade),
    )
    conn.commit()
    return cur.lastrowid


@pytest.fixture
def replan_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(app_module, "replan", lambda conn, now: calls.append(now) or 0)
    return calls


# --- Agenda ------------------------------------------------------------------------------------

def test_agenda_merges_all_kinds_sorted(env):
    c, conn = env["client"], env["conn"]
    monday = ts(2030, 1, 7)  # lunes
    conn.execute("INSERT INTO class_slot(course_id, weekday, start_min, end_min, place) "
                 "VALUES (10, 0, 540, 660, 'Aula 302')")
    add_exam(conn, ts(2030, 1, 7, 8), "Primer parcial", course_id=20)
    conn.executemany(
        "INSERT INTO deadline(course_id, kind, ext_id, name, due_at, url) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (10, "assign", 1, "TP 1", ts(2030, 1, 7, 10), "https://campus/tp1"),
            (10, "event", 2, "Charla", ts(2030, 1, 7, 12), None),
            # 01:00 del martes local: en UTC todavía es lunes 04:00, pero va al martes.
            (10, "assign", 3, "TP 2", ts(2030, 1, 8, 1), None),
            # domingo 23:30 local: en UTC ya es lunes, pero va al domingo.
            (10, "assign", 4, "TP 3", ts(2030, 1, 13, 23, 30), None),
            # domingo anterior a las 23:59: fuera de la semana.
            (10, "assign", 5, "TP 0", ts(2030, 1, 6, 23, 59), None),
        ],
    )
    conn.execute("INSERT INTO plan_session(id, course_id, topic_id, exam_id, starts_at, minutes, done) "
                 "VALUES (7, 10, 1, NULL, ?, 50, 1)", (ts(2030, 1, 7, 19),))
    conn.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (2, 10, 'planos', 'Planos')")
    conn.execute("INSERT INTO plan_session(id, course_id, topic_id, exam_id, starts_at, minutes, kind, topics_json) "
                 "VALUES (8, 10, 2, NULL, ?, 50, 'mixed_focus', '[2, 1]')", (ts(2030, 1, 9, 19),))
    conn.commit()

    # Un miércoles a la tarde se lleva al lunes de esa semana.
    r = c.get("/api/agenda", params={"week_start": ts(2030, 1, 9, 15)})
    assert r.status_code == 200
    days = r.json()["days"]
    assert [d["date"] for d in days] == [f"2030-01-{n:02d}" for n in range(7, 14)]

    monday_items = days[0]["items"]
    assert [i["kind"] for i in monday_items] == ["exam", "class", "deadline", "event", "session"]
    exam, klass, deadline, event, session = monday_items
    assert (exam["title"], exam["course"], exam["starts_at"]) == ("Primer parcial", "Física", ts(2030, 1, 7, 8))
    assert exam["ends_at"] == ts(2030, 1, 7, 10)
    assert (klass["title"], klass["course"], klass["place"]) == ("Álgebra", "Álgebra", "Aula 302")
    assert (klass["starts_at"], klass["ends_at"]) == (monday + 9 * 3600, monday + 11 * 3600)
    assert (deadline["title"], deadline["course"], deadline["url"]) == ("TP 1", "Álgebra", "https://campus/tp1")
    assert event["title"] == "Charla"
    # una sesión vieja (sin tipo) es de foco en su tema
    assert (session["title"], session["topic"], session["topic_id"], session["done"]) == (
        "Foco: Rectas", "Rectas", 1, True)
    assert (session["session_kind"], session["topic_ids"]) == ("focus", [1])
    assert (session["review_minutes"], session["focus_minutes"]) == (0, 50)
    assert session["ends_at"] == ts(2030, 1, 7, 19, 50)
    [mixed] = days[2]["items"]
    assert (mixed["title"], mixed["session_kind"], mixed["topic_ids"]) == (
        "Foco: Planos, con repaso de Rectas", "mixed_focus", [2, 1])
    assert (mixed["review_minutes"], mixed["focus_minutes"]) == (10, 40)

    assert [i["title"] for i in days[1]["items"]] == ["TP 2"]
    assert [i["title"] for i in days[6]["items"]] == ["TP 3"]
    assert all(i["title"] != "TP 0" for d in days for i in d["items"])
    # El ¿llegás? de la semana: el examen que cae en ella.
    assert [h["exam"] for h in r.json()["health"]] == ["Primer parcial"]


def test_agenda_defaults_to_this_week(env):
    r = env["client"].get("/api/agenda")
    assert r.status_code == 200
    days = r.json()["days"]
    assert len(days) == 7
    first = datetime.strptime(days[0]["date"], "%Y-%m-%d").date()
    today = datetime.now(TZ).date()
    assert first.weekday() == 0 and 0 <= (today - first).days < 7


def test_agenda_health_falls_back_to_next_exam(env):
    add_exam(env["conn"], ts(2031, 3, 3, 9), "Final")
    r = env["client"].get("/api/agenda", params={"week_start": ts(2030, 1, 7)})
    assert [h["exam"] for h in r.json()["health"]] == ["Final"]


def test_plan_health_route(env):
    add_exam(env["conn"], ts(2031, 3, 3, 9), "Final")
    r = env["client"].get("/api/plan/health")
    assert r.status_code == 200
    (h,) = r.json()
    assert h["exam"] == "Final" and h["verdict"] in ("llegás", "justo", "no llegás")
    # se explica: por qué (temas flojos con sus minutos) y qué hacer hoy (sin plan, nada)
    assert h["weak_topics"] == [{"topic_id": 1, "topic": "Rectas", "probability": 0.0, "minutes": 50}]
    assert (h["minutes_needed"], h["due_reviews"], h["today"]) == (50, 0, None)


def test_health_today_points_at_todays_session(env):
    conn = env["conn"]
    add_questions(conn, 1)
    now = int(time.time())
    exam = add_exam(conn, now + 30 * 86400, "Final")
    conn.execute("INSERT INTO plan_session(id, course_id, topic_id, exam_id, starts_at, minutes, kind, topics_json) "
                 "VALUES (5, 10, 1, ?, ?, 25, 'mixed_focus', '[1]')", (exam, now + 60))
    conn.commit()
    r = env["client"].get("/api/plan/health")
    today = r.json()[0]["today"]
    if datetime.fromtimestamp(now + 60, TZ).date() != datetime.fromtimestamp(now, TZ).date():
        pytest.skip("justo a medianoche: la sesión cae mañana")
    assert (today["id"], today["topic"], today["minutes"], today["focus_minutes"], today["label"]) == (
        5, "Rectas", 25, 18, "Foco: Rectas")


# --- Exámenes ----------------------------------------------------------------------------------

def test_exams_crud(env, replan_calls):
    c = env["client"]
    at = ts(2031, 5, 4, 9)
    r = c.post("/api/exams", json={"course_id": 10, "name": "Parcial 1", "at": at, "topic_ids": [1]})
    assert r.status_code == 200
    exam_id = r.json()["id"]
    (e,) = c.get("/api/exams").json()
    assert (e["id"], e["name"], e["at"], e["topic_ids"], e["editable"]) == (exam_id, "Parcial 1", at, [1], True)

    assert c.put(f"/api/exams/{exam_id}", json={"name": "Parcial uno", "max_grade": 100}).status_code == 200
    (e,) = c.get("/api/exams").json()
    assert (e["name"], e["max_grade"], e["at"]) == ("Parcial uno", 100, at)

    assert c.delete(f"/api/exams/{exam_id}").status_code == 200
    assert c.get("/api/exams").json() == []
    assert len(replan_calls) == 3


def test_exam_errors(env, replan_calls):
    c = env["client"]
    r = c.post("/api/exams", json={"course_id": 99, "name": "X", "at": ts(2031, 5, 4)})
    assert r.status_code == 400 and r.json()["detail"] == "El curso no existe"
    assert c.post("/api/exams", json={"course_id": 10, "name": " ", "at": 1}).status_code == 400
    assert c.put("/api/exams/999", json={"name": "X"}).status_code == 404
    assert c.delete("/api/exams/999").status_code == 404
    campus = add_exam(env["conn"], ts(2031, 5, 4), source="campus")
    assert c.put(f"/api/exams/{campus}", json={"name": "X"}).status_code == 400
    assert c.delete(f"/api/exams/{campus}").status_code == 400
    manual = add_exam(env["conn"], ts(2031, 5, 4))
    assert c.put(f"/api/exams/{manual}", json={"name": "  "}).status_code == 400
    assert c.put(f"/api/exams/{manual}", json={"at": None}).status_code == 400
    assert replan_calls == []


def test_exam_topics_route(env):
    exam_id = add_exam(env["conn"], ts(2031, 5, 4))
    r = env["client"].get(f"/api/exams/{exam_id}/topics")
    assert r.status_code == 200
    assert r.json() == [{"id": 1, "name": "Rectas"}]
    assert env["client"].get("/api/exams/999/topics").status_code == 404


def test_courses_route(env):
    r = env["client"].get("/api/courses")
    assert r.status_code == 200
    assert r.json() == [{"id": 10, "name": "Álgebra"}, {"id": 20, "name": "Física"}]


# --- Horarios ----------------------------------------------------------------------------------

def test_schedule_get(env):
    env["conn"].execute("UPDATE course SET fullname = 'Física 19 a 23 hs' WHERE id = 20")
    env["conn"].execute("INSERT INTO class_slot(course_id, weekday, start_min, end_min) VALUES (10, 1, 600, 720)")
    env["conn"].commit()
    r = env["client"].get("/api/schedule")
    assert r.status_code == 200
    body = r.json()
    assert [(s["course_id"], s["weekday"], s["start_min"]) for s in body["slots"]] == [(10, 1, 600)]
    assert body["suggestions"] == [
        {"course_id": 20, "course_name": "Física 19 a 23 hs", "start_min": 1140, "end_min": 1380}
    ]


def test_replan_called_on_schedule_write(env, replan_calls):
    c = env["client"]
    r = c.post("/api/schedule", json={"course_id": 10, "weekday": 2, "start_min": 540, "end_min": 660,
                                      "place": "Aula 5"})
    assert r.status_code == 200
    slot_id = r.json()["id"]
    assert len(replan_calls) == 1
    r = c.put(f"/api/schedule/{slot_id}", json={"course_id": 10, "weekday": 3, "start_min": 540,
                                                "end_min": 660, "place": None})
    assert r.status_code == 200
    assert [(s["weekday"], s["place"]) for s in c.get("/api/schedule").json()["slots"]] == [(3, None)]
    assert c.delete(f"/api/schedule/{slot_id}").status_code == 200
    assert c.get("/api/schedule").json()["slots"] == []
    assert len(replan_calls) == 3


def test_schedule_errors(env, replan_calls):
    c = env["client"]
    bad = {"course_id": 10, "weekday": 7, "start_min": 540, "end_min": 660}
    r = c.post("/api/schedule", json=bad)
    assert r.status_code == 400 and "lunes" in r.json()["detail"]
    ok = {**bad, "weekday": 1}
    assert c.put("/api/schedule/999", json=ok).status_code == 404
    assert c.delete("/api/schedule/999").status_code == 404
    assert replan_calls == []


def test_replan_errors_do_not_fail_the_request(env, monkeypatch):
    def boom(conn, now):
        raise RuntimeError("plan roto")

    monkeypatch.setattr(app_module, "replan", boom)
    r = env["client"].post("/api/schedule", json={"course_id": 10, "weekday": 2, "start_min": 540,
                                                  "end_min": 660})
    assert r.status_code == 200
    assert len(env["client"].get("/api/schedule").json()["slots"]) == 1


def test_schedule_write_replans_for_real(env):
    """Sin fakes: una clase nueva desplaza las sesiones que la pisaban."""
    conn, c = env["conn"], env["client"]
    add_questions(conn, 1)
    add_exam(conn, int(time.time()) + 10 * 86400)
    assert c.put("/api/plan/settings", json={"availability": [120] * 7, "study_start_min": 1140}).status_code == 200
    before = conn.execute("SELECT COUNT(*) FROM plan_session").fetchone()[0]
    assert before > 0
    for wd in range(7):
        assert c.post("/api/schedule", json={"course_id": 10, "weekday": wd, "start_min": 1140,
                                             "end_min": 1440}).status_code == 200
    rows = conn.execute("SELECT starts_at FROM plan_session WHERE starts_at >= ?", (int(time.time()),)).fetchall()
    for r in rows:
        local = datetime.fromtimestamp(r["starts_at"], TZ)
        assert local.hour < 19


# --- Plan --------------------------------------------------------------------------------------

def test_plan_settings(env, replan_calls):
    c = env["client"]
    r = c.get("/api/plan/settings")
    assert r.status_code == 200
    assert r.json() == {"availability": [0] * 7, "study_start_min": 1140}
    week = [60, 60, 0, 90, 30, 120, 0]
    assert c.put("/api/plan/settings", json={"availability": week, "study_start_min": 1200}).status_code == 200
    assert c.get("/api/plan/settings").json() == {"availability": week, "study_start_min": 1200}
    assert c.put("/api/plan/settings", json={"study_start_min": 480}).status_code == 200
    assert c.get("/api/plan/settings").json() == {"availability": week, "study_start_min": 480}
    assert len(replan_calls) == 2

    assert c.put("/api/plan/settings", json={"availability": [60] * 6}).status_code == 400
    assert c.put("/api/plan/settings", json={"availability": [2000] * 7}).status_code == 400
    assert c.put("/api/plan/settings", json={"study_start_min": 1440}).status_code == 400
    assert len(replan_calls) == 2


# --- Simulacro ---------------------------------------------------------------------------------

def test_mock_start_and_finish(env):
    conn, c = env["conn"], env["client"]
    add_questions(conn, 6)
    exam_id = add_exam(conn, ts(2031, 5, 4), max_grade=100)
    r = c.post("/api/mock/start", json={"exam_id": exam_id})
    assert r.status_code == 200
    start = r.json()
    assert len(start["items"]) == 6 and start["max_grade"] == 100 and start["seconds"] == 540
    assert "correct_index" not in start["items"][0]
    answers = {str(i["question_id"]): 0 for i in start["items"]}
    r = c.post(f"/api/mock/{start['attempt_id']}/finish", json={"answers": answers})
    assert r.status_code == 200
    result = r.json()
    assert (result["grade"], result["correct"], result["total"]) == (100, 6, 6)
    assert len(result["review"]) == 6


def test_mock_errors_mapped(env):
    conn, c = env["conn"], env["client"]
    assert c.post("/api/mock/start", json={"exam_id": 999}).status_code == 404
    exam_id = add_exam(conn, ts(2031, 5, 4))
    add_questions(conn, 4)
    r = c.post("/api/mock/start", json={"exam_id": exam_id})
    assert r.status_code == 409
    assert r.json() == {"detail": "Todavía no hay suficientes preguntas para un simulacro"}

    add_questions(conn, 1, start=5)
    attempt = c.post("/api/mock/start", json={"exam_id": exam_id}).json()["attempt_id"]
    first = c.post("/api/mock/start", json={"exam_id": exam_id}).json()
    qid = first["items"][0]["question_id"]
    r = c.post(f"/api/mock/{first['attempt_id']}/finish", json={"answers": {str(qid): 9}})
    assert r.status_code == 400  # opción fuera de rango; el intento sigue abierto
    assert c.post(f"/api/mock/{attempt}/finish", json={"answers": {}}).status_code == 200
    r = c.post(f"/api/mock/{attempt}/finish", json={"answers": {}})
    assert r.status_code == 409 and r.json() == {"detail": "Este simulacro ya terminó"}
    assert c.post("/api/mock/999/finish", json={"answers": {}}).status_code == 404

    # Todas las preguntas retiradas durante el intento: tampoco alcanza.
    conn.execute("UPDATE question SET retired = 1")
    conn.commit()
    r = c.post(f"/api/mock/{first['attempt_id']}/finish", json={"answers": {}})
    assert r.status_code == 409
    assert r.json() == {"detail": "Todavía no hay suficientes preguntas para un simulacro"}


# --- Calendario y ajustes ----------------------------------------------------------------------

def test_ics_content_type(env):
    add_exam(env["conn"], int(time.time()) + 86400, "Parcial; de Álgebra, uno")
    r = env["client"].get("/api/calendar.ics")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/calendar; charset=utf-8"
    assert r.headers["content-disposition"] == 'attachment; filename="estudia.ics"'
    assert r.text.startswith("BEGIN:VCALENDAR\r\n")
    assert "Parcial\\; de Álgebra\\, uno" in r.text


def test_settings_reminders(env):
    c = env["client"]
    assert c.get("/api/settings").json() == {"reminders": True, "age": None, "allow_download": False}
    assert c.put("/api/settings", json={"reminders": False}).status_code == 200
    assert c.get("/api/settings").json() == {"reminders": False, "age": None, "allow_download": False}
    row = env["conn"].execute("SELECT value FROM kv WHERE key = 'reminders_enabled'").fetchone()
    assert row["value"] == "0"
    assert c.put("/api/settings", json={"reminders": True}).status_code == 200
    assert c.get("/api/settings").json() == {"reminders": True, "age": None, "allow_download": False}


def test_writes_need_same_origin(env, replan_calls):
    c = env["client"]
    exam_id = add_exam(env["conn"], ts(2031, 5, 4))
    slot = {"course_id": 10, "weekday": 1, "start_min": 540, "end_min": 660}
    writes = [
        ("post", "/api/exams", {"course_id": 10, "name": "X", "at": ts(2031, 5, 4)}),
        ("put", f"/api/exams/{exam_id}", {"name": "X"}),
        ("delete", f"/api/exams/{exam_id}", None),
        ("post", "/api/schedule", slot),
        ("put", "/api/schedule/1", slot),
        ("delete", "/api/schedule/1", None),
        ("put", "/api/plan/settings", {"study_start_min": 600}),
        ("post", "/api/mock/start", {"exam_id": exam_id}),
        ("post", "/api/mock/1/finish", {"answers": {}}),
        ("put", "/api/settings", {"reminders": False}),
        ("put", "/api/settings", {"age": "adult"}),
        ("put", "/api/settings", {"allow_download": True}),
    ]
    for method, url, body in writes:
        kwargs = {"headers": EVIL}
        if body is not None:
            kwargs["json"] = body
        r = c.request(method.upper(), url, **kwargs)
        assert r.status_code == 403, (method, url)
    assert c.get("/api/settings").json() == {"reminders": True, "age": None, "allow_download": False}
    assert len(c.get("/api/exams").json()) == 1
    assert replan_calls == []
    # El mismo origen sí pasa.
    r = c.put("/api/settings", json={"reminders": False}, headers={"origin": f"http://127.0.0.1:{PORT}"})
    assert r.status_code == 200


# --- Recalcular el plan con las respuestas -----------------------------------------------------

def test_replan_throttled_after_answers(env, replan_calls, monkeypatch):
    marked = []
    monkeypatch.setattr(app_module, "mark_done_from_answers", lambda conn, now: marked.append(now) or 0)
    conn, c = env["conn"], env["client"]
    add_questions(conn, 1)
    for _ in range(3):
        assert c.post("/api/answer", json={"question_id": 1, "chosen_index": 0}).status_code == 200
    assert (len(marked), len(replan_calls)) == (1, 1)
    # Pasados 10 minutos, vuelve a recalcular.
    conn.execute("UPDATE kv SET value = ? WHERE key = 'plan_refreshed_at'", (str(int(time.time()) - 601),))
    conn.commit()
    assert c.post("/api/answer", json={"question_id": 1, "chosen_index": 0}).status_code == 200
    assert (len(marked), len(replan_calls)) == (2, 2)


def test_plan_refreshed_after_processing(env, replan_calls):
    env["client"].post("/api/site", json={"site_id": SITE_ID})
    jobs = env["app"].state.jobs
    assert jobs.run_now()
    assert len(replan_calls) == 1
    assert jobs.run_now()  # dentro de los 10 minutos: no se repite
    assert len(replan_calls) == 1


# --- Exportar a Anki -----------------------------------------------------------------------------


def test_anki_export_route(env):
    c = env["client"]
    r = c.get("/api/export/anki.apkg")
    assert r.status_code == 404 and r.json()["detail"] == "No hay preguntas para exportar"
    add_questions(env["conn"], 2)
    r = c.get("/api/export/anki.apkg?course_id=10")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"] == 'attachment; filename="estudia-algebra.apkg"'
    assert r.content[:2] == b"PK"
    assert c.get("/api/export/anki.apkg?course_id=20").status_code == 404
    todo = c.get("/api/export/anki.apkg")
    assert todo.headers["content-disposition"] == 'attachment; filename="estudia-todo.apkg"'


def test_mock_prediction_typed_answers_and_exam_result_api(env):
    conn, c = env["conn"], env["client"]
    add_questions(conn, 10)
    conn.execute("UPDATE question SET check_expr = '2'")
    conn.commit()
    exam_id = add_exam(conn, ts(2031, 5, 4), max_grade=10)
    assert c.post("/api/mock/start", json={"exam_id": exam_id, "predicted_grade": 11}).status_code == 400
    start = c.post("/api/mock/start", json={"exam_id": exam_id, "predicted_grade": 6.5}).json()
    assert start["predicted_grade"] == 6.5
    answers = {}
    for item in start["items"]:
        answers[str(item["question_id"])] = (
            {"typed": "2"} if item["format"] == "typed" else {"index": 0})
    assert any(i["format"] == "typed" for i in start["items"])
    r = c.post(f"/api/mock/{start['attempt_id']}/finish", json={"answers": answers})
    assert r.status_code == 200 and r.json()["correct"] == r.json()["total"]
    assert r.json()["predicted_grade"] == 6.5
    # una respuesta sin index ni typed es un pedido inválido
    again = c.post("/api/mock/start", json={"exam_id": exam_id}).json()
    bad = c.post(f"/api/mock/{again['attempt_id']}/finish",
                 json={"answers": {str(again["items"][0]["question_id"]): {}}})
    assert bad.status_code == 400

    assert c.post(f"/api/exams/{exam_id}/result", json={"grade": 7.5}).status_code == 400   # todavía no se rindió
    conn.execute("UPDATE exam SET opens_at = ? WHERE id = ?", (ts(2020, 5, 4), exam_id))
    conn.commit()
    assert c.post(f"/api/exams/{exam_id}/result", json={"grade": 7.5, "max_grade": 20}).status_code == 400
    assert c.post(f"/api/exams/{exam_id}/result", json={"grade": 7.5}).json()["max_grade"] == 10
    listed = [e for e in c.get("/api/exams").json() if e["id"] == exam_id][0]
    assert listed["result"]["grade"] == 7.5
    assert c.post(f"/api/exams/{exam_id}/result", json={"grade": 11}).status_code == 400
    assert c.post("/api/exams/999/result", json={"grade": 5}).status_code == 404
    assert c.post(f"/api/exams/{exam_id}/result", json={"grade": 5}, headers=EVIL).status_code == 403


def test_dismiss_suggestion_and_skip_exam_result_api(env):
    conn, c = env["conn"], env["client"]
    conn.execute("UPDATE course SET fullname = 'AM1 - 8.30 HS' WHERE id = 20")
    conn.commit()
    assert [s["course_id"] for s in c.get("/api/schedule").json()["suggestions"]] == [20]
    assert c.post("/api/schedule/suggestions/20/dismiss", headers=EVIL).status_code == 403
    assert c.post("/api/schedule/suggestions/20/dismiss").json() == {"ok": True}
    assert c.get("/api/schedule").json()["suggestions"] == []
    assert c.post("/api/schedule/suggestions/999/dismiss").status_code == 404

    exam_id = add_exam(conn, ts(2020, 5, 4))
    assert c.post(f"/api/exams/{exam_id}/result/skip", headers=EVIL).status_code == 403
    assert c.post(f"/api/exams/{exam_id}/result/skip").json() == {"ok": True}
    listed = [e for e in c.get("/api/exams").json() if e["id"] == exam_id][0]
    assert listed["result_skipped"] is True
    assert c.post("/api/exams/999/result/skip").status_code == 404
    future = add_exam(conn, ts(2031, 5, 4))
    assert c.post(f"/api/exams/{future}/result/skip").status_code == 400


def test_mock_result_can_be_read_back_after_finishing(env):
    conn, c = env["conn"], env["client"]
    add_questions(conn, 10)
    exam_id = add_exam(conn, ts(2031, 5, 4), max_grade=10)
    start = c.post("/api/mock/start", json={"exam_id": exam_id, "predicted_grade": 6}).json()
    assert c.get(f"/api/mock/{start['attempt_id']}").status_code == 409   # todavía no terminó
    answers = {str(i["question_id"]): {"index": 0} for i in start["items"]}
    finished = c.post(f"/api/mock/{start['attempt_id']}/finish", json={"answers": answers}).json()
    assert [r["question_id"] for r in finished["review"]] == [i["question_id"] for i in start["items"]]
    # Una segunda entrega (reintento) dice que ya terminó; el resultado se lee aparte.
    assert c.post(f"/api/mock/{start['attempt_id']}/finish", json={"answers": answers}).status_code == 409
    stored = c.get(f"/api/mock/{start['attempt_id']}").json()
    assert (stored["grade"], stored["correct"], stored["predicted_grade"]) == (finished["grade"], finished["correct"], 6)
    assert c.get("/api/mock/999").status_code == 404


def test_mock_finish_payload_limits(env):
    conn, c = env["conn"], env["client"]
    add_questions(conn, 6)
    exam_id = add_exam(conn, ts(2031, 5, 4))
    start = c.post("/api/mock/start", json={"exam_id": exam_id}).json()
    url = f"/api/mock/{start['attempt_id']}/finish"
    qid = str(start["items"][0]["question_id"])
    r = c.post(url, json={"answers": {qid: {"typed": "1" * 201}}})
    assert r.status_code == 422
    r = c.post(url, json={"answers": {str(i): 0 for i in range(1000, 1051)}})
    assert r.status_code == 422
    assert conn.execute("SELECT finished_at FROM mock_attempt WHERE id = ?",
                        (start["attempt_id"],)).fetchone()[0] is None
    assert c.post(url, json={"answers": {str(i): 0 for i in range(1000, 1050)}}).status_code == 200
