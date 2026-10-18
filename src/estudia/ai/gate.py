"""Puerta de edad: con un menor (o sin respuesta todavía) nada sale hacia el proveedor de IA."""
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

AGE_KEY = "age_gate"


class AIBlocked(Exception):
    """La puerta de edad no deja usar la IA.

    La tubería la deja pasar sin marcar `failed` el material (queda pendiente).
    """


def age_is_adult(db_path: Path | None = None, *, timeout_s: float = 30.0) -> bool:
    """True solo si la base dice "adult". Lectura liviana: no aplica esquema ni migraciones.

    Cualquier problema (sin base, bloqueada, sin tabla) cuenta como "no": es el lado seguro.
    """
    if db_path is None:
        from estudia.config import data_dir

        db_path = data_dir() / "estudia.db"
    try:
        conn = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True, timeout=timeout_s)
    except Exception:  # noqa: BLE001 - cualquier error (ruta rota, sin permiso) = no adulto
        return False
    try:
        row = conn.execute("SELECT value FROM kv WHERE key=?", (AGE_KEY,)).fetchone()
        return row is not None and row[0] == "adult"
    except Exception:  # noqa: BLE001
        return False
    finally:
        conn.close()


class GatedProvider:
    """Envuelve a un proveedor y revisa la puerta antes de CADA llamada.

    Así, cambiar a "menor" corta incluso una corrida que ya empezó.
    """

    def __init__(self, provider: Any, allowed: Callable[[], bool]):
        object.__setattr__(self, "_provider", provider)
        object.__setattr__(self, "_allowed", allowed)

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._provider, name)
        if not callable(value):
            return value

        def guarded(*args: Any, **kwargs: Any) -> Any:
            if not self._allowed():
                raise AIBlocked("la IA no está habilitada")
            return value(*args, **kwargs)

        return guarded

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._provider, name, value)
