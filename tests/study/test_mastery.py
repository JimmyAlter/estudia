import pytest

from estudia.db import connect
from estudia.study.mastery import topic_mastery

DAY = 86400
NOW = 1_000_000_000


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    c.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (1, 10, 'a', 'Tema A')")
    c.execute(
        "INSERT INTO question(id, topic_id, stem, options_json, correct_index, verified) "
        "VALUES (1, 1, 'p', '[\"x\",\"y\"]', 0, 1)"
    )
    c.commit()
    return c


def answer(conn, correct, at):
    conn.execute(
        "INSERT INTO answer(question_id, chosen_index, correct, at) VALUES (1, 0, ?, ?)",
        (correct, at),
    )
    conn.commit()


def test_mastery_zero_without_answers(conn):
    assert topic_mastery(conn, 1, NOW) == 0


def test_recent_answers_weigh_more(conn):
    answer(conn, 1, NOW)
    answer(conn, 0, NOW - 14 * DAY)
    # pesos 1 y 0.25 -> 1 / 1.25
    assert topic_mastery(conn, 1, NOW) == pytest.approx(0.8)


def test_only_last_20_answers_count(conn):
    for i in range(5):
        answer(conn, 0, NOW - 100 + i)
    for i in range(20):
        answer(conn, 1, NOW - 50 + i)
    assert topic_mastery(conn, 1, NOW) == pytest.approx(1.0)


def test_answers_to_retired_questions_do_not_count(conn):
    conn.execute(
        "INSERT INTO question(id, topic_id, stem, options_json, correct_index, verified, retired) "
        "VALUES (2, 1, 'mal', '[\"x\",\"y\"]', 0, 1, 1)"
    )
    answer(conn, 1, NOW)
    conn.execute("INSERT INTO answer(question_id, chosen_index, correct, at) VALUES (2, 1, 0, ?)", (NOW,))
    conn.commit()
    assert topic_mastery(conn, 1, NOW) == 1.0
    conn.execute("UPDATE question SET retired = 1 WHERE id = 1")
    conn.commit()
    assert topic_mastery(conn, 1, NOW) == 0
