"""Arranque de Estudia: servidor local, navegador y sincronización periódica.

Todo lo que tiene efectos va dentro de `main()`: la verificación con SymPy usa
un subproceso que vuelve a importar este módulo (spawn en Windows).
"""
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
from estudia.server.handler import ensure_installed

STARTUP_WAIT_S = 15


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


def main() -> None:
    # El login vuelve por `estudia://` o `moodlemobile://`: en Linux se registra solo.
    ensure_installed()
    port = free_port()
    port_file = data_dir() / "port"
    port_file.write_text(str(port), "utf-8")
    app = create_app(port=port)
    server = uvicorn.Server(uvicorn_config(app, port))

    # uvicorn vuelve a lanzar SIGTERM al terminar; sin esto el proceso muere
    # antes del `finally` y queda el archivo del puerto.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    stop = threading.Event()
    threading.Thread(target=periodic_sync, args=(app.state.jobs, stop), daemon=True).start()
    threading.Thread(
        target=_open_when_ready, args=(server, f"http://{HOST}:{port}/"), daemon=True
    ).start()
    try:
        server.run()
    finally:
        stop.set()
        # Un archivo viejo haría que el handler mande el token a otro proceso.
        port_file.unlink(missing_ok=True)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
