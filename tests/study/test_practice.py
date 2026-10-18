import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from estudia.db import connect
from estudia.study.practice import (
    NotTypeable,
    UnreadableAnswer,
    build_quiz,
    build_session_quiz,
    record_answer,
    retire_question,
)

NOW = 1_000_000_000
DAY = 86400
TZ = ZoneInfo("America/Argentina/Buenos_Aires")


def local(day: int, hour: int = 0, minute: int = 0) -> int:
    return int(datetime(2026, 10, day, hour, minute, tzinfo=TZ).timestamp())


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    c.execute("INSERT INTO material(id, section_id, cmid, kind, title, ref) VALUES (1, 1, 1, 'youtube', 'V', 'yt123')")
    c.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (1, 10, 'a', 'Tema A')")
    c.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (2, 10, 'b', 'Tema B')")
    c.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (3, 20, 'c', 'Otro curso')")
    c.commit()
    return c


def add_q(conn, qid, topic_id=1, verified=1, retired=0, check_expr=None):
    conn.execute(
        "INSERT INTO question(id, material_id, topic_id, stem, options_json, correct_index, "
        "explanation, common_error, source_seconds, verified, retired, dedupe_key, check_expr) "
        "VALUES (?, 1, ?, ?, ?, 1, 'porque sí', 'error típico', 42, ?, ?, ?, ?)",
        (qid, topic_id, f"p{qid}", json.dumps(["a", "b", "c"]), verified, retired, f"k{qid}", check_expr),
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


def test_failed_question_leaves_today_queue_after_one_hit(conn):
    for i in range(1, 6):
        add_q(conn, i)
    record_answer(conn, 3, 0, NOW)  # falla: vuelve en la misma sesión
    for t in (1, 2, 3, 4):
        assert ids(build_quiz(conn, 10, topic_id=1, size=1, now=NOW + t)) == [3]
    record_answer(conn, 3, 1, NOW + 10)  # un acierto alcanza para hoy
    assert ids(build_quiz(conn, 10, topic_id=1, size=5, now=NOW + 11))[-1] == 3
    # pero sigue en reaprendizaje: al día siguiente vuelve primera
    assert ids(build_quiz(conn, 10, topic_id=1, size=1, now=NOW + DAY))[0] == 3


def test_quiz_order_due_first(conn):
    now = local(17, 15, 30)
    for i in range(1, 8):
        add_q(conn, i)
    record_answer(conn, 1, 1, now - 30 * DAY)   # vencida hace mucho
    record_answer(conn, 2, 1, now - 10 * DAY)   # vencida hace menos
    record_answer(conn, 3, 1, now - 3600)       # vista hace una hora: no vence
    record_answer(conn, 4, 2, now - 600)        # error de esta sesión
    record_answer(conn, 7, 2, now - 1200)       # otro error de esta sesión, anterior
    record_answer(conn, 5, 2, now - 2 * DAY)    # en reaprendizaje ...
    record_answer(conn, 5, 1, now - 2 * DAY + 60)
    # ... con la próxima fecha más tarde hoy (como la dejaría un examen cercano)
    conn.execute("UPDATE review_state SET due = ? WHERE card_key = 'q:5'", (now + 1800,))
    conn.commit()
    # 6: nunca vista
    assert ids(build_quiz(conn, 10, now=now)) == [7, 4, 1, 2, 5, 6, 3]
    # las vencidas no se las saltea el límite: van antes que las nuevas
    assert ids(build_quiz(conn, 10, size=3, now=now)) == [7, 4, 1]


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


def count_answers(conn):
    return conn.execute("SELECT COUNT(*) FROM answer").fetchone()[0]


# --- Formato, fase y reaprendizaje de cada ítem -----------------------------------------------

def test_quiz_item_formats(conn):
    now = local(17, 15)
    add_q(conn, 1)                                   # nueva: opciones
    add_q(conn, 2)                                   # vista, sin check_expr: pensalo primero
    record_answer(conn, 2, 0, now - 3 * DAY)
    add_q(conn, 3, check_expr="2")                   # con check_expr y ya acertada: escrita
    record_answer(conn, 3, 1, now - 3 * DAY)
    for i in range(4, 24):                           # con check_expr, nunca acertadas: mitad y mitad
        add_q(conn, i, check_expr="2")
        record_answer(conn, i, 0, now - 3 * DAY)
    add_q(conn, 24, check_expr="2")                  # nueva con check_expr: opciones
    quiz = build_quiz(conn, 10, size=50, now=now)
    fmt = {i.question_id: i.format for i in quiz}
    assert (fmt[1], fmt[2], fmt[3], fmt[24]) == ("mcq", "think_first", "typed", "mcq")
    coin = [fmt[i] for i in range(4, 24)]
    assert set(coin) == {"typed", "think_first"}
    assert 4 <= coin.count("typed") <= 16
    assert [i.format for i in build_quiz(conn, 10, size=50, now=now)] == [i.format for i in quiz]
    assert {i.phase for i in quiz} == {"focus"}      # sin sesión, todo es foco


def test_quiz_item_relearning_day(conn):
    add_q(conn, 1)
    add_q(conn, 2)

    def relearning(now):
        return {i.question_id: i.relearning for i in build_quiz(conn, 10, now=now)}

    record_answer(conn, 1, 0, local(17, 10))                      # error: arranca el reaprendizaje
    assert relearning(local(17, 10, 1)) == {1: {"day": 1}, 2: None}
    record_answer(conn, 1, 1, local(17, 10, 2))                   # 1.er día cumplido
    assert relearning(local(18, 10))[1] == {"day": 2}
    record_answer(conn, 1, 1, local(18, 10, 1))                   # 2.º día cumplido
    assert relearning(local(20, 10))[1] == {"day": 3}
    record_answer(conn, 1, 1, local(20, 10, 1))                   # 3.er día: se gradúa
    assert relearning(local(20, 10, 2))[1] is None


# --- Sesión del plan ----------------------------------------------------------------------------

def add_session(conn, sid, kind, topic_id, topics, minutes=50, starts_at=NOW):
    conn.execute(
        "INSERT INTO plan_session(id, course_id, topic_id, exam_id, starts_at, minutes, done, kind, "
        "topics_json) VALUES (?, 10, ?, NULL, ?, ?, 0, ?, ?)",
        (sid, topic_id, starts_at, minutes, kind, json.dumps(topics)),
    )
    conn.commit()


@pytest.fixture
def session_bank(conn):
    """Tema A: 1 a 6 nuevas y 7 vencida. Tema B: 11 y 12 vencidas y 13 nueva."""
    now = local(17, 19)
    for i in range(1, 8):
        add_q(conn, i, topic_id=1)
    for i in (11, 12, 13):
        add_q(conn, i, topic_id=2)
    record_answer(conn, 11, 1, now - 30 * DAY)
    record_answer(conn, 7, 1, now - 25 * DAY)
    record_answer(conn, 12, 1, now - 20 * DAY)
    return now


def test_session_quiz_mixed_focus_review_then_focus(conn, session_bank):
    now = session_bank
    add_session(conn, 1, "mixed_focus", 1, [1, 2], minutes=50)
    quiz = build_session_quiz(conn, 1, size=10, now=now)
    # 10 de 50 minutos de repaso: 2 de 10 ítems, las más atrasadas de todos los temas.
    assert [i.phase for i in quiz] == ["review"] * 2 + ["focus"] * 6
    assert ids(quiz)[:2] == [11, 7]
    assert {i.topic for i in quiz[2:]} == {"Tema A"}
    assert sorted(ids(quiz)[2:]) == [1, 2, 3, 4, 5, 6]


def test_session_quiz_mixed_only_is_all_review_with_new_ones(conn, session_bank):
    now = session_bank
    add_session(conn, 2, "mixed_only", 1, [1, 2], minutes=50)
    quiz = build_session_quiz(conn, 2, size=20, now=now)
    assert {i.phase for i in quiz} == {"review"}
    assert ids(quiz)[:3] == [11, 7, 12]
    assert sorted(ids(quiz)) == [1, 2, 3, 4, 5, 6, 7, 11, 12, 13]


def test_session_quiz_focus_only_its_topic(conn, session_bank):
    now = session_bank
    add_session(conn, 3, "focus", 2, [2], minutes=25)
    quiz = build_session_quiz(conn, 3, size=10, now=now)
    assert ids(quiz) == [11, 12, 13]
    assert {i.phase for i in quiz} == {"focus"}


def test_session_quiz_unknown_session(conn):
    with pytest.raises(LookupError):
        build_session_quiz(conn, 99, size=10, now=NOW)


# --- Respuesta: orden mostrado, respuesta escrita y feedback -----------------------------------

def test_shown_order_maps_chosen_to_original(conn):
    add_q(conn, 1)   # opciones a, b, c; correcta la 1 ("b")
    fb = record_answer(conn, 1, 0, NOW, option_order=[1, 0, 2])   # tocó la primera que vio: "b"
    assert (fb.correct, fb.chosen_index, fb.correct_index) == (True, 1, 1)
    row = conn.execute("SELECT chosen_index, correct, option_order_json FROM answer").fetchone()
    assert (row["chosen_index"], row["correct"], json.loads(row["option_order_json"])) == (1, 1, [1, 0, 2])


@pytest.mark.parametrize("order", [[0, 1], [0, 0, 1], [0, 1, 5], [2, 1, 0, 3]])
def test_shown_order_must_be_a_permutation(conn, order):
    add_q(conn, 1)
    with pytest.raises(ValueError):
        record_answer(conn, 1, 0, NOW, option_order=order)
    assert count_answers(conn) == 0


def test_typed_answer_graded_against_check_expr(conn):
    add_q(conn, 1, check_expr="2*sqrt(2)")
    ok = record_answer(conn, 1, None, NOW, mode="typed", typed_text="2 raiz 2", confidence=2)
    assert (ok.correct, ok.typed_ok, ok.chosen_index) == (True, True, None)
    bad = record_answer(conn, 1, None, NOW + 1, mode="typed", typed_text="3", confidence=2)
    assert (bad.correct, bad.typed_ok) == (False, False)
    rows = conn.execute("SELECT chosen_index, correct, typed_text, mode FROM answer ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [(None, 1, "2 raiz 2", "typed"), (None, 0, "3", "typed")]
    # Con opciones, typed_ok no aplica.
    assert record_answer(conn, 1, 1, NOW + 2).typed_ok is None


def test_typed_answer_errors_write_nothing(conn):
    add_q(conn, 1, check_expr="2")
    add_q(conn, 2)
    with pytest.raises(UnreadableAnswer):
        record_answer(conn, 1, None, NOW, mode="typed", typed_text="dos")
    with pytest.raises(NotTypeable):
        record_answer(conn, 2, None, NOW, mode="typed", typed_text="2")
    for kwargs in ({"mode": "typed", "typed_text": None}, {"mode": "mcq"}):
        with pytest.raises(ValueError):
            record_answer(conn, 1, None, NOW, **kwargs)
    with pytest.raises(ValueError):   # escrita no lleva opción elegida
        record_answer(conn, 1, 0, NOW, mode="typed", typed_text="2")
    with pytest.raises(ValueError):   # ni orden de opciones
        record_answer(conn, 1, None, NOW, mode="typed", typed_text="2", option_order=[0, 1, 2])
    assert count_answers(conn) == 0


def test_feedback_next_label_requeue_and_progress(conn):
    add_q(conn, 1)
    add_q(conn, 2)
    add_q(conn, 3)
    fb = record_answer(conn, 1, 0, local(17, 10), confidence=2)
    assert (fb.requeue, fb.next_label, fb.relearn_progress, fb.feedback_style) == (
        True, "Vuelve al final de la sesión", "0 de 3 días", "alarm")
    fb = record_answer(conn, 1, 1, local(17, 10, 5), confidence=2)
    assert (fb.requeue, fb.next_label, fb.relearn_progress) == (False, "Vuelve mañana", "1 de 3 días")
    fb = record_answer(conn, 1, 1, local(18, 10), confidence=2)
    assert (fb.next_label, fb.relearn_progress) == ("Vuelve en 2 días", "2 de 3 días")
    fb = record_answer(conn, 1, 1, local(20, 10), confidence=2)
    assert re.fullmatch(r"La sabés: vuelve (mañana|en \d+ días)", fb.next_label), fb.next_label
    assert fb.relearn_progress is None
    # Acierto de suerte: vuelve en la sesión, aunque sea acierto.
    guess = record_answer(conn, 2, 1, local(17, 10), confidence=0)
    assert (guess.correct, guess.requeue, guess.feedback_style) == (True, True, "doubt")
    # Acierto con duda: vuelve, pero no "la sabés".
    unsure = record_answer(conn, 3, 1, local(17, 10), confidence=1)
    assert unsure.requeue is False
    assert re.fullmatch(r"Vuelve (mañana|en \d+ días)", unsure.next_label), unsure.next_label


# --- Preguntas ya guardadas cuyo distractor aceptaría la regla escrita ----------------------

def add_contradictory(conn, qid, check_expr="sqrt(2)"):
    """"1.41" es distractor (opción múltiple: mal) pero escrita, por √2, valdría: no se escribe nunca."""
    conn.execute(
        "INSERT INTO question(id, material_id, topic_id, stem, options_json, correct_index, "
        "explanation, common_error, source_seconds, verified, retired, dedupe_key, check_expr) "
        "VALUES (?, 1, 1, ?, ?, 1, 'porque sí', 'error típico', 42, 1, 0, ?, ?)",
        (qid, f"p{qid}", json.dumps(["1.41", "√2", "2"]), f"k{qid}", check_expr),
    )
    conn.commit()


def typed_safe(conn, qid):
    return conn.execute("SELECT typed_safe FROM question WHERE id = ?", (qid,)).fetchone()[0]


def test_existing_contradictory_question_is_never_served_typed(conn):
    now = local(17, 15)
    add_contradictory(conn, 1)
    record_answer(conn, 1, 1, now - 3 * DAY)        # ya acertada: sería escrita
    add_q(conn, 2, check_expr="2")                  # sana: sigue escrita
    record_answer(conn, 2, 1, now - 3 * DAY)
    for t in range(now, now + 20):
        fmt = {i.question_id: i.format for i in build_quiz(conn, 10, size=50, now=t)}
        assert fmt[1] == "think_first" and fmt[2] == "typed"
    assert (typed_safe(conn, 1), typed_safe(conn, 2)) == (0, 1)   # quedó anotado: no se recalcula


def test_flagged_question_uses_the_cache_without_the_worker(conn, monkeypatch):
    from estudia.study import practice as pr

    now = local(17, 15)
    add_q(conn, 1, check_expr="2")
    record_answer(conn, 1, 1, now - 3 * DAY)
    conn.execute("UPDATE question SET typed_safe = 0 WHERE id = 1")
    conn.commit()
    monkeypatch.setattr(pr, "typed_accepts_option", lambda *a: pytest.fail("usa el worker"))
    assert [i.format for i in build_quiz(conn, 10, now=now)] == ["think_first"]


def test_unknown_typed_safety_is_not_typed_and_not_cached(conn, monkeypatch):
    from estudia.study import practice as pr

    now = local(17, 15)
    add_q(conn, 1, check_expr="2")
    record_answer(conn, 1, 1, now - 3 * DAY)
    monkeypatch.setattr(pr, "typed_accepts_option", lambda *a: None)   # worker lento o caído
    assert [i.format for i in build_quiz(conn, 10, now=now)] == ["think_first"]
    assert typed_safe(conn, 1) is None


def test_contradictory_question_is_still_offered_as_multiple_choice(conn):
    now = local(17, 15)
    add_contradictory(conn, 1)
    assert [i.format for i in build_quiz(conn, 10, now=now)] == ["mcq"]
    fb = record_answer(conn, 1, 1, now)
    assert fb.correct is True


def test_typed_answer_to_a_flagged_question_is_refused_without_writing(conn):
    add_contradictory(conn, 1)
    conn.execute("UPDATE question SET typed_safe = 0 WHERE id = 1")
    conn.commit()
    with pytest.raises(NotTypeable):
        record_answer(conn, 1, None, NOW, mode="typed", typed_text="1.41")
    assert count_answers(conn) == 0


# --- Worker caído: corte en la primera duda y espera antes de volver a probar -------------------

def _seen_correct(conn, qids, now):
    for qid in qids:
        add_q(conn, qid, check_expr="2")
        record_answer(conn, qid, 1, now - 3 * DAY)


def test_failing_worker_is_tried_once_within_the_backoff_and_is_fast(conn, monkeypatch):
    import time as _time

    from estudia.generation import verify

    now = local(17, 15)
    _seen_correct(conn, range(1, 6), now)
    starts = []

    def failing_start():
        starts.append(1)
        return False

    verify._stop_worker()
    monkeypatch.setattr(verify, "_start_worker", failing_start)
    t0 = _time.monotonic()
    first = build_quiz(conn, 10, now=now)
    second = build_quiz(conn, 10, now=now + 1)
    assert _time.monotonic() - t0 < 1.0
    assert len(starts) <= 1
    assert {i.format for i in first + second} == {"think_first"}
    assert {r[0] for r in conn.execute("SELECT typed_safe FROM question")} == {None}


def test_first_unknown_stops_the_batch_and_writes_no_verdicts(conn, monkeypatch):
    from estudia.study import practice as pr

    now = local(17, 15)
    _seen_correct(conn, range(1, 6), now)
    calls = []

    def unknown(expr, option):
        calls.append(option)
        return None

    monkeypatch.setattr(pr, "typed_accepts_option", unknown)
    build_quiz(conn, 10, now=now)
    assert len(calls) == 1


def test_no_transaction_is_open_during_worker_calls(conn, monkeypatch):
    from estudia.study import practice as pr

    now = local(17, 15)
    _seen_correct(conn, range(1, 4), now)
    real, open_during = pr.typed_accepts_option, []

    def spy(expr, option):
        open_during.append(conn.in_transaction)
        return real(expr, option)

    monkeypatch.setattr(pr, "typed_accepts_option", spy)
    items = build_quiz(conn, 10, now=now)
    assert open_during and not any(open_during)
    assert {i.format for i in items} == {"typed"}
    assert {r[0] for r in conn.execute("SELECT typed_safe FROM question")} == {1}
    assert not conn.in_transaction


def test_typed_answer_to_a_flagged_question_points_to_the_options(conn):
    add_contradictory(conn, 1)
    conn.execute("UPDATE question SET typed_safe = 0 WHERE id = 1")
    conn.commit()
    with pytest.raises(NotTypeable, match="Esta pregunta va con opciones. Tocá «Mostrame opciones»."):
        record_answer(conn, 1, None, NOW, mode="typed", typed_text="1.41")
