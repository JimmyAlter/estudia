"""Dominio por tema: probabilidad estimada de recordar sus preguntas en un instante dado.

`P(tema, t) = Σ R_q(t) / max(vistas, min(total, 8))`, con R_q la recuperabilidad FSRS de cada
pregunta vista. Las no vistas cuentan 0, así que hacen falta al menos 8 preguntas vistas (o
todas, si el tema tiene menos) para llegar a 100 %. Una pregunta en reaprendizaje aporta como
mucho 0,5 hasta graduarse. Solo cuentan las preguntas verificadas y no retiradas.
"""
import sqlite3

from estudia.study.srs import STATE_RELEARNING, exam_dates_by_topic, retrievability

MIN_SEEN = 8            # preguntas vistas que hacen falta para poder llegar a 100 %
RELEARNING_CAP = 0.5    # tope de lo que aporta una pregunta en reaprendizaje
# Solo las del campus. Las generadas con el estilo de un examen (`origin='parcial_propio'`)
# viven solo en el simulacro de ese examen: no entran en la práctica, el dominio, los conteos,
# el plan ni la calibración. Se usa sobre la tabla `question` con el alias `q`.
REGULAR = "q.origin IS NULL"


def topic_probability(conn: sqlite3.Connection, topic_id: int, at: int) -> float:
    """Valor en [0, 1]: probabilidad estimada de recordar el tema en `at`.

    Solo lee `review_state`, que `connect` y cada respuesta registrada mantienen al día.
    """
    total = conn.execute(
        f"SELECT COUNT(*) FROM question q WHERE q.topic_id = ? AND q.verified = 1 AND q.retired = 0 AND {REGULAR}",
        (topic_id,),
    ).fetchone()[0]
    rows = conn.execute(
        "SELECT rs.stability, rs.last_review, rs.state FROM review_state rs "
        "JOIN question q ON rs.card_key = 'q:' || q.id "
        f"WHERE q.topic_id = ? AND q.verified = 1 AND q.retired = 0 AND {REGULAR}",
        (topic_id,),
    ).fetchall()
    denominator = max(len(rows), min(total, MIN_SEEN))
    if not denominator:
        return 0.0
    recalled = 0.0
    for r in rows:
        value = retrievability(r["stability"], r["last_review"], at)
        if r["state"] == STATE_RELEARNING:
            value = min(value, RELEARNING_CAP)
        recalled += value
    return recalled / denominator


def topic_mastery(conn: sqlite3.Connection, topic_id: int, now: int) -> float:
    """Alias de `topic_probability(conn, topic_id, now)`: el dominio de hoy."""
    return topic_probability(conn, topic_id, now)


def exam_horizon(conn: sqlite3.Connection, topic_id: int, now: int) -> int:
    """Fecha del examen más próximo (posterior a `now`) que incluye el tema; si no hay, `now`."""
    return next((d for d in exam_dates_by_topic(conn).get(topic_id, []) if d > now), now)


def topic_probability_at_exam(conn: sqlite3.Connection, topic_id: int, now: int) -> float:
    """Probabilidad de saber el tema el día de su examen más próximo (o hoy, si no tiene)."""
    return topic_probability(conn, topic_id, exam_horizon(conn, topic_id, now))
