import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "render_release_notes.py"
NOTES = ROOT / "packaging" / "windows" / "release-notes.md"
FACULTADES = ROOT / "docs" / "facultades.md"
WORKFLOW = ROOT / ".github" / "workflows" / "release-windows.yml"
HASH = "ab12" * 16

sys.path.insert(0, str(SCRIPT.parent))
try:
    import render_release_notes
finally:
    sys.path.remove(str(SCRIPT.parent))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_render_fills_version_and_hash():
    out = render_release_notes.render(_read(NOTES), "0.5.0", HASH)
    assert "EstudiaSetup-0.5.0.exe" in out
    assert HASH in out
    assert "{{" not in out
    assert out == render_release_notes.render(_read(NOTES), "0.5.0", HASH)


@pytest.mark.parametrize(
    ("template", "version", "sha"),
    [
        ("{{VERSION}} {{SHA256}}", "0.5.0", HASH.upper()),
        ("{{VERSION}} {{SHA256}}", "0.5.0", HASH[:63]),
        ("{{VERSION}} {{SHA256}}", "v0.5.0", HASH),
        ("{{VERSION}}", "0.5.0", HASH),
        ("{{SHA256}}", "0.5.0", HASH),
        ("{{VERSION}} {{SHA256}} {{OTRO}}", "0.5.0", HASH),
    ],
)
def test_render_rejects_bad_input(template, version, sha):
    with pytest.raises(ValueError):
        render_release_notes.render(template, version, sha)


def test_cli_writes_out_file(tmp_path):
    out = tmp_path / "rendered.md"
    subprocess.run(
        [sys.executable, str(SCRIPT), "--template", str(NOTES), "--version", "0.5.0",
         "--sha256", HASH, "--out", str(out)],
        check=True,
    )
    assert HASH in out.read_text(encoding="utf-8")
    bad = subprocess.run(
        [sys.executable, str(SCRIPT), "--template", str(NOTES), "--version", "v1",
         "--sha256", HASH, "--out", str(tmp_path / "x.md")],
        capture_output=True,
    )
    assert bad.returncode == 1


@pytest.mark.parametrize("path", [NOTES, FACULTADES])
def test_notes_and_doc_show_working_faculties(path):
    text = _read(path)
    for needle in ("Facultades que funcionan", "**Funciona**", "Todavía no",
                   "issues/new?template=facultad-no-funciona.yml"):
        assert needle in text


def test_notes_open_with_the_faculties_table():
    assert _read(NOTES).startswith("### Facultades que funcionan")


@pytest.mark.parametrize("path", [NOTES, FACULTADES])
def test_smartscreen_is_step_by_step_without_gif(path):
    text = _read(path)
    assert "1. En ese cartel, tocá **Más información**." in text
    for banned in (".gif", "![", "<img"):
        assert banned not in text


def test_workflow_publishes_sha256_and_keeps_signpath_off():
    text = _read(WORKFLOW)
    for needle in ("render_release_notes.py", "Get-FileHash",
                   "--notes-file release-notes.rendered.md", "gh release edit"):
        assert needle in text
    block = text.split("- name: Firmar con SignPath", 1)[1].split("\n      - ", 1)[0]
    assert "if: ${{ false }}" in block
    assert re.search(r"uses: signpath/github-action-submit-signing-request@[0-9a-f]{40}\b", block)


def test_pull_requests_that_touch_the_script_run_the_workflow():
    # Un cambio en el script de las notas tiene que probar el armado antes de un tag.
    text = _read(WORKFLOW)
    pr_paths = text.split("pull_request:", 1)[1].split("workflow_dispatch:", 1)[0]
    assert '"scripts/render_release_notes.py"' in pr_paths
