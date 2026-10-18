"""Contrato del proveedor de IA y tipos compartidos."""
import re
from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal, Protocol

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


class AINeedsPublicLinkError(AIVideoUnavailableError):
    """Gemini no acepta el enlace y la descarga está apagada: hace falta un video público
    (o activar la descarga en Ajustes). No se gastó ningún pedido extra."""


class AIDocumentUnreadableError(AIFormatError):
    """Gemini no pudo leer el PDF o las diapositivas (400 o 403 al analizarlo)."""


class AIUnavailableError(Exception):
    """Falla transitoria (5xx, 408, red, timeout): conviene reintentar más tarde."""


class AIAuthError(Exception):
    """Clave rechazada o revocada (401/403): fatal para toda la corrida."""


def option_key(text: str) -> str:
    """Forma de comparar opciones: sin espacios y sin distinguir mayúsculas."""
    return re.sub(r"\s+", "", text).casefold()


class VariantParam(BaseModel):
    name: str  # una letra minúscula
    low: int
    high: int  # inclusive


# Plantilla de variantes numéricas de una pregunta con `check_expr`. La valida
# `generation.variants.template_problems`; nada se evalúa fuera del worker de SymPy.
# Sin docstring a propósito: iría como "description" en el esquema que se le manda a Gemini.
class VariantTemplate(BaseModel):
    params: list[VariantParam]  # de 1 a 3
    original: list[int]  # valores de la pregunta original, en el orden de params
    stem: str  # con {a}, {b}…
    answer_expr: str  # SymPy con los parámetros
    distractor_exprs: list[str]  # 3, en el orden de los distractores en options
    explanation: str  # con {a} y {respuesta}, sin pasos intermedios
    common_error: str  # con {a} y {respuesta}, sin pasos intermedios


class GenQuestion(BaseModel):
    stem: str
    options: list[str]  # exactamente 4
    correct_index: int  # de 0 a 3
    explanation: str
    common_error: str
    topic: str  # idealmente uno de VideoAnalysis.topics; si no, se crea el tema
    source_seconds: int
    check_expr: str | None = None
    # Un análisis guardado sin plantilla se sigue leyendo.
    variant: VariantTemplate | None = None


class Segment(BaseModel):
    start_s: int
    end_s: int
    title: str


class VideoAnalysis(BaseModel):
    summary_md: str
    topics: list[str]  # de 1 a 3 temas
    questions: list[GenQuestion]
    # Tramos con marcas de tiempo; un análisis viejo guardado sin ellos se sigue leyendo.
    segments: list[Segment] = []


class DocQuestion(BaseModel):
    stem: str
    options: list[str]  # exactamente 4
    correct_index: int  # de 0 a 3
    explanation: str
    common_error: str
    topic: str
    source_page: int | None  # página del archivo (desde 1) o número de diapositiva
    check_expr: str | None = None
    variant: VariantTemplate | None = None


class DocumentAnalysis(BaseModel):
    summary_md: str
    topics: list[str]  # de 1 a 3 temas
    questions: list[DocQuestion]


@dataclass
class DocumentInput:
    """Lo que se le manda a Gemini de un documento.

    `data`: el PDF (ya cortado si es un tramo); `text`: el texto de las diapositivas.
    `pages`: páginas (o diapositivas) de lo que se manda; valida `source_page`.
    `part`: es un tramo de un documento más largo (se le dice a Gemini cuántas páginas tiene,
    porque las hojas traen impresos los números del documento entero).
    """

    kind: str
    title: str
    pages: int
    data: bytes | None = None
    text: str | None = None
    part: bool = False


ExerciseKind = Literal["opcion_multiple", "verdadero_falso", "desarrollo"]


class ExtractedExercise(BaseModel):
    kind: ExerciseKind
    points: float | None = None


class ExtractedStyle(BaseModel):
    """El formato de un parcial viejo, sin ningún enunciado: solo números y nombres de temas."""

    exercises: list[ExtractedExercise]
    minutes: int | None
    max_grade: float | None
    topics: list[str]
    formula_sheet: bool


class StyleQuestion(BaseModel):
    """Una pregunta generada con el estilo del examen. Siempre 4 opciones y una sola correcta,
    también en verdadero/falso y en desarrollo: así pasa por la misma verificación que todas."""

    stem: str
    options: list[str]  # exactamente 4
    correct_index: int  # de 0 a 3
    explanation: str
    common_error: str
    topic: str
    kind: ExerciseKind
    check_expr: str | None = None


class StyleQuestions(BaseModel):
    questions: list[StyleQuestion]


@dataclass
class StyleRequest:
    """Lo que se le pide a Gemini para una tanda con el estilo del examen.

    `kinds`: cuántas de cada tipo (la suma va de 1 a 10). `material_text`: los resúmenes de la
    clase de esos temas; la generación se apoya solo en eso.
    """

    course: str
    kinds: dict[str, int]
    topics: list[str]
    material_text: str
    formula_sheet: bool


@dataclass
class PastExamInput:
    """Un parcial viejo para mandarle a Gemini. `kind`: "pdf", "jpeg" o "png" (por los bytes)."""

    kind: str
    data: bytes


class AIProvider(Protocol):
    # Epoch hasta el que no se prueba el enlace de YouTube (Gemini lo rechazó con esta
    # clave); 0 = se prueba. La tubería lo recuerda entre corridas (kv "youtube_url_input").
    youtube_url_denied_until: float
    # Si se puede bajar el video y subirlo cuando el enlace no alcanza (kv "allow_download").
    allow_download: bool
    # Huella corta de la clave (nunca la clave): las marcas de cuota son por clave.
    key_id: str
    # Modelo agotado por el día -> epoch en que vuelve. La tubería lo carga y lo guarda
    # en kv ("model_exhausted:<key_id>:<modelo>") para no gastar pedidos en un modelo agotado.
    exhausted_until: dict[str, float]
    # Modelo que hizo el último análisis bien hecho: la pasada a ciegas no lo usa.
    last_analysis_model: str | None
    # Segundos de video del último análisis, según Gemini; None si no se sabe.
    last_video_seconds: float | None
    # Pedidos hechos por función ("analysis", "blind", "pdf") desde la última lectura.
    requests: dict[str, int]

    def analyze_video(self, youtube_id: str, title: str) -> VideoAnalysis: ...

    def analyze_document(self, doc: DocumentInput) -> DocumentAnalysis:
        """Resumen, temas y preguntas de un PDF o de diapositivas; cuenta como "pdf"."""
        ...

    def extract_exam_style(self, exam: PastExamInput, course_topics: list[str]) -> ExtractedStyle:
        """El formato de un parcial viejo (cantidad y tipo de ejercicios, puntaje, tiempo,
        nota máxima, temas y hoja de fórmulas), sin transcribir enunciados; cuenta como "pdf"."""
        ...

    def generate_style_questions(self, request: StyleRequest) -> list[StyleQuestion]:
        """Una tanda de preguntas con el estilo del examen, apoyada solo en el material de la
        clase. Rota la cadena de análisis, cuenta como "analysis" y fija `last_analysis_model`
        (la pasada a ciegas no lo usa)."""
        ...

    def answer_blind_batch(self, items: list[tuple[str, list[str]]],
                           exclude_models: Collection[str] = ()) -> list[int | None]:
        """Responde a ciegas (sin respuesta correcta ni explicación) todas las preguntas
        en un solo pedido. Un índice por pregunta, en el mismo orden; None si no sabe.
        `exclude_models`: modelos que no se pueden usar (el que hizo el análisis)."""
        ...

    def check_key(self) -> None:
        """Confirma que la clave autentica sin gastar cuota de generación. Lanza los errores de arriba."""
        ...
