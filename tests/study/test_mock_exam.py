import json
import math

import pytest

from estudia.db import connect
from estudia.study.mastery import topic_mastery
from estudia.study.mock_exam import NotEnoughQuestions, finish_mock, start_mock

NOW = 1_000_000_000


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    c.execute("INSERT INTO material(id, section_id, cmid, kind, title, ref) VALUES (1, 1, 1, 'youtube', 'V', 'yt1')")
    for tid, name in ((1, "Tema A"), (2, "Tema B"), (3, "Tema C")):
        c.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (?, 10, ?, ?)", (tid, f"s{tid}", name))
    c.execute("INSERT INTO course(id, fullname) VALUES (10, 'Curso')")
    c.commit()
    return c


def add_exam(conn, eid=1, topics=(1, 2, 3), max_grade=None, time_limit_s=None):
    conn.execute(
        "INSERT INTO exam(id, course_id, name, opens_at, max_grade, time_limit_s, source, topics_json) "
        "VALUES (?, 10, 'P1', ?, ?, ?, 'manual', ?)",
        (eid, NOW + 1000, max_grade, time_limit_s, json.dumps(list(topics))),
    )
    conn.commit()


def add_q(conn, qid, topic_id=1, verified=1, retired=0):
    conn.execute(
        "INSERT INTO question(id, material_id, topic_id, stem, options_json, correct_index, "
        "explanation, common_error, source_seconds, verified, retired, dedupe_key) "
        "VALUES (?, 1, ?, ?, ?, 1, 'porque sí', 'error', 42, ?, ?, ?)",
        (qid, topic_id, f"p{qid}", json.dumps(["a", "b", "c"]), verified, retired, f"k{qid}"),
    )
    conn.commit()


def fill(conn, n, topics=(1, 2, 3)):
    for i in range(n):
        add_q(conn, i + 1, topic_id=topics[i % len(topics)])


def test_count_formula(conn):
    add_exam(conn)
    fill(conn, 40)
    assert len(start_mock(conn, 1, NOW).items) == 10  # 3 temas x 3 = 9 -> mínimo 10
    add_exam(conn, 2, topics=(1,))
    assert len(start_mock(conn, 2, NOW).items) == 10
    for tid in (4, 5, 6, 7, 8, 9):
        conn.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (?, 10, ?, ?)", (tid, f"s{tid}", f"T{tid}"))
    conn.commit()
    add_exam(conn, 3, topics=(1, 2, 3, 4, 5, 6, 7, 8, 9))
    assert len(start_mock(conn, 3, NOW).items) == 20  # tope


def test_count_limited_to_available_and_spread(conn):
    add_exam(conn)
    fill(conn, 7)
    items = start_mock(conn, 1, NOW).items
    assert len(items) == 7
    assert {i.topic for i in items} == {"Tema A", "Tema B", "Tema C"}
    first_round = {i.topic for i in items[:3]}
    assert len(first_round) == 3


def test_only_verified_unretired(conn):
    add_exam(conn)
    fill(conn, 6)
    add_q(conn, 100, verified=0)
    add_q(conn, 101, retired=1)
    ids = {i.question_id for i in start_mock(conn, 1, NOW).items}
    assert ids == {1, 2, 3, 4, 5, 6}


def test_not_enough_questions(conn):
    add_exam(conn)
    fill(conn, 4)
    with pytest.raises(NotEnoughQuestions):
        start_mock(conn, 1, NOW)
    assert conn.execute("SELECT COUNT(*) FROM mock_attempt").fetchone()[0] == 0


def test_seconds_and_default_scale(conn):
    add_exam(conn)
    fill(conn, 12)
    s = start_mock(conn, 1, NOW)
    assert s.seconds == 90 * 10 and s.max_grade == 10
    add_exam(conn, 2, time_limit_s=600)
    assert start_mock(conn, 2, NOW).seconds == 600


def test_grade_scale_12(conn):
    add_exam(conn, max_grade=12)
    fill(conn, 12)
    s = start_mock(conn, 1, NOW)
    answers = {i.question_id: 1 for i in s.items[:5]}  # 5 de 10 bien
    answers[s.items[5].question_id] = 0
    r = finish_mock(conn, s.attempt_id, answers, NOW + 100)
    assert (r.correct, r.total, r.max_grade, r.grade) == (5, 10, 12, 6.0)
    assert not r.late
    row = conn.execute("SELECT * FROM mock_attempt WHERE id = ?", (s.attempt_id,)).fetchone()
    assert row["grade"] == 6.0 and row["finished_at"] == NOW + 100 and row["answers_json"]


def test_unanswered_count_wrong(conn):
    add_exam(conn)
    fill(conn, 10)
    s = start_mock(conn, 1, NOW)
    r = finish_mock(conn, s.attempt_id, {}, NOW + 5)
    assert (r.correct, r.total, r.grade) == (0, 10, 0.0)
    assert len(r.review) == 10
    assert all(f.chosen_index is None and not f.correct and f.explanation for f in r.review)
    assert conn.execute("SELECT COUNT(*) FROM answer WHERE mode = 'mock' AND correct = 0 "
                        "AND chosen_index IS NULL").fetchone()[0] == 10   # cada una vuelve mañana
    assert set(r.failed_topics) == {"Tema A", "Tema B", "Tema C"}


def test_finish_twice_rejected(conn):
    add_exam(conn)
    fill(conn, 10)
    s = start_mock(conn, 1, NOW)
    finish_mock(conn, s.attempt_id, {}, NOW + 5)
    with pytest.raises(ValueError):
        finish_mock(conn, s.attempt_id, {}, NOW + 6)


def test_bad_index_writes_nothing_and_unknown_ignored(conn):
    add_exam(conn)
    fill(conn, 10)
    s = start_mock(conn, 1, NOW)
    qid = s.items[0].question_id
    with pytest.raises(ValueError):
        finish_mock(conn, s.attempt_id, {qid: 1, s.items[1].question_id: 9}, NOW + 5)
    assert conn.execute("SELECT COUNT(*) FROM answer").fetchone()[0] == 0
    r = finish_mock(conn, s.attempt_id, {qid: 1, 9999: 7}, NOW + 5)
    assert r.correct == 1
    assert conn.execute("SELECT COUNT(*) FROM answer").fetchone()[0] == 10   # 1 respondida + 9 en blanco


def test_late_flag(conn):
    add_exam(conn)
    fill(conn, 10)
    s = start_mock(conn, 1, NOW)
    assert finish_mock(conn, s.attempt_id, {}, NOW + s.seconds + 30).late is False
    s2 = start_mock(conn, 1, NOW)
    assert finish_mock(conn, s2.attempt_id, {}, NOW + s2.seconds + 31).late is True


def test_answers_feed_mastery(conn):
    add_exam(conn, topics=(1,))
    fill(conn, 10, topics=(1,))
    before = topic_mastery(conn, 1, NOW + 10)
    s = start_mock(conn, 1, NOW)
    finish_mock(conn, s.attempt_id, {i.question_id: 1 for i in s.items}, NOW + 5)
    assert conn.execute("SELECT COUNT(*) FROM answer").fetchone()[0] == 10
    assert topic_mastery(conn, 1, NOW + 10) > before


def test_items_do_not_leak_answers(conn):
    add_exam(conn)
    fill(conn, 10)
    for item in start_mock(conn, 1, NOW).items:
        assert not hasattr(item, "correct_index")
        assert "correct_index" not in vars(item)


def test_prefers_questions_not_seen_in_previous_mocks(conn):
    add_exam(conn, topics=(1,))
    fill(conn, 20, topics=(1,))
    s1 = start_mock(conn, 1, NOW)
    finish_mock(conn, s1.attempt_id, {i.question_id: 1 for i in s1.items}, NOW + 5)
    s2 = start_mock(conn, 1, NOW + 10)
    assert not {i.question_id for i in s1.items} & {i.question_id for i in s2.items}


def test_finish_twice_from_two_connections(conn, tmp_path):
    add_exam(conn)
    fill(conn, 10)
    s = start_mock(conn, 1, NOW)
    other = connect(tmp_path / "t.db")
    answers = {i.question_id: 1 for i in s.items}
    finish_mock(conn, s.attempt_id, answers, NOW + 5)
    with pytest.raises(ValueError):
        finish_mock(other, s.attempt_id, answers, NOW + 6)
    assert conn.execute("SELECT COUNT(*) FROM answer").fetchone()[0] == 10


def test_failure_mid_loop_rolls_back_and_retry_works(conn, monkeypatch):
    import estudia.study.mock_exam as me

    add_exam(conn)
    fill(conn, 10)
    s = start_mock(conn, 1, NOW)
    answers = {i.question_id: 1 for i in s.items}
    real, calls = me._insert_answer, []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("falla")
        return real(*args, **kwargs)

    monkeypatch.setattr(me, "_insert_answer", flaky)
    with pytest.raises(RuntimeError):
        finish_mock(conn, s.attempt_id, answers, NOW + 5)
    assert conn.execute("SELECT COUNT(*) FROM answer").fetchone()[0] == 0
    assert conn.execute("SELECT finished_at FROM mock_attempt").fetchone()[0] is None
    monkeypatch.setattr(me, "_insert_answer", real)
    assert finish_mock(conn, s.attempt_id, answers, NOW + 6).correct == 10
    assert conn.execute("SELECT COUNT(*) FROM answer").fetchone()[0] == 10


def test_missing_exam_and_attempt(conn):
    with pytest.raises(LookupError):
        start_mock(conn, 99, NOW)
    with pytest.raises(LookupError):
        finish_mock(conn, 99, {}, NOW)


def test_question_retired_mid_attempt_is_skipped(conn):
    add_exam(conn)
    fill(conn, 10)
    s = start_mock(conn, 1, NOW)
    gone = s.items[0].question_id
    conn.execute("UPDATE question SET retired = 1 WHERE id = ?", (gone,))
    conn.commit()
    r = finish_mock(conn, s.attempt_id, {i.question_id: 1 for i in s.items}, NOW + 5)
    assert r.total == 9 and r.correct == 9 and len(r.review) == 9
    assert conn.execute("SELECT COUNT(*) FROM answer WHERE question_id = ?", (gone,)).fetchone()[0] == 0


def test_all_retired_mid_attempt_keeps_attempt_open(conn):
    add_exam(conn)
    fill(conn, 10)
    s = start_mock(conn, 1, NOW)
    conn.execute("UPDATE question SET retired = 1")
    conn.commit()
    with pytest.raises(NotEnoughQuestions):
        finish_mock(conn, s.attempt_id, {}, NOW + 5)
    assert conn.execute("SELECT finished_at FROM mock_attempt").fetchone()[0] is None


def test_seconds_stored_at_start_drive_late(conn):
    add_exam(conn, time_limit_s=600)
    fill(conn, 10)
    s = start_mock(conn, 1, NOW)
    conn.execute("UPDATE exam SET time_limit_s = 100000")
    conn.commit()
    assert finish_mock(conn, s.attempt_id, {}, NOW + 700).late is True


# --- Tarea 2.4: práctica reciente, escritas, predicción, errores y nota real ------------------

def set_check_expr(conn, qids, expr="2"):
    for qid in qids:
        conn.execute("UPDATE question SET check_expr = ? WHERE id = ?", (expr, qid))
    conn.commit()


def practice(conn, qid, at, mode="mcq"):
    conn.execute(
        "INSERT INTO answer(question_id, chosen_index, correct, at, mode) VALUES (?, 1, 1, ?, ?)",
        (qid, at, mode),
    )
    conn.commit()


def test_mock_avoids_recently_practiced(conn):
    add_exam(conn, topics=(1,))
    fill(conn, 20, topics=(1,))
    for qid in range(1, 11):
        practice(conn, qid, NOW - 86400)              # ayer
    for qid in range(11, 21):
        practice(conn, qid, NOW - 8 * 86400)          # hace más de una semana: no cuenta
    got = {i.question_id for i in start_mock(conn, 1, NOW).items}
    assert got == set(range(11, 21))


def test_mock_recent_practice_is_a_preference_not_a_filter(conn):
    add_exam(conn, topics=(1,))
    fill(conn, 12, topics=(1,))
    for qid in range(1, 10):
        practice(conn, qid, NOW - 3600)
    got = {i.question_id for i in start_mock(conn, 1, NOW).items}
    assert {10, 11, 12} <= got and len(got) == 10


def test_mock_typed_share(conn):
    add_exam(conn, topics=(1,))
    fill(conn, 20, topics=(1,))
    able = set(range(1, 11))
    set_check_expr(conn, able)
    for now in range(NOW, NOW + 12):
        s = start_mock(conn, 1, now)
        with_expr = [i for i in s.items if i.question_id in able]
        typed = [i for i in s.items if i.format == "typed"]
        assert all(i.question_id in able for i in typed)          # sin check_expr nunca se escribe
        if with_expr:
            n = len(with_expr)
            low, high = math.ceil(n * 0.3), math.floor(n * 0.5)
            assert len(typed) == (0 if low > high else min(high, max(low, round(n * 0.4))))
            assert low <= len(typed) <= high or low > high
    s = start_mock(conn, 1, NOW)
    assert [i.format for i in s.items if i.question_id > 10] == ["mcq"] * sum(i.question_id > 10 for i in s.items)


def test_mock_typed_share_none_without_check_expr(conn):
    add_exam(conn)
    fill(conn, 10)
    assert all(i.format == "mcq" for i in start_mock(conn, 1, NOW).items)


def test_prediction_stored(conn):
    add_exam(conn, max_grade=12)
    fill(conn, 10)
    s = start_mock(conn, 1, NOW, predicted_grade=7.456)
    assert s.predicted_grade == 7.46
    row = conn.execute("SELECT predicted_grade FROM mock_attempt WHERE id = ?", (s.attempt_id,)).fetchone()
    assert row["predicted_grade"] == 7.46
    assert finish_mock(conn, s.attempt_id, {}, NOW + 5).predicted_grade == 7.46
    assert start_mock(conn, 1, NOW).predicted_grade is None
    for bad in (-1, 12.5, float("nan"), "7", True):
        with pytest.raises(ValueError):
            start_mock(conn, 1, NOW, predicted_grade=bad)
    assert conn.execute("SELECT COUNT(*) FROM mock_attempt").fetchone()[0] == 2


def test_mock_errors_due_next_day(conn):
    from estudia.study.srs import day_start, local_day
    from datetime import timedelta

    add_exam(conn, topics=(1,))
    fill(conn, 10, topics=(1,))
    s = start_mock(conn, 1, NOW)
    wrong, right = s.items[0].question_id, s.items[1].question_id
    finish_mock(conn, s.attempt_id, {wrong: 0, right: 1}, NOW + 5)   # la correcta es la 1
    rows = {r["question_id"]: r for r in conn.execute("SELECT * FROM answer")}
    assert rows[wrong]["mode"] == "mock" and rows[wrong]["confidence"] is None
    tomorrow = day_start(local_day(NOW + 5) + timedelta(days=1))
    due = conn.execute("SELECT due FROM review_state WHERE card_key = ?", (f"q:{wrong}",)).fetchone()["due"]
    assert due == tomorrow
    good = conn.execute("SELECT due FROM review_state WHERE card_key = ?", (f"q:{right}",)).fetchone()["due"]
    assert good >= tomorrow   # el acierto es Good: nunca antes que mañana


def test_mock_typed_answers_graded_in_same_transaction(conn):
    add_exam(conn, topics=(1,))
    fill(conn, 10, topics=(1,))
    set_check_expr(conn, range(1, 11), "2*sqrt(2)")
    s = start_mock(conn, 1, NOW)
    typed = [i.question_id for i in s.items if i.format == "typed"]
    assert typed
    answers = {qid: {"typed": "2√2"} for qid in typed}
    answers[typed[0]] = {"typed": "3"}                            # error
    mcq = [i.question_id for i in s.items if i.format == "mcq"]
    answers.update({qid: {"index": 1} for qid in mcq})
    r = finish_mock(conn, s.attempt_id, answers, NOW + 5)
    assert r.correct == len(s.items) - 1
    row = conn.execute("SELECT * FROM answer WHERE question_id = ?", (typed[0],)).fetchone()
    assert (row["mode"], row["typed_text"], row["chosen_index"], row["correct"]) == ("mock", "3", None, 0)
    stored = json.loads(conn.execute("SELECT answers_json FROM mock_attempt").fetchone()[0])
    assert stored[str(typed[0])] == {"typed": "3"}


def test_mock_typed_rules_and_unreadable(conn):
    add_exam(conn, topics=(1,))
    fill(conn, 10, topics=(1,))
    set_check_expr(conn, range(1, 11), "2")
    s = start_mock(conn, 1, NOW)
    typed = [i.question_id for i in s.items if i.format == "typed"][0]
    mcq = [i.question_id for i in s.items if i.format == "mcq"][0]
    with pytest.raises(ValueError):                               # escrita en una de opciones
        finish_mock(conn, s.attempt_id, {mcq: {"typed": "2"}}, NOW + 5)
    with pytest.raises(ValueError):                               # las dos formas juntas
        finish_mock(conn, s.attempt_id, {typed: {"typed": "2", "index": 1}}, NOW + 5)
    assert conn.execute("SELECT COUNT(*) FROM answer").fetchone()[0] == 0
    r = finish_mock(conn, s.attempt_id, {typed: {"typed": "hola"}}, NOW + 5)   # ilegible: error
    assert r.correct == 0
    assert conn.execute("SELECT correct FROM answer").fetchone()[0] == 0


def test_exam_result_roundtrip(conn):
    from estudia.study.exams import list_exams, set_exam_result

    add_exam(conn, max_grade=12)
    after = NOW + 2000                                            # el examen abrió en NOW + 1000
    assert list_exams(conn, after)[0].result is None
    assert set_exam_result(conn, 1, 8.5, after) == {"grade": 8.5, "max_grade": 12.0, "at": after}
    assert list_exams(conn, after)[0].result == {"grade": 8.5, "max_grade": 12.0, "at": after}
    set_exam_result(conn, 1, 9, after + 1)                       # corregirla pisa la anterior
    assert conn.execute("SELECT COUNT(*) FROM exam_result").fetchone()[0] == 1
    assert list_exams(conn, after)[0].result["grade"] == 9
    for bad in (-1, 12.5, float("inf"), True):
        with pytest.raises(ValueError):
            set_exam_result(conn, 1, bad, after)
    with pytest.raises(LookupError):
        set_exam_result(conn, 99, 5, after)


def test_exam_result_rejected_before_exam_and_other_scale(conn):
    from estudia.study.exams import set_exam_result

    add_exam(conn, max_grade=12)
    with pytest.raises(ValueError):                               # el examen todavía no empezó
        set_exam_result(conn, 1, 8, NOW + 999)
    assert conn.execute("SELECT COUNT(*) FROM exam_result").fetchone()[0] == 0
    assert set_exam_result(conn, 1, 8, NOW + 1000)["max_grade"] == 12.0
    with pytest.raises(ValueError):                               # escala distinta de la del examen
        set_exam_result(conn, 1, 8, NOW + 1000, max_grade=10)
    assert set_exam_result(conn, 1, 8, NOW + 1000, max_grade=12)["max_grade"] == 12.0


def test_mock_typed_grading_unavailable_leaves_attempt_retryable(conn, monkeypatch):
    import estudia.study.practice as pr
    from estudia.generation.verify import GradingUnavailable

    add_exam(conn, topics=(1,))
    fill(conn, 10, topics=(1,))
    set_check_expr(conn, range(1, 11), "2")
    s = start_mock(conn, 1, NOW)
    answers = {i.question_id: ({"typed": "2"} if i.format == "typed" else {"index": 1}) for i in s.items}
    real = pr.grade_typed

    def down(*a, **k):
        raise GradingUnavailable("worker caído")

    monkeypatch.setattr(pr, "grade_typed", down)
    with pytest.raises(GradingUnavailable):
        finish_mock(conn, s.attempt_id, answers, NOW + 5)
    assert conn.execute("SELECT COUNT(*) FROM answer").fetchone()[0] == 0
    assert conn.execute("SELECT finished_at FROM mock_attempt").fetchone()[0] is None
    monkeypatch.setattr(pr, "grade_typed", real)
    assert finish_mock(conn, s.attempt_id, answers, NOW + 6).correct == len(s.items)
    with pytest.raises(ValueError):                               # segunda entrega
        finish_mock(conn, s.attempt_id, answers, NOW + 7)
    assert conn.execute("SELECT COUNT(*) FROM answer").fetchone()[0] == len(s.items)


def test_mock_typed_grading_runs_outside_transaction(conn, monkeypatch):
    import estudia.study.practice as pr

    add_exam(conn, topics=(1,))
    fill(conn, 10, topics=(1,))
    set_check_expr(conn, range(1, 11), "2")
    s = start_mock(conn, 1, NOW)
    answers = {i.question_id: ({"typed": "2"} if i.format == "typed" else {"index": 1}) for i in s.items}
    real, seen = pr.grade_typed, []

    def spy(*a, **k):
        seen.append(conn.in_transaction)
        return real(*a, **k)

    monkeypatch.setattr(pr, "grade_typed", spy)
    finish_mock(conn, s.attempt_id, answers, NOW + 5)
    assert seen and not any(seen)


def test_mock_unanswered_scheduled_for_tomorrow(conn):
    from estudia.study.srs import day_start, local_day
    from datetime import timedelta

    add_exam(conn, topics=(1,))
    fill(conn, 10, topics=(1,))
    s = start_mock(conn, 1, NOW)
    blank = s.items[0].question_id
    answers = {i.question_id: 1 for i in s.items[1:]}
    r = finish_mock(conn, s.attempt_id, answers, NOW + 5)
    assert r.correct == len(s.items) - 1
    row = conn.execute("SELECT * FROM answer WHERE question_id = ?", (blank,)).fetchone()
    assert (row["mode"], row["correct"], row["chosen_index"], row["typed_text"]) == ("mock", 0, None, None)
    tomorrow = day_start(local_day(NOW + 5) + timedelta(days=1))
    due = conn.execute("SELECT due FROM review_state WHERE card_key = ?", (f"q:{blank}",)).fetchone()["due"]
    assert due == tomorrow
    assert 0 <= topic_mastery(conn, 1, NOW + 10) <= 1                        # los lectores toleran chosen_index NULL


def test_review_entries_carry_question_id_and_stored_result_matches(conn):
    from estudia.study.mock_exam import mock_result

    add_exam(conn)
    fill(conn, 12)
    attempt = start_mock(conn, 1, NOW)
    ids = [i.question_id for i in attempt.items]
    with pytest.raises(ValueError):
        mock_result(conn, attempt.attempt_id)   # todavía no terminó
    # Una se retira en el medio: la revisión dice de qué pregunta es cada entrada.
    conn.execute("UPDATE question SET retired = 1 WHERE id = ?", (ids[0],))
    conn.commit()
    answers = {ids[1]: 1, ids[2]: 0}
    result = finish_mock(conn, attempt.attempt_id, answers, NOW + 60)
    assert [r.question_id for r in result.review] == ids[1:]
    stored = mock_result(conn, attempt.attempt_id)
    assert (stored.grade, stored.correct, stored.total, stored.failed_topics) == (
        result.grade, result.correct, result.total, result.failed_topics)
    assert [(r.question_id, r.correct, r.chosen_index) for r in stored.review] == [
        (r.question_id, r.correct, r.chosen_index) for r in result.review]
    with pytest.raises(LookupError):
        mock_result(conn, 999)


def test_mock_never_types_a_question_whose_distractor_the_typed_rule_accepts(conn):
    add_exam(conn, topics=(1,))
    fill(conn, 10, topics=(1,))
    set_check_expr(conn, range(1, 11), "sqrt(2)")
    bad = {1, 2, 3, 4, 5}
    for qid in bad:                                   # "1.41" distractor: escrita valdría
        conn.execute("UPDATE question SET options_json = ? WHERE id = ?",
                     (json.dumps(["1.41", "√2", "2"]), qid))
    for qid in set(range(6, 11)):
        conn.execute("UPDATE question SET options_json = ? WHERE id = ?",
                     (json.dumps(["1.4", "√2", "2"]), qid))
    conn.commit()
    for now in range(NOW, NOW + 10):
        s = start_mock(conn, 1, now)
        typed = {i.question_id for i in s.items if i.format == "typed"}
        assert typed and not typed & bad
        assert not set(json.loads(conn.execute(
            "SELECT typed_json FROM mock_attempt WHERE id = ?", (s.attempt_id,)).fetchone()[0])) & bad
    flags = dict(conn.execute("SELECT id, typed_safe FROM question").fetchall())
    assert {flags[q] for q in bad} == {0} and {flags[q] for q in range(6, 11)} == {1}


def test_mock_not_typeable_during_grading_counts_as_wrong(conn):
    add_exam(conn, topics=(1,))
    fill(conn, 10, topics=(1,))
    set_check_expr(conn, range(1, 11), "2")
    s = start_mock(conn, 1, NOW)
    typed = [i.question_id for i in s.items if i.format == "typed"]
    assert typed
    conn.execute("UPDATE question SET check_expr = 'x+1' WHERE id = ?", (typed[0],))   # ya no corrige
    conn.commit()
    r = finish_mock(conn, s.attempt_id, {typed[0]: {"typed": "2"}}, NOW + 5)
    assert r.correct == 0
    row = conn.execute("SELECT correct, typed_text FROM answer WHERE question_id = ?", (typed[0],)).fetchone()
    assert (row["correct"], row["typed_text"]) == (0, "2")


def test_document_question_has_no_youtube_link_in_review(conn):
    """Una pregunta de un PDF no tiene video: nunca un `youtube_id` armado con la ruta del archivo."""
    from estudia.study.mock_exam import mock_result

    conn.execute("UPDATE material SET kind='pdf', ref='documentos/1.pdf' WHERE id=1")
    conn.commit()
    add_exam(conn)
    fill(conn, 10)
    s = start_mock(conn, 1, NOW)
    r = finish_mock(conn, s.attempt_id, {i.question_id: 0 for i in s.items}, NOW + 100)
    assert r.review and all(f.youtube_id is None for f in r.review)
    again = mock_result(conn, s.attempt_id)
    assert again.review and all(f.youtube_id is None for f in again.review)


def test_video_question_keeps_youtube_link_in_review(conn):
    from estudia.study.mock_exam import mock_result

    add_exam(conn)
    fill(conn, 10)
    s = start_mock(conn, 1, NOW)
    r = finish_mock(conn, s.attempt_id, {i.question_id: 0 for i in s.items}, NOW + 100)
    assert {f.youtube_id for f in r.review} == {"yt1"}
    assert {f.youtube_id for f in mock_result(conn, s.attempt_id).review} == {"yt1"}


def test_mock_review_carries_source_label(conn, monkeypatch):
    from estudia.catalog import Site
    from estudia.study import citation
    from estudia.study.mock_exam import mock_result

    conn.execute("INSERT INTO kv(key, value) VALUES ('site_id', 'x')")
    monkeypatch.setattr(citation, "get_site",
                        lambda sid: Site(sid, "X", "X", "https://campus.example.edu", "browser"))
    conn.execute("UPDATE material SET kind='pdf', title='Apunte 3', ref='documentos/1.pdf',"
                 " module_kind='resource', module_id=55 WHERE id=1")
    conn.commit()
    add_exam(conn)
    fill(conn, 10)
    conn.execute("UPDATE question SET source_seconds=NULL, source_page=14")
    conn.commit()
    s = start_mock(conn, 1, NOW)
    r = finish_mock(conn, s.attempt_id, {i.question_id: 0 for i in s.items}, NOW + 100)
    for review in (r.review, mock_result(conn, s.attempt_id).review):
        assert {(f.source_label, f.source_page, f.source_url) for f in review} == {
            ("Apunte 3, pág. 14", 14, "https://campus.example.edu/mod/resource/view.php?id=55")}


# --- Variantes ---------------------------------------------------------------------------------

def make_group(conn, head, members):
    conn.execute(f"UPDATE question SET variant_group = ? WHERE id IN ({','.join('?' * (1 + len(members)))})",
                 (head, head, *members))
    conn.commit()


def test_mock_prefers_unseen_variant(conn):
    from estudia.study.practice import record_answer
    add_exam(conn)
    fill(conn, 8)                     # 1..8, en los temas 1, 2 y 3
    make_group(conn, 1, [4])          # 1 y 4 son del tema 1
    record_answer(conn, 1, 1, NOW - 2 * 86400)   # A practicada hace 2 días
    for seed in range(8):
        start = start_mock(conn, 1, NOW + seed)
        ids = {i.question_id for i in start.items}
        assert 4 in ids and 1 not in ids   # sin entregar: el intento no cuenta como visto


def test_mock_never_two_members_of_a_group(conn):
    add_exam(conn)
    fill(conn, 9)
    make_group(conn, 1, [4, 7])       # tema 1
    make_group(conn, 2, [5])          # tema 2
    for seed in range(6):
        items = start_mock(conn, 1, NOW + seed).items
        assert len(items) == len({i.card_key for i in items}) == 6
        assert {i.card_key for i in items} >= {"v:1", "v:2"}


def test_mock_counts_cards_for_the_minimum(conn):
    add_exam(conn)
    fill(conn, 6)
    make_group(conn, 1, [4])          # 6 filas, 5 tarjetas: alcanza
    assert len(start_mock(conn, 1, NOW).items) == 5
    make_group(conn, 2, [5])          # 4 tarjetas: no alcanza
    with pytest.raises(NotEnoughQuestions):
        start_mock(conn, 1, NOW)
