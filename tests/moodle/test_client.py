import httpx
import pytest
import respx

from estudia.moodle.client import MoodleClient, MoodleError, TokenInvalid

URL = "https://campus.example"
ENDPOINT = f"{URL}/webservice/rest/server.php"


def _client(http):
    return MoodleClient(URL, "TOK", http)


@respx.mock
def test_call_flattens_lists():
    ruta = respx.post(ENDPOINT).respond(json=[])
    with httpx.Client() as http:
        _client(http).call("x", courseids=[4242], opts={"a": [1]})
    cuerpo = ruta.calls.last.request.content.decode()
    assert "courseids%5B0%5D=4242" in cuerpo
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
    ruta = respx.get(f"{URL}/f.html").respond(content=b"hola")
    with httpx.Client() as http:
        assert _client(http).fetch_file(f"{URL}/f.html?forcedownload=1") == b"hola"
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
    respx.get(f"{URL}/f.html").respond(403)
    with httpx.Client() as http, pytest.raises(MoodleError) as exc:
        _client(http).fetch_file(f"{URL}/f.html?forcedownload=1")
    assert "403" in str(exc.value)
    _sin_token(exc.value)


@respx.mock
def test_fetch_file_transport_error_does_not_leak_token():
    respx.get(f"{URL}/f.html").mock(side_effect=httpx.ConnectError("boom"))
    with httpx.Client() as http, pytest.raises(MoodleError) as exc:
        _client(http).fetch_file(f"{URL}/f.html")
    _sin_token(exc.value)
