"""Final: plan de 4 a 6 semanas con todos los temas de la materia."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from estudia.db import connect
from estudia.study import planner
from estudia.study.exams import create_manual_exam, exam_topics
from estudia.study.planner import (
    FINAL_MAX_WEEKS,
    FINAL_MIN_WEEKS,
    FINAL_SHORT_NOTE,
    plan_health,
    replan,
    set_availability,
)

TZ = ZoneInfo("America/Argentina/Buenos_Aires")
DAY = 86400
NOW = int(datetime(2026, 10, 17, 15, 30, tzinfo=TZ).timestamp())  # sábado 15:30


def local_day(epoch: int):
    return datetime.fromtimestamp(epoch, TZ).date()


def at_9(days: int) -> int:
    """Las 9:00 locales, `days` días locales después de hoy."""
    d = local_day(NOW) + timedelta(days=days)
    return int(datetime(d.year, d.month, d.day, 9, tzinfo=TZ).timestamp())


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    c.execute("INSERT INTO course(id, site_id, fullname, shortname) VALUES (10, 's', 'Análisis', 'AM')")
    c.commit()
    return c


def add_topic(conn, tid, *, visible=1, section=True, questions=1):
    """Tema `tid` con preguntas verificadas; en una sección (visible u oculta) o sin material."""
    if section:
        conn.execute("INSERT INTO section(id, course_id, num, name, visible) VALUES (?, 10, ?, 's', ?)",
                     (1000 + tid, tid, visible))
        conn.execute("INSERT INTO material(id, section_id, cmid, kind, title, ref, status) "
                     "VALUES (?, ?, ?, 'youtube', 't', 'r', 'done')", (tid, 1000 + tid, tid))
    conn.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (?, 10, ?, ?)", (tid, f"t{tid}", f"T{tid}"))
    if section:
        conn.execute("INSERT INTO material_topic(material_id, topic_id) VALUES (?, ?)", (tid, tid))
    for i in range(questions):
        conn.execute(
            "INSERT INTO question(id, topic_id, stem, options_json, correct_index, verified, retired, dedupe_key) "
            "VALUES (?, ?, 'p', '[\"x\",\"y\"]', 0, 1, 0, ?)", (tid * 100 + i, tid, f"k{tid}-{i}"))
    conn.commit()


def final_sessions(conn, exam_id):
    return conn.execute("SELECT topic_id, starts_at, topics_json FROM plan_session WHERE exam_id = ?",
                        (exam_id,)).fetchall()


def test_final_constants():
    assert (FINAL_MIN_WEEKS, FINAL_MAX_WEEKS) == (4, 6)


def test_final_schedules_every_topic(conn, monkeypatch):
    for tid in range(1, 13):
        add_topic(conn, tid)
    set_availability(conn, {d: 100 for d in range(7)})
    # Cuatro temas casi firmes: con la prioridad de siempre la rotación no llegaría a ellos.
    monkeypatch.setattr(planner, "topic_probability",
                        lambda conn, topic_id, at: 0.97 if topic_id > 8 else 0.0)
    final = create_manual_exam(conn, 10, "Final", at_9(30), [], None, kind="final", now=NOW)
    replan(conn, NOW)
    assert {r["topic_id"] for r in final_sessions(conn, final)} == set(range(1, 13))
    # Los primeros 12 bloques son los 12 temas, sin repetir: primero lo que no tuvo ninguna sesión.
    first = conn.execute("SELECT topic_id FROM plan_session WHERE exam_id = ? ORDER BY starts_at LIMIT 12",
                         (final,)).fetchall()
    assert sorted(r[0] for r in first) == list(range(1, 13))
    # Ya cubiertos, vuelve la prioridad de siempre: los firmes no se repiten antes que los flojos.
    rest = conn.execute("SELECT topic_id FROM plan_session WHERE exam_id = ? ORDER BY starts_at LIMIT -1 OFFSET 12",
                        (final,)).fetchall()
    assert rest and all(r[0] <= 8 for r in rest)


def test_final_coverage_counts_kept_sessions(conn, monkeypatch):
    """Un tema que ya tuvo una sesión de ese final (hecha) no cuenta como sin cubrir: no se le
    adelanta a uno flojo."""
    for tid in (1, 3):
        add_topic(conn, tid)
    set_availability(conn, {d: 50 for d in range(7)})   # dos bloques de 25 por día
    monkeypatch.setattr(planner, "topic_probability",
                        lambda conn, topic_id, at: {1: 0.0, 3: 0.97}[topic_id])
    final = create_manual_exam(conn, 10, "Final", at_9(30), [], None, kind="final", now=NOW)
    conn.execute("INSERT INTO plan_session(course_id, topic_id, exam_id, starts_at, minutes, done, kind, topics_json) "
                 "VALUES (10, 3, ?, ?, 25, 1, 'focus', '[3]')", (final, NOW - 3 * DAY))
    conn.commit()
    replan(conn, NOW)
    first_two = conn.execute("SELECT topic_id FROM plan_session WHERE exam_id = ? AND done = 0 "
                             "ORDER BY starts_at LIMIT 2", (final,)).fetchall()
    assert [r[0] for r in first_two] == [1, 1]


def test_final_includes_hidden_section_topics(conn):
    add_topic(conn, 1)
    add_topic(conn, 2, visible=0)       # sección oculta
    add_topic(conn, 3, section=False)   # tema sin material
    final = create_manual_exam(conn, 10, "Final", at_9(30), [], None, kind="final", now=NOW)
    parcial = create_manual_exam(conn, 10, "Parcial", at_9(30), [], None, now=NOW)
    assert sorted(exam_topics(conn, final)) == [1, 2, 3]
    assert exam_topics(conn, parcial) == [1]
    # Sin congelar: un tema nuevo de la materia también entra al final.
    add_topic(conn, 4, visible=0)
    assert sorted(exam_topics(conn, final)) == [1, 2, 3, 4]
    # Y el plan también los prepara.
    set_availability(conn, {d: 100 for d in range(7)})
    replan(conn, NOW)
    assert {r["topic_id"] for r in final_sessions(conn, final)} == {1, 2, 3, 4}


def test_final_waits_until_six_weeks_before(conn):
    add_topic(conn, 1)
    set_availability(conn, {d: 100 for d in range(7)})
    final = create_manual_exam(conn, 10, "Final", at_9(60), [], None, kind="final", now=NOW)
    replan(conn, NOW)
    assert final_sessions(conn, final) == []
    # Con un horizonte más largo, la primera sesión cae justo el día examen − 42.
    replan(conn, NOW, horizon_days=30)
    first = min(r["starts_at"] for r in final_sessions(conn, final))
    assert local_day(first) == local_day(at_9(60)) - timedelta(days=7 * FINAL_MAX_WEEKS)


def test_final_window_does_not_hold_back_other_exams(conn):
    """El tema de un final lejano que también entra en un parcial cercano se prepara para el parcial."""
    add_topic(conn, 1)
    set_availability(conn, {d: 100 for d in range(7)})
    final = create_manual_exam(conn, 10, "Final", at_9(60), [], None, kind="final", now=NOW)
    parcial = create_manual_exam(conn, 10, "Parcial", at_9(10), [1], None, now=NOW)
    replan(conn, NOW)
    assert final_sessions(conn, final) == []
    assert {r["topic_id"] for r in final_sessions(conn, parcial)} == {1}


def test_final_under_four_weeks_note(conn):
    add_topic(conn, 1)
    soon = create_manual_exam(conn, 10, "Final", at_9(20), [], None, kind="final", now=NOW)
    later = create_manual_exam(conn, 10, "Final", at_9(35), [], None, kind="final", now=NOW)
    edge = create_manual_exam(conn, 10, "Final", at_9(28), [], None, kind="final", now=NOW)
    parcial = create_manual_exam(conn, 10, "Parcial", at_9(20), [], None, now=NOW)
    health = {h.exam_id: h for h in plan_health(conn, NOW)}
    assert health[soon].note == FINAL_SHORT_NOTE
    assert FINAL_SHORT_NOTE == "Faltan menos de 4 semanas: el plan usa el tiempo que hay."
    assert health[later].note is None
    assert health[edge].note is None   # 28 días justos no es "menos de 4 semanas"
    assert health[parcial].note is None
    assert health[soon].kind == "final"
