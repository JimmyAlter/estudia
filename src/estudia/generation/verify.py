"""Verificación de respuestas: red de seguridad antes de mostrar una pregunta.

`check_expr` viene del LLM, o sea que es entrada no confiable: nunca se ejecuta
código arbitrario. Se valida con una lista blanca de caracteres y nombres antes
de pasarla al parser de SymPy, y se acota el tamaño de las potencias.
"""

from __future__ import annotations

import ast
import atexit
import json
import multiprocessing
import random
import re
import string
import threading
import time
import unicodedata

import sympy
from sympy.parsing.sympy_parser import (
    convert_xor,
    parse_expr,
    rationalize,
    standard_transformations,
)

MAX_EXPR_LEN = 200
MAX_POWERS = 2
MAX_EXPONENT = 99
MAX_LITERAL_DIGITS = 15
# Tope de tiempo para todo el trabajo de SymPy (en un proceso aparte, que se mata).
EVAL_TIMEOUT_S = 2.0
# Respuesta escrita con decimales: además de estar bien redondeada a los decimales escritos, el
# error relativo no puede pasar de esto (así "1.4" no vale por √2, pero "1.41" sí).
TYPED_REL_TOLERANCE = sympy.Rational(5, 1000)

_GLOBALS = {
    "sqrt": sympy.sqrt,
    "pi": sympy.pi,
    "Rational": sympy.Rational,
    "Integer": sympy.Integer,
}
_ALLOWED_NAMES = set(_GLOBALS)
_ALLOWED_CHARS = re.compile(r"^[0-9A-Za-z+\-*/^().,\s]+$")
_DOT_LETTER = re.compile(r"\.\s*[A-Za-z]")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_TRANSFORMATIONS = standard_transformations + (rationalize, convert_xor)
_BIN_OPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)
_UNARY_OPS = (ast.USub, ast.UAdd)


def _is_small_int(node: ast.AST) -> bool:
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, _UNARY_OPS):
        node = node.operand
    return (
        isinstance(node, ast.Constant)
        and type(node.value) is int
        and node.value <= MAX_EXPONENT
    )


def _structure_ok(expr: str) -> bool:
    """Chequeo estructural: solo aritmética, llamadas permitidas y potencias chicas."""
    try:
        tree = ast.parse(expr.replace("^", "**"), mode="eval")
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return False
    powers = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.Expression, ast.Load, *_BIN_OPS, *_UNARY_OPS)):
            if isinstance(node, ast.Pow):
                continue
        elif isinstance(node, ast.BinOp):
            pass
        elif isinstance(node, ast.UnaryOp):
            pass
        elif isinstance(node, ast.Constant):
            if type(node.value) not in (int, float):
                return False
        elif isinstance(node, ast.Name):
            if node.id not in _ALLOWED_NAMES:
                return False
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.keywords:
                return False
            if node.func.id not in _ALLOWED_NAMES:
                return False
        else:
            return False
        if isinstance(node, ast.BinOp):
            if not isinstance(node.op, _BIN_OPS):
                return False
            if isinstance(node.op, ast.Pow):
                powers += 1
                if powers > MAX_POWERS or not _is_small_int(node.right):
                    return False
        if isinstance(node, ast.UnaryOp) and not isinstance(node.op, _UNARY_OPS):
            return False
    return True


def _is_safe(expr: str) -> bool:
    if not expr or len(expr) > MAX_EXPR_LEN:
        return False
    if "_" in expr or "lambda" in expr or _DOT_LETTER.search(expr):
        return False
    if not _ALLOWED_CHARS.match(expr):
        return False
    if any(name not in _ALLOWED_NAMES for name in re.findall(r"[A-Za-z]+", expr)):
        return False
    if any(len(n) > MAX_LITERAL_DIGITS + 1 for n in _NUMBER.findall(expr)):
        return False
    return _structure_ok(expr)


def _safe_parse(expr: str) -> sympy.Expr | None:
    """Parsea una expresión numérica ya normalizada; None si no es segura o válida."""
    if not _is_safe(expr):
        return None
    try:
        result = parse_expr(
            expr,
            local_dict={},
            global_dict=dict(_GLOBALS),
            transformations=_TRANSFORMATIONS,
        )
    except Exception:
        return None
    if not isinstance(result, sympy.Expr) or result.free_symbols:
        return None
    return result


def _normalize_option(option: str) -> str:
    text = option.strip().replace("−", "-").replace("π", "pi")
    if re.fullmatch(r"[+-]?\d+,\d+", text):
        text = text.replace(",", ".")
    text = re.sub(r"√\s*(\d+(?:\.\d+)?|\([^()]*\))", r"sqrt(\1)", text)
    return re.sub(r"(\d|\))\s*(sqrt|pi)", r"\1*\2", text)


def parse_option(option: str) -> sympy.Expr | None:
    """Convierte el texto de una opción en un número o expresión de SymPy."""
    if not isinstance(option, str):
        return None
    return _safe_parse(_normalize_option(option))


def _equal(expected_src: str, given_src: str) -> bool | None:
    """True/False si se pudo decidir; None si alguna de las dos no se pudo leer o no se pudo probar.

    Una opción que no se puede leer ("½", "50%", "1/2 cm") puede ser equivalente
    igual: no se sabe (None), nunca "distinta".
    """
    expected = _safe_parse(expected_src)
    if expected is None:
        return None
    if not expected.evalf(30).is_finite:
        return None
    given = _safe_parse(given_src)
    if given is None:
        return None
    diff = expected - given
    # Camino rápido numérico: si claramente no es cero, no hace falta simplify.
    approx = diff.evalf(30)
    if not approx.is_finite or abs(approx) > 1e-20:
        return False
    # Iguales a 30 dígitos pero sin prueba simbólica: no se sabe.
    return True if sympy.simplify(diff) == 0 else None


_ROOT_WORD = re.compile(r"ra[ií]z(?:\s+cuadrada)?(?:\s+de)?", re.IGNORECASE)
_DECIMAL = re.compile(r"[+-]?\d+\.(\d+)")
# Superíndices que se leen como exponente: una tira seguida es un solo `^(...)`. Hay que
# convertirlos antes de NFKC, que los volvería dígitos sueltos ("2⁴" → "24").
_SUPERSCRIPT = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺", "0123456789-+")
_SUPERSCRIPT_RUN = re.compile("[⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]+")
_LEADING_SIGN = re.compile(r"^([+-])\s+")
_BARE_DOT = re.compile(r"(?<!\d)\.(?=\d)")
UNREADABLE = "unreadable"       # respuesta del worker: lo escrito no se pudo leer
UNCHECKABLE = "uncheckable"     # respuesta del worker: la expresión de control no sirve
# Modo de trabajo del worker: ¿la regla de la respuesta escrita aceptaría el texto de esta opción?
TYPED_OPTION = "typed_option"
# Modo de trabajo del worker: valores de las variantes de una plantilla (etapa 3c).
VARIANTS = "variants"
VARIANTS_TIMEOUT_S = 10.0
MAX_VARIANT_TRIES = 60
# Topes de cada valor de una variante (clave y distractores).
MAX_ABS_VALUE = 10**6
MAX_DENOMINATOR = 1000
_PARAM_TOKEN = re.compile(r"(?<![A-Za-z_])([a-z])(?![A-Za-z_0-9])")
# Exponente más alto de una potencia en una plantilla (`a**2`, `a**4`; nunca `a**b`).
MAX_VARIANT_EXPONENT = 4
_MINUS = "−"


class GradingUnavailable(RuntimeError):
    """No se pudo corregir ahora (worker caído o tiempo agotado): se puede reintentar."""


class WorkerUnavailable(RuntimeError):
    """El worker de SymPy no anda ahora (no arrancó, se cayó, se pasó del tope o falló hace poco)."""


class UncheckableExpr(ValueError):
    """La expresión de control no se puede usar para corregir (ilegible, con letras o infinita)."""


def _is_script(char: str) -> bool:
    return unicodedata.decomposition(char).startswith(("<super>", "<sub>"))


def _normalize_typed(typed: str) -> str | None:
    """Lo que escribe un estudiante, al formato de las opciones: "2 raiz 2" → "2*sqrt(2)".

    None si queda algún superíndice o subíndice que no es un exponente numérico ("2ⁿ", "H₂").
    """
    text = _SUPERSCRIPT_RUN.sub(lambda m: "^(" + m.group().translate(_SUPERSCRIPT) + ")", typed.strip())
    if any(_is_script(ch) for ch in text):
        return None
    text = unicodedata.normalize("NFKC", text)   # dígitos de ancho completo, espacios raros
    text = _ROOT_WORD.sub("√", text)
    text = text.replace("×", "*").replace("·", "*").replace("÷", "/").replace("−", "-")
    text = _LEADING_SIGN.sub(r"\1", text)        # "- 1.41" → "-1.41"
    text = _BARE_DOT.sub("0.", text)             # ".333" → "0.333"
    return _normalize_option(text)


def _decimal_places(given_src: str) -> int | None:
    """Decimales de un número decimal escrito ("2.83" → 2); None si no es un decimal literal."""
    match = _DECIMAL.fullmatch(given_src)
    return len(match.group(1)) if match else None


def _terminating_places(value: sympy.Expr) -> int | None:
    """Decimales de un racional con desarrollo finito (12.5 → 1, 100 → 0); None si no lo tiene."""
    if not value.is_Rational:
        return None
    q = int(value.q)
    twos = fives = 0
    while q % 2 == 0:
        q, twos = q // 2, twos + 1
    while q % 5 == 0:
        q, fives = q // 5, fives + 1
    return max(twos, fives) if q == 1 else None


def _equal_typed(expected_src: str, given_src: str, places: int | None):
    """Corrige una respuesta escrita: True, False, UNREADABLE o UNCHECKABLE (control inservible).

    Todo en aritmética exacta (`rationalize` convierte los decimales en racionales):
    - si el valor exacto tiene desarrollo decimal finito (100, 0,25, 12345,678), solo vale igual;
    - si no, un decimal con k decimales vale si `|dado − exacto| ≤ 1/(2·10^k)` (bien redondeado)
      y `≤ 0,5 % · |exacto|`;
    - cualquier otra cosa (fracciones, √, π) vale solo con prueba simbólica: simplify(dif) == 0.
    """
    expected = _safe_parse(expected_src)
    if expected is None or not expected.is_finite:
        return UNCHECKABLE
    given = _safe_parse(given_src)
    if given is None or not given.is_finite:
        return UNREADABLE
    if places is None or not given.is_Rational:
        return sympy.simplify(expected - given) == 0
    # Un racional que quedó sin evaluar (12345.678*(√2+1)*(√2-1)) es igual de exacto: se simplifica.
    expected = sympy.simplify(expected)
    if _terminating_places(expected) is not None:
        return given == expected
    diff = abs(expected - given)
    bound = sympy.Min(sympy.Rational(1, 2 * 10**places), TYPED_REL_TOLERANCE * abs(expected))
    return sympy.Le(diff, bound) is sympy.true


def _typed_accepts(expected_src: str, option: str):
    """Corre en el worker: True si `grade_typed` aceptaría `option` escrita tal cual; False si no
    (distinta o ilegible como respuesta escrita); UNCHECKABLE si la expresión de control no sirve."""
    given_src = _normalize_typed(option)
    if given_src is None or not _is_safe(given_src):
        return False
    result = _equal_typed(expected_src, given_src, _decimal_places(given_src))
    if result == UNCHECKABLE:
        return UNCHECKABLE
    return result is True


def substitute(expr: str, values: dict[str, int]) -> str | None:
    """Cada parámetro suelto de la expresión pasa a `(<entero>)`, como texto.

    None si la expresión tiene una palabra que no es un parámetro ni un nombre de la lista blanca.
    El resultado no se evalúa acá: entra a `_safe_parse` tal cual, así que la lista blanca y
    `_structure_ok` deciden como con cualquier `check_expr` (`2a` → `2(3)` se rechaza).
    """
    if not isinstance(expr, str):
        return None
    if any(word not in values and word not in _ALLOWED_NAMES for word in re.findall(r"[A-Za-z]+", expr)):
        return None
    return _PARAM_TOKEN.sub(
        lambda m: f"({int(values[m.group(1)])})" if m.group(1) in values else m.group(0), expr)


def _arithmetic_node_ok(node: ast.AST, names: set[str], in_power: bool) -> bool:
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.Constant):
        return type(node.value) in (int, float)
    if isinstance(node, ast.UnaryOp):
        return isinstance(node.op, ast.USub) and _arithmetic_node_ok(node.operand, names, in_power)
    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Pow):
            exponent = node.right
            return (not in_power and isinstance(exponent, ast.Constant) and type(exponent.value) is int
                    and 1 <= exponent.value <= MAX_VARIANT_EXPONENT
                    and _arithmetic_node_ok(node.left, names, True))
        return (isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div))
                and _arithmetic_node_ok(node.left, names, in_power)
                and _arithmetic_node_ok(node.right, names, in_power))
    return False


def _expr_tree(expr: str) -> ast.AST | None:
    if not isinstance(expr, str) or not expr.strip() or len(expr) > MAX_EXPR_LEN:
        return None
    try:
        return ast.parse(expr.strip().replace("^", "**"), mode="eval").body
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None


def arithmetic_ok(expr: str, names) -> bool:
    """¿La cuenta de una plantilla usa solo aritmética que se verifica con certeza?

    Parámetros, números, + − * /, menos unario, paréntesis y potencias con un exponente fijo de
    1 a `MAX_VARIANT_EXPONENT`, sin potencias anidadas. Nada de funciones (sqrt esconde un valor
    absoluto o una rama), constantes como pi, ni parámetros en el exponente. Es un chequeo
    estático, sin evaluar; además la expresión sigue pasando por la lista blanca de siempre.
    """
    names = set(names)
    tree = _expr_tree(expr)
    if tree is None or not set(re.findall(r"[A-Za-z]+", expr)) <= names:
        return False
    return _arithmetic_node_ok(tree, names, False)


def fixed_numbers(expr: str) -> set:
    """Los números escritos en la cuenta, sin contar los exponentes (`2*c + b` → {2})."""
    tree = _expr_tree(expr)
    if tree is None:
        return set()
    exponents = {id(n.right) for n in ast.walk(tree) if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Pow)}
    return {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and id(n) not in exponents}


def has_power(exprs) -> bool:
    return any("**" in e or "^" in e for e in exprs)


def format_value(value: sympy.Rational, fractions: bool) -> str:
    """El texto de un valor exacto en una opción: `12`, `12,5` o `1/3`, con el signo `−`.

    Con `fractions` en False, un desarrollo decimal finito va con coma y sin ceros al final;
    si no es finito, o con `fractions` en True, va como `p/q`.
    """
    p, q = int(value.p), int(value.q)
    sign = _MINUS if p < 0 else ""
    p = abs(p)
    if q == 1:
        return f"{sign}{p}"
    places = _terminating_places(value)
    if fractions or places is None:
        return f"{sign}{p}/{q}"
    digits = p * 10**places // q
    whole, frac = divmod(digits, 10**places)
    return f"{sign}{whole}," + str(frac).zfill(places).rstrip("0")


def _value_ok(value) -> bool:
    """¿Es un racional finito dentro de los topes? (`zoo`, `nan`, √2 y π no lo son)."""
    return (isinstance(value, sympy.Rational) and abs(int(value.p)) <= MAX_ABS_VALUE * int(value.q)
            and int(value.q) <= MAX_DENOMINATOR)


def _sign(value: sympy.Rational) -> int:
    return int(sympy.sign(value))


def _evaluate(exprs: list[str], values: dict[str, int]) -> list | None:
    """Cada expresión con estos valores; None si alguna no se puede leer."""
    out = []
    for expr in exprs:
        src = substitute(expr, values)
        parsed = _safe_parse(src) if src is not None else None
        if parsed is None:
            return None
        out.append(parsed)
    return out


def _same_text(a: str, b: str) -> bool:
    """Iguales sin mirar espacios ni mayúsculas (como el enunciado en `variants.template_problems`)."""
    return re.sub(r"\s+", " ", a).strip().casefold() == re.sub(r"\s+", " ", b).strip().casefold()


def _key_depends_on_each_param(answer_expr: str, params: list[dict], original: dict[str, int],
                               key) -> bool:
    """¿Cambiar un solo parámetro (desde los valores originales) cambia la clave, para cada uno?

    Un parámetro que se cancela (`2*c + b + a - a`) mostraría en el enunciado un número que no
    entra en la cuenta: la clave de la variante no correspondería a su enunciado.
    """
    for p in params:
        name = p["name"]
        changed = False
        for value in range(int(p["low"]), int(p["high"]) + 1):
            if value == original[name]:
                continue
            other = _evaluate([answer_expr], {**original, name: value})
            if other is not None and _value_ok(other[0]) and other[0] != key:
                changed = True
                break
        if not changed:
            return False
    return True


def _texts_reproduce_original(payload: dict, original: dict[str, int], key, fractions: bool) -> bool:
    """¿La explicación y el error común de la plantilla, con los valores originales, dan los de la
    pregunta? Solo pueden usar los parámetros y la respuesta: no hay pasos intermedios que se
    puedan calcular mal."""
    from estudia.generation.variants import ANSWER_PLACEHOLDER, render   # perezoso: variants importa este módulo

    texts = {name: str(value) for name, value in original.items()}
    texts[ANSWER_PLACEHOLDER] = format_value(key, fractions)
    for template_key, original_key in (("explanation", "original_explanation"),
                                       ("common_error", "original_common_error")):
        rendered = render(payload[template_key], texts)
        if rendered is None or not _same_text(rendered, payload[original_key]):
            return False
    return True


# Números escritos con palabras en castellano (palabras enteras, sin mirar mayúsculas ni
# tildes): cardinales, fracciones y múltiplos, y ordinales. A propósito quedan afuera «un»,
# «una» y «uno» (casi toda la prosa los tiene) y «primero» («primero se suma»); todo lo demás
# falla cerrado, también «cuarto», que en la prosa suele ser un ordinal pero puede ser 1/4.
_NUMBER_WORDS = re.compile(
    r"\b(?:"
    r"cero|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez"
    r"|once|doce|trece|catorce|quince|dieci\w+|veinte|veinti\w*"
    r"|treinta|cuarenta|cincuenta|sesenta|setenta|ochenta|noventa"
    r"|cien|cientos?|quinient[oa]s|(?:dos|tres|cuatro|seis|sete|siete|ocho|nove|nueve)cient[oa]s"
    r"|mil|miles|millon|millones"
    r"|medi[oa]s?|mitad(?:es)?|tercios?|dobles?|triples?|cuadruples?|quintuples?|docenas?|decenas?|centenas?|billon|billones"
    r"|cuart[oa]s?|segund[oa]s?|tercer[oa]?s?|quint[oa]s?|sext[oa]s?|sep?tim[oa]s?|octav[oa]s?"
    r"|noven[oa]s?|decim[oa]s?"
    r")(?:e?s)?\b")


# Números romanos en mayúsculas, como palabra entera de dos letras o más («XIV», «MCM»). Una
# sola letra («I», «V») queda afuera: es más seguido una fórmula o una sigla que un número.
_ROMAN = re.compile(r"(?<![^\W\d_])[IVXLCDM]{2,}(?![^\W\d_])")


def _has_number_word(text: str) -> bool:
    if _ROMAN.search(text):
        return True
    plain = "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))
    return bool(_NUMBER_WORDS.search(plain.casefold()))


def _literal_has_number(text: str) -> bool:
    """¿Alguna parte literal (fuera de `{…}`) tiene un número: un carácter numérico de cualquier
    escritura (dígitos, ², ½, １, ٣…) o un número escrito con palabras (`_NUMBER_WORDS`)?
    Ante un texto que no se puede leer, sí."""
    try:
        parts = list(string.Formatter().parse(text))
    except ValueError:
        return True
    return any(any(unicodedata.category(ch).startswith("N") for ch in literal)
               or _has_number_word(literal) for literal, _, _, _ in parts)


def _spanish_list(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " y " + items[-1]


def variant_line(names: list[str], values: dict[str, int], answer_text: str) -> str:
    """La explicación fija de una variante: «Con a = 7, b = 4 y c = 5, la respuesta es 39.»"""
    pairs = [f"{name} = {values[name]}" for name in names]
    return f"Con {_spanish_list(pairs)}, la respuesta es {answer_text}."


def variant_texts(explanation: str, common_error: str, names: list[str], values: dict[str, int],
                  answer_text: str) -> tuple[str, str]:
    """La explicación y el error común de una variante (no de la original).

    Un número escrito en la plantilla, o un parámetro donde había un resultado intermedio que
    coincidía, quedaría mal con otros valores: nada de eso llega al estudiante.
    - Explicación: siempre `variant_line`; la plantilla se agrega después solo si no tiene
      ningún marcador y no tiene números. Ni siquiera {respuesta}: podría estar donde había otro
      número igual a la respuesta («x = {respuesta}» con f(4) = 4), y la línea ya la dice.
    - Error común: solo si es prosa pura, sin números ni marcadores; si no, vacío.
    """
    from estudia.generation.variants import _fields, _invisible   # perezoso: variants importa este módulo

    line = variant_line(names, values, answer_text)
    out_explanation = line
    fields = _fields(explanation)
    if fields == [] and not _literal_has_number(explanation) and not _invisible(explanation):
        if explanation.strip():
            out_explanation = f"{line} {explanation.strip()}"
    fields = _fields(common_error)
    keep = fields == [] and not _literal_has_number(common_error) and not _invisible(common_error)
    return out_explanation, (common_error if keep else "")


def _variant_values(payload: dict) -> list[dict] | str:
    """Corre en el worker: las variantes de una plantilla, o UNCHECKABLE si no reproduce la original.

    La plantilla se rechaza (UNCHECKABLE) si con los valores originales no da la clave, cada
    distractor, la explicación y el error común de la pregunta; si alguna cuenta se sale de
    `arithmetic_ok`; si algún parámetro no entra en la clave o la clave no depende de él; o si
    los originales se prestan a coincidencias (repetidos, 0 o 1, 2 con una potencia, o iguales a
    un número fijo de la clave).

    Una tupla de valores se descarta ante cualquier duda: un valor que no es un racional finito
    dentro de los topes (incluye la división por cero), un valor que cambia de signo o se hace
    cero respecto del original, valores o textos repetidos, un texto de la clave que no vuelve a
    dar la clave, o un distractor que la regla de la respuesta escrita aceptaría.

    Cada variante lleva además su explicación y su error común (`variant_texts`); quien llama
    los vuelve a armar y, si no coinciden, usa solo la línea fija.
    """
    params = payload["params"]
    names = [p["name"] for p in params]
    answer_expr = payload["answer_expr"]
    distractor_exprs = list(payload["distractor_exprs"])
    options = payload["original_options"]
    correct_index = payload["correct_index"]
    fractions = bool(payload["fractions"])
    original = dict(zip(names, (int(v) for v in payload["original"]), strict=True))
    distractors = [o for i, o in enumerate(options) if i != correct_index]
    if len(distractors) != len(distractor_exprs) or not 0 <= correct_index < len(options):
        return UNCHECKABLE
    # Originales que se prestan a coincidencias (a + c = a·c con 2 y 2; a·1 = a).
    if len(set(original.values())) != len(original) or any(v in (0, 1) for v in original.values()):
        return UNCHECKABLE
    # Cada parámetro del enunciado tiene que entrar en la clave.
    if not set(names) <= set(re.findall(r"[A-Za-z]+", answer_expr)):
        return UNCHECKABLE
    # Solo aritmética verificable, y nada de coincidencias en los originales.
    exprs = [answer_expr, *distractor_exprs]
    if not all(arithmetic_ok(e, names) for e in exprs):
        return UNCHECKABLE
    if has_power(exprs) and 2 in original.values():
        return UNCHECKABLE
    if any(v in fixed_numbers(answer_expr) for v in original.values()):
        return UNCHECKABLE

    # 1. Con los valores originales la plantilla tiene que dar la clave y cada distractor, en orden.
    src = substitute(answer_expr, original)
    if src is None or _equal(src, payload["check_expr"].strip()) is not True:
        return UNCHECKABLE
    for expr, option in zip(distractor_exprs, distractors):
        src = substitute(expr, original)
        if src is None or _equal(src, _normalize_option(option)) is not True:
            return UNCHECKABLE
    base = _evaluate(exprs, original)
    if base is None or not all(_value_ok(v) for v in base):
        return UNCHECKABLE
    if not _key_depends_on_each_param(answer_expr, params, original, base[0]):
        return UNCHECKABLE
    if not _texts_reproduce_original(payload, original, base[0], fractions):
        return UNCHECKABLE
    base_signs = [_sign(v) for v in base]

    # 2. Sorteos reproducibles con la semilla de la plantilla.
    rng = random.Random(payload["seed"])
    limit = int(payload["limit"])
    seen = {tuple(original[n] for n in names)}
    variants: list[dict] = []
    for _ in range(MAX_VARIANT_TRIES):
        if len(variants) >= limit:
            break
        draw = tuple(rng.randint(int(p["low"]), int(p["high"])) for p in params)
        if draw in seen:
            continue
        seen.add(draw)
        values = dict(zip(names, draw))
        results = _evaluate(exprs, values)
        if results is None or not all(_value_ok(v) for v in results):
            continue
        if [_sign(v) for v in results] != base_signs:
            continue
        options_values = results
        if len(set(options_values)) != len(options_values):
            continue
        texts = [format_value(v, fractions) for v in options_values]
        if len(set(texts)) != len(texts):
            continue
        answer_src = sympy.sstr(results[0])
        # El texto de la clave vuelve a dar la clave, tanto en la opción múltiple como escrito.
        if (_equal(answer_src, _normalize_option(texts[0])) is not True
                or _typed_accepts(answer_src, texts[0]) is not True):
            continue
        # Ningún distractor puede valer como respuesta escrita (por ejemplo 0,333 con clave 1/3).
        if any(_typed_accepts(answer_src, text) is not False for text in texts[1:]):
            continue
        explanation, common_error = variant_texts(
            payload["explanation"], payload["common_error"], names, values, texts[0])
        variants.append({
            "values": values,
            "answer_src": answer_src,
            "answer_text": texts[0],
            "distractor_texts": texts[1:],
            "explanation": explanation,
            "common_error": common_error,
        })
    return variants


# Worker único y reutilizable (se crea de forma perezosa). Quien arranque la app
# tiene que usar `if __name__ == "__main__":` y `multiprocessing.freeze_support()`
# (necesario en Windows / ejecutables congelados; se resuelve en la Tarea 9).
STARTUP_TIMEOUT_S = 20.0

_lock = threading.Lock()
_proc = None
_conn = None
_job_counter = 0
# Si el worker no arrancó o se pasó del tope, los chequeos que pueden esperar (`typed_accepts_option`)
# no lo vuelven a probar hasta este instante (time.monotonic); corregir una respuesta sí.
UNAVAILABLE_BACKOFF_S = 60.0
_unavailable_until = 0.0


def _mark_unavailable() -> None:
    global _unavailable_until
    _unavailable_until = time.monotonic() + UNAVAILABLE_BACKOFF_S


def _start_method() -> str:
    """forkserver donde existe (POSIX); spawn en el resto (Windows)."""
    if "forkserver" in multiprocessing.get_all_start_methods():
        return "forkserver"
    return "spawn"


_TEST_HANG = "test_hang"   # gancho de tests: el worker se cuelga con este trabajo


def _worker_main(conn) -> None:
    """Bucle del worker: recibe (id, decimales, esperado, dado) y responde (id, resultado).

    `decimales` "option": verificación de opciones (`_equal`: True, False o None). `TYPED_OPTION`:
    ¿la regla escrita acepta el texto de la opción? (`_typed_accepts`). Si no, respuesta
    escrita (`_equal_typed`: True, False, UNREADABLE, UNCHECKABLE o None) con esos decimales (None: no es decimal).
    `VARIANTS`: `esperado` es el JSON de una plantilla (`_variant_values`: lista o UNCHECKABLE).
    """
    conn.send("ready")
    while True:
        try:
            job = conn.recv()
        except (EOFError, OSError):
            return
        if job is None:
            return
        job_id, places, expected_src, given_src = job
        if places == _TEST_HANG:
            time.sleep(3600)   # solo para tests: un trabajo colgado de verdad, dentro del subproceso
        try:
            if places == "option":
                answer = _equal(expected_src, given_src)
            elif places == TYPED_OPTION:
                answer = _typed_accepts(expected_src, given_src)
            elif places == VARIANTS:
                try:
                    answer = _variant_values(json.loads(expected_src))
                except Exception:
                    answer = UNCHECKABLE   # payload roto: la plantilla no sirve (el worker anda)
            else:
                answer = _equal_typed(expected_src, given_src, places)
        except BaseException:
            answer = None
        conn.send((job_id, answer))


def _stop_worker() -> None:
    """Mata el worker (si hay) y lo descarta; el próximo llamado crea uno nuevo."""
    global _proc, _conn
    proc, conn = _proc, _conn
    _proc = _conn = None
    if conn is not None:
        try:
            conn.close()
        except OSError:
            pass
    if proc is not None:
        if proc.is_alive():
            proc.kill()
        proc.join()


def _start_worker() -> bool:
    global _proc, _conn
    method = _start_method()
    ctx = multiprocessing.get_context(method)
    if method == "forkserver":
        ctx.set_forkserver_preload(["sympy"])  # solo acelera; el worker importa igual
    parent, child = ctx.Pipe()
    proc = ctx.Process(target=_worker_main, args=(child,), daemon=True)
    proc.start()
    child.close()
    _proc, _conn = proc, parent
    try:
        if parent.poll(STARTUP_TIMEOUT_S) and parent.recv() == "ready":
            return True
    except (EOFError, OSError):
        pass
    _stop_worker()
    return False


def _equal_with_timeout(expected_src: str, given_src: str, places: int | str | None = "option",
                        timeout: float | None = None):
    """Resultado del worker; None si no arrancó, se pasó del tope o se cayó.

    `timeout` es el tope del trabajo (None: `EVAL_TIMEOUT_S`, leído en cada llamada). Una lista
    solo vale como respuesta en el modo `VARIANTS`.
    """
    global _job_counter
    with _lock:
        if _proc is not None and not _proc.is_alive():
            _stop_worker()
        if _conn is None and not _start_worker():
            _mark_unavailable()
            return None
        _job_counter += 1
        job_id = _job_counter
        answered = False
        try:
            _conn.send((job_id, places, expected_src, given_src))
            # El tope aplica al trabajo, no al arranque del worker.
            if _conn.poll(EVAL_TIMEOUT_S if timeout is None else timeout):
                reply_id, answer = _conn.recv()
                answered = reply_id == job_id
                if not answered:
                    return None
                if places == VARIANTS and isinstance(answer, list):
                    return answer
                if answer is True or answer is False or answer in (UNREADABLE, UNCHECKABLE):
                    return answer
            return None
        except (EOFError, OSError):
            return None
        finally:
            # Sin respuesta de ESTE trabajo (timeout, error, Ctrl+C) el worker se
            # descarta: no puede quedar una respuesta vieja en el pipe.
            if not answered:
                _mark_unavailable()
                _stop_worker()


def _shutdown() -> None:
    with _lock:
        _stop_worker()


atexit.register(_shutdown)


def check_math_tristate(check_expr: str, option: str) -> bool | None:
    """¿`check_expr` vale exactamente lo mismo que la opción?

    True: probado igual. False: probado distinto (las dos se leyeron como números).
    None: no se sabe — `check_expr` u opción que no se pueden leer, tiempo agotado
    o error del worker.
    Quien decide qué mostrar tiene que tratar None como "no verificado".
    """
    try:
        if not isinstance(check_expr, str) or not isinstance(option, str):
            return None
        expected_src = check_expr.strip()
        if not _is_safe(expected_src):
            return None
        given_src = _normalize_option(option)
        if not _is_safe(given_src):
            return None
        return _equal_with_timeout(expected_src, given_src)
    except Exception:
        return None


def check_math(check_expr: str, option: str) -> bool:
    """True solo si `check_expr` vale exactamente lo mismo que la opción."""
    return check_math_tristate(check_expr, option) is True


def typed_checkable(check_expr: str | None) -> bool:
    """¿La expresión de control sirve para corregir respuestas escritas?"""
    return isinstance(check_expr, str) and _is_safe(check_expr.strip())


def grade_typed(check_expr: str, typed: str) -> bool | None:
    """¿Lo que escribió el estudiante vale lo mismo que `check_expr`?

    Acepta las formas de una opción ("2√2", "2*sqrt(2)", "1/2", "0,5"), "raiz"/"raíz" ("2 raiz 2",
    "raíz de 8"), "²" y "³", y dígitos de ancho completo (NFKC). Un decimal se corrige con
    tolerancia exacta (ver `_equal_typed`); el resto necesita igualdad simbólica.
    True: correcta. False: leída y distinta. None: no se pudo leer; quien llama pide reescribirla.
    `UncheckableExpr` si la expresión de control no sirve; `GradingUnavailable` si el worker no
    respondió a tiempo o se cayó.
    Corre en el mismo worker con tope de tiempo que `check_math_tristate`.
    """
    if not typed_checkable(check_expr):
        raise UncheckableExpr("expresión de control ilegible")
    if not isinstance(typed, str):
        return None
    given_src = _normalize_typed(typed)
    if given_src is None or not _is_safe(given_src):
        return None
    result = _equal_with_timeout(check_expr.strip(), given_src, _decimal_places(given_src))
    if result == UNREADABLE:
        return None
    if result == UNCHECKABLE:
        raise UncheckableExpr("expresión de control inservible")
    if result is None:
        raise GradingUnavailable("no se pudo corregir")
    return result


def typed_accepts_option(check_expr: str | None, option: str) -> bool | None:
    """¿La regla de la respuesta escrita (`grade_typed`) aceptaría como correcta esta opción?

    Sirve para encontrar preguntas que se contradicen: un distractor que la opción múltiple da por
    mal ("1.41" con `check_expr` "sqrt(2)") y la escrita daría por bien.
    True: la aceptaría. False: no (distinta, o ilegible como respuesta escrita).
    None: no se sabe (expresión de control inservible, tiempo agotado o worker caído).
    Corre en el mismo worker con tope de tiempo. Si el worker falló en los últimos
    `UNAVAILABLE_BACKOFF_S` segundos devuelve None enseguida, sin volver a probarlo.
    """
    if not typed_checkable(check_expr) or not isinstance(option, str):
        return None
    if time.monotonic() < _unavailable_until:
        return None   # el worker falló hace poco: no se espera otra vez
    result = _equal_with_timeout(check_expr.strip(), option, TYPED_OPTION)
    return result if isinstance(result, bool) else None


def worker_available() -> bool:
    """False si el worker falló (no arrancó o se pasó del tope) hace menos de `UNAVAILABLE_BACKOFF_S`."""
    return time.monotonic() >= _unavailable_until


def compute_variant_values(payload: dict) -> list[dict] | None:
    """Las variantes de una plantilla, calculadas en el worker con tope `VARIANTS_TIMEOUT_S`.

    Lista (puede estar vacía) con `values`, `answer_src`, `answer_text` y `distractor_texts` de
    cada variante. None si la plantilla no sirve (no reproduce la original o
    el payload está roto). `WorkerUnavailable` si el worker no anda: no arrancó, se cayó, se
    pasó del tope, o falló hace poco (no se vuelve a probar durante `UNAVAILABLE_BACKOFF_S`,
    como `typed_accepts_option`). Ninguno de los dos casos da variantes, y no se reintenta.
    """
    try:
        payload_src = json.dumps(payload)
    except (TypeError, ValueError):
        return None
    if not worker_available():
        raise WorkerUnavailable("el worker de SymPy falló hace poco")
    result = _equal_with_timeout(payload_src, "", VARIANTS, timeout=VARIANTS_TIMEOUT_S)
    if isinstance(result, list):
        return result
    if result is None:
        raise WorkerUnavailable("el worker de SymPy no respondió")
    return None
