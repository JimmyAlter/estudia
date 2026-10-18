""""3 preguntas para arrancar": oferta, armado y que no deje rastro fuera de la tabla `answer`."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from estudia.db import connect
from estudia.study.exams import create_manual_exam
from estudia.study.home import home_view
from estudia.study.mastery import topic_probability
from estudia.study.mock_exam import _recently_practiced
from estudia.study.planner import mark_done_from_answers
from estudia.study.practice import MCQ, build_quiz, record_answer
from estudia.study.pretest import (
    PRETEST_SIZE,
    PretestOffer,
    build_pretest,
    close_pretest,
    pretest_offer,
    record_pretest_answer,
)
from estudia.study.progress import calibration
from estudia.study.srs import sync_review_state

TZ = ZoneInfo("America/Argentina/Buenos_Aires")
DAY = 86400


def local(day: int, hour: int = 0, minute: int = 0) -> int:
    return int(datetime(2026, 10, day, hour, minute, tzinfo=TZ).timestamp())


NOW = local(17, 15, 30)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    c.execute("INSERT INTO course(id, site_id, fullname, shortname) VALUES (10, 's', 'Análisis Matemático', 'AM')")
    c.execute("INSERT INTO section(id, course_id, num, name, visible) VALUES (1001, 10, 1, 's', 1)")
    c.commit()
    return c


def add_class(conn, mid, *, questions=5, updated_at=NOW - 3600, status="done", title=None,
              verified=1, retired=0, seconds=None):
    """Clase `mid` con su tema `mid` y preguntas `mid*100 + i` (segundo 60*i del video)."""
    conn.execute(
        "INSERT INTO material(id, section_id, cmid, kind, title, ref, status, updated_at) "
        "VALUES (?, 1001, ?, 'youtube', ?, 'r', ?, ?)",
        (mid, mid, title or f"Clase {mid}", status, updated_at))
    conn.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (?, 10, ?, ?)", (mid, f"t{mid}", f"Tema {mid}"))
    conn.execute("INSERT INTO material_topic(material_id, topic_id) VALUES (?, ?)", (mid, mid))
    secs = seconds or [60 * i for i in range(questions)]
    for i, s in enumerate(secs):
        conn.execute(
            "INSERT INTO question(id, material_id, topic_id, stem, options_json, correct_index, explanation,"
            " source_seconds, verified, retired) VALUES (?, ?, ?, ?, ?, 0, 'porque', ?, ?, ?)",
            (mid * 100 + i, mid, mid, f"P{mid}-{i}", json.dumps(["a", "b", "c", "d"]), s, verified, retired))
    conn.commit()


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# --- Oferta -------------------------------------------------------------------------------------

def test_offer_newest_unpracticed_class(conn):
    add_class(conn, 1, updated_at=NOW - 2 * DAY)
    add_class(conn, 2, updated_at=NOW - DAY, title="Límites")
    add_class(conn, 3, updated_at=NOW - 10 * DAY)              # vieja: fuera de la ventana
    add_class(conn, 4, updated_at=NOW - 3600, status="pending")  # todavía no está lista
    add_class(conn, 5, updated_at=NOW - 3600, questions=2)       # no llega a 3
    add_class(conn, 6, updated_at=NOW - 3600, verified=0)        # sin verificar
    add_class(conn, 7, updated_at=NOW - 3600, retired=1)         # retiradas
    assert pretest_offer(conn, NOW) == PretestOffer(2, "Límites", "Análisis Matemático")
    assert home_view(conn, NOW).pretest == PretestOffer(2, "Límites", "Análisis Matemático")


def test_offer_gone_after_close(conn):
    add_class(conn, 1, updated_at=NOW - 2 * DAY)
    add_class(conn, 2, updated_at=NOW - DAY)
    close_pretest(conn, 2, NOW)
    assert pretest_offer(conn, NOW).material_id == 1
    close_pretest(conn, 1, NOW)
    close_pretest(conn, 1, NOW)   # repetirlo no rompe nada
    assert pretest_offer(conn, NOW) is None
    assert home_view(conn, NOW).pretest is None
    assert json.loads(conn.execute("SELECT value FROM kv WHERE key = 'pretest_closed'").fetchone()[0]) == [2, 1]


def test_close_prunes_classes_outside_the_window(conn):
    """La lista de cerradas no crece para siempre: solo guarda clases de la ventana de 7 días."""
    add_class(conn, 1, updated_at=NOW - 2 * DAY)
    add_class(conn, 2, updated_at=NOW - DAY)
    close_pretest(conn, 1, NOW)
    close_pretest(conn, 2, NOW)
    # Seis días después la clase 1 ya salió de la ventana; la 2 todavía no.
    add_class(conn, 3, updated_at=NOW + 6 * DAY)
    close_pretest(conn, 3, NOW + 6 * DAY)
    assert json.loads(conn.execute("SELECT value FROM kv WHERE key = 'pretest_closed'").fetchone()[0]) == [2, 3]
    with pytest.raises(LookupError):
        close_pretest(conn, 99, NOW)


def test_no_offer_for_class_already_answered(conn):
    add_class(conn, 1, updated_at=NOW - 2 * DAY)
    add_class(conn, 2, updated_at=NOW - DAY)
    add_class(conn, 3, updated_at=NOW - 3600)
    record_answer(conn, 204, 1, NOW - 60, mode="typed_then_mcq")   # practicada
    record_pretest_answer(conn, 300, 0, NOW - 30)                    # pretest empezado
    assert pretest_offer(conn, NOW).material_id == 1


def test_no_offer_for_hidden_section(conn):
    add_class(conn, 1, updated_at=NOW - 2 * DAY)
    add_class(conn, 2, updated_at=NOW - DAY)
    conn.execute("INSERT INTO section(id, course_id, num, name, visible) VALUES (1002, 10, 2, 's', 0)")
    conn.execute("UPDATE material SET section_id = 1002 WHERE id = 2")
    conn.commit()
    assert pretest_offer(conn, NOW).material_id == 1
    conn.execute("UPDATE material SET section_id = NULL WHERE id = 1")   # sin sección: tampoco
    conn.commit()
    assert pretest_offer(conn, NOW) is None


# --- Armado -------------------------------------------------------------------------------------

def test_pretest_picks_three_of_that_class(conn):
    add_class(conn, 1, seconds=[400, 10, None, 900, 250, 600, 50])
    add_class(conn, 2)
    items = build_pretest(conn, 1)
    assert len(items) == PRETEST_SIZE == 3
    # Ordenadas por segundo (sin segundo, al principio): 102, 101, 106, 104, 100, 105, 103.
    assert [i.question_id for i in items] == [102, 104, 103]
    assert all(i.format == MCQ and i.topic == "Tema 1" and len(i.options) == 4 for i in items)


def test_pretest_needs_three_available(conn):
    add_class(conn, 1, questions=4)
    conn.execute("UPDATE question SET retired = 1 WHERE id IN (100, 101)")
    conn.commit()
    with pytest.raises(LookupError):
        build_pretest(conn, 1)
    with pytest.raises(LookupError):
        build_pretest(conn, 99)


def test_pretest_answer_feedback(conn):
    add_class(conn, 1)
    fb = record_pretest_answer(conn, 100, 1, NOW, option_order=[1, 0, 2, 3])   # tocó la posición 1 → "a"
    assert (fb.correct, fb.chosen_index, fb.correct_index) == (True, 0, 0)
    wrong = record_pretest_answer(conn, 101, 2, NOW)
    assert not wrong.correct
    assert wrong.feedback_style != "alarm"   # no se preguntó confianza: no es un error "seguro"
    assert (wrong.requeue, wrong.next_label, wrong.relearn_progress) == (False, None, None)
    rows = conn.execute("SELECT mode, confidence FROM answer ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("pretest", None), ("pretest", None)]


# --- Sin rastro (Review Focus 2) ----------------------------------------------------------------

def test_pretest_leaves_no_trace_outside_answer(conn):
    add_class(conn, 1)
    create_manual_exam(conn, 10, "Parcial", local(30, 9), [1], None)
    conn.execute("INSERT INTO plan_session(course_id, topic_id, starts_at, minutes) VALUES (10, 1, ?, 50)",
                 (local(17, 9),))
    conn.commit()
    sync_review_state(conn)
    conn.commit()
    before = {
        "review_state": count(conn, "review_state"),
        "p_now": topic_probability(conn, 1, NOW + 3600),
        "p_exam": topic_probability(conn, 1, local(30, 9)),
    }
    items = build_pretest(conn, 1)
    record_pretest_answer(conn, items[0].question_id, 0, NOW)       # bien
    record_pretest_answer(conn, items[1].question_id, 1, NOW + 10)  # mal
    record_pretest_answer(conn, items[2].question_id, 2, NOW + 20)  # mal
    assert count(conn, "answer") == 3

    assert count(conn, "review_state") == before["review_state"] == 0
    assert topic_probability(conn, 1, NOW + 3600) == before["p_now"] == 0
    assert topic_probability(conn, 1, local(30, 9)) == before["p_exam"] == 0
    topic = next(t for t in home_view(conn, NOW + 60).topics if t.id == 1)
    assert topic.practiced is False
    assert topic.seen == 0
    assert mark_done_from_answers(conn, NOW + 60) == 0


def test_pretest_leaves_no_trace_in_calibration(conn):
    add_class(conn, 1)
    for i, chosen in enumerate((0, 1, 2)):
        record_pretest_answer(conn, 100 + i, chosen, NOW + i)
    assert calibration(conn).total == 0


def test_pretest_does_not_mark_a_session_done(conn):
    """Aun con respuestas de sobra (5, todas bien) en la sesión de hoy, el pretest no la cumple."""
    add_class(conn, 1)
    conn.execute("INSERT INTO plan_session(course_id, topic_id, starts_at, minutes) VALUES (10, 1, ?, 50)",
                 (local(17, 9),))
    conn.commit()
    for i in range(5):
        record_pretest_answer(conn, 100 + i, 0, NOW + i)
    assert mark_done_from_answers(conn, NOW + 60) == 0
    # La misma cantidad en práctica sí la cumple: el corte es el modo, no el conteo.
    for i in range(5):
        record_answer(conn, 100 + i, 0, NOW + 100 + i)
    assert mark_done_from_answers(conn, NOW + 200) == 1


def test_pretest_is_not_practiced_in_home(conn):
    add_class(conn, 1)
    record_pretest_answer(conn, 100, 0, NOW)
    topic = home_view(conn, NOW + 60).topics[0]
    assert (topic.practiced, topic.seen, topic.total) == (False, 0, 5)
    record_answer(conn, 101, 0, NOW + 30)
    topic = home_view(conn, NOW + 60).topics[0]
    assert (topic.practiced, topic.seen) == (True, 1)


def test_pretest_ignored_when_exam_dates_rebuild_cards(conn):
    """srs: la rama que rehace un tema entero porque cambió su examen también ignora el pretest."""
    add_class(conn, 1)
    record_pretest_answer(conn, 100, 0, NOW)
    record_answer(conn, 101, 0, NOW + 10)
    row = dict(conn.execute("SELECT * FROM review_state WHERE card_key = 'q:101'").fetchone())
    create_manual_exam(conn, 10, "Parcial", local(30, 9), [1], None)
    sync_review_state(conn)
    conn.commit()
    keys = [r[0] for r in conn.execute("SELECT card_key FROM review_state")]
    assert keys == ["q:101"]
    assert dict(conn.execute("SELECT * FROM review_state WHERE card_key = 'q:101'").fetchone())["last_review"] \
        == row["last_review"]


def test_pretest_is_not_recent_practice_for_the_mock(conn):
    add_class(conn, 1)
    record_pretest_answer(conn, 100, 0, NOW)
    assert _recently_practiced(conn, NOW + 60) == set()


def test_pretest_question_comes_back_as_new_in_practice(conn):
    add_class(conn, 1, questions=3)
    for item in build_pretest(conn, 1):
        record_pretest_answer(conn, item.question_id, 0, NOW)   # todas bien
    quiz = build_quiz(conn, 10, topic_id=1, now=NOW + 60)
    assert sorted(i.question_id for i in quiz) == [100, 101, 102]
    assert all(i.format == MCQ and i.relearning is None for i in quiz)
    fb = record_answer(conn, 100, 0, NOW + 120)
    # Primera vez en práctica: la tarjeta arranca de cero, con una sola revisión.
    assert fb.next_label is not None
    assert count(conn, "review_state") == 1


@pytest.mark.parametrize("change", [
    "UPDATE material SET status = 'pending' WHERE id = 1",
    "UPDATE section SET visible = 0 WHERE id = 1001",
])
def test_pretest_needs_a_done_class_in_a_visible_section(conn, change):
    add_class(conn, 1)
    assert len(build_pretest(conn, 1)) == PRETEST_SIZE
    conn.execute(change)
    conn.commit()
    with pytest.raises(LookupError):
        build_pretest(conn, 1)
