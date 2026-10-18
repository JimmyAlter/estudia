"""Guardia: los tests nunca resuelven la carpeta de datos real del usuario."""
from pathlib import Path

import platformdirs

from estudia import config

# Se calcula al importar el módulo, antes de que el fixture cambie el entorno: es la
# carpeta de datos real del usuario. (No se compara con el HOME: en Windows la carpeta
# temporal de los tests está dentro del perfil del usuario.)
REAL_DATA = Path(platformdirs.user_data_dir("estudia")).resolve()


def test_data_dir_is_redirected_to_a_temporary_home(isolated_data_dir):
    path = config.data_dir().resolve()
    assert path.is_relative_to(Path(isolated_data_dir).resolve())
    assert not path.is_relative_to(REAL_DATA)


def test_default_database_path_is_not_the_real_one(isolated_data_dir):
    from estudia.db import connect

    conn = connect()  # la base por defecto, como la abre la app
    try:
        file = Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve()
    finally:
        conn.close()
    assert file.is_relative_to(Path(isolated_data_dir).resolve())
    assert not file.is_relative_to(REAL_DATA)


def test_env_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv(config.DATA_DIR_ENV, str(tmp_path / "x"))
    assert config.data_path() == tmp_path / "x"


def test_without_override_uses_platformdirs(monkeypatch):
    import platformdirs

    monkeypatch.delenv(config.DATA_DIR_ENV, raising=False)
    assert config.data_path() == Path(platformdirs.user_data_dir("estudia"))
