"""Verificación de respuestas: red de seguridad antes de mostrar una pregunta.

`check_expr` viene del LLM, o sea que es entrada no confiable: nunca se ejecuta
código arbitrario. Se valida con una lista blanca de caracteres y nombres antes
de pasarla al parser de SymPy, y se acota el tamaño de las potencias.
"""

from __future__ import annotations

import ast
import atexit
import multiprocessing
import re
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


class GradingUnavailable(RuntimeError):
    """No se pudo corregir ahora (worker caído o tiempo agotado): se puede reintentar."""


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


def _worker_main(conn) -> None:
    """Bucle del worker: recibe (id, decimales, esperado, dado) y responde (id, resultado).

    `decimales` "option": verificación de opciones (`_equal`: True, False o None). `TYPED_OPTION`:
    ¿la regla escrita acepta el texto de la opción? (`_typed_accepts`). Si no, respuesta
    escrita (`_equal_typed`: True, False, UNREADABLE, UNCHECKABLE o None) con esos decimales (None: no es decimal).
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
        try:
            if places == "option":
                answer = _equal(expected_src, given_src)
            elif places == TYPED_OPTION:
                answer = _typed_accepts(expected_src, given_src)
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


def _equal_with_timeout(expected_src: str, given_src: str, places: int | str | None = "option"):
    """Resultado del worker; None si no arrancó, se pasó del tope o se cayó."""
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
            if _conn.poll(EVAL_TIMEOUT_S):
                reply_id, answer = _conn.recv()
                answered = reply_id == job_id
                if answered and (answer is True or answer is False or answer in (UNREADABLE, UNCHECKABLE)):
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
