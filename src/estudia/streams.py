"""Salida estándar de los .exe sin consola.

Con PyInstaller y `console=False`, en Windows `sys.stdout` y `sys.stderr` son None:
cualquier `print` o el logging de uvicorn (que pregunta `sys.stdout.isatty()`)
revienta al arrancar. Acá se reemplazan por un archivo de log o por `os.devnull`.
"""
import os
import sys
from pathlib import Path

MAX_LOG_BYTES = 1024 * 1024


def _rotate(path: Path) -> None:
    """Pasado el tope, el log actual queda como `.1` (pisando el anterior) y se empieza otro."""
    try:
        if path.stat().st_size > MAX_LOG_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        pass


def redirect_missing_streams(log_path: Path | None) -> None:
    """Si falta stdout o stderr, los manda a `log_path` (o a `os.devnull` si es None).

    Los que existen (consola o salida redirigida) no se tocan.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    stream = None
    if log_path is not None:
        try:
            _rotate(log_path)
            stream = open(log_path, "a", encoding="utf-8", buffering=1)
        except OSError:
            stream = None
    if stream is None:
        stream = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
