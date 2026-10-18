"""Preguntas con el estilo del examen: las que le faltan al simulacro de la cátedra, en tandas.

Se generan solo con el material de la clase ya verificado (los resúmenes de los temas) y pasan
por exactamente la misma verificación que cualquier otra pregunta:
- `_passes_local_checks`: opciones distintas, SymPy confirma la marcada y ningún distractor, y
  ningún distractor entra en la tolerancia de la respuesta escrita;
- la pasada a ciegas, con un modelo que no es el que las generó.
Se guarda solo lo que coincide a ciegas. Quedan con `origin='parcial_propio'`: nunca salen de
la compu ni aparecen en la práctica de siempre.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from estudia.ai.base import (
    AIAuthError,
    AIFormatError,
    AIProvider,
    AIQuotaError,
    AIUnavailableError,
    StyleQuestion,
    StyleRequest,
)
from estudia.ai.gate import AIBlocked
from estudia.ai.quota import can_analyze, quota_day, record_usage
from estudia.config import LEGACY_ANALYSIS_MODEL
from estudia.generation.pipeline import (
    _passes_local_checks,
    awaiting_blind_count,
    blind_matches,
    collapse_spaces,
    load_exhausted,
    save_exhausted,
    student_question,
)
from estudia.study.exam_style import (
    EXERCISE_KINDS,
    ExamStyle,
    map_topics,
    saved_style,
    style_gaps,
    style_revision,
    style_topic_ids,
)

STYLE_BATCH = 10
STYLE_BATCHES_PER_DAY = 3
STYLE_SOURCE_MAX_CHARS = 60_000
ORIGIN = "parcial_propio"
# kv "style_batches:<examen>:<AAAA-MM-DD del Pacífico>" = tandas pedidas ese día para ese examen.
BATCHES_PREFIX = "style_batches:"


# Cortan la corrida (se propagan).
_STOPPERS = (AIQuotaError, AIUnavailableError, AIAuthError, AIBlocked)


def style_dedupe_key(exam_id: int, stem: str, *, revision: int) -> str:
    """La clave de una pregunta de estilo: por examen (no tiene material), revisión del estilo
    (`exam.style_revision`: sube al borrarlo) y enunciado normalizado. Con la revisión, un estilo
    borrado y guardado de nuevo puede volver a generar un enunciado que quedó retirado."""
    stem_key = collapse_spaces(stem).lower()
    if revision:
        return hashlib.sha1(f"style:{exam_id}:{revision}:{stem_key}".encode()).hexdigest()
    return hashlib.sha1(f"style:{exam_id}:{stem_key}".encode()).hexdigest()


def style_material_text(conn: sqlite3.Connection, topic_ids: list[int]) -> str:
    """Los resúmenes de los materiales listos (`done`) de esos temas, de secciones visibles, en
    bloques "## {título}\\n{resumen}", cortados en STYLE_SOURCE_MAX_CHARS. "" si no hay ninguno."""
    if not topic_ids:
        return ""
    rows = conn.execute(
        "SELECT m.title, m.summary_md FROM material m JOIN section s ON s.id = m.section_id"
        " WHERE m.status = 'done' AND s.visible = 1"
        " AND COALESCE(TRIM(m.summary_md), '') != ''"
        f" AND m.id IN (SELECT material_id FROM material_topic WHERE topic_id IN ({','.join('?' * len(topic_ids))}))"
        " ORDER BY m.id", list(topic_ids)).fetchall()
    text = "\n\n".join(f"## {r['title'] or 'Material'}\n{r['summary_md'].strip()}" for r in rows)
    return text[:STYLE_SOURCE_MAX_CHARS]


def _batches_key(exam_id: int, now: float) -> str:
    return f"{BATCHES_PREFIX}{exam_id}:{quota_day(now).isoformat()}"


def _batches_today(conn: sqlite3.Connection, exam_id: int, now: float) -> int:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (_batches_key(exam_id, now),)).fetchone()
    try:
        return max(0, int(row[0])) if row else 0
    except ValueError:
        return 0


def _count_batch(conn: sqlite3.Connection, exam_id: int, now: float) -> None:
    """Cuenta la tanda antes de pedirla y hace commit."""
    _set_batches(conn, exam_id, now, _batches_today(conn, exam_id, now) + 1)


def _uncount_batch(conn: sqlite3.Connection, exam_id: int, now: float) -> None:
    """Devuelve la tanda: la corrida se cortó sin que saliera el pedido de generación."""
    _set_batches(conn, exam_id, now, max(0, _batches_today(conn, exam_id, now) - 1))


def _set_batches(conn: sqlite3.Connection, exam_id: int, now: float, count: int) -> None:
    conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)",
                 (_batches_key(exam_id, now), str(count)))
    conn.commit()


def _prune_batches(conn: sqlite3.Connection, now: float) -> None:
    """Borra los contadores de tandas que no son de hoy (el mismo criterio que `prune_usage`)."""
    conn.execute("DELETE FROM kv WHERE key LIKE ? AND key NOT LIKE ?",
                 (BATCHES_PREFIX + "%", f"%:{quota_day(now).isoformat()}"))
    conn.commit()


def _style_exams(conn: sqlite3.Connection, now: float) -> list[sqlite3.Row]:
    """Exámenes con estilo guardado, con fecha futura (o abiertos) o sin fecha; el más próximo primero."""
    return conn.execute(
        "SELECT e.id, e.course_id, (SELECT MIN(c.fullname) FROM course c WHERE c.id = e.course_id)"
        " AS course FROM exam e"
        " WHERE e.style_json IS NOT NULL AND COALESCE(e.kind, 'parcial') != 'inscripcion'"
        " AND (e.opens_at IS NULL OR COALESCE(NULLIF(e.closes_at, 0), e.opens_at) > ?)"
        " ORDER BY e.opens_at IS NULL, e.opens_at, e.id", (now,)).fetchall()


def _batch_kinds(gaps: dict[str, int]) -> dict[str, int]:
    """Hasta STYLE_BATCH de lo que falta, en el orden de EXERCISE_KINDS."""
    kinds: dict[str, int] = {}
    left = STYLE_BATCH
    for kind in EXERCISE_KINDS:
        take = min(gaps.get(kind, 0), left)
        if take > 0:
            kinds[kind] = take
            left -= take
    return kinds


def _candidates(questions: list[StyleQuestion], kinds: dict[str, int]) -> list[StyleQuestion]:
    """Las que pasan lo que se verifica gratis y son de un tipo pedido, hasta lo pedido de cada uno."""
    taken = dict.fromkeys(kinds, 0)
    result = []
    for q in questions:
        if q.kind not in kinds or taken[q.kind] >= kinds[q.kind]:
            continue
        if len(q.options) != 4 or not q.stem.strip() or not _passes_local_checks(q):
            continue
        taken[q.kind] += 1
        result.append(q)
    return result


def _store(conn: sqlite3.Connection, exam_id: int, course_id: int, topics: list[int],
           kept: list[StyleQuestion]) -> int:
    """Guarda las que coincidieron a ciegas, en una transacción. Devuelve cuántas entraron."""
    revision = style_revision(conn, exam_id)
    stored = 0
    try:
        for q in kept:
            ids, _ = map_topics(conn, course_id, [q.topic])
            tid = next((t for t in ids if t in topics), topics[0])
            # Se verificó con el texto original; se guarda en notación de clase.
            text = student_question(q.stem, q.options, q.explanation, q.common_error)
            if text is None:
                continue
            check_expr = q.check_expr.strip() if q.check_expr and q.check_expr.strip() else None
            cur = conn.execute(
                "INSERT OR IGNORE INTO question(material_id, topic_id, stem, options_json,"
                " correct_index, explanation, common_error, source_seconds, source_page,"
                " check_expr, verified, retired, dedupe_key, origin, style_exam_id, style_kind)"
                " VALUES (NULL,?,?,?,?,?,?,NULL,NULL,?,1,0,?,?,?,?)",
                (tid, text.stem, json.dumps(text.options, ensure_ascii=False), q.correct_index,
                 text.explanation, text.common_error, check_expr,
                 style_dedupe_key(exam_id, text.stem, revision=revision), ORIGIN, exam_id, q.kind),
            )
            stored += cur.rowcount  # 0 si ya estaba (mismo enunciado)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return stored


def _analysis_sent(provider: AIProvider) -> int:
    return (getattr(provider, "requests", None) or {}).get("analysis", 0)


def _run_batch(conn: sqlite3.Connection, provider: AIProvider, exam, style: ExamStyle,
               topics: list[int], material: str, kinds: dict[str, int], now: float) -> int:
    """Pide una tanda (ya contada), la verifica y guarda lo que pasa.

    Una respuesta ilegible (de la generación o de la pasada a ciegas) descarta la tanda entera,
    que queda contada. Los cortes se propagan sin guardar nada; si cortan la generación antes de
    que salga el pedido (todos los modelos agotados, sin conexión, la puerta de edad), la tanda
    se devuelve: no se gastó nada.
    """
    by_id = dict(conn.execute(
        f"SELECT id, name FROM topic WHERE id IN ({','.join('?' * len(topics))})", topics).fetchall())
    names = [by_id[t] for t in topics if by_id.get(t)]
    request = StyleRequest(course=exam["course"] or "", kinds=kinds, topics=names,
                           material_text=material, formula_sheet=style.formula_sheet)
    sent_before = _analysis_sent(provider)
    try:
        questions = provider.generate_style_questions(request)
    except AIFormatError:
        return 0
    except _STOPPERS:
        if _analysis_sent(provider) <= sent_before:
            _uncount_batch(conn, exam["id"], now)
        raise
    generator = provider.last_analysis_model or LEGACY_ANALYSIS_MODEL
    candidates = _candidates(questions, kinds)
    if not candidates:
        return 0
    try:
        matches = blind_matches(provider, candidates, exclude_models={generator})
    except AIFormatError:
        return 0
    kept = [q for q, ok in zip(candidates, matches) if ok]
    return _store(conn, exam["id"], exam["course_id"], topics, kept)


def fill_style_gaps(conn: sqlite3.Connection, provider: AIProvider, now: float) -> int:
    """Genera lo que les falta a los exámenes con estilo guardado; devuelve cuántas guardó.

    Va por los exámenes con fecha futura (o sin fecha), el más próximo primero. Antes de cada
    tanda hace falta presupuesto "analysis" y la reserva de la pasada a ciegas (`can_analyze`),
    menos de STYLE_BATCHES_PER_DAY tandas hoy para ese examen y material de sus temas; si falta
    algo, sigue con el próximo examen. La tanda se cuenta antes de pedirla (y se devuelve si la
    corrida se corta antes de que salga el pedido de generación).

    Cuota, Gemini caído, clave rechazada y la puerta de edad se propagan (lo pedido igual se
    suma al uso del día).
    """
    load_exhausted(conn, provider)
    saved = 0
    try:
        for exam in _style_exams(conn, now):
            style = saved_style(conn, exam["id"])
            if style is None:
                continue
            topics = style_topic_ids(conn, exam["id"], style)
            material = style_material_text(conn, topics)
            while True:
                # Lo pedido hasta acá ya cuenta para el presupuesto del día.
                record_usage(conn, provider, now)
                kinds = _batch_kinds(style_gaps(conn, exam["id"]))
                if (not kinds or not material
                        or _batches_today(conn, exam["id"], now) >= STYLE_BATCHES_PER_DAY
                        or not can_analyze(conn, now, awaiting_blind_count(conn))):
                    break
                _count_batch(conn, exam["id"], now)
                saved += _run_batch(conn, provider, exam, style, topics, material, kinds, now)
    finally:
        record_usage(conn, provider, now)
        _prune_batches(conn, now)
        save_exhausted(conn, provider)
    return saved
