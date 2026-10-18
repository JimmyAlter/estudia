"""Exporta la agenda (clases, exámenes, entregas y sesiones de estudio) como calendario iCalendar (RFC 5545)."""
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from estudia.study.exams import ENROLLMENT, list_exams
from estudia.study.planner import sessions
from estudia.study.schedule import TZ, list_slots

HORIZON_DAYS = 60
EXAM_MIN = 120
DEADLINE_MIN = 30
_DAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
_TZID = "America/Argentina/Buenos_Aires"


# Controles que RFC 5545 no admite en un TEXT (todos menos \r y \n, que se escapan; el tab
# pasa a espacio para no pegar palabras).
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _escape(text: str) -> str:
    text = _CONTROL.sub("", text.replace("\t", " "))
    text = text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return text.replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n")


def _fold(line: str) -> str:
    """Pliega a 75 octetos UTF-8 por línea (las continuaciones llevan un espacio al inicio), sin partir caracteres."""
    out, cur, size, limit = [], "", 0, 75
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > limit:
            out.append(cur)
            cur, size, limit = " ", 1, 75
        cur += ch
        size += n
    out.append(cur)
    return "\r\n".join(out)


def _utc(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _local(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%S")


def _timed(uid: str, stamp: str, summary: str, start: int, end: int) -> list[str]:
    return [
        "BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp}",
        f"DTSTART:{_utc(start)}", f"DTEND:{_utc(end)}", f"SUMMARY:{_escape(summary)}", "END:VEVENT",
    ]


def _with_course(name: str, course: str) -> str:
    return f"{name} · {course}" if course else name


def build_ics(conn: sqlite3.Connection, now: int) -> str:
    stamp = _utc(now)
    horizon = now + HORIZON_DAYS * 86400
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Estudia//ES", "CALSCALE:GREGORIAN",
        f"X-WR-TIMEZONE:{_TZID}",
        "BEGIN:VTIMEZONE", f"TZID:{_TZID}",
        "BEGIN:STANDARD", "DTSTART:19700101T000000", "TZOFFSETFROM:-0300", "TZOFFSETTO:-0300", "TZNAME:-03",
        "END:STANDARD", "END:VTIMEZONE",
    ]

    # Clases: una regla semanal que arranca en la primera ocurrencia desde el día de la exportación.
    today = datetime.fromtimestamp(now, TZ).date()
    for s in list_slots(conn):
        day = today + timedelta(days=(s.weekday - today.weekday()) % 7)
        midnight = datetime(day.year, day.month, day.day)
        start, end = midnight + timedelta(minutes=s.start_min), midnight + timedelta(minutes=s.end_min)
        lines += [
            "BEGIN:VEVENT", f"UID:estudia-class-{s.id}@estudia.local", f"DTSTAMP:{stamp}",
            f"DTSTART;TZID={_TZID}:{_local(start)}", f"DTEND;TZID={_TZID}:{_local(end)}",
            f"RRULE:FREQ=WEEKLY;BYDAY={_DAYS[s.weekday]}",
            f"SUMMARY:{_escape(f'{s.course} (clase)' if s.course else 'Clase')}",
        ]
        if s.place:
            lines.append(f"LOCATION:{_escape(s.place)}")
        lines.append("END:VEVENT")

    for e in list_exams(conn, now):
        if e.kind == ENROLLMENT:
            label = _with_course(e.name, e.course)
            if e.at is not None and now <= e.at < horizon:
                lines += _timed(f"estudia-enroll-open-{e.id}@estudia.local", stamp,
                                f"Abre la inscripción: {label}", e.at, e.at + DEADLINE_MIN * 60)
            if e.closes_at is not None and now <= e.closes_at < horizon:
                lines += _timed(f"estudia-enroll-close-{e.id}@estudia.local", stamp,
                                f"Cierra la inscripción: {label}", e.closes_at - DEADLINE_MIN * 60, e.closes_at)
            continue
        if e.at is not None and now <= e.at < horizon:
            lines += _timed(f"estudia-exam-{e.id}@estudia.local", stamp,
                            f"Examen: {_with_course(e.name, e.course)}", e.at, e.at + EXAM_MIN * 60)

    for d in conn.execute(
        "SELECT d.id, d.kind, d.name, d.due_at, COALESCE(c.fullname, '') AS course "
        "FROM deadline d LEFT JOIN course c ON c.id = d.course_id "
        "WHERE d.due_at >= ? AND d.due_at < ? ORDER BY d.due_at, d.id", (now, horizon),
    ):
        label = "Evento" if d["kind"] == "event" else "Entrega"
        lines += _timed(f"estudia-{d['kind']}-{d['id']}@estudia.local", stamp,
                        f"{label}: {_with_course(d['name'] or '', d['course'])}",
                        d["due_at"] - DEADLINE_MIN * 60, d["due_at"])

    for p in sessions(conn, now, horizon):
        lines += _timed(f"estudia-session-{p.id}@estudia.local", stamp,
                        f"Estudiar · {_with_course(p.label, p.course)}", p.starts_at, p.starts_at + p.minutes * 60)

    lines.append("END:VCALENDAR")
    return "".join(_fold(line) + "\r\n" for line in lines)
