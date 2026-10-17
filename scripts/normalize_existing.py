"""Pasa a notación de clase las preguntas, resúmenes y temas ya guardados.

Uso (con la app cerrada):
    uv run python scripts/normalize_existing.py

Usa la base real. Es idempotente: se puede volver a correr. Una pregunta cuyas
opciones quedarían iguales no se toca (se avisa solo el id). No gasta pedidos.
"""
from __future__ import annotations

import logging
import sys

from estudia.db import connect
from estudia.generation.renormalize import normalize_existing


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    conn = connect()
    try:
        result = normalize_existing(conn)
    finally:
        conn.close()
    print(f"Preguntas actualizadas: {result.questions}")
    print(f"Preguntas sin tocar: {len(result.skipped)}")
    print(f"Resúmenes actualizados: {result.summaries}")
    print(f"Temas renombrados: {result.topics}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
