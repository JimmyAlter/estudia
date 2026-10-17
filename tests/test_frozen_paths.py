import subprocess
import sys
from pathlib import Path

import pytest

from estudia import config
from estudia.material.video import js_runtimes


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    """Simula el ejecutable de PyInstaller: `sys.frozen` y la carpeta `_MEIPASS`."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    return tmp_path


def _place_runtime(meipass: Path) -> Path:
    exe = "node.exe" if sys.platform == "win32" else "node"
    path = meipass / "runtime" / exe
    path.parent.mkdir()
    path.write_bytes(b"")
    return path


def test_bundled_runtime_when_frozen(frozen):
    path = _place_runtime(frozen)
    assert config.bundled_js_runtime() == path


def test_no_bundled_runtime_when_frozen_without_binary(frozen):
    assert config.bundled_js_runtime() is None


def test_no_bundled_runtime_when_not_frozen(tmp_path, monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    _place_runtime(tmp_path)
    assert config.bundled_js_runtime() is None


def test_video_uses_only_the_bundled_runtime(frozen):
    path = _place_runtime(frozen)
    assert js_runtimes() == {"node": {"path": str(path)}}


def test_handler_data_dir_prints_path(tmp_path):
    """Lo que hace el desinstalador: correr el handler con `--data-dir` y leer la salida."""
    result = subprocess.run(
        [sys.executable, "-m", "estudia.server.handler", "--data-dir"],
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == str(config.data_path())
    assert result.stdout.count("\n") == 1
