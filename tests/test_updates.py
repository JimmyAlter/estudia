import json

import httpx
import respx

from estudia import updates
from estudia.updates import RELEASE_API, latest_release

HTML_URL = "https://github.com/JimmyAlter/estudia/releases/tag/v0.2.0"
NOW = 1_790_000_000


class Kv:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value


def release(tag="v0.2.0", url=HTML_URL):
    return httpx.Response(200, json={"tag_name": tag, "html_url": url})


def check(kv, now=NOW):
    with httpx.Client() as http:
        return latest_release(http, now, kv.get, kv.set)


@respx.mock
def test_newer_release_returned():
    respx.get(RELEASE_API).mock(return_value=release("v0.2.0"))
    assert check(Kv()) == {"version": "0.2.0", "url": HTML_URL}


@respx.mock
def test_same_or_older_returns_none():
    for tag in ("v0.1.0", "v0.0.9"):
        respx.get(RELEASE_API).mock(return_value=release(tag))
        assert check(Kv()) is None


@respx.mock
def test_checks_at_most_daily():
    route = respx.get(RELEASE_API).mock(return_value=release())
    kv = Kv()
    assert check(kv)["version"] == "0.2.0"
    assert check(kv, NOW + 86399)["version"] == "0.2.0"  # sale de la caché
    assert route.call_count == 1
    check(kv, NOW + 86400)
    assert route.call_count == 2


@respx.mock
def test_network_error_returns_cache_or_none():
    route = respx.get(RELEASE_API)
    route.mock(side_effect=httpx.ConnectError("sin internet"))
    kv = Kv()
    assert check(kv) is None
    route.mock(return_value=httpx.Response(500))
    assert check(kv) is None
    route.mock(return_value=httpx.Response(200, content=b"no es json"))
    assert check(kv) is None
    route.mock(return_value=release())
    assert check(kv)["version"] == "0.2.0"
    route.mock(side_effect=httpx.ReadTimeout("lento"))
    assert check(kv, NOW + 90000)["version"] == "0.2.0"  # devuelve lo cacheado


@respx.mock
def test_tag_with_v_prefix_parsed():
    respx.get(RELEASE_API).mock(return_value=release("v0.10.0"))
    assert check(Kv())["version"] == "0.10.0"  # 0.10 > 0.1: no es comparación de texto
    respx.get(RELEASE_API).mock(return_value=release("0.3.0"))
    assert check(Kv())["version"] == "0.3.0"


@respx.mock
def test_foreign_or_broken_release_is_ignored():
    for body in (
        {"tag_name": "v0.2.0", "html_url": "https://evil.example/estudia/"},
        {"tag_name": "v0.2.0"},
        {"tag_name": "nightly", "html_url": HTML_URL},
        ["no", "es", "un", "objeto"],
    ):
        respx.get(RELEASE_API).mock(return_value=httpx.Response(200, json=body))
        assert check(Kv()) is None


@respx.mock
def test_cache_is_compared_with_installed_version(monkeypatch):
    respx.get(RELEASE_API).mock(return_value=release())
    kv = Kv()
    assert check(kv) is not None
    monkeypatch.setattr(updates, "__version__", "0.2.0")  # se actualizó
    assert check(kv, NOW + 10) is None
    assert json.loads(kv.data["update_latest"])["version"] == "0.2.0"


def test_default_http_sends_only_user_agent():
    with updates.default_http() as http:
        assert http.headers["user-agent"] == f"Estudia/{updates.__version__}"
        assert http.timeout.read == 5.0
        assert "cookie" not in http.headers
