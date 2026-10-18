"""Exámenes: los del campus (solo lectura) y los cargados a mano (editables)."""
import json
import math
import sqlite3
from dataclasses import dataclass

# Campos que se pueden cambiar en un examen manual, y la columna que tocan.
_EDITABLE = {"name": "name", "at": "opens_at", "max_grade": "max_grade", "time_limit_s": "time_limit_s"}


@dataclass
class ExamOut:
    id: int
    course_id: int
    course: str
    name: str
    at: int | None
    closes_at: int | None
    source: str
    topic_ids: list[int]
    max_grade: float | None
    time_limit_s: int | None
    attempts: int | None
    editable: bool
    result: dict | None = None   # nota real cargada: {"grade", "max_grade", "at"}
    result_skipped: bool = False  # "Prefiero no decir": no se vuelve a preguntar la nota


def _visible_course_topics(conn: sqlite3.Connection, course_id: int) -> list[int]:
    """Temas de las secciones visibles del curso (la regla del campus)."""
    return [
        r["id"]
        for r in conn.execute(
            "SELECT t.id, MIN(s.num) AS num FROM topic t "
            "JOIN material_topic mt ON mt.topic_id = t.id "
            "JOIN material m ON m.id = mt.material_id "
            "JOIN section s ON s.id = m.section_id "
            "WHERE s.visible = 1 AND t.course_id = ? GROUP BY t.id ORDER BY num, t.name",
            (course_id,),
        )
    ]


def _topics_json(conn: sqlite3.Connection, course_id: int, topic_ids: list[int]) -> str | None:
    """NULL significa 'todos los temas del curso' y se resuelve al leer; una lista explícita se valida."""
    topics = list(topic_ids)
    if not topics:
        return None
    own = {r["id"] for r in conn.execute("SELECT id FROM topic WHERE course_id = ?", (course_id,))}
    if not set(topics) <= own:
        raise ValueError("Hay temas que no son del curso del examen")
    return json.dumps(topics)


def _manual_row(conn: sqlite3.Connection, exam_id: int):
    row = conn.execute("SELECT id, course_id, source FROM exam WHERE id = ?", (exam_id,)).fetchone()
    if row is None or row["source"] != "manual":
        raise ValueError("Solo se pueden editar o borrar los exámenes cargados a mano")
    return row


def create_manual_exam(
    conn: sqlite3.Connection, course_id: int, name: str, at: int,
    topic_ids: list[int], max_grade: float | None,
) -> int:
    """Crea un examen manual (cmid NULL: la sincronización no lo toca). Sin temas, abarca todos los del curso."""
    if conn.execute("SELECT 1 FROM course WHERE id = ?", (course_id,)).fetchone() is None:
        raise ValueError("El curso no existe")
    name = (name or "").strip()
    if not name:
        raise ValueError("El examen necesita un nombre")
    if isinstance(at, bool) or not isinstance(at, int):
        raise ValueError("La fecha del examen tiene que ser un entero (epoch)")
    topics_json = _topics_json(conn, course_id, topic_ids)
    cur = conn.execute(
        "INSERT INTO exam(course_id, cmid, name, opens_at, closes_at, max_grade, source, topics_json) "
        "VALUES(?, NULL, ?, ?, NULL, ?, 'manual', ?)",
        (course_id, name, at, max_grade, topics_json),
    )
    conn.commit()
    return cur.lastrowid


def update_manual_exam(conn: sqlite3.Connection, exam_id: int, **fields) -> None:
    """Campos: name, at, max_grade, time_limit_s, topic_ids (vacío = todos los del curso)."""
    row = _manual_row(conn, exam_id)
    unknown = set(fields) - set(_EDITABLE) - {"topic_ids"}
    if unknown:
        raise ValueError(f"Campos no editables: {', '.join(sorted(unknown))}")
    if "name" in fields:
        fields["name"] = (fields["name"] or "").strip()
        if not fields["name"]:
            raise ValueError("El examen necesita un nombre")
    if "at" in fields and (isinstance(fields["at"], bool) or not isinstance(fields["at"], int)):
        raise ValueError("La fecha del examen tiene que ser un entero (epoch)")
    sets, params = [], []
    for key, value in fields.items():
        if key == "topic_ids":
            sets.append("topics_json = ?")
            params.append(_topics_json(conn, row["course_id"], value))
        else:
            sets.append(f"{_EDITABLE[key]} = ?")
            params.append(value)
    if sets:
        conn.execute(f"UPDATE exam SET {', '.join(sets)} WHERE id = ?", (*params, exam_id))
        conn.commit()


def delete_manual_exam(conn: sqlite3.Connection, exam_id: int) -> None:
    _manual_row(conn, exam_id)
    conn.execute("DELETE FROM plan_session WHERE exam_id = ?", (exam_id,))
    conn.execute("DELETE FROM exam_result WHERE exam_id = ?", (exam_id,))
    conn.execute("DELETE FROM exam WHERE id = ?", (exam_id,))
    conn.commit()


def exam_topics(conn: sqlite3.Connection, exam_id: int) -> list[int]:
    """Temas del examen: los guardados si es manual; si es del campus, los de las secciones visibles."""
    row = conn.execute(
        "SELECT course_id, source, topics_json FROM exam WHERE id = ?", (exam_id,)
    ).fetchone()
    if row is None:
        return []
    if row["source"] == "manual" and row["topics_json"] is not None:
        return json.loads(row["topics_json"])
    return _visible_course_topics(conn, row["course_id"])


def list_exams(conn: sqlite3.Connection, now: int) -> list[ExamOut]:
    """Todos los exámenes: los vigentes primero (por fecha), después los pasados (el más reciente primero)."""
    rows = conn.execute(
        "SELECT e.*, c.fullname AS course FROM exam e JOIN course c ON c.id = e.course_id"
    ).fetchall()

    def live(r) -> bool:
        return (r["closes_at"] or r["opens_at"] or 0) > now

    results = {
        r["exam_id"]: {"grade": r["grade"], "max_grade": r["max_grade"], "at": r["at"]}
        for r in conn.execute("SELECT exam_id, grade, max_grade, at FROM exam_result")
    }
    skipped = _skipped(conn)
    upcoming = sorted((r for r in rows if live(r)), key=lambda r: (r["opens_at"] or 0, r["id"]))
    past = sorted((r for r in rows if not live(r)), key=lambda r: (-(r["opens_at"] or 0), r["id"]))
    return [
        ExamOut(
            id=r["id"], course_id=r["course_id"], course=r["course"], name=r["name"],
            at=r["opens_at"], closes_at=r["closes_at"] or None, source=r["source"] or "campus",
            topic_ids=exam_topics(conn, r["id"]), max_grade=r["max_grade"],
            time_limit_s=r["time_limit_s"], attempts=r["attempts"],
            editable=r["source"] == "manual", result=results.get(r["id"]),
            result_skipped=r["id"] in skipped,
        )
        for r in upcoming + past
    ]


def set_exam_result(conn: sqlite3.Connection, exam_id: int, grade: float, now: int,
                    max_grade: float | None = None) -> dict:
    """Guarda la nota real del examen (una por examen; cargarla de nuevo la corrige).

    `LookupError` si el examen no existe; `ValueError` si el examen todavía no empezó, si la
    nota no es un número entre 0 y la escala o si `max_grade` no coincide con la escala del
    examen. La escala es siempre la del examen, o 10 si no tiene.
    """
    exam = conn.execute("SELECT max_grade, opens_at FROM exam WHERE id = ?", (exam_id,)).fetchone()
    if exam is None:
        raise LookupError(f"examen {exam_id} no existe")
    if exam["opens_at"] and exam["opens_at"] > now:
        raise ValueError("El examen todavía no empezó: la nota real se carga después")
    scale = exam["max_grade"] or 10.0
    if max_grade is not None and max_grade != scale:
        raise ValueError(f"La escala de la nota es la del examen ({scale:g})")
    for value in (grade, scale):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("La nota tiene que ser un número")
    if scale <= 0:
        raise ValueError("La escala de la nota tiene que ser mayor que cero")
    if not 0 <= grade <= scale:
        raise ValueError(f"La nota tiene que estar entre 0 y {scale:g}")
    conn.execute(
        "INSERT INTO exam_result(exam_id, grade, max_grade, at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(exam_id) DO UPDATE SET grade = excluded.grade, max_grade = excluded.max_grade, "
        "at = excluded.at",
        (exam_id, float(grade), float(scale), now),
    )
    conn.commit()
    return {"grade": float(grade), "max_grade": float(scale), "at": now}


SKIPPED_KEY = "exam_results_skipped"


def _skipped(conn: sqlite3.Connection) -> set[int]:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (SKIPPED_KEY,)).fetchone()
    try:
        return {int(x) for x in json.loads(row[0])} if row else set()
    except (TypeError, ValueError):
        return set()


def skip_exam_result(conn: sqlite3.Connection, exam_id: int, now: int) -> None:
    """"Prefiero no decir": la nota real de ese examen no se vuelve a pedir.

    `LookupError` si el examen no existe; `ValueError` si todavía no empezó.
    """
    exam = conn.execute("SELECT opens_at FROM exam WHERE id = ?", (exam_id,)).fetchone()
    if exam is None:
        raise LookupError(f"examen {exam_id} no existe")
    if exam["opens_at"] and exam["opens_at"] > now:
        raise ValueError("El examen todavía no empezó")
    ids = sorted(_skipped(conn) | {exam_id})
    conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)", (SKIPPED_KEY, json.dumps(ids)))
    conn.commit()
