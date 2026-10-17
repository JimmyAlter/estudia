"""Autenticación contra Moodle: lanzamiento por navegador, callback y token.php."""

import base64
import binascii
from dataclasses import dataclass
from urllib.parse import quote, unquote

import httpx

from estudia.catalog import Site


class AuthError(Exception):
    """Falla de autenticación. El mensaje es el código de error de Moodle si existe."""


@dataclass(frozen=True)
class TokenPair:
    token: str
    private_token: str | None = None
    signature: str | None = None


def launch_url(site: Site, passport: str, scheme: str = "estudia") -> str:
    return (
        f"{site.url.rstrip('/')}/admin/tool/mobile/launch.php"
        f"?service=moodle_mobile_app&passport={quote(passport)}&urlscheme={scheme}"
    )


def parse_callback(url: str) -> TokenPair:
    """Interpreta `<scheme>://token=<base64(firma:::token:::privatetoken)>`.

    Algunos navegadores agregan una `/` final y el base64 puede venir url-encoded.
    """
    _, sep, resto = url.partition("://")
    if not sep or not resto.startswith("token="):
        raise AuthError("callback inválido")
    codificado = unquote(resto[len("token="):]).rstrip("/")
    codificado += "=" * (-len(codificado) % 4)
    try:
        decodificado = base64.b64decode(codificado, validate=True).decode("utf-8")
    except (binascii.Error, ValueError) as exc:
        raise AuthError("callback inválido") from exc
    partes = decodificado.split(":::")
    if len(partes) < 2 or not partes[1]:
        raise AuthError("callback sin token")
    firma = partes[0] or None
    privado = partes[2] if len(partes) > 2 and partes[2] else None
    return TokenPair(token=partes[1], private_token=privado, signature=firma)


def password_login(site: Site, username: str, password: str, http: httpx.Client) -> TokenPair:
    respuesta = http.post(
        f"{site.url.rstrip('/')}/login/token.php",
        data={"username": username, "password": password, "service": "moodle_mobile_app"},
    )
    try:
        datos = respuesta.json()
    except ValueError as exc:
        raise AuthError("respuesta inválida") from exc
    if not isinstance(datos, dict) or not datos.get("token"):
        codigo = datos.get("errorcode") if isinstance(datos, dict) else None
        raise AuthError(codigo or "sin token")
    return TokenPair(token=datos["token"], private_token=datos.get("privatetoken"))
