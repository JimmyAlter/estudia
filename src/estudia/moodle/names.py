"""Nombres de los secretos del campus en el keyring. Sin dependencias: los usan el servidor y la demo."""


def moodle_token_name(site_id: str) -> str:
    return f"moodle_token:{site_id}"


def moodle_private_name(site_id: str) -> str:
    return f"moodle_private:{site_id}"
