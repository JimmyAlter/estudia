"""Pasa a notación de clase lo que ya está guardado (preguntas, resúmenes y temas).

Idempotente: correrlo dos veces no cambia nada la segunda. `check_expr` no se toca.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field

from estudia.generation.notation import sentence_case, student_math
from estudia.generation.pipeline import dedupe_key, student_question
from estudia.generation.style_questions import style_dedupe_key
from estudia.study.exam_style import style_revision

log = logging.getLogger(__name__)


@dataclass
class NormalizeResult:
    questions: int = 0  # preguntas cambiadas
    skipped: list[int] = field(default_factory=list)  # ids que no se tocaron (opciones o enunciado repetidos)
    summaries: int = 0
    topics: int = 0


def normalize_existing(conn: sqlite3.Connection) -> NormalizeResult:
    result = NormalizeResult()
    rows = conn.execute(
        "SELECT id, material_id, style_exam_id, retired, stem, options_json, explanation,"
        " common_error, dedupe_key FROM question ORDER BY id"
    ).fetchall()
    for row in rows:
        text = student_question(row["stem"] or "", json.loads(row["options_json"]),
                                row["explanation"] or "", row["common_error"] or "")
        if text is None:
            # Solo el id: el texto de las preguntas no va a los logs.
            log.warning("Pregunta %s sin cambios: dos opciones quedarían iguales", row["id"])
            result.skipped.append(row["id"])
            continue
        options_json = json.dumps(text.options, ensure_ascii=False)
        if row["retired"]:
            # Una retirada conserva su clave: la de estilo, porque su revisión ya pasó y no se
            # puede saber cuál era; la de un archivo que el campus reemplazó queda en NULL. Así
            # no choca con la que se generó después.
            key = row["dedupe_key"]
        elif row["style_exam_id"] is None:
            key = dedupe_key(row["material_id"], text.stem)
        else:
            # Una de estilo no tiene material: su clave es la del examen y la revisión vigente.
            key = style_dedupe_key(row["style_exam_id"], text.stem,
                                   revision=style_revision(conn, row["style_exam_id"]))
        new = (text.stem, options_json, text.explanation, text.common_error, key)
        old = (row["stem"], row["options_json"], row["explanation"], row["common_error"],
               row["dedupe_key"])
        if new == old:
            continue
        try:
            # Con otras opciones el chequeo de escritas (`typed_safe`) se vuelve a hacer.
            conn.execute(
                "UPDATE question SET stem=?, options_json=?, explanation=?, common_error=?,"
                " dedupe_key=?, typed_safe=CASE WHEN ? THEN typed_safe END"
                " WHERE id=?", (*new, options_json == row["options_json"], row["id"]),
            )
        except sqlite3.IntegrityError:
            log.warning("Pregunta %s sin cambios: el enunciado quedaría repetido", row["id"])
            result.skipped.append(row["id"])
            continue
        result.questions += 1

    for row in conn.execute(
        "SELECT id, summary_md FROM material WHERE summary_md IS NOT NULL"
    ).fetchall():
        summary = student_math(row["summary_md"])
        if summary != row["summary_md"]:
            conn.execute("UPDATE material SET summary_md=? WHERE id=?", (summary, row["id"]))
            result.summaries += 1

    for row in conn.execute("SELECT id, name FROM topic").fetchall():
        name = sentence_case(row["name"] or "")
        if name and name != row["name"]:
            conn.execute("UPDATE topic SET name=? WHERE id=?", (name, row["id"]))
            result.topics += 1
    conn.commit()
    return result
