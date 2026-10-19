"""Correr un trabajo riesgoso (parsear un PDF o un .pptx de afuera) en un proceso aparte.

Cada llamado arranca un hijo nuevo con tope de tiempo y, en POSIX, de memoria. Si se pasa
del tiempo se mata; si se cae, el proceso principal sigue de pie. Igual que el worker de
SymPy (`generation/verify.py`): forkserver en POSIX y spawn en Windows, así que también
anda congelado con PyInstaller (`multiprocessing.freeze_support()` en `__main__`).
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import sys
import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")

SANDBOX_MEMORY_MB = 1536
PROCESS_NAME = "estudia-sandbox"
_KILL_GRACE_S = 5
# A lo sumo tantos hijos a la vez; esperar un lugar cuenta dentro de `timeout_s`.
MAX_CONCURRENT = 2
_slots = threading.BoundedSemaphore(MAX_CONCURRENT)

logger = logging.getLogger(__name__)


class SandboxTimeout(Exception):
    """El trabajo se pasó del tiempo y el hijo se mató."""


class SandboxBusy(SandboxTimeout):
    """No hubo lugar libre a tiempo: no corrió ningún hijo, el archivo no tiene la culpa."""


class SandboxFailed(Exception):
    """El hijo falló o murió. El mensaje es solo el nombre del tipo de la excepción."""


def _limit_memory(memory_mb: int) -> None:
    if sys.platform == "win32":
        return  # en Windows alcanza con el tope de tiempo
    import resource

    limit = memory_mb * 1024 * 1024
    _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    if hard != resource.RLIM_INFINITY:
        limit = min(limit, hard)  # no se puede subir el tope duro del sistema
    try:
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ValueError, OSError):
        logger.warning("No se pudo fijar el tope de memoria del proceso de lectura")
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))  # sin volcados si se cae
    except (ValueError, OSError):
        pass


def _child_main(conn, target: Callable[..., Any], args: tuple, memory_mb: int) -> None:
    try:
        _limit_memory(memory_mb)
        reply = ("ok", target(*args))
    except BaseException as exc:  # noqa: BLE001 (todo error del hijo vuelve como su tipo)
        reply = ("error", type(exc).__name__)
    try:
        conn.send(reply)
    except BaseException as exc:  # noqa: BLE001 (p. ej. un resultado que no se puede serializar)
        try:
            conn.send(("error", type(exc).__name__))
        except BaseException:  # noqa: BLE001
            pass
    finally:
        conn.close()


def _stop(proc, *, answered: bool) -> None:
    if answered:
        proc.join(1)  # contestó: termina solo
    if proc.is_alive():
        proc.terminate()
        proc.join(_KILL_GRACE_S)
        if proc.is_alive():
            proc.kill()
    proc.join()


def run_limited(
    target: Callable[..., T],
    *args: Any,
    timeout_s: float,
    memory_mb: int = SANDBOX_MEMORY_MB,
) -> T:
    """`target(*args)` en un hijo. `target` tiene que ser una función de módulo (serializable)."""
    # Import local: el hijo importa este módulo y no tiene por qué cargar SymPy.
    from estudia.generation.verify import _start_method

    deadline = time.monotonic() + timeout_s
    if not _slots.acquire(timeout=max(timeout_s, 0)):
        raise SandboxBusy()
    try:
        return _run_child(_start_method(), target, args, deadline, memory_mb)
    finally:
        _slots.release()


def _run_child(method: str, target: Callable[..., Any], args: tuple, deadline: float, memory_mb: int):
    ctx = multiprocessing.get_context(method)
    parent, child = ctx.Pipe(duplex=False)
    proc = ctx.Process(
        target=_child_main,
        args=(child, target, args, memory_mb),
        name=PROCESS_NAME,
        daemon=True,
    )
    try:
        proc.start()
    except BaseException:
        parent.close()
        child.close()
        raise
    child.close()
    answered = False
    try:
        try:
            ready = parent.poll(max(deadline - time.monotonic(), 0))
        except (EOFError, OSError):
            ready = True
        if not ready:
            raise SandboxTimeout()
        try:
            status, value = parent.recv()
        except (EOFError, OSError):
            raise SandboxFailed("muerto") from None
        answered = True
        if status != "ok":
            raise SandboxFailed(value)
        return value
    finally:
        parent.close()
        _stop(proc, answered=answered)


def _test_sleep(seconds: float) -> None:
    """Gancho de tests: un trabajo que se cuelga de verdad, dentro del hijo."""
    time.sleep(seconds)


def _test_die() -> None:
    """Gancho de tests: el hijo muere sin responder."""
    os._exit(3)
