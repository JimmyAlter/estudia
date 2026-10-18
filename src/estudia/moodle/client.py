"""Cliente de solo lectura para el web service REST de Moodle."""

from typing import Any

import httpx


class MoodleError(Exception):
    """Error devuelto por Moodle. El mensaje es el `errorcode`."""


class TokenInvalid(MoodleError):
    """El token ya no sirve: hay que volver a iniciar sesión."""


class MoodleUnreachable(MoodleError):
    """No hubo respuesta de Moodle (red, timeout, HTTP de error o respuesta rota).

    No dice nada del campus: puede andar más tarde.
    """


_TOKEN_ERRORS = {"invalidtoken", "accessexception"}


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
        return f"HTTP {exc.response.status_code} en {url}"
    if isinstance(exc, httpx.RequestError):
        url = exc.request.url.copy_with(query=None) if getattr(exc, "_request", None) else "?"
        return f"{type(exc).__name__} en {url}"
    return f"respuesta inválida ({type(exc).__name__})"


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
        if isinstance(resultado, dict) and "errorcode" in resultado:
            codigo = resultado["errorcode"]
            if codigo in _TOKEN_ERRORS:
                raise TokenInvalid(codigo)
            raise MoodleError(codigo)
        return resultado

    def fetch_file(self, fileurl: str) -> bytes:
        """Descarga un archivo de Moodle agregando el token a la URL."""
        url = httpx.URL(fileurl).copy_merge_params({"token": self._token})
        mensaje = None
        try:
            respuesta = self._http.get(url, follow_redirects=True)
            respuesta.raise_for_status()
            return respuesta.content
        except httpx.HTTPError as exc:
            mensaje = _sanitized(exc)
        # Fuera del except: el mensaje y la cadena de excepciones no deben llevar el token.
        raise MoodleError(mensaje)
