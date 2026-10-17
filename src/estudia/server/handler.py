"""Entrada de los esquemas `estudia://` y `moodlemobile://`: reenvía la URL al servidor local.

Uso: `python -m estudia.server.handler <url>` (lo invoca el sistema al volver
del login en el navegador) o `python -m estudia.server.handler --install` para
registrar los esquemas (en Windows, `--install` / `--uninstall` escriben o borran
las claves de HKCU; `--data-dir` imprime la carpeta de datos sin crearla). Moodle suele forzar `moodlemobile://`
(`tool_mobile/forcedurlscheme`), por eso se registran los dos.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import httpx

from estudia import winreg_schemes
from estudia.config import data_dir, data_path
from estudia.server.app import HOST

DESKTOP_FILE = "estudia-handler.desktop"
SCHEME_MIMES = ("x-scheme-handler/estudia", "x-scheme-handler/moodlemobile")


def _exec_line(python: str) -> str:
    return f'Exec="{python}" -m estudia.server.handler %u'


def desktop_entry(python: str) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Estudia (inicio de sesión)\n"
        f"{_exec_line(python)}\n"
        "NoDisplay=true\n"
        "Terminal=false\n"
        f"MimeType={''.join(f'{m};' for m in SCHEME_MIMES)}\n"
    )


def _desktop_path() -> Path:
    return Path.home() / ".local" / "share" / "applications" / DESKTOP_FILE


def _register() -> Path:
    path = _desktop_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(desktop_entry(sys.executable), "utf-8")
    try:
        for mime in SCHEME_MIMES:
            subprocess.run(["xdg-mime", "default", DESKTOP_FILE, mime], check=True)
    except BaseException:
        # Sin el archivo, el próximo arranque lo vuelve a intentar.
        path.unlink(missing_ok=True)
        raise
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(path.parent)], check=False)
    return path


def handler_command() -> str:
    """Comando que Windows ejecuta al volver del login (`%1` es la URL)."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).with_name("EstudiaHandler.exe")
        return f'"{exe}" "%1"'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        pythonw = Path(sys.executable)
    return f'"{pythonw}" -m estudia.server.handler "%1"'


def install() -> int:
    """Registra el handler de `estudia://` y `moodlemobile://` para el usuario actual."""
    if sys.platform == "win32":
        winreg_schemes.register(winreg_schemes.RealReg(), handler_command())
        print("Listo: estudia:// y moodlemobile:// quedan a cargo de Estudia")
        return 0
    path = _register()
    print(f"Listo: {', '.join(SCHEME_MIMES)} quedan a cargo de {path}")
    return 0


def uninstall() -> int:
    """Deshace `install()`; en Windows devuelve `moodlemobile://` a la app que lo tenía."""
    if sys.platform == "win32":
        winreg_schemes.unregister(winreg_schemes.RealReg(), handler_command())
    else:
        _desktop_path().unlink(missing_ok=True)
    return 0


def _is_current(path: Path) -> bool:
    """El .desktop sirve si declara los dos esquemas y apunta a este intérprete."""
    try:
        lines = path.read_text("utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return False
    mimes = set()
    for line in lines:
        if line.startswith("MimeType="):
            mimes.update(m.strip() for m in line.removeprefix("MimeType=").split(";"))
    return set(SCHEME_MIMES) <= mimes and _exec_line(sys.executable) in lines


def ensure_installed() -> None:
    """Al arrancar: registra el handler si falta o quedó viejo. En Linux, sin xdg-mime no hace nada."""
    if sys.platform == "win32":
        reg, cmd = winreg_schemes.RealReg(), handler_command()
        try:
            # Al arrancar no se le saca moodlemobile:// a otra app; eso lo hace --install.
            if not winreg_schemes.is_registered(reg, cmd, takeover_moodle=False):
                winreg_schemes.register(reg, cmd, takeover_moodle=False)
        except OSError:
            pass  # se puede pegar el enlace a mano en la pantalla de inicio de sesión
        return
    if not sys.platform.startswith("linux") or _is_current(_desktop_path()):
        return
    if shutil.which("xdg-mime") is None:
        return
    try:
        _register()
    except (OSError, subprocess.SubprocessError):
        pass  # se puede pegar el enlace a mano en la pantalla de inicio de sesión


def is_estudia(base: str) -> bool:
    """El puerto puede haber quedado de una corrida anterior: se confirma antes de mandar el token."""
    try:
        datos = httpx.get(f"{base}/api/state", timeout=5).json()
    except (httpx.HTTPError, ValueError):
        return False
    return isinstance(datos, dict) and "step" in datos


_is_estudia = is_estudia


def forward(url: str) -> int:
    try:
        port = int((data_dir() / "port").read_text("utf-8").strip())
    except (OSError, ValueError):
        print("No se encontró Estudia. ¿Está abierta?", file=sys.stderr)
        return 1
    base = f"http://{HOST}:{port}"
    if not is_estudia(base):
        print("No se pudo avisar a Estudia. ¿Está abierta?", file=sys.stderr)
        return 1
    try:
        respuesta = httpx.post(f"{base}/api/login/callback", json={"url": url}, timeout=15)
    except httpx.HTTPError:
        # Sin la URL en el mensaje: lleva el token.
        print("No se pudo avisar a Estudia. ¿Está abierta?", file=sys.stderr)
        return 1
    if respuesta.status_code != 200:
        print(f"Estudia rechazó el inicio de sesión (HTTP {respuesta.status_code}).", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str]) -> int:
    if argv == ["--install"]:
        return install()
    if argv == ["--uninstall"]:
        return uninstall()
    if argv == ["--data-dir"]:
        print(data_path())
        return 0
    if len(argv) != 1:
        print("uso: python -m estudia.server.handler <url> | --install | --uninstall | --data-dir",
              file=sys.stderr)
        return 2
    return forward(argv[0])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
