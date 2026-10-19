"""Cliente de solo lectura para el web service REST de Moodle."""

import json
import logging
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any
from urllib.parse import unquote, urlsplit

import httpx

PLUGINFILE_PATH = "/webservice/pluginfile.php/"
MAX_REDIRECTS = 3
# Tope de `fetch_file` (el HTML de una page); los archivos grandes van por `download_file`.
FETCH_FILE_MAX = 5 * 1024 * 1024
# Un error de Moodle en JSON es chico; un JSON más grande se trata como archivo.
_ERROR_BODY_MAX = 64 * 1024
_CHUNK = 64 * 1024

_TOKEN_RE = re.compile(r"(?i)\b(ws)?token=[^&\s\"'#]+")


def scrub(text: str) -> str:
    """Tapa el valor de `token=` y `wstoken=` en cualquier texto (URLs, mensajes, logs)."""
    return _TOKEN_RE.sub(r"\1token=***", text)


class _ScrubFilter(logging.Filter):
    """Si alguien sube el logger de httpx a INFO, la URL sale sin el token."""

    def filter(self, record: logging.LogRecord) -> bool:
        mensaje = record.getMessage()
        limpio = scrub(mensaje)
        if limpio != mensaje:
            record.msg, record.args = limpio, None
        return True


# El INFO de httpx imprime la URL con la query (y ahí va el token de pluginfile).
for _nombre in ("httpx", "httpcore"):
    _logger = logging.getLogger(_nombre)
    if _logger.level < logging.WARNING:
        _logger.setLevel(logging.WARNING)
logging.getLogger("httpx").addFilter(_ScrubFilter())


class MoodleError(Exception):
    """Error devuelto por Moodle. El mensaje es el `errorcode`."""


class TokenInvalid(MoodleError):
    """El token ya no sirve: hay que volver a iniciar sesión."""


class MoodleUnreachable(MoodleError):
    """No hubo respuesta de Moodle (red, timeout, HTTP de error o respuesta rota).

    No dice nada del campus: puede andar más tarde.
    """


class ForeignFileUrl(MoodleError):
    """La URL (o una redirección) no es un archivo de este campus: el token no viaja."""


class FileTooLarge(MoodleError):
    """El archivo pasa el tope de bytes."""


class TooManyRedirects(MoodleError):
    """El campus redirigió más de `MAX_REDIRECTS` veces (un bucle, casi seguro)."""


_TOKEN_ERRORS = {"invalidtoken", "accessexception"}
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    """(esquema, host, puerto) con el host en minúsculas y el puerto por defecto explícito."""
    esquema = url.scheme.lower()
    return esquema, url.host.lower(), url.port or _DEFAULT_PORTS.get(esquema)


def _has_dot_segments(path: str) -> bool:
    """True si algún segmento, ya decodificado (`%2e`, `%2F`), es `.` o `..`."""
    return any(seg in (".", "..") for seg in unquote(path).split("/"))


def _flatten(valor: Any, prefijo: str, salida: dict[str, Any]) -> None:
    if isinstance(valor, dict):
        for clave, v in valor.items():
            _flatten(v, f"{prefijo}[{clave}]" if prefijo else str(clave), salida)
    elif isinstance(valor, (list, tuple)):
        for i, v in enumerate(valor):
            _flatten(v, f"{prefijo}[{i}]", salida)
    elif isinstance(valor, bool):
        salida[prefijo] = int(valor)
    else:
        salida[prefijo] = valor


def _sanitized(exc: Exception) -> str:
    """Mensaje de error sin query string (la URL puede llevar el token)."""
    if isinstance(exc, httpx.HTTPStatusError):
        url = exc.request.url.copy_with(query=None)
        return scrub(f"HTTP {exc.response.status_code} en {url}")
    if isinstance(exc, httpx.RequestError):
        url = exc.request.url.copy_with(query=None) if getattr(exc, "_request", None) else "?"
        return scrub(f"{type(exc).__name__} en {url}")
    return scrub(f"respuesta inválida ({type(exc).__name__})")


def _moodle_error(datos: Any) -> MoodleError | None:
    """El error que corresponde a una respuesta `{"errorcode": ...}`, o None si no lo es."""
    if not (isinstance(datos, dict) and "errorcode" in datos):
        return None
    codigo = scrub(str(datos["errorcode"]))
    return TokenInvalid(codigo) if codigo in _TOKEN_ERRORS else MoodleError(codigo)


def _json_error(cuerpo: bytes) -> MoodleError | None:
    try:
        return _moodle_error(json.loads(cuerpo))
    except ValueError:
        return None


class MoodleClient:
    def __init__(self, site_url: str, token: str, http: httpx.Client):
        self.site_url = site_url.rstrip("/")
        self._token = token
        self._http = http

    def call(self, fn: str, **params: Any) -> Any:
        """Llama a una función del web service. Listas y dicts se aplanan a `k[0][x]`."""
        datos: dict[str, Any] = {}
        for clave, valor in params.items():
            _flatten(valor, clave, datos)
        datos.update(wstoken=self._token, wsfunction=fn, moodlewsrestformat="json")
        mensaje = None
        try:
            respuesta = self._http.post(f"{self.site_url}/webservice/rest/server.php", data=datos)
            respuesta.raise_for_status()
            resultado = respuesta.json()
        except (httpx.HTTPError, ValueError) as exc:
            mensaje = _sanitized(exc)
        if mensaje is not None:
            # Se lanza fuera del except para no encadenar la excepción original.
            raise MoodleUnreachable(mensaje)
        error = _moodle_error(resultado)
        if error is not None:
            raise error
        return resultado

    def is_campus_file(self, url: str) -> bool:
        """True si `url` es un `pluginfile` de este campus: el único lugar al que viaja el token.

        Compara el sitio ya parseado (esquema, host sin importar mayúsculas, puerto con su
        valor por defecto), sin `user@`, y exige el path de pluginfile sin segmentos `.` ni
        `..` (tampoco codificados): ni antes ni después de que httpx normalice.
        """
        try:
            pedida, sitio = httpx.URL(url), httpx.URL(self.site_url)
            crudo = urlsplit(url).path
        except (httpx.InvalidURL, ValueError):
            return False
        if not pedida.is_absolute_url or pedida.userinfo or _origin(pedida) != _origin(sitio):
            return False
        if _has_dot_segments(crudo) or _has_dot_segments(pedida.path):
            return False
        return pedida.path.startswith(sitio.path.rstrip("/") + PLUGINFILE_PATH)

    @contextmanager
    def _open_campus_file(self, fileurl: str) -> Iterator[httpx.Response]:
        """Abre `fileurl` en streaming, con el token, siguiendo redirecciones solo al campus.

        Deja la respuesta final ya validada (2xx). Lo que lanza no lleva el token, pero
        un `httpx.HTTPError` sale tal cual: quien llama lo pasa por `_sanitized`.
        """
        if not self.is_campus_file(fileurl):
            raise ForeignFileUrl("la URL no es un archivo del campus")
        url = httpx.URL(fileurl)
        for _ in range(MAX_REDIRECTS + 1):
            con_token = url.copy_merge_params({"token": self._token})
            with self._http.stream("GET", con_token, follow_redirects=False) as respuesta:
                if respuesta.is_redirect:
                    destino = url.join(respuesta.headers["location"]).copy_remove_param("token")
                    if not self.is_campus_file(str(destino)):
                        raise ForeignFileUrl("el campus redirigió fuera de sus archivos")
                    url = destino
                    continue
                if not respuesta.is_success:
                    cuerpo = _read_capped(respuesta, _ERROR_BODY_MAX)
                    error = _json_error(cuerpo) if cuerpo is not None else None
                    sin_query = url.copy_with(query=None)
                    raise error or MoodleUnreachable(
                        scrub(f"HTTP {respuesta.status_code} en {sin_query}"))
                yield respuesta
                return
        raise TooManyRedirects("demasiadas redirecciones")

    def fetch_file(self, fileurl: str) -> bytes:
        """Descarga un archivo chico del campus (`pluginfile`, hasta `FETCH_FILE_MAX`)."""
        mensaje = None
        try:
            with self._open_campus_file(fileurl) as respuesta:
                cuerpo = _read_capped(respuesta, FETCH_FILE_MAX)
            if cuerpo is None:
                raise FileTooLarge("el archivo pasa el tope")
            return cuerpo
        except httpx.HTTPError as exc:
            mensaje = _sanitized(exc)
        # Fuera del except: el mensaje y la cadena de excepciones no deben llevar el token.
        raise MoodleUnreachable(mensaje)

    def download_file(self, fileurl: str, dest: Path, *, max_bytes: int) -> int:
        """Baja un archivo del campus a `dest` sin pasar de `max_bytes`. Devuelve los bytes.

        Escribe en un temporal propio (`.<dest>.<azar>.part`, al lado de `dest`) y recién al
        terminar bien lo mueve a `dest`: si algo falla, el temporal se borra y un `dest`
        anterior queda como estaba. Dos descargas al mismo `dest` no se pisan el temporal.
        """
        salida = tempfile.NamedTemporaryFile(
            dir=dest.parent, prefix=f".{dest.name}.", suffix=".part", delete=False)
        part = Path(salida.name)
        mensaje = None
        listo = False
        try:
            with salida, self._open_campus_file(fileurl) as respuesta:
                escritos = _stream_to(respuesta, salida, max_bytes)
                es_json = respuesta.headers.get("content-type", "").startswith("application/json")
            error = _json_error(part.read_bytes()) if es_json and escritos <= _ERROR_BODY_MAX else None
            if error is not None:
                raise error
            os.replace(part, dest)
            listo = True
        except httpx.HTTPError as exc:
            mensaje = _sanitized(exc)
        finally:
            if not listo:
                part.unlink(missing_ok=True)
        if mensaje is not None:
            # Fuera del except, como en fetch_file: sin el token ni la excepción de httpx.
            raise MoodleUnreachable(mensaje)
        return escritos


def _content_length(respuesta: httpx.Response) -> int | None:
    try:
        return int(respuesta.headers["content-length"])
    except (KeyError, ValueError):
        return None


def _read_capped(respuesta: httpx.Response, max_bytes: int) -> bytes | None:
    """El cuerpo si no pasa de `max_bytes`; None si pasa (sin leer más de la cuenta)."""
    largo = _content_length(respuesta)
    if largo is not None and largo > max_bytes:
        return None
    partes, total = [], 0
    for trozo in respuesta.iter_bytes(_CHUNK):
        total += len(trozo)
        if total > max_bytes:
            return None
        partes.append(trozo)
    return b"".join(partes)


def _stream_to(respuesta: httpx.Response, salida: IO[bytes], max_bytes: int) -> int:
    """Escribe el cuerpo en `salida` cortando apenas pasa `max_bytes` (o antes, por el header)."""
    largo = _content_length(respuesta)
    if largo is not None and largo > max_bytes:
        raise FileTooLarge("el archivo pasa el tope")
    escritos = 0
    for trozo in respuesta.iter_bytes(_CHUNK):
        escritos += len(trozo)
        if escritos > max_bytes:
            raise FileTooLarge("el archivo pasa el tope")
        salida.write(trozo)
    return escritos
