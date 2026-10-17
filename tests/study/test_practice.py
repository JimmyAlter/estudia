import json

import pytest

from estudia.db import connect
from estudia.study.practice import (
    build_quiz,
    record_answer,
    retire_question,
)

NOW = 1_000_000_000


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    c.execute("INSERT INTO material(id, section_id, cmid, kind, title, ref) VALUES (1, 1, 1, 'youtube', 'V', 'yt123')")
    c.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (1, 10, 'a', 'Tema A')")
    c.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (2, 10, 'b', 'Tema B')")
    c.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (3, 20, 'c', 'Otro curso')")
    c.commit()
    return c


def add_q(conn, qid, topic_id=1, verified=1, retired=0):
    conn.execute(
        "INSERT INTO question(id, material_id, topic_id, stem, options_json, correct_index, "
        "explanation, common_error, source_seconds, verified, retired, dedupe_key) "
        "VALUES (?, 1, ?, ?, ?, 1, 'porque sí', 'error típico', 42, ?, ?, ?)",
        (qid, topic_id, f"p{qid}", json.dumps(["a", "b", "c"]), verified, retired, f"k{qid}"),
    )
    conn.commit()


def ids(quiz):
    return [i.question_id for i in quiz]


def test_quiz_excludes_unverified_and_retired(conn):
    add_q(conn, 1)
    add_q(conn, 2, verified=0)
    add_q(conn, 3, retired=1)
    add_q(conn, 4, topic_id=3)
    assert ids(build_quiz(conn, 10, now=NOW)) == [1]


def test_quiz_items_do_not_leak_answer(conn):
    add_q(conn, 1)
    item = build_quiz(conn, 10, now=NOW)[0]
    assert not hasattr(item, "correct_index")
    assert "correct_index" not in vars(item)
    assert item.topic == "Tema A"
    assert item.options == ["a", "b", "c"]


def test_failed_question_returns_until_two_in_a_row(conn):
    for i in range(1, 6):
        add_q(conn, i)
    record_answer(conn, 3, 0, NOW)  # falla
    for t in (1, 2, 3, 4):
        assert ids(build_quiz(conn, 10, topic_id=1, size=1, now=NOW + t)) == [3]
    record_answer(conn, 3, 1, NOW + 10)  # un acierto no alcanza
    assert ids(build_quiz(conn, 10, topic_id=1, size=1, now=NOW + 11)) == [3]
    record_answer(conn, 3, 1, NOW + 20)  # segundo acierto seguido
    # ya sin prioridad: va detrás de las nunca respondidas
    assert ids(build_quiz(conn, 10, topic_id=1, size=5, now=NOW + 99))[-1] == 3


def test_fail_after_two_hits_is_pending_again(conn):
    add_q(conn, 1)
    add_q(conn, 2)
    for r in (0, 1, 1):
        record_answer(conn, 1, r, NOW)
    record_answer(conn, 2, 1, NOW)
    record_answer(conn, 1, 0, NOW + 5)
    assert ids(build_quiz(conn, 10, size=1, now=NOW + 6)) == [1]


def test_never_answered_before_already_answered(conn):
    add_q(conn, 1)
    add_q(conn, 2)
    record_answer(conn, 1, 1, NOW)
    assert ids(build_quiz(conn, 10, now=NOW + 1)) == [2, 1]


def test_quiz_deterministic_per_now_and_size_limit(conn):
    for i in range(1, 8):
        add_q(conn, i)
    a = build_quiz(conn, 10, size=4, now=NOW)
    assert a == build_quiz(conn, 10, size=4, now=NOW)
    assert len(a) == 4


def test_weakest_picks_lowest_mastery_topic(conn):
    add_q(conn, 1, topic_id=1)
    add_q(conn, 2, topic_id=2)
    record_answer(conn, 1, 1, NOW)
    record_answer(conn, 2, 0, NOW)
    assert ids(build_quiz(conn, 10, weakest=True, now=NOW + 1)) == [2]
    # topic_id gana sobre weakest
    assert ids(build_quiz(conn, 10, topic_id=1, weakest=True, now=NOW + 1)) == [1]


def test_weakest_without_questions_is_empty(conn):
    add_q(conn, 1, verified=0)
    assert build_quiz(conn, 10, weakest=True, now=NOW) == []


def test_record_answer_feedback(conn):
    add_q(conn, 1)
    ok = record_answer(conn, 1, 1, NOW)
    assert (ok.correct, ok.correct_index, ok.explanation) == (True, 1, "porque sí")
    assert (ok.common_error, ok.source_seconds, ok.youtube_id) == ("error típico", 42, "yt123")
    assert record_answer(conn, 1, 2, NOW).correct is False
    assert conn.execute("SELECT COUNT(*) FROM answer").fetchone()[0] == 2


@pytest.mark.parametrize("kwargs", [{"verified": 0}, {"retired": 1}])
def test_record_answer_unavailable_raises_without_writing(conn, kwargs):
    add_q(conn, 1, **kwargs)
    with pytest.raises(LookupError):
        record_answer(conn, 1, 0, NOW)
    with pytest.raises(LookupError):
        record_answer(conn, 999, 0, NOW)
    assert conn.execute("SELECT COUNT(*) FROM answer").fetchone()[0] == 0


@pytest.mark.parametrize("bad", [-1, 3])
def test_record_answer_out_of_range(conn, bad):
    add_q(conn, 1)
    with pytest.raises(ValueError):
        record_answer(conn, 1, bad, NOW)
    assert conn.execute("SELECT COUNT(*) FROM answer").fetchone()[0] == 0


def test_retire_question_removes_it_from_quiz(conn):
    add_q(conn, 1)
    add_q(conn, 2)
    retire_question(conn, 1)
    assert ids(build_quiz(conn, 10, now=NOW)) == [2]
