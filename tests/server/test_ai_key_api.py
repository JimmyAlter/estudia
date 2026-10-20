"""POST /api/ai: la clave se limpia, los errores van en castellano y nunca se filtra."""
import logging

import pytest
from fastapi.testclient import TestClient

from estudia.ai.base import AIAuthError, AIFormatError, AIQuotaError, AIUnavailableError
from estudia.ai.keys import (
    KEY_FORMAT_TEXT,
    KEY_MISSING_TEXT,
    KEY_REJECTED_TEXT,
    KEY_UNAVAILABLE_TEXT,
)
from estudia.db import connect
from estudia.secrets import get_secret
from estudia.server.app import create_app
from fakes import FakeProvider
from test_app import BASE, PORT, FakeMoodle

WELL_FORMED = "AIza" + "k" * 35
MALFORMED = "clave-rara-" + "z" * 20


class CheckProvider(FakeProvider):
    def __init__(self, exc):
        super().__init__({}, lambda s, o: 1)
        self.exc = exc

    def check_key(self):
        if self.exc:
            raise self.exc


@pytest.fixture
def make_env(tmp_path):
    def build(exc=None):
        built = []

        def factory(key):
            built.append(key)
            return CheckProvider(exc)

        db_path = tmp_path / "estudia.db"
        app = create_app(
            db_path=db_path,
            moodle_client_factory=lambda url, token: FakeMoodle(url, token),
            ai_provider_factory=factory,
            open_browser=lambda url: None,
            web_dist=tmp_path / "no-existe",
            port=PORT,
        )
        conn = connect(db_path)
        conn.execute("INSERT INTO kv(key, value) VALUES ('age_gate', 'adult')")
        conn.commit()
        conn.close()
        return TestClient(app, base_url=BASE), built, db_path

    return build


def post(client, key):
    return client.post("/api/ai", json={"provider": "gemini", "key": key})


def test_empty_key_says_to_paste_it(make_env):
    client, built, _ = make_env()
    for raw in ("", "  \n", '""'):
        r = post(client, raw)
        assert r.status_code == 400
        assert r.json()["detail"] == KEY_MISSING_TEXT
    assert built == []


def test_rejected_key_text_says_what_to_do():
    assert KEY_REJECTED_TEXT == "Google no aceptó esa clave. Creá otra en Google AI Studio y pegala acá."


def test_rejected_key_says_so_in_spanish(make_env):
    client, _, _ = make_env(AIAuthError("x"))
    r = post(client, WELL_FORMED)
    assert r.status_code == 400
    assert r.json()["detail"] == KEY_REJECTED_TEXT
    assert get_secret("gemini_key") is None


def test_invalid_request_is_also_rejected(make_env):
    client, _, _ = make_env(AIFormatError("x"))
    r = post(client, WELL_FORMED)
    assert (r.status_code, r.json()["detail"]) == (400, KEY_REJECTED_TEXT)


def test_malformed_key_rejected_by_google_gets_the_format_text(make_env):
    client, built, _ = make_env(AIAuthError("x"))
    r = post(client, MALFORMED)
    assert r.status_code == 400
    assert r.json()["detail"] == KEY_FORMAT_TEXT
    assert built == [MALFORMED]  # igual se le preguntó a Google
    assert get_secret("gemini_key") is None


def test_malformed_key_google_accepts_is_saved(make_env):
    # Simula un cambio futuro de formato: manda Google, no el patrón.
    client, _, _ = make_env(None)
    r = post(client, MALFORMED)
    assert r.status_code == 200
    assert get_secret("gemini_key") == MALFORMED


def test_unavailable_is_503_in_spanish(make_env):
    client, _, _ = make_env(AIUnavailableError("x"))
    r = post(client, WELL_FORMED)
    assert r.status_code == 503
    assert r.json()["detail"] == KEY_UNAVAILABLE_TEXT
    assert get_secret("gemini_key") is None


def test_quota_error_still_accepts_the_key(make_env):
    client, _, _ = make_env(AIQuotaError("x"))
    assert post(client, WELL_FORMED).status_code == 200
    assert get_secret("gemini_key") == WELL_FORMED


@pytest.mark.parametrize("raw", [
    f"  {WELL_FORMED}\n",
    f"“{WELL_FORMED}”",
    f'"{WELL_FORMED}"',
])
def test_quotes_and_whitespace_are_stripped_before_saving(make_env, raw):
    client, built, _ = make_env(None)
    assert post(client, raw).status_code == 200
    assert set(built) == {WELL_FORMED}
    assert get_secret("gemini_key") == WELL_FORMED


@pytest.mark.parametrize("exc", [AIAuthError("x"), AIUnavailableError("x"), None])
def test_ai_key_never_logged_or_echoed(make_env, caplog, exc):
    caplog.set_level(logging.DEBUG)
    secret = "AIza" + "S3cr3tK3y" * 3 + "Zz_-xyAB"
    client, _, db_path = make_env(exc)
    r = post(client, f"  “{secret}” ")
    # La clave nunca vuelve en el cuerpo, los headers ni la URL pedida.
    assert secret not in r.text
    assert all(secret not in f"{k}{v}" for k, v in r.headers.items())
    assert secret not in str(r.request.url)
    assert secret not in caplog.text
    assert secret not in db_path.read_bytes().decode("latin-1")


def test_malformed_key_never_logged_or_echoed(make_env, caplog):
    caplog.set_level(logging.DEBUG)
    secret = "no-es-una-clave-" + "Q9" * 10
    client, _, _ = make_env(AIAuthError("x"))
    r = post(client, secret)
    assert r.status_code == 400
    assert secret not in r.text
    assert secret not in caplog.text


def test_validation_error_does_not_echo_the_key(make_env, caplog):
    caplog.set_level(logging.DEBUG)
    secret = "AIza" + "V4l1d" * 7
    client, _, _ = make_env(None)
    r = client.post("/api/ai", json={"provider": "gemini", "key": [secret]})
    assert r.status_code == 422
    assert secret not in r.text
    assert secret not in caplog.text
