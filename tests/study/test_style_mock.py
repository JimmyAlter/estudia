"""Simulacro con el formato de la cátedra: la cantidad, el tiempo y la escala del estilo guardado,
y la nota ponderada por los puntos de cada ejercicio."""
import json

import pytest

from estudia.db import connect
from estudia.study.exam_style import ExamStyle, StyleGroup, save_style, style_gaps
from estudia.study.exams import create_manual_exam
from estudia.study.mock_exam import (
    STYLE_NOT_READY_TEXT,
    NotEnoughQuestions,
    finish_mock,
    mock_result,
    start_mock,
    style_not_ready_text,
)
from estudia.study.progress import calibration, predictions

NOW = 1_790_000_000


def seed(c):
    c.execute("INSERT INTO course(id, site_id, fullname, shortname) VALUES (10, 's', 'Análisis', 'AM')")
    c.execute("INSERT INTO section(id, course_id, num, name, visible) VALUES (1, 10, 1, 'S', 1)")
    c.execute("INSERT INTO material(id, section_id, cmid, kind, title, ref, status)"
              " VALUES (1, 1, 101, 'youtube', 'V', 'yt1', 'done')")
    for tid, name in ((1, "Función lineal"), (2, "Límites")):
        c.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (?, 10, ?, ?)", (tid, f"t{tid}", name))
        c.execute("INSERT INTO material_topic(material_id, topic_id) VALUES (1, ?)", (tid,))
    c.commit()


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    seed(c)
    yield c
    c.close()


def make_exam(conn, groups, minutes=None, max_grade=None, exam_max=None, time_limit_s=None) -> int:
    exam_id = create_manual_exam(conn, 10, "Primer parcial", NOW + 86400, [1, 2], exam_max, now=NOW)
    if time_limit_s is not None:
        conn.execute("UPDATE exam SET time_limit_s = ? WHERE id = ?", (time_limit_s, exam_id))
        conn.commit()
    save_style(conn, exam_id, ExamStyle(
        groups=[StyleGroup(kind=k, count=n, points_each=p) for k, n, p in groups],
        minutes=minutes, max_grade=max_grade, topic_ids=[1, 2]))
    return exam_id


def add_campus(conn, qid, topic_id=1, check_expr=None, typed_safe=None):
    conn.execute(
        "INSERT INTO question(id, material_id, topic_id, stem, options_json, correct_index, explanation,"
        " common_error, check_expr, typed_safe, verified, retired, dedupe_key)"
        " VALUES (?, 1, ?, ?, ?, 0, 'e', 'c', ?, ?, 1, 0, ?)",
        (qid, topic_id, f"campus {qid}", json.dumps(["9", "6", "3", "27"]), check_expr, typed_safe, f"c{qid}"))
    conn.commit()


def add_style(conn, qid, exam_id, kind, check_expr=None, typed_safe=None, topic_id=1):
    conn.execute(
        "INSERT INTO question(id, topic_id, stem, options_json, correct_index, explanation, common_error,"
        " check_expr, typed_safe, verified, retired, origin, style_exam_id, style_kind, dedupe_key)"
        " VALUES (?, ?, ?, ?, 0, 'e', 'c', ?, ?, 1, 0, 'parcial_propio', ?, ?, ?)",
        (qid, topic_id, f"estilo {qid}", json.dumps(["9", "6", "3", "27"]), check_expr, typed_safe,
         exam_id, kind, f"s{qid}"))
    conn.commit()


def kinds_of(conn, items):
    rows = dict(conn.execute("SELECT id, COALESCE(style_kind, 'opcion_multiple') FROM question").fetchall())
    return [rows[i.question_id] for i in items]


def test_style_mock_same_count_time_and_scale(conn):
    exam_id = make_exam(conn, [("opcion_multiple", 7, 1), ("desarrollo", 2, 2)], minutes=120, max_grade=10)
    for qid in range(1, 6):
        add_campus(conn, qid, topic_id=1 + qid % 2)
    add_style(conn, 50, exam_id, "opcion_multiple")
    add_style(conn, 51, exam_id, "opcion_multiple")
    add_style(conn, 60, exam_id, "desarrollo")
    add_style(conn, 61, exam_id, "desarrollo")
    start = start_mock(conn, exam_id, NOW, style=True)
    assert len(start.items) == 9 and start.seconds == 7200 and start.max_grade == 10
    assert start.style is True
    # En orden de grupo: primero las 7 de opción múltiple, después las 2 de desarrollo.
    assert kinds_of(conn, start.items) == ["opcion_multiple"] * 7 + ["desarrollo"] * 2
    assert {i.question_id for i in start.items[7:]} == {60, 61}
    assert [i.points for i in start.items] == [1.0] * 7 + [2.0] * 2
    row = conn.execute("SELECT style, points_json, question_ids_json FROM mock_attempt").fetchone()
    assert row["style"] == 1
    assert json.loads(row["points_json"]) == [1.0] * 7 + [2.0] * 2
    assert json.loads(row["question_ids_json"]) == [i.question_id for i in start.items]


def test_style_mock_time_and_scale_fallbacks(conn):
    exam_id = make_exam(conn, [("opcion_multiple", 3, 1)], exam_max=100, time_limit_s=3000)
    for qid in range(1, 4):
        add_campus(conn, qid)
    start = start_mock(conn, exam_id, NOW, style=True)
    assert (start.seconds, start.max_grade) == (3000, 100)
    conn.execute("UPDATE exam SET time_limit_s = NULL, max_grade = NULL WHERE id = ?", (exam_id,))
    conn.commit()
    start = start_mock(conn, exam_id, NOW, style=True)
    assert (start.seconds, start.max_grade) == (270, 10)


def test_style_mock_development_slots_typed_when_safe(conn):
    exam_id = make_exam(conn, [("opcion_multiple", 1, 1), ("desarrollo", 2, 2)])
    add_campus(conn, 1, check_expr="9", typed_safe=1)        # escribible, pero es de opción múltiple
    add_style(conn, 60, exam_id, "desarrollo", check_expr="9", typed_safe=1)
    add_style(conn, 61, exam_id, "desarrollo", check_expr="9", typed_safe=0)  # un distractor entraría
    start = start_mock(conn, exam_id, NOW, style=True)
    formats = {i.question_id: i.format for i in start.items}
    assert formats == {1: "mcq", 60: "typed", 61: "mcq"}
    typed = json.loads(conn.execute("SELECT typed_json FROM mock_attempt").fetchone()[0])
    assert typed == [60]


def weighted_setup(conn):
    exam_id = make_exam(conn, [("opcion_multiple", 2, 1), ("desarrollo", 1, 3)], max_grade=10)
    add_campus(conn, 1)
    add_campus(conn, 2)
    add_style(conn, 60, exam_id, "desarrollo")
    return exam_id


def test_style_mock_weighted_grade(conn):
    exam_id = weighted_setup(conn)
    start = start_mock(conn, exam_id, NOW, style=True, predicted_grade=7)
    # Solo el de desarrollo (3 de 5 puntos) bien: 10 × 3/5.
    result = finish_mock(conn, start.attempt_id, {1: 1, 2: 1, 60: 0}, NOW + 60)
    assert result.grade == 6.0 and result.max_grade == 10.0
    assert (result.correct, result.total) == (1, 3)
    # Las respuestas van como simulacro, sin confianza, y arman el repaso (el error vuelve mañana).
    rows = conn.execute("SELECT question_id, mode, confidence FROM answer ORDER BY question_id").fetchall()
    assert [tuple(r) for r in rows] == [(1, "mock", None), (2, "mock", None), (60, "mock", None)]
    assert conn.execute("SELECT 1 FROM review_state WHERE card_key = 'q:60'").fetchone() is not None
    # La respuesta a la de estilo no entra en la calibración.
    assert calibration(conn).total == 0


@pytest.mark.parametrize("answers, grade", [({1: 0, 2: 1, 3: 1}, 3.33), ({1: 0, 2: 0, 3: 1}, 6.67),
                                            ({1: 0, 2: 0, 3: 0}, 10.0), ({}, 0.0)])
def test_style_mock_grade_rounding(conn, answers, grade):
    exam_id = make_exam(conn, [("opcion_multiple", 3, 1.5)], max_grade=10)
    for qid in (1, 2, 3):
        add_campus(conn, qid)
    start = start_mock(conn, exam_id, NOW, style=True)
    assert finish_mock(conn, start.attempt_id, answers, NOW + 60).grade == grade


def test_style_mock_result_reread_keeps_weighted_grade(conn):
    exam_id = weighted_setup(conn)
    start = start_mock(conn, exam_id, NOW, style=True)
    finish_mock(conn, start.attempt_id, {1: 1, 2: 1, 60: 0}, NOW + 60)
    again = mock_result(conn, start.attempt_id)
    assert again.grade == 6.0 and again.max_grade == 10.0
    assert (again.correct, again.total) == (1, 3)
    assert [r.question_id for r in again.review] == [i.question_id for i in start.items]


def test_style_mock_retired_question_left_out_of_both_sums(conn):
    exam_id = weighted_setup(conn)
    start = start_mock(conn, exam_id, NOW, style=True)
    conn.execute("UPDATE question SET retired = 1 WHERE id = 60")
    conn.commit()
    # Queda 1 de 2 puntos: 10 × 1/2.
    assert finish_mock(conn, start.attempt_id, {1: 0, 2: 1}, NOW + 60).grade == 5.0


def test_style_questions_of_a_topic_left_out_do_not_count_nor_get_picked(conn):
    """El estudiante editó el estilo y sacó un tema: las de estilo de ese tema no cuentan para
    lo que falta ni entran al simulacro (si vuelve a poner el tema, vuelven)."""
    exam_id = make_exam(conn, [("desarrollo", 2, 2)])
    add_style(conn, 60, exam_id, "desarrollo", topic_id=1)
    add_style(conn, 61, exam_id, "desarrollo", topic_id=2)
    add_style(conn, 62, exam_id, "desarrollo", topic_id=2)
    assert style_gaps(conn, exam_id) == {"desarrollo": 0}
    save_style(conn, exam_id, ExamStyle(groups=[StyleGroup(kind="desarrollo", count=2, points_each=2)],
                                        topic_ids=[1]))
    assert style_gaps(conn, exam_id) == {"desarrollo": 1}
    with pytest.raises(NotEnoughQuestions):
        start_mock(conn, exam_id, NOW, style=True)
    add_style(conn, 63, exam_id, "desarrollo", topic_id=1)
    start = start_mock(conn, exam_id, NOW, style=True)
    assert {i.question_id for i in start.items} == {60, 63}


def test_style_mock_not_ready(conn):
    exam_id = make_exam(conn, [("opcion_multiple", 7, 1)])
    for qid in range(1, 6):
        add_campus(conn, qid)
    assert sum(style_gaps(conn, exam_id).values()) == 2
    with pytest.raises(NotEnoughQuestions) as err:
        start_mock(conn, exam_id, NOW, style=True)
    assert str(err.value) == STYLE_NOT_READY_TEXT.format(n=2)
    assert conn.execute("SELECT COUNT(*) FROM mock_attempt").fetchone()[0] == 0


def test_style_mock_needs_saved_style_and_not_short(conn):
    exam_id = create_manual_exam(conn, 10, "Sin estilo", NOW + 86400, [1], None, now=NOW)
    with pytest.raises(ValueError, match="Este examen no tiene un formato cargado"):
        start_mock(conn, exam_id, NOW, style=True)
    styled = make_exam(conn, [("opcion_multiple", 1, 1)])
    add_campus(conn, 1)
    with pytest.raises(ValueError) as err:
        start_mock(conn, styled, NOW, style=True, short=True)
    assert not isinstance(err.value, NotEnoughQuestions)


def test_regular_mock_unchanged_with_style_saved(tmp_path):
    def run(with_style: bool):
        c = connect(tmp_path / f"r{with_style}.db")
        seed(c)
        exam_id = create_manual_exam(c, 10, "Primer parcial", NOW + 86400, [1, 2], None, now=NOW)
        for qid in range(1, 13):
            add_campus(c, qid, topic_id=1 + qid % 2)
        if with_style:
            save_style(c, exam_id, ExamStyle(groups=[StyleGroup(kind="desarrollo", count=2, points_each=5)],
                                             minutes=30, max_grade=100, topic_ids=[1]))
            add_style(c, 60, exam_id, "desarrollo")
            add_style(c, 61, exam_id, "opcion_multiple")
        start = start_mock(c, exam_id, NOW)
        row = c.execute("SELECT style, points_json FROM mock_attempt").fetchone()
        c.close()
        return start, tuple(row)

    plain, plain_row = run(False)
    styled, styled_row = run(True)
    assert [i.question_id for i in styled.items] == [i.question_id for i in plain.items]
    assert (styled.seconds, styled.max_grade, styled.style) == (plain.seconds, plain.max_grade, False)
    assert all(i.points is None for i in styled.items)
    assert styled_row == plain_row == (None, None)


def test_predictions_use_the_style_mock_in_the_real_scale(conn):
    """El simulacro con estilo puede tener otra escala que la nota real: se lleva a la de la real."""
    exam_id = make_exam(conn, [("opcion_multiple", 2, 1)], max_grade=100, exam_max=10)
    add_campus(conn, 1)
    add_campus(conn, 2)
    start = start_mock(conn, exam_id, NOW, predicted_grade=80, style=True)
    finish_mock(conn, start.attempt_id, {1: 0, 2: 1}, NOW + 60)   # 50 sobre 100
    conn.execute("UPDATE exam SET opens_at = ? WHERE id = ?", (NOW - 10, exam_id))
    conn.execute("INSERT INTO exam_result(exam_id, grade, max_grade, at) VALUES (?, 7, 10, ?)", (exam_id, NOW))
    conn.commit()
    row, = predictions(conn)
    assert (row.predicted, row.mock_grade, row.real, row.max_grade) == (8.0, 5.0, 7.0, 10.0)


def test_style_not_ready_text_says_singular():
    assert style_not_ready_text(1) == "Todavía falta 1 pregunta con el formato de tu cátedra. Se prepara sola."
    assert style_not_ready_text(3) == "Todavía faltan 3 preguntas con el formato de tu cátedra. Se preparan solas."
