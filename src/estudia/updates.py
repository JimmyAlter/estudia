"""Aviso de versión nueva: consulta el último release de GitHub, como mucho una vez por día."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

import httpx
from packaging.version import InvalidVersion, Version

from estudia.version import __version__

log = logging.getLogger(__name__)

RELEASE_API = "https://api.github.com/repos/JimmyAlter/estudia/releases/latest"
RELEASE_URL_PREFIX = "https://github.com/JimmyAlter/estudia/"
CHECK_EVERY_S = 86400
RETRY_AFTER_FAILURE_S = 3600
FAILED_KEY = "update_failed_at"
TIMEOUT_S = 5.0
CHECKED_KEY = "update_checked_at"
LATEST_KEY = "update_latest"


def default_http() -> httpx.Client:
    """Cliente sin cookies ni datos del usuario: solo el nombre y la versión de la app."""
    return httpx.Client(
        timeout=TIMEOUT_S,
        headers={"User-Agent": f"Estudia/{__version__}", "Accept": "application/vnd.github+json"},
    )


def _newer(release: dict | None) -> dict | None:
    """El release solo si es mayor que la versión instalada."""
    if not isinstance(release, dict):
        return None
    try:
        version, url = release["version"], release["url"]
        if not isinstance(url, str) or not url.startswith(RELEASE_URL_PREFIX):
            return None
        return {"version": version, "url": url} if Version(version) > Version(__version__) else None
    except (KeyError, TypeError, InvalidVersion):
        return None


def _fetch(http: httpx.Client) -> dict | None:
    """El último release como {"version", "url"}; None si GitHub devuelve algo inservible."""
    r = http.get(RELEASE_API, timeout=TIMEOUT_S)
    r.raise_for_status()
    data = r.json()
    tag = str(data["tag_name"]).strip().removeprefix("v")
    Version(tag)  # tira InvalidVersion si no es una versión
    return {"version": tag, "url": data["html_url"]}


def _cached(kv_get: Callable[[str], str | None]) -> dict | None:
    try:
        raw = kv_get(LATEST_KEY)
        return json.loads(raw) if raw else None
    except (ValueError, TypeError):
        return None


def latest_release(
    http: httpx.Client,
    now: int,
    kv_get: Callable[[str], str | None],
    kv_set: Callable[[str, str], None],
) -> dict | None:
    """{"version", "url"} del release más nuevo si supera a la versión instalada; si no, None.

    Consulta GitHub como mucho una vez por día. Ante cualquier falla devuelve lo que
    haya en caché (o None) y nunca lanza.
    """
    try:
        def recent(key: str, window: int) -> bool:
            try:
                at = int(kv_get(key) or 0)
            except ValueError:
                return False
            return bool(at) and 0 <= now - at < window

        if recent(CHECKED_KEY, CHECK_EVERY_S) or recent(FAILED_KEY, RETRY_AFTER_FAILURE_S):
            return _newer(_cached(kv_get))
        try:
            release = _fetch(http)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, InvalidVersion) as exc:
            log.info("no se pudo consultar la versión nueva: %s", type(exc).__name__)
            kv_set(FAILED_KEY, str(now))
            return _newer(_cached(kv_get))
        kv_set(LATEST_KEY, json.dumps(release))
        kv_set(CHECKED_KEY, str(now))
        return _newer(release)
    except Exception:  # noqa: BLE001 - el aviso nunca debe romper nada
        log.info("falló el aviso de versión", exc_info=True)
        return None
