"""Feed del calendario personal del campus, para suscribirse desde el celular.

La URL lleva un token de autenticación del campus: es un secreto. Vive solo en
el keyring (`feed_secret_name`) y nunca se loguea ni viaja en una excepción.
"""
import logging
from typing import Any

from estudia.moodle.client import MoodleError, MoodleUnreachable

EXPORT_TOKEN_FN = "core_calendar_get_calendar_export_token"
# Errorcodes de la llamada del token que no dicen nada del feed: token vencido
# (la sincronización se encarga) o campus en mantenimiento. Se reintenta.
TRANSIENT_CODES = {"invalidtoken", "sitemaintenance"}

log = logging.getLogger(__name__)


class FeedUnreachable(Exception):
    """No se pudo saber si el campus ofrece el feed (corte, timeout, sesión rechazada).

    No es una respuesta: lo guardado se deja como está y se prueba más tarde.
    """


def feed_secret_name(site_id: str) -> str:
    return f"moodle_calendar_feed:{site_id}"


def feed_status_key(site_id: str) -> str:
    """kv con el resultado de la última consulta del campus: "saved" | "unavailable" (no es secreto)."""
    return f"calendar_feed_status:{site_id}"


def export_url(site_url: str, userid: int, token: str) -> str:
    return (f"{site_url}/calendar/export_execute.php?userid={userid}&authtoken={token}"
            "&preset_what=all&preset_time=recentupcoming")


def campus_calendar_url(site_url: str) -> str:
    """Página del campus donde el estudiante exporta el calendario a mano (la guía)."""
    return f"{site_url}/calendar/export.php"


def _offers(info: Any, fn: str) -> bool:
    functions = info.get("functions") if isinstance(info, dict) else None
    if not isinstance(functions, list):
        return False
    return any(isinstance(f, dict) and f.get("name") == fn for f in functions)


def fetch_feed_url(client) -> str | None:
    """URL del feed del calendario, o None si el campus no la ofrece.

    Primero mira si la función está en el servicio: llamar a una que no está
    responde `accessexception`, que el cliente toma como token vencido. Nada de
    este camino toca la sesión del campus.

    - None (respuesta definitiva): la función no está en el servicio, o su
      llamada devolvió un errorcode de Moodle (también `accessexception`).
    - `FeedUnreachable`: no se pudo saber. Falló site_info (con cualquier error),
      la llamada del token no tuvo respuesta (`MoodleUnreachable`) o devolvió
      uno de `TRANSIENT_CODES`.
    """
    failed = None
    try:
        info = client.call("core_webservice_get_site_info")
    except MoodleError as exc:
        failed = type(exc).__name__
    if failed is not None:
        # Fuera del except: la excepción nueva no encadena la original.
        log.info("No se pudo consultar el campus para el feed (%s)", failed)
        raise FeedUnreachable(failed)
    if not _offers(info, EXPORT_TOKEN_FN):
        log.info("El campus no ofrece el feed del calendario")
        return None
    try:
        result = client.call(EXPORT_TOKEN_FN)
    except MoodleUnreachable:
        failed = "MoodleUnreachable"
    except MoodleError as exc:
        # El mensaje de un MoodleError que no es MoodleUnreachable es el errorcode.
        if str(exc) not in TRANSIENT_CODES:
            log.info("El campus no da el feed del calendario (%s)", type(exc).__name__)
            return None
        failed = type(exc).__name__
    if failed is not None:
        log.info("No se pudo pedir el feed del calendario (%s)", failed)
        raise FeedUnreachable(failed)
    token = result.get("token") if isinstance(result, dict) else None
    userid = info.get("userid")
    if not isinstance(token, str) or not token or not isinstance(userid, int):
        log.info("El campus no devolvió un token para el feed del calendario")
        return None
    return export_url(client.site_url.rstrip("/"), userid, token)
