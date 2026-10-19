"""Presupuesto diario de pedidos a Gemini por función, contado en el día de la cuota.

El día de la cuota de Google es el del Pacífico (America/Los_Angeles): cambia a su
medianoche, no a la de Buenos Aires. El uso va en kv como `ai_used:<función>:<día>`.
"""
from __future__ import annotations

import datetime as dt
import math
import sqlite3
from zoneinfo import ZoneInfo

from estudia import config

FUNCTIONS = ("analysis", "blind", "pdf", "photo")
BLIND_BATCH_MAX = 4
QUOTA_TZ = ZoneInfo("America/Los_Angeles")


def quota_day(now: float) -> dt.date:
    return dt.datetime.fromtimestamp(now, QUOTA_TZ).date()


def _check(function: str) -> None:
    if function not in FUNCTIONS:
        raise ValueError(f"función de IA desconocida: {function!r}")


def _kv(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _used_key(function: str, now: float) -> str:
    return f"ai_used:{function}:{quota_day(now).isoformat()}"


def daily_budget(conn: sqlite3.Connection, function: str) -> int:
    _check(function)
    raw = _kv(conn, f"ai_budget:{function}")
    if raw is not None:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return config.DAILY_BUDGET[function]


def used_today(conn: sqlite3.Connection, function: str, now: float) -> int:
    _check(function)
    raw = _kv(conn, _used_key(function, now))
    try:
        return max(0, int(raw)) if raw is not None else 0
    except ValueError:
        return 0


def remaining(conn: sqlite3.Connection, function: str, now: float) -> int:
    return max(0, daily_budget(conn, function) - used_today(conn, function, now))


def add_used(conn: sqlite3.Connection, function: str, count: int, now: float) -> None:
    """Suma pedidos al día de la cuota. No hace commit."""
    _check(function)
    total = used_today(conn, function, now) + count
    conn.execute(
        "INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)",
        (_used_key(function, now), str(total)),
    )


def record_usage(conn: sqlite3.Connection, provider, now: float) -> None:
    """Pasa los pedidos que contó el proveedor a kv, vacía su contador y hace commit."""
    counts = getattr(provider, "requests", None)
    if counts:
        for function, count in counts.items():
            if count and function in FUNCTIONS:
                add_used(conn, function, count, now)
        provider.requests = {function: 0 for function in counts}
    conn.commit()


def blind_requests_needed(materials: int) -> int:
    return math.ceil(materials / BLIND_BATCH_MAX)


def can_analyze(conn: sqlite3.Connection, now: float, awaiting_blind: int) -> bool:
    """Reserva: no analizar si eso dejaría sin cuota a la pasada a ciegas de lo ya analizado.

    El presupuesto diario es un tope blando: se mira antes de cada análisis, pero un pedido se
    puede partir en varios (un lote a ciegas por modelo) y un formato ilegible se reintenta, así
    que el uso real del día puede pasarse un poco. Pasarse nunca marca nada como hecho sin
    verificar: un material queda `done` solo después de pasar la verificación a ciegas.
    """
    return remaining(conn, "analysis", now) > 0 and _blind_reserved(conn, now, awaiting_blind)


def can_analyze_document(conn: sqlite3.Connection, now: float, awaiting_blind: int) -> bool:
    """Como `can_analyze`, pero un PDF o unas diapositivas gastan del presupuesto "pdf".

    La reserva de la pasada a ciegas es la misma: un documento se verifica igual que un video.
    """
    return remaining(conn, "pdf", now) > 0 and _blind_reserved(conn, now, awaiting_blind)


def _blind_reserved(conn: sqlite3.Connection, now: float, awaiting_blind: int) -> bool:
    """¿Alcanza la pasada a ciegas para lo ya analizado más uno nuevo?"""
    return remaining(conn, "blind", now) >= blind_requests_needed(awaiting_blind + 1)


def prune_usage(conn: sqlite3.Connection, now: float) -> None:
    """Borra el uso de los días que no son el de hoy."""
    today = quota_day(now).isoformat()
    conn.execute(
        "DELETE FROM kv WHERE key LIKE 'ai_used:%' AND key NOT LIKE ?", (f"%:{today}",)
    )
    conn.commit()
