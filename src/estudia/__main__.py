"""Arranque de Estudia: servidor local, navegador y sincronización periódica.

Todo lo que tiene efectos va dentro de `main()`: la verificación con SymPy usa
un subproceso que vuelve a importar este módulo (spawn en Windows).
"""
import logging
import multiprocessing
import signal
import socket
import sys
import threading
import time
import webbrowser

import uvicorn

from estudia.config import data_dir
from estudia.server.app import HOST, create_app, periodic_sync
from estudia.launcher import acquire_lock, headless, running_instance
from estudia.server.handler import ensure_installed, is_estudia
from estudia.tray import run_tray, shutdown, tray_available

log = logging.getLogger(__name__)

STARTUP_WAIT_S = 15
INSTANCE_WAIT_S = 10  # espera a que la otra instancia, que ya tiene el lock, termine de arrancar


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def uvicorn_config(app, port: int) -> uvicorn.Config:
    return uvicorn.Config(app, host=HOST, port=port, log_level="warning")


def _open_when_ready(server: uvicorn.Server, url: str) -> None:
    deadline = time.monotonic() + STARTUP_WAIT_S
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.1)
    if server.started:
        webbrowser.open(url)


def _reuse(url: str) -> None:
    if not headless():
        webbrowser.open(url + "/")


def _wait_for_instance(port_file) -> str | None:
    deadline = time.monotonic() + INSTANCE_WAIT_S
    while True:
        url = running_instance(port_file, is_estudia)
        if url or time.monotonic() >= deadline:
            return url
        time.sleep(0.2)


BUSY_TEXT = (
    "Estudia ya se está abriendo o no responde. Cerrala desde la bandeja "
    "o el Administrador de tareas y volvé a abrirla."
)


def _message_box(text: str) -> None:
    import ctypes

    ctypes.windll.user32.MessageBoxW(None, text, "Estudia", 0x40)


CRASH_TEXT = "Estudia no pudo arrancar. Volvé a abrirla; si sigue pasando, avisanos en GitHub."


def _fail(text: str) -> None:
    """Avisa y sale con error."""
    print(text, file=sys.stderr)
    # Sin consola (la app de Windows no la tiene) el aviso se ve en un cuadro nativo.
    if sys.platform == "win32" and not headless():
        _message_box(text)
    sys.exit(1)


def _report_busy() -> None:
    """El lock lo tiene otra instancia que no contesta."""
    _fail(BUSY_TEXT)


def main() -> None:
    # El login vuelve por `estudia://` o `moodlemobile://`: en Linux se registra solo.
    ensure_installed()
    port_file = data_dir() / "port"
    url = running_instance(port_file, is_estudia)
    if url:
        _reuse(url)
        return
    # Un doble clic rápido: la primera instancia tiene el lock pero aún no escribió el puerto.
    lock = acquire_lock(data_dir() / "instance.lock")
    if lock is None:
        url = _wait_for_instance(port_file)
        if url:
            _reuse(url)
            return
        _report_busy()
    port = free_port()
    port_file.write_text(str(port), "utf-8")
    app = create_app(port=port)
    server = uvicorn.Server(uvicorn_config(app, port))

    # uvicorn vuelve a lanzar SIGTERM al terminar; sin esto el proceso muere
    # antes del `finally` y queda el archivo del puerto.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    stop = threading.Event()
    threading.Thread(target=periodic_sync, args=(app.state.jobs, stop), daemon=True).start()
    page_url = f"http://{HOST}:{port}/"
    use_tray = not headless()
    if use_tray and not tray_available():
        log.warning("No se pudo cargar la bandeja del sistema; Estudia sigue sin ícono.")
        use_tray = False
    if not headless():
        threading.Thread(target=_open_when_ready, args=(server, page_url), daemon=True).start()
    server_thread = None
    try:
        if use_tray:
            # pystray exige el hilo principal en Windows, así que el servidor va en otro.
            server_thread = threading.Thread(target=server.run, name="uvicorn")
            server_thread.start()
            run_tray(page_url, lambda: shutdown(server), lambda: not server_thread.is_alive())
            if not server.should_exit:  # el hilo murió solo: error de arranque o caída
                _fail(CRASH_TEXT)
        else:
            server.run()
    finally:
        shutdown(server)
        if server_thread is not None:
            server_thread.join()
        stop.set()
        # Un archivo viejo haría que el handler mande el token a otro proceso.
        port_file.unlink(missing_ok=True)
        lock.close()

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
