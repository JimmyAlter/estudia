"""Preguntas con el estilo del examen: se generan solo con el material de la clase y pasan por
exactamente la misma verificación que todas (chequeos locales y pasada a ciegas con otro modelo)."""
import json

import pytest

from estudia.ai.base import AIAuthError, AIQuotaError, StyleQuestion
from estudia.ai.gate import AIBlocked, GatedProvider
from estudia.ai.quota import used_today
from estudia.db import connect
from estudia.generation.renormalize import normalize_existing
from estudia.generation.style_questions import (
    STYLE_BATCH,
    STYLE_BATCHES_PER_DAY,
    STYLE_SOURCE_MAX_CHARS,
    fill_style_gaps,
    style_dedupe_key,
    style_material_text,
)
from estudia.study.anki import build_apkg, weekly_question_ids
from estudia.study.exam_style import ExamStyle, StyleGroup, clear_style, save_style, style_gaps
from estudia.study.exams import create_manual_exam, delete_manual_exam
from estudia.study.mock_exam import start_mock
from estudia.study.practice import build_quiz
from fakes import FakeProvider

NOW = 1_790_000_000
DAY = 86400


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "estudia.db")
    c.execute("INSERT INTO course(id, site_id, fullname, shortname) VALUES (10, 's', 'Análisis', 'AM')")
    c.execute("INSERT INTO section(id, course_id, num, name, visible) VALUES (1, 10, 1, 'S', 1)")
    for tid, slug, name in [(1, "funcion-lineal", "Función lineal"), (2, "limites", "Límites")]:
        c.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (?, 10, ?, ?)", (tid, slug, name))
    c.execute("INSERT INTO material(id, section_id, cmid, kind, title, ref, status, summary_md)"
              " VALUES (1, 1, 101, 'pdf', 'Apunte 1', 'a.pdf', 'done', 'La pendiente de una recta.')")
    c.execute("INSERT INTO material_topic(material_id, topic_id) VALUES (1, 1)")
    c.commit()
    yield c
    c.close()


def make_exam(conn, groups, topic_ids=(1,), at=NOW + 7 * DAY, name="Primer parcial") -> int:
    exam_id = create_manual_exam(conn, 10, name, at, [], None, now=NOW)
    save_style(conn, exam_id, ExamStyle(
        groups=[StyleGroup(kind=k, count=n, points_each=1) for k, n in groups],
        topic_ids=list(topic_ids)))
    return exam_id


def sq(stem, kind="opcion_multiple", correct=0, options=None, check_expr=None, topic="Función lineal"):
    return StyleQuestion(stem=stem, options=options or [f"{stem} a", f"{stem} b", f"{stem} c", f"{stem} d"],
                         correct_index=correct, explanation="Porque sí.", common_error="Confundir.",
                         topic=topic, kind=kind, check_expr=check_expr)


def add_campus_question(conn, qid, topic_id=1, retired=0):
    conn.execute("INSERT INTO question(id, material_id, topic_id, stem, options_json, correct_index,"
                 " explanation, common_error, verified, retired, dedupe_key)"
                 " VALUES (?, 1, ?, ?, ?, 0, 'e', 'c', 1, ?, ?)",
                 (qid, topic_id, f"Campus {qid}", json.dumps(["1", "2", "3", "4"]), retired, f"k{qid}"))
    conn.commit()


def style_rows(conn):
    return conn.execute("SELECT * FROM question WHERE origin = 'parcial_propio' ORDER BY id").fetchall()


def right(stem, options):
    """La pasada a ciegas acierta (la correcta es siempre la 0), salvo los enunciados con "mal"."""
    return 1 if "mal" in stem else 0


def test_gap_counts_per_kind(conn):
    exam_id = make_exam(conn, [("opcion_multiple", 7), ("desarrollo", 2)], topic_ids=[1, 2])
    for qid in range(1, 6):
        add_campus_question(conn, qid, topic_id=1 if qid % 2 else 2)
    add_campus_question(conn, 6, retired=1)  # retirada: no cuenta
    other = make_exam(conn, [("opcion_multiple", 1)], name="Otro")
    # Una de estilo de otro examen no cuenta para este.
    conn.execute("INSERT INTO question(topic_id, stem, options_json, correct_index, verified, retired,"
                 " origin, style_exam_id, style_kind, dedupe_key)"
                 " VALUES (1, 'Ajena', '[]', 0, 1, 0, 'parcial_propio', ?, 'opcion_multiple', 'x')", (other,))
    conn.commit()
    assert style_gaps(conn, exam_id) == {"opcion_multiple": 2, "desarrollo": 2}
    # Sin estilo guardado no falta nada.
    plain = create_manual_exam(conn, 10, "Sin estilo", NOW + DAY, [], None, now=NOW)
    assert style_gaps(conn, plain) == {}


def test_gap_topics_default_to_exam_topics(conn):
    exam_id = make_exam(conn, [("opcion_multiple", 3)], topic_ids=[])
    add_campus_question(conn, 1, topic_id=1)  # tema del examen (por su clase)
    add_campus_question(conn, 2, topic_id=2)  # sin clase visible: no es del examen
    assert style_gaps(conn, exam_id) == {"opcion_multiple": 2}


def test_batch_of_10_costs_two_requests(conn):
    exam_id = make_exam(conn, [("opcion_multiple", 6), ("verdadero_falso", 4)])
    batch = [sq(f"om {i}") for i in range(6)] + [sq(f"vf {i}", "verdadero_falso") for i in range(4)]
    prov = FakeProvider({}, right, style_batches=[batch])
    assert STYLE_BATCH == 10
    assert fill_style_gaps(conn, prov, NOW) == 10
    assert len(prov.style_requests) == 1 and prov.style_requests[0].kinds == {
        "opcion_multiple": 6, "verdadero_falso": 4}
    assert len(prov.blind_batches) == 1 and len(prov.blind_batches[0][0]) == 10
    # Una tanda = un pedido de generación + uno a ciegas.
    assert (used_today(conn, "analysis", NOW), used_today(conn, "blind", NOW),
            used_today(conn, "pdf", NOW)) == (1, 1, 0)
    request = prov.style_requests[0]
    assert request.course == "Análisis" and request.topics == ["Función lineal"]
    assert request.material_text == "## Apunte 1\nLa pendiente de una recta."
    assert style_gaps(conn, exam_id) == {"opcion_multiple": 0, "verdadero_falso": 0}


def test_batch_takes_up_to_10_in_kind_order(conn):
    exam_id = make_exam(conn, [("desarrollo", 5), ("opcion_multiple", 8)])
    prov = FakeProvider({}, right, style_batches=[[sq(f"om {i}") for i in range(8)]
                                                  + [sq(f"d {i}", "desarrollo") for i in range(2)],
                                                  [sq(f"d {i}", "desarrollo") for i in range(2, 5)]])
    assert fill_style_gaps(conn, prov, NOW) == 13
    assert [r.kinds for r in prov.style_requests] == [{"opcion_multiple": 8, "desarrollo": 2},
                                                      {"desarrollo": 3}]
    assert style_gaps(conn, exam_id) == {"opcion_multiple": 0, "desarrollo": 0}


def test_style_question_needs_blind_match_from_other_model(conn):
    """Review Focus 1: sin coincidencia a ciegas de otro modelo no se guarda nada."""
    exam_id = make_exam(conn, [("opcion_multiple", 3)])
    prov = FakeProvider({}, right, analysis_model="generador",
                        style_batches=[[sq("bien 1"), sq("mal 1"), sq("bien 2")]])
    assert fill_style_gaps(conn, prov, NOW) == 2
    # El modelo que generó no responde a ciegas.
    assert prov.blind_batches[0][1] == ("generador",)
    assert [r["stem"] for r in style_rows(conn)] == ["bien 1", "bien 2"]
    assert all(r["style_exam_id"] == exam_id for r in style_rows(conn))


def test_no_row_when_blind_quota_runs_out(conn):
    make_exam(conn, [("opcion_multiple", 3)])
    prov = FakeProvider({}, right, style_batches=[[sq("bien 1"), sq("bien 2")]],
                        blind_errors=[AIQuotaError("sin cuota")])
    with pytest.raises(AIQuotaError):
        fill_style_gaps(conn, prov, NOW)
    assert style_rows(conn) == []
    # Lo pedido igual cuenta para el presupuesto del día.
    assert used_today(conn, "analysis", NOW) == 1 and used_today(conn, "blind", NOW) == 1


def test_no_row_when_no_blind_model_remains(conn):
    """Si el único modelo a ciegas es el que generó, no hay a quién preguntar: nada se guarda."""
    make_exam(conn, [("opcion_multiple", 3)])
    prov = FakeProvider({}, right, analysis_model="generador", blind_models=("generador",),
                        style_batches=[[sq("bien 1"), sq("bien 2")]])
    with pytest.raises(AIQuotaError):
        fill_style_gaps(conn, prov, NOW)
    assert style_rows(conn) == [] and prov.blind_batches == []


def test_style_questions_fail_local_checks_not_saved(conn):
    make_exam(conn, [("opcion_multiple", 5), ("desarrollo", 3)])
    batch = [
        # SymPy no confirma la marcada.
        sq("expr mala", "desarrollo", options=["3", "2", "4", "5"], check_expr="2"),
        # Un distractor que la respuesta escrita aceptaría (1,41 por √2).
        sq("tolerancia", "desarrollo", options=["√2", "1.41", "2", "1"], check_expr="sqrt(2)"),
        # Dos opciones iguales.
        sq("repetida", options=["1", "1", "2", "3"]),
        # Un tipo que no se pidió.
        sq("de otro tipo", "verdadero_falso"),
        sq("sana", "desarrollo", options=["√2", "1.4", "2", "1"], check_expr="sqrt(2)"),
        sq("simple"),
    ]
    prov = FakeProvider({}, right, style_batches=[batch])
    assert fill_style_gaps(conn, prov, NOW) == 2
    # Solo las que pasaron lo local van a ciegas.
    assert [s for s, _ in prov.blind_batches[0][0]] == ["sana", "simple"]
    assert [(r["stem"], r["style_kind"], r["check_expr"]) for r in style_rows(conn)] == [
        ("sana", "desarrollo", "sqrt(2)"), ("simple", "opcion_multiple", None)]


def test_wrong_key_discarded_by_blind_pass(conn):
    """La clave marcada está mal (la correcta es la 1): la pasada a ciegas no coincide y se descarta."""
    make_exam(conn, [("opcion_multiple", 2)])
    prov = FakeProvider({}, lambda stem, options: 1,
                        style_batches=[[sq("clave mal", correct=0), sq("clave bien", correct=1)]])
    assert fill_style_gaps(conn, prov, NOW) == 1
    assert [r["stem"] for r in style_rows(conn)] == ["clave bien"]


def test_more_than_asked_is_capped_per_kind(conn):
    make_exam(conn, [("opcion_multiple", 2)])
    prov = FakeProvider({}, right, style_batches=[[sq(f"om {i}") for i in range(5)]])
    assert fill_style_gaps(conn, prov, NOW) == 2
    assert len(prov.blind_batches[0][0]) == 2


def test_style_questions_marked_parcial_propio(conn):
    exam_id = make_exam(conn, [("verdadero_falso", 1), ("desarrollo", 1)], topic_ids=[1, 2])
    batch = [sq("¿Cuál de estas afirmaciones es verdadera?", "verdadero_falso", topic="Límites"),
             sq("Calculá x**2 si x = 3", "desarrollo", options=["9", "6", "3", "27"], check_expr="9",
                topic="Termodinámica")]
    prov = FakeProvider({}, right, style_batches=[batch])
    assert fill_style_gaps(conn, prov, NOW) == 2
    rows = style_rows(conn)
    assert [(r["origin"], r["style_exam_id"], r["style_kind"], r["verified"], r["retired"]) for r in rows] == [
        ("parcial_propio", exam_id, "verdadero_falso", 1, 0), ("parcial_propio", exam_id, "desarrollo", 1, 0)]
    assert all(r["material_id"] is None and r["source_seconds"] is None and r["source_page"] is None
               for r in rows)
    # El tema sale de los del estilo: uno que no entra va al primero.
    assert [r["topic_id"] for r in rows] == [2, 1]
    # El texto queda en notación de clase y la clave es la de estilo.
    assert rows[1]["stem"] == "Calculá x² si x = 3"
    assert [r["dedupe_key"] for r in rows] == [style_dedupe_key(exam_id, r["stem"], revision=0) for r in rows]
    # No se crea ningún tema nuevo.
    assert conn.execute("SELECT COUNT(*) FROM topic").fetchone()[0] == 2


def test_style_questions_stay_out_of_regular_practice_and_anki(conn):
    exam_id = make_exam(conn, [("opcion_multiple", 6)], at=NOW + DAY)
    for qid in range(1, 6):
        add_campus_question(conn, qid)
    prov = FakeProvider({}, right, style_batches=[[sq("propia")]])
    assert fill_style_gaps(conn, prov, NOW) == 1
    own = style_rows(conn)[0]["id"]
    assert own not in {item.question_id for item in build_quiz(conn, 10, size=20, now=NOW)}
    assert own not in {item.question_id for item in build_quiz(conn, 10, topic_id=1, size=20, now=NOW)}
    assert own not in {item.question_id for item in start_mock(conn, exam_id, NOW).items}
    # Anki: ni pidiéndola por id (el mazo de la semana suma las de los temas del próximo examen).
    with pytest.raises(LookupError):
        build_apkg(conn, None, {own})
    build_apkg(conn, None, weekly_question_ids(conn, NOW))  # las del campus sí salen


def test_no_material_no_request(conn):
    make_exam(conn, [("opcion_multiple", 3)], topic_ids=[2])  # "Límites" no tiene resumen
    prov = FakeProvider({}, right, style_batches=[[sq("x")]])
    assert style_material_text(conn, [2]) == ""
    assert fill_style_gaps(conn, prov, NOW) == 0
    assert prov.style_requests == [] and prov.blind_batches == []
    assert used_today(conn, "analysis", NOW) == 0


def test_material_text_is_capped(conn):
    conn.execute("UPDATE material SET summary_md = ? WHERE id = 1", ("x" * (STYLE_SOURCE_MAX_CHARS + 50),))
    conn.execute("INSERT INTO material(id, section_id, cmid, kind, title, status, summary_md)"
                 " VALUES (2, 1, 102, 'pdf', 'Pendiente', 'pending', 'Sin terminar')")
    conn.execute("INSERT INTO material_topic(material_id, topic_id) VALUES (2, 1)")
    conn.commit()
    text = style_material_text(conn, [1])
    assert len(text) == STYLE_SOURCE_MAX_CHARS and text.startswith("## Apunte 1\n")
    assert "Sin terminar" not in text  # solo materiales `done`


def test_three_batches_per_exam_per_day(conn):
    make_exam(conn, [("opcion_multiple", 30)])
    # La pasada a ciegas nunca coincide: falta siempre lo mismo.
    prov = FakeProvider({}, lambda s, o: 3, style_batches=[[sq(f"t{i}")] for i in range(10)])
    assert STYLE_BATCHES_PER_DAY == 3
    assert fill_style_gaps(conn, prov, NOW) == 0
    assert len(prov.style_requests) == 3
    fill_style_gaps(conn, prov, NOW + 60)
    assert len(prov.style_requests) == 3
    tomorrow = NOW + DAY
    fill_style_gaps(conn, prov, tomorrow)
    assert len(prov.style_requests) == 6
    # El contador de ayer se borra.
    keys = [r[0] for r in conn.execute("SELECT key FROM kv WHERE key LIKE 'style_batches:%'")]
    assert len(keys) == 1


def test_format_error_discards_batch_and_counts_it(conn):
    make_exam(conn, [("opcion_multiple", 3)])
    prov = FakeProvider({}, right, style_batches=[])  # sin tanda: AIFormatError
    assert fill_style_gaps(conn, prov, NOW) == 0
    assert len(prov.style_requests) == STYLE_BATCHES_PER_DAY
    assert style_rows(conn) == []


def test_blind_reserve_and_budget_respected(conn):
    make_exam(conn, [("opcion_multiple", 3)])
    prov = FakeProvider({}, right, style_batches=[[sq("x")]])
    conn.execute("INSERT INTO kv(key, value) VALUES ('ai_budget:blind', '0')")
    conn.commit()
    assert fill_style_gaps(conn, prov, NOW) == 0
    assert prov.style_requests == []
    conn.execute("UPDATE kv SET value = '5' WHERE key = 'ai_budget:blind'")
    conn.execute("INSERT INTO kv(key, value) VALUES ('ai_budget:analysis', '0')")
    conn.commit()
    assert fill_style_gaps(conn, prov, NOW) == 0
    assert prov.style_requests == []


def test_nearest_exam_first_and_past_exams_skipped(conn):
    far = make_exam(conn, [("opcion_multiple", 1)], at=NOW + 20 * DAY, name="Lejano")
    near = make_exam(conn, [("opcion_multiple", 1)], at=NOW + 2 * DAY, name="Cercano")
    make_exam(conn, [("opcion_multiple", 1)], at=NOW - DAY, name="Pasado")
    prov = FakeProvider({}, right, style_batches=[[sq("primera")], [sq("segunda")]])
    assert fill_style_gaps(conn, prov, NOW) == 2
    assert len(prov.style_requests) == 2
    assert [(r["stem"], r["style_exam_id"]) for r in style_rows(conn)] == [("primera", near), ("segunda", far)]


def test_stoppers_propagate(conn):
    make_exam(conn, [("opcion_multiple", 1)])
    prov = FakeProvider({}, right, style_batches=[[sq("x")]], blind_errors=[AIAuthError("clave")])
    with pytest.raises(AIAuthError):
        fill_style_gaps(conn, prov, NOW)
    assert style_rows(conn) == []


def test_minor_never_generates(conn):
    make_exam(conn, [("opcion_multiple", 3)])
    fake = FakeProvider({}, right, style_batches=[[sq("x")]])
    gated = GatedProvider(fake, lambda: False)
    with pytest.raises(AIBlocked):
        fill_style_gaps(conn, gated, NOW)
    assert fake.style_requests == [] and fake.blind_batches == []
    assert fake.requests == {"analysis": 0, "blind": 0, "pdf": 0}
    assert used_today(conn, "analysis", NOW) == 0
    assert style_rows(conn) == []


def test_renormalize_keeps_style_key(conn):
    exam_id = make_exam(conn, [("desarrollo", 1)])
    conn.execute("INSERT INTO question(topic_id, stem, options_json, correct_index, explanation,"
                 " common_error, verified, retired, origin, style_exam_id, style_kind, dedupe_key)"
                 " VALUES (1, 'Calculá x**2', ?, 0, 'e', 'c', 1, 0, 'parcial_propio', ?, 'desarrollo', 'vieja')",
                 (json.dumps(["9", "6", "3", "27"]), exam_id))
    conn.commit()
    result = normalize_existing(conn)
    assert result.questions == 1
    row = style_rows(conn)[0]
    assert row["stem"] == "Calculá x²"
    assert row["dedupe_key"] == style_dedupe_key(exam_id, "Calculá x²", revision=0)
    assert normalize_existing(conn).questions == 0


def test_deleting_exam_or_style_retires_its_questions(conn, tmp_path):
    first = make_exam(conn, [("opcion_multiple", 1)])
    second = make_exam(conn, [("opcion_multiple", 1)], name="Segundo")
    prov = FakeProvider({}, right, style_batches=[[sq("del primero")], [sq("del segundo")]])
    assert fill_style_gaps(conn, prov, NOW) == 2
    delete_manual_exam(conn, first, base_dir=tmp_path)
    clear_style(conn, second)
    assert [(r["stem"], r["retired"]) for r in style_rows(conn)] == [("del primero", 1), ("del segundo", 1)]
    # Las del campus no se tocan.
    add_campus_question(conn, 50)
    clear_style(conn, second)
    assert conn.execute("SELECT retired FROM question WHERE id = 50").fetchone()[0] == 0


# --- Ronda de arreglos: las de estilo solo viven en su simulacro ---------------------------


def add_style_question(conn, stem, topic_id=1, exam_id=999):
    cur = conn.execute(
        "INSERT INTO question(topic_id, stem, options_json, correct_index, explanation, common_error,"
        " verified, retired, origin, style_exam_id, style_kind, dedupe_key)"
        " VALUES (?, ?, ?, 0, 'e', 'c', 1, 0, 'parcial_propio', ?, 'opcion_multiple', ?)",
        (topic_id, stem, json.dumps(["1", "2", "3", "4"]), exam_id, f"s-{stem}"))
    conn.commit()
    return cur.lastrowid


def answer_now(conn, qid, correct=1, confidence=None, mode=None):
    from fakes import commit_answers

    conn.execute("INSERT INTO answer(question_id, chosen_index, correct, at, confidence, mode)"
                 " VALUES (?, 0, ?, ?, ?, ?)", (qid, correct, NOW, confidence, mode))
    commit_answers(conn)


def test_mastery_and_home_counts_ignore_style_questions(conn):
    from estudia.study.home import home_view
    from estudia.study.mastery import topic_probability

    for qid in (1, 2, 3):
        add_campus_question(conn, qid)
        answer_now(conn, qid)
    for i in range(5):
        add_style_question(conn, f"estilo {i}")
    # 3 del campus vistas y recién acertadas: el tope es 3, no 8.
    assert topic_probability(conn, 1, NOW) == pytest.approx(1.0)
    row = next(t for t in home_view(conn, NOW).topics if t.id == 1)
    assert (row.seen, row.total) == (3, 3)


def test_calibration_ignores_style_answers(conn):
    from estudia.study.progress import calibration

    add_campus_question(conn, 1)
    before = calibration(conn)
    own = add_style_question(conn, "estilo")
    for _ in range(30):
        answer_now(conn, own, correct=1, confidence=2, mode="mcq")
    assert calibration(conn) == before


def test_practice_answer_rejects_style_question(conn):
    from estudia.study.practice import record_answer
    from estudia.study.pretest import record_pretest_answer

    own = add_style_question(conn, "estilo")
    with pytest.raises(LookupError):
        record_answer(conn, own, 0, NOW)
    with pytest.raises(LookupError):
        record_pretest_answer(conn, own, 0, NOW)
    assert conn.execute("SELECT COUNT(*) FROM answer").fetchone()[0] == 0


def test_style_only_topic_is_not_plannable(conn):
    from estudia.study.planner import replan, set_availability

    add_campus_question(conn, 1, topic_id=1)
    add_style_question(conn, "estilo", topic_id=2)
    create_manual_exam(conn, 10, "Parcial", NOW + 10 * DAY, [1, 2], None, now=NOW)
    set_availability(conn, {d: 60 for d in range(7)})
    replan(conn, NOW)
    rows = conn.execute("SELECT topic_id, topics_json FROM plan_session").fetchall()
    topics = {r["topic_id"] for r in rows} | {t for r in rows for t in json.loads(r["topics_json"] or "[]")}
    assert 1 in topics and 2 not in topics


def test_quiz_course_skips_courses_with_only_style_questions(conn):
    from estudia.server.app import _quiz_course

    conn.execute("INSERT INTO course(id, site_id, fullname, shortname) VALUES (5, 's', 'Álgebra', 'AL')")
    conn.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (7, 5, 'matrices', 'Matrices')")
    conn.commit()
    add_style_question(conn, "estilo", topic_id=7)
    add_campus_question(conn, 100, topic_id=1)
    assert _quiz_course(conn, None, NOW) == 10


def test_cleared_and_resaved_style_can_regenerate(conn):
    exam_id = make_exam(conn, [("opcion_multiple", 1)])
    prov = FakeProvider({}, right, style_batches=[[sq("misma")], [sq("misma")]])
    assert fill_style_gaps(conn, prov, NOW) == 1
    clear_style(conn, exam_id)
    save_style(conn, exam_id, ExamStyle(groups=[StyleGroup(kind="opcion_multiple", count=1, points_each=1)],
                                        topic_ids=[1]))
    assert style_gaps(conn, exam_id) == {"opcion_multiple": 1}
    assert fill_style_gaps(conn, prov, NOW) == 1
    assert [(r["stem"], r["retired"]) for r in style_rows(conn)] == [("misma", 1), ("misma", 0)]
    assert style_gaps(conn, exam_id) == {"opcion_multiple": 0}
    # Renormalizar no toca las claves (ni choca la retirada con la nueva).
    keys = [r["dedupe_key"] for r in style_rows(conn)]
    normalize_existing(conn)
    assert [r["dedupe_key"] for r in style_rows(conn)] == keys
    assert keys[1] == style_dedupe_key(exam_id, "misma", revision=1)


def test_material_text_needs_visible_section(conn):
    conn.execute("INSERT INTO section(id, course_id, num, name, visible) VALUES (2, 10, 2, 'Oculta', 0)")
    conn.execute("INSERT INTO material(id, section_id, cmid, kind, title, ref, status, summary_md)"
                 " VALUES (3, 2, 103, 'pdf', 'Oculto', 'o.pdf', 'done', 'Secreto de la oculta.')")
    conn.execute("INSERT INTO material_topic(material_id, topic_id) VALUES (3, 1)")
    conn.commit()
    text = style_material_text(conn, [1])
    assert "Apunte 1" in text and "Oculto" not in text


@pytest.mark.parametrize("sent", [False, True])
def test_batch_not_counted_when_nothing_was_sent(conn, sent):
    from estudia.ai.base import AIUnavailableError
    from estudia.generation.style_questions import _batches_today

    exam_id = make_exam(conn, [("opcion_multiple", 1)])
    prov = FakeProvider({}, right)

    def down(request):
        if sent:
            prov.requests["analysis"] += 1  # el pedido salió (y se cobró) antes de fallar
        raise AIUnavailableError("caído")

    prov.generate_style_questions = down
    with pytest.raises(AIUnavailableError):
        fill_style_gaps(conn, prov, NOW)
    assert _batches_today(conn, exam_id, NOW) == (1 if sent else 0)
