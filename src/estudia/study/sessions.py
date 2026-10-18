"""Sesiones del plan: tipos, temas y reparto de minutos. Lo comparten el plan y la práctica.

- `focus`: práctica en bloque del tema foco.
- `mixed_focus`: unos minutos de repaso de vencidas de todos los temas del examen, mezcladas, y el
  resto del tema foco (10 + 40 en un bloque de 50; 7 + 18 en uno de 25).
- `mixed_only`: toda la sesión es repaso mezclado de los temas del examen.
"""
import json
import sqlite3

SHORT_BLOCK = 25           # si el día tiene 50 minutos o menos
LONG_BLOCK = 50

FOCUS, MIXED_FOCUS, MIXED_ONLY = "focus", "mixed_focus", "mixed_only"
REVIEW_MIN = {LONG_BLOCK: 10, SHORT_BLOCK: 7}   # minutos de repaso mezclado al empezar la sesión


def session_topics(row: sqlite3.Row) -> list[int]:
    """Temas de una fila de `plan_session`, el foco primero (sin `topics_json`, solo el foco)."""
    return json.loads(row["topics_json"]) if row["topics_json"] else [row["topic_id"]]


def split_minutes(kind: str, minutes: int) -> tuple[int, int]:
    """(minutos de repaso mezclado, minutos de foco) de una sesión. Solo hay bloques de 50 y de 25."""
    if kind == MIXED_ONLY:
        return minutes, 0
    if kind == MIXED_FOCUS:
        if minutes not in REVIEW_MIN:
            raise ValueError(f"Una sesión mezclada dura {LONG_BLOCK} o {SHORT_BLOCK} minutos, no {minutes}")
        return REVIEW_MIN[minutes], minutes - REVIEW_MIN[minutes]
    return 0, minutes
