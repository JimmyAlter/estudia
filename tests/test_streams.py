import os
import subprocess
import sys

import pytest

from estudia import streams
from estudia.__main__ import uvicorn_config
from estudia.server.app import create_app


@pytest.fixture
def no_streams(monkeypatch):
    """Lo que ve un .exe de PyInstaller sin consola en Windows.

    Se llama dentro del test: pytest cambia `sys.stdout` al arrancar cada fase.
    """
    opened = []

    def strip():
        monkeypatch.setattr(sys, "stdout", None)
        monkeypatch.setattr(sys, "stderr", None)

    def redirect(log_path):
        strip()
        streams.redirect_missing_streams(log_path)
        opened.append(sys.stdout)

    yield redirect
    for stream in opened:
        stream.close()


def test_missing_streams_go_to_the_log(tmp_path, no_streams):
    log = tmp_path / "estudia.log"
    no_streams(log)
    print("hola")
    print("chau", file=sys.stderr)
    assert log.read_text("utf-8") == "hola\nchau\n"


def test_uvicorn_config_works_without_streams(tmp_path, no_streams):
    app = create_app(port=1)
    no_streams(tmp_path / "estudia.log")
    config = uvicorn_config(app, 1)  # el logging de uvicorn se configura acá
    config.load()
    assert config.use_colors is False


def test_uvicorn_config_without_colors_survives_even_without_the_guard(monkeypatch):
    """Con colores, uvicorn pregunta `sys.stdout.isatty()` y revienta si no hay stdout."""
    app = create_app(port=1)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    uvicorn_config(app, 1).load()


def test_log_rotates_past_the_cap(tmp_path, no_streams):
    log = tmp_path / "estudia.log"
    log.write_bytes(b"x" * (streams.MAX_LOG_BYTES + 1))
    no_streams(log)
    print("nuevo")
    assert log.read_text("utf-8") == "nuevo\n"
    assert (tmp_path / "estudia.log.1").stat().st_size == streams.MAX_LOG_BYTES + 1


def test_without_log_path_uses_devnull(no_streams):
    no_streams(None)
    assert sys.stdout.name == os.devnull
    print("se pierde")


def test_existing_streams_are_kept(tmp_path, capsys):
    before = sys.stdout
    streams.redirect_missing_streams(tmp_path / "estudia.log")
    assert sys.stdout is before
    assert not (tmp_path / "estudia.log").exists()


def test_handler_data_dir_without_stdout_writes_nothing(tmp_path):
    """El handler sin consola y sin stdout: no crea nada (ni log ni carpeta de datos)."""
    code = (
        "import sys, runpy; sys.stdout = None; sys.stderr = None; "
        "sys.argv = ['handler', '--data-dir']; "
        "runpy.run_module('estudia.server.handler', run_name='__main__')"
    )
    env = {**os.environ, "HOME": str(tmp_path), "XDG_DATA_HOME": str(tmp_path / "data"),
           "LOCALAPPDATA": str(tmp_path / "data")}
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                            cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert list(tmp_path.iterdir()) == []
