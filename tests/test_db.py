from estudia.db import connect

TABLES = {
    "site", "course", "section", "material", "topic", "material_topic",
    "exam", "question", "answer", "kv",
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
