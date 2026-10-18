"""Sincronización incremental de cursos, secciones, materiales y exámenes.

Solo usa funciones de lectura del web service.
"""

import logging
import sqlite3
import time
from dataclasses import dataclass

from estudia.moodle.client import MoodleClient, MoodleError, TokenInvalid
from estudia.moodle.pages import youtube_ids

log = logging.getLogger(__name__)


@dataclass
class SyncResult:
    new_materials: int = 0
    new_sections: int = 0


def _visible(obj: dict) -> bool:
    """Visible si `uservisible` existe y es verdadero; si no existe, `visible == 1`."""
    if "uservisible" in obj:
        return bool(obj["uservisible"])
    return obj.get("visible") == 1


def sync_all(conn: sqlite3.Connection, client: MoodleClient, site_id: str) -> SyncResult:
    """Sincroniza todo o nada: si algo falla se deshace y la base queda como estaba.

    Primero se baja todo del campus a memoria (solo lecturas en la base, sin el candado de
    escritura de SQLite); después se escribe todo en una sola transacción corta.
    """
    try:
        return _sync(conn, client, site_id)
    except BaseException:
        conn.rollback()
        raise


@dataclass
class _Fetched:
    """Todo lo que se bajó del campus, antes de escribir nada."""

    info: dict
    courses: list
    quizzes: dict
    contents: dict            # curso -> secciones de core_course_get_contents
    pages: dict               # cmid -> ids de YouTube (solo las pages que había que leer)
    assignments: dict | None  # None: fuente no disponible
    events: tuple | None      # (eventos, completo); None: fuente no disponible


def _sync(conn: sqlite3.Connection, client: MoodleClient, site_id: str) -> SyncResult:
    ahora = int(time.time())
    if conn.in_transaction:
        conn.commit()   # no llevar un candado abierto de antes a la fase de red
    fetched = _fetch(conn, client, ahora)
    return _write(conn, client.site_url, site_id, fetched, ahora)


def _fetch(conn: sqlite3.Connection, client: MoodleClient, ahora: int) -> _Fetched:
    """Fase de red: llamadas al campus y solo lecturas en la base (no abre transacción)."""
    info = client.call("core_webservice_get_site_info")
    cursos = client.call("core_enrol_get_users_courses", userid=info["userid"])
    ids = [c["id"] for c in cursos]
    quizzes: dict[int, dict] = {}
    if ids:
        respuesta = client.call("mod_quiz_get_quizzes_by_courses", courseids=ids)
        quizzes = {q["coursemodule"]: q for q in respuesta.get("quizzes", [])}
    contents: dict[int, list] = {}
    pages: dict[int, list[str]] = {}
    for curso_id in ids:
        contents[curso_id] = client.call("core_course_get_contents", courseid=curso_id)
        for sec in contents[curso_id]:
            if not _visible(sec):
                continue
            for mod in sec.get("modules", []):
                if _visible(mod) and mod.get("modname") == "page":
                    found = _fetch_page(conn, client, mod)
                    if found is not None:
                        pages[mod["id"]] = found
    assignments = events = None
    if ids:
        assignments = _fetch_assignments(client, ids)
        events = _fetch_events(client, ahora)
    return _Fetched(info, cursos, quizzes, contents, pages, assignments, events)


def _write(conn: sqlite3.Connection, site_url: str, site_id: str, f: _Fetched, ahora: int) -> SyncResult:
    """Fase de escritura: todo en una transacción corta, sin red."""
    resultado = SyncResult()
    conn.execute(
        "INSERT INTO site(id, url, userid, fullname) VALUES(?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET url=excluded.url, userid=excluded.userid, "
        "fullname=excluded.fullname",
        (site_id, site_url, f.info.get("userid"), f.info.get("fullname")),
    )
    for c in f.courses:
        conn.execute(
            "INSERT INTO course(id, site_id, fullname, shortname) VALUES(?,?,?,?) "
            "ON CONFLICT(site_id, id) DO UPDATE SET fullname=excluded.fullname, "
            "shortname=excluded.shortname",
            (c["id"], site_id, c.get("fullname"), c.get("shortname")),
        )
    ids = [c["id"] for c in f.courses]
    for curso_id in ids:
        for sec in f.contents.get(curso_id, []):
            existia = conn.execute("SELECT 1 FROM section WHERE id=?", (sec["id"],)).fetchone()
            conn.execute(
                "INSERT INTO section(id, course_id, num, name, visible) VALUES(?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, num=excluded.num, "
                "visible=excluded.visible",
                (sec["id"], curso_id, sec.get("section"), sec.get("name"), int(_visible(sec))),
            )
            if not existia:
                resultado.new_sections += 1
            if not _visible(sec):
                continue
            for mod in sec.get("modules", []):
                if not _visible(mod):
                    continue
                if mod.get("modname") == "page" and mod["id"] in f.pages:
                    resultado.new_materials += _write_page(conn, sec["id"], mod, f.pages[mod["id"]], ahora)
                elif mod.get("modname") == "quiz" and mod["id"] in f.quizzes:
                    _sync_exam(conn, curso_id, mod, f.quizzes[mod["id"]])

    if ids:
        _write_deadlines(conn, site_url, ids, f.assignments, f.events, ahora)

    conn.execute(
        "INSERT INTO kv(key, value) VALUES('last_sync_at', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(ahora),),
    )
    conn.commit()
    return resultado


# Códigos con los que Moodle indica que la función no está disponible para este
# usuario o servicio: la fuente se saltea y el resto de la sincronización sigue.
_SKIPPABLE_CODES = {"invalidparameter", "accessexception", "servicenotavailable"}
_EVENTS_PAGE = 50
_EVENTS_MAX_PAGES = 10
_DAY = 86400


def _skippable(exc: MoodleError) -> bool:
    # `accessexception` llega como TokenInvalid (el cliente lo mapea así), pero
    # `invalidtoken` sí tiene que cortar la sincronización.
    # Las llamadas previas (site_info, enrol, contents) ya validan el token: si
    # estuviera revocado, la sincronización abortó antes de llegar acá. Por eso
    # un `accessexception` en este punto significa "función no permitida".
    return str(exc) in _SKIPPABLE_CODES


def _site_url_or_none(site_url: str, url) -> str | None:
    """La URL solo si es del mismo campus: `site_url` + "/" como prefijo (no vale
    "campus.edu.evil.tld" ni "campus.edu@evil.tld")."""
    if isinstance(url, str) and url.startswith(site_url.rstrip("/") + "/"):
        return url
    return None


def _upsert_deadline(conn, course_id, kind: str, ext_id: int, name, due_at: int, url) -> None:
    conn.execute(
        "INSERT INTO deadline(course_id, kind, ext_id, name, due_at, url) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(kind, ext_id) DO UPDATE SET course_id=excluded.course_id, "
        "name=excluded.name, due_at=excluded.due_at, url=excluded.url",
        (course_id, kind, ext_id, name, due_at, url),
    )


def _sync_deadlines(conn, client, course_ids: list[int], now: int) -> int:
    """Baja y guarda entregas y eventos del calendario. Devuelve cuántos quedaron guardados.

    Si una de las dos fuentes no está disponible se saltea (sin tocar lo ya
    guardado de esa fuente); cualquier otro error corta la sincronización.
    """
    if not course_ids:
        return 0
    assignments = _fetch_assignments(client, course_ids)
    events = _fetch_events(client, now)
    return _write_deadlines(conn, client.site_url, course_ids, assignments, events, now)


def _write_deadlines(conn, site_url: str, course_ids: list[int], assignments, events, now: int) -> int:
    total = 0
    if assignments is not None:
        total += _write_assignments(conn, site_url, course_ids, assignments)
    if events is not None:
        total += _write_events(conn, site_url, course_ids, events, now)
    return total


def _fetch_assignments(client, course_ids: list[int]) -> dict | None:
    try:
        return client.call("mod_assign_get_assignments", courseids=course_ids)
    except MoodleError as exc:
        if not _skippable(exc):
            raise
        log.warning("Entregas del campus no disponibles (%s)", str(exc))
        return None


def _write_assignments(conn, site_url: str, course_ids: list[int], respuesta: dict) -> int:
    vistos: set[int] = set()
    presentes: set[int] = set()
    for curso in respuesta.get("courses", []):
        if curso.get("id") in course_ids:
            presentes.add(curso["id"])
        for a in curso.get("assignments", []):
            # `mod_assign_get_assignments` no devuelve `uservisible`: Moodle ya filtra
            # por visibilidad para el usuario, así que no hay nada más que chequear.
            due = a.get("duedate") or a.get("cutoffdate")
            if not due:
                continue
            vistos.add(a["id"])
            _upsert_deadline(
                conn, curso.get("id", a.get("course")), "assign", a["id"], a.get("name"), due,
                f"{site_url}/mod/assign/view.php?id={a['cmid']}" if a.get("cmid") else None,
            )
    if respuesta.get("warnings") or not presentes:
        # Respuesta dudosa (avisos del servidor) o sin cursos: no se borra nada.
        return len(vistos)
    marcas = ",".join("?" * len(presentes))
    for fila in conn.execute(
        f"SELECT ext_id FROM deadline WHERE kind='assign' AND course_id IN ({marcas})",
        sorted(presentes),
    ).fetchall():
        if fila["ext_id"] not in vistos:
            conn.execute("DELETE FROM deadline WHERE kind='assign' AND ext_id=?", (fila["ext_id"],))
    return len(vistos)


def _events_window(now: int) -> tuple[int, int]:
    return now - 7 * _DAY, now + 180 * _DAY


def _fetch_events(client, now: int) -> tuple[list[dict], bool] | None:
    """(eventos, completo) del calendario; None si la fuente no está disponible."""
    desde, hasta = _events_window(now)
    eventos: list[dict] = []
    completo = False
    despues = None
    for _ in range(_EVENTS_MAX_PAGES):
        params = {"timesortfrom": desde, "timesortto": hasta, "limitnum": _EVENTS_PAGE}
        if despues is not None:
            params["aftereventid"] = despues
        try:
            pagina = client.call("core_calendar_get_action_events_by_timesort", **params)
        except MoodleError as exc:
            if not _skippable(exc):
                raise
            log.warning("Eventos del campus no disponibles (%s)", str(exc))
            return None
        lote = pagina.get("events", [])
        eventos.extend(lote)
        if len(lote) < _EVENTS_PAGE:
            completo = True
            break
        despues = lote[-1]["id"]
    return eventos, completo


def _write_events(conn, site_url: str, course_ids: list[int], fetched: tuple, now: int) -> int:
    eventos, completo = fetched
    desde, hasta = _events_window(now)
    vistos: set[int] = set()
    for e in eventos:
        if e.get("modulename") in ("quiz", "assign"):
            continue
        due = e.get("timesort") or e.get("timestart")
        if not due:
            continue
        curso = (e.get("course") or {}).get("id") or e.get("courseid") or 0
        if curso and curso not in course_ids:
            continue  # evento de un curso que no se sincroniza
        vistos.add(e["id"])
        _upsert_deadline(conn, curso or None, "event", e["id"],
                         e.get("name"), due, _site_url_or_none(site_url, e.get("url")))
    # Solo se borra dentro de la ventana pedida y si se leyó completa. Una primera
    # página vacía con filas ya guardadas en la ventana se toma como sospechosa.
    filas = conn.execute(
        "SELECT ext_id FROM deadline WHERE kind='event' AND due_at BETWEEN ? AND ?",
        (desde, hasta),
    ).fetchall()
    if completo and (eventos or not filas):
        for fila in filas:
            if fila["ext_id"] not in vistos:
                conn.execute("DELETE FROM deadline WHERE kind='event' AND ext_id=?", (fila["ext_id"],))
    return len(vistos)


def _page_modified(mod: dict) -> int:
    """Última modificación del contenido de la page según Moodle (0 si no viene)."""
    info = mod.get("contentsinfo") or {}
    times = [info.get("lastmodified")] + [c.get("timemodified") for c in mod.get("contents", [])]
    return max((t for t in times if isinstance(t, int)), default=0)


def _needs_fetch(existente, mod: dict) -> bool:
    """Una page se lee si es nueva, o si quedó sin video y el profe la editó después."""
    return existente is None or (
        existente["status"] == "unsupported" and _page_modified(mod) > (existente["updated_at"] or 0)
    )


def _fetch_page(conn, client, mod: dict) -> list[str] | None:
    """Ids de YouTube de una page que hay que (re)leer; None si no hace falta leerla.

    Solo lee la base. Una page sin video se vuelve a mirar solo si el profe la editó después
    de la última vez (así se detecta un video agregado más tarde).
    """
    existente = conn.execute(
        "SELECT id, status, updated_at FROM material WHERE cmid=?", (mod["id"],)
    ).fetchone()
    if not _needs_fetch(existente, mod):
        return None
    archivos = [c for c in mod.get("contents", []) if c.get("fileurl")]
    if not archivos:
        return []
    try:
        html = client.fetch_file(archivos[0]["fileurl"])
    except TokenInvalid:
        raise
    except MoodleError:
        if existente is None:
            raise
        # Re-chequeo de una page ya conocida: si falla, sigue sin video y no
        # se cae toda la sincronización. Sin el mensaje (podría traer la URL).
        log.warning("No se pudo volver a leer una page sin video")
        html = b""
    return youtube_ids(html.decode("utf-8", "replace"))


def _write_page(conn, section_id: int, mod: dict, ids: list[str], ahora: int) -> int:
    """Crea o actualiza el material de una page ya leída. Devuelve 1 si quedó un video nuevo."""
    existente = conn.execute(
        "SELECT id, status, updated_at FROM material WHERE cmid=?", (mod["id"],)
    ).fetchone()
    if not _needs_fetch(existente, mod):
        return 0   # otra sincronización la resolvió mientras se leía
    if ids:
        kind, ref, status = "youtube", ids[0], "pending"
    else:
        kind, ref, status = "page", None, "unsupported"
    if existente is not None:
        conn.execute(
            "UPDATE material SET kind=?, ref=?, status=?, title=?, updated_at=? WHERE id=?",
            (kind, ref, status, mod.get("name"), ahora, existente["id"]),
        )
        return 1 if ids else 0
    conn.execute(
        "INSERT INTO material(section_id, cmid, kind, title, ref, status, updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (section_id, mod["id"], kind, mod.get("name"), ref, status, ahora),
    )
    return 1


def _sync_exam(conn, curso_id: int, mod: dict, quiz: dict) -> None:
    conn.execute(
        "INSERT INTO exam(course_id, cmid, name, opens_at, closes_at, attempts, "
        "time_limit_s, max_grade) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(cmid) DO UPDATE SET name=excluded.name, opens_at=excluded.opens_at, "
        "closes_at=excluded.closes_at, attempts=excluded.attempts, "
        "time_limit_s=excluded.time_limit_s, max_grade=excluded.max_grade",
        (curso_id, mod["id"], quiz.get("name", mod.get("name")), quiz.get("timeopen"),
         quiz.get("timeclose"), quiz.get("attempts"), quiz.get("timelimit"), quiz.get("grade")),
    )
