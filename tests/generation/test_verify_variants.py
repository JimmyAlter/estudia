"""Variantes calculadas en el worker de SymPy (etapa 3c, Tarea 6)."""
import json
import multiprocessing
import time

import pytest
import sympy

from estudia.generation import verify
from estudia.generation.variants import render, template_problems
from estudia.generation.verify import (
    WorkerUnavailable,
    compute_variant_values,
    format_value,
    substitute,
    worker_available,
)
from variant_fixtures import linear_question, simple_question, variant_payload


def _payload(question=None, **template_changes) -> dict:
    """El payload de la pregunta lineal, con cambios en la plantilla."""
    question = question or linear_question()
    if template_changes:
        question = question.model_copy(
            update={"variant": question.variant.model_copy(update=template_changes)})
    return variant_payload(question)


def _simple(options, check_expr, params, original, answer_expr, distractor_exprs, limit=40,
            seed=None, **kw) -> dict:
    """Payload de una plantilla corta (el enunciado no lo mira el worker)."""
    q = simple_question("x", "{a}", options, check_expr, params, original, answer_expr,
                        distractor_exprs, **kw)
    return variant_payload(q, seed=seed, limit=limit)


def _texts(variant: dict) -> list[str]:
    return [variant["answer_text"], *variant["distractor_texts"]]


def test_linear_template_gives_up_to_four_distinct_variants():
    variants = compute_variant_values(_payload())
    assert variants is not None and 1 <= len(variants) <= 4
    for v in variants:
        values = v["values"]
        assert values != {"a": 2, "b": 3, "c": 4}
        a, b, c = values["a"], values["b"], values["c"]
        assert v["answer_src"] == str(a * c + b)
        assert v["answer_text"] == str(a * c + b)
        assert v["distractor_texts"] == [str(a * (c + b)), str(a + b), str(a * c)]
        assert len(set(_texts(v))) == 4
    assert len({tuple(v["values"].values()) for v in variants}) == len(variants)


def test_same_seed_same_variants():
    assert compute_variant_values(_payload()) == compute_variant_values(_payload())


def test_limit_caps_the_variants():
    payload = _payload()
    payload["limit"] = 2
    assert len(compute_variant_values(payload)) == 2


def test_template_must_reproduce_original_key_and_distractors():
    # Otra cuenta para la clave: no reproduce el 11 original.
    assert compute_variant_values(_payload(answer_expr="a*c + b + 1")) is None
    # Los distractores tienen que seguir el orden de las opciones sin la correcta.
    assert compute_variant_values(_payload(distractor_exprs=["a*c", "a + b", "a*(c + b)"])) is None
    # Un distractor que no da su opción original.
    assert compute_variant_values(_payload(distractor_exprs=["a*(c + b)", "a + b", "a*c + 1"])) is None
    # La clave tiene que dar check_expr, no solo la opción correcta.
    question = linear_question().model_copy(update={"check_expr": "12"})
    assert compute_variant_values(_payload(question)) is None


def test_division_by_zero_values_are_skipped():
    # a/(c − 3), original a = 2, c = 5: la clave es 1; c = 3 divide por cero.
    payload = _simple(["1", "2", "5", "7"], "1", [("a", 1, 9), ("c", 2, 9)], [2, 5], "a/(c - 3)",
                      ["a", "c", "a + c"])
    variants = compute_variant_values(payload)
    assert variants
    assert all(v["values"]["c"] != 3 for v in variants)
    for v in variants:
        a, c = v["values"]["a"], v["values"]["c"]
        assert sympy.Rational(v["answer_src"]) == sympy.Rational(a, c - 3)


def test_division_by_zero_at_the_original_rejects_the_template():
    payload = _simple(["1", "2", "3", "5"], "1", [("a", 1, 9), ("c", 2, 9)], [2, 3], "a/(c - 3)",
                      ["a", "c", "a + c"])
    assert compute_variant_values(payload) is None


def test_irrational_templates_are_rejected():
    # √a con a en [2, 9] daría una clave irracional para casi todos los valores. Las plantillas
    # solo tienen + − * / y potencias fijas, así que una raíz se rechaza entera, en el worker
    # también (no solo en `template_problems`).
    payload = _simple(["3", "9", "10", "18"], "3", [("a", 3, 9)], [9], "sqrt(a)",
                      ["a", "a + 1", "2*a"], seed="semilla fija")
    assert compute_variant_values(payload) is None


def test_values_with_no_finite_key_are_skipped_without_functions():
    # Sin funciones, lo único no racional que puede salir es la división por cero (zoo): ver
    # `test_division_by_zero_values_are_skipped`. Con una semilla fija el resultado es fijo.
    payload = _simple(["1", "2", "5", "7"], "1", [("a", 1, 9), ("c", 2, 9)], [2, 5], "a/(c - 3)",
                      ["a", "c", "a + c"], seed="semilla fija")
    first = compute_variant_values(payload)
    assert first and first == compute_variant_values(payload)
    assert all(v["values"]["c"] != 3 for v in first)


def test_colliding_distractors_skip_the_tuple():
    variants = compute_variant_values(_payload())
    assert variants
    for v in variants:
        a, b, c = v["values"]["a"], v["values"]["b"], v["values"]["c"]
        assert a + b != a * c


def test_derived_is_not_part_of_the_payload():
    payload = _payload()
    assert "derived" not in payload
    for v in compute_variant_values(payload):
        assert set(v) == {"values", "answer_src", "answer_text", "distractor_texts",
                          "explanation", "common_error"}


def test_key_that_changes_sign_or_becomes_zero_skips_the_tuple():
    # c − a con el original c = 4, a = 2 (clave 2): las tuplas con c ≤ a se descartan.
    payload = _simple(["2", "6", "8", "3"], "2", [("a", 2, 9), ("c", 2, 9)], [2, 4], "c - a",
                      ["c + a", "a*c", "c - 1"])
    variants = compute_variant_values(payload)
    assert variants
    assert all(v["values"]["c"] > v["values"]["a"] for v in variants)


def test_typed_rule_must_reject_every_distractor():
    # Clave 1/a y un distractor fijo 0,333. Con a = 2 (el original) no hay problema; con a = 3 la
    # clave es 1/3 y la regla de la respuesta escrita aceptaría "0,333": esa tupla se descarta.
    payload = _simple(["0,5", "2", "0,333", "3"], "1/2", [("a", 2, 9)], [2], "1/a",
                      ["a", "333/1000", "a + 1"])
    assert payload["fractions"] is False
    assert verify._typed_accepts("1/3", "0,333") is True
    variants = compute_variant_values(payload)
    assert {v["values"]["a"] for v in variants} == {4, 5, 6, 7, 8, 9}
    for v in variants:
        assert all(verify._typed_accepts(v["answer_src"], t) is False for t in v["distractor_texts"])


def test_values_out_of_bounds_skip_the_tuple():
    # 1000·a⁴ con a en [3, 9]: hasta a = 5 entra (625000); desde a = 6 se pasa del tope.
    payload = _simple(["81000", "81", "3000", "8100"], "81000", [("a", 3, 9)], [3], "1000*a**4",
                      ["a**4", "1000*a", "100*a**4"])
    variants = compute_variant_values(payload)
    assert variants
    assert all(1000 * v["values"]["a"] ** 4 <= verify.MAX_ABS_VALUE for v in variants)
    assert {v["values"]["a"] for v in variants} <= {4, 5}


def test_fractions_follow_the_original_option():
    payload = _simple(["3/2", "6", "3", "5"], "3/2", [("a", 2, 9)], [3], "a/2",
                      ["2*a", "a", "a + 2"])
    assert payload["fractions"] is True
    assert any(v["answer_text"].endswith("/2") for v in compute_variant_values(payload))
    payload = _simple(["1,5", "6", "3", "5"], "3/2", [("a", 2, 9)], [3], "a/2",
                      ["2*a", "a", "a + 2"])
    assert payload["fractions"] is False
    assert any(v["answer_text"].endswith(",5") for v in compute_variant_values(payload))


@pytest.mark.parametrize("expr", ["__import__('os')", "a.real", "exp(a)", "x*a", "lambda: 1"])
def test_forbidden_names_never_reach_the_parser(expr):
    assert compute_variant_values(_payload(answer_expr=expr)) is None


@pytest.mark.parametrize("expr", ["2a", "a(c)", "a**c**b", "a.real"])
def test_substituted_expression_still_goes_through_the_whitelist(expr):
    src = substitute(expr, {"a": 2, "b": 3, "c": 4})
    assert src is None or verify._safe_parse(src) is None


def test_whitelist_unchanged():
    assert verify._ALLOWED_NAMES == {"sqrt", "pi", "Rational", "Integer"}


def test_substitute_wraps_values_and_keeps_allowed_names():
    assert substitute("sqrt(a)*Rational(b, 2)", {"a": 4, "b": 3}) == "sqrt((4))*Rational((3), 2)"
    assert substitute("pi*a", {"a": 2}) == "pi*(2)"
    assert substitute("x*a", {"a": 2}) is None
    assert substitute("exp(a)", {"a": 2}) is None


def test_format_value():
    assert format_value(sympy.Integer(12), False) == "12"
    assert format_value(sympy.Integer(-3), False) == "−3"
    assert format_value(sympy.Rational(25, 2), False) == "12,5"
    assert format_value(sympy.Rational(1, 3), False) == "1/3"
    assert format_value(sympy.Rational(-1, 4), True) == "−1/4"
    assert format_value(sympy.Rational(-1, 4), False) == "−0,25"
    assert format_value(sympy.Rational(1, 1000), False) == "0,001"
    assert format_value(sympy.Rational(-3, 1), True) == "−3"


def test_payload_is_plain_json():
    # Lo que viaja al worker es texto JSON, nunca un objeto de Python.
    json.dumps(_payload())


# --- La clave tiene que depender de lo que muestra el enunciado (revisión de las Tareas 5 y 6) ---

def _accepted(question) -> list[dict] | None:
    """Lo que haría quien llama: la revisión estática y después el worker."""
    if template_problems(question):
        return None
    return compute_variant_values(variant_payload(question))


PROBES = {
    # "1{a}": con a = 13 el enunciado dice 113 y la clave usa 10 + 13.
    "dígito pegado": simple_question(
        "¿Cuánto es 12 + 5?", "¿Cuánto es 1{a} + 5?", ["17", "7", "12", "25"], "17",
        [("a", 2, 20)], [2], "10 + a + 5", ["a + 5", "10 + a", "10 + a + 5 + 8"]),
    "marcadores pegados": simple_question(
        "¿Cuánto es 12 + 5?", "¿Cuánto es {a}{b} + 5?", ["17", "7", "12", "25"], "17",
        [("a", 1, 9), ("b", 2, 15)], [1, 2], "10*a + b + 5",
        ["a + b + 5 + 1", "10*a + b", "10*a + b + 13"]),
    # "0,{a}": con a = 11 el enunciado dice 0,11 y la clave usa 1,1.
    "coma decimal pegada": simple_question(
        "¿Cuánto es 0,5 + 1?", "¿Cuánto es 0,{a} + 1?", ["1,5", "2", "0,5", "3"], "3/2",
        [("a", 2, 20)], [5], "a/10 + 1", ["a/10 + 1/2", "a/10", "a/10 + 5/2"]),
    # El enunciado muestra {a}, pero la clave usa un 2 fijo.
    "parámetro fijo en la clave": simple_question(
        "Si f(x) = 2x + 3, ¿cuánto vale f(4)?", "Si f(x) = {a}x + {b}, ¿cuánto vale f({c})?",
        ["11", "14", "5", "8"], "11", [("a", 2, 9), ("b", 1, 9), ("c", 2, 9)], [2, 3, 4],
        "2*c + b", ["2*(c + b)", "2 + b", "2*c"]),
    # Con a = c = 2, a + c coincide con a·c.
    "originales que coinciden": simple_question(
        "Si f(x) = 2x + 3, ¿cuánto vale f(2)?", "Si f(x) = {a}x + {b}, ¿cuánto vale f({c})?",
        ["7", "10", "5", "4"], "7", [("a", 2, 9), ("b", 1, 9), ("c", 2, 9)], [2, 3, 2],
        "a + c + b", ["a*(c + b)", "a + b", "a*c"]),
    # El parámetro aparece en la clave pero se cancela: la clave no depende de él.
    "parámetro que se cancela": simple_question(
        "Si f(x) = 2x + 3, ¿cuánto vale f(4)?", "Si f(x) = {a}x + {b}, ¿cuánto vale f({c})?",
        ["11", "14", "5", "8"], "11", [("a", 2, 9), ("b", 1, 9), ("c", 2, 9)], [2, 3, 4],
        "2*c + b + a - a", ["2*(c + b)", "2 + b", "2*c"]),
}


@pytest.mark.parametrize("case", PROBES.keys())
def test_stem_and_key_must_match(case):
    assert _accepted(PROBES[case]) is None


def test_worker_rejects_a_parameter_missing_from_the_key():
    assert compute_variant_values(variant_payload(PROBES["parámetro fijo en la clave"])) is None


def test_worker_rejects_an_insensitive_parameter():
    # b + a − a no depende de a, sin ningún número fijo: solo lo frena el worker.
    q = simple_question("Con 3 y 4", "Con {a} y {b}", ["4", "7", "12", "1"], "4",
                        [("a", 2, 9), ("b", 2, 9)], [3, 4], "b + a - a", ["a + b", "a*b", "b - a"])
    assert template_problems(q) == []
    assert compute_variant_values(variant_payload(q)) is None


def _linear_with(original: list[int]) -> dict:
    """La plantilla lineal con otros originales, y la pregunta coherente con ellos."""
    a, b, c = original
    question = linear_question().model_copy(update={
        "options": [str(a * c + b), str(a * (c + b)), str(a + b), str(a * c)],
        "check_expr": str(a * c + b),
        "explanation": f"f({c}) = {a}·{c} + {b} = {a * c + b}.",
        "common_error": f"Olvidarse de sumar {b} a {a}·{c}.",
    })
    question.variant.original = original
    return variant_payload(question)


def test_worker_accepts_other_consistent_originals():
    assert compute_variant_values(_linear_with([3, 5, 7]))


@pytest.mark.parametrize("original", [[2, 3, 2], [3, 1, 4], [2, 3, 3]])
def test_worker_rejects_coincidence_prone_originals(original):
    # La pregunta es coherente con estos originales: solo los rechaza la regla de coincidencias.
    assert compute_variant_values(_linear_with(original)) is None


def test_linear_template_is_accepted_end_to_end():
    assert _accepted(linear_question())


def test_explanation_must_reproduce_the_original():
    # Una explicación que no corresponde a la de la pregunta.
    payload = _payload()
    payload["explanation"] = "f({c}) = {a}·{c} + {b} = {respuesta} + 1."
    assert compute_variant_values(payload) is None
    payload = _payload()
    payload["common_error"] = "Olvidarse de sumar {b} a {respuesta}."
    assert compute_variant_values(payload) is None
    # Un marcador que ya no existe (un paso intermedio) también la rechaza.
    payload = _payload()
    payload["explanation"] = "f({c}) = {producto} + {b} = {respuesta}."
    payload["original_explanation"] = "f(4) = 8 + 3 = 11."
    assert compute_variant_values(payload) is None
    # Espacios y mayúsculas no importan, como con el enunciado.
    payload = _payload()
    payload["original_explanation"] = "  F(4) = 2·4 + 3 =   11. "
    assert compute_variant_values(payload)


def test_explanation_rendered_in_a_variant_matches_its_values():
    question = linear_question()
    for v in compute_variant_values(variant_payload(question)):
        texts = {k: str(x) for k, x in v["values"].items()} | {"respuesta": v["answer_text"]}
        a, b, c = v["values"]["a"], v["values"]["b"], v["values"]["c"]
        assert render(question.variant.explanation, texts) == f"f({c}) = {a}·{c} + {b} = {a * c + b}."


# --- Salud del worker: plantilla inválida (None) contra worker que no anda (WorkerUnavailable) ---

def test_worker_timeout_raises_and_is_not_retried(monkeypatch):
    # Margen: con tope 0, `poll(0)` mira el pipe sin esperar, y el worker no pudo contestar un
    # trabajo que se acaba de mandar (la plantilla lineal tarda unos 2 ms con el worker tibio,
    # bastante más en frío). Con 1 ms el margen sería de apenas 2x y el test podría fallar al azar.
    monkeypatch.setattr(verify, "VARIANTS_TIMEOUT_S", 0.0)
    with pytest.raises(WorkerUnavailable):
        compute_variant_values(_payload())
    # El worker se mató: no queda ninguna respuesta vieja en el pipe ni un proceso colgado.
    assert verify._proc is None and verify._conn is None
    assert multiprocessing.active_children() == []
    # Durante la espera no se vuelve a probar: falla enseguida, sin arrancar otro worker.
    monkeypatch.setattr(verify, "VARIANTS_TIMEOUT_S", 10.0)
    assert worker_available() is False
    started = time.monotonic()
    with pytest.raises(WorkerUnavailable):
        compute_variant_values(_payload())
    assert time.monotonic() - started < 0.5
    assert verify._proc is None
    # Pasada la espera, vuelve a andar.
    verify._unavailable_until = 0.0
    assert worker_available() is True
    assert compute_variant_values(_payload())


def test_hanging_job_is_killed_at_the_given_timeout(monkeypatch):
    # Un trabajo colgado de verdad dentro del worker: se corta al tope que se pasa, no al de siempre.
    monkeypatch.setattr(verify, "EVAL_TIMEOUT_S", 60.0)
    verify._stop_worker()
    assert verify._equal_with_timeout("1", "1", verify._TEST_HANG, timeout=0.3) is None
    assert verify._proc is None and multiprocessing.active_children() == []


def test_malformed_payload_is_an_invalid_template_not_a_dead_worker():
    assert compute_variant_values({"not": "a payload"}) is None
    assert compute_variant_values({"bad": object()}) is None
    assert worker_available() is True
    assert verify._equal_with_timeout("1", "1", "option") is True
    assert compute_variant_values(_payload())


def test_worker_down_raises(monkeypatch):
    verify._stop_worker()
    monkeypatch.setattr(verify, "_start_worker", lambda: False)
    with pytest.raises(WorkerUnavailable):
        compute_variant_values(_payload())
    assert worker_available() is False


def test_check_worker_payload_is_the_fixture():
    from estudia import __main__ as app_main

    expected = variant_payload(linear_question())
    expected["seed"] = app_main._CHECK_VARIANTS_PAYLOAD["seed"]
    assert app_main._CHECK_VARIANTS_PAYLOAD == expected


# --- Ronda 2: solo aritmética verificable, cada dato una vez, sin pasos intermedios ---
# Cada plantilla de las sondas del revisor (probe4/5/6) que dio una variante mal. La pregunta
# original es coherente; la variante no lo era.

def _probe(stem, template_stem, options, check_expr, params, original, answer_expr, distractor_exprs,
           derived=(), explanation="Da {respuesta}.", common_error="Error.",
           original_explanation=None, original_common_error=None):
    q = simple_question(stem, template_stem, options, check_expr, params, original, answer_expr,
                        distractor_exprs, derived=derived, explanation=explanation,
                        common_error=common_error)
    update = {}
    if original_explanation is not None:
        update["explanation"] = original_explanation
    if original_common_error is not None:
        update["common_error"] = original_common_error
    return q.model_copy(update=update)


_ABC = [("a", 2, 9), ("b", 2, 9), ("c", 2, 9)]
WRONG_PROBES = {
    # "5²" con clave 2·5: en el original 2·2 = 2².
    "superíndice pegado, 2*a por a²": _probe(
        "¿Cuánto es 2²?", "¿Cuánto es {a}²?", ["4", "2", "8", "6"], "4", [("a", 2, 9)], [2],
        "2*a", ["a", "4*a", "a + 4"]),
    # "x6 + 7" se lee como x⁶ + 7.
    "letra pegada antes del marcador": _probe(
        "Si f(x) = x2 + 3, ¿f(4)?", "Si f(x) = x{a} + {b}, ¿f({c})?", ["11", "14", "5", "8"], "11",
        _ABC, [2, 3, 4], "a*c + b", ["a*(c + b)", "a + b", "a*c"]),
    # sqrt((a − 5)²) es |a − 5|: el enunciado dice a − 5.
    "valor absoluto escondido": _probe(
        "¿Cuánto es 7 − 5?", "¿Cuánto es {a} − 5?", ["2", "12", "7", "35"], "2", [("a", 2, 9)], [7],
        "sqrt((a - 5)**2)", ["a + 5", "a", "5*a"]),
    "valor absoluto de a − b": _probe(
        "¿Cuánto es 7 − 3?", "¿Cuánto es {a} − {b}?", ["4", "10", "21", "3"], "4",
        [("a", 2, 9), ("b", 2, 9)], [7, 3], "sqrt((a - b)**2)", ["a + b", "a*b", "b"]),
    # "el cuadrado de 5" con clave 10.
    "cuadrado como 2*a": _probe(
        "¿Cuál es el cuadrado de 2?", "¿Cuál es el cuadrado de {a}?", ["4", "2", "8", "6"], "4",
        [("a", 2, 9)], [2], "2*a", ["a", "4*a", "a + 4"],
        explanation="{a}·{a} = {respuesta}.", original_explanation="2·2 = 4."),
    # 2⁴ = 4²: "2 elevado a la 5" con clave 5².
    "exponente con parámetro": _probe(
        "¿Cuánto es 2 elevado a la 4?", "¿Cuánto es {a} elevado a la {b}?", ["16", "8", "6", "2"], "16",
        [("a", 2, 9), ("b", 2, 6)], [2, 4], "b**a", ["a*b", "a + b", "b - a"]),
    # Pasos intermedios falsos en la explicación o el error común.
    "derived mal en la explicación": _probe(
        "¿Cuánto es el doble de 2 más 1?", "¿Cuánto es el doble de {a} más 1?", ["5", "3", "6", "4"], "5",
        [("a", 2, 9)], [2], "2*a + 1", ["a + 1", "2*a + 2", "2*a"], derived=(("doble", "a**2"),),
        explanation="El doble de {a} es {doble}, más 1 da {respuesta}.",
        original_explanation="El doble de 2 es 4, más 1 da 5."),
    "derived mal en el error común": _probe(
        "¿Cuánto es el doble de 2 más 1?", "¿Cuánto es el doble de {a} más 1?", ["5", "3", "6", "4"], "5",
        [("a", 2, 9)], [2], "2*a + 1", ["a + 1", "2*a + 2", "2*a"], derived=(("doble", "a + 2"),),
        common_error="Quedarse con {doble}, el doble de {a}.",
        original_common_error="Quedarse con 4, el doble de 2."),
    "derived mal con dos datos": _probe(
        "¿Cuánto es el doble de 2 por 3?", "¿Cuánto es el doble de {a} por {b}?", ["12", "6", "7", "24"],
        "12", [("a", 2, 9), ("b", 3, 9)], [2, 3], "2*a*b", ["a*b", "2*a + b", "4*a*b"],
        derived=(("doble", "a + 2"),), explanation="El doble de {a} es {doble}; por {b} da {respuesta}.",
        original_explanation="El doble de 2 es 4; por 3 da 12."),
    # Separadores de miles y caracteres invisibles entre un dígito y el marcador.
    "espacio duro de miles": _probe(
        "¿Cuánto es 1\u00a0500 + 5?", "¿Cuánto es 1\u00a0{a} + 5?", ["1505", "505", "1500", "1510"], "1505",
        [("a", 495, 514)], [500], "1000 + a + 5", ["a + 5", "1000 + a", "1000 + a + 10"]),
    "espacio de miles": _probe(
        "¿Cuánto es 1 100 + 5?", "¿Cuánto es 1 {a} + 5?", ["1105", "105", "1100", "1110"], "1105",
        [("a", 95, 114)], [100], "1000 + a + 5", ["a + 5", "1000 + a", "1000 + a + 10"]),
    "apóstrofo de miles": _probe(
        "¿Cuánto es 1'100 + 5?", "¿Cuánto es 1'{a} + 5?", ["1105", "105", "1100", "1110"], "1105",
        [("a", 95, 114)], [100], "1000 + a + 5", ["a + 5", "1000 + a", "1000 + a + 10"]),
    "espacio de ancho cero": _probe(
        "¿Cuánto es 1\u200b2 + 5?", "¿Cuánto es 1\u200b{a} + 5?", ["17", "7", "12", "25"], "17",
        [("a", 2, 21)], [2], "10 + a + 5", ["a + 5", "10 + a", "10 + a + 13"]),
    "unión de palabras": _probe(
        "¿Cuánto es 1\u20602 + 5?", "¿Cuánto es 1\u2060{a} + 5?", ["17", "7", "12", "25"], "17",
        [("a", 2, 21)], [2], "10 + a + 5", ["a + 5", "10 + a", "10 + a + 13"]),
    # sqrt que esconde una constante para b > 5.
    "rama escondida con sqrt": _probe(
        "Si f(x) = 2x + 5, ¿cuánto vale f(4)?", "Si f(x) = {a}x + {b}, ¿cuánto vale f({c})?",
        ["13", "18", "7", "8"], "13", _ABC, [2, 5, 4],
        "a*c + 5 - (sqrt((b - 5)**2) - (b - 5))/2", ["a*(c + b)", "a + b", "a*c"]),
    # El mismo marcador con dos sentidos: coeficiente fijo 2 y f({a}).
    "marcador dos veces": _probe(
        "Si f(x) = 2x + 3, ¿cuánto vale f(2)?", "Si f(x) = {a}x + 3, ¿cuánto vale f({a})?",
        ["7", "10", "5", "4"], "7", [("a", 2, 9)], [2], "2*a + 3", ["2*(a + 3)", "a + 3", "2*a"]),
}

GOOD_PROBES = {
    "porcentaje": _probe(
        "¿Cuánto es el 20% de 50?", "¿Cuánto es el {a}% de 50?", ["10", "20", "5", "40"], "10",
        [("a", 11, 30)], [20], "a/2", ["a", "a/4", "2*a"]),
    "resta con signo menos": _probe(
        "¿Cuánto es −3 + 5?", "¿Cuánto es −{a} + 5?", ["2", "8", "−2", "15"], "2",
        [("a", 2, 4)], [3], "5 - a", ["5 + a", "a - 5", "5*a"]),
    "potencia con exponente fijo": _probe(
        "¿Cuánto es 3 al cubo menos 4?", "¿Cuánto es {a} al cubo menos 4?", ["23", "27", "4", "12"], "23",
        [("a", 3, 9)], [3], "a**3 - 4", ["a**3", "4", "3*a + 3"]),
}


@pytest.mark.parametrize("case", WRONG_PROBES.keys())
def test_reviewer_probes_that_gave_wrong_variants_are_rejected(case):
    q = WRONG_PROBES[case]
    if template_problems(q):
        return
    assert compute_variant_values(variant_payload(q, limit=40)) is None


@pytest.mark.parametrize("case", GOOD_PROBES.keys())
def test_reviewer_probes_that_behaved_still_work(case):
    q = GOOD_PROBES[case]
    assert template_problems(q) == []
    assert compute_variant_values(variant_payload(q, limit=40))


def test_worker_rechecks_the_grammar_and_coincidences():
    # El worker no confía en `template_problems`: repite cada regla con el payload solo.
    assert compute_variant_values(variant_payload(WRONG_PROBES["valor absoluto escondido"], limit=40)) is None
    assert compute_variant_values(variant_payload(WRONG_PROBES["exponente con parámetro"], limit=40)) is None
    # "el cuadrado de {a}" con 2*a: depende de a y reproduce la explicación; solo lo frena el 2
    # fijo de la clave, igual al original.
    assert compute_variant_values(variant_payload(WRONG_PROBES["cuadrado como 2*a"], limit=40)) is None
    # a² + b con a = 2: solo lo frena "ningún 2 con una potencia" (2² = 2·2 = 2 + 2).
    square = simple_question("x", "{a} {b}", ["7", "4", "3", "14"], "7", [("a", 2, 9), ("b", 3, 9)],
                             [2, 3], "a**2 + b", ["a**2", "b", "a**2*b + 2"])
    assert compute_variant_values(variant_payload(square, limit=40)) is None
    same_with_3 = simple_question("x", "{a} {b}", ["13", "9", "4", "38"], "13", [("a", 3, 9), ("b", 4, 9)],
                                  [3, 4], "a**2 + b", ["a**2", "b", "a**2*b + 2"])
    assert compute_variant_values(variant_payload(same_with_3, limit=40))


def test_arithmetic_ok():
    names = {"a", "b"}
    for expr in ["a*b + 2", "(a - b)/3", "-a + 1.5", "a**2 + b**4", "a^3", "333/1000 + a"]:
        assert verify.arithmetic_ok(expr, names), expr
    for expr in ["sqrt(a)", "abs(a)", "pi*a", "a**b", "a**5", "a**0", "(a**2)**2", "a**2**2",
                 "a**(1/2)", "Rational(a, 2)", "a < b", "1e5*a", "c*a", "+a", "a**-1", ""]:
        assert not verify.arithmetic_ok(expr, names), expr


def test_fixed_numbers_skip_exponents():
    assert verify.fixed_numbers("2*c + b") == {2}
    assert verify.fixed_numbers("a**3 - 4") == {4}
