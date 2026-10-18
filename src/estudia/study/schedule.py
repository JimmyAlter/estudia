"""Horarios de cursada: franjas semanales por curso y sus ocurrencias concretas."""
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Argentina/Buenos_Aires")
DEFAULT_CLASS_MIN = 120  # una cursada dura 2 h salvo que se diga otra cosa


@dataclass
class SlotOut:
    id: int
    course_id: int
    course: str
    weekday: int
    start_min: int
    end_min: int
    place: str | None


@dataclass
class SlotSuggestion:
    start_min: int
    end_min: int


@dataclass
class Occurrence:
    slot_id: int
    course_id: int
    course: str
    starts_at: int
    ends_at: int
    place: str | None


def _validate(
    conn: sqlite3.Connection, course_id: int, weekday: int, start_min: int, end_min: int,
    slot_id: int | None = None,
) -> None:
    if conn.execute("SELECT 1 FROM course WHERE id = ?", (course_id,)).fetchone() is None:
        raise ValueError("El curso no existe")
    if not 0 <= weekday <= 6:
        raise ValueError("El día tiene que ir de 0 (lunes) a 6 (domingo)")
    if not 0 <= start_min < end_min <= 1440:
        raise ValueError("El horario tiene que cumplir 0 <= inicio < fin <= 1440")
    # Un duplicado exacto no tiene sentido; las superposiciones parciales sí se permiten.
    if conn.execute(
        "SELECT 1 FROM class_slot WHERE course_id = ? AND weekday = ? AND start_min = ? AND end_min = ? "
        "AND id IS NOT ?", (course_id, weekday, start_min, end_min, slot_id),
    ).fetchone():
        raise ValueError("Ese horario ya está cargado")


def add_slot(
    conn: sqlite3.Connection, course_id: int, weekday: int, start_min: int, end_min: int, place: str | None
) -> int:
    _validate(conn, course_id, weekday, start_min, end_min)
    cur = conn.execute(
        "INSERT INTO class_slot(course_id, weekday, start_min, end_min, place, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (course_id, weekday, start_min, end_min, place or None, int(time.time())),
    )
    conn.commit()
    return cur.lastrowid


def update_slot(
    conn: sqlite3.Connection, slot_id: int, course_id: int, weekday: int,
    start_min: int, end_min: int, place: str | None,
) -> None:
    if conn.execute("SELECT 1 FROM class_slot WHERE id = ?", (slot_id,)).fetchone() is None:
        raise ValueError("El horario no existe")
    _validate(conn, course_id, weekday, start_min, end_min, slot_id)
    conn.execute(
        "UPDATE class_slot SET course_id = ?, weekday = ?, start_min = ?, end_min = ?, place = ? WHERE id = ?",
        (course_id, weekday, start_min, end_min, place or None, slot_id),
    )
    conn.commit()


def delete_slot(conn: sqlite3.Connection, slot_id: int) -> None:
    conn.execute("DELETE FROM class_slot WHERE id = ?", (slot_id,))
    conn.commit()


def list_slots(conn: sqlite3.Connection) -> list[SlotOut]:
    rows = conn.execute(
        "SELECT s.id, s.course_id, COALESCE(c.fullname, '') AS course, s.weekday, "
        "s.start_min, s.end_min, s.place "
        "FROM class_slot s LEFT JOIN course c ON c.id = s.course_id "
        "ORDER BY s.weekday, s.start_min, s.id"
    ).fetchall()
    return [SlotOut(**dict(r)) for r in rows]


# "14:00 a 16:00 hs", "19 a 23 hs", "08:30-11:00": rango de inicio a fin
_RANGE = re.compile(
    r"(?<![\w.:])(\d{1,2})(?:[:.](\d{2}))?(\s+(?:a|hasta)\s+|\s*[-–]\s*)"
    r"(\d{1,2})(?:[:.](\d{2}))?(\s*hs\b)?(?![\w:])",
    re.IGNORECASE,
)
# "8.30 HS", "8:30 hs", "18 HS", "19hs": la hora lleva marcador "hs"
_WITH_HS = re.compile(r"(?<![\w.:])(\d{1,2})(?:[:.](\d{2}))?\s*hs\b", re.IGNORECASE)
# "08:30" suelto, con dos puntos
_CLOCK = re.compile(r"(?<![\w.:])(\d{1,2}):(\d{2})(?![\w:])")
# "a las 19" o "a las 19:30"
_A_LAS = re.compile(r"\ba las\s+(\d{1,2})(?:[:.](\d{2}))?(?![\w:])", re.IGNORECASE)


def _minutes(hour: str, minute: str | None, last_hour: int) -> int | None:
    h, m = int(hour), int(minute or 0)
    if 6 <= h <= last_hour and m < 60 and (h < 24 or m == 0):
        return h * 60 + m
    return None


def _find_range(name: str) -> SlotSuggestion | None:
    """Busca de forma solapada, para que un número suelto ("Comisión 2 - 14:00 a ...") no tape el rango."""
    pos = 0
    while (m := _RANGE.search(name, pos)) is not None:
        pos = m.start() + 1
        h1, m1, sep, h2, m2, hs = m.groups()
        # Con guion ("Aula 7 - 8 hs", "2027 - 2028") es rango solo si ambos extremos traen minutos;
        # con "a"/"hasta" alcanza el "hs" final.
        dash = sep.strip() in ("-", "–")
        if (dash and not (m1 and m2)) or (not dash and not hs and not (m1 and m2)):
            continue
        start, end = _minutes(h1, m1, 23), _minutes(h2, m2, 24)
        if start is None or end is None:
            continue
        return SlotSuggestion(start, end if end > start else min(start + DEFAULT_CLASS_MIN, 1440))
    return None


def suggest_from_course_name(name: str) -> SlotSuggestion | None:
    """Detecta la hora de inicio (y el fin, si viene un rango) en el nombre del curso; no sugiere día."""
    name = name or ""
    if (r := _find_range(name)) is not None:
        return r
    for pattern in (_WITH_HS, _CLOCK, _A_LAS):
        for m in pattern.finditer(name):
            start = _minutes(m.group(1), m.group(2), 23)
            if start is not None:
                return SlotSuggestion(start, min(start + DEFAULT_CLASS_MIN, 1440))
    return None


DISMISSED_KEY = "schedule_suggestions_dismissed"


def _dismissed(conn: sqlite3.Connection) -> set[int]:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (DISMISSED_KEY,)).fetchone()
    try:
        return {int(x) for x in json.loads(row[0])} if row else set()
    except (TypeError, ValueError):
        return set()


def dismiss_suggestion(conn: sqlite3.Connection, course_id: int) -> None:
    """"Descartar": la sugerencia de horario de ese curso no vuelve a ofrecerse."""
    ids = sorted(_dismissed(conn) | {course_id})
    conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)", (DISMISSED_KEY, json.dumps(ids)))
    conn.commit()


def suggestions(conn: sqlite3.Connection) -> list[dict]:
    """Horarios sugeridos desde el nombre, solo para cursos sin ningún horario cargado ni sugerencia descartada."""
    out = []
    dismissed = _dismissed(conn)
    for c in conn.execute(
        "SELECT id, fullname FROM course WHERE id NOT IN (SELECT course_id FROM class_slot) ORDER BY fullname"
    ):
        if c["id"] in dismissed:
            continue
        s = suggest_from_course_name(c["fullname"])
        if s:
            out.append({"course_id": c["id"], "course_name": c["fullname"],
                        "start_min": s.start_min, "end_min": s.end_min})
    return out


def occurrences(conn: sqlite3.Connection, start: int, end: int) -> list[Occurrence]:
    """Clases concretas con inicio en [start, end), usando el día local de Buenos Aires."""
    slots = list_slots(conn)
    day = datetime.fromtimestamp(start, TZ).date() - timedelta(days=1)
    last = datetime.fromtimestamp(end, TZ).date() + timedelta(days=1)
    out = []
    while day <= last:
        midnight = datetime(day.year, day.month, day.day, tzinfo=TZ)
        for s in slots:
            if s.weekday != day.weekday():
                continue
            starts = int((midnight + timedelta(minutes=s.start_min)).timestamp())
            ends = int((midnight + timedelta(minutes=s.end_min)).timestamp())
            if start <= starts < end:
                out.append(Occurrence(s.id, s.course_id, s.course, starts, ends, s.place))
        day += timedelta(days=1)
    out.sort(key=lambda o: (o.starts_at, o.slot_id))
    return out
