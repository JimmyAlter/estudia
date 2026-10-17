"""Instancia única de Estudia: detectar una corrida viva, tomar el lock y modo sin escritorio."""
import os
import sys
from collections.abc import Callable
from pathlib import Path

from estudia.server.app import HOST


def running_instance(port_file: Path, probe: Callable[[str], bool]) -> str | None:
    """URL base de la instancia viva, o None si no hay archivo de puerto o quedó viejo."""
    try:
        port = int(port_file.read_text("utf-8").strip())
    except (OSError, ValueError):
        return None
    base = f"http://{HOST}:{port}"
    return base if probe(base) else None


def headless() -> bool:
    return os.environ.get("ESTUDIA_HEADLESS") == "1"


def opens_browser() -> bool:
    """False sin escritorio o con ESTUDIA_NO_BROWSER=1 (la bandeja sigue: lo usa la prueba de CI)."""
    return not headless() and os.environ.get("ESTUDIA_NO_BROWSER") != "1"


def acquire_lock(path: Path) -> object | None:
    """Lock exclusivo de archivo que dura lo que viva el proceso. None si ya está tomado.

    El objeto devuelto hay que guardarlo: al cerrarse (o al morir el proceso) se libera.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+b")
    try:
        if sys.platform == "win32":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f
