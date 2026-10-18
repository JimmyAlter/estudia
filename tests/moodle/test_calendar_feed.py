"""Feed del calendario del campus: token de exportación y URL armada."""
import copy
import json
import logging
from pathlib import Path

import pytest

from estudia.moodle.calendar_feed import (
    EXPORT_TOKEN_FN,
    FeedUnreachable,
    export_url,
    feed_secret_name,
    feed_status_key,
    fetch_feed_url,
)
from estudia.moodle.client import MoodleError, MoodleUnreachable, TokenInvalid

FIXTURES = Path(__file__).parent.parent / "fixtures" / "moodle"
SITE_INFO = json.loads((FIXTURES / "core_webservice_get_site_info_sample.json").read_text("utf-8"))
EXPORT = json.loads(
    (FIXTURES / "core_calendar_get_calendar_export_token_sample.json").read_text("utf-8"))
FEED_TOKEN = "f00dfeedcafe0123456789abcdef0123"
SITE_URL = "https://campus.example"


class FeedMoodle:
    """Cliente falso: responde site_info y el token de exportación desde las fixtures."""

    def __init__(self, site_info=None, export=None, export_error=None, site_url=SITE_URL):
        self.site_url = site_url
        self.site_info = SITE_INFO if site_info is None else site_info
        self.export = EXPORT if export is None else export
        self.export_error = export_error
        self.calls: list[str] = []

    def call(self, fn, **params):
        self.calls.append(fn)
        if fn == "core_webservice_get_site_info":
            return self.site_info
        if fn == EXPORT_TOKEN_FN:
            if self.export_error is not None:
                raise self.export_error
            return self.export
        raise AssertionError(f"función inesperada: {fn}")


def test_secret_name_and_url_shape():
    assert feed_secret_name("utn-frba") == "moodle_calendar_feed:utn-frba"
    assert feed_status_key("utn-frba") == "calendar_feed_status:utn-frba"
    assert export_url(SITE_URL, 4242, "abc") == (
        f"{SITE_URL}/calendar/export_execute.php?userid=4242&authtoken=abc"
        "&preset_what=all&preset_time=recentupcoming")


def test_feed_url_built_from_token():
    client = FeedMoodle()
    assert fetch_feed_url(client) == export_url(SITE_URL, 4242, FEED_TOKEN)
    assert client.calls == ["core_webservice_get_site_info", EXPORT_TOKEN_FN]


def test_feed_url_ignores_trailing_slash_in_site_url():
    client = FeedMoodle(site_url=SITE_URL + "/")
    assert fetch_feed_url(client) == export_url(SITE_URL, 4242, FEED_TOKEN)


def test_function_missing_returns_none_without_calling():
    info = copy.deepcopy(SITE_INFO)
    info["functions"] = [f for f in info["functions"] if f["name"] != EXPORT_TOKEN_FN]
    client = FeedMoodle(site_info=info)
    assert fetch_feed_url(client) is None
    assert EXPORT_TOKEN_FN not in client.calls


def test_site_info_without_functions_returns_none():
    info = {k: v for k, v in SITE_INFO.items() if k != "functions"}
    client = FeedMoodle(site_info=info)
    assert fetch_feed_url(client) is None
    assert EXPORT_TOKEN_FN not in client.calls


@pytest.mark.parametrize("error", [MoodleError("nopermissions"), TokenInvalid("accessexception"),
                                   MoodleError("calendarexportdisabled")])
def test_moodle_error_returns_none(error):
    client = FeedMoodle(export_error=error)
    assert fetch_feed_url(client) is None


@pytest.mark.parametrize("error", [TokenInvalid("invalidtoken"), MoodleError("nopermissions"),
                                   MoodleUnreachable("HTTP 503 en https://campus.example/x")])
def test_site_info_error_is_not_definitive(error):
    """Sin site_info no se sabe si el campus ofrece el feed: se reintenta más tarde."""
    class Failing(FeedMoodle):
        def call(self, fn, **params):
            raise error

    with pytest.raises(FeedUnreachable) as exc:
        fetch_feed_url(Failing())
    assert exc.value.__cause__ is None and exc.value.__context__ is None


@pytest.mark.parametrize("error", [MoodleUnreachable("HTTP 503 en https://campus.example/x"),
                                   MoodleUnreachable("ConnectTimeout en https://campus.example/x"),
                                   TokenInvalid("invalidtoken"),
                                   MoodleError("sitemaintenance")])
def test_transient_export_error_is_not_definitive(error):
    with pytest.raises(FeedUnreachable):
        fetch_feed_url(FeedMoodle(export_error=error))


@pytest.mark.parametrize("export", [{"token": "", "warnings": []}, {"warnings": []},
                                    {"token": None}, ["raro"]])
def test_empty_or_odd_token_returns_none(export):
    assert fetch_feed_url(FeedMoodle(export=export)) is None


def test_fetch_never_logs_the_token(caplog):
    caplog.set_level(logging.DEBUG)
    assert fetch_feed_url(FeedMoodle()) is not None
    assert fetch_feed_url(FeedMoodle(export_error=MoodleError("nopermissions"))) is None
    with pytest.raises(FeedUnreachable) as exc:
        fetch_feed_url(FeedMoodle(export_error=MoodleUnreachable("HTTP 503")))
    assert FEED_TOKEN not in caplog.text and FEED_TOKEN not in str(exc.value)
