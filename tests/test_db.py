import pytest

from estudia.db import connect

TABLES = {
    "site", "course", "section", "material", "topic", "material_topic",
    "exam", "question", "answer", "kv",
    "class_slot", "deadline", "plan_session", "availability", "mock_attempt", "review_state", "exam_result",
    "past_exam",
}


def _tables(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r["name"] for r in rows}


def test_connect_creates_schema(tmp_path):
    conn = connect(tmp_path / "e.db")
    assert _tables(conn) == TABLES
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.close()


def test_connect_is_idempotent(tmp_path):
    connect(tmp_path / "e.db").close()
    conn = connect(tmp_path / "e.db")
    assert _tables(conn) == TABLES


def _material_columns(conn):
    return {r[1] for r in conn.execute("PRAGMA table_info(material)")}


def test_migrates_a_database_from_before_fail_kind(tmp_path):
    import sqlite3

    path = tmp_path / "vieja.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE material(id INTEGER PRIMARY KEY, section_id INTEGER, cmid INTEGER UNIQUE,"
        " kind TEXT, title TEXT, ref TEXT, status TEXT DEFAULT 'pending',"
        " summary_md TEXT, error TEXT, updated_at INTEGER)"
    )
    old.execute("INSERT INTO material(id, title, status, error) VALUES (1, 'Clase', 'failed', 'x')")
    old.commit()
    old.close()

    conn = connect(path)
    assert {"fail_kind", "analysis_json"} <= _material_columns(conn)
    row = conn.execute("SELECT title, status, error, fail_kind, analysis_json FROM material").fetchone()
    assert tuple(row) == ("Clase", "failed", "x", None, None)
    conn.close()
    # Conectar de nuevo no vuelve a migrar ni falla.
    connect(path).close()


def test_new_database_has_the_added_columns(tmp_path):
    conn = connect(tmp_path / "e.db")
    assert {"fail_kind", "analysis_json"} <= _material_columns(conn)
    conn.close()


# --- D2: la descarga de videos queda apagada, salvo en instalaciones que ya la usaban ----

def _kv(conn, key):
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _existing_install(path, *, denied=False, done=False):
    """Una base de antes de la opción de descarga."""
    import sqlite3

    old = sqlite3.connect(path)
    old.execute("CREATE TABLE kv(key TEXT PRIMARY KEY, value TEXT)")
    old.execute("CREATE TABLE material(id INTEGER PRIMARY KEY, section_id INTEGER, cmid INTEGER UNIQUE,"
                " kind TEXT, title TEXT, ref TEXT, status TEXT DEFAULT 'pending',"
                " summary_md TEXT, error TEXT, updated_at INTEGER)")
    old.execute("INSERT INTO kv(key, value) VALUES ('last_sync_at', '1')")
    if denied:
        old.execute("INSERT INTO kv(key, value) VALUES ('youtube_url_input', 'denied')")
    if done:
        old.execute("INSERT INTO material(id, kind, title, ref, status)"
                    " VALUES (1, 'youtube', 'Clase', 'aaaaaaaaaaa', 'done')")
    old.commit()
    old.close()


def test_fresh_install_keeps_download_off(tmp_path):
    conn = connect(tmp_path / "estudia.db")
    assert _kv(conn, "allow_download") is None
    conn.close()


def test_install_that_processed_videos_by_download_keeps_download_on(tmp_path):
    path = tmp_path / "estudia.db"
    _existing_install(path, denied=True, done=True)
    conn = connect(path)
    assert _kv(conn, "allow_download") == "1"
    conn.close()


def test_install_with_downloaded_videos_on_disk_keeps_download_on(tmp_path):
    path = tmp_path / "estudia.db"
    _existing_install(path)
    (tmp_path / "videos").mkdir()
    (tmp_path / "videos" / "aaaaaaaaaaa.mp4").write_bytes(b"video")
    conn = connect(path)
    assert _kv(conn, "allow_download") == "1"
    conn.close()


@pytest.mark.parametrize("denied, done", [(False, True), (True, False), (False, False)])
def test_install_that_never_downloaded_stays_off(tmp_path, denied, done):
    path = tmp_path / "estudia.db"
    _existing_install(path, denied=denied, done=done)
    (tmp_path / "videos").mkdir()  # carpeta vacía (o con restos que no son videos)
    (tmp_path / "videos" / ".aaaaaaaaaaa-tmp").mkdir()
    conn = connect(path)
    assert _kv(conn, "allow_download") is None
    conn.close()


def test_download_migration_runs_once(tmp_path):
    path = tmp_path / "estudia.db"
    _existing_install(path, denied=True, done=True)
    conn = connect(path)
    assert _kv(conn, "allow_download") == "1"
    # El estudiante la apaga en Ajustes: reconectar no la vuelve a prender.
    conn.execute("UPDATE kv SET value='0' WHERE key='allow_download'")
    conn.commit()
    conn.close()
    conn = connect(path)
    assert _kv(conn, "allow_download") == "0"
    conn.close()


def test_fresh_install_does_not_turn_on_later(tmp_path):
    """Una instalación nueva que después usa la ruta del enlace no se prende sola."""
    path = tmp_path / "estudia.db"
    conn = connect(path)
    conn.execute("INSERT INTO kv(key, value) VALUES ('youtube_url_input', '99999999999')")
    conn.execute("INSERT INTO material(id, kind, title, ref, status) VALUES (1, 'youtube', 'C', 'a', 'done')")
    conn.commit()
    conn.close()
    conn = connect(path)
    assert _kv(conn, "allow_download") is None
    conn.close()


def test_connect_drops_the_old_global_calendar_feed_status(tmp_path):
    """La marca global vieja del calendario (antes de ser por campus) se borra al abrir la base."""
    conn = connect(tmp_path / "e.db")
    conn.execute("INSERT INTO kv(key, value) VALUES ('calendar_feed_status', 'unavailable')")
    conn.execute("INSERT INTO kv(key, value) VALUES ('calendar_feed_status:utn-frba', 'saved')")
    conn.commit()
    conn.close()
    for _ in range(2):   # la segunda vez no hay nada que borrar y no falla
        conn = connect(tmp_path / "e.db")
        keys = {r[0] for r in conn.execute("SELECT key FROM kv WHERE key LIKE 'calendar_feed_status%'")}
        assert keys == {"calendar_feed_status:utn-frba"}
        conn.close()


FILE_COLUMNS = {"file_key", "module_id", "module_kind", "remote_url", "remote_size", "parent_id",
                "page_from", "page_to", "pages", "has_text", "file_sha256", "download_attempts"}


def test_old_db_gets_file_columns_and_index(tmp_path):
    """Una base de antes de los archivos del campus, con varios videos: migra dos veces sin chocar."""
    import sqlite3

    path = tmp_path / "vieja.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE material(id INTEGER PRIMARY KEY, section_id INTEGER, cmid INTEGER UNIQUE,"
        " kind TEXT, title TEXT, ref TEXT, status TEXT DEFAULT 'pending', summary_md TEXT,"
        " error TEXT, updated_at INTEGER, fail_kind TEXT, analysis_json TEXT, analysis_model TEXT,"
        " segments_json TEXT)")
    for i in range(1, 6):
        old.execute("INSERT INTO material(id, cmid, kind, title, ref, status) VALUES (?, ?, 'youtube',"
                    " ?, ?, 'done')", (i, 1000 + i, f"Clase {i}", f"vid{i:08d}"))
    old.commit()
    old.close()

    connect(path).close()
    conn = connect(path)
    assert FILE_COLUMNS <= _material_columns(conn)
    index = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='material_file_key'").fetchone()
    assert index is not None and "UNIQUE" in index["sql"]
    rows = conn.execute("SELECT kind, status, file_key, parent_id FROM material ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("youtube", "done", None, None)] * 5
    conn.execute("INSERT INTO material(kind, file_key) VALUES ('pdf', '7:/a.pdf')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO material(kind, file_key) VALUES ('pdf', '7:/a.pdf')")
    conn.close()


def test_new_database_has_the_file_columns(tmp_path):
    conn = connect(tmp_path / "e.db")
    assert FILE_COLUMNS <= _material_columns(conn)
    conn.execute("INSERT INTO material(kind) VALUES ('youtube')")
    conn.execute("INSERT INTO material(kind) VALUES ('youtube')")   # muchos NULL no chocan
    conn.close()


def test_connect_without_create_never_creates_the_file(tmp_path):
    import sqlite3

    missing = tmp_path / "no-existe.db"
    with pytest.raises(sqlite3.OperationalError):
        connect(missing, create=False)
    assert not missing.exists()


def test_connect_without_create_opens_an_existing_base(tmp_path):
    path = tmp_path / "esta.db"
    connect(path).close()
    conn = connect(path, create=False)
    conn.execute("INSERT INTO kv(key, value) VALUES ('x', '1')")
    conn.commit()
    conn.close()
    assert connect(path).execute("SELECT value FROM kv WHERE key = 'x'").fetchone()[0] == "1"


def test_rw_uri_handles_unc_and_windows_paths():
    from pathlib import PurePosixPath, PureWindowsPath

    from estudia.db import rw_uri

    assert rw_uri(PureWindowsPath(r"\\servidor\compartida\estudia\demo.db")) == \
        "file:////servidor/compartida/estudia/demo.db?mode=rw"
    assert rw_uri(PureWindowsPath(r"C:\Users\Ana María\demo.db")) == \
        "file:///C:/Users/Ana%20Mar%C3%ADa/demo.db?mode=rw"
    # Un ? o un # en la carpeta no cortan la ruta.
    assert rw_uri(PurePosixPath("/home/ana/¿qué?#1/demo.db")) == \
        "file:///home/ana/%C2%BFqu%C3%A9%3F%231/demo.db?mode=rw"


def test_connect_without_create_works_with_odd_characters(tmp_path):
    folder = tmp_path / "con espacio #1 ?x%"
    folder.mkdir()
    connect(folder / "a.db").close()
    connect(folder / "a.db", create=False).close()
    assert sorted(p.name for p in folder.iterdir()) == ["a.db"]


def test_connect_closes_the_connection_when_the_base_is_corrupt(tmp_path, monkeypatch):
    import sqlite3

    path = tmp_path / "rota.db"
    path.write_bytes(b"esto no es una base de SQLite" * 100)
    closed = []
    real_connect = sqlite3.connect

    class Spy:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def __setattr__(self, name, value):
            if name == "_conn":
                object.__setattr__(self, name, value)
            else:
                setattr(self._conn, name, value)

        def close(self):
            closed.append(True)
            self._conn.close()

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: Spy(real_connect(*a, **k)))
    with pytest.raises(sqlite3.DatabaseError):
        connect(path, create=False)
    assert closed == [True]


def test_variant_columns_migrate_old_db(tmp_path):
    """Una base de antes de las variantes (el SCHEMA sin las columnas nuevas) las recibe al
    conectar, con su índice, y conectar otra vez no falla."""
    import sqlite3

    from estudia.db import SCHEMA

    path = tmp_path / "vieja.db"
    old = sqlite3.connect(path)
    old.executescript(SCHEMA)
    assert "variant_group" not in {r[1] for r in old.execute("PRAGMA table_info(question)")}
    old.execute("INSERT INTO question(id, stem, verified, retired, dedupe_key) VALUES (1, 'p', 1, 0, 'k')")
    old.commit()
    old.close()

    connect(path).close()
    conn = connect(path)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(question)")}
    assert {"variant_group", "variant_values_json"} <= columns
    index = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='question_variant_group'").fetchone()
    assert index is not None and "variant_group" in index["sql"]
    row = conn.execute("SELECT stem, variant_group, variant_values_json FROM question").fetchone()
    assert tuple(row) == ("p", None, None)
    conn.close()
