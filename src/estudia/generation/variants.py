"""Variantes numéricas de una pregunta con `check_expr` (etapa 3c).

La plantilla viene del LLM, o sea que es entrada no confiable. Acá solo se valida su forma
(nombres, rangos, marcadores y expresiones), sin evaluar nada: las cuentas se hacen solamente
en el worker de SymPy, con tope de tiempo. Las expresiones se revisan con la misma lista blanca
que `check_expr` (`verify._is_safe`), después de reemplazar cada parámetro por `(<entero>)`.
"""

from __future__ import annotations

import hashlib
import re
import string
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from estudia.ai.base import DocQuestion, GenQuestion, VariantTemplate
from estudia.generation.verify import (
    _is_safe,
    arithmetic_ok,
    compute_variant_values,
    fixed_numbers,
    has_power,
    substitute,
    variant_line,
    variant_texts,
)

MAX_GROUP_SIZE = 5       # preguntas por grupo, contando la original
MAX_PARAMS = 3
MAX_PARAM_VALUES = 20    # valores enteros de un rango, contando los dos extremos
MAX_PARAM_VALUE = 1000
ANSWER_PLACEHOLDER = "respuesta"
DISTRACTORS = 3

_PARAM_NAME = re.compile(r"^[a-z]$")
_WORD = re.compile(r"[A-Za-z]+")
# Un marcador pegado a un número o a otro marcador: el valor nuevo cambiaría un número escrito
# ("1{a}" con a = 13 da "113"). Cada carácter se pasa a una clase y se buscan estos patrones:
#   F marcador, D dígito de cualquier escritura (también ², ½, １, ١), A letra, S coma o punto,
#   T espacio de cualquier tipo o apóstrofo (separadores de miles: "1 {a}", "1'{a}").
# Se rechazan "{a}{b}", "1{a}", "{a}0", "x{a}" (se lee como exponente), "0,{a}", "{a},5",
# "{a},{b}", "1 {a}" y "{a} 000". Una letra después sí vale: "{a}x" es coeficiente por variable.
_GLUED = re.compile(r"FF|[DA]F|FD|[DF]SF|FS[DF]|DTF|FTD")
_APOSTROPHES = "'’ʼ‘"


def _char_class(char: str) -> str:
    category = unicodedata.category(char)
    if category.startswith("N"):
        return "D"
    if char.isalpha():
        return "A"
    if char in ",.":
        return "S"
    if category == "Zs" or char in _APOSTROPHES:
        return "T"
    return "o"


def _glued(text: str) -> bool:
    """¿Algún marcador del texto está pegado a un número o a otro marcador? (ver `_GLUED`)"""
    classes = "".join("".join(_char_class(ch) for ch in literal) + ("" if field is None else "F")
                      for literal, field, _, _ in string.Formatter().parse(text))
    return bool(_GLUED.search(classes))


def _invisible(text: str) -> bool:
    """¿Tiene caracteres de formato invisibles (espacio de ancho cero, unión de palabras…)?"""
    return any(unicodedata.category(ch) == "Cf" for ch in text)


def _fields(text: str) -> list[str] | None:
    """Los marcadores `{nombre}` del texto, en orden; None si alguno trae formato o conversión
    (`{a:2}`, `{a!r}`) o si hay una llave suelta (también `{{`, que se leería como una llave)."""
    try:
        parts = list(string.Formatter().parse(text))
    except ValueError:
        return None
    fields = []
    for literal, field, spec, conversion in parts:
        if "{" in literal or "}" in literal:
            return None
        if field is None:
            continue
        if spec or conversion is not None:
            return None
        fields.append(field)
    return fields


def render(text: str, values: dict[str, str]) -> str | None:
    """Reemplaza cada `{nombre}` por su valor. None si aparece un marcador desconocido, un campo
    con formato o conversión, o una llave suelta."""
    try:
        parts = list(string.Formatter().parse(text))
    except ValueError:
        return None
    out = []
    for literal, field, spec, conversion in parts:
        if "{" in literal or "}" in literal:
            return None
        out.append(literal)
        if field is None:
            continue
        if spec or conversion is not None or field not in values:
            return None
        out.append(values[field])
    return "".join(out)


def _expr_ok(expr: str, value_sets: list[dict[str, int]]) -> bool:
    """¿La expresión usa solo los parámetros y la gramática de `check_expr`, con cualquiera de
    estos juegos de valores? Revisión estática (lista blanca y AST), sin evaluar nada. Usa la
    misma sustitución que el worker (`verify.substitute`)."""
    src = expr.strip()
    if not src:
        return False
    for values in value_sets:
        replaced = substitute(src, values)
        if replaced is None or not _is_safe(replaced):
            return False
    return True


def _params_problems(t: VariantTemplate) -> list[str]:
    problems = []
    names = [p.name for p in t.params]
    if not 1 <= len(t.params) <= MAX_PARAMS:
        problems.append("cantidad de parámetros fuera de 1 a 3")
    if any(not _PARAM_NAME.match(n) for n in names):
        problems.append("nombre de parámetro inválido")
    if len(set(names)) != len(names):
        problems.append("nombres de parámetros repetidos")
    for p in t.params:
        if not (1 <= p.low < p.high <= MAX_PARAM_VALUE and p.high - p.low + 1 <= MAX_PARAM_VALUES):
            problems.append("rango de parámetro inválido")
            break
    if len(t.original) != len(t.params):
        problems.append("cantidad de valores originales distinta de la de parámetros")
    elif any(not p.low <= v <= p.high for p, v in zip(t.params, t.original)):
        problems.append("valor original fuera de su rango")
    return problems


def _text_problems(t: VariantTemplate, question: GenQuestion | DocQuestion) -> list[str]:
    from estudia.generation.pipeline import collapse_spaces  # perezoso: pipeline usará este módulo

    problems = []
    params = {p.name for p in t.params}
    for label, text in (("enunciado", t.stem), ("explicación", t.explanation), ("error común", t.common_error)):
        if _invisible(text):
            problems.append(f"{label} con caracteres invisibles")
    stem_fields = _fields(t.stem)
    if stem_fields is None:
        problems.append("enunciado con formato o llaves sueltas")
    elif not stem_fields:
        problems.append("enunciado sin marcadores")
    elif not set(stem_fields) <= params:
        problems.append("enunciado con marcadores que no son parámetros")
    elif set(stem_fields) != params:
        problems.append("parámetro que no aparece en el enunciado")
    elif any(n > 1 for n in Counter(stem_fields).values()):
        problems.append("parámetro repetido en el enunciado")
    elif _glued(t.stem):
        problems.append("enunciado con un marcador pegado a un número o a otro marcador")
    elif len(t.original) == len(t.params):
        # Los originales son enteros positivos; la Tarea 6 los escribe con verify.format_value.
        original = {p.name: str(v) for p, v in zip(t.params, t.original)}
        rendered = render(t.stem, original)
        if rendered is None or (collapse_spaces(rendered).casefold()
                                != collapse_spaces(question.stem).casefold()):
            problems.append("el enunciado con los valores originales no da el de la pregunta")
    allowed = params | {ANSWER_PLACEHOLDER}   # sin pasos intermedios: nada que se pueda calcular mal
    for label, text in (("explicación", t.explanation), ("error común", t.common_error)):
        fields = _fields(text)
        if fields is None:
            problems.append(f"{label} con formato o llaves sueltas")
        elif not set(fields) <= allowed:
            problems.append(f"{label} con marcadores desconocidos")
        elif _glued(text):
            problems.append(f"{label} con un marcador pegado a un número o a otro marcador")
    return problems


def _expr_problems(t: VariantTemplate) -> list[str]:
    names = [p.name for p in t.params]
    # Con los originales y con los máximos: los máximos dan el exponente y el largo más grandes
    # (los valores son positivos), que es lo único de `_is_safe` que depende de los valores.
    value_sets = [dict(zip(names, t.original)), {p.name: p.high for p in t.params}]
    problems = []
    if not _expr_ok(t.answer_expr, value_sets):
        problems.append("answer_expr inválida")
    # Cada número del enunciado tiene que entrar en la clave; que la clave dependa de verdad de
    # cada uno lo comprueba el worker.
    if not set(names) <= set(_WORD.findall(t.answer_expr)):
        problems.append("parámetro que no aparece en answer_expr")
    if any(not _expr_ok(e, value_sets) for e in t.distractor_exprs):
        problems.append("distractor_exprs inválida")
    # Solo aritmética que se verifica con certeza (sin raíces, funciones ni exponentes variables).
    if not arithmetic_ok(t.answer_expr, names):
        problems.append("answer_expr fuera de la aritmética permitida")
    if any(not arithmetic_ok(e, names) for e in t.distractor_exprs):
        problems.append("distractor_exprs fuera de la aritmética permitida")
    # Un 2 con una potencia coincide con otras cuentas (2² = 2·2 = 2 + 2); un número fijo de la
    # clave igual a un original puede ser un dato del enunciado escrito a mano (`2*c` con a = 2).
    if has_power([t.answer_expr, *t.distractor_exprs]) and 2 in t.original:
        problems.append("valor original 2 con una potencia")
    if any(v in fixed_numbers(t.answer_expr) for v in t.original):
        problems.append("número fijo en answer_expr igual a un valor original")
    return problems


def template_problems(question: GenQuestion | DocQuestion) -> list[str]:
    """Lista vacía si la plantilla de la pregunta sirve; si no, un motivo corto por problema.

    Los motivos no llevan texto de la pregunta ni del estudiante: se pueden loguear en DEBUG.
    No evalúa nada: que la plantilla reproduzca la original lo comprueba el worker (Tarea 6).
    """
    t = question.variant
    if t is None:
        return ["sin plantilla"]
    problems = []
    if not (question.check_expr or "").strip():
        problems.append("pregunta sin check_expr")
    if len(t.distractor_exprs) != DISTRACTORS:
        problems.append("cantidad de distractores distinta de 3")
    params_problems = _params_problems(t)
    # Originales repetidos, 0 o 1 se prestan a coincidencias: con a = c = 2, a + c da lo mismo
    # que a·c, y con b = 1, a·b da lo mismo que a. La plantilla podría reproducir la original
    # con otra cuenta.
    if len(set(t.original)) != len(t.original) or any(v in (0, 1) for v in t.original):
        problems.append("valores originales repetidos o iguales a 0 o 1")
    problems += params_problems + _text_problems(t, question)
    # Con parámetros inválidos no hay juegos de valores confiables para revisar las expresiones.
    problems += _expr_problems(t) if not params_problems else ["expresiones sin revisar"]
    return problems


@dataclass
class VariantQuestion:
    """Una variante ya escrita con sus valores. Tiene los atributos que lee
    `pipeline._passes_local_checks` y los que guarda `pipeline._finalize`."""

    stem: str
    options: list[str]
    correct_index: int
    explanation: str
    common_error: str
    topic: str
    check_expr: str
    values: dict[str, int]
    source_seconds: int | None = None
    source_page: int | None = None


def variant_payload(question: GenQuestion | DocQuestion, limit: int) -> dict:
    """El pedido de `verify.compute_variant_values` para una pregunta con plantilla.

    La semilla sale de la plantilla: la misma plantilla da siempre los mismos valores.
    """
    t = question.variant
    return {
        "params": [p.model_dump() for p in t.params],
        "original": list(t.original),
        "answer_expr": t.answer_expr,
        "distractor_exprs": list(t.distractor_exprs),
        "explanation": t.explanation,
        "common_error": t.common_error,
        "check_expr": question.check_expr,
        "original_options": list(question.options),
        "original_explanation": question.explanation,
        "original_common_error": question.common_error,
        "correct_index": question.correct_index,
        "fractions": "/" in question.options[question.correct_index],
        "seed": hashlib.sha256(t.model_dump_json().encode()).hexdigest(),
        "limit": limit,
    }


def _stem_key(stem: str) -> str:
    from estudia.generation.pipeline import collapse_spaces  # perezoso: pipeline usa este módulo

    return collapse_spaces(stem).casefold()


def _variant(question: GenQuestion | DocQuestion, computed: dict) -> VariantQuestion | None:
    """La variante escrita exactamente con los valores que devolvió el worker; None si algo no
    se puede escribir (no debería pasar: el worker ya renderizó la plantilla con los originales)."""
    t = question.variant
    try:
        values = {str(k): int(v) for k, v in computed["values"].items()}
        answer_text, answer_src = str(computed["answer_text"]), str(computed["answer_src"])
        distractors = [str(d) for d in computed["distractor_texts"]]
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    if set(values) != {p.name for p in t.params} or len(distractors) != DISTRACTORS:
        return None
    texts = {name: str(value) for name, value in values.items()}
    stem = render(t.stem, texts)
    if stem is None:
        return None
    # Explicación y error común con la regla de `verify.variant_texts`, armados acá y en el
    # worker: si no coinciden, ante la duda, solo la línea fija y sin error común.
    names = [p.name for p in t.params]
    explanation, common_error = variant_texts(t.explanation, t.common_error, names, values, answer_text)
    if (computed.get("explanation"), computed.get("common_error")) != (explanation, common_error):
        explanation, common_error = variant_line(names, values, answer_text), ""
    # La correcta en el mismo lugar que en la original; los distractores en las demás, en orden.
    options = distractors[:question.correct_index] + [answer_text] + distractors[question.correct_index:]
    return VariantQuestion(
        stem=stem, options=options, correct_index=question.correct_index,
        explanation=explanation, common_error=common_error, topic=question.topic,
        check_expr=answer_src, values=values,
        source_seconds=getattr(question, "source_seconds", None),
        source_page=getattr(question, "source_page", None),
    )


def build_variants(question: GenQuestion | DocQuestion,
                   passes: Callable[[object], bool]) -> list[VariantQuestion]:
    """Las variantes de una original, hasta `MAX_GROUP_SIZE - 1`, cada una con `passes(v) is True`
    y un enunciado distinto del de la original y de las demás.

    Lista vacía si la plantilla no sirve (`template_problems`) o el worker la rechaza.
    `verify.WorkerUnavailable` se propaga: quien llama decide no pedir más.
    Las variantes todavía no pasaron la pasada a ciegas: eso lo hace la tubería.
    """
    if template_problems(question):
        return []
    computed = compute_variant_values(variant_payload(question, MAX_GROUP_SIZE - 1))
    if computed is None:
        return []
    variants: list[VariantQuestion] = []
    seen = {_stem_key(question.stem)}
    for item in computed:
        if len(variants) >= MAX_GROUP_SIZE - 1:
            break
        variant = _variant(question, item)
        if variant is None or _stem_key(variant.stem) in seen:
            continue
        if passes(variant) is not True:
            continue
        seen.add(_stem_key(variant.stem))
        variants.append(variant)
    return variants
