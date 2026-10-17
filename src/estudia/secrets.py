"""Tokens de Moodle y clave de IA, guardados en el keyring del sistema."""
import keyring
from keyring.errors import PasswordDeleteError

from estudia.config import SECRETS_SERVICE


def get_secret(name: str) -> str | None:
    return keyring.get_password(SECRETS_SERVICE, name)


def set_secret(name: str, value: str) -> None:
    keyring.set_password(SECRETS_SERVICE, name, value)


def delete_secret(name: str) -> None:
    try:
        keyring.delete_password(SECRETS_SERVICE, name)
    except PasswordDeleteError:
        pass  # no existía: borrar es idempotente
