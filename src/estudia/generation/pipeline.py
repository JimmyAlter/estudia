"""Tubería de generación: video -> resumen + preguntas verificadas.

Regla central: una pregunta solo se guarda (verified=1) si sus opciones son
distintas, la respuesta a ciegas coincide con `correct_index` y, cuando hay
`check_expr`, SymPy confirma la opción marcada y ninguna otra. Todo lo demás se
descarta y no se guarda. La pasada a ciegas va en un solo pedido por material y con
otro modelo que el que hizo el análisis.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
import sqlite3
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from estudia.ai.base import (
    AIAuthError,
    AIFormatError,
    AINeedsPublicLinkError,
    AIProvider,
    AIQuotaError,
    AIUnavailableError,
    AIVideoUnavailableError,
    VideoAnalysis,
    option_key,
)
from estudia.config import (
    DAILY_VIDEO_CAP_S,
    LEGACY_ANALYSIS_MODEL,
    MAX_EXHAUSTED_S,
    VIDEO_ESTIMATE_S,
    YOUTUBE_URL_DENIED_S,
)
from estudia.generation.notation import sentence_case, student_math
from estudia.ai.gate import AIBlocked
from estudia.generation.verify import check_math_tristate, typed_accepts_option

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
# Epoch hasta el que no se prueba el enlace de YouTube (Gemini lo rechazó con esta
# clave). Antes valía "denied" para siempre: ahora vence a los 7 días.
URL_INPUT_KEY = "youtube_url_input"
LEGACY_DENIED = "denied"
# "1" si el estudiante activó la descarga del video (yt-dlp + subida) en Ajustes.
ALLOW_DOWNLOAD_KEY = "allow_download"
# El video no se pudo leer por el enlace y la descarga está apagada.
NEEDS_LINK = "needs_public_link"
NEEDS_LINK_TEXT = ("Este video no es público. Pedile a la cátedra el archivo en el campus,"
                   " o activá la descarga en Ajustes")
# Tope diario de YouTube (copia el límite del plan gratuito para videos por enlace). El tope
# es de Gemini, así que su día es el de la cuota de Gemini (Pacífico, como rotation.py);
# todo lo demás usa el día de Buenos Aires (AAAA-MM-DD):
# - kv "youtube_seconds:<día del Pacífico>" = segundos de video que Gemini leyó por enlace;
# - kv "youtube_measured:<día del Pacífico>" = "<cantidad>:<segundos>" de las duraciones medidas;
# - kv "youtube_denied_videos:<día de Buenos Aires>" = ids (JSON) cuyo enlace rechazó toda la cadena.
VIDEO_DAY_PREFIX = "youtube_seconds:"
MEASURED_PREFIX = "youtube_measured:"
DENIED_VIDEOS_PREFIX = "youtube_denied_videos:"
DAY_PREFIXES = (VIDEO_DAY_PREFIX, MEASURED_PREFIX, DENIED_VIDEOS_PREFIX)
CAP_PREFIXES = (VIDEO_DAY_PREFIX, MEASURED_PREFIX)
# Motivo de corte propio: se llegó al tope de YouTube del día (vuelve a la medianoche del Pacífico).
YOUTUBE_CAP = "youtube_cap"
LOCAL_TZ = ZoneInfo("America/Argentina/Buenos_Aires")
CAP_TZ = ZoneInfo("America/Los_Angeles")   # la zona del día de la cuota de Gemini (rotation.py)
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
    status: str = "done"  # done | failed | needs_public_link


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def slugify(name: str) -> str:
    """Minúsculas, sin tildes, palabras unidas por guiones."""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def dedupe_key(material_id: int, stem: str) -> str:
    return hashlib.sha1(f"{material_id}:{_collapse(stem).lower()}".encode()).hexdigest()


def options_distinct(options: list[str]) -> bool:
    keys = [option_key(o) for o in options]
    return len(set(keys)) == len(keys)


@dataclass
class StudentText:
    """Lo que ve el estudiante de una pregunta, ya en notación de clase."""

    stem: str
    options: list[str]
    explanation: str
    common_error: str


def student_question(stem: str, options: list[str], explanation: str,
                     common_error: str) -> StudentText | None:
    """Pasa a notación de clase el texto de una pregunta (no `check_expr`).

    None si dos opciones quedan iguales: la pregunta tendría dos respuestas iguales.
    """
    text = StudentText(student_math(stem), [student_math(o) for o in options],
                       student_math(explanation), student_math(common_error))
    return text if options_distinct(text.options) else None


def _passes_local_checks(question) -> bool:
    """Lo que se verifica gratis: opciones distintas y, con `check_expr`, que valga la
    marcada y ninguna otra."""
    options = question.options
    if not 0 <= question.correct_index < len(options):
        return False
    if not options_distinct(options):
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
        # Y ninguno puede ser aceptado por la regla de la respuesta escrita ("1.41" por √2):
        # la opción múltiple lo daría por mal y la escrita por bien. Ante la duda, se descarta.
        for i, option in enumerate(options):
            if i != question.correct_index and typed_accepts_option(expr, option) is not False:
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


def download_allowed(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT value FROM kv WHERE key=?", (ALLOW_DOWNLOAD_KEY,)).fetchone()
    return row is not None and row[0] == "1"


def local_day(now: float) -> dt.date:
    return dt.datetime.fromtimestamp(now, LOCAL_TZ).date()


def cap_day(now: float) -> dt.date:
    """El día del tope de YouTube: el de la cuota de Gemini (Pacífico)."""
    return dt.datetime.fromtimestamp(now, CAP_TZ).date()


def next_cap_reset(now: float) -> float:
    """Cuándo vuelve el tope de YouTube: la medianoche del Pacífico, como la cuota de Gemini."""
    day = cap_day(now) + dt.timedelta(days=1)
    return dt.datetime(day.year, day.month, day.day, tzinfo=CAP_TZ).timestamp()


def _day_key(prefix: str, now: float) -> str:
    day = cap_day(now) if prefix in CAP_PREFIXES else local_day(now)
    return f"{prefix}{day.isoformat()}"


def _kv_text(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _kv_put(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)", (key, value))


def video_seconds_today(conn: sqlite3.Connection, now: float) -> float:
    try:
        return float(_kv_text(conn, _day_key(VIDEO_DAY_PREFIX, now)) or 0)
    except ValueError:
        return 0.0


def _measured_today(conn: sqlite3.Connection, now: float) -> tuple[int, float]:
    try:
        count, total = (_kv_text(conn, _day_key(MEASURED_PREFIX, now)) or "0:0").split(":")
        return int(count), float(total)
    except ValueError:
        return 0, 0.0


def video_estimate(conn: sqlite3.Connection, now: float) -> float:
    """Duración que se supone antes de mandar un video: el promedio de las medidas hoy
    o, sin medidas, VIDEO_ESTIMATE_S. El campus no da la duración del video."""
    count, total = _measured_today(conn, now)
    return total / count if count and total > 0 else float(VIDEO_ESTIMATE_S)


def _count_link_read(conn: sqlite3.Connection, provider: AIProvider, now: float) -> None:
    """Suma al tope del día el video que Gemini leyó por enlace (aunque la respuesta no
    haya servido): lo que informó Gemini o, si no informó, lo supuesto. Uno subido no cuenta."""
    if not getattr(provider, "last_link_sent", False):
        return
    estimate = video_estimate(conn, now)
    seconds = getattr(provider, "last_video_seconds", None)
    if seconds:
        count, total = _measured_today(conn, now)
        _kv_put(conn, _day_key(MEASURED_PREFIX, now), f"{count + 1}:{round(total + seconds)}")
    used = video_seconds_today(conn, now) + (seconds or estimate)
    _kv_put(conn, _day_key(VIDEO_DAY_PREFIX, now), str(round(used)))


def _link_denied(provider: AIProvider, now: float) -> bool:
    return (getattr(provider, "youtube_url_denied_until", 0.0) or 0.0) > now


def _cap_reached(conn: sqlite3.Connection, now: float) -> bool:
    """¿Mandar otro video por enlace pasaría el tope del día? El primero va siempre."""
    used = video_seconds_today(conn, now)
    return used > 0 and used + video_estimate(conn, now) > DAILY_VIDEO_CAP_S


def _analysis(conn, provider: AIProvider, material_id: int,
              row, clock: Callable[[], float] = time.time) -> tuple[VideoAnalysis, str]:
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
    try:
        analysis = provider.analyze_video(row["ref"], row["title"])
    except AIFormatError:
        # Una respuesta inservible igual gastó el video del día si fue por enlace.
        _count_link_read(conn, provider, clock())
        conn.commit()
        raise
    model = provider.last_analysis_model or LEGACY_ANALYSIS_MODEL
    conn.execute(
        "UPDATE material SET analysis_json=?, analysis_model=? WHERE id=?",
        (analysis.model_dump_json(), model, material_id),
    )
    _count_link_read(conn, provider, clock())
    conn.commit()
    return analysis, model


def _topic_id(conn, course_id: int, name: str) -> int | None:
    slug = slugify(name)
    if not slug:
        return None
    # El slug no distingue mayúsculas: "Función Lineal" y "Función lineal" son el mismo tema.
    conn.execute(
        "INSERT OR IGNORE INTO topic(course_id, slug, name) VALUES (?, ?, ?)",
        (course_id, slug, sentence_case(name)),
    )
    return conn.execute(
        "SELECT id FROM topic WHERE course_id=? AND slug=?", (course_id, slug)
    ).fetchone()[0]


def process_material(conn: sqlite3.Connection, provider: AIProvider, material_id: int,
                     clock: Callable[[], float] = time.time) -> ProcessResult:
    """Procesa un material. Quota/Unavailable/Auth se propagan sin guardar nada."""
    row = conn.execute(
        "SELECT m.ref, m.title, m.analysis_json, m.analysis_model, s.course_id FROM material m"
        " JOIN section s ON s.id = m.section_id WHERE m.id=?",
        (material_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"material {material_id} inexistente")
    try:
        analysis, analysis_model = _analysis(conn, provider, material_id, row, clock)
        kept = _verified(provider, analysis, analysis_model)
    except AINeedsPublicLinkError:
        _set_status(conn, material_id, NEEDS_LINK, error=NEEDS_LINK_TEXT)
        conn.commit()
        return ProcessResult(0, 0, NEEDS_LINK)
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
            # Se verificó con el texto original; se guarda en notación de clase.
            text = student_question(q.stem, q.options, q.explanation, q.common_error)
            if text is None:
                continue
            check_expr = q.check_expr.strip() if q.check_expr and q.check_expr.strip() else None
            cur = conn.execute(
                "INSERT OR IGNORE INTO question(material_id, topic_id, stem, options_json,"
                " correct_index, explanation, common_error, source_seconds, check_expr,"
                " verified, retired, dedupe_key) VALUES (?,?,?,?,?,?,?,?,?,1,0,?)",
                (
                    material_id, tid, text.stem, json.dumps(text.options, ensure_ascii=False),
                    q.correct_index, text.explanation, text.common_error, q.source_seconds,
                    check_expr, dedupe_key(material_id, text.stem),
                ),
            )
            stored += cur.rowcount  # 0 si ya estaba (mismo enunciado)
        _set_status(conn, material_id, "done", summary_md=student_math(analysis.summary_md))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return ProcessResult(stored, len(analysis.questions) - stored)


def retry_failed(conn: sqlite3.Connection, kinds: tuple[str, ...] | None = None) -> int:
    """Vuelve a poner en cola los materiales `failed` y los que piden un enlace público;
    devuelve cuántos.

    Con `kinds`, de los `failed` solo los que fallaron por esos motivos (y los de antes de
    fail_kind). Los que piden enlace vuelven siempre: si el enlace sigue rechazado y la
    descarga apagada, vuelven a quedar así sin gastar un pedido.
    """
    failed = "status='failed'"
    params: list = [int(time.time())]
    if kinds is not None:
        failed += f" AND (fail_kind IS NULL OR fail_kind IN ({','.join('?' * len(kinds))}))"
        params.extend(kinds)
    sql = ("UPDATE material SET status='pending', error=NULL, fail_kind=NULL, analysis_json=NULL,"
           f" analysis_model=NULL, updated_at=? WHERE status='{NEEDS_LINK}' OR ({failed})")
    cur = conn.execute(sql, params)
    conn.commit()
    return cur.rowcount


def retry_needs_link(conn: sqlite3.Connection) -> int:
    """Vuelve a poner en cola solo los que piden un enlace público (se activó la descarga)."""
    cur = conn.execute("UPDATE material SET status='pending', error=NULL, updated_at=?"
                       " WHERE status=?", (int(time.time()), NEEDS_LINK))
    conn.commit()
    return cur.rowcount


def _load_url_denial(conn: sqlite3.Connection, provider: AIProvider, now: float) -> None:
    """Pasa al proveedor la marca vigente del enlace rechazado y si se puede descargar.

    La marca vieja ("denied", sin fecha) pasa a valer 7 días desde ahora.
    """
    row = conn.execute("SELECT value FROM kv WHERE key=?", (URL_INPUT_KEY,)).fetchone()
    until = 0.0
    if row and row[0] == LEGACY_DENIED:
        until = now + YOUTUBE_URL_DENIED_S
    elif row:
        try:
            until = float(row[0])
        except (TypeError, ValueError):
            until = 0.0
    if until > now:
        provider.youtube_url_denied_until = min(until, now + YOUTUBE_URL_DENIED_S)
    provider.allow_download = download_allowed(conn)
    # Los videos rechazados hoy en corridas anteriores: con dos distintos se marca la clave.
    try:
        denied = json.loads(_kv_text(conn, _day_key(DENIED_VIDEOS_PREFIX, now)) or "[]")
    except ValueError:
        denied = []
    provider.url_denied_videos = {str(v) for v in denied} if isinstance(denied, list) else set()


def _save_url_denial(conn: sqlite3.Connection, provider: AIProvider, now: float) -> None:
    until = getattr(provider, "youtube_url_denied_until", 0.0) or 0.0
    if until > now:
        # La próxima corrida no prueba el enlace hasta que venza.
        conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)", (URL_INPUT_KEY, str(int(until))))
    else:
        conn.execute("DELETE FROM kv WHERE key=?", (URL_INPUT_KEY,))
    denied = sorted(getattr(provider, "url_denied_videos", None) or ())
    if denied:
        _kv_put(conn, _day_key(DENIED_VIDEOS_PREFIX, now), json.dumps(denied))
    # Los contadores de días anteriores ya no sirven.
    for prefix in DAY_PREFIXES:
        conn.execute("DELETE FROM kv WHERE key GLOB ? AND key != ?", (prefix + "*", _day_key(prefix, now)))
    conn.commit()


def _stop(conn: sqlite3.Connection, reason: str, resets_at: float | None) -> None:
    conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)", (STOP_KEY, reason))
    if resets_at is not None:
        conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)",
                     (QUOTA_RESET_KEY, str(int(resets_at))))
    conn.commit()


def process_pending(conn: sqlite3.Connection, provider: AIProvider, budget: int = 10,
                    clock: Callable[[], float] = time.time) -> int:
    """Procesa hasta `budget` materiales pendientes; devuelve cuántos quedaron `done`.

    Orden: secciones visibles por `num`, luego id de material. Ante
    Quota/Unavailable/Auth se detiene y guarda el motivo en kv["ai_stop_reason"]
    (y, por cuota, cuándo vuelve el primer modelo en kv["ai_quota_resets_at"]).
    Los modelos agotados por el día se leen y se guardan en kv["model_exhausted:<clave>:*"].
    Con DAILY_VIDEO_CAP_S de video leído por enlace en el día de la cuota de Gemini (Pacífico),
    saltea los que mandarían otro video (los ya analizados terminan igual) y deja el motivo
    "youtube_cap", que vuelve a la medianoche del Pacífico. Cualquier otra excepción
    marca ese material `failed` (con un texto genérico) y sigue con el próximo.
    """
    conn.execute("DELETE FROM kv WHERE key IN (?, ?)", (STOP_KEY, QUOTA_RESET_KEY))
    conn.commit()
    if budget <= 0:
        return 0  # (LIMIT negativo en SQLite sería "sin límite")
    pending = conn.execute(
        "SELECT m.id, m.analysis_json IS NOT NULL AS paid FROM material m"
        " JOIN section s ON s.id = m.section_id"
        " WHERE m.status='pending' AND s.visible=1"
        " ORDER BY s.num, m.id LIMIT ?",
        (budget,),
    ).fetchall()
    _load_url_denial(conn, provider, clock())
    load_exhausted(conn, provider)
    finished = 0
    capped_at: float | None = None
    try:
        for material_id, paid in pending:
            now = clock()
            # Con el enlace rechazado no sale nada por enlace: el tope no aplica.
            if not paid and not _link_denied(provider, now) and _cap_reached(conn, now):
                capped_at = now  # mañana; los ya analizados siguen
                continue
            try:
                result = process_material(conn, provider, material_id, clock)
            except (AIQuotaError, AIUnavailableError, AIAuthError) as exc:
                reason = next(r for cls, r in STOP_REASONS if isinstance(exc, cls))
                _stop(conn, reason, getattr(exc, "resets_at", None))
                capped_at = None  # el otro motivo manda
                break
            except AIBlocked:
                # La puerta de edad se cerró: no es culpa del material, queda pendiente.
                conn.rollback()
                raise
            except Exception as exc:  # noqa: BLE001 - un material roto no traba la cola
                # Solo el tipo: el mensaje podría traer datos del proveedor.
                log.warning("Falló el material %s (%s)", material_id, type(exc).__name__)
                conn.rollback()
                _set_status(conn, material_id, "failed", error=GENERIC_ERROR_TEXT, fail_kind=FAIL_ERROR)
                conn.commit()
                continue
            if result.status == "done":
                finished += 1
        if capped_at is not None:
            _stop(conn, YOUTUBE_CAP, next_cap_reset(capped_at))
    finally:
        _save_url_denial(conn, provider, clock())
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
