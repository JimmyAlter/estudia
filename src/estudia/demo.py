"""La materia de ejemplo: preguntas propias, escritas para Estudia, que vienen con el paquete."""

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from importlib.resources import files
from pathlib import Path

from pydantic import BaseModel

from estudia import db
from estudia.generation.pipeline import slugify
from estudia.moodle.names import moodle_token_name
from estudia.secrets import get_secret
from estudia.study.citation import SITE_KEY
from estudia.study.schedule import TZ

DEMO_ASSET = "demo-materia.json"

# La demo vive en su propia base, junto a la real. De la base real solo usa esta marca.
DEMO_KEY = "demo_active"
DEMO_FILE = "demo.db"
DEMO_SITE = "demo"   # course.site_id en la base de ejemplo
DEMO_CONNECTED_TEXT = "Ya conectaste tu campus: la materia de ejemplo ya no hace falta."
# Lo que puede quedar de la base de ejemplo: la base, sus diarios y la que se estaba armando.
_DEMO_SUFFIXES = ("", "-journal", "-wal", "-shm", ".tmp", ".tmp-journal", ".tmp-wal", ".tmp-shm")

# Las rutas de estudio que, con la demo prendida, leen y escriben en demo.db. Ninguna otra
# (exportaciones, .ics, inicio, plan, campus, IA) la ve nunca.
DEMO_ROUTES: frozenset[tuple[str, str]] = frozenset({
    ("POST", "/api/quiz"),
    ("POST", "/api/answer"),
    ("POST", "/api/answers/{answer_id}/explain"),
    ("POST", "/api/question/{question_id}/retire"),
    ("POST", "/api/question/{question_id}/unretire"),
    ("GET", "/api/progress"),
    ("GET", "/api/exams"),
    ("GET", "/api/exams/{exam_id}/topics"),
    ("POST", "/api/mock/start"),
    ("POST", "/api/mock/{attempt_id}/finish"),
    ("GET", "/api/mock/{attempt_id}"),
})

log = logging.getLogger(__name__)


class DemoUnavailable(Exception):
    """El campus ya está conectado (o la base real ya tiene materias): la demo no hace falta."""


class DemoVariant(BaseModel):
    """Otra versión de una pregunta con otros números, escrita y revisada a mano (no se genera)."""
    stem: str
    options: list[str]
    correct_index: int
    explanation: str
    common_error: str
    check_expr: str
    values: dict[str, int]


class DemoQuestion(BaseModel):
    topic: str
    stem: str
    options: list[str]
    correct_index: int
    explanation: str
    common_error: str
    check_expr: str | None
    # Con variantes, la pregunta es la cabeza de su grupo, como las que arma la tubería.
    variant_values: dict[str, int] | None = None
    variants: list[DemoVariant] = []


class DemoSubject(BaseModel):
    version: int
    course_name: str
    summary_md: str
    exam_name: str
    exam_days_ahead: int
    max_grade: float
    topics: list[str]
    questions: list[DemoQuestion]


def load_demo_subject() -> DemoSubject:
    """Lee la materia de ejemplo de los datos del paquete (igual desde el código y congelado)."""
    text = (files("estudia") / "assets" / DEMO_ASSET).read_text(encoding="utf-8")
    return DemoSubject.model_validate_json(text)


def demo_path(db_path: Path) -> Path:
    return db_path.with_name(DEMO_FILE)


def _flag_on(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (DEMO_KEY,)).fetchone()
    return row is not None and row[0] == "1"


def demo_active(conn: sqlite3.Connection, db_path: Path) -> bool:
    """La marca de la base real vale '1' y la base de ejemplo existe."""
    return _flag_on(conn) and demo_path(db_path).exists()


def _exam_at(now: int, days_ahead: int) -> int:
    """18:00 local, `days_ahead` días después de hoy."""
    day = datetime.fromtimestamp(now, TZ).date() + timedelta(days=days_ahead)
    return int(datetime(day.year, day.month, day.day, 18, tzinfo=TZ).timestamp())


def build_demo_db(path: Path, subject: DemoSubject, now: int) -> None:
    """Arma la base de ejemplo en `path` (nueva), con la materia, sus temas, sus preguntas y un parcial."""
    conn = db.connect(path)
    try:
        with conn:
            conn.execute("INSERT INTO course(id, site_id, fullname, shortname) VALUES (1, ?, ?, 'Ejemplo')",
                         (DEMO_SITE, subject.course_name))
            conn.execute("INSERT INTO section(id, course_id, num, name, visible) VALUES (1, 1, 1, ?, 1)",
                         (subject.course_name,))
            # Una fecha vieja: así no se ofrece el pretest de la clase.
            conn.execute(
                "INSERT INTO material(id, section_id, cmid, kind, title, status, summary_md, updated_at)"
                " VALUES (1, 1, NULL, 'demo', ?, 'done', ?, ?)",
                (subject.course_name, subject.summary_md, now - 30 * 86400))
            topic_ids: dict[str, int] = {}
            for topic_id, name in enumerate(subject.topics, start=1):
                conn.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (?, 1, ?, ?)",
                             (topic_id, slugify(name), name))
                conn.execute("INSERT INTO material_topic(material_id, topic_id) VALUES (1, ?)", (topic_id,))
                topic_ids[name] = topic_id
            insert = (
                "INSERT INTO question(material_id, topic_id, stem, options_json, correct_index,"
                " explanation, common_error, check_expr, verified, retired, origin, dedupe_key,"
                " variant_group, variant_values_json)"
                " VALUES (1, ?, ?, ?, ?, ?, ?, ?, 1, 0, NULL, ?, ?, ?)")
            for i, q in enumerate(subject.questions):
                tid = topic_ids[q.topic]
                head_id = conn.execute(
                    insert, (tid, q.stem, json.dumps(q.options, ensure_ascii=False), q.correct_index,
                             q.explanation, q.common_error, q.check_expr, f"demo:{i}", None, None)).lastrowid
                if not q.variants:
                    continue
                # Como `pipeline._finalize`: la original es la cabeza (`variant_group` = su id).
                conn.execute("UPDATE question SET variant_group = ?, variant_values_json = ? WHERE id = ?",
                             (head_id, json.dumps(q.variant_values), head_id))
                for k, v in enumerate(q.variants, start=1):
                    conn.execute(
                        insert, (tid, v.stem, json.dumps(v.options, ensure_ascii=False), v.correct_index,
                                 v.explanation, v.common_error, v.check_expr, f"demo:{i}:{k}", head_id,
                                 json.dumps(v.values)))
            conn.execute(
                "INSERT INTO exam(id, course_id, cmid, name, opens_at, max_grade, source, topics_json, kind,"
                " created_at) VALUES (1, 1, NULL, ?, ?, ?, 'manual', ?, 'parcial', ?)",
                (subject.exam_name, _exam_at(now, subject.exam_days_ahead), subject.max_grade,
                 json.dumps(list(topic_ids.values())), now))
    finally:
        conn.close()


def _campus_connected(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (SITE_KEY,)).fetchone()
    return row is not None and bool(row[0]) and bool(get_secret(moodle_token_name(row[0])))


def _remove_files(db_path: Path) -> None:
    base = demo_path(db_path)
    for suffix in _DEMO_SUFFIXES:
        path = base.with_name(base.name + suffix)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            # Windows no borra un archivo abierto: queda apagada y se borra al próximo arranque.
            log.info("No se pudo borrar %s (%s); se borra al próximo arranque", path.name, type(exc).__name__)


def start_demo(conn: sqlite3.Connection, db_path: Path, now: int) -> None:
    """Prende la demo: arma demo.db aparte y recién después guarda la marca en la base real.

    `DemoUnavailable` si el campus ya está conectado o la base real ya tiene materias.
    """
    if _campus_connected(conn) or conn.execute("SELECT 1 FROM course LIMIT 1").fetchone() is not None:
        raise DemoUnavailable(DEMO_CONNECTED_TEXT)
    if demo_active(conn, db_path):
        return
    target = demo_path(db_path)
    tmp = target.with_name(target.name + ".tmp")
    for suffix in ("", "-journal", "-wal", "-shm"):
        tmp.with_name(tmp.name + suffix).unlink(missing_ok=True)
    build_demo_db(tmp, load_demo_subject(), now)
    os.replace(tmp, target)
    conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?, '1')", (DEMO_KEY,))
    conn.commit()


def remove_demo(conn: sqlite3.Connection, db_path: Path) -> None:
    """Apaga la demo (la marca primero) y borra sus archivos. Si un archivo no se deja borrar,
    la demo igual queda apagada y `cleanup_leftover_demo` lo borra en el próximo arranque."""
    conn.execute("DELETE FROM kv WHERE key = ?", (DEMO_KEY,))
    conn.commit()
    _remove_files(db_path)


def cleanup_leftover_demo(conn: sqlite3.Connection, db_path: Path) -> None:
    """Sin la marca, borra lo que haya quedado de una demo (un borrado que falló, un armado cortado)."""
    if not _flag_on(conn):
        _remove_files(db_path)
