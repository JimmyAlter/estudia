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


# Worker único y reutilizable (se crea de forma perezosa). Quien arranque la app
# tiene que usar `if __name__ == "__main__":` y `multiprocessing.freeze_support()`
# (necesario en Windows / ejecutables congelados; se resuelve en la Tarea 9).
STARTUP_TIMEOUT_S = 20.0

_lock = threading.Lock()
_proc = None
_conn = None
_job_counter = 0


def _start_method() -> str:
    """forkserver donde existe (POSIX); spawn en el resto (Windows)."""
    if "forkserver" in multiprocessing.get_all_start_methods():
        return "forkserver"
    return "spawn"


def _worker_main(conn) -> None:
    """Bucle del worker: recibe (esperado, dado), responde True, False o None."""
    conn.send("ready")
    while True:
        try:
            job = conn.recv()
        except (EOFError, OSError):
            return
        if job is None:
            return
        job_id, expected_src, given_src = job
        try:
            answer = _equal(expected_src, given_src)
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


def _equal_with_timeout(expected_src: str, given_src: str) -> bool | None:
    """Resultado del worker; None si no arrancó, se pasó del tope o se cayó."""
    global _job_counter
    with _lock:
        if _proc is not None and not _proc.is_alive():
            _stop_worker()
        if _conn is None and not _start_worker():
            return None
        _job_counter += 1
        job_id = _job_counter
        answered = False
        try:
            _conn.send((job_id, expected_src, given_src))
            # El tope aplica al trabajo, no al arranque del worker.
            if _conn.poll(EVAL_TIMEOUT_S):
                reply_id, answer = _conn.recv()
                answered = reply_id == job_id
                if answered and answer in (True, False):
                    return answer
            return None
        except (EOFError, OSError):
            return None
        finally:
            # Sin respuesta de ESTE trabajo (timeout, error, Ctrl+C) el worker se
            # descarta: no puede quedar una respuesta vieja en el pipe.
            if not answered:
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
