"""Tubería de generación: video -> resumen + preguntas verificadas.

Regla central: una pregunta solo se guarda (verified=1) si sus opciones son
distintas, la respuesta a ciegas coincide con `correct_index` y, cuando hay
`check_expr`, SymPy confirma la opción marcada y ninguna otra. Todo lo demás se
descarta y no se guarda. La pasada a ciegas va en un solo pedido por material y con
otro modelo que el que hizo el análisis.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass

from pydantic import ValidationError

from estudia.ai.base import (
    AIAuthError,
    AIFormatError,
    AIProvider,
    AIQuotaError,
    AIUnavailableError,
    AIVideoUnavailableError,
    VideoAnalysis,
    option_key,
)
from estudia.config import LEGACY_ANALYSIS_MODEL, MAX_EXHAUSTED_S
from estudia.generation.verify import check_math_tristate

STOP_REASONS = ((AIQuotaError, "quota"), (AIUnavailableError, "unavailable"), (AIAuthError, "auth"))
FORMAT_ERROR_TEXT = "La IA devolvió una respuesta con formato inválido."
VIDEO_ERROR_TEXT = "Gemini no pudo abrir este video (puede ser privado o no listado)."
GENERIC_ERROR_TEXT = "No se pudo preparar esta clase por un error inesperado."
STOP_KEY = "ai_stop_reason"
# Epoch en que vuelve el primer modelo agotado por el día (si se cortó por cuota).
QUOTA_RESET_KEY = "ai_quota_resets_at"
# kv "model_exhausted:<key_id>:<modelo>" = epoch hasta el que ese modelo no tiene cuota
# con esa clave (key_id: huella corta de la clave, nunca la clave).
EXHAUSTED_PREFIX = "model_exhausted:"
# "denied" si Gemini no acepta enlaces de YouTube con esta clave: se sube el video.
URL_INPUT_KEY = "youtube_url_input"
# material.fail_kind: por qué falló, para decidir qué reintentar.
FAIL_FORMAT, FAIL_VIDEO, FAIL_ERROR = "format", "video", "error"
# Con una clave nueva vale la pena reintentar lo que pudo fallar por la IA; un video
# privado sigue privado. NULL = falló antes de existir fail_kind.
RETRY_ON_NEW_KEY = (FAIL_FORMAT, FAIL_ERROR)

log = logging.getLogger(__name__)


@dataclass
class ProcessResult:
    kept: int
    discarded: int
    status: str = "done"  # done | failed


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def slugify(name: str) -> str:
    """Minúsculas, sin tildes, palabras unidas por guiones."""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _dedupe_key(material_id: int, stem: str) -> str:
    return hashlib.sha1(f"{material_id}:{_collapse(stem).lower()}".encode()).hexdigest()


def _passes_local_checks(question) -> bool:
    """Lo que se verifica gratis: opciones distintas y, con `check_expr`, que valga la
    marcada y ninguna otra."""
    options = question.options
    if not 0 <= question.correct_index < len(options):
        return False
    keys = [option_key(o) for o in options]
    if len(set(keys)) != len(keys):
        return False
    expr = question.check_expr
    if expr is not None and expr.strip():
        # La marcada tiene que estar probada igual; ante la duda (None) se descarta.
        if check_math_tristate(expr, options[question.correct_index]) is not True:
            return False
        # Un distractor equivalente ("1/2" y "0,5") haría dos respuestas correctas:
        # cada uno tiene que estar probado distinto. Un tiempo agotado no alcanza.
        for i, option in enumerate(options):
            if i != question.correct_index and check_math_tristate(expr, option) is not False:
                return False
    return True


def blind_matches(provider: AIProvider, questions: list, exclude_models) -> list[bool]:
    """Pasada a ciegas en un pedido: True para cada pregunta que el modelo acierta.

    Solo enunciado y opciones. Si no vuelve una respuesta por pregunta no se puede
    saber cuál es cuál: ninguna pasa. Deja pasar Quota/Unavailable/Auth/Format.
    """
    if not questions:
        return []
    answers = provider.answer_blind_batch(
        [(q.stem, list(q.options)) for q in questions], exclude_models=exclude_models
    )
    if len(answers) != len(questions):
        return [False] * len(questions)
    return [a is not None and a == q.correct_index for q, a in zip(questions, answers)]


def _verified(provider: AIProvider, analysis: VideoAnalysis, analysis_model: str) -> list:
    """Las preguntas que pasan todo: primero lo local (gratis), después la pasada a
    ciegas con otro modelo que el del análisis."""
    candidates = [q for q in analysis.questions if _passes_local_checks(q)]
    matches = blind_matches(provider, candidates, (analysis_model,))
    return [q for q, ok in zip(candidates, matches) if ok]


def _set_status(conn, material_id: int, status: str, summary_md=None, error=None,
                fail_kind=None) -> None:
    """Cierra el material (done/failed): el análisis guardado ya no hace falta.

    Con `done` queda anotado qué modelo hizo el análisis (para verificar de nuevo con otro).
    """
    conn.execute(
        "UPDATE material SET status=?, summary_md=?, error=?, fail_kind=?, analysis_json=NULL,"
        " analysis_model=CASE WHEN ?='done' THEN analysis_model END, updated_at=? WHERE id=?",
        (status, summary_md, error, fail_kind, status, int(time.time()), material_id),
    )


def _analysis(conn, provider: AIProvider, material_id: int,
              row) -> tuple[VideoAnalysis, str]:
    """El análisis del video y el modelo que lo hizo; si una corrida anterior ya lo
    pagó, se reusa.

    Se guarda apenas llega, antes de la pasada a ciegas: si esa pasada se corta por
    cuota, la próxima corrida no vuelve a analizar el video. El modelo se guarda para
    que la pasada a ciegas no lo use.
    """
    if row["analysis_json"]:
        try:
            cached = VideoAnalysis.model_validate_json(row["analysis_json"])
            # Sin modelo anotado: es de antes de anotarlo, cuando había uno solo.
            return cached, row["analysis_model"] or LEGACY_ANALYSIS_MODEL
        except ValidationError:
            pass  # guardado roto: se vuelve a pedir
    analysis = provider.analyze_video(row["ref"], row["title"])
    model = provider.last_analysis_model or LEGACY_ANALYSIS_MODEL
    conn.execute(
        "UPDATE material SET analysis_json=?, analysis_model=? WHERE id=?",
        (analysis.model_dump_json(), model, material_id),
    )
    conn.commit()
    return analysis, model


def _topic_id(conn, course_id: int, name: str) -> int | None:
    slug = slugify(name)
    if not slug:
        return None
    conn.execute(
        "INSERT OR IGNORE INTO topic(course_id, slug, name) VALUES (?, ?, ?)",
        (course_id, slug, _collapse(name)),
    )
    return conn.execute(
        "SELECT id FROM topic WHERE course_id=? AND slug=?", (course_id, slug)
    ).fetchone()[0]


def process_material(conn: sqlite3.Connection, provider: AIProvider, material_id: int) -> ProcessResult:
    """Procesa un material. Quota/Unavailable/Auth se propagan sin guardar nada."""
    row = conn.execute(
        "SELECT m.ref, m.title, m.analysis_json, m.analysis_model, s.course_id FROM material m"
        " JOIN section s ON s.id = m.section_id WHERE m.id=?",
        (material_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"material {material_id} inexistente")
    try:
        analysis, analysis_model = _analysis(conn, provider, material_id, row)
        kept = _verified(provider, analysis, analysis_model)
    except AIVideoUnavailableError:
        _set_status(conn, material_id, "failed", error=VIDEO_ERROR_TEXT, fail_kind=FAIL_VIDEO)
        conn.commit()
        return ProcessResult(0, 0, "failed")
    except AIFormatError:
        _set_status(conn, material_id, "failed", error=FORMAT_ERROR_TEXT, fail_kind=FAIL_FORMAT)
        conn.commit()
        return ProcessResult(0, 0, "failed")

    # Recién acá se escribe: preguntas y `done` van en una sola transacción.
    course_id = row["course_id"]
    stored = 0
    try:
        analysis_tids = []
        for name in analysis.topics:
            tid = _topic_id(conn, course_id, name)
            if tid is not None:
                analysis_tids.append(tid)
                conn.execute(
                    "INSERT OR IGNORE INTO material_topic(material_id, topic_id) VALUES (?, ?)",
                    (material_id, tid),
                )
        for q in kept:
            tid = _topic_id(conn, course_id, q.topic)
            if tid is not None:
                # El tema de la pregunta puede no estar en `topics`: igual queda ligado a la clase.
                conn.execute(
                    "INSERT OR IGNORE INTO material_topic(material_id, topic_id) VALUES (?, ?)",
                    (material_id, tid),
                )
            elif analysis_tids:
                # Tema en blanco o sin letras ni números: va al primer tema de la clase.
                tid = analysis_tids[0]
            else:
                continue  # sin tema nunca aparecería en un cuestionario
            check_expr = q.check_expr.strip() if q.check_expr and q.check_expr.strip() else None
            cur = conn.execute(
                "INSERT OR IGNORE INTO question(material_id, topic_id, stem, options_json,"
                " correct_index, explanation, common_error, source_seconds, check_expr,"
                " verified, retired, dedupe_key) VALUES (?,?,?,?,?,?,?,?,?,1,0,?)",
                (
                    material_id, tid, q.stem, json.dumps(q.options, ensure_ascii=False),
                    q.correct_index, q.explanation, q.common_error, q.source_seconds,
                    check_expr, _dedupe_key(material_id, q.stem),
                ),
            )
            stored += cur.rowcount  # 0 si ya estaba (mismo enunciado)
        _set_status(conn, material_id, "done", summary_md=analysis.summary_md)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return ProcessResult(stored, len(analysis.questions) - stored)


def retry_failed(conn: sqlite3.Connection, kinds: tuple[str, ...] | None = None) -> int:
    """Vuelve a poner en cola los materiales `failed`; devuelve cuántos.

    Con `kinds`, solo los que fallaron por esos motivos (y los de antes de fail_kind).
    """
    sql = ("UPDATE material SET status='pending', error=NULL, fail_kind=NULL, analysis_json=NULL,"
           " analysis_model=NULL, updated_at=? WHERE status='failed'")
    params: list = [int(time.time())]
    if kinds is not None:
        sql += f" AND (fail_kind IS NULL OR fail_kind IN ({','.join('?' * len(kinds))}))"
        params.extend(kinds)
    cur = conn.execute(sql, params)
    conn.commit()
    return cur.rowcount


def process_pending(conn: sqlite3.Connection, provider: AIProvider, budget: int = 10) -> int:
    """Procesa hasta `budget` materiales pendientes; devuelve cuántos quedaron `done`.

    Orden: secciones visibles por `num`, luego id de material. Ante
    Quota/Unavailable/Auth se detiene y guarda el motivo en kv["ai_stop_reason"]
    (y, por cuota, cuándo vuelve el primer modelo en kv["ai_quota_resets_at"]).
    Los modelos agotados por el día se leen y se guardan en kv["model_exhausted:<clave>:*"].
    Cualquier otra excepción marca ese material `failed` (con un texto genérico)
    y sigue con el próximo.
    """
    conn.execute("DELETE FROM kv WHERE key IN (?, ?)", (STOP_KEY, QUOTA_RESET_KEY))
    conn.commit()
    if budget <= 0:
        return 0  # (LIMIT negativo en SQLite sería "sin límite")
    ids = [
        r[0]
        for r in conn.execute(
            "SELECT m.id FROM material m JOIN section s ON s.id = m.section_id"
            " WHERE m.status='pending' AND s.visible=1"
            " ORDER BY s.num, m.id LIMIT ?",
            (budget,),
        )
    ]
    row = conn.execute("SELECT value FROM kv WHERE key=?", (URL_INPUT_KEY,)).fetchone()
    if row and row[0] == "denied":
        provider.youtube_url_denied = True
    load_exhausted(conn, provider)
    finished = 0
    try:
        for material_id in ids:
            try:
                result = process_material(conn, provider, material_id)
            except (AIQuotaError, AIUnavailableError, AIAuthError) as exc:
                reason = next(r for cls, r in STOP_REASONS if isinstance(exc, cls))
                conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)", (STOP_KEY, reason))
                resets_at = getattr(exc, "resets_at", None)
                if resets_at is not None:
                    conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)",
                                 (QUOTA_RESET_KEY, str(int(resets_at))))
                conn.commit()
                break
            except Exception as exc:  # noqa: BLE001 - un material roto no traba la cola
                # Solo el tipo: el mensaje podría traer datos del proveedor.
                log.warning("Falló el material %s (%s)", material_id, type(exc).__name__)
                conn.rollback()
                _set_status(conn, material_id, "failed", error=GENERIC_ERROR_TEXT, fail_kind=FAIL_ERROR)
                conn.commit()
                continue
            if result.status == "done":
                finished += 1
    finally:
        if provider.youtube_url_denied:
            # La próxima corrida sube el video directo, sin probar el enlace.
            conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?, 'denied')", (URL_INPUT_KEY,))
            conn.commit()
        save_exhausted(conn, provider)
    return finished


def _marks_prefix(provider: AIProvider) -> str:
    return f"{EXHAUSTED_PREFIX}{provider.key_id}:"


def load_exhausted(conn: sqlite3.Connection, provider: AIProvider) -> None:
    """Pasa al proveedor los modelos todavía agotados con su clave (de corridas anteriores).

    Una marca más lejana que el tope no puede ser la cuota diaria: se ignora.
    """
    now = time.time()
    prefix = _marks_prefix(provider)
    for key, value in conn.execute("SELECT key, value FROM kv WHERE key GLOB ?",
                                   (prefix + "*",)).fetchall():
        try:
            until = float(value)
        except (TypeError, ValueError):
            continue
        model = key.removeprefix(prefix)
        if now < until <= now + MAX_EXHAUSTED_S and until > provider.exhausted_until.get(model, 0.0):
            provider.exhausted_until[model] = until


def save_exhausted(conn: sqlite3.Connection, provider: AIProvider) -> None:
    """Guarda las marcas vigentes de esta clave (acotadas al tope) y borra las vencidas.

    Solo toca las de su clave: una corrida vieja que termina después de cambiar la
    clave no pisa las marcas de la nueva. También borra las del formato sin clave.
    """
    now = time.time()
    prefix = _marks_prefix(provider)
    conn.execute("DELETE FROM kv WHERE key GLOB ? OR (key GLOB ? AND key NOT GLOB ?)",
                 (prefix + "*", EXHAUSTED_PREFIX + "*", EXHAUSTED_PREFIX + "*:*"))
    for model, until in provider.exhausted_until.items():
        if until > now:
            conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)",
                         (prefix + model, str(int(min(until, now + MAX_EXHAUSTED_S)))))
    conn.commit()


def forget_exhausted_models(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM kv WHERE key GLOB ?", (EXHAUSTED_PREFIX + "*",))
    conn.commit()
