import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

import estudia.__main__ as entry
from estudia.launcher import acquire_lock, headless, opens_browser, running_instance


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
    # Al morir el dueño queda libre. En Windows el sistema lo suelta un poco después.
    deadline = time.monotonic() + 10
    while (lock := acquire_lock(path)) is None and time.monotonic() < deadline:
        time.sleep(0.1)
    assert lock is not None
    lock.close()


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(entry, "ensure_installed", lambda: None)
    monkeypatch.setattr(entry, "data_dir", lambda: tmp_path)
    monkeypatch.delenv("ESTUDIA_HEADLESS", raising=False)
    monkeypatch.delenv("ESTUDIA_NO_BROWSER", raising=False)

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


BUSY_TEXT = (
    "Estudia ya se está abriendo o no responde. Cerrala desde la bandeja "
    "o el Administrador de tareas y volvé a abrirla."
)


@pytest.fixture
def lock_held(isolated, monkeypatch):
    clock = iter(range(0, 1000, 4))
    boxes = []
    monkeypatch.setattr(entry, "running_instance", lambda *_: None)
    monkeypatch.setattr(entry, "acquire_lock", lambda path: None)
    monkeypatch.setattr(entry.time, "sleep", lambda s: None)
    monkeypatch.setattr(entry.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(entry, "_message_box", boxes.append)
    return boxes


def test_main_gives_up_after_wait(lock_held, monkeypatch, capsys):
    monkeypatch.setattr(entry.sys, "platform", "linux")
    opened = []
    monkeypatch.setattr(entry.webbrowser, "open", opened.append)
    with pytest.raises(SystemExit) as exc:
        entry.main()
    assert exc.value.code == 1
    assert opened == []
    assert BUSY_TEXT in capsys.readouterr().err
    assert lock_held == []  # fuera de Windows no hay cuadro de mensaje


def test_main_busy_shows_message_box_on_windows(lock_held, monkeypatch):
    monkeypatch.setattr(entry.sys, "platform", "win32")
    with pytest.raises(SystemExit):
        entry.main()
    assert lock_held == [BUSY_TEXT]


def test_main_busy_headless_has_no_message_box(lock_held, monkeypatch):
    monkeypatch.setattr(entry.sys, "platform", "win32")
    monkeypatch.setenv("ESTUDIA_HEADLESS", "1")
    with pytest.raises(SystemExit):
        entry.main()
    assert lock_held == []


class FakeServer:
    def __init__(self, config=None):
        self.started = False
        self.should_exit = False
        self.run_thread = None

    def run(self):
        self.run_thread = threading.current_thread()
        self.started = True
        while not self.should_exit:
            time.sleep(0.01)


class FakeLock:
    closed = False

    def close(self):
        self.closed = True


@pytest.fixture
def serving(tmp_path, monkeypatch):
    lock = FakeLock()
    server = FakeServer()
    monkeypatch.setattr(entry, "ensure_installed", lambda: None)
    monkeypatch.setattr(entry, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(entry, "running_instance", lambda *_: None)
    monkeypatch.setattr(entry, "acquire_lock", lambda path: lock)
    monkeypatch.setattr(entry, "periodic_sync", lambda jobs, stop: None)
    reminders = []
    monkeypatch.setattr(entry, "run_reminders", lambda stop, connect: reminders.append(connect))
    apps = []

    def fake_create_app(db_path, port):
        apps.append(db_path)
        return SimpleNamespace(state=SimpleNamespace(jobs=None))

    monkeypatch.setattr(entry, "create_app", fake_create_app)
    monkeypatch.setattr(entry.uvicorn, "Server", lambda config: server)
    monkeypatch.setattr(entry.signal, "signal", lambda *a: None)
    monkeypatch.setattr(entry.webbrowser, "open", lambda url: None)
    monkeypatch.delenv("ESTUDIA_HEADLESS", raising=False)
    monkeypatch.delenv("ESTUDIA_NO_BROWSER", raising=False)
    exits = []
    monkeypatch.setattr(entry.os, "_exit", exits.append)
    return SimpleNamespace(lock=lock, server=server, port_file=tmp_path / "port", exits=exits,
                           reminders=reminders, apps=apps, data_dir=tmp_path)


def test_main_runs_server_in_thread_and_tray_in_main(serving, monkeypatch):
    seen = {}

    def fake_run_tray(url, on_quit):
        seen["tray_thread"] = threading.current_thread()
        seen["url"] = url
        assert serving.port_file.exists()
        on_quit()

    monkeypatch.setattr(entry, "tray_available", lambda: True)
    monkeypatch.setattr(entry, "run_tray", lambda url, on_quit, stop: fake_run_tray(url, on_quit))
    entry.main()
    assert seen["tray_thread"] is threading.main_thread()
    assert seen["url"].startswith("http://127.0.0.1:")
    assert serving.server.run_thread is not threading.main_thread()
    assert serving.server.should_exit
    assert not serving.port_file.exists()
    assert serving.lock.closed
    assert serving.exits == [0]  # con bandeja se sale sin esperar hilos colgados


def test_main_keeps_serving_when_tray_crashes(serving, monkeypatch, caplog):
    def boom(url, on_quit, stop):
        raise RuntimeError("tray")

    monkeypatch.setattr(entry, "tray_available", lambda: True)
    monkeypatch.setattr(entry, "run_tray", boom)
    timer = threading.Timer(0.3, lambda: setattr(serving.server, "should_exit", True))
    timer.start()
    with caplog.at_level("ERROR"):
        entry.main()
    assert "La bandeja dejó de andar" in caplog.text
    assert serving.server.run_thread is not threading.main_thread()
    assert not timer.is_alive()  # el servidor siguió hasta que se le pidió terminar
    assert not serving.port_file.exists()
    assert serving.lock.closed
    assert serving.exits == [0]


def test_main_without_pystray_runs_server_in_main_thread(serving, monkeypatch, caplog):
    monkeypatch.setattr(entry, "tray_available", lambda: False)
    monkeypatch.setattr(entry, "run_tray", lambda *a: pytest.fail("sin bandeja"))
    threading.Timer(0.3, lambda: setattr(serving.server, "should_exit", True)).start()
    with caplog.at_level("WARNING"):
        entry.main()
    assert serving.server.run_thread is threading.main_thread()
    assert "bandeja" in caplog.text
    assert not serving.port_file.exists()
    assert serving.lock.closed


def test_main_headless_has_no_tray(serving, monkeypatch):
    monkeypatch.setenv("ESTUDIA_HEADLESS", "1")
    monkeypatch.setattr(entry, "run_tray", lambda *a: pytest.fail("sin bandeja"))
    threading.Timer(0.3, lambda: setattr(serving.server, "should_exit", True)).start()
    entry.main()
    assert serving.server.run_thread is threading.main_thread()
    assert serving.lock.closed
    assert serving.exits == []  # sin bandeja, sale normal


def test_main_server_thread_dies_stops_tray_and_cleans_up(serving, monkeypatch, capsys):
    boxes = []
    serving.server.run = lambda: None  # arranque que falla: el hilo termina enseguida

    def fake_run_tray(url, on_quit, should_stop):
        for _ in range(100):
            if should_stop():
                return  # equivale a icon.stop()
            time.sleep(0.01)
        pytest.fail("el vigilante no vio morir al servidor")

    monkeypatch.setattr(entry, "tray_available", lambda: True)
    monkeypatch.setattr(entry, "run_tray", fake_run_tray)
    monkeypatch.setattr(entry.sys, "platform", "win32")
    monkeypatch.setattr(entry, "_message_box", boxes.append)
    entry.main()
    assert serving.exits == [1]
    assert boxes == [entry.CRASH_TEXT]
    assert entry.CRASH_TEXT in capsys.readouterr().err
    assert not serving.port_file.exists()
    assert serving.lock.closed


def test_quit_does_not_hang_on_endless_background_task(tmp_path, monkeypatch):
    """Una tarea de fondo que no termina no deja a "Salir" esperando para siempre."""
    from fastapi import BackgroundTasks, FastAPI
    import httpx

    release = threading.Event()
    started = threading.Event()

    def endless() -> None:
        started.set()
        release.wait(60)

    app = FastAPI()
    app.state.jobs = None

    @app.post("/hang")
    def hang(background: BackgroundTasks) -> dict:
        background.add_task(endless)
        return {}

    lock = FakeLock()
    exits = []
    monkeypatch.setattr(entry, "ensure_installed", lambda: None)
    monkeypatch.setattr(entry, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(entry, "running_instance", lambda *_: None)
    monkeypatch.setattr(entry, "acquire_lock", lambda path: lock)
    monkeypatch.setattr(entry, "create_app", lambda db_path, port: app)
    monkeypatch.setattr(entry, "periodic_sync", lambda jobs, stop: None)
    monkeypatch.setattr(entry.signal, "signal", lambda *a: None)
    monkeypatch.setattr(entry.webbrowser, "open", lambda url: None)
    monkeypatch.setattr(entry.os, "_exit", exits.append)
    monkeypatch.delenv("ESTUDIA_HEADLESS", raising=False)
    monkeypatch.setattr(entry, "tray_available", lambda: True)

    def fake_run_tray(url, on_quit, should_stop):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                httpx.post(url + "hang", timeout=5)
                break
            except httpx.HTTPError:
                time.sleep(0.05)
        assert started.wait(5)
        on_quit()  # "Salir" mientras la tarea sigue

    monkeypatch.setattr(entry, "run_tray", fake_run_tray)
    begin = time.monotonic()
    try:
        entry.main()
    finally:
        release.set()
    elapsed = time.monotonic() - begin
    assert elapsed < entry.GRACEFUL_SHUTDOWN_S + entry.SERVER_JOIN_S + 3
    assert exits == [0]
    assert lock.closed
    assert not (tmp_path / "port").exists()


def test_main_takes_the_lock_when_the_other_instance_dies(serving, monkeypatch):
    locks = iter([None, None, serving.lock])
    monkeypatch.setattr(entry, "acquire_lock", lambda path: next(locks))
    monkeypatch.setattr(entry.time, "sleep", lambda s: None)
    monkeypatch.setenv("ESTUDIA_HEADLESS", "1")
    threading.Timer(0.3, lambda: setattr(serving.server, "should_exit", True)).start()
    entry.main()
    assert serving.server.started  # arrancó como instancia principal
    assert serving.lock.closed


def test_opens_browser_env(monkeypatch):
    monkeypatch.delenv("ESTUDIA_HEADLESS", raising=False)
    monkeypatch.delenv("ESTUDIA_NO_BROWSER", raising=False)
    assert opens_browser()
    monkeypatch.setenv("ESTUDIA_NO_BROWSER", "1")
    assert not opens_browser()
    monkeypatch.delenv("ESTUDIA_NO_BROWSER")
    monkeypatch.setenv("ESTUDIA_HEADLESS", "1")
    assert not opens_browser()


def test_main_no_browser_keeps_tray(serving, monkeypatch):
    opened, trays = [], []
    monkeypatch.setenv("ESTUDIA_NO_BROWSER", "1")
    monkeypatch.setattr(entry.webbrowser, "open", opened.append)
    monkeypatch.setattr(entry, "tray_available", lambda: True)

    def fake_run_tray(url, on_quit, should_stop):
        trays.append(url)
        on_quit()

    monkeypatch.setattr(entry, "run_tray", fake_run_tray)
    entry.main()
    assert len(trays) == 1
    assert opened == []


def test_main_reuse_no_browser_does_not_open_browser(isolated, monkeypatch):
    opened = []
    monkeypatch.setenv("ESTUDIA_NO_BROWSER", "1")
    monkeypatch.setattr(entry, "running_instance", lambda *_: "http://127.0.0.1:4321")
    monkeypatch.setattr(entry.webbrowser, "open", opened.append)
    entry.main()
    assert opened == []


def test_reminders_use_the_same_database_as_the_app(serving, monkeypatch):
    """Los recordatorios abren la base que usa el servidor, nunca la de por defecto."""
    monkeypatch.setattr(entry, "tray_available", lambda: False)
    serving.server.should_exit = True
    entry.main()
    for _ in range(100):
        if serving.reminders:
            break
        time.sleep(0.01)
    assert serving.apps == [serving.data_dir / "estudia.db"]
    assert len(serving.reminders) == 1
    conn = serving.reminders[0]()
    try:
        file = conn.execute("PRAGMA database_list").fetchone()[2]
    finally:
        conn.close()
    assert file == str(serving.data_dir / "estudia.db")
