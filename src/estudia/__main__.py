"""Arranque de Estudia: servidor local, navegador y sincronización periódica.

Todo lo que tiene efectos va dentro de `main()`: la verificación con SymPy usa
un subproceso que vuelve a importar este módulo (spawn en Windows).
"""
import logging
import multiprocessing
import os
import signal
import socket
import sys
import threading
import time
import webbrowser
from functools import partial

import uvicorn

from estudia.config import data_dir
from estudia.db import connect as db_connect
from estudia.server.app import HOST, create_app, periodic_sync
from estudia.launcher import acquire_lock, headless, opens_browser, running_instance
from estudia.server.handler import ensure_installed, is_estudia
from estudia.reminders import run_reminders
from estudia.streams import redirect_missing_streams
from estudia.tray import run_tray, shutdown, tray_available

log = logging.getLogger(__name__)

STARTUP_WAIT_S = 15
INSTANCE_WAIT_S = 10  # espera a que la otra instancia, que ya tiene el lock, termine de arrancar
GRACEFUL_SHUTDOWN_S = 3  # uvicorn corta los pedidos y tareas de fondo que sigan después de esto
SERVER_JOIN_S = 5


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def uvicorn_config(app, port: int) -> uvicorn.Config:
    # Sin colores: sin consola no hay terminal que los entienda (ver estudia.streams).
    # Con un límite al apagar: una tarea de fondo larga (primera sincronización, Gemini)
    # no puede dejar a "Salir" esperando para siempre.
    return uvicorn.Config(app, host=HOST, port=port, log_level="warning", use_colors=False,
                          timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S)


def _open_when_ready(server: uvicorn.Server, url: str) -> None:
    deadline = time.monotonic() + STARTUP_WAIT_S
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.1)
    if server.started:
        webbrowser.open(url)


def _reuse(url: str) -> None:
    if opens_browser():
        webbrowser.open(url + "/")


def _wait_for_instance(port_file, lock_path) -> tuple[str | None, object | None]:
    """Espera a la instancia que tiene el lock: (su URL, None), o (None, el lock) si murió."""
    deadline = time.monotonic() + INSTANCE_WAIT_S
    while True:
        url = running_instance(port_file, is_estudia)
        if url:
            return url, None
        # La otra pudo cerrarse o caerse antes de escribir el puerto.
        lock = acquire_lock(lock_path)
        if lock is not None or time.monotonic() >= deadline:
            return None, lock
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


CHECK_WORKER_FLAG = "--check-worker"


def _worker_works() -> bool:
    """El worker de SymPy arranca (forkserver o spawn, también congelado) y corrige bien."""
    from estudia.generation.verify import check_math_tristate, grade_typed

    try:
        return (check_math_tristate("sqrt(2)", "√2") is True
                and grade_typed("sqrt(2)", "1.41") is True
                and grade_typed("sqrt(2)", "1.4") is False)
    except Exception:
        return False


def check_worker() -> int:
    """`Estudia.exe --check-worker`: 0 si el worker de SymPy anda, 1 si no (lo usa la prueba de CI)."""
    ok = _worker_works()
    print("worker de SymPy: " + ("bien" if ok else "no anda"))
    return 0 if ok else 1


def warn_if_worker_broken() -> None:
    """Al arrancar: un solo aviso en el log si el worker de SymPy no anda (sin detalles)."""
    if not _worker_works():
        log.warning("No arrancó el verificador de matemática (SymPy): las respuestas escritas "
                    "y la verificación de preguntas nuevas no van a andar.")


def main() -> None:
    # El login vuelve por `estudia://` o `moodlemobile://`: en Linux se registra solo.
    ensure_installed()
    port_file = data_dir() / "port"
    url = running_instance(port_file, is_estudia)
    if url:
        _reuse(url)
        return
    # Un doble clic rápido: la primera instancia tiene el lock pero aún no escribió el puerto.
    lock_path = data_dir() / "instance.lock"
    lock = acquire_lock(lock_path)
    if lock is None:
        url, lock = _wait_for_instance(port_file, lock_path)
        if url:
            _reuse(url)
            return
        if lock is None:
            _report_busy()
    port = free_port()
    port_file.write_text(str(port), "utf-8")
    # Una sola base para el servidor y los recordatorios.
    db_path = data_dir() / "estudia.db"
    app = create_app(db_path=db_path, port=port)
    server = uvicorn.Server(uvicorn_config(app, port))

    # uvicorn vuelve a lanzar SIGTERM al terminar; sin esto el proceso muere
    # antes del `finally` y queda el archivo del puerto.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    stop = threading.Event()
    threading.Thread(target=periodic_sync, args=(app.state.jobs, stop), daemon=True).start()
    threading.Thread(target=warn_if_worker_broken, name="worker-check", daemon=True).start()
    if not headless():
        threading.Thread(target=run_reminders, args=(stop, partial(db_connect, db_path)), daemon=True).start()
    page_url = f"http://{HOST}:{port}/"
    use_tray = not headless()
    if use_tray and not tray_available():
        log.warning("No se pudo cargar la bandeja del sistema; Estudia sigue sin ícono.")
        use_tray = False
    if opens_browser():
        threading.Thread(target=_open_when_ready, args=(server, page_url), daemon=True).start()
    server_thread = None
    exit_code = 0
    try:
        if use_tray:
            # pystray exige el hilo principal en Windows, así que el servidor va en otro.
            server_thread = threading.Thread(target=server.run, name="uvicorn")
            server_thread.start()
            try:
                run_tray(page_url, lambda: shutdown(server), lambda: not server_thread.is_alive())
            except Exception:
                # Sin ícono se puede seguir usando desde el navegador: no se corta el servidor.
                log.exception("La bandeja dejó de andar; Estudia sigue sin ícono.")
                while server_thread.is_alive():
                    server_thread.join(0.5)
            if not server.should_exit:  # el hilo murió solo: error de arranque o caída
                _fail(CRASH_TEXT)
        else:
            server.run()
    except SystemExit as exc:
        if not use_tray:
            raise
        exit_code = exc.code if isinstance(exc.code, int) else int(exc.code is not None)
    finally:
        shutdown(server)
        if server_thread is not None:
            server_thread.join(SERVER_JOIN_S)
        stop.set()
        # Un archivo viejo haría que el handler mande el token a otro proceso.
        port_file.unlink(missing_ok=True)
        lock.close()
    if use_tray:
        _hard_exit(exit_code)


def _hard_exit(code: int) -> None:
    """Termina ya, sin esperar hilos que sigan en un trabajo largo.

    Si no, el proceso queda vivo sin ícono ni servidor. SQLite aguanta un corte así.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    for handler in logging.getLogger().handlers:
        handler.flush()
    os._exit(code)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    # Estudia.exe no tiene consola: lo que se imprima (uvicorn, errores) va al log.
    redirect_missing_streams(data_dir() / "estudia.log")
    if CHECK_WORKER_FLAG in sys.argv[1:]:
        sys.exit(check_worker())
    main()
