"""`Estudia --check-video-tools`: yt-dlp y su runtime de JavaScript andan, sin red."""
import os
import subprocess
import sys

NO_APP_ENV = {"ESTUDIA_HEADLESS": "1", "ESTUDIA_NO_BROWSER": "1", "BROWSER": "true"}


def test_check_video_tools_cli_exits_zero():
    env = {**os.environ, **NO_APP_ENV}
    proc = subprocess.run([sys.executable, "-m", "estudia", "--check-video-tools"],
                          capture_output=True, text=True, timeout=120, env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Herramientas de video: bien" in proc.stdout


def test_check_video_tools_fails_when_bundled_runtime_is_missing(monkeypatch, tmp_path, capsys):
    from estudia import __main__ as app_main
    from estudia import config

    missing = tmp_path / "runtime" / "node.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(config, "bundled_js_runtime_path", lambda: missing)
    assert app_main.check_video_tools() == 1
    out = capsys.readouterr().out
    assert "bien" not in out
    assert str(tmp_path) not in out  # solo el paso, sin rutas


def test_check_video_tools_ok_in_process():
    from estudia import __main__ as app_main

    assert app_main.check_video_tools() == 0
