"""Exportación a Anki: un mazo por curso con las preguntas verificadas y no retiradas."""
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import unicodedata
from html import escape

import genanki

# NUNCA CAMBIAR: Anki identifica el tipo de nota por este número. Si cambia, al
# reimportar se crea un tipo nuevo y las tarjetas ya estudiadas se duplican.
MODEL_ID = 1_760_700_001

_YOUTUBE_ID = re.compile(r"[A-Za-z0-9_-]{11}")

_CSS = """\
.card { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; font-size: 18px;
  line-height: 1.5; text-align: left; max-width: 40em; margin: 0 auto; }
.meta { font-size: 14px; opacity: 0.7; }
ol.options { padding-left: 1.5em; }
"""

_FRONT = """\
<p class="meta">{{Materia}} / {{Tema}}</p>
<p>{{Pregunta}}</p>
{{Opciones}}
"""

_BACK = """\
{{FrontSide}}
<hr id="answer">
<p>Respuesta correcta: {{Respuesta}}</p>
<p>{{Explicacion}}</p>
{{#ErrorComun}}<p>Error común: {{ErrorComun}}</p>{{/ErrorComun}}
<p class="meta">{{Fuente}}</p>
"""

MODEL = genanki.Model(
    MODEL_ID,
    "Estudia (opción múltiple)",
    fields=[{"name": n} for n in (
        "Pregunta", "Opciones", "Respuesta", "Explicacion", "ErrorComun", "Fuente", "Tema", "Materia",
    )],
    templates=[{"name": "Tarjeta", "qfmt": _FRONT, "afmt": _BACK}],
    css=_CSS,
)


def deck_id(course_id: int) -> int:
    """Número estable por curso, dentro del rango que Anki acepta."""
    digest = hashlib.sha256(f"estudia-course-{course_id}".encode()).digest()
    return (1 << 30) + int.from_bytes(digest[:4], "big") % (1 << 30)


def course_slug(text: str) -> str:
    """Nombre de archivo seguro: solo ASCII, minúsculas y guiones."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-") or "mazo"


def _source(title: str, kind: str, ref: str | None, seconds: int | None) -> str:
    text = f"Clase: {escape(title or '')}"
    if seconds is None:
        return text
    text += f", minuto {seconds // 60:02d}:{seconds % 60:02d}"
    if kind == "youtube" and ref and _YOUTUBE_ID.fullmatch(ref):
        text += f' (<a href="https://youtu.be/{ref}?t={seconds}">ver en YouTube</a>)'
    return text


def _note(r: sqlite3.Row) -> genanki.Note:
    options = json.loads(r["options_json"])
    items = "".join(f"<li>{escape(str(o))}</li>" for o in options)
    idx = r["correct_index"]
    letter = chr(65 + idx)
    answer = f"<b>{letter}. {escape(str(options[idx]))}</b>"
    return genanki.Note(
        model=MODEL,
        guid=genanki.guid_for("estudia", r["id"]),
        fields=[
            escape(r["stem"] or ""),
            f'<ol class="options" type="A">{items}</ol>',
            answer,
            escape(r["explanation"] or ""),
            escape(r["common_error"] or ""),
            _source(r["title"], r["kind"], r["ref"], r["source_seconds"]),
            escape(r["topic"] or ""),
            escape(r["course"] or ""),
        ],
    )


def build_apkg(conn: sqlite3.Connection, course_id: int | None) -> bytes:
    """Archivo .apkg con un mazo por curso (o solo el pedido).

    Levanta LookupError si no hay ninguna pregunta verificada para exportar.
    """
    sql = (
        "SELECT q.id, q.stem, q.options_json, q.correct_index, q.explanation, q.common_error,"
        " q.source_seconds, t.course_id, t.name AS topic, m.title, m.kind, m.ref,"
        " (SELECT MIN(fullname) FROM course WHERE id = t.course_id) AS course"
        " FROM question q JOIN topic t ON t.id = q.topic_id JOIN material m ON m.id = q.material_id"
        " WHERE q.verified = 1 AND q.retired = 0"
    )
    params: list = []
    if course_id is not None:
        sql += " AND t.course_id = ?"
        params.append(course_id)
    sql += " ORDER BY t.course_id, q.id"

    decks: dict[int, genanki.Deck] = {}
    for r in conn.execute(sql, params):
        cid = r["course_id"]
        if cid not in decks:
            # "::" separa submazos en Anki: un curso con "::" en el nombre quedaría partido.
            name = re.sub(r":{2,}", ":", r["course"] or f"Curso {cid}")
            decks[cid] = genanki.Deck(deck_id(cid), name)
        decks[cid].add_note(_note(r))
    if not decks:
        raise LookupError("No hay preguntas para exportar")

    fd, path = tempfile.mkstemp(suffix=".apkg")
    os.close(fd)
    try:
        genanki.Package(list(decks.values())).write_to_file(path)
        with open(path, "rb") as f:
            return f.read()
    finally:
        os.unlink(path)
