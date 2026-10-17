"""Dobles de prueba compartidos."""
from collections.abc import Callable
from types import SimpleNamespace

from google.genai import errors, types

from estudia.ai.base import AIFormatError, VideoAnalysis


class FakeProvider:
    """Proveedor falso. `blind(stem, options)` responde cada pregunta de la pasada a
    ciegas; si lanza AIFormatError, esa pregunta queda sin respuesta (None)."""

    youtube_url_denied = False
    key_id = "fake"

    def __init__(
        self,
        analyses: dict[str, VideoAnalysis],
        blind: Callable[[str, list[str]], int | None],
        analysis_model: str | None = "analizador",
    ):
        self.analyses = analyses
        self.blind = blind
        self.analysis_model = analysis_model
        self.exhausted_until: dict[str, float] = {}
        self.last_analysis_model: str | None = None
        self.blind_batches: list[tuple[list[tuple[str, list[str]]], tuple[str, ...]]] = []

    def analyze_video(self, youtube_id: str, title: str) -> VideoAnalysis:
        if youtube_id not in self.analyses:
            raise AIFormatError(f"sin análisis simulado para {youtube_id}")
        self.last_analysis_model = self.analysis_model
        return self.analyses[youtube_id]

    def answer_blind(self, stem: str, options: list[str]) -> int | None:
        try:
            return self.blind(stem, options)
        except AIFormatError:
            return None

    def answer_blind_batch(self, items, exclude_models=()):
        self.blind_batches.append((items, tuple(exclude_models)))
        return [self.answer_blind(stem, options) for stem, options in items]

    def check_key(self) -> None:
        pass


class DeniedYoutubeUrlClient:
    """Gemini falso: 403 para el enlace de YouTube; con el video subido, `analysis_text`.

    `blind`: texto de la respuesta a la pasada a ciegas (un JSON con "answers")."""

    def __init__(self, analysis_text=None, blind='{"answers": [{"q": 1, "answer": 1}]}'):
        self.analysis_text, self.blind = analysis_text, blind
        self.models = self
        self.files = self
        self.deleted = []

    def generate_content(self, model, contents, config):
        if isinstance(contents, str):
            return SimpleNamespace(text=self.blind)
        uri = contents.parts[0].file_data.file_uri
        if "youtube.com" in uri:
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
