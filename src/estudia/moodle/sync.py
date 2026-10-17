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
    """Sincroniza todo o nada: si algo falla se deshace y la base queda como estaba."""
    try:
        return _sync(conn, client, site_id)
    except BaseException:
        conn.rollback()
        raise


def _sync(conn: sqlite3.Connection, client: MoodleClient, site_id: str) -> SyncResult:
    resultado = SyncResult()
    ahora = int(time.time())

    info = client.call("core_webservice_get_site_info")
    conn.execute(
        "INSERT INTO site(id, url, userid, fullname) VALUES(?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET url=excluded.url, userid=excluded.userid, "
        "fullname=excluded.fullname",
        (site_id, client.site_url, info.get("userid"), info.get("fullname")),
    )
    cursos = client.call("core_enrol_get_users_courses", userid=info["userid"])
    for c in cursos:
        conn.execute(
            "INSERT INTO course(id, site_id, fullname, shortname) VALUES(?,?,?,?) "
            "ON CONFLICT(site_id, id) DO UPDATE SET fullname=excluded.fullname, "
            "shortname=excluded.shortname",
            (c["id"], site_id, c.get("fullname"), c.get("shortname")),
        )

    ids = [c["id"] for c in cursos]
    quizzes: dict[int, dict] = {}
    if ids:
        respuesta = client.call("mod_quiz_get_quizzes_by_courses", courseids=ids)
        quizzes = {q["coursemodule"]: q for q in respuesta.get("quizzes", [])}

    for curso_id in ids:
        for sec in client.call("core_course_get_contents", courseid=curso_id):
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
                if mod.get("modname") == "page":
                    resultado.new_materials += _sync_page(conn, client, sec["id"], mod, ahora)
                elif mod.get("modname") == "quiz" and mod["id"] in quizzes:
                    _sync_exam(conn, curso_id, mod, quizzes[mod["id"]])

    conn.execute(
        "INSERT INTO kv(key, value) VALUES('last_sync_at', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(ahora),),
    )
    conn.commit()
    return resultado


def _page_modified(mod: dict) -> int:
    """Última modificación del contenido de la page según Moodle (0 si no viene)."""
    info = mod.get("contentsinfo") or {}
    times = [info.get("lastmodified")] + [c.get("timemodified") for c in mod.get("contents", [])]
    return max((t for t in times if isinstance(t, int)), default=0)


def _sync_page(conn, client, section_id: int, mod: dict, ahora: int) -> int:
    """Crea el material de una page si todavía no existe. Devuelve 1 si lo creó.

    Una page sin video se vuelve a mirar solo si el profe la editó después de la
    última vez (así se detecta un video agregado más tarde).
    """
    existente = conn.execute(
        "SELECT id, status, updated_at FROM material WHERE cmid=?", (mod["id"],)
    ).fetchone()
    if existente is not None and (
        existente["status"] != "unsupported" or _page_modified(mod) <= (existente["updated_at"] or 0)
    ):
        return 0
    archivos = [c for c in mod.get("contents", []) if c.get("fileurl")]
    ids: list[str] = []
    if archivos:
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
        ids = youtube_ids(html.decode("utf-8", "replace"))
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
