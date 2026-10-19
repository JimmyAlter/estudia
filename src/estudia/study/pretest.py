""""3 preguntas para arrancar": un pretest con preguntas de una clase recién procesada.

Las respuestas quedan en `answer` con `mode = "pretest"` y nada más: no cuentan para el dominio,
el repaso, la probabilidad, el plan ni la calibración, y la pregunta vuelve después como nunca vista.
"""
import json
import sqlite3
from dataclasses import dataclass

from estudia.study.practice import MCQ, AnswerFeedback, QuizItem, _insert_answer

PRETEST_SIZE = 3
PRETEST_WINDOW_DAYS = 7
CLOSED_KEY = "pretest_closed"   # lista JSON de ids de material ya cerrados ("Ahora no" o terminado)

_AVAILABLE = "q.verified = 1 AND q.retired = 0"


@dataclass
class PretestOffer:
    material_id: int
    title: str
    course: str
    kind: str = "youtube"   # youtube | pdf | slides: un documento no tiene video


def _closed(conn: sqlite3.Connection) -> list[int]:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (CLOSED_KEY,)).fetchone()
    try:
        return [int(m) for m in json.loads(row["value"])] if row else []
    except (TypeError, ValueError):
        return []   # una marca rota vuelve a ofrecer, nada más


def pretest_offer(conn: sqlite3.Connection, now: int) -> PretestOffer | None:
    """La clase lista más reciente (de los últimos 7 días, en una sección visible) con 3 preguntas
    disponibles, sin ninguna respuesta en ningún modo y que no se cerró."""
    closed = _closed(conn)
    sql = (
        "SELECT m.id, m.title, COALESCE(m.kind, 'youtube') AS kind, (SELECT MIN(c.fullname) FROM course c WHERE c.id = s.course_id) AS course "
        "FROM material m JOIN section s ON s.id = m.section_id "
        "WHERE s.visible = 1 AND m.status = 'done' AND m.updated_at >= ? "
        f"AND (SELECT COUNT(*) FROM question q WHERE q.material_id = m.id AND {_AVAILABLE}) >= ? "
        "AND NOT EXISTS (SELECT 1 FROM answer a JOIN question q ON q.id = a.question_id "
        "WHERE q.material_id = m.id) "
    )
    params: list = [now - PRETEST_WINDOW_DAYS * 86400, PRETEST_SIZE]
    if closed:
        sql += f"AND m.id NOT IN ({','.join('?' * len(closed))}) "
        params.extend(closed)
    row = conn.execute(sql + "ORDER BY m.updated_at DESC, m.id DESC LIMIT 1", params).fetchone()
    if row is None:
        return None
    return PretestOffer(row["id"], row["title"] or "", row["course"] or "", row["kind"])


def build_pretest(conn: sqlite3.Connection, material_id: int) -> list[QuizItem]:
    """3 preguntas de la clase repartidas a lo largo del video o del documento: ordenadas por
    `source_seconds` o, en un documento, por `source_page` (las sin ninguno, primero), la primera, la del medio y la última. `LookupError` si no hay 3 o
    si la clase no está lista (`done`) en una sección visible, como la oferta."""
    rows = conn.execute(
        "SELECT q.id, q.stem, q.options_json, t.name AS topic FROM question q "
        "JOIN topic t ON t.id = q.topic_id "
        "JOIN material m ON m.id = q.material_id JOIN section s ON s.id = m.section_id "
        f"WHERE q.material_id = ? AND m.status = 'done' AND s.visible = 1 AND {_AVAILABLE} "
        "ORDER BY COALESCE(q.source_seconds, q.source_page) IS NOT NULL,"
        " COALESCE(q.source_seconds, q.source_page), q.id",
        (material_id,),
    ).fetchall()
    n = len(rows)
    if n < PRETEST_SIZE:
        raise LookupError(f"la clase {material_id} no tiene {PRETEST_SIZE} preguntas disponibles")
    return [
        QuizItem(r["id"], r["stem"], json.loads(r["options_json"]), r["topic"], format=MCQ)
        for r in (rows[0], rows[n // 2], rows[n - 1])
    ]


def record_pretest_answer(
    conn: sqlite3.Connection, question_id: int, chosen_index: int, now: int,
    option_order: list[int] | None = None,
) -> AnswerFeedback:
    """Registra una respuesta del pretest (con commit). No se pregunta confianza.

    Mismos errores que `practice._insert_answer`: `LookupError` si la pregunta no está disponible,
    `ValueError` si la opción o el orden no corresponden.
    """
    feedback = _insert_answer(conn, question_id, chosen_index, now, mode="pretest",
                              confidence=None, option_order=option_order)
    conn.commit()
    if not feedback.correct:
        # Sin confianza no hay "error seguro": la corrección completa, sin alarma.
        feedback.feedback_style = "full"
    return feedback


def close_pretest(conn: sqlite3.Connection, material_id: int, now: int) -> None:
    """Cierra la oferta de esa clase: al terminar el pretest o con "Ahora no". `LookupError` si la
    clase no existe.

    Solo se guardan las clases que todavía están en la ventana de la oferta (actualizadas en los
    últimos 7 días): las otras no se ofrecen igual, así la lista no crece para siempre.
    """
    if conn.execute("SELECT 1 FROM material WHERE id = ?", (material_id,)).fetchone() is None:
        raise LookupError(f"la clase {material_id} no existe")
    closed = _closed(conn)
    if material_id not in closed:
        closed.append(material_id)
    recent = {
        r[0]
        for r in conn.execute(
            f"SELECT id FROM material WHERE updated_at >= ? AND id IN ({','.join('?' * len(closed))})",
            (now - PRETEST_WINDOW_DAYS * 86400, *closed),
        )
    }
    conn.execute(
        "INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (CLOSED_KEY, json.dumps([m for m in closed if m in recent])),
    )
    conn.commit()
