"""Recordatorios en la PC: examen de mañana, clase por empezar y sesión de estudio de hoy.

`due_reminders` es puro (lee la base y devuelve qué avisar); el hilo de `main()` es
quien manda los avisos y recuerda las claves enviadas en kv `reminders_sent`.
"""
import json
import logging
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import sqlite3
from collections.abc import Callable

from estudia import tray
from estudia.study.schedule import TZ, occurrences

log = logging.getLogger(__name__)

EXAM_EVE_HOUR = 18         # el aviso del examen sale desde las 18:00 locales del día anterior
CLASS_LEAD_S = 15 * 60     # la clase se avisa desde 15 minutos antes
SESSION_WINDOW_S = 10 * 60  # la sesión se avisa al empezar y hasta 10 minutos después
ENROLL_LEAD_DAYS = 3       # la inscripción se avisa desde 3 días antes del cierre
ENROLL_FROM_HOUR = 9       # ...a partir de las 9:00 locales de ese día
KEEP_SENT_S = 5 * 24 * 3600  # más que la ventana más larga de un aviso (la de inscripción)
CHECK_EVERY_S = 60
NOTIFY_SEND_TIMEOUT_S = 5
TITLE = "Estudia"
KV_ENABLED = "reminders_enabled"
KV_SENT = "reminders_sent"


@dataclass
class Reminder:
    key: str
    title: str
    text: str


def reminders_enabled(conn: sqlite3.Connection) -> bool:
    """Prendidos por defecto; solo un 0 / false / no / off explícito los apaga."""
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (KV_ENABLED,)).fetchone()
    if row is None or row["value"] is None:
        return True
    return str(row["value"]).strip().lower() not in ("0", "false", "no", "off")


def _clock(epoch: int) -> str:
    t = datetime.fromtimestamp(epoch, TZ)
    return f"{t.hour}:{t.minute:02d}"


def _local_date(epoch: int):
    return datetime.fromtimestamp(epoch, TZ).date()


def _minutes_word(n: int) -> str:
    return "1 minuto" if n == 1 else f"{n} minutos"


def _exam_reminders(conn: sqlite3.Connection, now: int) -> list[Reminder]:
    out = []
    today = _local_date(now)
    for r in conn.execute(
        "SELECT id, name, opens_at FROM exam WHERE opens_at IS NOT NULL AND opens_at > ? "
        "AND COALESCE(kind, 'parcial') != 'inscripcion' ORDER BY opens_at, id",
        (now,),
    ):
        day = _local_date(r["opens_at"])
        # "Mañana" es el día local siguiente al de hoy; antes de las 18:00 todavía no se avisa.
        if day - today != timedelta(days=1) or datetime.fromtimestamp(now, TZ).hour < EXAM_EVE_HOUR:
            continue
        out.append(Reminder(f"exam:{r['id']}:{day.isoformat()}", TITLE,
                            f"Mañana: {r['name']}, {_clock(r['opens_at'])}"))
    return out


def _enrollment_reminders(conn: sqlite3.Connection, now: int) -> list[Reminder]:
    """Una inscripción se avisa una vez, desde las 9:00 locales del día de cierre menos 3 y hasta el cierre."""
    out = []
    for r in conn.execute(
        "SELECT id, name, closes_at FROM exam WHERE kind = 'inscripcion' AND closes_at IS NOT NULL "
        "AND closes_at > ? ORDER BY closes_at, id", (now,),
    ):
        first_day = _local_date(r["closes_at"]) - timedelta(days=ENROLL_LEAD_DAYS)
        from_at = int(datetime(first_day.year, first_day.month, first_day.day, ENROLL_FROM_HOUR, tzinfo=TZ).timestamp())
        if now < from_at:
            continue
        close = datetime.fromtimestamp(r["closes_at"], TZ)
        out.append(Reminder(f"enroll:{r['id']}:{r['closes_at']}", TITLE,
                            f"Cierra la inscripción: {r['name']}, el {close.day}/{close.month} a las {_clock(r['closes_at'])}"))
    return out


def _class_reminders(conn: sqlite3.Connection, now: int) -> list[Reminder]:
    out = []
    for o in occurrences(conn, now, now + CLASS_LEAD_S):
        left = -(-(o.starts_at - now) // 60)  # minutos que faltan, redondeando para arriba
        when = "Ahora" if left <= 0 else f"En {_minutes_word(left)}"
        text = f"{when}: {o.course}" + (f", {o.place}" if o.place else "")
        out.append(Reminder(f"class:{o.slot_id}:{_local_date(o.starts_at).isoformat()}", TITLE, text))
    return out


def _session_reminders(conn: sqlite3.Connection, now: int) -> list[Reminder]:
    """Sesión que empieza: el tema foco o, si es mezclada, lo que dice el plan (`planner._label`)."""
    from estudia.study.planner import sessions
    from estudia.study.sessions import MIXED_FOCUS, MIXED_ONLY

    out = []
    for s in sessions(conn, now - SESSION_WINDOW_S + 1, now + 1):
        if s.done:
            continue
        minutes = _minutes_word(s.minutes)
        if s.kind in (MIXED_ONLY, MIXED_FOCUS) and len(s.topic_ids) > 1:
            text = f"Hora de estudiar, {minutes}. {s.label}"
        else:
            text = f"Hora de estudiar: {s.topic}, {minutes}"
        out.append(Reminder(f"session:{s.id}", TITLE, text))
    return out


def due_reminders(conn: sqlite3.Connection, now: int, sent: set[str]) -> list[Reminder]:
    """Avisos que corresponden en `now` (epoch) y todavía no están en `sent`. Apagados: lista vacía."""
    if not reminders_enabled(conn):
        return []
    found = (_exam_reminders(conn, now) + _enrollment_reminders(conn, now) + _class_reminders(conn, now) + _session_reminders(conn, now))
    return [r for r in found if r.key not in sent]


def load_sent(conn: sqlite3.Connection) -> dict[str, int]:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (KV_SENT,)).fetchone()
    try:
        data = json.loads(row["value"]) if row and row["value"] else {}
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, (int, float))}


def mark_sent(conn: sqlite3.Connection, keys: list[str], now: int) -> None:
    """Suma las claves a kv y descarta las de hace más de 3 días."""
    sent = load_sent(conn)
    sent.update({k: now for k in keys})
    sent = {k: v for k, v in sent.items() if v > now - KEEP_SENT_S}
    conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)", (KV_SENT, json.dumps(sent)))
    conn.commit()


def send(title: str, text: str) -> bool:
    """Primero la bandeja; en Linux, `notify-send`. True si algún camino lo mostró."""
    if tray.notify(title, text):
        return True
    if sys.platform.startswith("linux") and (exe := shutil.which("notify-send")):
        try:
            done = subprocess.run([exe, "-a", "Estudia", title, text], timeout=NOTIFY_SEND_TIMEOUT_S,
                                  capture_output=True, check=False)
            return done.returncode == 0
        except Exception:
            log.warning("notify-send falló", exc_info=True)
    return False


def check_once(conn: sqlite3.Connection, now: int | None = None, sender=send) -> int:
    """Manda los avisos pendientes; una clave se marca como enviada solo si `sender` devolvió True."""
    now = int(time.time()) if now is None else now
    sent = load_sent(conn)
    done = [r.key for r in due_reminders(conn, now, set(sent)) if sender(r.title, r.text)]
    if done:
        mark_sent(conn, done, now)
    return len(done)


def run_reminders(stop: threading.Event, connect: Callable[[], sqlite3.Connection]) -> None:
    """Cuerpo del hilo: revisa cada 60 s con su propia conexión hasta que `stop` se active.

    `connect` abre la base que usa la app: la elige quien lanza el hilo (nunca una por defecto).
    """
    conn = None
    # Una base bloqueada al arrancar no apaga los avisos de toda la sesión: se reintenta.
    while conn is None:
        try:
            conn = connect()
        except Exception:
            log.warning("No se pudo abrir la base para los recordatorios; se reintenta", exc_info=True)
            if stop.wait(CHECK_EVERY_S):
                return
    try:
        while not stop.is_set():
            try:
                check_once(conn)
            except Exception:
                log.exception("Falló la revisión de recordatorios")
            stop.wait(CHECK_EVERY_S)
    finally:
        conn.close()
