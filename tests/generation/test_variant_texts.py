"""Textos de las variantes (revisión final de la etapa 3c): la explicación y el error común de una
variante nunca llevan un número que no se pueda garantizar.

La explicación de una variante es siempre la línea fija «Con a = 7, b = 4 y c = 5, la respuesta es
39.»; la plantilla del modelo se agrega después solo si sus partes literales no tienen números y su
único marcador es {respuesta}. El error común queda solo si es prosa pura (sin números ni
marcadores). La original conserva sus textos.
"""
import json
import re

import pytest

from estudia.ai.base import GenQuestion, VariantTemplate, VideoAnalysis
from estudia.db import connect
from estudia.generation import verify
from estudia.generation.pipeline import process_pending
from estudia.generation.variants import template_problems
from fakes import FakeProvider

_LINEAR = re.compile(r"= (\d+)x \+ (\d+), ¿cuánto vale [fg]\((\d+)\)\?")


def blind_solving_linear(stem, options):
    """Resuelve de verdad «f(x) = ax + b, ¿f(c)?» y elige la opción por su texto (no por lugar)."""
    m = _LINEAR.search(stem)
    if m is None:
        return None
    a, b, c = (int(x) for x in m.groups())
    return options.index(str(a * c + b))


def _question(stem, options, check, explanation, common_error, template):
    return GenQuestion(stem=stem, options=options, correct_index=0, explanation=explanation,
                       common_error=common_error, topic="T", source_seconds=0, check_expr=check,
                       variant=VariantTemplate.model_validate(template))


_PARAMS_F = [{"name": "a", "low": 2, "high": 9}, {"name": "b", "low": 3, "high": 9},
             {"name": "c", "low": 3, "high": 9}]
_PARAMS_G = [{"name": "a", "low": 3, "high": 9}, {"name": "b", "low": 3, "high": 9},
             {"name": "c", "low": 3, "high": 9}]
_EXPRS = {"answer_expr": "a*c + b", "distractor_exprs": ["a*(c + b)", "a + b", "a*c"]}


def literal_common_error_question(explanation="f(4) = 2·4 + 3 = 11.",
                                  template_explanation="f({c}) = {a}·{c} + {b} = {respuesta}.",
                                  common_error="Sumar antes de multiplicar da 14.",
                                  template_common_error="Sumar antes de multiplicar da 14."):
    """La sonda del revisor: un número fijo (14, un distractor) en el error común."""
    return _question(
        "Si f(x) = 2x + 3, ¿cuánto vale f(4)?", ["11", "14", "5", "8"], "11",
        explanation, common_error,
        dict(params=_PARAMS_F, original=[2, 3, 4], stem="Si f(x) = {a}x + {b}, ¿cuánto vale f({c})?",
             explanation=template_explanation, common_error=template_common_error, **_EXPRS))


def literal_intermediate_question():
    """La sonda del revisor: un resultado intermedio fijo (20) en la explicación."""
    return _question(
        "Si g(x) = 5x + 3, ¿cuánto vale g(4)?", ["23", "35", "8", "20"], "23",
        "g(4) = 5·4 + 3 = 20 + 3 = 23.", "Olvidarse de sumar.",
        dict(params=_PARAMS_G, original=[5, 3, 4], stem="Si g(x) = {a}x + {b}, ¿cuánto vale g({c})?",
             explanation="g({c}) = {a}·{c} + {b} = 20 + {b} = {respuesta}.",
             common_error="Olvidarse de sumar.", **_EXPRS))


def _run(tmp_path, question):
    assert template_problems(question) == []
    conn = connect(tmp_path / "t.db")
    conn.execute("INSERT INTO section(id, course_id, num, name, visible) VALUES (1, 10, 1, 'S1', 1)")
    conn.execute("INSERT INTO material(id, section_id, cmid, kind, title, ref, status)"
                 " VALUES (1, 1, 1001, 'youtube', 'V', 'v1', 'pending')")
    conn.commit()
    prov = FakeProvider({"v1": VideoAnalysis(summary_md="# R", topics=["T"], questions=[question])},
                        blind_solving_linear)
    process_pending(conn, prov)
    rows = conn.execute("SELECT id, stem, options_json, correct_index, explanation, common_error,"
                        " variant_group, variant_values_json FROM question ORDER BY id").fetchall()
    head, members = rows[0], rows[1:]
    assert head["variant_group"] == head["id"]
    assert members, "el grupo tiene que tener variantes"
    return head, members


def _line(values: dict, answer: str) -> str:
    pairs = [f"{n} = {values[n]}" for n in ("a", "b", "c")]
    return f"Con {pairs[0]}, {pairs[1]} y {pairs[2]}, la respuesta es {answer}."


def _key(row) -> str:
    return json.loads(row["options_json"])[row["correct_index"]]


@pytest.mark.parametrize("make,common_error", [
    (literal_common_error_question, ""),
    # «Olvidarse de sumar.» es prosa pura (sin números ni marcadores): se queda.
    (literal_intermediate_question, "Olvidarse de sumar."),
])
def test_reviewer_probes_store_no_derived_number(tmp_path, make, common_error):
    question = make()
    head, members = _run(tmp_path, question)
    # La original queda con sus textos, sin tocar.
    assert (head["explanation"], head["common_error"]) == (question.explanation, question.common_error)
    for r in members:
        values = json.loads(r["variant_values_json"])
        text = r["explanation"] + " " + (r["common_error"] or "")
        assert "20" not in text and "14" not in text
        assert r["explanation"] == _line(values, _key(r))
        assert (r["common_error"] or "") == common_error


def test_placeholder_coinciding_with_an_intermediate_falls_back(tmp_path):
    # {b} escrito donde había un 3 que era c − 1: con otros valores ya no coincide.
    question = literal_common_error_question(
        explanation="f(4) = 2·4 + 3 = 11.", template_explanation="f({c}) = {a}·{c} + {b} = {respuesta}.",
        common_error="Sumar {b} de más.".replace("{b}", "3"), template_common_error="Sumar {b} de más.")
    _, members = _run(tmp_path, question)
    for r in members:
        values = json.loads(r["variant_values_json"])
        assert r["explanation"] == _line(values, _key(r))
        assert not r["common_error"]


def test_full_width_digit_in_a_template_falls_back(tmp_path):
    question = literal_common_error_question(
        explanation="El resultado es 11, más de １ cifra.",
        template_explanation="El resultado es {respuesta}, más de １ cifra.",
        common_error="Confundir el orden de １ paso.", template_common_error="Confundir el orden de １ paso.")
    _, members = _run(tmp_path, question)
    for r in members:
        values = json.loads(r["variant_values_json"])
        assert "１" not in r["explanation"] + (r["common_error"] or "")
        assert r["explanation"] == _line(values, _key(r))
        assert not r["common_error"]


def test_respuesta_template_falls_back_and_prose_common_error_is_kept(tmp_path):
    # {respuesta} solo podría estar donde había otro número igual a la respuesta (ver abajo):
    # la línea fija ya dice la respuesta, así que una plantilla con marcadores no se agrega.
    question = literal_common_error_question(
        explanation="Se multiplica y después se suma: da 11.",
        template_explanation="Se multiplica y después se suma: da {respuesta}.",
        common_error="Sumar antes de multiplicar.", template_common_error="Sumar antes de multiplicar.")
    _, members = _run(tmp_path, question)
    for r in members:
        values = json.loads(r["variant_values_json"])
        key = _key(r)
        assert r["explanation"] == _line(values, key)
        assert r["common_error"] == "Sumar antes de multiplicar."


def test_prose_template_without_placeholders_is_appended(tmp_path):
    question = literal_common_error_question(
        explanation="Se multiplica y después se suma.",
        template_explanation="Se multiplica y después se suma.",
        common_error="Sumar antes de multiplicar.", template_common_error="Sumar antes de multiplicar.")
    _, members = _run(tmp_path, question)
    for r in members:
        values = json.loads(r["variant_values_json"])
        assert r["explanation"] == f"{_line(values, _key(r))} Se multiplica y después se suma."


def blind_solving_minus(stem, options):
    m = re.search(r"= (\d+)x − (\d+), ¿cuánto vale f\((\d+)\)\?", stem)
    if m is None:
        return None
    a, b, c = (int(x) for x in m.groups())
    return options.index(str(a * c - b))


def test_respuesta_standing_in_for_a_parameter_equal_to_the_answer_falls_back(tmp_path):
    # La sonda de la re-revisión: f(x) = 3x − 8 en 4 da 4, así que «x = {respuesta}» reproduce la
    # original, pero en una variante diría «x = 10» con c = 3.
    question = _question(
        "Si f(x) = 3x − 8, ¿cuánto vale f(4)?", ["4", "20", "12", "7"], "4",
        "Se evalúa en x = 4 y da 4.", "Restar mal.",
        dict(params=[{"name": n, "low": 2, "high": 9} for n in "abc"], original=[3, 8, 4],
             stem="Si f(x) = {a}x − {b}, ¿cuánto vale f({c})?", answer_expr="a*c - b",
             distractor_exprs=["a*c + b", "a*c", "a + c"],
             explanation="Se evalúa en x = {respuesta} y da {respuesta}.", common_error="Restar mal."))
    assert template_problems(question) == []
    conn = connect(tmp_path / "t.db")
    conn.execute("INSERT INTO section(id, course_id, num, name, visible) VALUES (1, 10, 1, 'S1', 1)")
    conn.execute("INSERT INTO material(id, section_id, cmid, kind, title, ref, status)"
                 " VALUES (1, 1, 1001, 'youtube', 'V', 'v1', 'pending')")
    conn.commit()
    prov = FakeProvider({"v1": VideoAnalysis(summary_md="# R", topics=["T"], questions=[question])},
                        blind_solving_minus)
    process_pending(conn, prov)
    rows = conn.execute("SELECT explanation, options_json, correct_index, variant_values_json"
                        " FROM question ORDER BY id").fetchall()
    assert rows[0]["explanation"] == "Se evalúa en x = 4 y da 4."
    assert len(rows) > 1
    for r in rows[1:]:
        values = json.loads(r["variant_values_json"])
        assert r["explanation"] == _line(values, _key(r))
        assert "Se evalúa" not in r["explanation"]


# --- Reglas, sin la tubería -----------------------------------------------------------------

def test_variant_line_uses_the_spanish_list_style():
    assert verify.variant_line(["a"], {"a": 7}, "39") == "Con a = 7, la respuesta es 39."
    assert verify.variant_line(["a", "b"], {"a": 7, "b": 4}, "39") == "Con a = 7 y b = 4, la respuesta es 39."
    assert (verify.variant_line(["a", "b", "c"], {"a": 7, "b": 4, "c": 5}, "−1/3")
            == "Con a = 7, b = 4 y c = 5, la respuesta es −1/3.")


@pytest.mark.parametrize("explanation,kept", [
    ("Da {respuesta}.", False),
    ("Se suma y se multiplica.", True),
    ("Da {respuesta}, más que 2.", False),
    ("Da {respuesta}, el doble de {a}.", False),
    ("Da {b}.", False),
    ("Da {respuesta}²", False),
    ("La mitad (½) da {respuesta}.", False),
    ("Da {respuesta} en ٣ pasos.", False),
    ("Da {respuesta:2}.", False),
    ("Da {respuesta", False),
])
def test_explanation_template_rule(explanation, kept):
    line = "Con a = 7, la respuesta es 39."
    expl, _ = verify.variant_texts(explanation, "Error.", ["a"], {"a": 7}, "39")
    if kept:
        assert expl == line + " " + explanation.replace("{respuesta}", "39")
    else:
        assert expl == line


@pytest.mark.parametrize("common_error,kept", [
    ("Sumar antes de multiplicar.", True),
    ("Sumar antes de multiplicar da 14.", False),
    ("Quedarse con {respuesta}.", False),
    ("Olvidarse de {b}.", False),
    ("Contar ⅓ de más.", False),
])
def test_common_error_template_rule(common_error, kept):
    _, err = verify.variant_texts("Da {respuesta}.", common_error, ["a"], {"a": 7}, "39")
    assert err == (common_error if kept else "")


def test_parent_falls_back_when_the_worker_disagrees():
    from estudia.generation.variants import _variant

    question = literal_common_error_question(
        explanation="Se multiplica y se suma.", template_explanation="Se multiplica y se suma.",
        common_error="Sumar antes de multiplicar.", template_common_error="Sumar antes de multiplicar.")
    computed = {"values": {"a": 3, "b": 4, "c": 5}, "answer_src": "19", "answer_text": "19",
                "distractor_texts": ["27", "7", "15"]}
    line = "Con a = 3, b = 4 y c = 5, la respuesta es 19."
    agree = _variant(question, {**computed, "explanation": f"{line} Se multiplica y se suma.",
                                "common_error": "Sumar antes de multiplicar."})
    assert (agree.explanation, agree.common_error) == (f"{line} Se multiplica y se suma.",
                                                       "Sumar antes de multiplicar.")
    for worker in ({}, {"explanation": line, "common_error": ""},
                   {"explanation": f"{line} Se multiplica y se suma.", "common_error": "Otra cosa."}):
        v = _variant(question, {**computed, **worker})
        assert (v.explanation, v.common_error) == (line, "")


def test_worker_returns_the_same_texts_as_the_parent():
    from variant_fixtures import variant_payload

    question = literal_intermediate_question()
    computed = verify.compute_variant_values(variant_payload(question))
    assert computed
    for item in computed:
        values = item["values"]
        assert item["explanation"] == _line(values, item["answer_text"])
        assert item["common_error"] == "Olvidarse de sumar."


# --- Números escritos con palabras (revisión final 3c, segunda vuelta) ------------------------

def test_reviewer_probe_with_a_number_word_empties_the_common_error(tmp_path):
    question = literal_common_error_question(
        common_error="Sumar antes de multiplicar da catorce.",
        template_common_error="Sumar antes de multiplicar da catorce.")
    _, members = _run(tmp_path, question)
    for r in members:
        assert "catorce" not in r["explanation"]
        assert not r["common_error"]


@pytest.mark.parametrize("text,kept", [
    ("Olvidarse de sumar.", True),
    ("Sumar antes de multiplicar da catorce.", False),
    ("Da el doble.", False),
    ("Da el DOBLE.", False),
    ("Da una docena.", False),
    ("Sumar una decena.", False),
    ("Da el quíntuple.", False),
    ("Un billón.", False),
    ("Quedarse con la mitad.", False),
    ("Contar dieciséis pasos.", False),
    ("Contar dieciseis pasos.", False),
    ("Sumar veintitrés.", False),
    ("Restar doscientas.", False),
    ("Sumar quinientos.", False),
    ("Sumar setecientos.", False),
    ("Es el sétimo.", False),
    ("Es el séptimo.", False),
    ("Es el tercer paso.", False),
    ("Con veinte y treintas.", False),
    ("Usar un millón.", False),
    ("Tomar el séptimo término.", False),
    ("Dividir en tercios.", False),
    ("Contar cero.", False),
    ("Hay tres.", False),
    # Fuera de la lista a propósito: «un», «una», «uno» y «primero» son prosa común.
    ("Sumar un término de más en el primero y olvidar una parte.", True),
    ("Da un cuarto de lo correcto.", False),
    ("Las cuartas partes.", False),
    ("Sumar antes da XIV.", False),
    ("Sumar antes da MCM.", False),
    ("Sumar antes da CATÓRCE.", False),
    # Una sola mayúscula o una sigla con otras letras no es un número romano.
    ("Usar la fórmula I del apunte y la del DNI.", True),
    ("Mezclar MIX con otra cosa.", False),   # MIX también es un romano válido: falla cerrado
    # Palabras enteras: «dosis», «tresillo» o «once» dentro de otra palabra no cuentan.
    ("Confundir la dosis con el tresillo.", True),
])
def test_number_words_in_common_error(text, kept):
    _, err = verify.variant_texts("Da {respuesta}.", text, ["a"], {"a": 7}, "39")
    assert err == (text if kept else "")


@pytest.mark.parametrize("explanation,kept", [
    ("Se multiplica y después se suma.", True),
    ("Es el doble de lo que da sin sumar.", False),
    ("Da más de cien.", False),
])
def test_number_words_in_explanation(explanation, kept):
    line = "Con a = 7, la respuesta es 39."
    expl, _ = verify.variant_texts(explanation, "Error.", ["a"], {"a": 7}, "39")
    assert expl == (line + " " + explanation.replace("{respuesta}", "39") if kept else line)
