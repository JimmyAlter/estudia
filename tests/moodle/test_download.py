"""Descarga por `pluginfile`: solo del campus, con tope y sin filtrar el token (Review Focus 2)."""
import logging

import httpx
import pytest
import respx

from estudia.moodle.client import (
    FETCH_FILE_MAX,
    PLUGINFILE_PATH,
    FileTooLarge,
    ForeignFileUrl,
    MoodleClient,
    MoodleError,
    MoodleUnreachable,
    TokenInvalid,
    TooManyRedirects,
    scrub,
)

SITE = "https://aulasvirtuales.frba.utn.edu.ar"
TOKEN = "t0k3n-secreto"
FILE = f"{SITE}{PLUGINFILE_PATH}3808140/mod_resource/content/1/apunte.pdf"
MB = 1024 * 1024
CAP = 50 * MB
PDF = b"%PDF-1.4\n" + b"x" * 1000


def _client(http):
    return MoodleClient(SITE, TOKEN, http)


def _sin_token(exc):
    """Ni el mensaje ni la cadena de excepciones llevan el token."""
    visto = set()
    while exc is not None and id(exc) not in visto:
        visto.add(id(exc))
        assert TOKEN not in str(exc) and TOKEN not in repr(exc)
        exc = exc.__cause__ or exc.__context__


def _partes(carpeta):
    return list(carpeta.glob("*.part"))


def _sin_archivos(dest):
    assert not dest.exists()
    assert _partes(dest.parent) == []


class _NoLeer(httpx.SyncByteStream):
    """Cuerpo que falla si alguien lo lee."""

    def __iter__(self):
        raise AssertionError("se leyó el cuerpo")


class _Chunks(httpx.SyncByteStream):
    """Cuerpo sin Content-Length, de a 1 MB; anota cuántos trozos se pidieron."""

    def __init__(self, total_mb, on_chunk=None):
        self.total_mb = total_mb
        self.leidos = 0
        self.on_chunk = on_chunk

    def __iter__(self):
        for _ in range(self.total_mb):
            if self.on_chunk:
                self.on_chunk(self.leidos)
            self.leidos += 1
            yield b"\0" * MB


@respx.mock
def test_download_streams_to_dest(tmp_path):
    ruta = respx.get(FILE).respond(200, content=PDF, headers={"Content-Type": "application/pdf"})
    dest = tmp_path / "123.pdf"
    with httpx.Client() as http:
        n = _client(http).download_file(FILE, dest, max_bytes=CAP)
    assert n == len(PDF) and dest.read_bytes() == PDF
    assert _partes(tmp_path) == []
    assert ruta.calls.last.request.url.params["token"] == TOKEN


@respx.mock
@pytest.mark.parametrize("url", [
    "https://evil.example/webservice/pluginfile.php/1/a.pdf",
    "https://aulasvirtuales.frba.utn.edu.ar.evil.tld/webservice/pluginfile.php/1/a.pdf",
    "https://aulasvirtuales.frba.utn.edu.ar@evil.tld/webservice/pluginfile.php/1/a.pdf",
    "http://aulasvirtuales.frba.utn.edu.ar/webservice/pluginfile.php/1/a.pdf",
    f"{SITE}/mod/resource/view.php?id=1",
    f"{SITE}:8443/webservice/pluginfile.php/1/a.pdf",
    f"{SITE}/webservice/pluginfile.php/../../login/index.php",
    f"{SITE}/webservice/pluginfile.php/%2e%2e/%2E%2E/login/index.php",
    f"{SITE}/webservice/pluginfile.php/1%2F..%2F..%2F..%2Flogin/index.php",
    f"{SITE}/webservice/pluginfile.php/./1/a.pdf",
    f"{SITE}/webservice/pluginfile.php/1/%2e/a.pdf",
    "https://aulasvirtuales.frba.utn.edu.ar\\@evil.tld/webservice/pluginfile.php/1/a.pdf",
    "https://user:pw@aulasvirtuales.frba.utn.edu.ar/webservice/pluginfile.php/1/a.pdf",
    "https://aulasvirtuales.frba.utn.edu.ar:80/webservice/pluginfile.php/1/a.pdf",
    "https://xaulasvirtuales.frba.utn.edu.ar/webservice/pluginfile.php/1/a.pdf",
    f"{SITE}/WEBSERVICE/pluginfile.php/1/a.pdf",
    f"{SITE}/webservice/pluginfile.php",
    "/webservice/pluginfile.php/1/a.pdf",
    "no es una url",
])
def test_foreign_url_refused_without_request(tmp_path, url):
    dest = tmp_path / "1.pdf"
    with httpx.Client() as http:
        cliente = _client(http)
        assert not cliente.is_campus_file(url)
        with pytest.raises(ForeignFileUrl) as exc:
            cliente.download_file(url, dest, max_bytes=CAP)
        _sin_token(exc.value)
        with pytest.raises(ForeignFileUrl):
            cliente.fetch_file(url)
    assert respx.calls.call_count == 0
    _sin_archivos(dest)


@pytest.mark.parametrize("url", [
    FILE,
    FILE + "?forcedownload=1",
    FILE.replace("aulasvirtuales.frba.utn.edu.ar", "AulasVirtuales.FRBA.utn.edu.ar"),
    FILE.replace("utn.edu.ar/", "utn.edu.ar:443/"),
    f"{SITE}{PLUGINFILE_PATH}3808140/mod_resource/content/1/Apunte%20pr%C3%A1ctico.pdf",
])
def test_is_campus_file_accepts_pluginfile(url):
    cliente = MoodleClient(SITE + "/", TOKEN, None)
    assert cliente.is_campus_file(url)


def test_is_campus_file_with_site_path_and_port():
    cliente = MoodleClient("http://campus.example:8080/moodle", TOKEN, None)
    assert cliente.is_campus_file("http://campus.example:8080/moodle/webservice/pluginfile.php/1/a")
    assert not cliente.is_campus_file("http://campus.example/moodle/webservice/pluginfile.php/1/a")
    assert not cliente.is_campus_file("http://campus.example:8080/webservice/pluginfile.php/1/a")
    assert not cliente.is_campus_file(
        "http://campus.example:8080/moodle/webservice/pluginfile.php/%2e%2e/%2e%2e/x")


@respx.mock
def test_redirect_to_other_host_is_refused(tmp_path):
    respx.get(FILE).respond(302, headers={"Location": "https://cdn.evil.example/x.pdf"})
    evil = respx.route(host__regex=r"evil\.example").respond(200, content=PDF)
    dest = tmp_path / "1.pdf"
    with httpx.Client() as http, pytest.raises(ForeignFileUrl) as exc:
        _client(http).download_file(FILE, dest, max_bytes=CAP)
    assert evil.call_count == 0
    assert respx.calls.call_count == 1
    _sin_token(exc.value)
    _sin_archivos(dest)


@respx.mock
def test_redirect_to_same_site_non_pluginfile_is_refused(tmp_path):
    respx.get(FILE).respond(302, headers={"Location": "/login/index.php"})
    dest = tmp_path / "1.pdf"
    with httpx.Client() as http, pytest.raises(ForeignFileUrl):
        _client(http).download_file(FILE, dest, max_bytes=CAP)
    assert respx.calls.call_count == 1


@respx.mock
def test_same_site_redirect_followed(tmp_path):
    final = f"{SITE}{PLUGINFILE_PATH}3808140/mod_resource/content/2/apunte.pdf"
    respx.get(FILE).respond(303, headers={"Location": f"{final}?token=otro&forcedownload=1"})
    destino = respx.get(final).respond(200, content=PDF)
    dest = tmp_path / "1.pdf"
    with httpx.Client() as http:
        assert _client(http).download_file(FILE, dest, max_bytes=CAP) == len(PDF)
    params = destino.calls.last.request.url.params
    assert params.get_list("token") == [TOKEN] and params["forcedownload"] == "1"
    assert dest.read_bytes() == PDF


@respx.mock
def test_relative_redirect_followed(tmp_path):
    respx.get(FILE).respond(302, headers={"Location": "otro.pdf"})
    destino = respx.get(FILE.rsplit("/", 1)[0] + "/otro.pdf").respond(200, content=PDF)
    with httpx.Client() as http:
        _client(http).download_file(FILE, tmp_path / "1.pdf", max_bytes=CAP)
    assert destino.call_count == 1


@respx.mock
def test_redirect_loop_stops(tmp_path):
    otro = f"{SITE}{PLUGINFILE_PATH}1/b.pdf"
    respx.get(FILE).respond(302, headers={"Location": otro})
    respx.get(otro).respond(302, headers={"Location": FILE})
    dest = tmp_path / "1.pdf"
    with httpx.Client() as http, pytest.raises(TooManyRedirects) as exc:
        _client(http).download_file(FILE, dest, max_bytes=CAP)
    assert isinstance(exc.value, MoodleError)
    assert respx.calls.call_count == 4   # el pedido original y 3 redirecciones
    _sin_token(exc.value)
    _sin_archivos(dest)


@respx.mock
def test_size_cap_by_header_and_while_streaming(tmp_path):
    dest = tmp_path / "1.pdf"

    # Content-Length de 51 MB: se corta sin leer el cuerpo.
    respx.get(FILE).mock(return_value=httpx.Response(
        200, headers={"Content-Length": str(51 * MB)}, stream=_NoLeer()))
    with httpx.Client() as http, pytest.raises(FileTooLarge) as exc:
        _client(http).download_file(FILE, dest, max_bytes=CAP)
    _sin_token(exc.value)
    _sin_archivos(dest)

    # Sin Content-Length, 51 MB por streaming: se corta al pasar el tope y se borra el .part.
    vistos = []
    cuerpo = _Chunks(51, on_chunk=lambda i: vistos.append(len(_partes(tmp_path))) if i == 50 else None)
    respx.get(FILE).mock(return_value=httpx.Response(200, stream=cuerpo))
    with httpx.Client() as http, pytest.raises(FileTooLarge) as exc:
        _client(http).download_file(FILE, dest, max_bytes=CAP)
    assert vistos == [1]             # el .part llegó a existir a mitad de camino
    _sin_token(exc.value)
    _sin_archivos(dest)


@respx.mock
def test_previous_dest_survives_failed_download(tmp_path):
    dest = tmp_path / "1.pdf"
    dest.write_bytes(b"version vieja")
    respx.get(FILE).mock(return_value=httpx.Response(200, stream=_Chunks(3)))
    with httpx.Client() as http, pytest.raises(FileTooLarge):
        _client(http).download_file(FILE, dest, max_bytes=2 * MB)
    assert dest.read_bytes() == b"version vieja"
    assert _partes(tmp_path) == []


@respx.mock
def test_concurrent_downloads_to_same_dest_do_not_clash(tmp_path):
    """Dos descargas al mismo `dest`: cada una tiene su temporal y ninguna borra la otra."""
    otro = f"{SITE}{PLUGINFILE_PATH}1/b.pdf"
    dest = tmp_path / "1.pdf"
    resultados = []

    def a_mitad(i):
        if i == 1:   # A ya escribió un trozo en su temporal: B baja entera al mismo dest
            with httpx.Client() as http2:
                resultados.append(_client(http2).download_file(otro, dest, max_bytes=CAP))
            assert dest.read_bytes() == PDF

    respx.get(FILE).mock(return_value=httpx.Response(200, stream=_Chunks(3, on_chunk=a_mitad)))
    respx.get(otro).respond(200, content=PDF)
    with httpx.Client() as http:
        resultados.append(_client(http).download_file(FILE, dest, max_bytes=CAP))
    assert resultados == [len(PDF), 3 * MB]
    assert dest.stat().st_size == 3 * MB
    assert _partes(tmp_path) == []


@respx.mock
def test_moodle_error_json_instead_of_file(tmp_path):
    respx.get(FILE).respond(200, json={"error": "Token inválido", "errorcode": "invalidtoken"})
    dest = tmp_path / "1.pdf"
    with httpx.Client() as http, pytest.raises(TokenInvalid):
        _client(http).download_file(FILE, dest, max_bytes=CAP)
    _sin_archivos(dest)

    respx.get(FILE).respond(403, json={"errorcode": "nopermissions"})
    with httpx.Client() as http, pytest.raises(MoodleError, match="nopermissions") as exc:
        _client(http).download_file(FILE, dest, max_bytes=CAP)
    assert not isinstance(exc.value, (TokenInvalid, MoodleUnreachable))
    _sin_archivos(dest)


@respx.mock
@pytest.mark.parametrize("respuesta", [
    {"status_code": 404},
    {"status_code": 500},
    {"side_effect": httpx.ReadTimeout("lento")},
    {"side_effect": httpx.ConnectError("boom")},
])
def test_http_errors_are_unreachable(tmp_path, respuesta):
    ruta = respx.get(FILE)
    if "side_effect" in respuesta:
        ruta.mock(side_effect=respuesta["side_effect"])
    else:
        ruta.respond(respuesta["status_code"], text="<html>error</html>")
    dest = tmp_path / "1.pdf"
    with httpx.Client() as http, pytest.raises(MoodleUnreachable) as exc:
        _client(http).download_file(FILE, dest, max_bytes=CAP)
    if "status_code" in respuesta:
        assert str(respuesta["status_code"]) in str(exc.value)
    _sin_token(exc.value)
    _sin_archivos(dest)


class _TimeoutAMitad(httpx.SyncByteStream):
    def __iter__(self):
        yield b"\0" * 1000
        raise httpx.ReadTimeout("lento")


@respx.mock
def test_timeout_mid_stream_deletes_part(tmp_path):
    respx.get(FILE).mock(return_value=httpx.Response(200, stream=_TimeoutAMitad()))
    dest = tmp_path / "1.pdf"
    with httpx.Client() as http, pytest.raises(MoodleUnreachable) as exc:
        _client(http).download_file(FILE, dest, max_bytes=CAP)
    _sin_token(exc.value)
    _sin_archivos(dest)


@respx.mock
def test_fetch_file_has_size_cap():
    respx.get(FILE).mock(return_value=httpx.Response(200, stream=_Chunks(FETCH_FILE_MAX // MB + 1)))
    with httpx.Client() as http, pytest.raises(FileTooLarge) as exc:
        _client(http).fetch_file(FILE)
    _sin_token(exc.value)

    respx.get(FILE).mock(return_value=httpx.Response(
        200, headers={"Content-Length": str(FETCH_FILE_MAX + 1)}, stream=_NoLeer()))
    with httpx.Client() as http, pytest.raises(FileTooLarge):
        _client(http).fetch_file(FILE)


@respx.mock
def test_fetch_file_redirect_loop_and_foreign_redirect():
    respx.get(FILE).respond(302, headers={"Location": FILE})
    with httpx.Client() as http, pytest.raises(TooManyRedirects):
        _client(http).fetch_file(FILE)
    respx.get(FILE).respond(302, headers={"Location": "https://evil.example/x"})
    with httpx.Client() as http, pytest.raises(ForeignFileUrl):
        _client(http).fetch_file(FILE)


def test_httpx_loggers_quiet():
    """Su INFO imprime la URL con la query: quedan en WARNING como mínimo."""
    for nombre in ("httpx", "httpcore"):
        assert logging.getLogger(nombre).level >= logging.WARNING


def _casos(tmp_path):
    """Éxito, 404, timeout, FileTooLarge, redirección a otro host y JSON de error."""
    otro = f"{SITE}{PLUGINFILE_PATH}1/b.pdf"
    return [
        ("ok", lambda: respx.get(FILE).respond(200, content=PDF), None),
        ("redir", lambda: (respx.get(FILE).respond(302, headers={"Location": otro}),
                           respx.get(otro).respond(200, content=PDF)), None),
        ("404", lambda: respx.get(FILE).respond(404), MoodleUnreachable),
        ("timeout", lambda: respx.get(FILE).mock(side_effect=httpx.ConnectTimeout("lento")),
         MoodleUnreachable),
        ("grande", lambda: respx.get(FILE).mock(return_value=httpx.Response(
            200, stream=_Chunks(3))), FileTooLarge),
        ("ajeno", lambda: respx.get(FILE).respond(
            302, headers={"Location": f"https://evil.example/x?token={TOKEN}"}), ForeignFileUrl),
        ("json", lambda: respx.get(FILE).respond(
            200, json={"errorcode": "invalidtoken", "message": f"token={TOKEN}"}), TokenInvalid),
    ]


@pytest.mark.parametrize("httpx_level", [None, logging.DEBUG])
def test_download_token_never_logged_or_raised(tmp_path, caplog, httpx_level):
    """Review Focus 2: ni en caplog (DEBUG), ni en la excepción, ni en su cadena.

    Con `httpx_level=DEBUG` se fuerza el logger de httpx (como si alguien lo subiera):
    igual no sale el token.
    """
    caplog.set_level(logging.DEBUG)
    if httpx_level is not None:
        caplog.set_level(httpx_level, logger="httpx")
        caplog.set_level(httpx_level, logger="httpcore")
    for nombre, preparar, esperada in _casos(tmp_path):
        dest = tmp_path / f"{nombre}.pdf"
        with respx.mock, httpx.Client() as http:
            preparar()
            cliente = _client(http)
            if esperada is None:
                cliente.download_file(FILE + "?forcedownload=1", dest, max_bytes=2 * MB)
                assert TOKEN not in str(cliente.fetch_file(FILE))
            else:
                with pytest.raises(esperada) as exc:
                    cliente.download_file(FILE, dest, max_bytes=2 * MB)
                _sin_token(exc.value)
                assert TOKEN not in repr(exc.value.__context__)
        if httpx_level is not None and nombre == "ok":
            assert "HTTP Request" in caplog.text   # el logger forzado sí escribió
    assert TOKEN not in caplog.text


def test_scrub_masks_token_and_wstoken():
    texto = (f"GET {FILE}?forcedownload=1&token={TOKEN} y wstoken=abc123&x=1 "
             f"y 'TOKEN=zzz' y \"WsToken=yyy#frag\" sin tocar mytoken=no ni token= solo")
    limpio = scrub(texto)
    assert TOKEN not in limpio and "abc123" not in limpio
    assert "zzz" not in limpio and "yyy" not in limpio
    assert "token=***" in limpio and "wstoken=***&x=1" in limpio
    assert "Wstoken=***#frag" in limpio and "'token=***'" in limpio
    assert "mytoken=no" in limpio
    assert scrub("nada que ver") == "nada que ver"
