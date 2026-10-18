"""Recuperatorio: solo los temas con P < 0,7, sin contenido nuevo, simulacro corto y retención 0,92."""
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from estudia.db import connect
from estudia.study import planner
from estudia.study.exams import create_manual_exam, exam_topics, update_manual_exam
from estudia.study.mock_exam import (
    MIN_QUESTIONS,
    RETAKE_MOCK_QUESTIONS,
    SHORT_NOT_ENOUGH_TEXT,
    NotEnoughQuestions,
    start_mock,
)
from estudia.study.planner import (
    RETAKE_MOCK_AFTER_DAYS,
    RETAKE_THRESHOLD,
    plan_health,
    replan,
    retake_topics,
    sessions,
    set_availability,
)
from estudia.study.srs import (
    EXAMS_KEY,
    RETENTION_FAR,
    WATERMARK_KEY,
    exam_dates_by_topic,
    exam_schedule_by_topic,
    sync_review_state,
    target_retention,
)

TZ = ZoneInfo("America/Argentina/Buenos_Aires")
DAY = 86400


def local(day: int, hour: int = 0, minute: int = 0, month: int = 10) -> int:
    """Epoch de una hora local de Buenos Aires en 2026."""
    return int(datetime(2026, month, day, hour, minute, tzinfo=TZ).timestamp())


def midnight(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp())


NOW = local(17, 15, 30)  # sábado 15:30


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    c.execute("INSERT INTO course(id, site_id, fullname, shortname) VALUES (10, 's', 'Análisis', 'AM')")
    c.commit()
    return c


def add_topic(conn, tid, questions=1):
    """Tema `tid` en una sección visible, con preguntas verificadas `tid*100 + i`."""
    conn.execute(
        "INSERT OR IGNORE INTO section(id, course_id, num, name, visible) VALUES (?, 10, ?, 's', 1)",
        (1000 + tid, tid),
    )
    conn.execute(
        "INSERT INTO material(id, section_id, cmid, kind, title, ref, status) "
        "VALUES (?, ?, ?, 'youtube', 't', 'r', 'done')", (tid, 1000 + tid, tid))
    conn.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (?, 10, ?, ?)", (tid, f"t{tid}", f"T{tid}"))
    conn.execute("INSERT INTO material_topic(material_id, topic_id) VALUES (?, ?)", (tid, tid))
    for i in range(questions):
        conn.execute(
            "INSERT INTO question(id, material_id, topic_id, stem, options_json, correct_index, verified, "
            "retired, dedupe_key) VALUES (?, ?, ?, 'p', '[\"x\",\"y\"]', 0, 1, 0, ?)",
            (tid * 100 + i, tid, tid, f"k{tid}-{i}"))
    conn.commit()


def fake_probability(monkeypatch, values: dict[int, float]):
    """`planner.topic_probability` devuelve `values[tema]` (se puede cambiar el dict después)."""
    monkeypatch.setattr(planner, "topic_probability", lambda conn, topic_id, at: values[topic_id])


def named_topics(conn, exam_id, since=0) -> set[int]:
    """Todos los temas que nombra alguna sesión del examen desde `since`: el foco y los de `topics_json`."""
    out: set[int] = set()
    for r in conn.execute("SELECT topic_id, topics_json FROM plan_session WHERE exam_id = ? AND starts_at >= ?",
                          (exam_id, since)):
        out.add(r["topic_id"])
        out.update(json.loads(r["topics_json"] or "[]"))
    return out


def test_retake_never_schedules_topic_with_p_at_least_07(conn, monkeypatch):
    """Review Focus 5: ni el foco ni la mezcla nombran un tema con P ≥ 0,7, ni con P = 0,7 exacto."""
    for tid in (1, 2, 3, 4):
        add_topic(conn, tid)
    set_availability(conn, {d: 150 for d in range(7)})
    fake_probability(monkeypatch, {1: 0.4, 2: 0.7, 3: 0.9, 4: 0.5})
    # A 12 días: hay sesiones de foco y, en la última semana, todo mezclado.
    retake = create_manual_exam(conn, 10, "Recuperatorio", local(29, 9), [], None,
                                kind="recuperatorio", now=NOW)
    assert retake_topics(conn, retake) == [1, 4]
    assert replan(conn, NOW) > 0
    rows = conn.execute("SELECT kind, topics_json FROM plan_session WHERE exam_id = ?", (retake,)).fetchall()
    assert {r["kind"] for r in rows} >= {"focus", "mixed_only"}
    assert any(len(json.loads(r["topics_json"])) > 1 for r in rows)   # hay mezcla de verdad
    assert named_topics(conn, retake) == {1, 4}
    # "¿Llegás?" mira los mismos temas: ni 2 ni 3 aparecen entre los flojos.
    health = next(h for h in plan_health(conn, NOW) if h.exam_id == retake)
    assert health.kind == "recuperatorio"
    assert {w.topic_id for w in health.weak_topics} == {1, 4}
    assert health.minutes_needed == round(50 * (0.6 + 0.5))


def test_retake_threshold_value():
    assert RETAKE_THRESHOLD == 0.7


def test_retake_topic_drops_out_after_practice(conn, monkeypatch):
    for tid in (1, 2):
        add_topic(conn, tid)
    set_availability(conn, {d: 100 for d in range(7)})
    values = {1: 0.5, 2: 0.3}
    fake_probability(monkeypatch, values)
    retake = create_manual_exam(conn, 10, "Recuperatorio", local(29, 9), [], None,
                                kind="recuperatorio", now=NOW)
    replan(conn, NOW)
    assert named_topics(conn, retake) == {1, 2}
    values[1] = 0.8   # lo practicó: ya lo tiene firme para el día del recuperatorio
    later = local(18, 15, 30)   # al día siguiente: las sesiones de ayer quedan, las futuras se rehacen
    past_before = conn.execute("SELECT * FROM plan_session WHERE starts_at < ? ORDER BY id", (later,)).fetchall()
    assert past_before and replan(conn, later) > 0
    past_after = conn.execute("SELECT * FROM plan_session WHERE starts_at < ? ORDER BY id", (later,)).fetchall()
    assert [dict(r) for r in past_after] == [dict(r) for r in past_before]
    assert named_topics(conn, retake, since=later) == {2}
    assert retake_topics(conn, retake) == [2]


def test_retake_freezes_topics_at_creation(conn):
    add_topic(conn, 1)
    add_topic(conn, 2)
    retake = create_manual_exam(conn, 10, "Recuperatorio", local(29, 9), [], None,
                                kind="recuperatorio", now=NOW)
    parcial = create_manual_exam(conn, 10, "Parcial", local(29, 9), [], None, now=NOW)
    stored = conn.execute("SELECT topics_json FROM exam WHERE id = ?", (retake,)).fetchone()[0]
    assert json.loads(stored) == [1, 2]   # lista explícita, no NULL
    add_topic(conn, 3)   # contenido nuevo del curso, cargado después
    assert exam_topics(conn, retake) == [1, 2]
    assert exam_topics(conn, parcial) == [1, 2, 3]   # el parcial sigue abarcando todo el curso
    # Una lista elegida a mano se respeta tal cual.
    update_manual_exam(conn, retake, topic_ids=[2])
    assert exam_topics(conn, retake) == [2]


def test_retake_target_retention_092_from_day_one(conn):
    assert target_retention(NOW, NOW + 30 * DAY, retake=True) == 0.92
    assert target_retention(NOW, NOW + 30 * DAY) == RETENTION_FAR   # un parcial a 30 días: 0,85
    # En la reproducción: con el recuperatorio a 30 días la tarjeta vence antes que con un parcial.
    add_topic(conn, 1)
    exam = create_manual_exam(conn, 10, "Parcial", NOW + 30 * DAY, [1], None, now=NOW)
    conn.execute("INSERT INTO answer(question_id, chosen_index, correct, at, mode) VALUES (100, 0, 1, ?, 'mcq')",
                 (NOW,))
    sync_review_state(conn)
    due_parcial = conn.execute("SELECT due FROM review_state WHERE card_key = 'q:100'").fetchone()[0]
    conn.execute("UPDATE exam SET kind = 'recuperatorio' WHERE id = ?", (exam,))
    assert exam_schedule_by_topic(conn) == {1: [(NOW + 30 * DAY, True)]}
    assert exam_dates_by_topic(conn) == {1: [NOW + 30 * DAY]}   # para el resto, igual que antes
    assert sync_review_state(conn) == 1   # cambiar el tipo cuenta como cambio del examen
    due_retake = conn.execute("SELECT due FROM review_state WHERE card_key = 'q:100'").fetchone()[0]
    assert due_retake < due_parcial


def test_old_exam_marker_rebuilds_once(conn):
    """Una base con la marca vieja (solo fechas) rehace una vez y después queda quieta."""
    add_topic(conn, 1)
    create_manual_exam(conn, 10, "Recuperatorio", NOW + 30 * DAY, [], None, kind="recuperatorio", now=NOW)
    conn.execute("INSERT INTO answer(question_id, chosen_index, correct, at, mode) VALUES (100, 0, 1, ?, 'mcq')",
                 (NOW,))
    sync_review_state(conn)
    expected = dict(conn.execute("SELECT * FROM review_state").fetchone())
    # Como la dejaba la versión anterior: fechas sueltas y la tarjeta derivada sin el recuperatorio.
    conn.execute("UPDATE kv SET value = ? WHERE key = ?", (json.dumps({"1": [NOW + 30 * DAY]}), EXAMS_KEY))
    conn.execute("UPDATE review_state SET due = 0")
    assert sync_review_state(conn) == 1
    assert dict(conn.execute("SELECT * FROM review_state").fetchone()) == expected
    assert sync_review_state(conn) == 0
    assert json.loads(conn.execute("SELECT value FROM kv WHERE key = ?", (EXAMS_KEY,)).fetchone()[0]) \
        == {"1": [[NOW + 30 * DAY, True]]}


def test_retake_replay_ignores_pretest_and_explain_no_brings_back(conn):
    """Con un recuperatorio, la reproducción sigue sin contar el pretest y un "No" de "Explicá por qué"
    trae la pregunta pronto sin mandarla a reaprendizaje."""
    add_topic(conn, 1, questions=3)
    create_manual_exam(conn, 10, "Recuperatorio", NOW + 30 * DAY, [], None, kind="recuperatorio", now=NOW)
    rows = [(100, 2, None, "mcq"), (101, 2, 0, "mcq"), (102, 1, None, "mcq")]
    for qid, confidence, explain, mode in rows:
        conn.execute("INSERT INTO answer(question_id, chosen_index, correct, at, mode, confidence, explain_match) "
                     "VALUES (?, 0, 1, ?, ?, ?, ?)", (qid, NOW, mode, confidence, explain))
    conn.execute("INSERT INTO answer(question_id, chosen_index, correct, at, mode) VALUES (100, 0, 1, ?, 'pretest')",
                 (NOW - DAY,))
    conn.execute("DELETE FROM kv WHERE key IN (?, ?)", (WATERMARK_KEY, EXAMS_KEY))
    sync_review_state(conn)
    state = {r["card_key"]: dict(r) for r in conn.execute("SELECT * FROM review_state")}
    # El pretest de la 100 no cuenta: una sola revisión, la de práctica.
    assert state["q:100"]["last_review"] == NOW
    # Seguro con "no dije lo mismo": nota Again (vuelve en ~1 día), pero no es un error.
    assert state["q:101"]["stability"] < state["q:102"]["stability"] < state["q:100"]["stability"]
    assert state["q:101"]["due"] - NOW <= DAY + 3600
    assert (state["q:101"]["state"], state["q:101"]["relearn_days"]) == (state["q:100"]["state"], 0)


def test_retake_short_mock_six_questions(conn, monkeypatch):
    for tid in (1, 2, 3):
        add_topic(conn, tid, questions=5)
    fake_probability(monkeypatch, {1: 0.2, 2: 0.95, 3: 0.6})
    retake = create_manual_exam(conn, 10, "Recuperatorio", NOW + 10 * DAY, [], None,
                                kind="recuperatorio", now=NOW)
    mock = start_mock(conn, retake, NOW, short=True)
    assert RETAKE_MOCK_QUESTIONS == 6 and MIN_QUESTIONS == 5
    assert len(mock.items) == 6
    ids = [i.question_id for i in mock.items]
    assert {qid // 100 for qid in ids} == {1, 3}   # solo los temas que todavía no están firmes
    # Sin `short`, el simulacro completo sigue igual.
    assert len(start_mock(conn, retake, NOW).items) == 10
    # En un parcial, el corto sale de todos sus temas.
    parcial = create_manual_exam(conn, 10, "Parcial", NOW + 10 * DAY, [], None, now=NOW)
    assert len(start_mock(conn, parcial, NOW, short=True).items) == 6


def test_retake_mock_suggested_three_days_after_creation(conn):
    add_topic(conn, 1)
    created = local(17, 15, 30)
    retake = create_manual_exam(conn, 10, "Recuperatorio", local(31, 9), [], None,
                                kind="recuperatorio", now=created)
    parcial = create_manual_exam(conn, 10, "Parcial", local(31, 9), [], None, now=created)

    def suggested(exam_id, now=created):
        return next(h for h in plan_health(conn, now) if h.exam_id == exam_id).mock_suggested_at

    assert RETAKE_MOCK_AFTER_DAYS == 3
    assert suggested(retake) == midnight(date(2026, 10, 20))
    assert suggested(retake, local(20, 23, 59)) == midnight(date(2026, 10, 20))   # todavía es ese día
    assert suggested(retake, local(21, 0, 1)) is None                             # ya pasó
    assert suggested(parcial) is None
    # Si a los 3 días ya es el recuperatorio (o después), se sugiere el día anterior.
    on_day = create_manual_exam(conn, 10, "Recup 2", local(20, 9), [], None, kind="recuperatorio", now=created)
    before = create_manual_exam(conn, 10, "Recup 3", local(19, 9), [], None, kind="recuperatorio", now=created)
    assert suggested(on_day) == midnight(date(2026, 10, 19))
    assert suggested(before) == midnight(date(2026, 10, 18))


def test_retake_short_mock_not_enough_weak_questions(conn, monkeypatch):
    """Pocos temas flojos: el aviso habla de los temas flojos, no de todo el examen."""
    add_topic(conn, 1, questions=3)
    add_topic(conn, 2, questions=10)
    fake_probability(monkeypatch, {1: 0.2, 2: 0.95})
    retake = create_manual_exam(conn, 10, "Recuperatorio", NOW + 10 * DAY, [], None,
                                kind="recuperatorio", now=NOW)
    with pytest.raises(NotEnoughQuestions) as exc:
        start_mock(conn, retake, NOW, short=True)
    assert str(exc.value) == SHORT_NOT_ENOUGH_TEXT
    assert SHORT_NOT_ENOUGH_TEXT == \
        "Todavía no hay suficientes preguntas de los temas flojos para un simulacro corto."
    assert conn.execute("SELECT COUNT(*) FROM mock_attempt").fetchone()[0] == 0


def test_retake_from_parcial_keeps_only_its_topics(conn, monkeypatch):
    """"Armar el recuperatorio" manda los temas del parcial: un tema de la materia que no estaba en
    el parcial no entra nunca, ni en el plan ni en el simulacro, aunque esté flojo (P < 0,7)."""
    for tid in (1, 2, 3, 4):
        add_topic(conn, tid, questions=6)
    set_availability(conn, {d: 150 for d in range(7)})
    fake_probability(monkeypatch, {1: 0.4, 2: 0.9, 3: 0.3, 4: 0.2, 5: 0.1})
    parcial = create_manual_exam(conn, 10, "Primer parcial", NOW - 10 * DAY, [1, 2], None, now=NOW - 20 * DAY)
    retake = create_manual_exam(conn, 10, "Recuperatorio", local(29, 9), exam_topics(conn, parcial), None,
                                kind="recuperatorio", now=NOW)
    add_topic(conn, 5, questions=6)   # contenido nuevo después de cargarlo
    assert exam_topics(conn, retake) == [1, 2]
    assert retake_topics(conn, retake) == [1]
    assert replan(conn, NOW) > 0
    assert named_topics(conn, retake) == {1}
    health = next(h for h in plan_health(conn, NOW) if h.exam_id == retake)
    assert {w.topic_id for w in health.weak_topics} == {1}
    mock = start_mock(conn, retake, NOW, short=True)
    assert {i.question_id // 100 for i in mock.items} == {1}
