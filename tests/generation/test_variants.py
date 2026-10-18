"""Plantilla de variantes numéricas: validación estática (sin evaluar nada con SymPy)."""
import json

import pytest

from estudia.ai.base import DocQuestion, GenQuestion, VariantParam, VariantTemplate, VideoAnalysis
from estudia.generation.pipeline import load_analysis
from estudia.generation.prompts import (
    _VARIANT_RULE,
    ANALYSIS_PROMPT,
    DOCUMENT_PROMPT,
    STYLE_QUESTIONS_PROMPT,
)
from estudia.generation.variants import (
    ANSWER_PLACEHOLDER,
    MAX_GROUP_SIZE,
    MAX_PARAM_VALUE,
    MAX_PARAM_VALUES,
    MAX_PARAMS,
    render,
    template_problems,
)
from variant_fixtures import linear_question


def test_constants():
    assert (MAX_GROUP_SIZE, MAX_PARAMS, MAX_PARAM_VALUES, MAX_PARAM_VALUE) == (5, 3, 20, 1000)
    assert ANSWER_PLACEHOLDER == "respuesta"


def test_linear_template_has_no_problems():
    assert template_problems(linear_question()) == []


def test_doc_question_template_has_no_problems():
    q = linear_question()
    doc = DocQuestion(**q.model_dump(exclude={"source_seconds"}), source_page=3)
    assert template_problems(doc) == []


def _q(question=None, **template_over) -> GenQuestion:
    """La pregunta lineal con cambios en la plantilla (`template_over`) o en la pregunta."""
    q = linear_question()
    if template_over:
        q = q.model_copy(update={"variant": q.variant.model_copy(update=template_over)})
    if question:
        q = q.model_copy(update=question)
    return q


def _params(*specs):
    return [VariantParam(name=n, low=lo, high=hi) for n, lo, hi in specs]


_ABC = (("a", 2, 9), ("b", 1, 9), ("c", 2, 9))
_STEM_ABC = "Si f(x) = {a}x + {b}, ¿cuánto vale f({c})?"

CASES = {
    # La pregunta no tiene plantilla o no tiene check_expr.
    "sin plantilla": _q({"variant": None}),
    "check_expr None": _q({"check_expr": None}),
    "check_expr vacío": _q({"check_expr": "  "}),
    # Nombres de parámetros.
    "nombre de dos letras": _q(params=_params(("ab", 2, 9), *_ABC[1:]),
                               stem="Si f(x) = {ab}x + {b}, ¿cuánto vale f({c})?"),
    "nombre en mayúscula": _q(params=_params(("A", 2, 9), *_ABC[1:]),
                              stem="Si f(x) = {A}x + {b}, ¿cuánto vale f({c})?", answer_expr="A*c + b"),
    "nombre con acento": _q(params=_params(("á", 2, 9), *_ABC[1:])),
    "nombre vacío": _q(params=_params(("", 2, 9), *_ABC[1:])),
    "nombre repetido": _q(params=_params(("a", 2, 9), ("a", 1, 9), ("c", 2, 9))),
    # Cantidad de parámetros.
    "sin parámetros": _q(params=[], original=[]),
    "cuatro parámetros": _q(params=_params(*_ABC, ("d", 1, 9)), original=[2, 3, 4, 5]),
    # Rangos.
    "21 valores": _q(params=_params(("a", 1, 21), *_ABC[1:])),
    "low cero": _q(params=_params(("a", 0, 9), *_ABC[1:])),
    "low negativo": _q(params=_params(("a", -3, 9), *_ABC[1:])),
    "low igual a high": _q(params=_params(("a", 2, 2), *_ABC[1:])),
    "low mayor que high": _q(params=_params(("a", 9, 2), *_ABC[1:])),
    "high pasado del tope": _q(params=_params(("a", 990, 1001), *_ABC[1:]), original=[1000, 3, 4],
                               question={"stem": "Si f(x) = 1000x + 3, ¿cuánto vale f(4)?"}),
    # Valores originales.
    "original corto": _q(original=[2, 3]),
    "original largo": _q(original=[2, 3, 4, 5]),
    "original fuera de rango": _q(original=[10, 3, 4],
                                  question={"stem": "Si f(x) = 10x + 3, ¿cuánto vale f(4)?"}),
    # Distractores.
    "dos distractores": _q(distractor_exprs=["a*(c + b)", "a + b"]),
    "cuatro distractores": _q(distractor_exprs=["a*(c + b)", "a + b", "a*c", "b*c"]),
    # Enunciado.
    "enunciado sin marcadores": _q(params=_params(("a", 2, 9)), original=[2],
                                   stem="Si f(x) = 2x + 3, ¿cuánto vale f(4)?", answer_expr="a*4 + 3",
                                   distractor_exprs=["a*7", "a + 3", "a*4"],
                                   explanation="f(4) = {respuesta}.", common_error="Olvidarse de sumar 3."),
    "marcador desconocido en el enunciado": _q(stem=_STEM_ABC + " {d}"),
    "marcador de otro nombre en el enunciado": _q(stem=_STEM_ABC + " {producto}"),
    "marcador respuesta en el enunciado": _q(stem=_STEM_ABC + " {respuesta}"),
    "formato en el enunciado": _q(stem="Si f(x) = {a:>3}x + {b}, ¿cuánto vale f({c})?"),
    "conversión en el enunciado": _q(stem="Si f(x) = {a!r}x + {b}, ¿cuánto vale f({c})?"),
    "llave suelta en el enunciado": _q(stem=_STEM_ABC + " {"),
    "llave doble en el enunciado": _q(stem=_STEM_ABC + " {{a}}"),
    "atributo en el enunciado": _q(stem="Si f(x) = {a.real}x + {b}, ¿cuánto vale f({c})?"),
    "parámetro sin usar en el enunciado": _q(stem="Si f(x) = {a}x + {b}, ¿cuánto vale f(4)?"),
    "el enunciado no reproduce la original": _q(stem="Si f(x) = {a}x − {b}, ¿cuánto vale f({c})?"),
    "orden de originales cambiado": _q(original=[3, 2, 4]),
    # Explicación y error común.
    "marcador desconocido en la explicación": _q(explanation="f({c}) = {d} = {respuesta}."),
    "formato en la explicación": _q(explanation="f({c}) = {respuesta:2}."),
    "llave suelta en el error común": _q(common_error="Olvidarse de sumar {b} y quedarse con {producto"),
    "marcador desconocido en el error común": _q(common_error="Olvidarse de {suma}."),
    # Expresiones: solo los parámetros y la gramática de check_expr.
    "answer_expr con un nombre desconocido": _q(answer_expr="a*c + d"),
    "answer_expr con otro nombre": _q(answer_expr="producto + b"),
    "answer_expr con producto implícito": _q(answer_expr="a c + b"),
    "answer_expr con número pegado": _q(answer_expr="2a + b"),
    "answer_expr con llamada a parámetro": _q(answer_expr="a(c) + b"),
    "answer_expr con función no permitida": _q(answer_expr="exp(a) + b"),
    "answer_expr con atributo": _q(answer_expr="a.real + b"),
    "answer_expr con dunder": _q(answer_expr="__import__('os')"),
    "answer_expr con comparación": _q(answer_expr="a < b"),
    "answer_expr con decimal científico": _q(answer_expr="1e5*a"),
    "answer_expr vacía": _q(answer_expr=""),
    "answer_expr con exponente grande en el rango": _q(
        params=_params(("a", 2, 9), ("b", 90, 109), ("c", 2, 9)), original=[2, 93, 4],
        question={"stem": "Si f(x) = 2x + 93, ¿cuánto vale f(4)?", "check_expr": "2**93"},
        answer_expr="a**b"),
    "answer_expr con tres potencias": _q(answer_expr="a**2 + b**2 + c**2"),
    "distractor inválido": _q(distractor_exprs=["a*(c + b)", "a + x", "a*c"]),
    # Solo aritmética verificable (ronda 2): sin funciones, constantes ni exponentes variables.
    "answer_expr con sqrt": _q(answer_expr="sqrt(a*a)*c + b"),
    "answer_expr con pi": _q(answer_expr="a*c + b + pi - pi"),
    "answer_expr con Rational": _q(answer_expr="Rational(a*c + b, 1)"),
    "answer_expr con parámetro en el exponente": _q(answer_expr="a**b + 3"),
    "answer_expr con potencias anidadas": _q(answer_expr="(a**2)**2 - 5"),
    "answer_expr con exponente 5": _q(answer_expr="a**5 - 21"),
    "answer_expr con exponente negativo": _q(answer_expr="a**-1*22"),
    "distractor con sqrt": _q(distractor_exprs=["a*(c + b)", "sqrt(a*a) + b", "a*c"]),
    "explicación con un paso intermedio": _q(explanation="f({c}) = {producto} + {b} = {respuesta}."),
    # Cada dato una vez en el enunciado, sin caracteres invisibles ni separadores de miles.
    "marcador repetido en el enunciado": _q(stem="Si f(x) = {a}x + {b}, ¿cuánto vale f({c})? ({a})"),
    "espacio de ancho cero en el enunciado": _q(stem="Si f(x) = {a}\u200bx + {b}, ¿cuánto vale f({c})?"),
    "unión de palabras en la explicación": _q(explanation="f({c}) =\u2060 {respuesta}."),
    "espacio de miles antes del marcador": _q(stem="Si f(x) = 1 {a}x + {b}, ¿cuánto vale f({c})?"),
    "espacio duro de miles": _q(stem="Si f(x) = 1\u00a0{a}x + {b}, ¿cuánto vale f({c})?"),
    "espacio fino de miles": _q(stem="Si f(x) = 1\u202f{a}x + {b}, ¿cuánto vale f({c})?"),
    "apóstrofo de miles": _q(stem="Si f(x) = 1'{a}x + {b}, ¿cuánto vale f({c})?"),
    "espacio de miles después del marcador": _q(stem="Si f(x) = {a} 000x + {b}, ¿cuánto vale f({c})?"),
    "superíndice pegado": _q(stem="Si f(x) = {a}²x + {b}, ¿cuánto vale f({c})?"),
    "fracción pegada": _q(stem="Si f(x) = {a}½x + {b}, ¿cuánto vale f({c})?"),
    "dígito de ancho completo pegado": _q(stem="Si f(x) = １{a}x + {b}, ¿cuánto vale f({c})?"),
    "letra antes del marcador": _q(stem="Si f(x) = x{a} + {b}, ¿cuánto vale f({c})?"),
    # Originales que se prestan a coincidencias.
    "original 2 con una potencia": _q(answer_expr="a**2*c - 5 + b"),
    "número fijo igual a un original": _q(answer_expr="a*c + b + 2 - 2"),
    # Marcadores pegados a un número o a otro marcador: el valor nuevo cambiaría el número escrito.
    "dígito antes del marcador": _q(stem="Si f(x) = 1{a}x + {b}, ¿cuánto vale f({c})?"),
    "dígito después del marcador": _q(stem="Si f(x) = {a}0x + {b}, ¿cuánto vale f({c})?"),
    "coma decimal antes del marcador": _q(stem="Si f(x) = 0,{a}x + {b}, ¿cuánto vale f({c})?"),
    "coma decimal después del marcador": _q(stem="Si f(x) = {a},5x + {b}, ¿cuánto vale f({c})?"),
    "punto decimal antes del marcador": _q(stem="Si f(x) = 1.{a}x + {b}, ¿cuánto vale f({c})?"),
    "marcadores pegados": _q(stem="Si f(x) = {a}{b}x, ¿cuánto vale f({c})?"),
    "dígito pegado en la explicación": _q(explanation="f({c}) = 1{respuesta}."),
    "marcadores pegados en el error común": _q(common_error="Quedarse con {b}{a}."),
    # La clave tiene que depender de cada número del enunciado.
    "parámetro fijo en la clave": _q(answer_expr="2*c + b", distractor_exprs=["2*(c + b)", "2 + b", "2*c"]),
    # Originales que se prestan a coincidencias (a + c = a·c con 2 y 2; a·1 = a).
    "originales repetidos": _q(original=[2, 3, 2],
                               question={"stem": "Si f(x) = 2x + 3, ¿cuánto vale f(2)?"}),
    "original 1": _q(original=[2, 1, 4], question={"stem": "Si f(x) = 2x + 1, ¿cuánto vale f(4)?"}),
}


# El motivo que tiene que aparecer en cada caso (puede haber otros).
EXPECTED = {
    'sin plantilla': 'sin plantilla',
    'check_expr None': 'pregunta sin check_expr',
    'check_expr vacío': 'pregunta sin check_expr',
    'nombre de dos letras': 'nombre de parámetro inválido',
    'nombre en mayúscula': 'nombre de parámetro inválido',
    'nombre con acento': 'nombre de parámetro inválido',
    'nombre vacío': 'nombre de parámetro inválido',
    'nombre repetido': 'nombres de parámetros repetidos',
    'sin parámetros': 'cantidad de parámetros fuera de 1 a 3',
    'cuatro parámetros': 'cantidad de parámetros fuera de 1 a 3',
    '21 valores': 'rango de parámetro inválido',
    'low cero': 'rango de parámetro inválido',
    'low negativo': 'rango de parámetro inválido',
    'low igual a high': 'rango de parámetro inválido',
    'low mayor que high': 'rango de parámetro inválido',
    'high pasado del tope': 'rango de parámetro inválido',
    'original corto': 'cantidad de valores originales distinta de la de parámetros',
    'original largo': 'cantidad de valores originales distinta de la de parámetros',
    'original fuera de rango': 'valor original fuera de su rango',
    'dos distractores': 'cantidad de distractores distinta de 3',
    'cuatro distractores': 'cantidad de distractores distinta de 3',
    'enunciado sin marcadores': 'enunciado sin marcadores',
    'marcador desconocido en el enunciado': 'enunciado con marcadores que no son parámetros',
    'marcador de otro nombre en el enunciado': 'enunciado con marcadores que no son parámetros',
    'marcador respuesta en el enunciado': 'enunciado con marcadores que no son parámetros',
    'formato en el enunciado': 'enunciado con formato o llaves sueltas',
    'conversión en el enunciado': 'enunciado con formato o llaves sueltas',
    'llave suelta en el enunciado': 'enunciado con formato o llaves sueltas',
    'llave doble en el enunciado': 'enunciado con formato o llaves sueltas',
    'atributo en el enunciado': 'enunciado con marcadores que no son parámetros',
    'parámetro sin usar en el enunciado': 'parámetro que no aparece en el enunciado',
    'el enunciado no reproduce la original': 'el enunciado con los valores originales no da el de la pregunta',
    'orden de originales cambiado': 'el enunciado con los valores originales no da el de la pregunta',
    'marcador desconocido en la explicación': 'explicación con marcadores desconocidos',
    'formato en la explicación': 'explicación con formato o llaves sueltas',
    'llave suelta en el error común': 'error común con formato o llaves sueltas',
    'marcador desconocido en el error común': 'error común con marcadores desconocidos',
    'answer_expr con un nombre desconocido': 'answer_expr inválida',
    'answer_expr con otro nombre': 'answer_expr inválida',
    'answer_expr con producto implícito': 'answer_expr inválida',
    'answer_expr con número pegado': 'answer_expr inválida',
    'answer_expr con llamada a parámetro': 'answer_expr inválida',
    'answer_expr con función no permitida': 'answer_expr inválida',
    'answer_expr con atributo': 'answer_expr inválida',
    'answer_expr con dunder': 'answer_expr inválida',
    'answer_expr con comparación': 'answer_expr inválida',
    'answer_expr con decimal científico': 'answer_expr inválida',
    'answer_expr vacía': 'answer_expr inválida',
    'answer_expr con exponente grande en el rango': 'answer_expr inválida',
    'answer_expr con tres potencias': 'answer_expr inválida',
    'distractor inválido': 'distractor_exprs inválida',
    'answer_expr con sqrt': 'answer_expr fuera de la aritmética permitida',
    'answer_expr con pi': 'answer_expr fuera de la aritmética permitida',
    'answer_expr con Rational': 'answer_expr fuera de la aritmética permitida',
    'answer_expr con parámetro en el exponente': 'answer_expr fuera de la aritmética permitida',
    'answer_expr con potencias anidadas': 'answer_expr fuera de la aritmética permitida',
    'answer_expr con exponente 5': 'answer_expr fuera de la aritmética permitida',
    'answer_expr con exponente negativo': 'answer_expr fuera de la aritmética permitida',
    'distractor con sqrt': 'distractor_exprs fuera de la aritmética permitida',
    'explicación con un paso intermedio': 'explicación con marcadores desconocidos',
    'marcador repetido en el enunciado': 'parámetro repetido en el enunciado',
    'espacio de ancho cero en el enunciado': 'enunciado con caracteres invisibles',
    'unión de palabras en la explicación': 'explicación con caracteres invisibles',
    'espacio de miles antes del marcador': 'enunciado con un marcador pegado a un número o a otro marcador',
    'espacio duro de miles': 'enunciado con un marcador pegado a un número o a otro marcador',
    'espacio fino de miles': 'enunciado con un marcador pegado a un número o a otro marcador',
    'apóstrofo de miles': 'enunciado con un marcador pegado a un número o a otro marcador',
    'espacio de miles después del marcador': 'enunciado con un marcador pegado a un número o a otro marcador',
    'superíndice pegado': 'enunciado con un marcador pegado a un número o a otro marcador',
    'fracción pegada': 'enunciado con un marcador pegado a un número o a otro marcador',
    'dígito de ancho completo pegado': 'enunciado con un marcador pegado a un número o a otro marcador',
    'letra antes del marcador': 'enunciado con un marcador pegado a un número o a otro marcador',
    'original 2 con una potencia': 'valor original 2 con una potencia',
    'número fijo igual a un original': 'número fijo en answer_expr igual a un valor original',
    'dígito antes del marcador': 'enunciado con un marcador pegado a un número o a otro marcador',
    'dígito después del marcador': 'enunciado con un marcador pegado a un número o a otro marcador',
    'coma decimal antes del marcador': 'enunciado con un marcador pegado a un número o a otro marcador',
    'coma decimal después del marcador': 'enunciado con un marcador pegado a un número o a otro marcador',
    'punto decimal antes del marcador': 'enunciado con un marcador pegado a un número o a otro marcador',
    'marcadores pegados': 'enunciado con un marcador pegado a un número o a otro marcador',
    'dígito pegado en la explicación': 'explicación con un marcador pegado a un número o a otro marcador',
    'marcadores pegados en el error común': 'error común con un marcador pegado a un número o a otro marcador',
    'parámetro fijo en la clave': 'parámetro que no aparece en answer_expr',
    'originales repetidos': 'valores originales repetidos o iguales a 0 o 1',
    'original 1': 'valores originales repetidos o iguales a 0 o 1',
}


@pytest.mark.parametrize("case", CASES.keys())
def test_template_problems(case):
    problems = template_problems(CASES[case])
    assert EXPECTED[case] in problems
    assert all(isinstance(p, str) and p for p in problems)


def test_problems_do_not_include_question_text():
    q = _q({"stem": "Texto privado del estudiante 1234"})
    problems = template_problems(q)
    assert problems
    assert not any("privado" in p or "1234" in p for p in problems)


def test_twenty_values_and_top_value_are_accepted():
    q = _q(params=_params(("a", 981, 1000), *_ABC[1:]), original=[1000, 3, 4],
           question={"stem": "Si f(x) = 1000x + 3, ¿cuánto vale f(4)?", "check_expr": "4003"})
    assert template_problems(q) == []


def test_stem_compared_ignoring_spaces_and_case():
    q = _q({"stem": "  si  f(x) = 2x + 3,\n¿CUÁNTO vale f(4)? "})
    assert template_problems(q) == []


def test_placeholder_next_to_letters_or_spaces_is_fine():
    # "{a}x" (coeficiente por variable), "f({c})" y "x²" no cambian ningún número escrito.
    assert template_problems(_q(stem="Si f(x) = {a}x + {b}, ¿cuánto vale f({c})?")) == []
    assert template_problems(_q(explanation="f({c}) = {a}·{c} + {b} = {respuesta}.")) == []
    assert template_problems(_q(stem="Si f(x) = {a}x + {b}, ¿cuánto vale f( {c} )?",
                                question={"stem": "Si f(x) = 2x + 3, ¿cuánto vale f( 4 )?"})) == []


def test_render_replaces_known_placeholders():
    assert render("{a} + {b} = {respuesta}", {"a": "2", "b": "3", "respuesta": "5"}) == "2 + 3 = 5"
    assert render("sin marcadores", {}) == "sin marcadores"
    assert render("{a}{a}", {"a": "7"}) == "77"


def test_render_rejects_unknown_placeholders_and_stray_braces():
    values = {"a": "2", "b": "3"}
    assert render("{c}", values) is None
    assert render("{}", values) is None
    assert render("{0}", values) is None
    assert render("{a:2}", values) is None
    assert render("{a:>3}", values) is None
    assert render("{a!r}", values) is None
    assert render("{a!s}", values) is None
    assert render("{a.real}", values) is None
    assert render("{a[0]}", values) is None
    assert render("{a", values) is None
    assert render("a}", values) is None
    assert render("{ a }", values) is None
    assert render("{{a}}", values) is None
    assert render("conjunto {1, 2}", values) is None


def _raw_video_analysis(question: dict) -> str:
    return json.dumps({"summary_md": "# Resumen", "topics": ["Funciones lineales"],
                       "questions": [question]})


def test_old_analysis_json_without_variant_still_loads():
    q = linear_question().model_dump(exclude={"variant"})
    assert "variant" not in q
    analysis = load_analysis("youtube", _raw_video_analysis(q))
    assert isinstance(analysis, VideoAnalysis)
    assert analysis.questions[0].variant is None


def test_analysis_json_with_variant_round_trips():
    raw = _raw_video_analysis(linear_question().model_dump())
    analysis = load_analysis("youtube", raw)
    assert analysis.questions[0] == linear_question()
    assert template_problems(analysis.questions[0]) == []


def test_derived_from_the_model_is_ignored():
    # Las plantillas ya no tienen pasos intermedios: un "derived" que mande el modelo no se lee,
    # y una explicación que lo use queda con un marcador desconocido.
    data = linear_question().model_dump()
    data["variant"]["derived"] = [{"name": "producto", "expr": "a*c"}]
    q = GenQuestion.model_validate(data)
    assert "derived" not in VariantTemplate.model_fields
    assert not hasattr(q.variant, "derived")
    assert template_problems(q) == []


VARIANT_RULE_TEXT = """\
- "variant": completalo solo en preguntas con "check_expr" cuyos números se \
puedan cambiar sin cambiar el razonamiento; en las demás, null. Es una plantilla \
de la misma pregunta:
  - "params": de 1 a 3 parámetros, cada uno con "name" (una letra minúscula), \
"low" y "high" (enteros positivos, con como mucho 20 valores entre los dos, \
contando los dos).
  - "original": los valores de los parámetros en esta pregunta, en el mismo orden. \
Distintos entre sí, ninguno 0 ni 1, y ninguno 2 si alguna cuenta tiene una potencia.
  - "stem", "explanation" y "common_error": los mismos textos de la pregunta, \
con cada número que cambia escrito como {a}, {b}, etc. En "stem" cada \
parámetro aparece una sola vez. En "explanation" y "common_error" solo podés usar \
los parámetros y {respuesta}: nada de resultados intermedios.
  - En las variantes la app escribe sola una línea con los valores y la respuesta. \
La explicación de la plantilla se agrega a esa línea solo si es prosa sin \
números ni marcadores (tampoco el de la respuesta). El error común se muestra \
en las variantes con la misma regla.
  - "answer_expr": la cuenta de la respuesta con las letras de los parámetros. \
Solo números, +, -, *, / y paréntesis, con "*" siempre explícito, y ** solo con \
un exponente fijo de 1 a 4. Nada de raíces ni otras funciones. Con los valores \
de "original" tiene que dar exactamente "check_expr".
  - "distractor_exprs": 3 cuentas con las mismas reglas, una por cada error \
típico, en el mismo orden en que aparecen los distractores en "options".
"""


def test_variant_rule_in_video_and_document_prompts_only():
    assert ANALYSIS_PROMPT.endswith(_VARIANT_RULE)
    assert DOCUMENT_PROMPT.endswith(_VARIANT_RULE)
    assert _VARIANT_RULE not in STYLE_QUESTIONS_PROMPT
    assert '"variant"' not in STYLE_QUESTIONS_PROMPT


def test_variant_rule_reaches_the_model_verbatim():
    # Los prompts pasan por str.format: las llaves de {a} y {respuesta} van escapadas.
    video = ANALYSIS_PROMPT.format(title="Clase 1")
    document = DOCUMENT_PROMPT.format(title="Apunte 3", source="el apunte en PDF", pages_note="")
    assert video.endswith(VARIANT_RULE_TEXT)
    assert document.endswith(VARIANT_RULE_TEXT)


# --- Tarea 7: variantes armadas desde una original ------------------------------------------


def test_build_variants_from_linear_question():
    from estudia.generation.pipeline import _passes_local_checks
    from estudia.generation.variants import VariantQuestion, build_variants

    original = linear_question()
    variants = build_variants(original, _passes_local_checks)
    assert 1 <= len(variants) <= MAX_GROUP_SIZE - 1
    stems = {v.stem for v in variants}
    assert len(stems) == len(variants) and original.stem not in stems
    for v in variants:
        assert isinstance(v, VariantQuestion)
        a, b, c = v.values["a"], v.values["b"], v.values["c"]
        assert v.correct_index == 0
        assert v.check_expr == str(a * c + b)
        assert v.options[0] == str(a * c + b)
        assert v.options[1:] == [str(a * (c + b)), str(a + b), str(a * c)]
        assert v.stem == f"Si f(x) = {a}x + {b}, ¿cuánto vale f({c})?"
        # La plantilla usa parámetros: la variante lleva solo la línea fija, sin error común.
        assert v.explanation == f"Con a = {a}, b = {b} y c = {c}, la respuesta es {a * c + b}."
        assert v.common_error == ""
        assert "{" not in v.explanation and "{" not in v.common_error
        assert (v.topic, v.source_seconds, v.source_page) == (original.topic, original.source_seconds, None)


def test_build_variants_keeps_correct_index_of_the_original():
    from estudia.generation.pipeline import _passes_local_checks
    from estudia.generation.variants import build_variants

    original = linear_question()
    moved = original.model_copy(update={"options": ["14", "5", "11", "8"], "correct_index": 2})
    template = moved.variant.model_copy(update={"distractor_exprs": ["a*(c + b)", "a + b", "a*c"]})
    moved = moved.model_copy(update={"variant": template})
    variants = build_variants(moved, _passes_local_checks)
    assert variants
    for v in variants:
        a, b, c = v.values["a"], v.values["b"], v.values["c"]
        assert v.correct_index == 2
        assert v.options == [str(a * (c + b)), str(a + b), str(a * c + b), str(a * c)]


def test_build_variants_drops_what_local_checks_reject():
    from estudia.generation.variants import build_variants

    seen = []

    def passes(v):
        seen.append(v.values["a"])
        return v.values["a"] != 5

    variants = build_variants(linear_question(), passes)
    assert all(v.values["a"] != 5 for v in variants)
    assert len(variants) <= MAX_GROUP_SIZE - 1
    # Solo cuenta un True de verdad: algo que no es True (None, 1) se descarta.
    assert build_variants(linear_question(), lambda v: 1) == []
    assert build_variants(linear_question(), lambda v: None) == []


def test_no_variants_without_check_expr_or_template():
    from estudia.generation.variants import build_variants

    q = linear_question()
    assert build_variants(q.model_copy(update={"variant": None}), lambda v: True) == []
    assert build_variants(q.model_copy(update={"check_expr": None}), lambda v: True) == []
    broken = q.variant.model_copy(update={"answer_expr": "sqrt(a)*c + b"})
    assert build_variants(q.model_copy(update={"variant": broken}), lambda v: True) == []


def test_no_variants_when_the_worker_rejects_the_template(monkeypatch):
    from estudia.generation import variants as variants_module

    monkeypatch.setattr(variants_module, "compute_variant_values", lambda payload: None)
    assert variants_module.build_variants(linear_question(), lambda v: True) == []


def test_worker_unavailable_propagates(monkeypatch):
    from estudia.generation import variants as variants_module
    from estudia.generation.verify import WorkerUnavailable

    def down(payload):
        raise WorkerUnavailable("caído")

    monkeypatch.setattr(variants_module, "compute_variant_values", down)
    with pytest.raises(WorkerUnavailable):
        variants_module.build_variants(linear_question(), lambda v: True)


def test_build_variants_skips_repeated_stems(monkeypatch):
    from estudia.generation import variants as variants_module

    one = {"values": {"a": 3, "b": 5, "c": 4}, "answer_src": "17", "answer_text": "17",
           "distractor_texts": ["27", "8", "12"]}
    original = {"values": {"a": 2, "b": 3, "c": 4}, "answer_src": "11", "answer_text": "11",
                "distractor_texts": ["14", "5", "8"]}
    monkeypatch.setattr(variants_module, "compute_variant_values", lambda payload: [one, one, original])
    variants = variants_module.build_variants(linear_question(), lambda v: True)
    assert [v.values for v in variants] == [one["values"]]


def test_build_variants_never_more_than_group_allows(monkeypatch):
    from estudia.generation import variants as variants_module

    many = [{"values": {"a": a, "b": 5, "c": 7}, "answer_src": str(7 * a + 5), "answer_text": str(7 * a + 5),
             "distractor_texts": [str(12 * a), str(a + 5), str(7 * a)]} for a in range(3, 10)]
    payloads = []
    monkeypatch.setattr(variants_module, "compute_variant_values",
                        lambda payload: payloads.append(payload) or many)
    variants = variants_module.build_variants(linear_question(), lambda v: True)
    assert len(variants) == MAX_GROUP_SIZE - 1
    assert payloads[0]["limit"] == MAX_GROUP_SIZE - 1


def test_malformed_worker_output_gives_no_variant(monkeypatch):
    from estudia.generation import variants as variants_module

    bad = [{"values": {"a": 3}}, {"values": {"a": 3, "b": 5, "z": 4}, "answer_src": "1", "answer_text": "1",
                                  "distractor_texts": ["2", "3", "4"]}, "nada"]
    monkeypatch.setattr(variants_module, "compute_variant_values", lambda payload: bad)
    assert variants_module.build_variants(linear_question(), lambda v: True) == []
