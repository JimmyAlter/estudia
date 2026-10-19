"""Tubería de generación: video, PDF o diapositivas -> resumen + preguntas verificadas.

Regla central: una pregunta solo se guarda (verified=1) si sus opciones son
distintas, la respuesta a ciegas coincide con `correct_index` y, cuando hay
`check_expr`, SymPy confirma la opción marcada y ninguna otra. Todo lo demás se
descarta y no se guarda. La pasada a ciegas va en un pedido por lote de hasta
BLIND_BATCH_MAX clases y nunca con un modelo que haya analizado alguna de ellas.
Un documento pasa exactamente por la misma verificación que un video.
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
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from estudia.ai.base import (
    AIAuthError,
    AIDocumentUnreadableError,
    AIFormatError,
    AINeedsPublicLinkError,
    AIProvider,
    AIQuotaError,
    AIUnavailableError,
    AIVideoUnavailableError,
    DocumentAnalysis,
    DocumentInput,
    VideoAnalysis,
    option_key,
)
from estudia.ai.quota import (
    BLIND_BATCH_MAX,
    can_analyze,
    can_analyze_document,
    prune_usage,
    quota_day,
    record_usage,
    remaining,
)
from estudia.config import (
    DAILY_VIDEO_CAP_S,
    LEGACY_ANALYSIS_MODEL,
    MAX_EXHAUSTED_S,
    VIDEO_ESTIMATE_S,
    YOUTUBE_URL_DENIED_S,
    data_dir,
)
from estudia.generation.notation import sentence_case, student_math
from estudia.ai.gate import AIBlocked
from estudia.generation.verify import check_math_tristate, typed_accepts_option
from estudia.material.documents import DocumentBusy, pdf_pages_bytes, slides_text
from estudia.material.files import (
    FAIL_DOWNLOAD,
    TOO_LARGE_TEXT,
    UNREADABLE_TEXT,
    TooLarge,
    UnreadableFile,
)

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
# Motivo de corte propio: se llegó al presupuesto diario de pedidos (estudia.ai.quota), o
# analizar más dejaría sin pedidos a la pasada a ciegas. Vuelve a la medianoche del Pacífico.
BUDGET = "budget"
LOCAL_TZ = ZoneInfo("America/Argentina/Buenos_Aires")
CAP_TZ = ZoneInfo("America/Los_Angeles")   # la zona del día de la cuota de Gemini (rotation.py)
# material.fail_kind: por qué falló, para decidir qué reintentar.
FAIL_FORMAT, FAIL_VIDEO, FAIL_ERROR = "format", "video", "error"
# Un PDF o unas diapositivas que no se pudieron leer (el archivo o Gemini).
FAIL_DOCUMENT = "document"
# Con una clave nueva vale la pena reintentar lo que pudo fallar por la IA; un video
# privado sigue privado y un archivo ilegible sigue ilegible. NULL = falló antes de existir fail_kind.
RETRY_ON_NEW_KEY = (FAIL_FORMAT, FAIL_ERROR)
# material.kind de los documentos del campus; NULL es un video de antes de existir `kind`.
DOC_KINDS = ("pdf", "slides")
# Lo que entra en la tubería: los demás (`page`, `file`) nunca quedan pendientes de análisis.
PIPELINE_KINDS = ("youtube", *DOC_KINDS)
DOC_UNREADABLE_TEXT = UNREADABLE_TEXT   # "No se pudo leer", fijado por la spec
# Marca de página del resumen de un documento: "(pág. 14)" o, en diapositivas, "(diap. 14)".
_PAGE_MARK = re.compile(r"( ?)\((?:pág|diap)\. (\d+)\)")

log = logging.getLogger(__name__)


@dataclass
class ProcessResult:
    kept: int
    discarded: int
    status: str = "done"  # done | failed | needs_public_link


def collapse_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def slugify(name: str) -> str:
    """Minúsculas, sin tildes, palabras unidas por guiones."""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def dedupe_key(material_id: int, stem: str) -> str:
    return hashlib.sha1(f"{material_id}:{collapse_spaces(stem).lower()}".encode()).hexdigest()


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


def _local_candidates(analysis: VideoAnalysis | DocumentAnalysis) -> list:
    """Las preguntas que pasan lo que se verifica gratis; solo estas van a ciegas."""
    return [q for q in analysis.questions if _passes_local_checks(q)]


def _set_status(conn, material_id: int, status: str, summary_md=None, error=None,
                fail_kind=None, segments_json=None) -> None:
    """Cierra el material (done/failed): el análisis guardado ya no hace falta.

    Con `done` queda anotado qué modelo hizo el análisis (para verificar de nuevo con otro).
    """
    conn.execute(
        "UPDATE material SET status=?, summary_md=?, error=?, fail_kind=?, analysis_json=NULL,"
        " analysis_model=CASE WHEN ?='done' THEN analysis_model END, segments_json=?,"
        " updated_at=? WHERE id=?",
        (status, summary_md, error, fail_kind, status, segments_json, int(time.time()), material_id),
    )


def download_allowed(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT value FROM kv WHERE key=?", (ALLOW_DOWNLOAD_KEY,)).fetchone()
    return row is not None and row[0] == "1"


def local_day(now: float) -> dt.date:
    return dt.datetime.fromtimestamp(now, LOCAL_TZ).date()


def cap_day(now: float) -> dt.date:
    """El día del tope de YouTube: el de la cuota de Gemini (Pacífico)."""
    return quota_day(now)


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


def is_document(kind: str | None) -> bool:
    return kind in DOC_KINDS


def load_analysis(kind: str | None, raw: str) -> VideoAnalysis | DocumentAnalysis:
    """El análisis guardado de un material, con el tipo que corresponde a su `kind`.

    Lanza ValidationError (un ValueError) si no es un análisis de ese tipo: uno de video
    nunca se lee como de documento ni al revés.
    """
    model = DocumentAnalysis if is_document(kind) else VideoAnalysis
    return model.model_validate_json(raw)


def document_input(conn: sqlite3.Connection, material_id: int, base_dir: Path) -> DocumentInput:
    """Lo que se le manda a Gemini de un documento o de un tramo.

    Un PDF entero va con los bytes del archivo; un tramo, solo con sus páginas (un PDF nuevo
    que empieza en su página 1). Las diapositivas van como texto, numeradas desde 1 dentro
    del tramo. UnreadableFile si el archivo no se puede leer o su ruta sale de `base_dir`.
    """
    row = conn.execute("SELECT kind, title, ref, pages, page_from, page_to FROM material"
                       " WHERE id=?", (material_id,)).fetchone()
    if row is None:
        raise LookupError(f"material {material_id} inexistente")
    kind, page_from, page_to = row["kind"], row["page_from"], row["page_to"]
    if not is_document(kind):
        raise ValueError(f"material {material_id} no es un documento")
    root = Path(base_dir).resolve()
    path = (root / (row["ref"] or "")).resolve()
    if not path.is_relative_to(root) or path == root:
        raise UnreadableFile(UNREADABLE_TEXT)
    part = page_from is not None and page_to is not None
    pages = page_to - page_from + 1 if part else (row["pages"] or 0)
    title = row["title"] or ""
    if kind == "pdf":
        if part:
            data = pdf_pages_bytes(path, page_from, page_to)
        else:
            try:
                data = path.read_bytes()
            except OSError:
                raise UnreadableFile(UNREADABLE_TEXT) from None
        return DocumentInput(kind=kind, title=title, pages=pages, data=data, part=part)
    if not path.is_file():
        raise UnreadableFile(UNREADABLE_TEXT)
    text = slides_text(path, page_from or 1, page_to if part else None)
    return DocumentInput(kind=kind, title=title, pages=pages, text=text, part=part)


def _analysis(conn, provider: AIProvider, material_id: int,
              row, clock: Callable[[], float] = time.time) -> tuple[VideoAnalysis | DocumentAnalysis, str]:
    """El análisis del material y el modelo que lo hizo; si una corrida anterior ya lo
    pagó, se reusa.

    Se guarda apenas llega, antes de la pasada a ciegas: si esa pasada se corta por
    cuota, la próxima corrida no vuelve a analizar el material. El modelo se guarda para
    que la pasada a ciegas no lo use. Un documento va por `analyze_document` y nunca suma
    al tope de YouTube; un video va por `analyze_video`.
    """
    document = is_document(row["kind"])
    if row["analysis_json"]:
        try:
            cached = load_analysis(row["kind"], row["analysis_json"])
            # Sin modelo anotado: es de antes de anotarlo, cuando había uno solo.
            return cached, row["analysis_model"] or LEGACY_ANALYSIS_MODEL
        except ValidationError:
            pass  # guardado roto: se vuelve a pedir
    if document:
        analysis = provider.analyze_document(document_input(conn, material_id, data_dir()))
    else:
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
    if not document:
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


def _material_row(conn, material_id: int):
    row = conn.execute(
        "SELECT m.kind, m.ref, m.title, m.analysis_json, m.analysis_model, m.pages,"
        " m.page_from, m.page_to, s.course_id FROM material m"
        " JOIN section s ON s.id = m.section_id WHERE m.id=?",
        (material_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"material {material_id} inexistente")
    return row


def _analyze_or_close(conn, provider: AIProvider, material_id: int,
                      clock: Callable[[], float]) -> ProcessResult | None:
    """Deja guardado el análisis (lo pide si hace falta). Si el video o el documento no se
    puede leer o la respuesta es inservible, cierra el material y devuelve cómo quedó; si
    no, None. Quota/Unavailable/Auth se propagan sin guardar nada."""
    row = _material_row(conn, material_id)
    try:
        _analysis(conn, provider, material_id, row, clock)
    except (UnreadableFile, AIDocumentUnreadableError):
        # El archivo (o Gemini) no lo pudo leer: va antes que AIFormatError, que es su base.
        _set_status(conn, material_id, "failed", error=DOC_UNREADABLE_TEXT, fail_kind=FAIL_DOCUMENT)
        conn.commit()
        return ProcessResult(0, 0, "failed")
    except (TooLarge, ValueError) as exc:
        # Solo en un documento: más páginas que el tope, o un tramo con un rango inválido
        # (`pdf_pages_bytes`). En un video sigue siendo un error inesperado.
        if not is_document(row["kind"]):
            raise
        conn.rollback()
        text = TOO_LARGE_TEXT if isinstance(exc, TooLarge) else DOC_UNREADABLE_TEXT
        _set_status(conn, material_id, "failed", error=text, fail_kind=FAIL_DOCUMENT)
        conn.commit()
        return ProcessResult(0, 0, "failed")
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
    return None


def _page_range(row) -> tuple[int, int] | None:
    """Las páginas del documento entero que cubre lo que se mandó; None si no se sabe."""
    if row["page_from"] is not None and row["page_to"] is not None:
        return row["page_from"], row["page_to"]
    if row["pages"]:
        return 1, row["pages"]
    return None


def _page_offset(row) -> int:
    """Cuánto correr las páginas que cita Gemini para que sean las del documento entero.

    Un tramo (de PDF o de diapositivas) se manda numerado desde 1: se corre `page_from - 1`.
    """
    return row["page_from"] - 1 if row["page_from"] is not None else 0


def _in_document(page: int, offset: int, first: int, last: int | None) -> int | None:
    """La página citada `page` (contada desde 1 en lo mandado) en el documento entero.

    En un tramo que no empieza en la 1 (`offset` > 0), una cita desde `page_from`
    (= `offset` + 1) puede ser el número impreso en la hoja y no el del archivo: ante la
    duda, None. Una cita que falta vale; una equivocada, no.
    """
    if offset > 0 and page > offset:
        return None
    page += offset
    if last is None or not first <= page <= last:
        return None
    return page


def _document_page(row, page: int | None) -> int | None:
    """La página del documento entero; None si no hay, si cae fuera de lo que se mandó o si
    no se sabe cuántas páginas tiene."""
    if page is None:
        return None
    first, last = _page_range(row) or (1, None)
    return _in_document(page, _page_offset(row), first, last)


def _shift_pages(summary_md: str, offset: int, first: int = 1, last: int | None = None,
                 unit: str = "pág.") -> str:
    """Corre `offset` las marcas "(pág. N)" del resumen (de un tramo al documento entero) y
    las escribe con `unit` ("pág." en un PDF, "diap." en diapositivas).

    Con la misma regla que las preguntas: una marca que cae fuera de `first..last` (o todas,
    si `last` es None) o que puede ser un número impreso se borra.
    """
    def replace(m: re.Match) -> str:
        page = _in_document(int(m.group(2)), offset, first, last)
        return "" if page is None else f"{m.group(1)}({unit} {page})"

    return _PAGE_MARK.sub(replace, summary_md)


def _finalize(conn, material_id: int, analysis: VideoAnalysis | DocumentAnalysis,
              kept: list) -> ProcessResult:
    """Escribe temas, preguntas verificadas, tramos y `done` en una sola transacción.

    Un documento guarda la página citada (la del documento entero) y no tiene segundos
    ni tramos; un video, al revés.
    """
    row = _material_row(conn, material_id)
    course_id = row["course_id"]
    document = is_document(row["kind"])
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
            source_page = _document_page(row, getattr(q, "source_page", None)) if document else None
            cur = conn.execute(
                "INSERT OR IGNORE INTO question(material_id, topic_id, stem, options_json,"
                " correct_index, explanation, common_error, source_seconds, source_page,"
                " check_expr, verified, retired, dedupe_key) VALUES (?,?,?,?,?,?,?,?,?,?,1,0,?)",
                (
                    material_id, tid, text.stem, json.dumps(text.options, ensure_ascii=False),
                    q.correct_index, text.explanation, text.common_error,
                    getattr(q, "source_seconds", None), source_page,
                    check_expr, dedupe_key(material_id, text.stem),
                ),
            )
            stored += cur.rowcount  # 0 si ya estaba (mismo enunciado)
        segments = getattr(analysis, "segments", None)
        segments_json = (json.dumps([s.model_dump() for s in segments], ensure_ascii=False)
                         if segments else None)
        summary_md = analysis.summary_md
        if document:
            first, last = _page_range(row) or (1, None)
            unit = "diap." if row["kind"] == "slides" else "pág."
            summary_md = _shift_pages(summary_md, _page_offset(row), first, last, unit)
        _set_status(conn, material_id, "done", summary_md=student_math(summary_md),
                    segments_json=segments_json)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return ProcessResult(stored, len(analysis.questions) - stored)


def _analysis_model_of(conn, material_id: int) -> str:
    """El modelo que analizó la clase; sin anotar, el único de antes (LEGACY_ANALYSIS_MODEL)."""
    row = conn.execute("SELECT COALESCE(analysis_model, ?) FROM material WHERE id=?",
                       (LEGACY_ANALYSIS_MODEL, material_id)).fetchone()
    if row is None:
        raise LookupError(f"material {material_id} inexistente")
    return row[0]


def paid_batches(conn: sqlite3.Connection, material_ids: list[int]) -> list[list[int]]:
    """Agrupa los pendientes ya analizados (en orden de cola) por el modelo que los analizó,
    en el orden en que aparece cada modelo, y parte cada grupo en lotes de hasta
    BLIND_BATCH_MAX."""
    groups: dict[str, list[int]] = {}
    for material_id in material_ids:
        groups.setdefault(_analysis_model_of(conn, material_id), []).append(material_id)
    return [ids[i:i + BLIND_BATCH_MAX] for ids in groups.values()
            for i in range(0, len(ids), BLIND_BATCH_MAX)]


def verify_batch(conn: sqlite3.Connection, provider: AIProvider,
                 material_ids: list[int]) -> list[ProcessResult]:
    """Pasada a ciegas de un lote de clases ya analizadas, en un solo pedido.

    El pedido excluye el modelo de análisis de todas las clases del lote (si no queda
    ninguno, la rotación da AIQuotaError y el lote sigue pendiente). Las respuestas
    vuelven a su clase por posición y cada clase se cierra con `_finalize`; una clase sin
    candidatos se cierra sin pedido. Quota/Unavailable/Auth se propagan sin escribir nada.
    Una respuesta inservible con más de una clase en el pedido se reintenta clase por
    clase; con una sola, la clase queda `failed` (formato).
    """
    loaded = []
    for material_id in material_ids:
        row = _material_row(conn, material_id)
        if not row["analysis_json"]:
            raise LookupError(f"material {material_id} sin análisis guardado")
        analysis = load_analysis(row["kind"], row["analysis_json"])
        loaded.append((material_id, analysis, _local_candidates(analysis)))
    exclude = tuple(dict.fromkeys(_analysis_model_of(conn, mid) for mid in material_ids))
    questions = [q for _, _, candidates in loaded for q in candidates]
    try:
        matches = blind_matches(provider, questions, exclude)
    except AIFormatError:
        asked = [mid for mid, _, candidates in loaded if candidates]
        if len(asked) > 1:
            # No se sabe qué clase rompió la respuesta: cada una va sola.
            log.info("Pasada a ciegas ilegible para %d clases; se reintenta de a una", len(asked))
            # Por la cola: un error inesperado deja esa clase `failed` y no la vuelve a pedir.
            return [result for mid in material_ids for result in _verify_in_queue(conn, provider, [mid])]
        results = []
        for mid, analysis, candidates in loaded:
            if candidates:
                _set_status(conn, mid, "failed", error=FORMAT_ERROR_TEXT, fail_kind=FAIL_FORMAT)
                conn.commit()
                results.append(ProcessResult(0, 0, "failed"))
            else:
                results.append(_finalize(conn, mid, analysis, []))
        return results
    results = []
    offset = 0
    for material_id, analysis, candidates in loaded:
        own = matches[offset:offset + len(candidates)]
        offset += len(candidates)
        kept = [q for q, ok in zip(candidates, own) if ok]
        results.append(_finalize(conn, material_id, analysis, kept))
    return results


def process_material(conn: sqlite3.Connection, provider: AIProvider, material_id: int,
                     clock: Callable[[], float] = time.time) -> ProcessResult:
    """Procesa un material: lo analiza si hace falta y lo verifica solo (lote de una
    clase). Quota/Unavailable/Auth se propagan sin guardar nada más que el análisis."""
    closed = _analyze_or_close(conn, provider, material_id, clock)
    if closed is not None:
        return closed
    return verify_batch(conn, provider, [material_id])[0]


def retry_failed(conn: sqlite3.Connection, kinds: tuple[str, ...] | None = None) -> int:
    """Vuelve a poner en cola los materiales `failed` y los que piden un enlace público;
    devuelve cuántos.

    Con `kinds`, de los `failed` solo los que fallaron por esos motivos (y los de antes de
    fail_kind). Los que piden enlace vuelven siempre: si el enlace sigue rechazado y la
    descarga apagada, vuelven a quedar así sin gastar un pedido.
    """
    now = int(time.time())
    # Un archivo del campus que no se pudo bajar vuelve a la cola de descargas, de cero.
    download = conn.execute(
        "UPDATE material SET status='downloading', error=NULL, fail_kind=NULL,"
        " download_attempts=0, updated_at=? WHERE status='failed' AND fail_kind=?",
        (now, FAIL_DOWNLOAD),
    ).rowcount if kinds is None or FAIL_DOWNLOAD in kinds else 0
    failed = "status='failed'"
    params: list = [now]
    if kinds is not None:
        failed += f" AND (fail_kind IS NULL OR fail_kind IN ({','.join('?' * len(kinds))}))"
        params.extend(kinds)
    sql = ("UPDATE material SET status='pending', error=NULL, fail_kind=NULL, analysis_json=NULL,"
           f" analysis_model=NULL, segments_json=NULL, updated_at=? WHERE status='{NEEDS_LINK}' OR ({failed})")
    cur = conn.execute(sql, params)
    conn.commit()
    return download + cur.rowcount


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


def record_stop(conn: sqlite3.Connection, reason: str, resets_at: float | None) -> None:
    """Anota el motivo de corte; si hay varios en una corrida, queda el último."""
    conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)", (STOP_KEY, reason))
    if resets_at is not None:
        conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)",
                     (QUOTA_RESET_KEY, str(int(resets_at))))
    else:
        conn.execute("DELETE FROM kv WHERE key=?", (QUOTA_RESET_KEY,))
    conn.commit()


def stop_reason_for(exc: Exception) -> str:
    return next(r for cls, r in STOP_REASONS if isinstance(exc, cls))


def _fail_unexpected(conn: sqlite3.Connection, material_id: int, exc: Exception) -> None:
    # Solo el tipo: el mensaje podría traer datos del proveedor.
    log.warning("Falló el material %s (%s)", material_id, type(exc).__name__)
    conn.rollback()
    _set_status(conn, material_id, "failed", error=GENERIC_ERROR_TEXT, fail_kind=FAIL_ERROR)
    conn.commit()


def _verify_in_queue(conn: sqlite3.Connection, provider: AIProvider,
                     material_ids: list[int]) -> list[ProcessResult]:
    """`verify_batch` dentro de la cola: un error inesperado no traba a las demás clases.

    Con un lote de varias, las que siguen pendientes se reintentan de a una; una sola
    queda `failed` con el texto genérico. Quota/Unavailable/Auth y la puerta de edad se
    propagan.
    """
    try:
        return verify_batch(conn, provider, material_ids)
    except (AIQuotaError, AIUnavailableError, AIAuthError):
        raise
    except AIBlocked:
        # La puerta de edad se cerró: no es culpa del material, queda pendiente.
        conn.rollback()
        raise
    except Exception as exc:  # noqa: BLE001 - un material roto no traba la cola
        conn.rollback()
        if len(material_ids) == 1:
            _fail_unexpected(conn, material_ids[0], exc)
            return [ProcessResult(0, 0, "failed")]
        log.warning("Falló un lote a ciegas (%s); se reintenta de a una clase", type(exc).__name__)
        return [result for material_id in _still_pending(conn, material_ids)
                for result in _verify_in_queue(conn, provider, [material_id])]


def _still_pending(conn: sqlite3.Connection, material_ids: list[int]) -> list[int]:
    """Los de `material_ids` (en ese orden) que siguen pendientes con un análisis legible."""
    ready = []
    for material_id in material_ids:
        row = conn.execute("SELECT kind, analysis_json FROM material WHERE id=? AND status='pending'",
                           (material_id,)).fetchone()
        if row is None or not row["analysis_json"]:
            continue
        try:
            load_analysis(row["kind"], row["analysis_json"])
        except ValidationError:
            continue  # guardado roto: la próxima corrida lo vuelve a analizar
        ready.append(material_id)
    return ready


@dataclass
class QueueItem:
    material_id: int
    paid: bool                # ya tiene el análisis guardado
    exam_at: int | None       # el examen futuro más próximo de su curso


# Videos y documentos; NULL es un material de antes de que existiera `kind`, que siempre fue
# un video. Un documento partido queda `split` y entran sus tramos.
_IN_PIPELINE = "COALESCE(m.kind, 'youtube') IN (" + ",".join(f"'{k}'" for k in PIPELINE_KINDS) + ")"


def _kinds_sql(kinds: tuple[str, ...]) -> str:
    return "(" + ",".join(f"'{k}'" for k in kinds if k in PIPELINE_KINDS) + ")" if kinds else "(NULL)"


def pending_queue(conn: sqlite3.Connection, now: int, limit: int,
                  unpaid_kinds: tuple[str, ...] = PIPELINE_KINDS) -> list[QueueItem]:
    """Los pendientes de secciones visibles, primero los del curso que rinde antes.

    Una clase sin analizar todavía no tiene temas: "los temas del próximo examen" se
    resuelven por curso. Un examen abierto (cierra después de `now`) todavía cuenta, como en
    `home.next_exam`. Sin examen futuro van al final; dentro de lo mismo, por sección y por id.
    Los que no tienen análisis guardado entran solo si su `kind` está en `unpaid_kinds`
    (los ya analizados, siempre).
    """
    rows = conn.execute(
        "SELECT m.id, m.analysis_json IS NOT NULL AS paid,"
        " (SELECT MIN(e.opens_at) FROM exam e WHERE e.course_id = s.course_id AND COALESCE(NULLIF(e.closes_at, 0), e.opens_at) > ? AND COALESCE(e.kind, 'parcial') != 'inscripcion')"
        " AS exam_at FROM material m JOIN section s ON s.id = m.section_id"
        " WHERE m.status='pending' AND s.visible=1 AND " + _IN_PIPELINE +
        " AND (m.analysis_json IS NOT NULL OR COALESCE(m.kind, 'youtube') IN "
        + _kinds_sql(unpaid_kinds) + ")"
        " ORDER BY exam_at IS NULL, exam_at, s.num, m.id LIMIT ?",
        (now, limit),
    ).fetchall()
    return [QueueItem(r[0], bool(r[1]), r[2]) for r in rows]


def awaiting_blind_count(conn: sqlite3.Connection) -> int:
    """Clases ya analizadas que esperan la pasada a ciegas."""
    return conn.execute(
        "SELECT COUNT(*) FROM material m JOIN section s ON s.id = m.section_id"
        " WHERE m.status='pending' AND s.visible=1 AND m.analysis_json IS NOT NULL AND " + _IN_PIPELINE
    ).fetchone()[0]


def _analyzable_kinds(conn: sqlite3.Connection, now: float) -> tuple[str, ...]:
    """Los `kind` que todavía tienen presupuesto de análisis hoy: un video gasta "analysis";
    un PDF o unas diapositivas, "pdf"."""
    kinds: tuple[str, ...] = ()
    if remaining(conn, "analysis", now) > 0:
        kinds += ("youtube",)
    if remaining(conn, "pdf", now) > 0:
        kinds += DOC_KINDS
    return kinds


def _unpaid_waiting(conn: sqlite3.Connection, kinds: tuple[str, ...]) -> bool:
    """¿Hay pendientes sin analizar de estos `kind`?"""
    return conn.execute(
        "SELECT 1 FROM material m JOIN section s ON s.id = m.section_id"
        " WHERE m.status='pending' AND s.visible=1 AND m.analysis_json IS NULL AND "
        "COALESCE(m.kind, 'youtube') IN " + _kinds_sql(kinds) + " LIMIT 1"
    ).fetchone() is not None


def _is_document_material(conn: sqlite3.Connection, material_id: int) -> bool:
    row = conn.execute("SELECT kind FROM material WHERE id=?", (material_id,)).fetchone()
    return row is not None and is_document(row[0])


def process_pending(conn: sqlite3.Connection, provider: AIProvider, budget: int = 10,
                    clock: Callable[[], float] = time.time) -> int:
    """Procesa hasta `budget` materiales pendientes; devuelve cuántos quedaron `done`.

    Orden: `pending_queue` (primero el curso del próximo examen, después sección e id). Los
    sin analizar de un tipo cuyo presupuesto del día ya está gastado al empezar ("analysis"
    para un video, "pdf" para un documento) no entran en la ventana: quedan pendientes con
    el motivo "budget" y no le quitan el lugar a los del otro tipo. Con el tope de YouTube
    ya alcanzado al empezar, lo mismo con los videos sin analizar (motivo "youtube_cap").
    Va en dos fases:
    1. Analiza los que no tienen análisis guardado, en orden (cada análisis se guarda
       apenas llega). Con DAILY_VIDEO_CAP_S de video leído por enlace en el día de la
       cuota de Gemini (Pacífico), saltea los que mandarían otro video y deja el motivo
       "youtube_cap". Saltea también los que dejarían sin presupuesto del día a la pasada
       a ciegas de lo ya analizado, o sin presupuesto propio (`can_analyze` para un video,
       `can_analyze_document` para un PDF o unas diapositivas), con el motivo "budget".
       Los dos vuelven a la medianoche del Pacífico. Un documento no tiene tope de
       YouTube. Si los lugares para leer archivos están ocupados, el documento queda
       pendiente para la próxima corrida.
    2. Verifica a ciegas, en lotes (`paid_batches`), todos los ya analizados de la ventana,
       mientras quede presupuesto a ciegas del día (si no, corta con "budget").
    Ante Quota/Unavailable/Auth corta la fase y guarda el motivo en kv["ai_stop_reason"]
    (y, por cuota, cuándo vuelve el primer modelo en kv["ai_quota_resets_at"]); queda el
    último. Si la fase 1 se corta por cuota, presupuesto, el tope de YouTube o Gemini caído,
    la fase 2 corre igual; por clave, no. Los pedidos hechos se suman al uso del día de la
    cuota. Los modelos agotados por el día se leen y se guardan en
    kv["model_exhausted:<clave>:*"]. Cualquier otra excepción marca ese material `failed`
    (con un texto genérico) y sigue con el próximo.
    """
    conn.execute("DELETE FROM kv WHERE key IN (?, ?)", (STOP_KEY, QUOTA_RESET_KEY))
    conn.commit()
    if budget <= 0:
        return 0  # (LIMIT negativo en SQLite sería "sin límite")
    start = clock()
    _load_url_denial(conn, provider, start)
    load_exhausted(conn, provider)
    # Lo sin analizar de un tipo sin presupuesto hoy no entra en la ventana: si no, doce PDFs
    # sin presupuesto adelante dejarían afuera al video de atrás (y al revés).
    analyzable = _analyzable_kinds(conn, start)
    held_back = tuple(k for k in PIPELINE_KINDS if k not in analyzable)
    capped_at: float | None = None
    # Lo mismo con el tope de YouTube ya alcanzado (salvo con el enlace rechazado, que no lee
    # nada por enlace): los videos sin analizar esperan a mañana y no tapan a los documentos.
    if "youtube" in analyzable and not _link_denied(provider, start) and _cap_reached(conn, start):
        analyzable = tuple(k for k in analyzable if k != "youtube")
        if _unpaid_waiting(conn, ("youtube",)):
            capped_at = start
    window = [item.material_id for item in pending_queue(conn, int(start), budget, analyzable)]
    # Los que quedaron afuera esperan el presupuesto de mañana: el motivo es "budget".
    over_budget_at: float | None = start if held_back and _unpaid_waiting(conn, held_back) else None
    analysis_stop: str | None = None
    try:
        # Fase 1: análisis.
        for material_id in window:
            now = clock()
            # Lo pedido hasta acá ya cuenta para el presupuesto del día.
            record_usage(conn, provider, now)
            if not _still_pending(conn, [material_id]):  # sin análisis legible: hay que pedirlo
                if _is_document_material(conn, material_id):
                    # Un documento gasta del presupuesto "pdf" y no lee video: sin tope de YouTube.
                    if not can_analyze_document(conn, now, awaiting_blind_count(conn)):
                        over_budget_at = now
                        continue
                else:
                    if not can_analyze(conn, now, awaiting_blind_count(conn)):
                        over_budget_at = now  # la reserva de la pasada a ciegas manda
                        continue
                    # Con el enlace rechazado no sale nada por enlace: el tope no aplica.
                    if not _link_denied(provider, now) and _cap_reached(conn, now):
                        capped_at = now  # mañana; los ya analizados siguen
                        continue
            try:
                _analyze_or_close(conn, provider, material_id, clock)
            except DocumentBusy:
                # Los lugares para leer archivos estaban ocupados: no es el archivo, queda pendiente.
                conn.rollback()
                continue
            except (AIQuotaError, AIUnavailableError, AIAuthError) as exc:
                analysis_stop = stop_reason_for(exc)
                record_stop(conn, analysis_stop, getattr(exc, "resets_at", None))
                capped_at = over_budget_at = None  # el otro motivo manda
                break
            except AIBlocked:
                # La puerta de edad se cerró: no es culpa del material, queda pendiente.
                conn.rollback()
                raise
            except Exception as exc:  # noqa: BLE001 - un material roto no traba la cola
                _fail_unexpected(conn, material_id, exc)
        if capped_at is not None:
            record_stop(conn, YOUTUBE_CAP, next_cap_reset(capped_at))
        if over_budget_at is not None:
            record_stop(conn, BUDGET, next_cap_reset(over_budget_at))
        # Fase 2: pasada a ciegas en lotes. Sin clave no tiene sentido; con la cadena de
        # análisis caída sí: la cadena a ciegas usa otros modelos y, si también está
        # caída, se corta sin escribir nada.
        if analysis_stop != "auth":
            for batch in paid_batches(conn, _still_pending(conn, window)):
                now = clock()
                record_usage(conn, provider, now)
                if remaining(conn, "blind", now) <= 0:
                    # Un motivo de la fase 1 (con su propio regreso) no se pisa.
                    if conn.execute("SELECT 1 FROM kv WHERE key = ?", (STOP_KEY,)).fetchone() is None:
                        record_stop(conn, BUDGET, next_cap_reset(now))
                    break
                try:
                    _verify_in_queue(conn, provider, batch)
                except (AIQuotaError, AIUnavailableError, AIAuthError) as exc:
                    record_stop(conn, stop_reason_for(exc), getattr(exc, "resets_at", None))
                    break
    finally:
        now = clock()
        record_usage(conn, provider, now)
        prune_usage(conn, now)
        _save_url_denial(conn, provider, now)
        save_exhausted(conn, provider)
    if not window:
        return 0
    marks = ",".join("?" * len(window))
    return conn.execute(f"SELECT COUNT(*) FROM material WHERE status='done' AND id IN ({marks})",
                        window).fetchone()[0]


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
