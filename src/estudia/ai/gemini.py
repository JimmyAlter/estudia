"""Proveedor de IA basado en Gemini (google-genai)."""
import hashlib
import logging
import os
import sys
import time
from secrets import token_hex
from collections.abc import Callable, Collection
from pathlib import Path
from typing import TypeVar

import httpx
from google import genai
from google.genai import errors, types
from pydantic import ValidationError

from estudia import config
from estudia.ai.base import (
    AIAuthError,
    AIDocumentUnreadableError,
    AIFormatError,
    AINeedsPublicLinkError,
    AIQuotaError,
    AIUnavailableError,
    AIVideoUnavailableError,
    DocQuestion,
    DocumentAnalysis,
    DocumentInput,
    ExtractedStyle,
    GenQuestion,
    PastExamInput,
    Segment,
    StyleQuestion,
    StyleQuestions,
    StyleRequest,
    VideoAnalysis,
    option_key,
)
from estudia.ai.blind import BlindItems, blind_config, blind_prompt, parse_blind
from estudia.ai.gate import age_is_adult
from estudia.ai.rotation import (
    DAILY,
    DENIED,
    QUOTA,
    TRANSIENT,
    UNREADABLE,
    UNSUPPORTED,
    LinkDenied,
    ModelRotation,
    NextModel,
    is_daily_quota,
    is_invalid_key,
    is_permission_denied,
    is_quota,
    is_transient,
    is_unsupported,
    mentions_url,
    retry_delay,
)
from estudia.config import (
    GEMINI_TIMEOUT_S,
    YOUTUBE_URL_DENIED_S,
    data_dir,
)
from estudia.generation.prompts import (
    ANALYSIS_PROMPT,
    DOCUMENT_PROMPT,
    STYLE_PROMPT,
    STYLE_QUESTIONS_PROMPT,
)
from estudia.material import video as video_module
from estudia.material.files import UPLOAD_COPY_PREFIX
from estudia.material.video import VideoDownloadError, mime_type

# Un 429 con espera corta es el límite por minuto del plan gratuito: se espera y se reintenta.
MAX_RETRY_WAIT_S = 60.0
QUOTA_RETRIES = 2
# Un 503 ("alta demanda") o un 504 (DEADLINE_EXCEEDED) se reintenta una vez después de
# esperar; si sigue, se pasa al próximo modelo de la cadena (sin marcarlo agotado).
OVERLOAD_WAIT_S = 20.0
TRANSIENT_RETRIES = 1
# Un video subido tarda en quedar listo (ACTIVE); se consulta cada tanto hasta este tope.
FILE_ACTIVE_TIMEOUT_S = 300.0
FILE_POLL_S = 5.0

# Tokens por segundo de video con MEDIA_RESOLUTION_LOW: el audio va a 32 por segundo y
# cada fotograma (uno por segundo) a 66. Sirven para saber cuánto video se mandó.
AUDIO_TOKENS_PER_S = 32
# La duración sale de los tokens y es aproximada: los tramos pueden pasarse por esto.
SEGMENT_SLACK_S = 30
LOW_RES_FRAME_TOKENS = 66

# La clave entera se da por rechazada para enlaces recién con 403 en esta cantidad de
# videos distintos: un solo video privado no frena a los demás.
DENIED_VIDEOS_FOR_KEY = 2

# De dónde sale el video en un pedido de análisis.
YOUTUBE_URL, UPLOADED_FILE = "youtube_url", "uploaded_file"
# Valor de `video` en `_generate` cuando se manda un documento (PDF o diapositivas).
DOCUMENT = "document"

# Un PDF de hasta este tamaño va dentro del pedido; si pesa más, se sube con la API de archivos.
INLINE_DOCUMENT_MAX_BYTES = 18 * 1024 * 1024
PDF_MIME = "application/pdf"
# El mime de un parcial viejo según su tipo (que salió de los bytes al cargarlo).
PAST_EXAM_MIMES = {"pdf": PDF_MIME, "jpeg": "image/jpeg", "png": "image/png"}
DOCUMENT_UNREADABLE = "Gemini no pudo leer el archivo"

# Modelos que puede usar cada clave (client.models.list()), por huella de la clave.
# Se consulta una vez y vale mientras dure el proceso: si Google agrega o saca un
# modelo, se nota recién al reabrir la app.
_LISTED_MODELS: dict[str, frozenset[str]] = {}

log = logging.getLogger(__name__)


class _YoutubeUrlRejected(Exception):
    """Gemini no aceptó el enlace de YouTube; `remember`: vale para todos los videos."""

    def __init__(self, remember: bool):
        super().__init__("Gemini no aceptó el enlace de YouTube")
        self.remember = remember


_Analysis = TypeVar("_Analysis", VideoAnalysis, DocumentAnalysis)


def _question_ok(q: GenQuestion | DocQuestion | StyleQuestion) -> bool:
    if len(q.options) != 4 or not 0 <= q.correct_index <= 3 or not q.stem.strip():
        return False
    keys = [option_key(o) for o in q.options]
    return all(keys) and len(set(keys)) == 4


def _validate(analysis: _Analysis) -> _Analysis:
    """Rechaza el análisis si el resumen o los temas son inválidos; descarta preguntas sueltas.

    Una pregunta con un tema fuera de `topics` se acepta: la tubería crea el tema.
    """
    if not analysis.summary_md.strip():
        raise AIFormatError("el resumen está vacío")
    if not 1 <= len(analysis.topics) <= 3 or any(not t.strip() for t in analysis.topics):
        raise AIFormatError("debe haber entre 1 y 3 temas")
    questions = [q for q in analysis.questions if _question_ok(q)]
    if not questions:
        raise AIFormatError("no hay preguntas válidas")
    return analysis.model_copy(update={"questions": questions})


def _validate_document(analysis: DocumentAnalysis, pages: int) -> DocumentAnalysis:
    """Mismas reglas que `_validate`. Una página citada fuera del archivo queda en None:
    la pregunta sirve igual, solo pierde la cita."""
    analysis = _validate(analysis)
    questions = [
        q if q.source_page is None or 1 <= q.source_page <= pages
        else q.model_copy(update={"source_page": None})
        for q in analysis.questions
    ]
    return analysis.model_copy(update={"questions": questions})


def valid_segments(segments: list[Segment], duration_s: float | None) -> list[Segment]:
    """Todo o nada: si algún tramo es inválido devuelve []."""
    previous_end = 0
    for seg in segments:
        if not 0 <= seg.start_s < seg.end_s or not seg.title.strip():
            return []
        if seg.start_s < previous_end:
            return []
        if duration_s is not None and seg.end_s > duration_s + SEGMENT_SLACK_S:
            return []
        previous_end = seg.end_s
    return list(segments)


def _short_name(name: str) -> str:
    return name.removeprefix("models/")


def _can_generate(model) -> bool:
    actions = getattr(model, "supported_actions", None)
    return actions is None or "generateContent" in actions


def _analysis_config() -> types.GenerateContentConfig:
    return types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=VideoAnalysis,
        # Menos tokens por fotograma: alcanza para pizarrón y diapositivas.
        media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
    )


def _document_config() -> types.GenerateContentConfig:
    return types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=DocumentAnalysis,
    )


def _style_config() -> types.GenerateContentConfig:
    return types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=ExtractedStyle,
    )


def _style_questions_config() -> types.GenerateContentConfig:
    return types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=StyleQuestions,
    )


def style_questions_prompt(request: StyleRequest) -> str:
    kinds = "\n".join(f"  - {n} de tipo \"{kind}\"" for kind, n in request.kinds.items() if n > 0)
    topics = "\n".join(f"  - {name}" for name in request.topics if name.strip())
    sheet = ("En el examen se puede usar una hoja de fórmulas: no hace falta preguntar fórmulas de memoria."
             if request.formula_sheet else
             "En el examen no hay hoja de fórmulas.")
    return STYLE_QUESTIONS_PROMPT.format(course=request.course, kinds=kinds, topics=topics,
                                         formula_sheet=sheet, material=request.material_text)


def _pages_note(doc: DocumentInput) -> str:
    """En un tramo, cuántas páginas (o diapositivas) tiene lo mandado: sus hojas traen
    impresos los números del documento entero."""
    if not doc.part or doc.pages <= 0:
        return ""
    unit = "páginas" if doc.data is not None else "diapositivas"
    return f" Este archivo tiene {doc.pages} {unit}."


def _course_topics_text(course_topics: list[str]) -> str:
    names = [t.strip() for t in course_topics if t.strip()]
    if not names:
        return "  (La materia todavía no tiene temas cargados.)"
    return "\n".join(f"  - {name}" for name in names)


def _video_seconds(response) -> float | None:
    """Duración del video según el uso que informa Gemini; None si no lo informa."""
    usage = getattr(response, "usage_metadata", None)
    tokens: dict[str, int] = {}
    for item in getattr(usage, "prompt_tokens_details", None) or []:
        modality = getattr(item, "modality", None)
        name = str(getattr(modality, "value", modality))
        count = getattr(item, "token_count", None)
        if isinstance(count, int) and count > 0:
            tokens[name] = tokens.get(name, 0) + count
    if "AUDIO" in tokens:
        return tokens["AUDIO"] / AUDIO_TOKENS_PER_S
    if "VIDEO" in tokens:
        return tokens["VIDEO"] / LOW_RES_FRAME_TOKENS
    return None


def _download_video(youtube_id: str, dest_dir: Path) -> Path:
    """yt-dlp, buscado recién al usarlo (con la descarga apagada nunca se llega acá)."""
    return video_module.download_video(youtube_id, dest_dir)


def _state(file) -> str:
    state = getattr(file, "state", None)
    return str(getattr(state, "value", state))


class GeminiProvider:
    def __init__(self, api_key: str, analyze_models: list[str] | None = None,
                 blind_models: list[str] | None = None, client=None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time,
                 download: Callable[[str, Path], Path] = _download_video,
                 video_dir: Path | None = None, youtube_url_denied: bool = False,
                 allow_download: bool = False):
        self._analyze_models = list(analyze_models or config.MODEL_CHAINS["analysis"])
        self._blind_models = list(blind_models or config.MODEL_CHAINS["blind"])
        self._sleep = sleep
        self._clock = clock
        self._download = download
        self._video_dir = video_dir
        # Huella corta de la clave (nunca la clave): separa el cache de modelos y las
        # marcas de cuota de cada clave (la cuota es por proyecto).
        self.key_id = hashlib.sha256(api_key.encode()).hexdigest()[:12]
        # Algunas claves no pueden pasarle a Gemini un enlace de YouTube (403 en toda la
        # cadena). Se recuerda 7 días (epoch hasta cuándo) para no gastar un pedido por video.
        self.youtube_url_denied_until = 0.0
        self.youtube_url_denied = youtube_url_denied
        # Bajar el video y subirlo cuando el enlace no alcanza: solo si el estudiante lo
        # activó en Ajustes (la tubería lo carga de kv "allow_download").
        self.allow_download = allow_download
        # Videos cuyo enlace rechazó toda la cadena (la tubería los recuerda en el día).
        self.url_denied_videos: set[str] = set()
        self.last_video_seconds: float | None = None
        # True si Gemini leyó el video por el enlace en el último análisis (cuenta para
        # el tope diario de YouTube), aunque la respuesta no haya servido.
        self.last_link_sent = False
        self._rotation = ModelRotation(clock)
        self.last_analysis_model: str | None = None
        self.last_blind_model: str | None = None
        # Pedidos a Gemini por función desde la última lectura (quien lee lo reinicia).
        self.requests: dict[str, int] = {"analysis": 0, "blind": 0, "pdf": 0}
        if client is None:
            client = genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_S * 1000),
            )
        self._client = client

    @property
    def youtube_url_denied(self) -> bool:
        return self.youtube_url_denied_until > self._clock()

    @youtube_url_denied.setter
    def youtube_url_denied(self, denied: bool) -> None:
        self.youtube_url_denied_until = self._clock() + YOUTUBE_URL_DENIED_S if denied else 0.0

    # --- Cadena de modelos -------------------------------------------------------

    def _listed_models(self) -> frozenset[str] | None:
        """Modelos que esta clave puede usar; None si no se pudo consultar (no se cachea)."""
        cached = _LISTED_MODELS.get(self.key_id)
        if cached is not None:
            return cached
        try:
            names = frozenset(_short_name(m.name) for m in self._client.models.list()
                              if _can_generate(m))
        except (errors.APIError, httpx.TransportError):
            return None
        if names:
            _LISTED_MODELS[self.key_id] = names
        return names or None

    @property
    def exhausted_until(self) -> dict[str, float]:
        """Modelo agotado por el día -> epoch en que vuelve (la tubería lo carga y guarda)."""
        return self._rotation.exhausted_until

    def _rotate(self, chain: list[str], attempt: Callable[[str], object]):
        """Corre `attempt` sobre los modelos de la cadena que esta clave puede usar."""
        listed = self._listed_models()
        return self._rotation.run([m for m in chain if listed is None or m in listed], attempt)

    def _count_request(self, function: str) -> None:
        self.requests[function] = self.requests.get(function, 0) + 1

    def _generate(self, model: str, contents, gen_config, *, video: str | None = None,
                  function: str):
        """Un pedido a `model`. `video`: de dónde sale el video (YOUTUBE_URL o UPLOADED_FILE),
        DOCUMENT si es un PDF o diapositivas, None si es solo texto. `function`: a qué función
        se le cuenta el pedido ("analysis", "blind" o "pdf"); un 429 no se cuenta porque no
        gasta cuota de Google.
        Lanza NextModel si conviene probar el siguiente modelo."""
        quota_tries = transient_tries = 0
        while True:
            try:
                response = self._client.models.generate_content(
                    model=model, contents=contents, config=gen_config
                )
                self._count_request(function)
                return response
            except errors.APIError as e:
                if is_quota(e):
                    if is_daily_quota(e):
                        self._rotation.mark_exhausted(model, retry_delay(e))
                        raise NextModel(DAILY) from None
                    delay = retry_delay(e)
                    if quota_tries < QUOTA_RETRIES and delay is not None and delay <= MAX_RETRY_WAIT_S:
                        quota_tries += 1
                        log.info("Gemini: %s llegó al límite por minuto; se espera %.0f s", model, delay)
                        self._sleep(delay)
                        continue
                    raise NextModel(QUOTA) from None
                self._count_request(function)  # un 429 no gasta cuota de Google; lo demás sí
                if is_transient(e):
                    if transient_tries < TRANSIENT_RETRIES:
                        transient_tries += 1
                        log.info("Gemini: %s no respondió (código %s); se reintenta", model, e.code)
                        self._sleep(OVERLOAD_WAIT_S)
                        continue
                    raise NextModel(TRANSIENT) from None
                if is_unsupported(e, youtube_url=video == YOUTUBE_URL):
                    raise NextModel(UNSUPPORTED) from None
                if video == YOUTUBE_URL and is_permission_denied(e):
                    # Otro modelo puede aceptar el enlace: se prueba antes de darlo por rechazado.
                    log.info("Gemini: %s no aceptó el enlace de YouTube; se prueba otro", model)
                    raise NextModel(DENIED) from None
                self._raise_mapped(e, video)
            except httpx.TransportError:
                # No se cuenta: no hubo respuesta de Google y no se sabe si el pedido se
                # facturó; el presupuesto diario es una guarda blanda.
                raise AIUnavailableError("no se pudo conectar con Gemini") from None

    @staticmethod
    def _raise_mapped(e: errors.APIError, video: str | None):
        """Traduce el error a uno propio; los mensajes llevan solo el código, nunca la clave.

        Con video o documento, un 403 no prueba que la clave sea mala (la de texto puede
        andar): la clave se juzga solo con pedidos de texto.
        """
        code = e.code
        if code == 401 or (code == 400 and is_invalid_key(e)) or (code == 403 and video is None):
            raise AIAuthError(f"Gemini rechazó la clave (código {code})") from None
        if code == 408 or (isinstance(code, int) and code >= 500):
            raise AIUnavailableError(f"Gemini no disponible (código {code})") from None
        if video == DOCUMENT and code in (400, 403):
            raise AIDocumentUnreadableError(DOCUMENT_UNREADABLE) from None
        if video == YOUTUBE_URL and code == 400 and mentions_url(e):
            raise _YoutubeUrlRejected(remember=False) from None
        if video and code in (400, 403):
            raise AIVideoUnavailableError(f"Gemini no pudo abrir el video (código {code})") from None
        raise AIFormatError(f"pedido inválido a Gemini (código {code})") from None

    def analyze_video(self, youtube_id: str, title: str) -> VideoAnalysis:
        """Primero con el enlace de YouTube. Si Gemini no lo acepta: con la descarga
        activada baja el video y lo sube; si no, AINeedsPublicLinkError (sin otro pedido)."""
        self.last_analysis_model = None
        self.last_video_seconds = None
        self.last_link_sent = False
        prompt = types.Part(text=ANALYSIS_PROMPT.format(title=title))
        if not self.youtube_url_denied:
            url = f"https://www.youtube.com/watch?v={youtube_id}"
            try:
                return self._analyze(types.FileData(file_uri=url), prompt, YOUTUBE_URL)
            except _YoutubeUrlRejected as e:
                if e.remember:
                    self._remember_denied(youtube_id)
        if not self.allow_download:
            raise AINeedsPublicLinkError("Gemini no acepta el enlace y la descarga está apagada")
        return self._analyze_uploaded(youtube_id, prompt)

    def _remember_denied(self, youtube_id: str) -> None:
        """Un 403 en toda la cadena puede ser un video privado: la clave queda marcada
        recién cuando le pasa a DENIED_VIDEOS_FOR_KEY videos distintos."""
        self.url_denied_videos.add(youtube_id)
        if len(self.url_denied_videos) >= DENIED_VIDEOS_FOR_KEY:
            self.youtube_url_denied = True

    def _analyze(self, file_data: types.FileData, prompt: types.Part, video: str) -> VideoAnalysis:
        """Rota por la cadena de análisis; el mismo video (enlace o subido) sirve para todos."""
        contents = types.Content(parts=[types.Part(file_data=file_data), prompt])

        def attempt(model: str) -> VideoAnalysis:
            response = self._generate(model, contents, _analysis_config(), video=video,
                                       function="analysis")
            if video == YOUTUBE_URL:
                self.last_link_sent = True
            try:
                analysis = VideoAnalysis.model_validate_json(response.text or "")
            except ValidationError:
                raise AIFormatError("la respuesta no es un análisis válido") from None
            analysis = _validate(analysis)
            seconds = _video_seconds(response)
            # Tramos inválidos se descartan: nunca invalidan el análisis.
            analysis = analysis.model_copy(
                update={"segments": valid_segments(analysis.segments, seconds)})
            self.last_analysis_model = model
            self.last_video_seconds = seconds
            return analysis

        try:
            return self._rotate(self._analyze_models, attempt)
        except LinkDenied:
            raise _YoutubeUrlRejected(remember=True) from None

    def _analyze_uploaded(self, youtube_id: str, prompt: types.Part) -> VideoAnalysis:
        """Baja el video, lo sube a Gemini, lo analiza y borra la copia remota siempre."""
        video_dir = self._video_dir or data_dir() / "videos"
        try:
            path = self._download(youtube_id, video_dir)
        except (VideoDownloadError, OSError):
            raise AIVideoUnavailableError("no se pudo bajar el video") from None
        uploaded = None
        try:
            uploaded = self._upload(path)
            active = self._wait_active(uploaded)
            file_data = types.FileData(file_uri=active.uri, mime_type=active.mime_type)
            analysis = self._analyze(file_data, prompt, UPLOADED_FILE)
        except (AIQuotaError, AIUnavailableError, AIAuthError):
            raise  # el material queda pendiente: la copia local se reusa en el reintento
        except Exception:
            _remove_local(path)  # el material termina fallido: la copia ya no sirve
            raise
        finally:
            if uploaded is not None:
                self._delete_remote(uploaded)
        # El análisis queda guardado en la base: la copia local ya no hace falta.
        _remove_local(path)
        return analysis

    def _upload(self, path: Path, mime: str | None = None, source: str = UPLOADED_FILE):
        """Sube `path` a la API de archivos. `source`: UPLOADED_FILE (video) o DOCUMENT."""
        try:
            return self._client.files.upload(file=path, config={"mime_type": mime or mime_type(path)})
        except errors.APIError as e:
            if is_quota(e):
                raise AIQuotaError("cuota de Gemini agotada") from None
            if source == DOCUMENT:
                self._raise_mapped(e, DOCUMENT)  # 400/403: ilegible; 5xx: se reintenta después
            raise AIVideoUnavailableError(f"no se pudo subir el video (código {e.code})") from None
        except (httpx.TransportError, OSError):
            if source == DOCUMENT:
                # No es culpa del archivo: el material queda pendiente y se reintenta.
                raise AIUnavailableError("no se pudo subir el archivo a Gemini") from None
            raise AIVideoUnavailableError("no se pudo subir el video") from None

    def _wait_active(self, uploaded, source: str = UPLOADED_FILE):
        file, waited = uploaded, 0.0
        while _state(file) != "ACTIVE":
            if _state(file) == "FAILED" or waited >= FILE_ACTIVE_TIMEOUT_S:
                if source == DOCUMENT:
                    raise AIDocumentUnreadableError(DOCUMENT_UNREADABLE)
                raise AIVideoUnavailableError("Gemini no pudo procesar el video subido")
            self._sleep(FILE_POLL_S)
            waited += FILE_POLL_S
            try:
                file = self._client.files.get(name=uploaded.name)
            except errors.APIError as e:
                if is_quota(e):
                    raise AIQuotaError("cuota de Gemini agotada") from None
                self._raise_mapped(e, source)
            except httpx.TransportError:
                raise AIUnavailableError("no se pudo conectar con Gemini") from None
        return file

    def _delete_remote(self, uploaded) -> None:
        try:
            self._client.files.delete(name=uploaded.name)
        except Exception as exc:  # noqa: BLE001 - Gemini lo borra solo a las 48 h
            log.warning("No se pudo borrar el archivo subido a Gemini (%s)", type(exc).__name__)

    # --- Documentos (PDF y diapositivas) ----------------------------------------

    def _document_part(self, data: bytes, mime: str) -> tuple[types.Part, object | None]:
        """La parte del pedido con el documento y el archivo subido (None si va adentro).

        Hasta INLINE_DOCUMENT_MAX_BYTES va dentro del pedido. Si pesa más, se sube como el
        video: quien llama borra el archivo subido en un `finally` con `_delete_remote`.
        """
        if len(data) <= INLINE_DOCUMENT_MAX_BYTES:
            return types.Part.from_bytes(data=data, mime_type=mime), None
        path = _write_upload_copy(data, _UPLOAD_SUFFIXES.get(mime, ".bin"))
        try:
            uploaded = self._upload(path, mime, DOCUMENT)
        finally:
            _remove_local(path)  # ya está subido (o falló): la copia local no hace falta
        try:
            active = self._wait_active(uploaded, DOCUMENT)
        except BaseException:
            self._delete_remote(uploaded)
            raise
        part = types.Part(file_data=types.FileData(file_uri=active.uri, mime_type=active.mime_type))
        return part, uploaded

    def analyze_document(self, doc: DocumentInput) -> DocumentAnalysis:
        """Resumen, temas y preguntas con la página citada. Rota la cadena de análisis y
        cada pedido cuenta como "pdf". El PDF va como documento; las diapositivas, como texto."""
        self.last_analysis_model = None
        uploaded = None
        try:
            if doc.data is not None:
                doc_part, uploaded = self._document_part(doc.data, PDF_MIME)
                source = "el apunte en PDF"
            elif doc.text is not None:
                doc_part, source = types.Part(text=doc.text), "las diapositivas"
            else:
                raise AIFormatError("el documento no tiene contenido para mandar")
            prompt = types.Part(text=DOCUMENT_PROMPT.format(
                title=doc.title, source=source, pages_note=_pages_note(doc)))
            contents = types.Content(parts=[doc_part, prompt])

            def attempt(model: str) -> DocumentAnalysis:
                response = self._generate(model, contents, _document_config(), video=DOCUMENT,
                                           function="pdf")
                try:
                    analysis = DocumentAnalysis.model_validate_json(response.text or "")
                except ValidationError:
                    raise AIFormatError("la respuesta no es un análisis válido") from None
                analysis = _validate_document(analysis, doc.pages)
                self.last_analysis_model = model
                return analysis

            return self._rotate(self._analyze_models, attempt)
        finally:
            if uploaded is not None:
                self._delete_remote(uploaded)

    def extract_exam_style(self, exam: PastExamInput, course_topics: list[str]) -> ExtractedStyle:
        """El formato de un parcial viejo, en un pedido (rota la cadena de análisis; cuenta
        como "pdf"). El prompt prohíbe transcribir enunciados y solo vuelven números y temas."""
        mime = PAST_EXAM_MIMES.get(exam.kind)
        if mime is None:
            raise AIFormatError("tipo de parcial desconocido")
        uploaded = None
        try:
            doc_part, uploaded = self._document_part(exam.data, mime)
            prompt = types.Part(text=STYLE_PROMPT.format(
                course_topics=_course_topics_text(course_topics)))
            contents = types.Content(parts=[doc_part, prompt])

            def attempt(model: str) -> ExtractedStyle:
                response = self._generate(model, contents, _style_config(), video=DOCUMENT,
                                           function="pdf")
                try:
                    return ExtractedStyle.model_validate_json(response.text or "")
                except ValidationError:
                    raise AIFormatError("la respuesta no es un estilo válido") from None

            return self._rotate(self._analyze_models, attempt)
        finally:
            if uploaded is not None:
                self._delete_remote(uploaded)

    def generate_style_questions(self, request: StyleRequest) -> list[StyleQuestion]:
        """Una tanda con el estilo del examen, solo con texto (los resúmenes de la clase). Rota la
        cadena de análisis, cuenta como "analysis" y anota el modelo en `last_analysis_model` para
        que la pasada a ciegas no lo use. Las preguntas mal armadas se descartan; si no queda
        ninguna, AIFormatError."""
        self.last_analysis_model = None
        prompt = style_questions_prompt(request)

        def attempt(model: str) -> list[StyleQuestion]:
            response = self._generate(model, prompt, _style_questions_config(), function="analysis")
            try:
                batch = StyleQuestions.model_validate_json(response.text or "")
            except ValidationError:
                raise AIFormatError("la respuesta no es una tanda válida") from None
            questions = [q for q in batch.questions if _question_ok(q)]
            if not questions:
                raise AIFormatError("no hay preguntas válidas")
            self.last_analysis_model = model
            return questions

        return self._rotate(self._analyze_models, attempt)

    def check_key(self) -> None:
        """Valida la clave listando modelos: no gasta pedidos de generación."""
        try:
            names = frozenset(_short_name(m.name) for m in self._client.models.list()
                              if _can_generate(m))
        except errors.APIError as e:
            if is_quota(e):
                return  # la clave autenticó: solo hay límite de pedidos
            code = e.code
            if code in (401, 403) or (code == 400 and is_invalid_key(e)):
                raise AIAuthError(f"Gemini rechazó la clave (código {code})") from None
            if code == 408 or (isinstance(code, int) and code >= 500):
                raise AIUnavailableError(f"Gemini no disponible (código {code})") from None
            raise AIFormatError(f"pedido inválido a Gemini (código {code})") from None
        except httpx.TransportError:
            raise AIUnavailableError("no se pudo conectar con Gemini") from None
        if names:
            _LISTED_MODELS[self.key_id] = names

    def answer_blind_batch(self, items: BlindItems,
                           exclude_models: Collection[str] = ()) -> list[int | None]:
        """Un solo pedido para todas las preguntas; solo enunciado y opciones (nunca la
        respuesta ni la explicación). Una respuesta ilegible prueba el siguiente modelo."""
        if not items:
            return []
        prompt = blind_prompt(items)
        chain = [m for m in self._blind_models if m not in exclude_models]

        def attempt(model: str) -> list[int | None]:
            response = self._generate(model, prompt, blind_config(model), function="blind")
            answers = parse_blind(response.text or "", items)
            if answers is None:
                log.info("Gemini: %s no devolvió respuestas legibles; se prueba otro", model)
                raise NextModel(UNREADABLE)
            self.last_blind_model = model
            return answers

        return self._rotate(chain, attempt)


_UPLOAD_SUFFIXES = {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png"}


def _write_upload_copy(data: bytes, suffix: str) -> Path:
    """Escribe la copia para subir. AIUnavailableError (y nada a medio escribir) si falla el disco."""
    folder = config.data_dir()
    path = folder / f"{UPLOAD_COPY_PREFIX}{token_hex(8)}{suffix}"
    try:
        with open(path, "xb") as fh:
            fh.write(data)
    except OSError:
        _remove_local(path)  # quedó una copia a medio escribir
        # Falla del disco, no del archivo: el material queda pendiente y se reintenta.
        raise AIUnavailableError("no se pudo preparar el archivo para subirlo") from None
    return path


def _remove_local(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("No se pudo borrar la copia local (%s)", type(exc).__name__)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("uso: python -m estudia.ai.gemini <youtube_id>", file=sys.stderr)
        return 2
    key = os.environ.get("ESTUDIA_GEMINI_KEY")
    if not key:
        print("falta la variable de entorno ESTUDIA_GEMINI_KEY", file=sys.stderr)
        return 2
    # Mismo bloqueo que la app: sin respuesta de edad adulta en la base real, no se envía nada.
    if not age_is_adult():
        print("la IA no está habilitada (falta la respuesta de edad o es menor)", file=sys.stderr)
        return 3
    analysis = GeminiProvider(key).analyze_video(argv[1], argv[1])
    print(analysis.summary_md)
    print(f"\nTemas: {', '.join(analysis.topics)}")
    print(f"Preguntas: {len(analysis.questions)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
