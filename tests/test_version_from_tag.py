import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "version_from_tag.py"

sys.path.insert(0, str(SCRIPT.parent))
try:
    import version_from_tag
finally:
    sys.path.remove(str(SCRIPT.parent))


def _fake_repo(root: Path, version: str = "0.1.0", info: str | None = None) -> None:
    (root / "src" / "estudia").mkdir(parents=True)
    (root / "web").mkdir()
    (root / "packaging" / "windows").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "estudia"\nversion = "{version}"\n\n'
        '[tool.otra]\nversion = "9.9.9"\n', "utf-8")
    (root / "src" / "estudia" / "version.py").write_text(
        f'"""Versión de Estudia."""\n\n__version__ = "{version}"\n', "utf-8")
    (root / "packaging" / "windows" / "version_info.txt").write_text(
        version_from_tag.version_info(version) if info is None else info, "utf-8")


def _snapshot(root: Path) -> dict:
    return {p: p.read_text("utf-8") for p in root.rglob("*") if p.is_file()}


def _run(*args: str) -> subprocess.CompletedProcess:
    # Sin PYTHONIOENCODING, en Windows el script escribe en cp1252 al no tener consola.
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, encoding="utf-8", env=env)


def test_check_matching_tag_changes_nothing(tmp_path):
    _fake_repo(tmp_path, "0.2.13")
    before = _snapshot(tmp_path)
    result = _run("--check", "v0.2.13", "--root", str(tmp_path))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0.2.13"
    assert _snapshot(tmp_path) == before


def test_check_fails_when_tag_differs(tmp_path):
    _fake_repo(tmp_path, "0.2.13")
    before = _snapshot(tmp_path)
    result = _run("--check", "v0.2.14", "--root", str(tmp_path))
    assert result.returncode != 0
    assert "0.2.13" in result.stderr and "v0.2.14" in result.stderr
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("tag", ["0.2.0", "v0.2", "v0.2.0-rc1", "v01.2.3x", ""])
def test_check_rejects_bad_tag(tmp_path, tag):
    _fake_repo(tmp_path)
    before = _snapshot(tmp_path)
    result = _run("--check", tag, "--root", str(tmp_path))
    assert result.returncode != 0
    assert "vX.Y.Z" in result.stderr
    assert _snapshot(tmp_path) == before


def test_fails_when_pyproject_and_version_py_differ(tmp_path):
    _fake_repo(tmp_path, "0.2.0")
    (tmp_path / "src/estudia/version.py").write_text('__version__ = "0.3.0"\n', "utf-8")
    result = _run("--write-env", "--root", str(tmp_path))
    assert result.returncode != 0
    assert "0.2.0" in result.stderr and "0.3.0" in result.stderr
    assert not (tmp_path / "web/.env.production").exists()


def test_fails_when_version_info_is_stale(tmp_path):
    _fake_repo(tmp_path, "0.2.0", info="viejo")
    result = _run("--check", "v0.2.0", "--root", str(tmp_path))
    assert result.returncode != 0
    assert "version_info" in result.stderr


def test_write_env_uses_repo_version(tmp_path):
    _fake_repo(tmp_path, "0.2.13")
    result = _run("--write-env", "--root", str(tmp_path))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0.2.13"
    assert (tmp_path / "web/.env.production").read_text("utf-8") == "VITE_APP_VERSION=0.2.13\n"


def test_bump_rewrites_files(tmp_path):
    _fake_repo(tmp_path)
    result = _run("--bump", "v0.2.13", "--root", str(tmp_path))
    assert result.returncode == 0, result.stderr

    pyproject = (tmp_path / "pyproject.toml").read_text("utf-8")
    assert 'version = "0.2.13"' in pyproject
    assert 'version = "9.9.9"' in pyproject  # solo la de [project]
    assert '__version__ = "0.2.13"' in (tmp_path / "src/estudia/version.py").read_text("utf-8")
    assert not (tmp_path / "web/.env.production").exists()

    info = (tmp_path / "packaging/windows/version_info.txt").read_text("utf-8")
    assert "filevers=(0, 2, 13, 0)" in info
    assert "prodvers=(0, 2, 13, 0)" in info
    assert "StringStruct('ProductVersion', '0.2.13')" in info
    assert "StringStruct('FileVersion', '0.2.13')" in info
    assert _run("--check", "v0.2.13", "--root", str(tmp_path)).returncode == 0


def test_bump_rejects_bad_tag(tmp_path):
    _fake_repo(tmp_path)
    before = _snapshot(tmp_path)
    result = _run("--bump", "v0.2", "--root", str(tmp_path))
    assert result.returncode != 0
    assert _snapshot(tmp_path) == before


def test_repo_versions_agree():
    """version.py, pyproject y el version_info del repo dicen la misma versión."""
    from estudia.version import __version__

    assert version_from_tag.repo_version(ROOT) == __version__
