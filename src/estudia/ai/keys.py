"""La clave de Gemini: limpieza de lo que se pega y los textos de error.

El formato (`AIza` + 35 caracteres) es solo una pista: Google puede cambiarlo, así que
nunca se rechaza una clave por no coincidir. La que decide es Google (`check_key`).
La clave no se loguea ni se incluye en ningún mensaje.
"""
import re

GEMINI_KEY_PATTERN = re.compile(r"^AIza[0-9A-Za-z_-]{35}$")

KEY_MISSING_TEXT = "Pegá la clave primero."
KEY_FORMAT_TEXT = (
    "Eso no parece una clave de Gemini: empieza con AIza y tiene 39 caracteres. "
    "Revisá que se haya copiado entera."
)
KEY_REJECTED_TEXT = "Google no aceptó esa clave. Creá otra en Google AI Studio y pegala acá."
KEY_UNAVAILABLE_TEXT = "No se pudo verificar la clave ahora. Probá en un rato."

_QUOTES = "\"'`“”„‘’‚«»"


def normalize_key(raw: str) -> str | None:
    """Saca espacios, saltos de línea y comillas (rectas o tipográficas) de los bordes.

    Devuelve la clave limpia, o None si quedó vacía. No valida el formato."""
    key = raw.strip()
    while key and key[0] in _QUOTES and key[-1] in _QUOTES and len(key) >= 2:
        key = key[1:-1].strip()
    # Una comilla suelta de un solo lado tampoco es parte de la clave.
    key = key.strip(_QUOTES + " \t\r\n")
    return key or None


def looks_like_gemini_key(key: str) -> bool:
    return GEMINI_KEY_PATTERN.match(key) is not None
