import pytest
from fakes import commit_answers

from estudia.db import connect
from estudia.study.exams import create_manual_exam
from estudia.study.mastery import (
    exam_horizon,
    topic_mastery,
    topic_probability,
    topic_probability_at_exam,
)

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
    commit_answers(conn)


def test_mastery_zero_without_answers(conn):
    assert topic_mastery(conn, 1, NOW) == 0


def add_questions(conn, ids, topic_id=1):
    for qid in ids:
        conn.execute(
            "INSERT INTO question(id, topic_id, stem, options_json, correct_index, verified) "
            "VALUES (?, ?, 'p', '[\"x\",\"y\"]', 0, 1)",
            (qid, topic_id),
        )
    conn.commit()


def answer_q(conn, qid, correct, at):
    conn.execute(
        "INSERT INTO answer(question_id, chosen_index, correct, at) VALUES (?, 0, ?, ?)", (qid, correct, at))
    commit_answers(conn)


def test_probability_decays_without_practice(conn):
    add_questions(conn, range(2, 9))
    for qid in range(1, 9):          # 8 preguntas, cada una acertada en tres días distintos
        for d in (0, 2, 6):
            answer_q(conn, qid, 1, NOW - 30 * DAY + d * DAY)
    last = NOW - 24 * DAY
    values = [topic_probability(conn, 1, last + d * DAY) for d in range(31)]
    assert values[0] == pytest.approx(1.0)
    assert all(a > b for a, b in zip(values, values[1:]))      # baja todos los días, sin piso
    assert topic_mastery(conn, 1, NOW) == topic_probability(conn, 1, NOW)


def test_unseen_count_zero_floor_8(conn):
    add_questions(conn, range(2, 11))                 # 10 preguntas en el tema
    answer_q(conn, 1, 1, NOW)
    answer_q(conn, 2, 1, NOW)
    assert topic_probability(conn, 1, NOW) == pytest.approx(2 / 8)
    for qid in range(3, 10):                          # 9 vistas: ya no hay piso
        answer_q(conn, qid, 1, NOW)
    assert topic_probability(conn, 1, NOW) == pytest.approx(1.0)
    # un tema con menos de 8 preguntas llega a 100 % viéndolas todas
    conn.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (2, 10, 'b', 'Tema B')")
    add_questions(conn, (20, 21, 22), topic_id=2)
    answer_q(conn, 20, 1, NOW)
    assert topic_probability(conn, 2, NOW) == pytest.approx(1 / 3)
    answer_q(conn, 21, 1, NOW)
    answer_q(conn, 22, 1, NOW)
    assert topic_probability(conn, 2, NOW) == pytest.approx(1.0)


def test_relearning_question_counts_at_most_half(conn):
    answer_q(conn, 1, 0, NOW)
    answer_q(conn, 1, 1, NOW + 60)
    assert topic_probability(conn, 1, NOW + 120) == pytest.approx(0.5)


def test_probability_at_exam_uses_nearest_exam(conn):
    answer_q(conn, 1, 1, NOW)
    assert exam_horizon(conn, 1, NOW) == NOW
    assert topic_probability_at_exam(conn, 1, NOW) == pytest.approx(1.0)
    conn.execute("INSERT INTO course(id, site_id, fullname, shortname) VALUES (10, 's', 'C', 'C')")
    conn.commit()
    create_manual_exam(conn, 10, "Lejos", NOW + 40 * DAY, [1], None)
    create_manual_exam(conn, 10, "Cerca", NOW + 10 * DAY, [1], None)
    create_manual_exam(conn, 10, "Pasado", NOW - DAY, [1], None)
    assert exam_horizon(conn, 1, NOW) == NOW + 10 * DAY
    assert topic_probability_at_exam(conn, 1, NOW) == topic_probability(conn, 1, NOW + 10 * DAY) < 1


def test_answers_to_retired_questions_do_not_count(conn):
    conn.execute(
        "INSERT INTO question(id, topic_id, stem, options_json, correct_index, verified, retired) "
        "VALUES (2, 1, 'mal', '[\"x\",\"y\"]', 0, 1, 1)"
    )
    answer(conn, 1, NOW)
    conn.execute("INSERT INTO answer(question_id, chosen_index, correct, at) VALUES (2, 1, 0, ?)", (NOW,))
    commit_answers(conn)
    assert topic_mastery(conn, 1, NOW) == 1.0
    conn.execute("UPDATE question SET retired = 1 WHERE id = 1")
    conn.commit()
    assert topic_mastery(conn, 1, NOW) == 0
