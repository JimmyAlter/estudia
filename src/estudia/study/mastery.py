"""Dominio por tema: promedio ponderado de las últimas respuestas, con decaimiento."""
import sqlite3

LAST_ANSWERS = 20
HALF_LIFE_DAYS = 7
SECONDS_PER_DAY = 86400


def topic_mastery(conn: sqlite3.Connection, topic_id: int, now: int) -> float:
    """Valor en [0, 1]: aciertos ponderados / peso total de las últimas 20 respuestas.

    Cada respuesta pesa `0.5 ** (edad_días / 7)`. Sin respuestas, 0. Las respuestas a
    preguntas retiradas ("Esta está mal") no cuentan: la pregunta podía estar mal.
    """
    rows = conn.execute(
        "SELECT a.correct, a.at FROM answer a "
        "JOIN question q ON q.id = a.question_id "
        "WHERE q.topic_id = ? AND q.retired = 0 ORDER BY a.at DESC, a.id DESC LIMIT ?",
        (topic_id, LAST_ANSWERS),
    ).fetchall()
    total = 0.0
    correct = 0.0
    for row in rows:
        age_days = max(0, now - row["at"]) / SECONDS_PER_DAY
        weight = 0.5 ** (age_days / HALF_LIFE_DAYS)
        total += weight
        if row["correct"]:
            correct += weight
    return correct / total if total else 0.0
