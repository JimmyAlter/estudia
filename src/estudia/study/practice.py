"""Práctica: armado del cuestionario, registro de respuestas y repaso de errores."""
import json
import random
import sqlite3
from dataclasses import dataclass

from estudia.study.mastery import topic_mastery


@dataclass
class QuizItem:
    """Pregunta para mostrar; a propósito no incluye la respuesta correcta."""

    question_id: int
    stem: str
    options: list[str]
    topic: str


@dataclass
class AnswerFeedback:
    correct: bool
    correct_index: int
    explanation: str
    common_error: str | None
    source_seconds: int | None
    youtube_id: str | None


def _is_pending_review(results: list[int]) -> bool:
    """`results`: aciertos (1/0) de una pregunta, del más viejo al más nuevo.

    Pendiente si la última respuesta fue un error, o si alguna vez falló y
    las últimas 2 no son ambas aciertos.
    """
    if not any(r == 0 for r in results):
        return False
    return not (len(results) >= 2 and results[-1] and results[-2])


def _weakest_topic(conn: sqlite3.Connection, course_id: int, now: int) -> int | None:
    rows = conn.execute(
        "SELECT DISTINCT t.id FROM topic t JOIN question q ON q.topic_id = t.id "
        "WHERE t.course_id = ? AND q.verified = 1 AND q.retired = 0 ORDER BY t.id",
        (course_id,),
    ).fetchall()
    if not rows:
        return None
    return min((r["id"] for r in rows), key=lambda tid: topic_mastery(conn, tid, now))


def build_quiz(
    conn: sqlite3.Connection,
    course_id: int,
    *,
    topic_id: int | None = None,
    weakest: bool = False,
    size: int = 10,
    now: int,
) -> list[QuizItem]:
    """Cuestionario de hasta `size` preguntas verificadas y no retiradas.

    Orden: pendientes de repaso, nunca respondidas, el resto; cada grupo
    al azar con semilla `now`. Si vienen `topic_id` y `weakest`, gana `topic_id`.
    """
    if topic_id is None and weakest:
        topic_id = _weakest_topic(conn, course_id, now)
        if topic_id is None:
            return []

    sql = (
        "SELECT q.id, q.stem, q.options_json, t.name AS topic FROM question q "
        "JOIN topic t ON t.id = q.topic_id "
        "WHERE t.course_id = ? AND q.verified = 1 AND q.retired = 0"
    )
    params: list = [course_id]
    if topic_id is not None:
        sql += " AND t.id = ?"
        params.append(topic_id)
    questions = conn.execute(sql + " ORDER BY q.id", params).fetchall()

    history: dict[int, list[int]] = {}
    for row in conn.execute(
        "SELECT question_id, correct FROM answer ORDER BY at, id"
    ):
        history.setdefault(row["question_id"], []).append(1 if row["correct"] else 0)

    pending, fresh, rest = [], [], []
    for q in questions:
        results = history.get(q["id"])
        if not results:
            fresh.append(q)
        elif _is_pending_review(results):
            pending.append(q)
        else:
            rest.append(q)

    rng = random.Random(now)
    ordered = []
    for group in (pending, fresh, rest):
        rng.shuffle(group)
        ordered.extend(group)

    return [
        QuizItem(q["id"], q["stem"], json.loads(q["options_json"]), q["topic"])
        for q in ordered[:size]
    ]


def record_answer(
    conn: sqlite3.Connection, question_id: int, chosen_index: int, now: int
) -> AnswerFeedback:
    """Registra la respuesta y devuelve la corrección.

    `LookupError` si la pregunta no existe, no está verificada o está retirada;
    `ValueError` si `chosen_index` está fuera de rango. No escribe nada en esos casos.
    """
    q = conn.execute(
        "SELECT q.*, m.ref AS youtube_id FROM question q "
        "LEFT JOIN material m ON m.id = q.material_id "
        "WHERE q.id = ? AND q.verified = 1 AND q.retired = 0",
        (question_id,),
    ).fetchone()
    if q is None:
        raise LookupError(f"pregunta {question_id} no disponible")
    options = json.loads(q["options_json"])
    if not 0 <= chosen_index < len(options):
        raise ValueError(f"opción {chosen_index} fuera de rango")

    correct = chosen_index == q["correct_index"]
    conn.execute(
        "INSERT INTO answer(question_id, chosen_index, correct, at) VALUES (?, ?, ?, ?)",
        (question_id, chosen_index, 1 if correct else 0, now),
    )
    conn.commit()
    return AnswerFeedback(
        correct=correct,
        correct_index=q["correct_index"],
        explanation=q["explanation"],
        common_error=q["common_error"],
        source_seconds=q["source_seconds"],
        youtube_id=q["youtube_id"],
    )


def retire_question(conn: sqlite3.Connection, question_id: int) -> None:
    """Botón "Esta está mal": la pregunta deja de mostrarse."""
    conn.execute("UPDATE question SET retired = 1 WHERE id = ?", (question_id,))
    conn.commit()


def unretire_question(conn: sqlite3.Connection, question_id: int) -> None:
    """Deshacer el reporte de error: la pregunta vuelve a mostrarse."""
    conn.execute("UPDATE question SET retired = 0 WHERE id = ?", (question_id,))
    conn.commit()
