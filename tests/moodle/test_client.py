import httpx
import pytest
import respx

from estudia.moodle.client import MoodleClient, MoodleError, MoodleUnreachable, TokenInvalid

URL = "https://campus.example"
ENDPOINT = f"{URL}/webservice/rest/server.php"
FILE = f"{URL}/webservice/pluginfile.php/1/mod_page/content/f.html"


def _client(http):
    return MoodleClient(URL, "TOK", http)


@respx.mock
def test_call_flattens_lists():
    ruta = respx.post(ENDPOINT).respond(json=[])
    with httpx.Client() as http:
        _client(http).call("x", courseids=[101], opts={"a": [1]})
    cuerpo = ruta.calls.last.request.content.decode()
    assert "courseids%5B0%5D=101" in cuerpo
    assert "opts%5Ba%5D%5B0%5D=1" in cuerpo
    assert "wsfunction=x" in cuerpo and "wstoken=TOK" in cuerpo


@respx.mock
def test_invalid_token_raises():
    respx.post(ENDPOINT).respond(200, json={"errorcode": "invalidtoken", "message": "x"})
    with httpx.Client() as http, pytest.raises(TokenInvalid):
        _client(http).call("x")


@respx.mock
def test_other_errorcode_raises_moodle_error():
    respx.post(ENDPOINT).respond(200, json={"errorcode": "nopermission"})
    with httpx.Client() as http, pytest.raises(MoodleError, match="nopermission") as exc:
        _client(http).call("x")
    assert not isinstance(exc.value, TokenInvalid)


@respx.mock
def test_fetch_file_adds_token():
    ruta = respx.get(FILE).respond(content=b"hola")
    with httpx.Client() as http:
        assert _client(http).fetch_file(f"{FILE}?forcedownload=1") == b"hola"
    url = ruta.calls.last.request.url
    assert url.params["token"] == "TOK" and url.params["forcedownload"] == "1"


def _sin_token(exc):
    visto = set()
    while exc is not None and id(exc) not in visto:
        visto.add(id(exc))
        assert "TOK" not in str(exc) and "TOK" not in repr(exc)
        exc = exc.__cause__ or exc.__context__


@respx.mock
def test_fetch_file_error_does_not_leak_token():
    respx.get(FILE).respond(403)
    with httpx.Client() as http, pytest.raises(MoodleError) as exc:
        _client(http).fetch_file(f"{FILE}?forcedownload=1")
    assert "403" in str(exc.value)
    _sin_token(exc.value)


@respx.mock
def test_fetch_file_transport_error_does_not_leak_token():
    respx.get(FILE).mock(side_effect=httpx.ConnectError("boom"))
    with httpx.Client() as http, pytest.raises(MoodleError) as exc:
        _client(http).fetch_file(FILE)
    _sin_token(exc.value)


@respx.mock
@pytest.mark.parametrize("respuesta", [
    {"status_code": 503},
    {"side_effect": httpx.ConnectTimeout("lento")},
    {"content": b"<html>no es json</html>"},
])
def test_transport_errors_are_unreachable(respuesta):
    """Red, timeout, 5xx o respuesta rota: MoodleUnreachable (sigue siendo MoodleError)."""
    ruta = respx.post(ENDPOINT)
    if "side_effect" in respuesta:
        ruta.mock(side_effect=respuesta["side_effect"])
    else:
        ruta.respond(respuesta.get("status_code", 200), content=respuesta.get("content", b""))
    with httpx.Client() as http, pytest.raises(MoodleUnreachable) as exc:
        _client(http).call("x")
    assert isinstance(exc.value, MoodleError)
    _sin_token(exc.value)


@respx.mock
def test_errorcode_is_not_unreachable():
    respx.post(ENDPOINT).respond(200, json={"errorcode": "nopermissions"})
    with httpx.Client() as http, pytest.raises(MoodleError) as exc:
        _client(http).call("x")
    assert not isinstance(exc.value, MoodleUnreachable)
