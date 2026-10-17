"""Textos para el estudiante: notación matemática de pizarrón y nombres de temas.

`student_math` pasa lo que escribe un programador ("x**2", "a*b", "!=", `código`)
a la notación de clase ("x²", "a·b", "≠"). Es determinística e idempotente. No se
usa con `check_expr`, que sigue en sintaxis SymPy.
"""
from __future__ import annotations

import re

_SUP = "⁰¹²³⁴⁵⁶⁷⁸⁹⁻ⁿ"
_TO_SUP = str.maketrans("0123456789-−", "⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁻")
# Marcas de negrita/itálica de Markdown, cambiadas por caracteres de uso privado
# mientras se convierte: así nunca se confunden con potencias ni productos.
_BOLD, _ITALIC = "", ""

_INLINE_CODE = re.compile(r"(`+)([^`\n]+?)\1")
# Una negrita no abre pegada a un operando (eso es "x**2") ni cierra antes de un
# exponente: "**Ejemplo: x**2**" es negrita con una potencia adentro.
_BOLD_RE = re.compile(r"(?<![\w)\]*])\*\*(?=[^\s*])([^\n]+?)(?<=[^\s*])\*\*(?![*\d(])")
_ITALIC_RE = re.compile(r"(?<![\w)\]*])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![\w*(])")

# "**" o "^" detrás de un operando. Con espacios, de los dos lados ("x ** 2").
_POW_OP = r"(?<=[\w)\]])(?:[ \t]+\*\*[ \t]+|\*\*|[ \t]*\^[ \t]*)"
_POW_INT = re.compile(_POW_OP + r"(?:\([ \t]*([-−]?\d+)[ \t]*\)|([-−]?\d+)(?![.,]?\d)|(n)(?!\w))")
_POW_OTHER = re.compile(r"(?<=[\w)\]])(?:[ \t]+\*\*[ \t]+|\*\*)(?=[(\w])")
_SQRT = re.compile(r"\bsqrt[ \t]*\(")
_SYMBOLS = (("!=", "≠"), ("->", "→"), ("<=", "≤"), (">=", "≥"))

# Un operando suelto: un número (con un coeficiente literal, "2x"), una letra con
# subíndice ("x0", "a1") o un paréntesis que cierra; con su exponente si lo tiene.
_NUM = r"\d+(?:[.,]\d+)?"
_LEFT = rf"(?P<left>(?<![\w.,])(?:{_NUM}[A-Za-z]?|[A-Za-z]\d*)[{_SUP}]*|\)[{_SUP}]*)"
_RIGHT = rf"(?:(?:{_NUM}[A-Za-z]?|[A-Za-z]\d*)[{_SUP}]*(?![\w])|[(√π])"
_PRODUCT = re.compile(_LEFT + r"(?P<op>[ \t]*)\*(?P<op2>[ \t]*)(?=" + _RIGHT + ")")
# Resta entre operandos; sin espacios, no entre dos números ("10-12" es un rango).
_MINUS_SPACED = re.compile(_LEFT + r"(?P<op>[ \t]+)-(?P<op2>[ \t]+)(?=" + _RIGHT + ")")
_MINUS_TIGHT = re.compile(_LEFT + r"(?P<op>)-(?P<op2>)(?=" + _RIGHT + ")")
# Menos de un número o una letra: al principio, después de un espacio, un paréntesis
# o un operador ("= -1", "(-3", "x^(-1)").
_MINUS_UNARY = re.compile(
    r"(?:^|(?<=[\s(\[{=,;:+*/<>≤≥≠→·]))-(?=\d|[A-Za-z]\d*[" + _SUP + r"]*(?!\w)|[(√])",
    re.MULTILINE,
)


def _power(match: re.Match) -> str:
    exponent = match.group(1) or match.group(2) or match.group(3)
    return "ⁿ" if exponent == "n" else exponent.translate(_TO_SUP)


def _operator(symbol: str):
    def repl(match: re.Match) -> str:
        return match.group("left") + match.group("op") + symbol + match.group("op2")
    return repl


def _tight_minus(match: re.Match) -> str:
    left = match.group("left")
    after = match.string[match.end():]
    if re.match(r"\d", left) and re.match(r"\d", after) and re.fullmatch(_NUM, left):
        return match.group(0)  # "10-12": rango, no resta
    return left + "−"


def student_math(text: str) -> str:
    """Notación de clase: sin backticks, con superíndices, "·", "≠", "→", "√" y "−"."""
    if not text:
        return text
    text = _INLINE_CODE.sub(r"\2", text)
    text = _BOLD_RE.sub(lambda m: _BOLD + m.group(1) + _BOLD, text)
    text = _ITALIC_RE.sub(lambda m: _ITALIC + m.group(1) + _ITALIC, text)
    text = _POW_INT.sub(_power, text)
    text = _POW_OTHER.sub("^", text)
    text = _SQRT.sub("√(", text)
    for code, symbol in _SYMBOLS:
        text = text.replace(code, symbol)
    text = _PRODUCT.sub(_operator("·"), text)
    text = _MINUS_SPACED.sub(_operator("−"), text)
    text = _MINUS_TIGHT.sub(_tight_minus, text)
    text = _MINUS_UNARY.sub("−", text)
    return text.replace(_BOLD, "**").replace(_ITALIC, "*")


# --- Nombres de temas ---------------------------------------------------------------

# Palabras de una letra que en un título van en minúscula ("Rectas Y Planos").
_SMALL_WORDS = set("YEOUA")


def _alpha_runs(word: str) -> list[str]:
    return re.findall(r"[^\W\d_]+", word)


def _is_capitalized(word: str) -> bool:
    """"Función", "Lineal-Afín", "(Parte": cada tramo de letras empieza en mayúscula y sigue en minúscula."""
    runs = [r for r in _alpha_runs(word) if len(r) > 1]
    return bool(runs) and all(r[0].isupper() and r[1:].islower() for r in runs)


def _lower_first_letters(word: str) -> str:
    return re.sub(r"[^\W\d_]+", lambda m: m.group(0)[0].lower() + m.group(0)[1:], word)


def sentence_case(name: str) -> str:
    """Nombre de tema con mayúscula solo al principio.

    "Definición y Gráfica de la Función Cuadrática" -> "Definición y gráfica de la
    función cuadrática". Las siglas en mayúsculas ("UTN", "R", "II") y las palabras
    con mayúsculas en el medio ("McLaurin") quedan. Si el nombre ya está en
    minúsculas (alguna palabra de 4 letras o más después de la primera arranca en
    minúscula), las mayúsculas que tiene son nombres propios y también quedan.
    """
    words = name.split()
    if not words:
        return ""
    rest = words[1:]
    already_sentence = any(
        len(runs := _alpha_runs(w)) and len("".join(runs)) >= 4 and runs[0][0].islower()
        for w in rest
    )
    if not already_sentence:
        rest = [
            w.lower() if w in _SMALL_WORDS
            else _lower_first_letters(w) if _is_capitalized(w)
            else w
            for w in rest
        ]
    first = re.sub(r"[^\W\d_]", lambda m: m.group(0).upper(), words[0], count=1)
    return " ".join([first, *rest])
