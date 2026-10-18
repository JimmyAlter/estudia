"""Plan de estudio: reparte el tiempo libre entre los temas que más lo necesitan antes de cada examen.

Cada sesión tiene un tema foco, elegido por prioridad (lo que falta saber el día del examen) con
rotación entre días, y un tipo que dice qué se hace en ella (ver `sessions`):
- `focus`: un tema con menos de 2 sesiones hechas se practica así, en bloque.
- `mixed_focus`: repaso mezclado y después el tema foco.
- `mixed_only`: en los últimos 7 días antes del examen, todo repaso mezclado de los temas del
  examen. El tema foco queda como el que encabeza la mezcla (el que más lo necesita).

Un recuperatorio solo planifica los temas que todavía no están firmes: los de P < 0,7 el día del
recuperatorio (`retake_topics`). Ni el foco ni la mezcla nombran otro.

Un final abarca todos los temas de la materia y se prepara desde 6 semanas antes. Entre sus
candidatos van primero los temas sin ninguna sesión para ese final, así el plan los cubre todos.
"""
import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from estudia.study.exams import FINAL, RETAKE, exam_topics
from estudia.study.mastery import REGULAR, topic_probability
from estudia.study.schedule import TZ, occurrences
from estudia.study.sessions import (
    FOCUS,
    LONG_BLOCK,
    MIXED_FOCUS,
    MIXED_ONLY,
    SHORT_BLOCK,
    session_topics,
    split_minutes,
)
from estudia.study.srs import GUESS, NEAR_EXAM_DAYS, sync_review_state

DEFAULT_START_MIN = 1140   # 19:00
BREAK_MIN = 10             # descanso entre bloques seguidos
MIN_FREE_TODAY = 25        # con menos libre en lo que queda de hoy, hoy no se planifica
REPEAT_FACTOR = 0.35      # cada repetición reciente de un tema multiplica su prioridad por esto
REPEAT_WINDOW_DAYS = 3     # días anteriores (además del propio) que cuentan como "reciente"
DONE_ANSWERS = 5           # respuestas del tema en el día que marcan la sesión como hecha ...
DONE_CORRECT = 3           # ... si además hay tantos aciertos que no fueron "Adiviné"

BLOCKED_SESSIONS = 2       # sesiones hechas de un tema antes de pasar a la mezcla
MIXED_ONLY_DAYS = NEAR_EXAM_DAYS   # última semana antes del examen: todo mezclado

RETAKE_THRESHOLD = 0.7     # en un recuperatorio entran los temas con P menor a esto el día del examen
RETAKE_MOCK_AFTER_DAYS = 3  # simulacro corto sugerido a los 3 días de cargar el recuperatorio

FINAL_MIN_WEEKS = 4        # con menos, el plan del final avisa que usa el tiempo que hay
FINAL_MAX_WEEKS = 6        # los temas de un final son candidatos desde 6 semanas antes
FINAL_SHORT_NOTE = "Faltan menos de 4 semanas: el plan usa el tiempo que hay."


@dataclass
class SessionOut:
    id: int
    course_id: int
    course: str
    topic_id: int
    topic: str
    exam_id: int | None
    exam: str | None
    starts_at: int
    minutes: int
    done: bool
    kind: str               # focus | mixed_focus | mixed_only
    topic_ids: list[int]    # el foco primero; en las mezcladas, todos los temas del examen
    review_minutes: int     # repaso mezclado de vencidas al empezar
    focus_minutes: int      # tema foco después
    label: str              # "Foco: Límites", "Foco: Límites, con repaso de Derivadas", "Repaso mezclado: Límites y Derivadas"


@dataclass
class WeakTopic:
    topic_id: int
    topic: str
    probability: float      # de saberlo el día del examen
    minutes: int            # lo que le falta: 50 × (1 − P) + 1 por repaso que vence antes del examen


@dataclass
class ExamHealth:
    exam_id: int
    exam: str
    course: str
    at: int
    minutes_needed: int
    minutes_available: int
    verdict: str   # "llegás" | "justo" | "no llegás"
    due_reviews: int = 0                                       # repasos que vencen antes del examen
    weak_topics: list[WeakTopic] = field(default_factory=list)   # por qué: los 3 de menor P
    today: SessionOut | None = None                            # qué hacer: la próxima sesión de hoy
    kind: str = "parcial"                                      # tipo del examen (`exams.EXAM_KINDS`)
    note: str | None = None                                    # una línea de aclaración, sin alarma
    mock_suggested_at: int | None = None                       # recuperatorio: día del simulacro corto


# --- Configuración -----------------------------------------------------------------------------

def set_availability(conn: sqlite3.Connection, minutes_by_weekday: dict[int, int]) -> None:
    """Guarda los minutos de estudio de los días dados (0 = lunes); los demás quedan como estaban."""
    for weekday, minutes in minutes_by_weekday.items():
        if isinstance(weekday, bool) or not isinstance(weekday, int) or not 0 <= weekday <= 6:
            raise ValueError("El día tiene que ir de 0 (lunes) a 6 (domingo)")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 0 <= minutes <= 1440:
            raise ValueError("Los minutos por día tienen que ser un entero entre 0 y 1440")
    conn.executemany(
        "INSERT INTO availability(weekday, minutes) VALUES (?, ?) "
        "ON CONFLICT(weekday) DO UPDATE SET minutes = excluded.minutes",
        list(minutes_by_weekday.items()),
    )
    conn.commit()


def get_availability(conn: sqlite3.Connection) -> dict[int, int]:
    """Minutos por día de la semana (0 = lunes); los días sin cargar valen 0."""
    out = {d: 0 for d in range(7)}
    for r in conn.execute("SELECT weekday, minutes FROM availability"):
        if r["weekday"] in out:
            out[r["weekday"]] = r["minutes"] or 0
    return out


def preferred_start_min(conn: sqlite3.Connection) -> int:
    """Hora preferida para empezar a estudiar, en minutos desde la medianoche local."""
    row = conn.execute("SELECT value FROM kv WHERE key = 'study_start_min'").fetchone()
    try:
        value = int(row["value"]) if row else DEFAULT_START_MIN
    except (TypeError, ValueError):
        return DEFAULT_START_MIN
    return value if 0 <= value < 1440 else DEFAULT_START_MIN


# --- Utilidades de días locales ----------------------------------------------------------------

def _local_day(epoch: int) -> date:
    return datetime.fromtimestamp(epoch, TZ).date()


def _midnight(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=TZ).timestamp())


def _day_bounds(day: date) -> tuple[int, int]:
    return _midnight(day), _midnight(day + timedelta(days=1))


def _merge(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Une los intervalos que se pisan o se tocan (dos franjas de cursada pueden superponerse)."""
    out: list[list[int]] = []
    for start, end in sorted(intervals):
        if out and start <= out[-1][1]:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return [(s, e) for s, e in out]


def _free_seconds(start: int, end: int, busy: list[tuple[int, int]]) -> int:
    taken = sum(max(0, min(e, end) - max(s, start)) for s, e in busy)
    return max(0, end - start - taken)


def _block_sizes(budget: int) -> list[int]:
    """Con 50 minutos o menos, bloques de 25; si no, bloques de 50 y uno de 25 si sobran 25 o más."""
    if budget <= 2 * SHORT_BLOCK:
        return [SHORT_BLOCK] * (max(0, budget) // SHORT_BLOCK)
    sizes = [LONG_BLOCK] * (budget // LONG_BLOCK)
    if budget % LONG_BLOCK >= SHORT_BLOCK:
        sizes.append(SHORT_BLOCK)
    return sizes


def _layout(start: int, end: int, busy: list[tuple[int, int]], sizes: list[int]) -> list[tuple[int, int]]:
    """(inicio, minutos) de los bloques en orden desde `start`, saltando lo ocupado, sin pasarse de `end`."""
    out: list[tuple[int, int]] = []
    cursor = start
    for minutes in sizes:
        length = minutes * 60
        while (clash := next((e for s, e in busy if s < cursor + length and cursor < e), None)) is not None:
            cursor = clash
        if cursor + length > end:
            break
        out.append((cursor, minutes))
        cursor += length + BREAK_MIN * 60
    return out


# --- Temas y exámenes --------------------------------------------------------------------------

def _plannable_topics(conn: sqlite3.Connection) -> dict[int, int]:
    """Temas con al menos una pregunta verificada y no retirada: id → curso."""
    return {
        r["id"]: r["course_id"]
        for r in conn.execute(
            "SELECT DISTINCT t.id, t.course_id FROM topic t JOIN question q ON q.topic_id = t.id "
            f"WHERE q.verified = 1 AND q.retired = 0 AND {REGULAR}"
        )
    }


def _future_exams(conn: sqlite3.Connection, now: int) -> list[sqlite3.Row]:
    """Exámenes con fecha posterior a `now`, ordenados por fecha; los sin fecha no cuentan."""
    return conn.execute(
        "SELECT e.id, e.name, e.opens_at, COALESCE(e.kind, 'parcial') AS kind, e.created_at, "
        "c.fullname AS course FROM exam e "
        "LEFT JOIN course c ON c.id = e.course_id "
        "WHERE e.opens_at IS NOT NULL AND e.opens_at > ? AND COALESCE(e.kind, 'parcial') != 'inscripcion' ORDER BY e.opens_at, e.id",
        (now,),
    ).fetchall()


def retake_topics(conn: sqlite3.Connection, exam_id: int) -> list[int]:
    """Temas del examen que todavía no están firmes: P < 0,7 el día del examen, en el orden de `exam_topics`."""
    row = conn.execute("SELECT opens_at FROM exam WHERE id = ?", (exam_id,)).fetchone()
    if row is None or row["opens_at"] is None:
        return []
    return [t for t in exam_topics(conn, exam_id)
            if topic_probability(conn, t, row["opens_at"]) < RETAKE_THRESHOLD]


def _scheduled_topics(conn: sqlite3.Connection, exam: sqlite3.Row) -> list[int]:
    """Temas que el plan prepara para el examen: en un recuperatorio, solo los que no están firmes."""
    return retake_topics(conn, exam["id"]) if exam["kind"] == RETAKE else exam_topics(conn, exam["id"])


def _retake_mock_at(exam: sqlite3.Row, now: int) -> int | None:
    """Inicio del día local del simulacro corto: 3 días después de cargar el recuperatorio, como
    mucho el día anterior a él. None si no es recuperatorio, si no se sabe cuándo se cargó o si
    ese día ya pasó."""
    if exam["kind"] != RETAKE or exam["created_at"] is None:
        return None
    day = min(_local_day(exam["created_at"]) + timedelta(days=RETAKE_MOCK_AFTER_DAYS),
              _local_day(exam["opens_at"]) - timedelta(days=1))
    return _midnight(day) if day >= _local_day(now) else None


def _priority(probability: float, days_left: int) -> float:
    """Lo que falta saber el día del examen (1 − P), con más peso cuanto más cerca está."""
    return (1 - probability) * (1 + 7 / max(1, days_left))


# --- Plan --------------------------------------------------------------------------------------

def replan(conn: sqlite3.Connection, now: int, horizon_days: int = 14) -> int:
    """Rehace las sesiones futuras no hechas desde `now`. Las pasadas y las hechas quedan. Devuelve las creadas.

    Todo pasa en una transacción: si algo falla, el borrado se deshace.
    """
    with conn:
        return _replan(conn, now, horizon_days)


def _replan(conn: sqlite3.Connection, now: int, horizon_days: int) -> int:
    conn.execute("DELETE FROM plan_session WHERE done = 0 AND starts_at >= ?", (now,))
    # Un examen nuevo, movido o borrado cambia los repasos de sus temas (regla de Cepeda).
    sync_review_state(conn)

    topics = _plannable_topics(conn)
    # Para cada tema, sus exámenes por fecha: (día local del examen, id del examen).
    exams_by_topic: dict[int, list[tuple[date, int]]] = {}
    exam_at: dict[int, int] = {}
    topics_of_exam: dict[int, list[int]] = {}
    final_from: dict[int, date] = {}   # final → primer día local en que sus temas son candidatos
    for exam in _future_exams(conn, now):
        exam_day = _local_day(exam["opens_at"])
        exam_at[exam["id"]] = exam["opens_at"]
        if exam["kind"] == FINAL:
            final_from[exam["id"]] = exam_day - timedelta(weeks=FINAL_MAX_WEEKS)
        # Un recuperatorio deja afuera los temas firmes: así ni el foco ni la mezcla los nombran.
        topics_of_exam[exam["id"]] = [t for t in _scheduled_topics(conn, exam) if t in topics]
        for topic_id in topics_of_exam[exam["id"]]:
            exams_by_topic.setdefault(topic_id, []).append((exam_day, exam["id"]))
    if not exams_by_topic:
        return 0

    # Probabilidad de saber cada tema el día del examen para el que se prepara: lo que se olvida
    # hasta entonces pesa, así que un tema dominado hoy que se va a olvidar vuelve a tener sesiones.
    probability: dict[tuple[int, int], float] = {}

    def p_at_exam(topic_id: int, exam_id: int) -> float:
        if (topic_id, exam_id) not in probability:
            probability[topic_id, exam_id] = topic_probability(conn, topic_id, exam_at[exam_id])
        return probability[topic_id, exam_id]

    # Sesiones de foco o mezcla con foco ya hechas por tema: las dos primeras van en bloque.
    blocked: dict[int, int] = {
        r["topic_id"]: r["n"]
        for r in conn.execute(
            "SELECT topic_id, COUNT(*) AS n FROM plan_session WHERE done = 1 AND COALESCE(kind, 'focus') != ? "
            "GROUP BY topic_id",
            (MIXED_ONLY,),
        )
    }
    availability = get_availability(conn)
    start_min = preferred_start_min(conn)
    today = _local_day(now)
    last_exam_day = max(d for items in exams_by_topic.values() for d, _ in items)
    # Temas que ya tienen alguna sesión de cada final (las que quedaron y las que se van creando).
    covered: dict[int, set[int]] = {e: set() for e in final_from}
    if final_from:
        for r in conn.execute(
            f"SELECT exam_id, topic_id FROM plan_session WHERE exam_id IN ({','.join('?' * len(final_from))})",
            list(final_from),
        ):
            covered[r["exam_id"]].add(r["topic_id"])
    created = 0

    for offset in range(horizon_days):
        day = today + timedelta(days=offset)
        if day >= last_exam_day:
            break
        # Cada tema se prepara para su examen más cercano que todavía no llegó.
        candidates = {}
        for topic_id, items in exams_by_topic.items():
            # Un final todavía lejano no cuenta: el tema se prepara para otro examen, o espera.
            ahead = [(d, e) for d, e in items if d > day and day >= final_from.get(e, day)]
            if ahead:
                exam_day, exam_id = min(ahead)
                candidates[topic_id] = (exam_id, (exam_day - day).days)
        if not candidates:
            continue

        day_start, day_end = _day_bounds(day)
        kept = conn.execute(
            "SELECT topic_id, starts_at, minutes, done FROM plan_session WHERE starts_at >= ? AND starts_at < ?",
            (day_start, day_end),
        ).fetchall()
        # Descuentan lo hecho y lo que ya empezó (aunque no esté marcado como hecho).
        used = sum(r["minutes"] for r in kept if r["done"] or r["starts_at"] <= now)
        sizes = _block_sizes(availability[day.weekday()] - used)
        if not sizes:
            continue
        # Las sesiones que quedan llevan su descanso alrededor, como entre bloques nuevos.
        pad = BREAK_MIN * 60
        busy = _merge(
            [(o.starts_at, o.ends_at) for o in occurrences(conn, day_start, day_end)]
            + [(r["starts_at"] - pad, r["starts_at"] + r["minutes"] * 60 + pad) for r in kept]
        )
        first = day_start + start_min * 60
        if day == today and now > first:
            first = day_start + math.ceil((now - day_start) / 300) * 300
            if _free_seconds(first, day_end, busy) < MIN_FREE_TODAY * 60:
                continue

        # Repeticiones por tema en el día y los anteriores de la ventana (sesiones que quedaron y nuevas).
        assigned: dict[int, int] = {
            r["topic_id"]: r["n"]
            for r in conn.execute(
                "SELECT topic_id, COUNT(*) AS n FROM plan_session WHERE starts_at >= ? AND starts_at < ? "
                "GROUP BY topic_id",
                (_midnight(day - timedelta(days=REPEAT_WINDOW_DAYS)), day_end),
            )
        }
        # La última sesión nueva del día: si la siguiente es igual y va pegada, se unen (ver abajo).
        last: dict | None = None
        for starts_at, minutes in _layout(first, day_end, busy, sizes):
            # Cada repetición reciente multiplica la prioridad por REPEAT_FACTOR; empate: id menor.
            # En la última semana la rotación sigue eligiendo el tema que encabeza la mezcla.
            def rank(t: int) -> tuple[float, int]:
                return (-_priority(p_at_exam(t, candidates[t][0]), candidates[t][1])
                        * REPEAT_FACTOR ** assigned.get(t, 0), t)

            topic_id = min(candidates, key=rank)
            exam_id = candidates[topic_id][0]
            if exam_id in covered:
                # En un final, primero los temas sin ninguna sesión para él; después, la prioridad.
                uncovered = [t for t in candidates if candidates[t][0] == exam_id and t not in covered[exam_id]]
                if uncovered and topic_id not in uncovered:
                    topic_id = min(uncovered, key=rank)
                covered[exam_id].add(topic_id)
            exam_id, days_left = candidates[topic_id]
            mixed = [topic_id] + [t for t in topics_of_exam[exam_id] if t != topic_id]
            if days_left <= MIXED_ONLY_DAYS:
                kind, topics_for_session = MIXED_ONLY, mixed
            elif blocked.get(topic_id, 0) < BLOCKED_SESSIONS:
                kind, topics_for_session = FOCUS, [topic_id]
            else:
                kind, topics_for_session = MIXED_FOCUS, mixed
            # Dos bloques seguidos con lo mismo (mismo examen, tipo y temas; fuera de la mezcla
            # pura, también el mismo foco) son una sola sesión, mientras no pase de un bloque largo.
            if (
                last is not None
                and last["exam_id"] == exam_id
                and last["kind"] == kind
                and set(last["topics"]) == set(topics_for_session)
                and (kind == MIXED_ONLY or last["topic_id"] == topic_id)
                and starts_at == last["starts_at"] + (last["minutes"] + BREAK_MIN) * 60
                and last["minutes"] + minutes <= LONG_BLOCK
            ):
                last["minutes"] += minutes
                conn.execute("UPDATE plan_session SET minutes = ? WHERE id = ?", (last["minutes"], last["id"]))
                last = None   # ya ocupa un bloque largo entero
                continue
            assigned[topic_id] = assigned.get(topic_id, 0) + 1
            if kind != MIXED_ONLY:
                # Las sesiones planeadas antes que esta cuentan como hechas para cuando llegue.
                blocked[topic_id] = blocked.get(topic_id, 0) + 1
            cur = conn.execute(
                "INSERT INTO plan_session(course_id, topic_id, exam_id, starts_at, minutes, done, kind, "
                "topics_json) VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                (topics[topic_id], topic_id, exam_id, starts_at, minutes, kind, json.dumps(topics_for_session)),
            )
            last = {"id": cur.lastrowid, "exam_id": exam_id, "kind": kind, "topic_id": topic_id,
                    "topics": topics_for_session, "starts_at": starts_at, "minutes": minutes}
            created += 1
    return created


def _names(names: list[str]) -> str:
    """Hasta dos nombres y "y N más": "A", "A y B", "A, B y 2 más".

    Si algún nombre ya tiene una "y" ("Paralelismo y perpendicularidad"), los nombres se unen con
    coma: "A y B" se leería como tres temas.
    """
    if len(names) <= 2:
        return (", " if any(" y " in n for n in names) else " y ").join(names)
    return f"{names[0]}, {names[1]} y {len(names) - 2} más"


def _sorted_names(names: list[str]) -> list[str]:
    return sorted(names, key=str.casefold)


def _label(kind: str, topic_names: list[str]) -> str:
    """Qué se hace en la sesión; `topic_names` en el orden de `topic_ids` (el foco primero).

    Los temas del repaso van en orden alfabético: la misma mezcla se llama igual sea cual sea el foco.
    """
    if kind == MIXED_ONLY:
        return f"Repaso mezclado: {_names(_sorted_names(topic_names))}"
    if kind == MIXED_FOCUS and len(topic_names) > 1:
        return f"Foco: {topic_names[0]}, con repaso de {_names(_sorted_names(topic_names[1:]))}"
    return f"Foco: {topic_names[0]}"


def sessions(conn: sqlite3.Connection, start: int, end: int) -> list[SessionOut]:
    """Sesiones con inicio en [start, end), con el nombre del tema, del curso y del examen."""
    rows = conn.execute(
        "SELECT p.id, p.course_id, COALESCE(c.fullname, '') AS course, p.topic_id, "
        "COALESCE(t.name, '') AS topic, p.exam_id, e.name AS exam, p.starts_at, p.minutes, p.done, "
        "COALESCE(p.kind, 'focus') AS kind, p.topics_json "
        "FROM plan_session p LEFT JOIN topic t ON t.id = p.topic_id "
        "LEFT JOIN course c ON c.id = p.course_id LEFT JOIN exam e ON e.id = p.exam_id "
        "WHERE p.starts_at >= ? AND p.starts_at < ? ORDER BY p.starts_at, p.id",
        (start, end),
    ).fetchall()
    names = {r["id"]: r["name"] or "" for r in conn.execute("SELECT id, name FROM topic")} if rows else {}
    out = []
    for r in rows:
        topic_ids = session_topics(r)
        review, focus = split_minutes(r["kind"], r["minutes"])
        fields = {k: r[k] for k in r.keys() if k != "topics_json"}
        out.append(SessionOut(**{**fields, "done": bool(r["done"])}, topic_ids=topic_ids,
                              review_minutes=review, focus_minutes=focus,
                              label=_label(r["kind"], [names.get(t, "") for t in topic_ids])))
    return out


def _counted_topics(row: sqlite3.Row) -> set[int]:
    """Temas cuyas respuestas cuentan para la sesión: el foco; en una `mixed_only`, todos los suyos."""
    return set(session_topics(row)) if row["kind"] == MIXED_ONLY else {row["topic_id"]}


def mark_done_from_answers(conn: sqlite3.Connection, now: int) -> int:
    """Marca hechas las sesiones de hoy o anteriores con 5 o más respuestas y, entre ellas, 3 o más
    aciertos que no fueron "Adiviné" (un acierto de suerte no es recuperación). El pretest no cuenta.

    Cada respuesta cuenta para una sola sesión. Por día local se reparten en orden de hora: una
    respuesta va a la primera sesión todavía sin completar que la cuenta (ver `_counted_topics`)
    y que empezó a esa hora o antes. Nunca va a una sesión que todavía no empezó, salvo la
    práctica temprana: lo respondido antes de la primera sesión del día cuenta para esa sola.
    Una sesión se completa al cumplir la regla y deja de recibir respuestas. El reparto se rehace igual en cada
    llamada (las sesiones ya hechas también reciben las suyas), así que repetirlo no marca más.
    """
    _, today_end = _day_bounds(_local_day(now))
    by_day: dict[date, list[sqlite3.Row]] = {}
    for r in conn.execute(
        "SELECT id, topic_id, starts_at, done, COALESCE(kind, 'focus') AS kind, topics_json FROM plan_session "
        "WHERE starts_at < ? ORDER BY starts_at, id",
        (today_end,),
    ).fetchall():
        by_day.setdefault(_local_day(r["starts_at"]), []).append(r)

    marked = 0
    for day, day_sessions in by_day.items():
        if all(r["done"] for r in day_sessions):
            continue
        counted = {r["id"]: _counted_topics(r) for r in day_sessions}
        topics = sorted(set().union(*counted.values()))
        day_start, day_end = _day_bounds(day)
        tally = {r["id"]: [0, 0] for r in day_sessions}   # respuestas, aciertos que valen
        first = day_sessions[0]
        complete: set[int] = set()
        for a in conn.execute(
            f"SELECT q.topic_id, a.at, a.correct = 1 AND COALESCE(a.confidence, -1) != ? AS good "
            f"FROM answer a JOIN question q ON q.id = a.question_id "
            f"WHERE {REGULAR} AND q.topic_id IN ({','.join('?' * len(topics))}) AND a.at >= ? AND a.at < ? "
            f"AND COALESCE(a.mode, '') != 'pretest' ORDER BY a.at, a.id",
            (GUESS, *topics, day_start, day_end),
        ):
            open_ = [r for r in day_sessions if r["id"] not in complete and a["topic_id"] in counted[r["id"]]]
            target = next((r for r in open_ if r["starts_at"] <= a["at"]), None)
            if target is None and a["at"] < first["starts_at"] and first in open_:
                target = first   # práctica temprana: adelantarse a la primera sesión vale
            if target is None:
                continue
            count = tally[target["id"]]
            count[0] += 1
            count[1] += bool(a["good"])
            if count[0] >= DONE_ANSWERS and count[1] >= DONE_CORRECT:
                complete.add(target["id"])
        for r in day_sessions:
            if r["id"] in complete and not r["done"]:
                conn.execute("UPDATE plan_session SET done = 1 WHERE id = ?", (r["id"],))
                marked += 1
    conn.commit()
    return marked


# --- ¿Llegás? ----------------------------------------------------------------------------------

def _due_before(conn: sqlite3.Connection, topic_ids: list[int], at: int) -> dict[int, int]:
    """Tema → preguntas (verificadas, no retiradas) cuyo repaso vence antes de `at`."""
    if not topic_ids:
        return {}
    return {
        r["topic_id"]: r["n"]
        for r in conn.execute(
            f"SELECT q.topic_id, COUNT(*) AS n FROM review_state rs JOIN question q ON rs.card_key = 'q:' || q.id "
            f"WHERE q.verified = 1 AND q.retired = 0 AND {REGULAR} AND rs.due < ? "
            f"AND q.topic_id IN ({','.join('?' * len(topic_ids))}) GROUP BY q.topic_id",
            (at, *topic_ids),
        )
    }


def plan_health(conn: sqlite3.Connection, now: int) -> list[ExamHealth]:
    """Para cada examen futuro: minutos que hacen falta, minutos que hay, si alcanzan, por qué y qué hacer hoy.

    `minutes_needed = Σ_temas 50 × (1 − P(tema, examen)) + repasos que vencen antes del examen × 1`.
    Cuenta todos los temas del examen (`exam_topics`), tengan o no preguntas; en un recuperatorio,
    solo los que no están firmes (`retake_topics`).
    `minutes_available` suma la disponibilidad desde hoy hasta el día anterior al examen; la de hoy
    se limita a lo que queda desde max(ahora, hora preferida) hasta las 24:00 y descuenta lo ya hecho.
    `weak_topics`: los 3 temas con menor P a los que les falta algo, con sus minutos.
    `today`: la próxima sesión de hoy para ese examen que no está hecha ni terminó.
    `mock_suggested_at`: en un recuperatorio, el día sugerido para el simulacro corto (ver `_retake_mock_at`).
    `note`: en un final a menos de 4 semanas (días locales), `FINAL_SHORT_NOTE`.
    """
    availability = get_availability(conn)
    today = _local_day(now)
    today_start, today_end = _day_bounds(today)
    start = max(now, today_start + preferred_start_min(conn) * 60)
    studied_today = conn.execute(
        "SELECT COALESCE(SUM(minutes), 0) FROM plan_session WHERE done = 1 AND starts_at >= ? AND starts_at < ?",
        (today_start, today_end),
    ).fetchone()[0]
    pending_today = [s for s in sessions(conn, today_start, today_end)
                     if not s.done and s.starts_at + s.minutes * 60 > now]
    names = {r["id"]: r["name"] or "" for r in conn.execute("SELECT id, name FROM topic")}

    out = []
    for exam in _future_exams(conn, now):
        topic_ids = _scheduled_topics(conn, exam)
        at = exam["opens_at"]
        due = _due_before(conn, topic_ids, at)
        # Lo que le falta a cada tema el día del examen: un tema que se olvida necesita más minutos.
        gaps = []
        for topic_id in topic_ids:
            p = topic_probability(conn, topic_id, at)
            gaps.append((p, topic_id, LONG_BLOCK * (1 - p) + due.get(topic_id, 0)))
        needed = round(sum(minutes for _, _, minutes in gaps))
        # `sorted` es estable: ante empates manda el orden de los temas (por sección).
        weak = [WeakTopic(t, names.get(t, ""), p, round(minutes))
                for p, t, minutes in sorted(gaps, key=lambda g: g[0]) if round(minutes) > 0][:3]
        exam_day = _local_day(at)
        days = [today + timedelta(days=i) for i in range((exam_day - today).days)]
        available = sum(availability[d.weekday()] for d in days[1:])
        if days:
            left_today = max(0, today_end - start) // 60
            available += max(0, min(availability[today.weekday()] - studied_today, left_today))
        # Comparación en enteros: 120 % y 80 % sin errores de coma flotante.
        if available * 10 >= needed * 12:
            verdict = "llegás"
        elif available * 10 >= needed * 8:
            verdict = "justo"
        else:
            verdict = "no llegás"
        short_final = exam["kind"] == FINAL and (exam_day - today).days < FINAL_MIN_WEEKS * 7
        out.append(ExamHealth(
            exam["id"], exam["name"], exam["course"] or "", at, needed, available, verdict,
            due_reviews=sum(due.values()), weak_topics=weak,
            today=next((s for s in pending_today if s.exam_id == exam["id"]), None),
            kind=exam["kind"], note=FINAL_SHORT_NOTE if short_final else None,
            mock_suggested_at=_retake_mock_at(exam, now),
        ))
    return out
