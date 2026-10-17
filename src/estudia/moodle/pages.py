"""Extracción de IDs de YouTube del HTML de una page de Moodle."""

import html
import re

_YOUTUBE = re.compile(
    r"(?:youtube(?:-nocookie)?\.com/(?:watch\?(?:[^\"'<>\s]*?&)?v=|embed/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})"
)


def youtube_ids(html_text: str) -> list[str]:
    """IDs de YouTube en orden de aparición, sin repetidos."""
    texto = html.unescape(html_text)
    vistos: dict[str, None] = {}
    for m in _YOUTUBE.finditer(texto):
        vistos.setdefault(m.group(1))
    return list(vistos)
