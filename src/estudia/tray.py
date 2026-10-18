"""Ícono de la bandeja del sistema: abrir Estudia en el navegador y salir."""
import multiprocessing
import threading
import webbrowser
from collections.abc import Callable
from importlib import resources

import uvicorn

OPEN_LABEL = "Abrir Estudia"
QUIT_LABEL = "Salir"
CHILD_JOIN_S = 5

_icon = None  # el ícono mientras la bandeja está corriendo


def notify(title: str, text: str) -> bool:
    """Muestra un aviso desde el ícono. False si no hay ícono o falla; nunca levanta."""
    icon = _icon
    if icon is None:
        return False
    try:
        icon.notify(text, title)  # pystray: notify(message, title)
        return True
    except Exception:
        return False


def tray_available() -> bool:
    """False si pystray no se puede importar (por ejemplo, Linux sin pantalla)."""
    try:
        import pystray  # noqa: F401
    except Exception:
        return False
    return True


def run_tray(
    url: str,
    on_quit: Callable[[], None],
    should_stop: Callable[[], bool] | None = None,
) -> None:
    """Muestra el ícono y bloquea hasta "Salir" o hasta que `should_stop()` dé True.

    En Windows hay que llamarla desde el hilo principal.
    """
    import pystray
    from PIL import Image

    with resources.as_file(resources.files("estudia") / "assets" / "icono-bandeja.png") as path:
        image = Image.open(path)
        image.load()

    def open_app(icon, item) -> None:
        webbrowser.open(url)

    done = threading.Event()

    def quit_app(icon, item) -> None:
        done.set()
        try:
            on_quit()
        finally:
            icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(OPEN_LABEL, open_app, default=True),
        pystray.MenuItem(QUIT_LABEL, quit_app),
    )
    def watch(icon) -> None:
        global _icon
        icon.visible = True
        # Recién con el ícono visible los avisos pueden salir por él.
        _icon = icon
        while should_stop is not None and not done.wait(0.5):
            if should_stop():
                icon.stop()
                return

    global _icon
    icon = pystray.Icon("Estudia", image, "Estudia", menu)
    try:
        icon.run(watch)
    finally:
        _icon = None
        done.set()


def shutdown(server: uvicorn.Server) -> None:
    """Pide al servidor que termine y mata los procesos hijos (verificación con SymPy, etc.)."""
    server.should_exit = True
    for child in multiprocessing.active_children():
        child.terminate()
        child.join(CHILD_JOIN_S)
