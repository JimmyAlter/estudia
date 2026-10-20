"""Estilo del examen: cuántos ejercicios y de qué tipo, cuánto vale cada uno, el tiempo, la nota
máxima, los temas y si hay hoja de fórmulas.

Sale de los parciales viejos que cargó el estudiante (uno por parcial, ya extraído por Gemini),
se promedia entre todos y el estudiante lo puede corregir a mano. De un parcial se guardan solo
estos números y los temas: nunca un enunciado.
"""

from __future__ import annotations

import math
import sqlite3
from collections import Counter
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints, ValidationError, model_validator

from estudia.ai.base import ExerciseKind, ExtractedStyle
from estudia.generation.pipeline import slugify
from estudia.study.exams import ENROLLMENT, exam_topics
from estudia.study.recall import normalize_words, same_word
from estudia.study.srs import HEAD

EXERCISE_KINDS = ("opcion_multiple", "verdadero_falso", "desarrollo")
STYLE_MAX_QUESTIONS = 30
STYLE_MAX_MINUTES = 600
STYLE_MAX_POINTS = 100
# Temas que no entran en los del curso: texto libre, con tope de cantidad y de largo.
STYLE_MAX_UNMATCHED = 10
STYLE_MAX_TOPIC_CHARS = 80

ENROLLMENT_TEXT = "Una inscripción no tiene estilo de examen."
FOREIGN_TOPIC_TEXT = "Hay temas que no son del curso del examen"
NO_EXERCISES_TEXT = "El examen necesita al menos un ejercicio"

UnmatchedTopic = Annotated[str, StringConstraints(max_length=STYLE_MAX_TOPIC_CHARS)]


class StyleGroup(BaseModel):
    kind: ExerciseKind
    count: int = Field(ge=1, le=STYLE_MAX_QUESTIONS)
    points_each: float = Field(gt=0, le=STYLE_MAX_POINTS)


class ExamStyle(BaseModel):
    """El estilo que se guarda en `exam.style_json` (y en `past_exam.style_json`, uno por parcial).

    Sin ningún grupo no es un estilo: `save_style` lo rechaza con un texto (400) y `saved_style`
    lo lee como None. Todo lo demás se valida acá (422 en la API).
    """

    groups: list[StyleGroup] = Field(max_length=len(EXERCISE_KINDS))
    minutes: int | None = Field(default=None, ge=1, le=STYLE_MAX_MINUTES)
    max_grade: float | None = Field(default=None, gt=0, le=STYLE_MAX_POINTS)
    topic_ids: list[int] = []
    topics_unmatched: list[UnmatchedTopic] = Field(default=[], max_length=STYLE_MAX_UNMATCHED)
    formula_sheet: bool = False

    @model_validator(mode="after")
    def _check_groups(self) -> ExamStyle:
        kinds = [g.kind for g in self.groups]
        if len(set(kinds)) != len(kinds):
            raise ValueError("Hay un tipo de ejercicio repetido")
        if sum(g.count for g in self.groups) > STYLE_MAX_QUESTIONS:
            raise ValueError(f"El examen puede tener hasta {STYLE_MAX_QUESTIONS} ejercicios")
        return self

    @property
    def questions(self) -> int:
        return sum(g.count for g in self.groups)


@dataclass
class StyleSource:
    """Un parcial que entró en el promedio: solo su nombre y sus números."""

    past_exam_id: int
    name: str
    questions: int
    minutes: int | None
    max_grade: float | None
    formula_sheet: bool


# --- Redondeos ----------------------------------------------------------------------


def round_half_up(x: float) -> int:
    """Redondeo escolar: 2,5 da 3 (el `round` de Python da 2)."""
    return int(Decimal(repr(x)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def round_half_point(x: float) -> float:
    """Al 0,5 más cercano, con las mitades hacia arriba (1,25 da 1,5)."""
    return round_half_up(x * 2) / 2


def _points(x: float) -> float:
    """Puntaje de un ejercicio: al 0,5, nunca 0 y con el tope."""
    return min(float(STYLE_MAX_POINTS), max(0.5, round_half_point(x)))


def _largest_remainder(shares: dict[str, float], total: int) -> dict[str, int]:
    """Reparte `total` en enteros proporcionales a `shares` (método del mayor resto).

    Los empates de resto se rompen por el orden de EXERCISE_KINDS.
    """
    weight = sum(shares.values())
    if total <= 0 or weight <= 0:
        return {k: 0 for k in shares}
    exact = {k: v * total / weight for k, v in shares.items()}
    result = {k: math.floor(v) for k, v in exact.items()}
    left = total - sum(result.values())
    order = sorted(shares, key=lambda k: (-(exact[k] - result[k]), EXERCISE_KINDS.index(k)))
    for k in order[:left]:
        result[k] += 1
    return result


def _cap_unmatched(names) -> list[str]:
    """Sin vacíos ni repetidos (sin distinguir mayúsculas), y a lo sumo STYLE_MAX_UNMATCHED."""
    result: list[str] = []
    for name in names:
        if name and name.casefold() not in {r.casefold() for r in result}:
            result.append(name)
        if len(result) == STYLE_MAX_UNMATCHED:
            break
    return result


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


# --- Temas --------------------------------------------------------------------------


def map_topics(conn: sqlite3.Connection, course_id: int, names: list[str]) -> tuple[list[int], list[str]]:
    """Los nombres de temas que leyó Gemini, llevados a los temas del curso.

    Un nombre corresponde a un tema si tienen el mismo slug, o si al menos la mitad (hacia arriba)
    de las palabras del tema aparecen en el nombre (`recall.same_word`: "Funciones lineales"
    entra en "Función lineal"). Si entra en varios, gana el que tiene más palabras cubiertas.
    Los que no entran en ninguno vuelven en `unmatched`. Sin repetidos, en el orden de llegada.
    """
    topics = [
        (r["id"], slugify(r["name"] or ""), normalize_words(r["name"] or ""))
        for r in conn.execute("SELECT id, name FROM topic WHERE course_id = ? ORDER BY id", (course_id,))
    ]
    ids: list[int] = []
    unmatched: list[str] = []
    seen_unmatched: set[str] = set()
    for raw in names:
        name = " ".join(str(raw).split())
        if not name:
            continue
        slug = slugify(name)
        match = next((tid for tid, tslug, _ in topics if slug and tslug == slug), None)
        if match is None:
            said = normalize_words(name)
            best: tuple[float, int, int] | None = None
            for tid, _, words in topics:
                if not words:
                    continue
                hits = sum(1 for w in words if any(same_word(w, s) for s in said))
                if hits >= math.ceil(len(words) / 2):
                    score = (hits / len(words), hits, -tid)
                    if best is None or score > best:
                        best = score
            match = -best[2] if best is not None else None
        if match is not None:
            if match not in ids:
                ids.append(match)
        elif name.casefold() not in seen_unmatched:
            seen_unmatched.add(name.casefold())
            unmatched.append(name)
    return ids, unmatched


# --- De lo extraído al estilo ------------------------------------------------------


def to_exam_style(conn: sqlite3.Connection, course_id: int, extracted: ExtractedStyle) -> ExamStyle:
    """El estilo de un parcial a partir de lo que leyó Gemini.

    Agrupa por tipo (en el orden de EXERCISE_KINDS); `points_each` es el promedio de los
    puntajes de ese tipo al 0,5 y, sin puntajes, `max_grade / total` (o 1). Lo que está fuera
    de rango se acomoda: más de 30 ejercicios se reparten en 30, un tiempo o una nota absurdos
    quedan sin dato. `ValueError` si no hay ningún ejercicio.
    """
    total = len(extracted.exercises)
    if total == 0:
        raise ValueError("el parcial no tiene ejercicios")
    counts = Counter(e.kind for e in extracted.exercises)
    if total > STYLE_MAX_QUESTIONS:
        counts = Counter(_largest_remainder(
            {k: float(counts[k]) for k in EXERCISE_KINDS if counts[k]}, STYLE_MAX_QUESTIONS))
    max_grade = extracted.max_grade
    if max_grade is None or not 0 < max_grade <= STYLE_MAX_POINTS:
        max_grade = None
    minutes = extracted.minutes
    if minutes is None or not 1 <= minutes <= STYLE_MAX_MINUTES:
        minutes = None
    groups = []
    for kind in EXERCISE_KINDS:
        if counts[kind] <= 0:
            continue
        given = [e.points for e in extracted.exercises
                 if e.kind == kind and e.points is not None and 0 < e.points <= STYLE_MAX_POINTS]
        if given:
            points = _points(_mean(given))
        elif max_grade is not None:
            points = _points(max_grade / total)
        else:
            points = 1.0
        groups.append(StyleGroup(kind=kind, count=counts[kind], points_each=points))
    topic_ids, unmatched = map_topics(conn, course_id, extracted.topics)
    unmatched = _cap_unmatched(name[:STYLE_MAX_TOPIC_CHARS].strip() for name in unmatched)
    return ExamStyle(groups=groups, minutes=minutes,
                     max_grade=round_half_point(max_grade) if max_grade is not None else None,
                     topic_ids=topic_ids, topics_unmatched=unmatched,
                     formula_sheet=extracted.formula_sheet)


def average_style(styles: list[ExamStyle]) -> ExamStyle:
    """El promedio de los estilos de varios parciales.

    - el total de ejercicios es el promedio de los totales, redondeado hacia arriba en las mitades;
    - cada tipo lleva el promedio de su cantidad, ajustado a ese total por mayor resto;
    - el puntaje de cada tipo es el promedio de los parciales que lo tienen, al 0,5;
    - el tiempo es el promedio entero de los que lo tienen, y la nota máxima, al 0,5;
    - la hoja de fórmulas va si la tiene la mitad o más;
    - los temas son la unión, los más repetidos primero y después por primera aparición (los
      que no son del curso, a lo sumo STYLE_MAX_UNMATCHED).

    Con un solo estilo devuelve ese mismo.
    """
    if not styles:
        raise ValueError("no hay estilos para promediar")
    if len(styles) == 1:
        return styles[0]
    n = len(styles)
    total = min(STYLE_MAX_QUESTIONS, round_half_up(_mean([float(s.questions) for s in styles])))
    by_kind = [{g.kind: g for g in s.groups} for s in styles]
    shares = {k: sum(kinds[k].count for kinds in by_kind if k in kinds) / n
              for k in EXERCISE_KINDS if any(k in kinds for kinds in by_kind)}
    counts = _largest_remainder(shares, max(1, total))
    groups = [
        StyleGroup(kind=k, count=counts[k],
                   points_each=_points(_mean([kinds[k].points_each for kinds in by_kind if k in kinds])))
        for k in EXERCISE_KINDS if counts.get(k, 0) > 0
    ]
    minutes = [s.minutes for s in styles if s.minutes is not None]
    grades = [s.max_grade for s in styles if s.max_grade is not None]

    frequency: Counter[int] = Counter()
    first_seen: dict[int, int] = {}
    for s in styles:
        for tid in dict.fromkeys(s.topic_ids):
            frequency[tid] += 1
            first_seen.setdefault(tid, len(first_seen))
    unmatched = _cap_unmatched(name for s in styles for name in s.topics_unmatched)
    topic_ids = sorted(frequency, key=lambda t: (-frequency[t], first_seen[t]))

    return ExamStyle(
        groups=groups,
        minutes=round_half_up(_mean([float(m) for m in minutes])) if minutes else None,
        max_grade=round_half_point(_mean(grades)) if grades else None,
        topic_ids=topic_ids,
        topics_unmatched=unmatched,
        formula_sheet=sum(1 for s in styles if s.formula_sheet) * 2 >= n,
    )


# --- Lo que se guarda ---------------------------------------------------------------


def _parse(raw: str | None) -> ExamStyle | None:
    if raw is None:
        return None
    try:
        style = ExamStyle.model_validate_json(raw)
    except (ValidationError, ValueError):
        return None
    return style if style.groups else None


def _exam_row(conn: sqlite3.Connection, exam_id: int):
    row = conn.execute("SELECT id, course_id, kind, style_json FROM exam WHERE id = ?", (exam_id,)).fetchone()
    if row is None:
        raise LookupError(f"examen {exam_id} no existe")
    return row


def _course_topic_ids(conn: sqlite3.Connection, course_id: int) -> set[int]:
    return {r["id"] for r in conn.execute("SELECT id FROM topic WHERE course_id = ?", (course_id,))}


def proposed_style(conn: sqlite3.Connection, exam_id: int) -> tuple[ExamStyle | None, list[StyleSource]]:
    """El promedio de los parciales del examen que ya se leyeron (`done`), con de dónde salió.

    Un tema que ya no es del curso se descarta. `LookupError` si el examen no existe.
    """
    exam = _exam_row(conn, exam_id)
    own = _course_topic_ids(conn, exam["course_id"])
    styles: list[ExamStyle] = []
    sources: list[StyleSource] = []
    for r in conn.execute("SELECT id, name, style_json FROM past_exam"
                          " WHERE exam_id = ? AND status = 'done' ORDER BY id", (exam_id,)):
        style = _parse(r["style_json"])
        if style is None:
            continue
        style = style.model_copy(update={"topic_ids": [t for t in style.topic_ids if t in own]})
        styles.append(style)
        sources.append(StyleSource(past_exam_id=r["id"], name=r["name"] or "Parcial",
                                   questions=style.questions, minutes=style.minutes,
                                   max_grade=style.max_grade, formula_sheet=style.formula_sheet))
    if not styles:
        return None, []
    return average_style(styles), sources


def saved_style(conn: sqlite3.Connection, exam_id: int) -> ExamStyle | None:
    """El estilo que guardó el estudiante; None si no hay o si el guardado está roto."""
    row = conn.execute("SELECT style_json FROM exam WHERE id = ?", (exam_id,)).fetchone()
    return _parse(row["style_json"]) if row is not None else None


def save_style(conn: sqlite3.Connection, exam_id: int, style: ExamStyle) -> None:
    """Guarda el estilo del examen. `LookupError` si no existe; `ValueError` si es una
    inscripción o si algún tema no es del curso del examen. Hace commit."""
    exam = _exam_row(conn, exam_id)
    if exam["kind"] == ENROLLMENT:
        raise ValueError(ENROLLMENT_TEXT)
    if not style.groups:
        raise ValueError(NO_EXERCISES_TEXT)
    topic_ids = list(dict.fromkeys(style.topic_ids))
    if not set(topic_ids) <= _course_topic_ids(conn, exam["course_id"]):
        raise ValueError(FOREIGN_TOPIC_TEXT)
    style = style.model_copy(update={"topic_ids": topic_ids})
    conn.execute("UPDATE exam SET style_json = ? WHERE id = ?", (style.model_dump_json(), exam_id))
    conn.commit()


def clear_style(conn: sqlite3.Connection, exam_id: int) -> None:
    """Borra el estilo guardado (vuelve a valer el propuesto) y retira las preguntas generadas con
    él. Sube `style_revision`: lo que se genere después tiene otra clave y no choca con las
    retiradas. `LookupError` si el examen no existe."""
    _exam_row(conn, exam_id)
    conn.execute("UPDATE exam SET style_json = NULL, style_revision = COALESCE(style_revision, 0) + 1"
                 " WHERE id = ?", (exam_id,))
    retire_style_questions(conn, exam_id)
    conn.commit()


def style_revision(conn: sqlite3.Connection, exam_id: int) -> int:
    """La revisión del estilo del examen (0 si nunca se borró o si el examen no existe)."""
    row = conn.execute("SELECT COALESCE(style_revision, 0) FROM exam WHERE id = ?", (exam_id,)).fetchone()
    return row[0] if row is not None else 0


def retire_style_questions(conn: sqlite3.Connection, exam_id: int) -> None:
    """Retira las preguntas generadas con el estilo de ese examen. No hace commit."""
    conn.execute("UPDATE question SET retired = 1 WHERE style_exam_id = ?", (exam_id,))


# --- Lo que falta ---------------------------------------------------------------------


def style_topic_ids(conn: sqlite3.Connection, exam_id: int, style: ExamStyle) -> list[int]:
    """`T`: los temas del estilo o, si no eligió ninguno, los del examen."""
    return list(style.topic_ids) or exam_topics(conn, exam_id)


def own_topic_filter(topics: list[int]) -> tuple[str, list[int]]:
    """El filtro SQL (sobre `q.topic_id`) de las preguntas de estilo que valen para los temas `T`:
    si el estudiante sacó un tema del estilo, las de ese tema no cuentan ni salen en el simulacro
    (no se retiran: si vuelve a poner el tema, vuelven). Sin temas, no filtra."""
    if not topics:
        return "", []
    return f" AND q.topic_id IN ({','.join('?' * len(topics))})", list(topics)


def style_gaps(conn: sqlite3.Connection, exam_id: int) -> dict[str, int]:
    """Cuántas preguntas faltan de cada tipo del estilo guardado para armar el simulacro.

    Solo cuentan las verificadas y no retiradas. Para opción múltiple sirven las del campus de los
    temas `T` (`style_topic_ids`) y las de estilo de este examen de ese tipo; verdadero/falso y
    desarrollo solo salen de las de estilo de este examen. Una de estilo cuenta solo si su tema
    está en `T` (`own_topic_filter`). Sin estilo guardado devuelve `{}`.
    """
    style = saved_style(conn, exam_id)
    if style is None:
        return {}
    topics = style_topic_ids(conn, exam_id, style)
    in_topics, params = own_topic_filter(topics)
    own = dict(conn.execute(
        "SELECT style_kind, COUNT(*) FROM question q WHERE style_exam_id = ? AND origin IS NOT NULL"
        f" AND verified = 1 AND retired = 0{in_topics} GROUP BY style_kind",
        (exam_id, *params)).fetchall())
    campus = 0
    if topics:
        campus = conn.execute(
            f"SELECT COUNT(*) FROM question q WHERE q.topic_id IN ({','.join('?' * len(topics))})"
            f" AND q.origin IS NULL AND q.verified = 1 AND q.retired = 0 AND {HEAD}", topics).fetchone()[0]
    gaps = {}
    for group in style.groups:
        available = own.get(group.kind, 0) + (campus if group.kind == "opcion_multiple" else 0)
        gaps[group.kind] = max(0, group.count - available)
    return gaps

