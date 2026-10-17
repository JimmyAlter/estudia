import subprocess
import sys

import pytest

import estudia.__main__ as entry
from estudia.launcher import acquire_lock, headless, running_instance


def test_no_port_file_returns_none(tmp_path):
    assert running_instance(tmp_path / "port", lambda base: True) is None


def test_stale_port_returns_none(tmp_path):
    port_file = tmp_path / "port"
    port_file.write_text("4321", "utf-8")
    assert running_instance(port_file, lambda base: False) is None


def test_live_instance_returns_url(tmp_path):
    port_file = tmp_path / "port"
    port_file.write_text("4321", "utf-8")
    assert running_instance(port_file, lambda base: True) == "http://127.0.0.1:4321"


def test_garbage_port_file_returns_none(tmp_path):
    port_file = tmp_path / "port"
    port_file.write_text("no es un puerto", "utf-8")
    assert running_instance(port_file, lambda base: True) is None


def test_headless_env(monkeypatch):
    monkeypatch.delenv("ESTUDIA_HEADLESS", raising=False)
    assert headless() is False
    monkeypatch.setenv("ESTUDIA_HEADLESS", "1")
    assert headless() is True
    monkeypatch.setenv("ESTUDIA_HEADLESS", "0")
    assert headless() is False


_HOLDER = (
    "import sys, time\n"
    "from pathlib import Path\n"
    "from estudia.launcher import acquire_lock\n"
    "lock = acquire_lock(Path(sys.argv[1]))\n"
    "print('ok' if lock else 'busy', flush=True)\n"
    "time.sleep(30)\n"
)


def test_second_lock_fails(tmp_path):
    # El lock es por proceso: dentro del mismo no bloquea, así que lo toma un subproceso.
    path = tmp_path / "instance.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(path)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert holder.stdout.readline().strip() == "ok"
        assert acquire_lock(path) is None
    finally:
        holder.kill()
        holder.wait()
    lock = acquire_lock(path)  # al morir el dueño queda libre
    assert lock is not None
    lock.close()


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(entry, "ensure_installed", lambda: None)
    monkeypatch.setattr(entry, "data_dir", lambda: tmp_path)
    monkeypatch.delenv("ESTUDIA_HEADLESS", raising=False)

    def no_server(*a, **k):
        raise AssertionError("no debía crear el servidor")

    monkeypatch.setattr(entry, "create_app", no_server)
    return tmp_path


def test_main_reuses_instance(isolated, monkeypatch):
    opened = []
    monkeypatch.setattr(entry, "running_instance", lambda *_: "http://127.0.0.1:4321")
    monkeypatch.setattr(entry.webbrowser, "open", opened.append)
    entry.main()
    assert opened == ["http://127.0.0.1:4321/"]
    assert not (isolated / "instance.lock").exists()


def test_main_reuse_headless_does_not_open_browser(isolated, monkeypatch):
    opened = []
    monkeypatch.setenv("ESTUDIA_HEADLESS", "1")
    monkeypatch.setattr(entry, "running_instance", lambda *_: "http://127.0.0.1:4321")
    monkeypatch.setattr(entry.webbrowser, "open", opened.append)
    entry.main()
    assert opened == []


def test_main_waits_for_instance_holding_lock(isolated, monkeypatch):
    opened = []
    answers = iter([None, None, "http://127.0.0.1:4321"])
    monkeypatch.setattr(entry, "running_instance", lambda *_: next(answers))
    monkeypatch.setattr(entry, "acquire_lock", lambda path: None)
    monkeypatch.setattr(entry.time, "sleep", lambda s: None)
    monkeypatch.setattr(entry.webbrowser, "open", opened.append)
    entry.main()
    assert opened == ["http://127.0.0.1:4321/"]


def test_main_gives_up_after_wait(isolated, monkeypatch):
    opened = []
    clock = iter(range(0, 1000, 4))
    monkeypatch.setattr(entry, "running_instance", lambda *_: None)
    monkeypatch.setattr(entry, "acquire_lock", lambda path: None)
    monkeypatch.setattr(entry.time, "sleep", lambda s: None)
    monkeypatch.setattr(entry.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(entry.webbrowser, "open", opened.append)
    entry.main()
    assert opened == []
