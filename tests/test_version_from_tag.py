import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "version_from_tag.py"


def _fake_repo(root: Path) -> None:
    (root / "src" / "estudia").mkdir(parents=True)
    (root / "web").mkdir()
    (root / "packaging" / "windows").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "estudia"\nversion = "0.1.0"\n\n'
        '[tool.otra]\nversion = "9.9.9"\n', "utf-8")
    (root / "src" / "estudia" / "version.py").write_text(
        '"""Versión de Estudia."""\n\n__version__ = "0.1.0"\n', "utf-8")
    (root / "packaging" / "windows" / "version_info.txt").write_text("viejo", "utf-8")


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, encoding="utf-8")


def test_version_from_tag_rewrites_files(tmp_path):
    _fake_repo(tmp_path)
    result = _run("v0.2.13", "--root", str(tmp_path))
    assert result.returncode == 0, result.stderr

    pyproject = (tmp_path / "pyproject.toml").read_text("utf-8")
    assert 'version = "0.2.13"' in pyproject
    assert 'version = "9.9.9"' in pyproject  # solo la de [project]
    assert '__version__ = "0.2.13"' in (tmp_path / "src/estudia/version.py").read_text("utf-8")
    assert (tmp_path / "web/.env.production").read_text("utf-8") == "VITE_APP_VERSION=0.2.13\n"

    info = (tmp_path / "packaging/windows/version_info.txt").read_text("utf-8")
    assert "filevers=(0, 2, 13, 0)" in info
    assert "prodvers=(0, 2, 13, 0)" in info
    assert "StringStruct('ProductVersion', '0.2.13')" in info
    assert "StringStruct('FileVersion', '0.2.13')" in info


def test_committed_version_info_matches_the_package():
    """El version_info del repo es el que escribiría el script para la versión actual."""
    from estudia.version import __version__

    sys.path.insert(0, str(SCRIPT.parent))
    try:
        import version_from_tag
    finally:
        sys.path.remove(str(SCRIPT.parent))
    committed = (ROOT / "packaging/windows/version_info.txt").read_text("utf-8")
    assert committed == version_from_tag.version_info(__version__)


@pytest.mark.parametrize("tag", ["0.2.0", "v0.2", "v0.2.0-rc1", "v01.2.3x", ""])
def test_rejects_bad_tag(tmp_path, tag):
    _fake_repo(tmp_path)
    before = (tmp_path / "pyproject.toml").read_text("utf-8")
    result = _run(tag, "--root", str(tmp_path))
    assert result.returncode != 0
    assert "vX.Y.Z" in result.stderr
    assert (tmp_path / "pyproject.toml").read_text("utf-8") == before
    assert not (tmp_path / "web/.env.production").exists()
