"""Guardia: los tests nunca resuelven la carpeta de datos dentro del HOME real."""
from pathlib import Path

from estudia import config

# Se lee al importar el módulo, antes de que el fixture cambie HOME/USERPROFILE.
REAL_HOME = Path.home().resolve()


def _real_home() -> Path:
    return REAL_HOME


def test_data_dir_is_redirected_to_a_temporary_home(isolated_data_dir):
    path = config.data_dir().resolve()
    assert path.is_relative_to(Path(isolated_data_dir).resolve())
    assert not path.is_relative_to(_real_home())


def test_default_database_path_is_not_the_real_one(isolated_data_dir):
    from estudia.db import connect

    conn = connect()  # la base por defecto, como la abre la app
    try:
        file = Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve()
    finally:
        conn.close()
    assert file.is_relative_to(Path(isolated_data_dir).resolve())


def test_env_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv(config.DATA_DIR_ENV, str(tmp_path / "x"))
    assert config.data_path() == tmp_path / "x"


def test_without_override_uses_platformdirs(monkeypatch):
    import platformdirs

    monkeypatch.delenv(config.DATA_DIR_ENV, raising=False)
    assert config.data_path() == Path(platformdirs.user_data_dir("estudia"))
