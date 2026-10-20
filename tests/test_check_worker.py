"""`Estudia --check-worker`: el worker de SymPy arranca y corrige (lo usa la prueba del .exe)."""
import logging
import os
import subprocess
import sys


def test_check_worker_cli_exits_zero():
    # Sin navegador ni bandeja por si el flag no se reconociera y arrancara la app entera.
    env = {**os.environ, "ESTUDIA_HEADLESS": "1", "ESTUDIA_NO_BROWSER": "1"}
    proc = subprocess.run([sys.executable, "-m", "estudia", "--check-worker"],
                          capture_output=True, text=True, timeout=60, env=env)
    assert proc.returncode == 0, proc.stderr
    assert "worker" in proc.stdout.lower()


def test_check_worker_fails_when_the_worker_does_not_answer(monkeypatch):
    from estudia import __main__ as app_main
    from estudia.generation import verify

    monkeypatch.setattr(verify, "_start_worker", lambda: False)
    verify._stop_worker()
    assert app_main.check_worker() == 1


def test_check_worker_ok_in_process():
    from estudia import __main__ as app_main

    assert app_main.check_worker() == 0


def test_startup_warns_once_without_details_if_the_worker_cannot_start(monkeypatch, caplog):
    from estudia import __main__ as app_main
    from estudia.generation import verify

    monkeypatch.setattr(verify, "_start_worker", lambda: False)
    verify._stop_worker()
    with caplog.at_level(logging.WARNING, logger="estudia.__main__"):
        app_main.warn_if_worker_broken()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "SymPy" in warnings[0].getMessage()


def test_startup_is_quiet_when_the_worker_works(caplog):
    from estudia import __main__ as app_main

    with caplog.at_level(logging.WARNING, logger="estudia.__main__"):
        app_main.warn_if_worker_broken()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_check_worker_runs_variants_mode(monkeypatch):
    from estudia import __main__ as app_main
    from estudia.generation import verify

    assert app_main._worker_works() is True
    monkeypatch.setattr(verify, "compute_variant_values", lambda payload: None)
    assert app_main._worker_works() is False
