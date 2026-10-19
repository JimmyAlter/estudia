"""Dobles de prueba compartidos."""
from collections.abc import Callable
from types import SimpleNamespace

from google.genai import errors, types

from estudia.ai.base import (
    AIFormatError,
    AIQuotaError,
    DocumentAnalysis,
    DocumentInput,
    ExtractedStyle,
    PastExamInput,
    StyleQuestion,
    StyleRequest,
    VideoAnalysis,
)


class FakeProvider:
    """Proveedor falso. `blind(stem, options)` responde cada pregunta de la pasada a
    ciegas; si lanza AIFormatError, esa pregunta queda sin respuesta (None)."""

    youtube_url_denied_until = 0.0
    allow_download = False
    key_id = "fake"

    def __init__(
        self,
        analyses: dict[str, VideoAnalysis],
        blind: Callable[[str, list[str]], int | None],
        analysis_model: str | None = "analizador",
        video_seconds: float | None = None,
        by_link: bool = True,
        models_by_ref: dict[str, str] | None = None,
        blind_errors: list[Exception | None] | None = None,
        blind_models: tuple[str, ...] | None = None,
        documents: dict[str, DocumentAnalysis] | None = None,
        styles: dict[bytes, ExtractedStyle] | None = None,
        style_batches: list[list[StyleQuestion]] | None = None,
    ):
        self.analyses = analyses
        self.video_seconds = video_seconds
        self.by_link = by_link
        self.last_video_seconds: float | None = None
        self.last_link_sent = False
        self.url_denied_videos: set[str] = set()
        self.analyzed_refs: list[str] = []
        self.blind = blind
        self.analysis_model = analysis_model
        # Modelo de análisis por id de video (pisa a `analysis_model`).
        self.models_by_ref = models_by_ref or {}
        self.requests: dict[str, int] = {"analysis": 0, "blind": 0, "pdf": 0}
        # Análisis de documentos por título (`doc.title`) y documentos que se mandaron.
        self.documents = documents or {}
        self.analyzed_docs: list[DocumentInput] = []
        # Estilos de parciales viejos por sus bytes, y lo que se mandó (parcial, temas del curso).
        self.styles = styles or {}
        self.style_calls: list[tuple[PastExamInput, list[str]]] = []
        # Tandas con el estilo del examen, en orden (una por pedido), y lo que se pidió.
        self.style_batches = list(style_batches or [])
        self.style_requests: list[StyleRequest] = []
        self.exhausted_until: dict[str, float] = {}
        self.last_analysis_model: str | None = None
        self.blind_batches: list[tuple[list[tuple[str, list[str]]], tuple[str, ...]]] = []
        # Errores para los próximos pedidos a ciegas, en orden (uno por pedido; None = sin error).
        self.blind_errors = list(blind_errors or [])
        # Cadena a ciegas simulada: si todos sus modelos están excluidos, AIQuotaError
        # sin pedido (como la rotación real con la cadena vacía). None = sin cadena.
        self.blind_models = blind_models

    def analyze_video(self, youtube_id: str, title: str) -> VideoAnalysis:
        self.analyzed_refs.append(youtube_id)
        self.last_link_sent = False
        if youtube_id not in self.analyses:
            raise AIFormatError(f"sin análisis simulado para {youtube_id}")
        self.requests["analysis"] += 1
        self.last_analysis_model = self.models_by_ref.get(youtube_id, self.analysis_model)
        self.last_video_seconds = self.video_seconds
        self.last_link_sent = self.by_link
        return self.analyses[youtube_id]

    def analyze_document(self, doc: DocumentInput) -> DocumentAnalysis:
        self.analyzed_docs.append(doc)
        if doc.title not in self.documents:
            raise AIFormatError(f"sin análisis simulado para {doc.title}")
        self.requests["pdf"] += 1
        self.last_analysis_model = self.models_by_ref.get(doc.title, self.analysis_model)
        return self.documents[doc.title]

    def extract_exam_style(self, exam: PastExamInput, course_topics: list[str]) -> ExtractedStyle:
        self.style_calls.append((exam, list(course_topics)))
        if exam.data not in self.styles:
            raise AIFormatError("sin estilo simulado para ese parcial")
        self.requests["pdf"] += 1
        return self.styles[exam.data]

    def generate_style_questions(self, request: StyleRequest) -> list[StyleQuestion]:
        self.style_requests.append(request)
        if not self.style_batches:
            raise AIFormatError("sin tanda simulada")
        self.requests["analysis"] += 1
        self.last_analysis_model = self.analysis_model
        return self.style_batches.pop(0)

    def answer_blind(self, stem: str, options: list[str]) -> int | None:
        try:
            return self.blind(stem, options)
        except AIFormatError:
            return None

    def answer_blind_batch(self, items, exclude_models=()):
        if self.blind_models is not None and all(m in exclude_models for m in self.blind_models):
            raise AIQuotaError("no queda modelo a ciegas")
        self.requests["blind"] += 1
        self.blind_batches.append((items, tuple(exclude_models)))
        if self.blind_errors and (error := self.blind_errors.pop(0)) is not None:
            raise error
        return [self.answer_blind(stem, options) for stem, options in items]

    def check_key(self) -> None:
        pass


class DeniedYoutubeUrlClient:
    """Gemini falso: 403 para el enlace de YouTube; con el video subido, `analysis_text`.

    `blind`: texto de la respuesta a la pasada a ciegas (un JSON con "answers")."""

    def __init__(self, analysis_text=None, blind='{"answers": [{"q": 1, "answer": 1}]}', denied=None):
        """`denied`: ids de YouTube que dan 403 (None = todos); los demás responden por el enlace."""
        self.analysis_text, self.blind, self.denied_ids = analysis_text, blind, denied
        self.models = self
        self.files = self
        self.deleted = []
        self.url_calls = 0

    def generate_content(self, model, contents, config):
        if isinstance(contents, str):
            return SimpleNamespace(text=self.blind)
        uri = contents.parts[0].file_data.file_uri
        if "youtube.com" in uri:
            self.url_calls += 1
            if self.denied_ids is not None and uri.rsplit("=", 1)[-1] not in self.denied_ids:
                return SimpleNamespace(text=self.analysis_text)
            raise errors.ClientError(403, {"error": {
                "code": 403, "status": "PERMISSION_DENIED",
                "message": "The caller does not have permission"}})
        return SimpleNamespace(text=self.analysis_text)

    def list(self):
        return []  # sin listado: se usa la cadena configurada tal cual

    def upload(self, file, config):
        return types.File(name="files/x", uri="https://gemini/files/x",
                          mime_type=config["mime_type"], state=types.FileState.ACTIVE)

    def delete(self, name):
        self.deleted.append(name)


def commit_answers(conn) -> None:
    """Para tests que insertan respuestas con SQL crudo: refleja en `review_state` lo que entró
    (como hacen `connect` y `record_answer`) y confirma. Las lecturas ya no sincronizan."""
    from estudia.study.srs import sync_review_state

    sync_review_state(conn)
    conn.commit()
