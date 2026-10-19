"""Estilo del examen: temas del curso, promedio entre parciales y lo que guarda el estudiante."""
import pytest
from pydantic import ValidationError

from estudia.ai.base import ExtractedExercise, ExtractedStyle
from estudia.db import connect
from estudia.study.exam_style import (
    ExamStyle,
    StyleGroup,
    average_style,
    clear_style,
    map_topics,
    proposed_style,
    round_half_up,
    save_style,
    saved_style,
    to_exam_style,
)
from estudia.study.exams import create_manual_exam, list_exams

NOW = 1_790_000_000


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "estudia.db")
    c.execute("INSERT INTO course(id, site_id, fullname, shortname) VALUES (10, 's', 'Análisis', 'AM')")
    c.execute("INSERT INTO course(id, site_id, fullname, shortname) VALUES (20, 's', 'Física', 'FI')")
    for tid, course, name in [(1, 10, "Función lineal"), (2, 10, "Límites"),
                              (3, 10, "Función cuadrática"), (9, 20, "Cinemática")]:
        c.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (?, ?, ?, ?)",
                  (tid, course, name.lower(), name))
    c.commit()
    yield c
    c.close()


def style(groups, minutes=None, max_grade=None, topic_ids=(), unmatched=(), formula_sheet=False):
    return ExamStyle(groups=[StyleGroup(kind=k, count=n, points_each=p) for k, n, p in groups],
                     minutes=minutes, max_grade=max_grade, topic_ids=list(topic_ids),
                     topics_unmatched=list(unmatched), formula_sheet=formula_sheet)


def extracted(kinds_points, minutes=None, max_grade=None, topics=(), formula_sheet=False):
    return ExtractedStyle(exercises=[ExtractedExercise(kind=k, points=p) for k, p in kinds_points],
                          minutes=minutes, max_grade=max_grade, topics=list(topics),
                          formula_sheet=formula_sheet)


def test_map_topics_to_course(conn):
    ids, unmatched = map_topics(conn, 10, ["Funciones lineales", "Termodinámica", "límites",
                                           "Cinemática", "Funciones lineales"])
    assert ids == [1, 2]
    # Un tema de otra materia no entra en esta.
    assert unmatched == ["Termodinámica", "Cinemática"]


def test_map_topics_prefers_the_closest_topic(conn):
    ids, unmatched = map_topics(conn, 10, ["Funciones cuadráticas"])
    assert ids == [3] and unmatched == []


def test_to_exam_style_groups_by_kind_and_rounds_points(conn):
    result = to_exam_style(conn, 10, extracted(
        [("desarrollo", 2.0), ("opcion_multiple", 1.0), ("opcion_multiple", 1.5),
         ("desarrollo", 2.5)],
        minutes=90, max_grade=10, topics=["Límites", "Termodinámica"], formula_sheet=True))
    assert [(g.kind, g.count, g.points_each) for g in result.groups] == [
        ("opcion_multiple", 2, 1.5), ("desarrollo", 2, 2.5)]
    assert (result.minutes, result.max_grade, result.formula_sheet) == (90, 10, True)
    assert result.topic_ids == [2] and result.topics_unmatched == ["Termodinámica"]


def test_to_exam_style_without_points_and_out_of_range_values(conn):
    by_grade = to_exam_style(conn, 10, extracted([("verdadero_falso", None)] * 4, max_grade=10))
    assert by_grade.groups[0].points_each == 2.5
    plain = to_exam_style(conn, 10, extracted([("verdadero_falso", None)] * 40, minutes=9000))
    assert plain.groups[0].count == 30 and plain.groups[0].points_each == 1
    assert plain.minutes is None and plain.max_grade is None
    with pytest.raises(ValueError):
        to_exam_style(conn, 10, extracted([]))


def test_average_of_two_past_exams(conn):
    exam_id = create_manual_exam(conn, 10, "Primer parcial", NOW + 86400, [], None, now=NOW)
    first = to_exam_style(conn, 10, extracted(
        [("opcion_multiple", 1.0)] * 8 + [("desarrollo", 1.0)] * 2, minutes=120, max_grade=10,
        topics=["Funciones lineales"]))
    second = to_exam_style(conn, 10, extracted(
        [("opcion_multiple", None)] * 6 + [("desarrollo", None)] * 2, max_grade=10, topics=["Límites"]))
    for i, s in enumerate([first, second], start=1):
        conn.execute("INSERT INTO past_exam(id, exam_id, name, kind, status, style_json, sha256)"
                     " VALUES (?, ?, ?, 'pdf', 'done', ?, ?)",
                     (i, exam_id, f"Parcial {i}", s.model_dump_json(), str(i)))
    # Uno pendiente o fallido no cuenta.
    conn.execute("INSERT INTO past_exam(id, exam_id, name, kind, status, sha256)"
                 " VALUES (3, ?, 'Parcial 3', 'pdf', 'failed', '3')", (exam_id,))
    conn.commit()
    proposed, sources = proposed_style(conn, exam_id)
    assert [(g.kind, g.count) for g in proposed.groups] == [("opcion_multiple", 7), ("desarrollo", 2)]
    assert proposed.minutes == 120 and proposed.max_grade == 10
    assert proposed.topic_ids == [1, 2]
    assert [(s.past_exam_id, s.name, s.questions) for s in sources] == [
        (1, "Parcial 1", 10), (2, "Parcial 2", 8)]


def test_average_rounds_half_up_and_keeps_total():
    assert round_half_up(10.5) == 11 and round_half_up(2.5) == 3
    a = style([("opcion_multiple", 5, 1.0), ("verdadero_falso", 5, 0.5), ("desarrollo", 1, 2.0)],
              minutes=90, max_grade=10, topic_ids=[2, 1], unmatched=["Series"], formula_sheet=True)
    b = style([("opcion_multiple", 4, 1.5), ("verdadero_falso", 5, 0.5), ("desarrollo", 1, 3.0)],
              minutes=105, max_grade=9, topic_ids=[1, 3], unmatched=["series", "Vectores"])
    avg = average_style([a, b])
    # 11 y 10 ejercicios: el promedio 10,5 da 11 (y no 10), y los tipos suman justo eso.
    assert [(g.kind, g.count) for g in avg.groups] == [
        ("opcion_multiple", 5), ("verdadero_falso", 5), ("desarrollo", 1)]
    assert sum(g.count for g in avg.groups) == 11
    # 1,25 da 1,5 y 2,5 queda 2,5.
    assert [g.points_each for g in avg.groups] == [1.5, 0.5, 2.5]
    assert avg.minutes == 98            # 97,5
    assert avg.max_grade == 9.5
    assert avg.formula_sheet is True    # la mitad la tiene
    assert avg.topic_ids == [1, 2, 3]   # el 1 está en los dos; después por primera aparición
    assert avg.topics_unmatched == ["Series", "Vectores"]
    assert average_style([a]) is a


def test_style_validation():
    with pytest.raises(ValidationError):
        style([("opcion_multiple", 31, 1.0)])
    with pytest.raises(ValidationError):
        style([("opcion_multiple", 20, 1.0), ("desarrollo", 11, 1.0)])
    with pytest.raises(ValidationError):
        style([("opcion_multiple", 2, 1.0), ("opcion_multiple", 3, 1.0)])
    with pytest.raises(ValidationError):
        style([("desarrollo", 2, 0)])
    with pytest.raises(ValidationError):
        style([("desarrollo", 2, 1.0)], minutes=601)
    # Sin ejercicios no es un estilo, pero lo rechaza `save_style` con un texto (400 en la API).
    assert style([]).groups == []


def test_save_and_clear_style(conn):
    exam_id = create_manual_exam(conn, 10, "Primer parcial", NOW + 86400, [], None, now=NOW)
    chosen = style([("desarrollo", 4, 2.5)], minutes=120, max_grade=10, topic_ids=[1, 2, 1])
    save_style(conn, exam_id, chosen)
    assert saved_style(conn, exam_id).topic_ids == [1, 2]
    assert list_exams(conn, NOW)[0].has_style is True
    with pytest.raises(ValueError):
        save_style(conn, exam_id, style([("desarrollo", 4, 2.5)], topic_ids=[9]))
    conn.execute("UPDATE exam SET style_json = '{roto' WHERE id = ?", (exam_id,))
    conn.commit()
    assert saved_style(conn, exam_id) is None
    clear_style(conn, exam_id)
    assert list_exams(conn, NOW)[0].has_style is False
    enrollment = create_manual_exam(conn, 10, "Inscripción", NOW + 86400, [], None,
                                    kind="inscripcion", closes_at=NOW + 2 * 86400, now=NOW)
    with pytest.raises(ValueError):
        save_style(conn, enrollment, chosen)
    with pytest.raises(LookupError):
        save_style(conn, 999, chosen)
    with pytest.raises(ValueError, match="al menos un ejercicio"):
        save_style(conn, exam_id, style([]))
    conn.execute("UPDATE exam SET style_json = ? WHERE id = ?", (style([]).model_dump_json(), exam_id))
    conn.commit()
    assert saved_style(conn, exam_id) is None


def test_free_text_topics_are_capped():
    with pytest.raises(ValidationError):
        style([("desarrollo", 2, 1.0)], unmatched=[f"Tema {i}" for i in range(11)])
    with pytest.raises(ValidationError):
        style([("desarrollo", 2, 1.0)], unmatched=["x" * 81])
    a = style([("desarrollo", 2, 1.0)], unmatched=[f"A{i}" for i in range(10)])
    b = style([("desarrollo", 2, 1.0)], unmatched=[f"B{i}" for i in range(10)])
    assert average_style([a, b]).topics_unmatched == [f"A{i}" for i in range(10)]
