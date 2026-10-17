"""Contrato del proveedor de IA y tipos compartidos."""
import re
from collections.abc import Collection
from typing import Protocol

from pydantic import BaseModel


class AIQuotaError(Exception):
    """Se agotó la cuota o el límite de pedidos del proveedor.

    `resets_at`: epoch en que vuelve el primer modelo agotado por el día, si se sabe.
    """

    def __init__(self, message: str = "cuota agotada", resets_at: float | None = None):
        super().__init__(message)
        self.resets_at = resets_at


class AIFormatError(Exception):
    """La respuesta del proveedor no cumple el formato esperado (o el pedido es inválido)."""


class AIVideoUnavailableError(AIFormatError):
    """Gemini no pudo abrir el video (400 al analizarlo): suele ser privado o no listado."""


class AIUnavailableError(Exception):
    """Falla transitoria (5xx, 408, red, timeout): conviene reintentar más tarde."""


class AIAuthError(Exception):
    """Clave rechazada o revocada (401/403): fatal para toda la corrida."""


def option_key(text: str) -> str:
    """Forma de comparar opciones: sin espacios y sin distinguir mayúsculas."""
    return re.sub(r"\s+", "", text).casefold()


class GenQuestion(BaseModel):
    stem: str
    options: list[str]  # exactamente 4
    correct_index: int  # de 0 a 3
    explanation: str
    common_error: str
    topic: str  # idealmente uno de VideoAnalysis.topics; si no, se crea el tema
    source_seconds: int
    check_expr: str | None = None


class VideoAnalysis(BaseModel):
    summary_md: str
    topics: list[str]  # de 1 a 3 temas
    questions: list[GenQuestion]


class AIProvider(Protocol):
    # True si el proveedor no puede recibir el enlace de YouTube y tiene que subir el
    # video. La tubería lo recuerda entre corridas (kv "youtube_url_input").
    youtube_url_denied: bool
    # Huella corta de la clave (nunca la clave): las marcas de cuota son por clave.
    key_id: str
    # Modelo agotado por el día -> epoch en que vuelve. La tubería lo carga y lo guarda
    # en kv ("model_exhausted:<key_id>:<modelo>") para no gastar pedidos en un modelo agotado.
    exhausted_until: dict[str, float]
    # Modelo que hizo el último análisis bien hecho: la pasada a ciegas no lo usa.
    last_analysis_model: str | None

    def analyze_video(self, youtube_id: str, title: str) -> VideoAnalysis: ...

    def answer_blind_batch(self, items: list[tuple[str, list[str]]],
                           exclude_models: Collection[str] = ()) -> list[int | None]:
        """Responde a ciegas (sin respuesta correcta ni explicación) todas las preguntas
        en un solo pedido. Un índice por pregunta, en el mismo orden; None si no sabe.
        `exclude_models`: modelos que no se pueden usar (el que hizo el análisis)."""
        ...

    def check_key(self) -> None:
        """Confirma que la clave autentica sin gastar cuota de generación. Lanza los errores de arriba."""
        ...
