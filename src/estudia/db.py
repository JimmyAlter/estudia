"""Conexión SQLite y esquema."""
import sqlite3
from pathlib import Path

from estudia.config import data_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS site(
    id TEXT PRIMARY KEY, url TEXT, userid INTEGER, fullname TEXT);
CREATE TABLE IF NOT EXISTS course(
    id INTEGER, site_id TEXT, fullname TEXT, shortname TEXT,
    PRIMARY KEY(site_id, id));
CREATE TABLE IF NOT EXISTS section(
    id INTEGER PRIMARY KEY, course_id INTEGER, num INTEGER, name TEXT,
    visible INTEGER);
CREATE TABLE IF NOT EXISTS material(
    id INTEGER PRIMARY KEY, section_id INTEGER, cmid INTEGER UNIQUE,
    kind TEXT,                          -- 'youtube'
    title TEXT, ref TEXT,               -- id de YouTube
    status TEXT DEFAULT 'pending',      -- pending|done|failed|unsupported
    summary_md TEXT, error TEXT, updated_at INTEGER,
    fail_kind TEXT,                     -- si failed: format|video|error
    analysis_json TEXT,                 -- análisis ya pagado de un pendiente
    analysis_model TEXT);               -- modelo que hizo ese análisis
CREATE TABLE IF NOT EXISTS topic(
    id INTEGER PRIMARY KEY, course_id INTEGER, slug TEXT, name TEXT,
    UNIQUE(course_id, slug));
CREATE TABLE IF NOT EXISTS material_topic(
    material_id INTEGER, topic_id INTEGER,
    PRIMARY KEY(material_id, topic_id));
CREATE TABLE IF NOT EXISTS exam(
    id INTEGER PRIMARY KEY, course_id INTEGER, cmid INTEGER UNIQUE, name TEXT,
    opens_at INTEGER, closes_at INTEGER, attempts INTEGER,
    time_limit_s INTEGER, max_grade REAL);
CREATE TABLE IF NOT EXISTS question(
    id INTEGER PRIMARY KEY, material_id INTEGER, topic_id INTEGER,
    stem TEXT, options_json TEXT, correct_index INTEGER, explanation TEXT,
    common_error TEXT, source_seconds INTEGER, check_expr TEXT,
    verified INTEGER DEFAULT 0, retired INTEGER DEFAULT 0,
    dedupe_key TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS answer(
    id INTEGER PRIMARY KEY, question_id INTEGER, chosen_index INTEGER,
    correct INTEGER, at INTEGER);
CREATE TABLE IF NOT EXISTS kv(
    key TEXT PRIMARY KEY, value TEXT);  -- last_sync_at, etc.
"""

# Columnas agregadas después de la primera versión: las bases viejas las reciben
# con ALTER TABLE al conectar. Solo columnas nuevas y anulables (sin pérdida de datos).
ADDED_COLUMNS = (
    ("material", "fail_kind", "TEXT"),
    ("material", "analysis_json", "TEXT"),
    ("material", "analysis_model", "TEXT"),
)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, decl in ADDED_COLUMNS:
        if column in _columns(conn, table):
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        except sqlite3.OperationalError:
            # Otra conexión pudo agregarla recién; si sigue faltando, es un error de verdad.
            if column not in _columns(conn, table):
                raise
    conn.commit()


def connect(path: Path | None = None, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Abre la base (por defecto en la carpeta de datos) y aplica el esquema.

    `check_same_thread=False` es para conexiones que abre y cierra un hilo distinto
    del que las usa (el servidor): cada pedido tiene la suya, nunca se comparten.
    """
    if path is None:
        path = data_dir() / "estudia.db"
    conn = sqlite3.connect(path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn
