"""Prueba real de punta a punta: análisis + pasada a ciegas en lote para un video,
con la clave guardada en el keyring.

Uso:
    uv run python scripts/smoke_video.py <youtube_id>            # enlace primero, subida si hace falta
    uv run python scripts/smoke_video.py <youtube_id> --upload   # directo por la subida

Gasta cuota de Gemini: no corre en los tests. Usa una base y una carpeta temporales
(no toca los datos reales) y nunca muestra la clave. Al final dice qué modelos se
usaron, cuántas preguntas quedaron y cuántos pedidos de generación se hicieron.
"""
from __future__ import annotations

import argparse
import logging
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

from google import genai
from google.genai import types

from estudia.ai.gemini import GeminiProvider
from estudia.config import GEMINI_TIMEOUT_S
from estudia.db import connect
from estudia.generation.pipeline import process_material
from estudia.secrets import get_secret


class CountingModels:
    """Envuelve client.models para contar los pedidos de generación por modelo."""

    def __init__(self, models):
        self._models = models
        self.calls: Counter[str] = Counter()

    def generate_content(self, model, contents, config):
        self.calls[model] += 1
        return self._models.generate_content(model=model, contents=contents, config=config)

    def list(self):
        return self._models.list()


class CountingClient:
    def __init__(self, client):
        self._client = client  # si se pierde la referencia, el cliente se cierra solo
        self.models = CountingModels(client.models)
        self.files = client.files


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("youtube_id")
    parser.add_argument("--upload", action="store_true", help="saltear el enlace de YouTube")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # httpx registra cada URL pedida; no hace falta (y algunas podrían llevar datos).
    logging.getLogger("httpx").setLevel(logging.WARNING)

    key = get_secret("gemini_key")
    if not key:
        print("no hay clave de Gemini en el keyring (gemini_key)", file=sys.stderr)
        return 2
    client = CountingClient(genai.Client(
        api_key=key, http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_S * 1000)))
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        provider = GeminiProvider(key, client=client, video_dir=tmp_path / "videos",
                                  youtube_url_denied=args.upload)
        conn = connect(tmp_path / "smoke.db")
        conn.execute("INSERT INTO section(id, course_id, num, name, visible) VALUES (1, 1, 1, 'S', 1)")
        conn.execute("INSERT INTO material(id, section_id, cmid, kind, title, ref, status)"
                     " VALUES (1, 1, 1, 'youtube', ?, ?, 'pending')", (args.youtube_id, args.youtube_id))
        conn.commit()
        start = time.monotonic()
        try:
            result = process_material(conn, provider, 1)
        except Exception as exc:  # noqa: BLE001 - solo el tipo y el mensaje propio
            print(f"falló: {type(exc).__name__}: {exc}", file=sys.stderr)
            print(f"Pedidos de generación: {dict(client.models.calls)}", file=sys.stderr)
            print(f"Modelos agotados por hoy: {sorted(provider.exhausted_until)}", file=sys.stderr)
            return 1
        elapsed = time.monotonic() - start
        row = conn.execute("SELECT status, summary_md FROM material WHERE id=1").fetchone()
        stems = [r[0] for r in conn.execute("SELECT stem FROM question ORDER BY id")]
        conn.close()
        left = list((tmp_path / "videos").iterdir()) if (tmp_path / "videos").exists() else []
    route = "subida" if provider.youtube_url_denied else "enlace de YouTube"
    print(row["summary_md"] or "")
    print(f"\nEstado: {result.status} | Ruta: {route} | {elapsed:.0f} s")
    print(f"Modelo del análisis: {provider.last_analysis_model}")
    print(f"Modelo de la pasada a ciegas: {provider.last_blind_model}")
    print(f"Preguntas guardadas: {result.kept} | descartadas: {result.discarded}")
    for stem in stems:
        print(f"  - {stem}")
    print(f"Pedidos de generación: {sum(client.models.calls.values())} {dict(client.models.calls)}")
    print(f"Modelos agotados por hoy: {sorted(provider.exhausted_until)}")
    print(f"Archivos locales que quedaron: {len(left)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
