"""Lectura de los parciales viejos con Gemini: un pedido por parcial, contado como "pdf".

De cada parcial se guarda solo su estilo (`past_exam.style_json`: números y temas). Nada del
parcial se escribe en el log: ni su nombre, ni lo que respondió Gemini.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from estudia.ai.base import (
    AIAuthError,
    AIProvider,
    AIQuotaError,
    AIUnavailableError,
    PastExamInput,
)
from estudia.ai.gate import AIBlocked
from estudia.ai.quota import record_usage, remaining
from estudia.generation.pipeline import BUDGET, load_exhausted, next_cap_reset, record_stop, save_exhausted
from estudia.study.exam_style import to_exam_style
from estudia.study.past_exams import UNREADABLE_TEXT, file_path

# Parciales que se mandan a Gemini por corrida (intentos, salgan bien o no): así la lectura de
# los parciales nunca se lleva todo el presupuesto "pdf" de los apuntes del campus. Los que
# quedan se toman en la próxima corrida, que `Jobs` programa enseguida.
MAX_STYLES_PER_RUN = 2

# Cortan la corrida y dejan el parcial `pending` (no es culpa del archivo).
_STOPPERS = (AIQuotaError, AIUnavailableError, AIAuthError, AIBlocked)


def _fail(conn: sqlite3.Connection, past_exam_id: int) -> None:
    conn.rollback()
    conn.execute("UPDATE past_exam SET status = 'failed', error = ?, style_json = NULL WHERE id = ?",
                 (UNREADABLE_TEXT, past_exam_id))
    conn.commit()


def _course_topics(conn: sqlite3.Connection, course_id: int) -> list[str]:
    return [r["name"] for r in conn.execute(
        "SELECT name FROM topic WHERE course_id = ? AND name IS NOT NULL ORDER BY id", (course_id,))]


def extract_pending_styles(conn: sqlite3.Connection, provider: AIProvider, now: float,
                           base_dir: Path) -> int:
    """Lee el estilo de los parciales `pending`, por id, mientras quede presupuesto "pdf" del día
    y hasta MAX_STYLES_PER_RUN intentos.

    Devuelve cuántos quedaron `done`. Un parcial que Gemini no puede leer (o cuyo estilo no
    sirve) queda `failed` con "No se pudo leer". Cuota, Gemini caído, clave rechazada y la puerta
    de edad se propagan y el parcial sigue `pending`. Sin presupuesto, corta con el motivo
    "budget" (vuelve a la medianoche del Pacífico). Los pedidos hechos se suman al uso del día
    en cualquier salida.
    """
    load_exhausted(conn, provider)
    done = attempts = 0
    try:
        rows = conn.execute(
            "SELECT p.id, p.kind, e.course_id FROM past_exam p JOIN exam e ON e.id = p.exam_id"
            " WHERE p.status = 'pending' ORDER BY p.id"
        ).fetchall()
        for row in rows:
            if attempts >= MAX_STYLES_PER_RUN:
                break
            # Lo pedido hasta acá ya cuenta para el presupuesto del día.
            record_usage(conn, provider, now)
            if remaining(conn, "pdf", now) <= 0:
                record_stop(conn, BUDGET, next_cap_reset(now))
                break
            try:
                data = file_path(base_dir, row["id"], row["kind"]).read_bytes()
            except (KeyError, OSError):
                _fail(conn, row["id"])  # sin archivo no hay nada que leer
                continue
            attempts += 1
            try:
                extracted = provider.extract_exam_style(PastExamInput(kind=row["kind"], data=data),
                                                        _course_topics(conn, row["course_id"]))
                style = to_exam_style(conn, row["course_id"], extracted)
            except _STOPPERS:
                conn.rollback()
                raise
            except Exception:  # noqa: BLE001 - un parcial raro no traba la cola (y no se loguea)
                # Ilegible (AIFormatError, AIDocumentUnreadableError), respuesta inválida, sin
                # ejercicios (ValueError) o cualquier otra cosa: "No se pudo leer".
                _fail(conn, row["id"])
                continue
            conn.execute("UPDATE past_exam SET status = 'done', error = NULL, style_json = ? WHERE id = ?",
                         (style.model_dump_json(), row["id"]))
            conn.commit()
            done += 1
    finally:
        record_usage(conn, provider, now)
        save_exhausted(conn, provider)
    return done
