"""API del feed del calendario del campus: el secreto solo sale por `reveal`."""
import copy
import json
import logging
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from estudia.catalog import Site, get_site
from estudia.db import connect
from estudia.moodle.calendar_feed import (
    EXPORT_TOKEN_FN,
    export_url,
    feed_secret_name,
    feed_status_key,
)
from estudia.moodle.client import MoodleClient, MoodleError, MoodleUnreachable, TokenInvalid
from estudia.secrets import get_secret, set_secret
from estudia.server import app as app_module
from estudia.server.app import create_app, forget_moodle_session
from test_app import BASE, PORT, SITE_ID, SITE_URL, FakeMoodle, callback_url, make_provider, start_login

FIXTURES = Path(__file__).parent.parent / "fixtures" / "moodle"
SITE_INFO = json.loads((FIXTURES / "core_webservice_get_site_info_sample.json").read_text("utf-8"))
EXPORT = json.loads(
    (FIXTURES / "core_calendar_get_calendar_export_token_sample.json").read_text("utf-8"))
FEED_TOKEN = "f00dfeedcafe0123456789abcdef0123"
FEED_URL = export_url(SITE_URL, SITE_INFO["userid"], FEED_TOKEN)
EVIL = {"origin": "http://evil.example"}


class FeedMoodle(FakeMoodle):
    """FakeMoodle con `functions` en site_info y el token de exportación."""

    offers_feed = True
    # Error solo de la llamada del token de exportación (site_info anda).
    export_error: Exception | None = None

    def call(self, fn, **params):
        if self.fail_with is not None:
            raise self.fail_with
        if fn == "core_webservice_get_site_info":
            info = copy.deepcopy(SITE_INFO)
            if not self.offers_feed:
                info["functions"] = [f for f in info["functions"] if f["name"] != EXPORT_TOKEN_FN]
            return info
        if fn == EXPORT_TOKEN_FN:
            if self.export_error is not None:
                raise self.export_error
            return EXPORT
        return super().call(fn, **params)


def export_failing(error):
    return type("ExportFails", (FeedMoodle,), {"export_error": error})


class NoFeedMoodle(FeedMoodle):
    offers_feed = False


@pytest.fixture
def env(tmp_path):
    state = {"moodle_error": None, "moodle_cls": FeedMoodle}
    db_path = tmp_path / "estudia.db"

    def moodle_factory(site_url, token):
        return state["moodle_cls"](site_url, token, state["moodle_error"])

    app = create_app(
        db_path=db_path,
        moodle_client_factory=moodle_factory,
        ai_provider_factory=make_provider,
        open_browser=lambda url: None,
        web_dist=tmp_path / "no-existe",
        port=PORT,
    )
    state.update(app=app, client=TestClient(app, base_url=BASE), db_path=db_path)
    conn = connect(db_path)
    conn.execute("INSERT INTO kv(key, value) VALUES ('age_gate', 'adult')")
    conn.commit()
    conn.close()
    return state


def login(env):
    c = env["client"]
    assert c.post("/api/site", json={"site_id": SITE_ID}).status_code == 200
    set_secret(f"moodle_token:{SITE_ID}", "tok-moodle")
    return c


def kv_value(db_path, key):
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def test_status_unknown_before_fetch(env):
    c = login(env)
    assert c.get("/api/calendar-feed").json() == {
        "status": "unknown", "campus_calendar_url": f"{SITE_URL}/calendar/export.php"}
    assert c.post("/api/calendar-feed/reveal").status_code == 404


def test_fetch_requires_campus_session(env):
    c = env["client"]
    assert c.post("/api/site", json={"site_id": SITE_ID}).status_code == 200
    assert c.post("/api/calendar-feed/fetch").status_code == 401


def test_fetch_saves_to_keyring_not_db(env):
    c = login(env)
    r = c.post("/api/calendar-feed/fetch")
    assert r.status_code == 200 and r.json() == {"status": "saved", "retry": False}
    assert get_secret(feed_secret_name(SITE_ID)) == FEED_URL
    assert kv_value(env["db_path"], feed_status_key(SITE_ID)) == "saved"
    assert kv_value(env["db_path"], "calendar_feed_status") is None
    conn = connect(env["db_path"])
    try:
        dump = "\n".join(conn.iterdump())
    finally:
        conn.close()
    assert FEED_TOKEN not in dump
    assert c.post("/api/calendar-feed/reveal").json() == {"url": FEED_URL}


@respx.mock
def test_feed_token_never_leaves_reveal(env, caplog):
    """Review Focus 3: con el cliente real (respx, sin red), el token solo sale por reveal."""
    caplog.set_level(logging.DEBUG)
    endpoint = respx.post(f"{SITE_URL}/webservice/rest/server.php")

    def respond(request):
        fn = dict(httpx.QueryParams(request.content.decode()))["wsfunction"]
        if fn == "core_webservice_get_site_info":
            return httpx.Response(200, json=SITE_INFO)
        if fn == EXPORT_TOKEN_FN:
            return httpx.Response(200, json=EXPORT)
        return httpx.Response(200, json={"errorcode": "invalidrecord"})

    endpoint.side_effect = respond
    http = httpx.Client()
    env["app"].state.jobs.moodle_client_factory = lambda url, token: MoodleClient(url, token, http)
    c = login(env)

    fetched = c.post("/api/calendar-feed/fetch")
    assert fetched.status_code == 200
    assert FEED_TOKEN not in fetched.text

    for path in ("/api/state", "/api/calendar-feed", "/api/settings", "/api/agenda", "/api/home"):
        r = c.get(path)
        assert r.status_code == 200, path
        assert FEED_TOKEN not in r.text, path

    conn = connect(env["db_path"])
    try:
        dump = "\n".join(conn.iterdump())
    finally:
        conn.close()
    assert FEED_TOKEN not in dump
    assert FEED_TOKEN not in caplog.text

    revealed = c.post("/api/calendar-feed/reveal")
    assert revealed.status_code == 200
    assert FEED_TOKEN in revealed.json()["url"]
    assert revealed.headers["cache-control"] == "no-store"
    http.close()


def test_reveal_rejects_foreign_origin(env):
    c = login(env)
    assert c.post("/api/calendar-feed/fetch").status_code == 200
    r = c.post("/api/calendar-feed/reveal", headers=EVIL)
    assert r.status_code == 403
    assert FEED_TOKEN not in r.text
    assert c.post("/api/calendar-feed/fetch", headers=EVIL).status_code == 403


def test_reveal_rejects_foreign_host(env):
    c = login(env)
    assert c.post("/api/calendar-feed/fetch").status_code == 200
    r = c.post("/api/calendar-feed/reveal", headers={"host": "evil.example"})
    assert r.status_code == 400
    assert FEED_TOKEN not in r.text


def test_reveal_is_post_only(env):
    c = login(env)
    assert c.post("/api/calendar-feed/fetch").status_code == 200
    assert c.get("/api/calendar-feed/reveal").status_code == 405


def test_fallback_guide_when_unavailable(env):
    env["moodle_cls"] = NoFeedMoodle
    c = login(env)
    r = c.post("/api/calendar-feed/fetch")
    assert r.json() == {"status": "unavailable", "retry": False}
    assert c.get("/api/calendar-feed").json() == {
        "status": "unavailable", "campus_calendar_url": f"{SITE_URL}/calendar/export.php"}
    assert c.post("/api/calendar-feed/reveal").status_code == 404
    assert get_secret(feed_secret_name(SITE_ID)) is None


def test_unavailable_replaces_an_old_feed(env):
    c = login(env)
    assert c.post("/api/calendar-feed/fetch").json() == {"status": "saved", "retry": False}
    env["moodle_cls"] = NoFeedMoodle
    assert c.post("/api/calendar-feed/fetch").json() == {"status": "unavailable", "retry": False}
    assert get_secret(feed_secret_name(SITE_ID)) is None
    assert c.post("/api/calendar-feed/reveal").status_code == 404


def test_accessexception_on_export_is_unavailable_and_keeps_the_session(env):
    """La función fuera del servicio responde accessexception: guía, sin cerrar la sesión."""
    c = login(env)
    env["moodle_cls"] = export_failing(TokenInvalid("accessexception"))
    assert c.post("/api/calendar-feed/fetch").json() == {"status": "unavailable", "retry": False}
    assert get_secret(f"moodle_token:{SITE_ID}") == "tok-moodle"
    assert c.get("/api/state").json()["step"] != "login"


@pytest.mark.parametrize("error", [MoodleError("nopermissions"), MoodleError("calendarexportdisabled")])
def test_definitive_export_error_deletes_saved_feed(env, error):
    c = login(env)
    assert c.post("/api/calendar-feed/fetch").json() == {"status": "saved", "retry": False}
    env["moodle_cls"] = export_failing(error)
    assert c.post("/api/calendar-feed/fetch").json() == {"status": "unavailable", "retry": False}
    assert get_secret(feed_secret_name(SITE_ID)) is None
    assert c.get("/api/calendar-feed").json()["status"] == "unavailable"


@pytest.mark.parametrize("error", [
    MoodleUnreachable("HTTP 503 en https://campus.example/webservice/rest/server.php"),
    MoodleUnreachable("ConnectTimeout en https://campus.example/webservice/rest/server.php"),
    TokenInvalid("invalidtoken"),
    MoodleError("sitemaintenance"),
])
def test_transient_export_error_keeps_saved_feed(env, error):
    c = login(env)
    assert c.post("/api/calendar-feed/fetch").json() == {"status": "saved", "retry": False}
    env["moodle_cls"] = export_failing(error)
    assert c.post("/api/calendar-feed/fetch").json() == {"status": "saved", "retry": True}
    assert get_secret(feed_secret_name(SITE_ID)) == FEED_URL
    assert c.get("/api/calendar-feed").json()["status"] == "saved"
    assert c.post("/api/calendar-feed/reveal").json() == {"url": FEED_URL}


@pytest.mark.parametrize("error", [
    TokenInvalid("invalidtoken"),
    MoodleUnreachable("ConnectError en https://campus.example/webservice/rest/server.php"),
])
def test_site_info_failure_keeps_status_and_session(env, error):
    """Sin site_info no se sabe nada: estado igual, reintentar, y la sesión no se toca."""
    c = login(env)
    env["moodle_error"] = error
    assert c.post("/api/calendar-feed/fetch").json() == {"status": "unknown", "retry": True}
    assert kv_value(env["db_path"], feed_status_key(SITE_ID)) is None
    assert get_secret(f"moodle_token:{SITE_ID}") == "tok-moodle"
    assert c.get("/api/state").json()["step"] != "login"


def test_transient_failure_keeps_unavailable(env):
    env["moodle_cls"] = NoFeedMoodle
    c = login(env)
    assert c.post("/api/calendar-feed/fetch").json() == {"status": "unavailable", "retry": False}
    env["moodle_error"] = MoodleUnreachable("HTTP 502 en https://campus.example/x")
    assert c.post("/api/calendar-feed/fetch").json() == {"status": "unavailable", "retry": True}


OTHER = Site(id="campus-prueba", name="Campus de prueba", university="Prueba",
             url="https://campus.example", login="browser")


def test_status_is_per_campus(env, monkeypatch):
    monkeypatch.setattr(
        app_module, "get_site", lambda site_id: OTHER if site_id == OTHER.id else get_site(site_id))
    env["moodle_cls"] = NoFeedMoodle
    c = login(env)
    assert c.post("/api/calendar-feed/fetch").json() == {"status": "unavailable", "retry": False}

    # Otro campus: no hereda el estado del anterior.
    assert c.post("/api/site", json={"site_id": OTHER.id}).status_code == 200
    assert c.get("/api/calendar-feed").json() == {
        "status": "unknown", "campus_calendar_url": "https://campus.example/calendar/export.php"}
    set_secret(f"moodle_token:{OTHER.id}", "tok-otro")
    env["moodle_cls"] = FeedMoodle
    assert c.post("/api/calendar-feed/fetch").json() == {"status": "saved", "retry": False}
    assert get_secret(feed_secret_name(OTHER.id)).startswith("https://campus.example/calendar/")

    # Volver al primero: sigue como estaba.
    assert c.post("/api/site", json={"site_id": SITE_ID}).status_code == 200
    assert c.get("/api/calendar-feed").json()["status"] == "unavailable"
    assert c.post("/api/calendar-feed/reveal").status_code == 404


def test_old_global_status_key_is_ignored(env):
    c = login(env)
    conn = connect(env["db_path"])
    conn.execute("INSERT INTO kv(key, value) VALUES ('calendar_feed_status', 'unavailable')")
    conn.commit()
    conn.close()
    assert c.get("/api/calendar-feed").json()["status"] == "unknown"


def test_logout_forgets_feed(env):
    c = login(env)
    assert c.post("/api/calendar-feed/fetch").json() == {"status": "saved", "retry": False}
    # El campus rechaza el token en la sincronización: se olvida la sesión entera.
    env["moodle_error"] = TokenInvalid("invalidtoken")
    assert c.post("/api/sync").status_code == 202
    assert get_secret(f"moodle_token:{SITE_ID}") is None
    assert get_secret(feed_secret_name(SITE_ID)) is None
    assert c.post("/api/calendar-feed/reveal").status_code == 404
    assert c.get("/api/calendar-feed").json()["status"] == "unknown"


def test_forget_moodle_session_deletes_feed_secret():
    set_secret(feed_secret_name(SITE_ID), FEED_URL)
    forget_moodle_session(SITE_ID)
    assert get_secret(feed_secret_name(SITE_ID)) is None


def test_new_login_forgets_the_previous_feed(env):
    """Otra cuenta en el mismo campus nunca ve el feed de la anterior."""
    c = login(env)
    assert c.post("/api/calendar-feed/fetch").json() == {"status": "saved", "retry": False}
    env["moodle_cls"] = NoFeedMoodle
    passport = start_login(c)
    r = c.post("/api/login/callback", json={"url": callback_url(passport, token="tok-otra-cuenta")})
    assert r.status_code == 200
    assert get_secret(feed_secret_name(SITE_ID)) is None
    assert kv_value(env["db_path"], feed_status_key(SITE_ID)) is None
    assert c.post("/api/calendar-feed/reveal").status_code == 404
    assert c.get("/api/calendar-feed").json()["status"] == "unknown"


def test_same_token_login_keeps_the_feed(env):
    c = login(env)
    assert c.post("/api/calendar-feed/fetch").json() == {"status": "saved", "retry": False}
    passport = start_login(c)
    r = c.post("/api/login/callback", json={"url": callback_url(passport, token="tok-moodle")})
    assert r.status_code == 200
    assert get_secret(feed_secret_name(SITE_ID)) == FEED_URL
