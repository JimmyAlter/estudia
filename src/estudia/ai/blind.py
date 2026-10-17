"""Pasada a ciegas en lote: armado del pedido y lectura estricta de la respuesta.

El modelo ve solo enunciados y opciones (nunca la respuesta ni la explicación) y
devuelve {"answers": [{"q": 1, "answer": 2}, {"q": 2, "answer": null}, ...]}.
Cada respuesta lleva el número de su pregunta: si vienen desordenadas, repetidas,
de menos o de más, no se puede saber cuál es cuál y se descarta el lote entero.
"""
import json
import re

from google.genai import types
from pydantic import BaseModel

from estudia.generation.prompts import BLIND_BATCH_PROMPT

BlindItems = list[tuple[str, list[str]]]


class BlindAnswer(BaseModel):
    q: int
    answer: int | None


class BlindAnswers(BaseModel):
    answers: list[BlindAnswer]


def is_gemma(model: str) -> bool:
    return model.startswith("gemma")


def blind_config(model: str) -> types.GenerateContentConfig:
    if is_gemma(model):
        # Gemma no tiene modo JSON con esquema: se le pide JSON en el texto y se lee tolerante.
        return types.GenerateContentConfig(temperature=0)
    return types.GenerateContentConfig(
        temperature=0, response_mime_type="application/json", response_schema=BlindAnswers,
    )


def blind_prompt(items: BlindItems) -> str:
    blocks = []
    for n, (stem, options) in enumerate(items, start=1):
        listed = "\n".join(f"{i}. {o}" for i, o in enumerate(options))
        blocks.append(f"Pregunta {n}: {stem}\nOpciones:\n{listed}")
    return BLIND_BATCH_PROMPT.format(n=len(items), questions="\n\n".join(blocks))


def loose_json(text: str):
    """JSON de una respuesta que puede traer texto alrededor o un bloque ```json```."""
    text = text.strip()
    candidates = [text]
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        candidates.append(fenced.group(1).strip())
    for open_, close in (("{", "}"), ("[", "]")):
        start, end = text.find(open_), text.rfind(close)
        if 0 <= start < end:
            candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    return None


def _strict_int(value) -> int | None:
    """Un entero de verdad (o un dígito como texto); nunca un bool ni un decimal."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"\s*\d+\s*", value):
        return int(value)
    return None


def parse_blind(text: str, items: BlindItems) -> list[int | None] | None:
    """Un índice por pregunta, en orden (None si no sabe o el índice no vale).

    None si el lote no se puede leer con seguridad: no es JSON, o los "q" no son
    exactamente 1..n en orden (faltan, sobran, se repiten o vienen desordenados).
    """
    data = loose_json(text)
    if isinstance(data, dict):
        data = data.get("answers")
    if not isinstance(data, list) or len(data) != len(items):
        return None
    answers: list[int | None] = []
    for n, (entry, (_, options)) in enumerate(zip(data, items), start=1):
        if not isinstance(entry, dict) or _strict_int(entry.get("q")) != n:
            return None
        index = _strict_int(entry.get("answer"))
        answers.append(index if index is not None and 0 <= index < len(options) else None)
    return answers
