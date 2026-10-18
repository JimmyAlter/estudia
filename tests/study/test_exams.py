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


# --- Tipo de examen e Inscripción ----------------------------------------------------------------

from estudia.study.exams import ENROLLMENT, EXAM_KINDS, set_exam_result, skip_exam_result  # noqa: E402
from estudia.study.mock_exam import start_mock  # noqa: E402
from estudia.study.planner import plan_health, replan  # noqa: E402
from estudia.study.srs import exam_dates_by_topic  # noqa: E402


def add_enrollment(conn, opens, closes, name="Inscripción a finales"):
    return create_manual_exam(conn, 10, name, opens, [], None, kind=ENROLLMENT, closes_at=closes, now=NOW)


def add_question(conn, qid, topic_id):
    conn.execute(
        "INSERT INTO question(id, material_id, topic_id, stem, options_json, correct_index, explanation,"
        " verified, retired, dedupe_key) VALUES (?, ?, ?, 'p', '[\"a\",\"b\"]', 0, 'e', 1, 0, ?)",
        (qid, topic_id, topic_id, f"k{qid}"),
    )
    conn.commit()


def test_exam_kinds_and_default_kind():
    assert EXAM_KINDS == ("parcial", "recuperatorio", "final", "inscripcion")
    assert ENROLLMENT == "inscripcion"


def test_create_stores_kind_and_created_at(conn):
    eid = create_manual_exam(conn, 10, "Final", NOW + DAY, [], None, kind="final", closes_at=NOW + 2 * DAY, now=NOW)
    row = conn.execute("SELECT kind, created_at, closes_at FROM exam WHERE id = ?", (eid,)).fetchone()
    assert (row["kind"], row["created_at"], row["closes_at"]) == ("final", NOW, None)
    assert next(e for e in list_exams(conn, NOW) if e.id == eid).kind == "final"
    with pytest.raises(ValueError):
        create_manual_exam(conn, 10, "X", NOW + DAY, [], None, kind="otro")


def test_campus_exam_defaults_to_parcial(conn):
    conn.execute("INSERT INTO exam(course_id, cmid, name, opens_at, source) VALUES (10, 5, 'Quiz', ?, 'campus')", (NOW + DAY,))
    conn.commit()
    assert list_exams(conn, NOW)[0].kind == "parcial"


def test_enrollment_requires_close_after_open(conn):
    with pytest.raises(ValueError):
        create_manual_exam(conn, 10, "I", NOW + DAY, [], None, kind=ENROLLMENT, now=NOW)
    with pytest.raises(ValueError):
        add_enrollment(conn, NOW + DAY, NOW + DAY)
    with pytest.raises(ValueError):
        add_enrollment(conn, NOW + DAY, NOW)
    add_topic(conn, 1)
    with pytest.raises(ValueError):
        create_manual_exam(conn, 10, "I", NOW + DAY, [1], None, kind=ENROLLMENT, closes_at=NOW + 2 * DAY, now=NOW)
    with pytest.raises(ValueError):
        create_manual_exam(conn, 10, "I", NOW + DAY, [], 10.0, kind=ENROLLMENT, closes_at=NOW + 2 * DAY, now=NOW)
    assert list_exams(conn, NOW) == []


def test_enrollment_edit_closes_at_and_type_fixed(conn):
    eid = add_enrollment(conn, NOW + DAY, NOW + 3 * DAY)
    update_manual_exam(conn, eid, closes_at=NOW + 5 * DAY)
    assert conn.execute("SELECT closes_at FROM exam WHERE id = ?", (eid,)).fetchone()[0] == NOW + 5 * DAY
    with pytest.raises(ValueError):
        update_manual_exam(conn, eid, at=NOW + 6 * DAY)  # abriría después del cierre
    with pytest.raises(ValueError):
        update_manual_exam(conn, eid, kind="parcial")
    pid = create_manual_exam(conn, 10, "P", NOW + DAY, [], None)
    with pytest.raises(ValueError):
        update_manual_exam(conn, pid, closes_at=NOW + 2 * DAY)


def test_enrollment_has_no_topics_plan_or_mock(conn):
    add_topic(conn, 1)
    for i in range(1, 13):
        add_question(conn, i, 1)
    eid = add_enrollment(conn, NOW + 5 * DAY, NOW + 9 * DAY)
    assert exam_topics(conn, eid) == []
    replan(conn, NOW)
    assert conn.execute("SELECT COUNT(*) FROM plan_session WHERE exam_id = ?", (eid,)).fetchone()[0] == 0
    assert plan_health(conn, NOW) == []
    with pytest.raises(ValueError, match="Una inscripción no tiene simulacro"):
        start_mock(conn, eid, NOW)


def test_enrollment_not_next_exam(conn):
    add_enrollment(conn, NOW + DAY, NOW + 2 * DAY)
    assert next_exam(conn, NOW) is None
    assert home_view(conn, NOW).exam is None
    pid = create_manual_exam(conn, 10, "Parcial", NOW + 10 * DAY, [], None, now=NOW)
    row = next_exam(conn, NOW)
    assert row["name"] == "Parcial" and row["id"] == pid


def test_enrollment_does_not_set_exam_horizon(conn):
    add_topic(conn, 1)
    add_enrollment(conn, NOW + DAY, NOW + 2 * DAY)
    assert exam_dates_by_topic(conn) == {}


def test_enrollment_has_no_result(conn):
    eid = add_enrollment(conn, NOW - 2 * DAY, NOW + DAY)
    with pytest.raises(ValueError):
        set_exam_result(conn, eid, 7, NOW)
    with pytest.raises(ValueError):
        skip_exam_result(conn, eid, NOW)


def test_old_exams_default_to_parcial(tmp_path):
    path = tmp_path / "old.db"
    c = connect(path)
    c.execute("INSERT INTO course(id, site_id, fullname, shortname) VALUES (10, 's', 'A', 'A')")
    c.execute("INSERT INTO exam(id, course_id, name, opens_at) VALUES (1, 10, 'Viejo', 5)")
    c.execute("ALTER TABLE exam DROP COLUMN kind")
    c.execute("ALTER TABLE exam DROP COLUMN created_at")
    c.commit()
    c.close()
    c = connect(path)  # migra
    row = c.execute("SELECT kind, created_at FROM exam WHERE id = 1").fetchone()
    assert (row["kind"], row["created_at"]) == ("parcial", None)
    c.close()
    connect(path).close()  # idempotente
