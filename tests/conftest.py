import keyring
import pytest

# Variables que deciden dónde quedan los datos de la app (platformdirs) en cada sistema.
DATA_ENV = ("HOME", "USERPROFILE", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
            "XDG_STATE_HOME", "APPDATA", "LOCALAPPDATA")
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


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path_factory, monkeypatch):
    """Ningún test toca los datos reales: HOME y las carpetas XDG/AppData van a un temporal.

    No se reemplaza config.data_path: así un subproceso (que hereda el entorno) resuelve
    la misma carpeta que el test.
    """
    home = tmp_path_factory.mktemp("home")
    for name in DATA_ENV:
        monkeypatch.setenv(name, str(home / name.lower()))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("ESTUDIA_DATA_DIR", str(home / "estudia-data"))
    yield home


@pytest.fixture(autouse=True)
def worker_backoff_cleared():
    """La espera tras una falla del worker de SymPy es por proceso: cada test arranca sin ella."""
    from estudia.generation import verify

    verify._unavailable_until = 0.0
    yield
    verify._unavailable_until = 0.0
