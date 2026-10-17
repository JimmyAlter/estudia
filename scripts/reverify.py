"""Vuelve a verificar a ciegas todas las preguntas guardadas y retira las que no pasan.

Uso (con la app cerrada):
    uv run python scripts/reverify.py

Usa la base real y la clave de Gemini del keyring (gemini_key); gasta un pedido por
material. Muestra solo cantidades: nunca la clave ni el texto de las preguntas. Si
se corta por cuota, se puede volver a correr: sigue con los materiales que faltan.
"""
from __future__ import annotations

import sys

from estudia.ai.gemini import GeminiProvider
from estudia.db import connect
from estudia.generation.reverify import reverify_all
from estudia.secrets import get_secret


def main() -> int:
    key = get_secret("gemini_key")
    if not key:
        print("no hay clave de Gemini en el keyring (gemini_key)", file=sys.stderr)
        return 2
    conn = connect()
    try:
        result = reverify_all(conn, GeminiProvider(key))
    finally:
        conn.close()
    print(f"Materiales verificados: {result.materials_checked}")
    print(f"Preguntas que siguen: {result.kept}")
    print(f"Preguntas retiradas: {result.retired}")
    print(f"Materiales sin verificar todavía: {result.materials_left}")
    if result.stop_reason:
        print(f"Se cortó por: {result.stop_reason} (volver a correr más tarde)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
