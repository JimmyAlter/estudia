import base64
from urllib.parse import quote

import httpx
import pytest
import respx

from estudia.catalog import get_site
from estudia.moodle.auth import AuthError, TokenPair, launch_url, parse_callback, password_login


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def test_launch_url():
    url = launch_url(get_site("utn-frba"), "123")
    assert url.startswith("https://aulasvirtuales.frba.utn.edu.ar/")
    assert "admin/tool/mobile/launch.php?service=moodle_mobile_app&passport=123&urlscheme=estudia" in url


def test_parse_callback():
    url = "estudia://token=" + _b64("firma:::TOK:::PRIV")
    assert parse_callback(url) == TokenPair("TOK", "PRIV", "firma")


def test_parse_callback_moodlemobile_scheme():
    # Moodle fuerza `moodlemobile://` por defecto (tool_mobile/forcedurlscheme).
    url = "moodlemobile://token=" + _b64("firma:::TOK:::PRIV")
    assert parse_callback(url) == TokenPair("TOK", "PRIV", "firma")


def test_parse_callback_trailing_slash_and_urlencoded():
    cod = _b64("firma:::TOK:::PRIV")
    assert parse_callback("estudia://token=" + cod + "/") == TokenPair("TOK", "PRIV", "firma")
    assert parse_callback("estudia://token=" + quote(cod, safe="")) == TokenPair("TOK", "PRIV", "firma")


def test_parse_callback_without_private_token():
    assert parse_callback("estudia://token=" + _b64("firma:::TOK")) == TokenPair("TOK", None, "firma")


@pytest.mark.parametrize("url", ["", "garbage", "estudia://token=", "estudia://token=!!!", "estudia://otro=abc",
                                 "estudia://token=" + base64.b64encode(b"sin separadores").decode()])
def test_parse_callback_rejects_garbage(url):
    with pytest.raises(AuthError):
        parse_callback(url)


@respx.mock
def test_password_login():
    site = get_site("utn-frba")
    ruta = respx.post(f"{site.url}/login/token.php")
    ruta.respond(json={"token": "T", "privatetoken": "P"})
    with httpx.Client() as http:
        assert password_login(site, "u", "p", http) == TokenPair("T", "P", None)
    cuerpo = ruta.calls.last.request.content.decode()
    assert "service=moodle_mobile_app" in cuerpo and "username=u" in cuerpo


@respx.mock
def test_password_login_error():
    site = get_site("utn-frba")
    respx.post(f"{site.url}/login/token.php").respond(
        json={"error": "Invalid login", "errorcode": "invalidlogin"}
    )
    with httpx.Client() as http, pytest.raises(AuthError, match="invalidlogin"):
        password_login(site, "u", "mala", http)
