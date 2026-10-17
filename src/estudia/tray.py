"""Ícono de la bandeja del sistema: abrir Estudia en el navegador y salir."""
import multiprocessing
import webbrowser
from collections.abc import Callable
from importlib import resources

import uvicorn

OPEN_LABEL = "Abrir Estudia"
QUIT_LABEL = "Salir"
CHILD_JOIN_S = 5


def tray_available() -> bool:
    """False si pystray no se puede importar (por ejemplo, Linux sin pantalla)."""
    try:
        import pystray  # noqa: F401
    except Exception:
        return False
    return True


def run_tray(url: str, on_quit: Callable[[], None]) -> None:
    """Muestra el ícono y bloquea hasta "Salir". En Windows hay que llamarla desde el hilo principal."""
    import pystray
    from PIL import Image

    with resources.as_file(resources.files("estudia") / "assets" / "icono.png") as path:
        image = Image.open(path)
        image.load()

    def open_app(icon, item) -> None:
        webbrowser.open(url)

    def quit_app(icon, item) -> None:
        try:
            on_quit()
        finally:
            icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(OPEN_LABEL, open_app, default=True),
        pystray.MenuItem(QUIT_LABEL, quit_app),
    )
    pystray.Icon("Estudia", image, "Estudia", menu).run()


def shutdown(server: uvicorn.Server) -> None:
    """Pide al servidor que termine y mata los procesos hijos (verificación con SymPy, etc.)."""
    server.should_exit = True
    for child in multiprocessing.active_children():
        child.terminate()
        child.join(CHILD_JOIN_S)
