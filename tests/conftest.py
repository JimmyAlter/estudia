import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError


class MemoryKeyring(KeyringBackend):
    """Backend de keyring en memoria, para que ningún test toque el keyring real."""

    priority = 1

    def __init__(self):
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self._store.get((service, username))

    def set_password(self, service, username, password):
        self._store[(service, username)] = password

    def delete_password(self, service, username):
        try:
            del self._store[(service, username)]
        except KeyError:
            raise PasswordDeleteError("no existe") from None


@pytest.fixture(autouse=True)
def memory_keyring():
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


@pytest.fixture(autouse=True)
def fresh_model_listing():
    """El listado de modelos se cachea por proceso: cada test arranca sin él."""
    from estudia.ai import gemini

    gemini._LISTED_MODELS.clear()
    yield
    gemini._LISTED_MODELS.clear()
