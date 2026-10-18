import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from estudia.db import connect
from estudia.study.exams import (
    create_manual_exam,
    delete_manual_exam,
    exam_topics,
    list_exams,
    update_manual_exam,
)
from estudia.study.home import home_view, next_exam

TZ = ZoneInfo("America/Argentina/Buenos_Aires")
NOW = int(datetime(2026, 10, 17, 15, 30, tzinfo=TZ).timestamp())
DAY = 86400


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    c.execute("INSERT INTO course(id, site_id, fullname, shortname) VALUES (10, 's', 'Análisis', 'AM')")
    c.commit()
    return c


def add_topic(conn, id, visible=1):
    conn.execute(
        "INSERT OR IGNORE INTO section(id, course_id, num, name, visible) VALUES (?, 10, ?, 's', ?)",
        (1000 + id, id, visible),
    )
    conn.execute(
        "INSERT INTO material(id, section_id, cmid, kind, title, ref, status) "
        "VALUES (?, ?, ?, 'youtube', 't', 'r', 'done')", (id, 1000 + id, id))
    conn.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (?, 10, ?, ?)", (id, f"t{id}", f"T{id}"))
    conn.execute("INSERT INTO material_topic(material_id, topic_id) VALUES (?, ?)", (id, id))
    conn.commit()


def add_campus_exam(conn, id, opens_at):
    conn.execute(
        "INSERT INTO exam(id, course_id, cmid, name, opens_at, closes_at, attempts, time_limit_s, max_grade) "
        "VALUES (?, 10, ?, 'CAMPUS', ?, 0, 1, 7200, 12)", (id, id, opens_at))
    conn.commit()


def test_manual_exam_is_next_exam(conn):
    add_campus_exam(conn, 1, NOW + 30 * DAY)
    exam_id = create_manual_exam(conn, 10, "Parcial", NOW + 5 * DAY, [], 10.0)
    row = next_exam(conn, NOW)
    assert row["name"] == "Parcial"
    assert home_view(conn, NOW).exam.name == "Parcial"
    assert [e.id for e in list_exams(conn, NOW)] == [exam_id, 1]
    out = list_exams(conn, NOW)[0]
    assert (out.source, out.editable, out.course, out.closes_at) == ("manual", True, "Análisis", None)


def test_past_manual_exam_ignored(conn):
    create_manual_exam(conn, 10, "Viejo", NOW - DAY, [], None)
    assert next_exam(conn, NOW) is None
    assert home_view(conn, NOW).exam is None


def test_cannot_edit_campus_exam(conn):
    add_campus_exam(conn, 1, NOW + DAY)
    with pytest.raises(ValueError):
        update_manual_exam(conn, 1, name="x")
    with pytest.raises(ValueError):
        delete_manual_exam(conn, 1)
    assert conn.execute("SELECT name FROM exam WHERE id = 1").fetchone()["name"] == "CAMPUS"


def test_manual_exam_update_and_delete(conn):
    add_topic(conn, 1)
    exam_id = create_manual_exam(conn, 10, "P", NOW + DAY, [1], 10)
    update_manual_exam(conn, exam_id, name="P2", at=NOW + 2 * DAY, topic_ids=[1])
    row = conn.execute("SELECT name, opens_at FROM exam WHERE id = ?", (exam_id,)).fetchone()
    assert (row["name"], row["opens_at"]) == ("P2", NOW + 2 * DAY)
    with pytest.raises(ValueError):
        update_manual_exam(conn, exam_id, cmid=5)
    delete_manual_exam(conn, exam_id)
    assert conn.execute("SELECT COUNT(*) FROM exam").fetchone()[0] == 0


def test_exam_topics_manual_vs_campus(conn):
    add_topic(conn, 1)
    add_topic(conn, 2)
    add_topic(conn, 3, visible=0)
    add_campus_exam(conn, 1, NOW + DAY)
    assert sorted(exam_topics(conn, 1)) == [1, 2]
    picked = create_manual_exam(conn, 10, "P", NOW + DAY, [2], None)
    assert exam_topics(conn, picked) == [2]
    everything = create_manual_exam(conn, 10, "Q", NOW + DAY, [], None)
    assert sorted(exam_topics(conn, everything)) == [1, 2]
    add_topic(conn, 4)
    assert sorted(exam_topics(conn, everything)) == [1, 2, 4]
    assert exam_topics(conn, picked) == [2]


def test_manual_exam_without_topics_yet_picks_up_later_ones(conn):
    exam_id = create_manual_exam(conn, 10, "P", NOW + DAY, [], None)
    assert exam_topics(conn, exam_id) == []
    add_topic(conn, 7)
    assert exam_topics(conn, exam_id) == [7]


def test_create_manual_exam_validates(conn):
    add_topic(conn, 1)
    conn.execute("INSERT INTO course(id, site_id, fullname, shortname) VALUES (20, 's', 'F', 'F')")
    for args in [(99, "P", NOW, [], None), (10, "  ", NOW, [], None), (10, "P", "mañana", [], None),
                 (10, "P", NOW, [1234], None), (20, "P", NOW, [1], None)]:
        with pytest.raises(ValueError):
            create_manual_exam(conn, *args)
    assert conn.execute("SELECT COUNT(*) FROM exam").fetchone()[0] == 0


def test_delete_manual_exam_removes_plan_sessions_keeps_attempts(conn):
    exam_id = create_manual_exam(conn, 10, "P", NOW + DAY, [], None)
    conn.execute("INSERT INTO plan_session(course_id, exam_id, starts_at, minutes) VALUES (10, ?, 1, 25)", (exam_id,))
    conn.execute("INSERT INTO mock_attempt(exam_id, started_at) VALUES (?, 1)", (exam_id,))
    delete_manual_exam(conn, exam_id)
    assert conn.execute("SELECT COUNT(*) FROM plan_session").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM mock_attempt").fetchone()[0] == 1


def test_sync_upsert_keeps_manual_exams(conn):
    from estudia.moodle.sync import _sync_exam

    exam_id = create_manual_exam(conn, 10, "P", NOW + DAY, [], None)
    _sync_exam(conn, 10, {"id": 99, "name": "C"}, {"name": "C", "timeopen": NOW + 2 * DAY})
    assert conn.execute("SELECT source FROM exam WHERE id = ?", (exam_id,)).fetchone()[0] == "manual"
    assert conn.execute("SELECT source FROM exam WHERE cmid = 99").fetchone()[0] == "campus"


def test_migration_idempotent_on_old_db(tmp_path):
    path = tmp_path / "vieja.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE exam(id INTEGER PRIMARY KEY, course_id INTEGER, cmid INTEGER UNIQUE, name TEXT,"
        " opens_at INTEGER, closes_at INTEGER, attempts INTEGER, time_limit_s INTEGER, max_grade REAL)")
    old.execute("INSERT INTO exam(id, course_id, cmid, name, opens_at) VALUES (1, 10, 5, 'V', 100)")
    old.commit()
    old.close()
    connect(path).close()
    conn = connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(exam)")}
    assert {"source", "topics_json"} <= cols
    assert conn.execute("SELECT source, topics_json FROM exam").fetchone()[:] == ("campus", None)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"class_slot", "deadline", "plan_session", "availability", "mock_attempt"} <= tables
    conn.close()


def test_skip_exam_result_only_after_the_exam(conn):
    from estudia.study.exams import skip_exam_result

    past = create_manual_exam(conn, 10, "Pasado", NOW - DAY, [], None)
    future = create_manual_exam(conn, 10, "Futuro", NOW + DAY, [], None)
    with pytest.raises(ValueError):
        skip_exam_result(conn, future, NOW)
    with pytest.raises(LookupError):
        skip_exam_result(conn, 999, NOW)
    skip_exam_result(conn, past, NOW)
    skip_exam_result(conn, past, NOW)
    by_id = {e.id: e for e in list_exams(conn, NOW)}
    assert by_id[past].result_skipped is True
    assert by_id[future].result_skipped is False
    assert by_id[past].result is None
