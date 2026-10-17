from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from estudia.db import connect
from estudia.study.home import home_view

TZ = ZoneInfo("America/Argentina/Buenos_Aires")
NOW = int(datetime(2026, 10, 17, 15, 30, tzinfo=TZ).timestamp())
EXAM_AT = int(datetime(2027, 3, 20, tzinfo=TZ).timestamp())
DAY = 86400


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    c.execute("INSERT INTO course(id, site_id, fullname, shortname) VALUES (10, 's', 'Análisis Matemático', 'AM')")
    c.execute("INSERT INTO course(id, site_id, fullname, shortname) VALUES (20, 's', 'Física', 'F')")
    c.commit()
    return c


def add_exam(conn, id, course_id, opens_at, closes_at=0, name="PRIMER CUESTIONARIO"):
    conn.execute(
        "INSERT INTO exam(id, course_id, cmid, name, opens_at, closes_at, attempts, time_limit_s, max_grade) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, 7200, 12)",
        (id, course_id, id, name, opens_at, closes_at),
    )
    conn.commit()


def add_topic(conn, id, course_id, name, section_num, visible=1, status="done"):
    conn.execute(
        "INSERT OR IGNORE INTO section(id, course_id, num, name, visible) VALUES (?, ?, ?, 's', ?)",
        (course_id * 100 + section_num, course_id, section_num, visible),
    )
    conn.execute(
        "INSERT INTO material(id, section_id, cmid, kind, title, ref, status) "
        "VALUES (?, ?, ?, 'youtube', 't', 'r', ?)",
        (id, course_id * 100 + section_num, id, status),
    )
    conn.execute(
        "INSERT INTO topic(id, course_id, slug, name) VALUES (?, ?, ?, ?)",
        (id, course_id, f"t{id}", name),
    )
    conn.execute("INSERT INTO material_topic(material_id, topic_id) VALUES (?, ?)", (id, id))
    conn.execute(
        "INSERT INTO question(id, topic_id, stem, options_json, correct_index, verified) "
        "VALUES (?, ?, 'p', '[\"x\",\"y\"]', 0, 1)",
        (id, id),
    )
    conn.commit()


def answer(conn, qid, correct):
    conn.execute(
        "INSERT INTO answer(question_id, chosen_index, correct, at) VALUES (?, 0, ?, ?)",
        (qid, correct, NOW),
    )
    conn.commit()


def test_next_exam_is_soonest_future(conn):
    add_exam(conn, 1, 10, EXAM_AT + 30 * DAY, name="SEGUNDO")
    add_exam(conn, 2, 10, EXAM_AT)
    add_exam(conn, 3, 10, NOW - 5 * DAY, NOW - DAY, name="VIEJO")
    exam = home_view(conn, NOW).exam
    assert exam.name == "PRIMER CUESTIONARIO"
    assert exam.course == "Análisis Matemático"
    assert exam.days_left == 154
    assert (exam.attempts, exam.time_limit_s, exam.max_grade) == (1, 7200, 12)
    assert exam.opens_at == EXAM_AT


def test_days_left_ignores_hour_of_day(conn):
    add_exam(conn, 1, 10, EXAM_AT)
    late = int(datetime(2026, 10, 17, 23, 59, tzinfo=TZ).timestamp())
    early = int(datetime(2026, 10, 17, 0, 0, tzinfo=TZ).timestamp())
    assert home_view(conn, late).exam.days_left == 154
    assert home_view(conn, early).exam.days_left == 154


def test_open_exam_is_next_with_zero_days(conn):
    add_exam(conn, 1, 10, NOW - DAY, NOW + DAY)
    add_exam(conn, 2, 10, EXAM_AT)
    exam = home_view(conn, NOW).exam
    assert exam.days_left == 0
    assert exam.opens_at == NOW - DAY


def test_exam_without_close_date_uses_opens_at(conn):
    add_exam(conn, 1, 10, NOW - DAY, None)
    assert home_view(conn, NOW).exam is None


def test_topics_are_those_of_visible_sections(conn):
    add_exam(conn, 1, 10, EXAM_AT)
    add_topic(conn, 1, 10, "Zeta", 1)
    add_topic(conn, 2, 10, "Alfa", 1)
    add_topic(conn, 3, 10, "Oculto", 2, visible=0)
    add_topic(conn, 4, 10, "Primero", 0)
    add_topic(conn, 5, 20, "Otro curso", 1)
    answer(conn, 2, 1)
    topics = home_view(conn, NOW).topics
    assert [t.name for t in topics] == ["Primero", "Alfa", "Zeta"]
    assert [t.practiced for t in topics] == [False, True, False]
    assert topics[1].mastery == pytest.approx(1.0)


def test_suggestion_is_weakest(conn):
    add_topic(conn, 1, 10, "Fuerte", 1)
    add_topic(conn, 2, 10, "Débil", 1)
    add_topic(conn, 3, 10, "Medio", 1)
    answer(conn, 1, 1)
    answer(conn, 2, 0)
    answer(conn, 3, 1)
    answer(conn, 3, 0)
    assert home_view(conn, NOW).suggestion.name == "Débil"


def test_suggestion_ties_by_section_then_name(conn):
    add_topic(conn, 1, 10, "B", 2)
    add_topic(conn, 2, 10, "C", 1)
    add_topic(conn, 3, 10, "A", 1)
    assert home_view(conn, NOW).suggestion.name == "A"


def test_no_exam(conn):
    assert home_view(conn, NOW).exam is None
    assert home_view(conn, NOW).suggestion is None
    add_topic(conn, 1, 10, "Tema", 1)
    add_topic(conn, 2, 20, "Otro", 1)
    add_exam(conn, 1, 10, NOW - 5 * DAY, NOW - DAY)
    view = home_view(conn, NOW)
    assert view.exam is None
    assert [t.name for t in view.topics] == ["Otro", "Tema"]


def test_processing_counts_visible_sections_only(conn):
    add_topic(conn, 1, 10, "A", 1, status="pending")
    add_topic(conn, 2, 10, "B", 1, status="pending")
    add_topic(conn, 3, 10, "C", 1, status="done")
    add_topic(conn, 4, 10, "D", 1, status="failed")
    add_topic(conn, 5, 10, "E", 1, status="unsupported")
    add_topic(conn, 6, 10, "F", 2, visible=0, status="pending")
    p = home_view(conn, NOW).processing
    assert (p.pending, p.done, p.failed) == (2, 1, 1)


def test_answers_to_retired_questions_do_not_mark_practiced(conn):
    add_exam(conn, 1, 10, EXAM_AT)
    add_topic(conn, 1, 10, "Alfa", 1)
    answer(conn, 1, 1)
    conn.execute("UPDATE question SET retired = 1 WHERE id = 1")
    conn.commit()
    topic = home_view(conn, NOW).topics[0]
    assert (topic.practiced, topic.mastery) == (False, 0)
