"""Ningún código baja de una URL de terceros: solo módulos conocidos hacen pedidos de red."""
import ast
import inspect
from pathlib import Path

import estudia
from estudia.moodle.client import MoodleClient

SRC = Path(estudia.__file__).parent
NETWORK_MODULES = {"httpx", "urllib", "requests", "socket", "http", "aiohttp"}
FORBIDDEN_IMPORTS = {"urllib.request", "urllib3", "requests", "aiohttp", "http.client"}
HTTP_OBJECTS = {"httpx", "_http", "http", "requests"}
HTTP_CALLS = {"get", "post", "stream", "put", "request", "Client", "AsyncClient", "head", "patch", "delete"}

# Los únicos módulos que hacen pedidos, cada uno con su motivo.
ALLOWED_REQUESTS = {
    "moodle/client.py": "al campus elegido (webservice y pluginfile, con el chequeo de sitio)",
    "moodle/auth.py": "login con usuario y contraseña, al campus elegido",
    "updates.py": "GitHub releases",
    "server/app.py": "solo crea el httpx.Client que se inyecta a las fábricas",
    "server/handler.py": "la app contra su propio servidor en 127.0.0.1 (esquema estudia://)",
}
# Módulos que importan o usan red sin pedir nada afuera.
ALLOWED_IMPORTS = {
    **ALLOWED_REQUESTS,
    "ai/gemini.py": "el SDK de Gemini; httpx solo para atrapar TransportError",
    "material/video.py": "yt-dlp, solo a YouTube; sin imports de red propios",
    "moodle/sync.py": "urllib.parse (no hace pedidos)",
    "db.py": "urllib.parse.quote para la URI de SQLite (no hace pedidos)",
    "__main__.py": "socket para buscar un puerto libre en 127.0.0.1",
}


def _modules():
    for path in sorted(SRC.rglob("*.py")):
        yield path.relative_to(SRC).as_posix(), ast.parse(path.read_text(encoding="utf-8"))


def _imports(tree) -> set[str]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module)
            found.update(f"{node.module}.{a.name}" for a in node.names)
    return found


def _makes_requests(tree) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr in HTTP_CALLS:
            base = f.value
            # `provider.requests.get(...)` es un diccionario de contadores: solo cuenta el Name `requests`.
            if isinstance(base, ast.Name) and base.id in HTTP_OBJECTS:
                return True
            if isinstance(base, ast.Attribute) and base.attr == "_http":
                return True
        if isinstance(f, ast.Attribute) and f.attr in ("urlopen", "create_connection", "connect") \
                and isinstance(f.value, ast.Name) and f.value.id in ("urllib", "socket", "request"):
            return True
    return False


def test_only_known_modules_make_http_requests():
    requesting = {name for name, tree in _modules() if _makes_requests(tree)}
    assert requesting, "el recorrido no encontró ni un pedido: el test pasaría en vacío"
    assert requesting <= set(ALLOWED_REQUESTS), f"módulos nuevos con pedidos HTTP: {sorted(requesting - set(ALLOWED_REQUESTS))}"


def test_no_module_imports_a_downloader():
    for name, tree in _modules():
        bad = {i for i in _imports(tree) if i in FORBIDDEN_IMPORTS or i.startswith("urllib.request")}
        assert not bad, f"{name} importa {sorted(bad)}"


def test_network_libraries_only_in_known_modules():
    using = set()
    for name, tree in _modules():
        if {i.split(".")[0] for i in _imports(tree)} & NETWORK_MODULES - {"http"} or \
                any(i.startswith(("http.client", "http.server")) for i in _imports(tree)):
            using.add(name)
        elif any(isinstance(n, ast.Import) and any(a.name == "http" for a in n.names) for n in ast.walk(tree)):
            using.add(name)
    # urllib.parse se usa para armar URLs, no para pedirlas; el chequeo de arriba ya prohíbe urllib.request.
    assert using <= set(ALLOWED_IMPORTS), f"módulos nuevos con librerías de red: {sorted(using - set(ALLOWED_IMPORTS))}"


def test_download_file_is_the_only_file_fetch_and_checks_site():
    # download_file no chequea el sitio por sí mismo: abre el archivo con _open_campus_file,
    # que es quien llama a is_campus_file (antes de poner el token y en cada redirección).
    assert "_open_campus_file" in inspect.getsource(MoodleClient.download_file)
    assert "is_campus_file" in inspect.getsource(MoodleClient._open_campus_file)
    # Y ningún otro método del cliente abre un stream: no hay otro camino para bajar un archivo.
    streamers = [n for n, f in inspect.getmembers(MoodleClient, inspect.isfunction)
                 if ".stream(" in inspect.getsource(f)]
    assert streamers == ["_open_campus_file"]
