# -*- mode: python ; coding: utf-8 -*-
# PyInstaller: `pyinstaller packaging/windows/estudia.spec` desde la raíz del repo.
# Deja `dist/Estudia/` con Estudia.exe (la app) y EstudiaHandler.exe (el handler de
# `estudia://` y `moodlemobile://`), los dos sin consola y con una sola copia de Python.
#
# Runtime de JavaScript para yt-dlp (YouTube exige resolver un desafío en JS)
# ---------------------------------------------------------------------------
# Elegido: node portable (solo node.exe), porque es el más chico y yt-dlp lo usa
# con `--js-runtimes node:<ruta>` (probado con yt-dlp 2026.08.19).
# Medido el 2026-10-17:
#   - deno.exe 2.9.7 (wheel win_amd64 del paquete `deno` de PyPI): 97.462.048 bytes
#     (41,5 MB comprimido con gzip).
#   - node.exe v24.22.0 LTS win-x64:                                 93.882.184 bytes
#     (35,1 MB comprimido con gzip).
# Fuente (la Tarea 6 lo baja en CI, verifica el checksum y lo deja en
# packaging/windows/runtime/node.exe; el binario no se commitea):
#   URL:    https://nodejs.org/dist/v24.22.0/win-x64/node.exe
#   SHA256: ba93f9a70bd86fce3ebbb39ec758501458453467aa92a34a0a3602d1e3fc2b9c
#   (de https://nodejs.org/dist/v24.22.0/SHASUMS256.txt, línea "win-x64/node.exe")
# En el ejecutable queda en `runtime/node.exe`; `estudia.config.bundled_js_runtime()`
# lo encuentra ahí y `estudia.material.video` lo usa en lugar de deno.
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).resolve().parents[1]  # noqa: F821 - SPECPATH lo define PyInstaller
SRC = ROOT / "src"
HERE = ROOT / "packaging" / "windows"
ICON = str(SRC / "estudia" / "assets" / "icono.ico")
VERSION_INFO = str(HERE / "version_info.txt")  # lo escribe scripts/version_from_tag.py
JS_RUNTIME = HERE / "runtime" / "node.exe"
WEB_DIST = ROOT / "web" / "dist"

for required, hint in ((JS_RUNTIME, "bajá node.exe (ver el comentario de arriba)"),
                       (WEB_DIST / "index.html", "corré `npm run build` en web/")):
    if not required.is_file():
        raise SystemExit(f"Falta {required}: {hint}")


def _no_tests(name: str) -> bool:
    return ".tests" not in name and not name.endswith(".conftest")


datas = [
    (str(WEB_DIST), "web/dist"),  # estudia.server.app.web_dist_dir()
    (str(SRC / "estudia" / "catalog" / "facultades.json"), "estudia/catalog"),
    (str(SRC / "estudia" / "assets"), "estudia/assets"),
    (str(JS_RUNTIME), "runtime"),  # estudia.config.bundled_js_runtime()
]
datas += collect_data_files("tzdata")
datas += collect_data_files("certifi")
# Los scripts del desafío de YouTube (el hook de yt-dlp también los agrega).
datas += collect_data_files("yt_dlp_ejs", includes=["**/*.js"])
# keyring busca sus backends por entry points: sin la metadata no encuentra el de Windows.
datas += copy_metadata("keyring")
datas += copy_metadata("google-genai")

hiddenimports = [
    # keyring: el backend de Windows (Credential Manager) se carga por entry point.
    "keyring.backends.Windows",
    "win32ctypes.core",
    # Solo existe si pywin32 está instalado; keyring lo usa si lo encuentra.
    "win32timezone",
    # pystray elige el backend en tiempo de ejecución.
    "pystray._win32",
    # yt-dlp carga los extractores de forma perezosa, por nombre.
    "yt_dlp.extractor.lazy_extractors",
    "tzdata",
]
# uvicorn elige loop, protocolo y lifespan por nombre (uvicorn.loops.auto,
# uvicorn.protocols.http.auto, uvicorn.lifespan.on, ...).
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("keyring.backends")
hiddenimports += collect_submodules("tzdata")
hiddenimports += collect_submodules("google.genai", filter=_no_tests)
hiddenimports += collect_submodules("sympy", filter=_no_tests)
hiddenimports += collect_submodules("yt_dlp")
# Los módulos de la app: el worker de SymPy se arranca con spawn y vuelve a importarlos.
hiddenimports += collect_submodules("estudia")

excludes = [
    "deno",  # el runtime incluido es node; el paquete de PyPI solo sirve en desarrollo
    "tkinter",
    "pytest",
]

common = dict(
    pathex=[str(SRC)],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)

app_a = Analysis([str(SRC / "estudia" / "__main__.py")], **common)  # noqa: F821
handler_a = Analysis([str(SRC / "estudia" / "server" / "handler.py")], **common)  # noqa: F821

app_pyz = PYZ(app_a.pure)  # noqa: F821
handler_pyz = PYZ(handler_a.pure)  # noqa: F821

exe_options = dict(
    exclude_binaries=True,
    console=False,
    icon=ICON,
    version=VERSION_INFO,
    upx=False,
)

app_exe = EXE(app_pyz, app_a.scripts, [], name="Estudia", **exe_options)  # noqa: F821
handler_exe = EXE(handler_pyz, handler_a.scripts, [], name="EstudiaHandler",  # noqa: F821
                  **exe_options)

COLLECT(  # noqa: F821
    app_exe, app_a.binaries, app_a.datas,
    handler_exe, handler_a.binaries, handler_a.datas,
    name="Estudia",
    upx=False,
)
