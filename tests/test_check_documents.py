"""`Estudia --check-documents`: pdfium, python-pptx, Pillow y multipart andan (lo usa la prueba del .exe)."""
import os
import subprocess
import sys
from pathlib import Path

SPEC = Path(__file__).resolve().parents[1] / "packaging" / "windows" / "estudia.spec"
# Sin navegador ni bandeja por si el flag no se reconociera y arrancara la app entera.
NO_APP_ENV = {"ESTUDIA_HEADLESS": "1", "ESTUDIA_NO_BROWSER": "1", "BROWSER": "true"}


def test_check_documents_cli_exits_zero():
    env = {**os.environ, **NO_APP_ENV}
    proc = subprocess.run([sys.executable, "-m", "estudia", "--check-documents"],
                          capture_output=True, text=True, timeout=120, env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Lectura de documentos: bien" in proc.stdout


def test_check_documents_fails_when_pdfium_missing(monkeypatch, capsys):
    from estudia import __main__ as app_main
    from estudia.material import documents

    def broken(*args, **kwargs):
        raise OSError("no se encontró pdfium.dll")

    monkeypatch.setattr(documents, "inspect_document", broken)
    assert app_main.check_documents() == 1
    out = capsys.readouterr().out
    assert "bien" not in out
    assert "pdfium.dll" not in out  # solo el paso, sin detalles


def test_spec_collects_pdfium_and_pptx():
    text = SPEC.read_text("utf-8")
    assert 'collect_dynamic_libs("pypdfium2_raw")' in text
    assert 'collect_data_files("pptx")' in text
    assert "python_multipart" in text
