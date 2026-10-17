"""Rotación entre modelos de Gemini: clasifica errores y lleva las marcas de cuota.

El plan gratuito da 20 pedidos por día a cada modelo. Cuando uno se agota se marca
hasta que vuelve y se prueba el siguiente de la cadena.
"""
import datetime as dt
import logging
import re
from collections.abc import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from google.genai import errors

from estudia.ai.base import AIFormatError, AIQuotaError, AIUnavailableError
from estudia.config import MAX_EXHAUSTED_S

# Por qué se deja un modelo y se prueba el siguiente de la cadena.
DAILY, QUOTA, TRANSIENT, UNSUPPORTED, UNREADABLE = (
    "daily", "quota", "transient", "unsupported", "unreadable")


log = logging.getLogger(__name__)


class NextModel(Exception):
    """Este modelo no sirve para este pedido: se prueba el siguiente de la cadena."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# --- Clasificación de errores de la API -------------------------------------------

def error_body(e: errors.APIError) -> dict:
    details = e.details if isinstance(e.details, dict) else {}
    inner = details.get("error")
    return inner if isinstance(inner, dict) else details


# Clave inválida, vencida o sin permiso para la API de Gemini.
BAD_KEY_REASONS = {"API_KEY_INVALID", "API_KEY_EXPIRED", "API_KEY_SERVICE_BLOCKED"}


def is_invalid_key(e: errors.APIError) -> bool:
    """Gemini responde a una clave mala con 400 INVALID_ARGUMENT y un reason de BAD_KEY_REASONS."""
    for item in error_body(e).get("details") or []:
        if isinstance(item, dict) and item.get("reason") in BAD_KEY_REASONS:
            return True
    message = (e.message or "").lower()
    return "api key" in message and any(w in message for w in ("not valid", "invalid", "expired"))


def retry_delay(e: errors.APIError) -> float | None:
    """Segundos de RetryInfo.retryDelay (p. ej. "37s"), si el error los trae."""
    for item in error_body(e).get("details") or []:
        if not isinstance(item, dict) or not str(item.get("@type", "")).endswith("RetryInfo"):
            continue
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)s\s*", str(item.get("retryDelay", "")))
        if match:
            return float(match.group(1))
    return None


def is_quota(e: errors.APIError) -> bool:
    return e.code == 429 or getattr(e, "status", None) == "RESOURCE_EXHAUSTED"


def is_daily_quota(e: errors.APIError) -> bool:
    """429 del tope diario (quotaId GenerateRequestsPerDayPerProjectPerModel-FreeTier)."""
    for item in error_body(e).get("details") or []:
        if not isinstance(item, dict) or not str(item.get("@type", "")).endswith("QuotaFailure"):
            continue
        for violation in item.get("violations") or []:
            if isinstance(violation, dict) and "PerDay" in str(violation.get("quotaId", "")):
                return True
    return False


def is_transient(e: errors.APIError) -> bool:
    """Modelo sobrecargado (503) o sin respuesta a tiempo (504)."""
    return e.code in (503, 504) or getattr(e, "status", None) in ("UNAVAILABLE", "DEADLINE_EXCEEDED")


def is_permission_denied(e: errors.APIError) -> bool:
    return e.code == 403 or getattr(e, "status", None) == "PERMISSION_DENIED"


def mentions_url(e: errors.APIError) -> bool:
    message = (e.message or "").lower()
    return any(w in message for w in ("youtube", "url", "file_uri", "fileuri"))


def is_unsupported(e: errors.APIError, *, youtube_url: bool) -> bool:
    """400 porque el modelo no admite algo del pedido (video, modo JSON, instrucciones).

    `youtube_url`: el pedido manda un enlace de YouTube; si el error habla del enlace,
    es el enlace lo que no se acepta (se prueba la subida), no el modelo.
    """
    if e.code != 400 or is_invalid_key(e):
        return False
    if youtube_url and mentions_url(e):
        return False
    message = (e.message or "").lower()
    return any(w in message for w in ("not supported", "unsupported", "not enabled"))


def seconds_to_pacific_midnight(now: float) -> float:
    """La cuota diaria vuelve a la medianoche del Pacífico; sin la zona, en 24 h."""
    try:
        zone = ZoneInfo("America/Los_Angeles")
    except ZoneInfoNotFoundError:
        return 86400.0
    local = dt.datetime.fromtimestamp(now, zone)
    midnight = (local + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(midnight.timestamp() - now, 60.0)


def clamp_exhausted(until: float, now: float) -> float:
    return min(until, now + MAX_EXHAUSTED_S)


# --- Rotación ---------------------------------------------------------------------

class ModelRotation:
    """Marcas de modelos agotados por el día y el recorrido de una cadena de modelos."""

    def __init__(self, clock: Callable[[], float]):
        self._clock = clock
        # Modelo -> epoch en que vuelve a tener cuota.
        self.exhausted_until: dict[str, float] = {}

    def is_exhausted(self, model: str) -> bool:
        return self.exhausted_until.get(model, 0.0) > self._clock()

    def mark_exhausted(self, model: str, delay: float | None) -> None:
        now = self._clock()
        wait = delay if delay is not None else seconds_to_pacific_midnight(now)
        self.exhausted_until[model] = clamp_exhausted(now + wait, now)
        log.info("Gemini: %s agotó la cuota del día; se pasa al siguiente modelo", model)

    def run(self, candidates: list[str], attempt: Callable[[str], object]):
        """Prueba `attempt(modelo)` con cada candidato hasta que uno responda.

        `attempt` lanza NextModel para seguir con el próximo; cualquier otro error
        (clave mala, video privado, red caída) corta en seguida. Si ninguno responde:
        - AIUnavailableError si alguno estaba saturado (vuelve en minutos);
        - AIQuotaError si alguno se quedó sin cuota, o si no hay ningún candidato:
          el material queda pendiente y conserva el análisis ya pagado;
        - AIFormatError si todos respondieron algo inservible.
        """
        reasons = set()
        for model in candidates:
            if self.is_exhausted(model):
                reasons.add(DAILY)
                continue
            try:
                return attempt(model)
            except NextModel as e:
                reasons.add(e.reason)
        if TRANSIENT in reasons:
            raise AIUnavailableError("los modelos de Gemini no responden ahora")
        if not reasons or reasons & {DAILY, QUOTA}:
            resets = [self.exhausted_until[m] for m in candidates if self.is_exhausted(m)]
            raise AIQuotaError("cuota de Gemini agotada", resets_at=min(resets) if resets else None)
        raise AIFormatError("ningún modelo de Gemini pudo responder este pedido")
