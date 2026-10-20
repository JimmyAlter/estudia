"""Conexión SQLite y esquema."""
import sqlite3
from pathlib import Path, PurePath
from urllib.parse import quote

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
CREATE TABLE IF NOT EXISTS class_slot(
    id INTEGER PRIMARY KEY, course_id INTEGER, weekday INTEGER,  -- 0=lunes
    start_min INTEGER, end_min INTEGER, place TEXT, created_at INTEGER);
CREATE TABLE IF NOT EXISTS deadline(
    id INTEGER PRIMARY KEY, course_id INTEGER, kind TEXT,        -- 'assign'|'event'
    ext_id INTEGER, name TEXT, due_at INTEGER, url TEXT, UNIQUE(kind, ext_id));
CREATE TABLE IF NOT EXISTS plan_session(
    id INTEGER PRIMARY KEY, course_id INTEGER, topic_id INTEGER, exam_id INTEGER,
    starts_at INTEGER, minutes INTEGER, done INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS availability(
    weekday INTEGER PRIMARY KEY, minutes INTEGER);
CREATE TABLE IF NOT EXISTS mock_attempt(
    id INTEGER PRIMARY KEY, exam_id INTEGER, started_at INTEGER, finished_at INTEGER,
    question_ids_json TEXT, answers_json TEXT, grade REAL, max_grade REAL);
CREATE TABLE IF NOT EXISTS exam_result(
    exam_id INTEGER PRIMARY KEY,        -- la nota real que cargó el estudiante, una por examen
    grade REAL, max_grade REAL, at INTEGER);
CREATE TABLE IF NOT EXISTS review_state(
    card_key TEXT PRIMARY KEY,          -- 'q:<id>'; más adelante 'v:<grupo de variantes>'
    stability REAL, difficulty REAL,    -- FSRS
    state INTEGER,                      -- 2 repaso, 3 en reaprendizaje (como fsrs.State)
    due INTEGER, last_review INTEGER,   -- epoch UTC
    relearn_days INTEGER DEFAULT 0,     -- días locales distintos con acierto desde el último error
    relearn_last_day TEXT);             -- el último de esos días, AAAA-MM-DD en Buenos Aires
CREATE TABLE IF NOT EXISTS past_exam(
    id INTEGER PRIMARY KEY, exam_id INTEGER NOT NULL,
    origin TEXT NOT NULL DEFAULT 'parcial_propio',   -- nunca sale de la compu
    name TEXT, kind TEXT,               -- pdf | jpeg | png (por los bytes)
    path TEXT, size INTEGER, sha256 TEXT,   -- path relativa a la carpeta de datos
    status TEXT DEFAULT 'pending',      -- pending | done | failed
    style_json TEXT, error TEXT, created_at INTEGER,
    UNIQUE(exam_id, sha256));
"""

# Columnas agregadas después de la primera versión: las bases viejas las reciben
# con ALTER TABLE al conectar. Solo columnas nuevas y anulables (sin pérdida de datos).
ADDED_COLUMNS = (
    ("material", "fail_kind", "TEXT"),
    ("material", "analysis_json", "TEXT"),
    ("material", "analysis_model", "TEXT"),
    ("material", "segments_json", "TEXT"),         # tramos [{start_s, end_s, title}] de la clase; NULL = ninguno
    ("exam", "source", "TEXT DEFAULT 'campus'"),   # 'campus' | 'manual'
    ("exam", "topics_json", "TEXT"),               # solo manuales: ids de temas
    ("exam", "kind", "TEXT DEFAULT 'parcial'"),    # parcial | recuperatorio | final | inscripcion
    ("exam", "created_at", "INTEGER"),             # cuándo se cargó; NULL en los anteriores
    ("mock_attempt", "seconds", "INTEGER"),        # reloj del simulacro al empezar
    ("mock_attempt", "predicted_grade", "REAL"),   # la nota que el estudiante esperaba, en la escala del examen
    ("mock_attempt", "typed_json", "TEXT"),        # ids de las preguntas que se responden escribiendo
    ("answer", "confidence", "INTEGER"),           # 0 adiviné, 1 me parece, 2 seguro; NULL = seguro
    ("answer", "latency_ms", "INTEGER"),           # desde que se mostró el enunciado
    ("answer", "mode", "TEXT"),                    # mcq|mcq_think_first|typed|typed_then_mcq|mock|pretest|probe
    ("answer", "option_order_json", "TEXT"),       # orden en que se mostraron las opciones
    ("answer", "typed_text", "TEXT"),              # lo que escribió, si escribió
    ("answer", "options_revealed_ms", "INTEGER"),  # "pensalo primero": cuándo abrió las opciones
    ("answer", "explain_prompted", "INTEGER"),     # 1 si después de este acierto se pidió "Explicá por qué"
    ("answer", "explain_match", "INTEGER"),        # 1 "Sí, dije lo mismo", 0 "No"; NULL = sin respuesta. Lo escrito no se guarda
    ("plan_session", "kind", "TEXT DEFAULT 'focus'"),  # focus | mixed_focus | mixed_only
    ("plan_session", "topics_json", "TEXT"),       # temas de la sesión, el foco primero; NULL = solo topic_id
    # ¿Se puede ofrecer escrita? 1 sí; 0 no (la regla escrita aceptaría un distractor, "1.41" por √2);
    # NULL todavía no se sabe: se calcula en el worker la primera vez que haría falta (practice.typed_safe_ids).
    ("question", "typed_safe", "INTEGER"),
    # Página del documento entero (desde 1) o número de diapositiva; NULL en los videos o sin cita.
    ("question", "source_page", "INTEGER"),
    # Archivos del campus (`resource` y `folder`). NULL en los videos y en las pages.
    ("material", "file_key", "TEXT"),              # "{module_id}:{filepath}{filename}"
    ("material", "module_id", "INTEGER"),          # id del módulo del campus
    ("material", "module_kind", "TEXT"),           # resource | folder
    ("material", "remote_url", "TEXT"),            # la `fileurl` del campus, sin token
    ("material", "remote_size", "INTEGER"),        # bytes según el campus
    ("material", "remote_modified", "INTEGER"),    # `timemodified` según el campus; si cambia, se baja de nuevo
    ("material", "parent_id", "INTEGER"),          # en una parte: el documento que se partió
    ("material", "page_from", "INTEGER"),          # en una parte: desde qué página (desde 1)
    ("material", "page_to", "INTEGER"),            # en una parte: hasta qué página (inclusivo)
    ("material", "pages", "INTEGER"),              # páginas, o diapositivas
    ("material", "has_text", "INTEGER"),           # 0 si es escaneado (casi sin texto)
    ("material", "file_sha256", "TEXT"),           # del archivo bajado
    ("material", "download_attempts", "INTEGER"),  # descargas que fallaron; NULL = ninguna
    ("exam", "style_json", "TEXT"),                # estilo del examen (ExamStyle) que eligió el estudiante; NULL = ninguno
    # Preguntas generadas con el estilo de un examen: nunca salen de la compu (ni Anki, ni exportaciones).
    ("question", "origin", "TEXT"),                # NULL = del campus; 'parcial_propio' = generada con el estilo
    ("question", "style_exam_id", "INTEGER"),      # el examen de cuyo estilo salió
    ("question", "style_kind", "TEXT"),            # opcion_multiple | verdadero_falso | desarrollo
    # Sube cada vez que se borra el estilo (y se retiran sus preguntas): va en la clave de las
    # preguntas de estilo, así un estilo guardado de nuevo puede volver a generar el mismo enunciado.
    ("exam", "style_revision", "INTEGER"),         # NULL = 0
    # Simulacro con el formato de la cátedra: puntos de cada pregunta (alineados con
    # question_ids_json) y 1 si se armó con el estilo. NULL en los de siempre.
    ("mock_attempt", "points_json", "TEXT"),
    ("mock_attempt", "style", "INTEGER"),
    # Variantes numéricas (etapa 3c): id de la original del grupo (la original lo tiene con su propio
    # id); NULL = sin grupo. Y los valores de los parámetros ({"a": 3, …}), también en la original.
    ("question", "variant_group", "INTEGER"),
    ("question", "variant_values_json", "TEXT"),
)

# Índices sobre columnas de ADDED_COLUMNS: corren después de agregarlas. Un índice único
# de SQLite admite muchos NULL, así que los materiales de antes (sin archivo) no chocan.
ADDED_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS material_file_key ON material(file_key)",
    "CREATE INDEX IF NOT EXISTS question_variant_group ON question(variant_group)",
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
    for statement in ADDED_INDEXES:
        conn.execute(statement)
    conn.commit()


# La descarga de videos (yt-dlp) pasó a ser opcional y apagada. Una instalación que ya
# analizó videos bajándolos la conserva encendida, una sola vez (kv de abajo).
DOWNLOAD_MIGRATED_KEY = "allow_download_migrated"
VIDEO_FILE_SUFFIXES = (".mp4", ".webm", ".mkv")


def _kv_value(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _has_downloaded_videos(video_dir: Path) -> bool:
    try:
        return any(p.is_file() and p.suffix.lower() in VIDEO_FILE_SUFFIXES for p in video_dir.iterdir())
    except OSError:
        return False


def _migrate_allow_download(conn: sqlite3.Connection, path: Path) -> None:
    """Prende `allow_download` si esta instalación ya usaba la descarga. Corre una vez.

    No se anotaba qué material se bajó: cuenta como usada si Gemini ya había rechazado
    el enlace (la marca vieja "denied", con la que todo se bajaba) y hay clases listas,
    o si quedan videos bajados en la carpeta `videos` junto a la base.
    """
    if _kv_value(conn, DOWNLOAD_MIGRATED_KEY) is not None:
        return
    if _kv_value(conn, "allow_download") is None:
        legacy_denied = _kv_value(conn, "youtube_url_input") == "denied"
        done = conn.execute(
            "SELECT 1 FROM material WHERE kind='youtube' AND status='done' LIMIT 1").fetchone()
        on_disk = str(path) != ":memory:" and _has_downloaded_videos(Path(path).parent / "videos")
        if (legacy_denied and done) or on_disk:
            conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES ('allow_download', '1')")
    conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?, '1')", (DOWNLOAD_MIGRATED_KEY,))
    conn.commit()


# El estado del calendario del campus antes se guardaba en una sola marca global; ahora va por
# campus (`calendar_feed_status:<sitio>`). La vieja no se lee más: se borra al abrir la base.
LEGACY_FEED_STATUS_KEY = "calendar_feed_status"


def _drop_legacy_feed_status(conn: sqlite3.Connection) -> None:
    """Borra la marca global vieja si está. Si no está, solo lee."""
    if _kv_value(conn, LEGACY_FEED_STATUS_KEY) is not None:
        conn.execute("DELETE FROM kv WHERE key = ?", (LEGACY_FEED_STATUS_KEY,))
        conn.commit()


def rw_uri(path: PurePath) -> str:
    """URI de SQLite que abre `path` solo si ya existe. Autoridad vacía explícita (`file://`), así
    una ruta UNC (`//servidor/carpeta`) o con letra de unidad (`C:/...`) llega entera a SQLite;
    `?`, `#` y `%` en las carpetas van escapados."""
    posix = path.as_posix()
    if not posix.startswith("/"):
        posix = "/" + posix   # C:/x -> /C:/x
    return f"file://{quote(posix, safe='/:')}?mode=rw"


def connect(path: Path | None = None, *, check_same_thread: bool = True,
            create: bool = True) -> sqlite3.Connection:
    """Abre la base (por defecto en la carpeta de datos) y aplica el esquema.

    `check_same_thread=False` es para conexiones que abre y cierra un hilo distinto
    del que las usa (el servidor): cada pedido tiene la suya, nunca se comparten.

    Con `create=False` la base tiene que existir (`mode=rw`): si no está, `sqlite3.OperationalError`
    y no se crea ningún archivo. La usa la demo, que puede borrarse mientras un pedido la abre.
    """
    if path is None:
        path = data_dir() / "estudia.db"
    if create:
        conn = sqlite3.connect(path, check_same_thread=check_same_thread)
    else:
        conn = sqlite3.connect(rw_uri(Path(path).resolve()), uri=True,
                               check_same_thread=check_same_thread)
    # Si algo falla (una base rota), la conexión se cierra: en Windows un archivo abierto no se borra.
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        _migrate(conn)
        _migrate_allow_download(conn, path)
        _drop_legacy_feed_status(conn)
        # La primera vez reproduce la historia de respuestas en review_state; después no hace nada.
        from estudia.study.srs import sync_review_state

        sync_review_state(conn)
        conn.commit()
    except BaseException:
        conn.close()
        raise
    return conn
