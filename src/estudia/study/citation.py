"""La cita de una pregunta de un documento ("Apunte 3, pág. 14") y el enlace a su página del campus.

El enlace es siempre la página del módulo en el campus (`/mod/.../view.php?id=`), nunca la URL
del archivo (`pluginfile.php`), que solo sirve con el token.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping

from estudia.catalog import get_site

SITE_KEY = "site_id"   # kv: el campus elegido (lo escribe `/api/site`)
DOCUMENT_KINDS = ("pdf", "slides")

# Para citar: el título que se cita es el del archivo en el campus (en un tramo, el del padre),
# y el enlace va al módulo del padre. Va con `material m` y la unión de abajo.
SOURCE_JOIN = "LEFT JOIN material p ON p.id = m.parent_id"
SOURCE_COLUMNS = (
    "m.kind AS source_kind, COALESCE(p.title, m.title) AS source_title, m.cmid AS source_cmid,"
    " COALESCE(p.module_kind, m.module_kind) AS source_module_kind,"
    " COALESCE(p.module_id, m.module_id) AS source_module_id"
)

_MODULE_PATHS = {"resource": "/mod/resource/view.php", "folder": "/mod/folder/view.php"}
_PAGE_PATH = "/mod/page/view.php"


def source_label(kind: str | None, title: str | None, source_seconds: int | None,
                 source_page: int | None) -> str | None:
    """"Apunte 3, pág. 14" o "Clase 2, diap. 5"; sin página, el título. None en un video (la
    interfaz arma su cita con el segundo) o si el documento no tiene título."""
    if kind not in DOCUMENT_KINDS:
        return None
    name = (title or "").strip()
    if not name:
        return None
    if source_page is None:
        return name
    unit = "pág." if kind == "pdf" else "diap."
    return f"{name}, {unit} {source_page}"


def _positive_int(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _field(row, key: str):
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def campus_url(site_url: str | None, row: Mapping) -> str | None:
    """La página del módulo en el campus: un recurso o una carpeta por `module_id`, una página
    (de donde sale un video) por `cmid`. `row` trae `cmid`, `module_kind` y `module_id`; en
    un tramo, los del padre. None sin campus o sin módulo."""
    if not site_url:
        return None
    module_path = _MODULE_PATHS.get(_field(row, "module_kind"))
    if module_path is not None:
        module_id = _positive_int(_field(row, "module_id"))
        return f"{site_url}{module_path}?id={module_id}" if module_id else None
    cmid = _positive_int(_field(row, "cmid"))
    return f"{site_url}{_PAGE_PATH}?id={cmid}" if cmid else None


def site_url(conn: sqlite3.Connection, lookup: Callable | None = None) -> str | None:
    """URL base del campus elegido, solo si es http(s). `lookup`: de dónde sale el campus
    (por defecto, el catálogo)."""
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (SITE_KEY,)).fetchone()
    if not row or not row[0]:
        return None
    try:
        url = (lookup or get_site)(row[0]).url.rstrip("/")
    except KeyError:
        return None
    return url if url.startswith(("https://", "http://")) else None


def citation(base_url: str | None, row: Mapping) -> tuple[str | None, str | None]:
    """(cita, enlace al campus) de una pregunta leída con SOURCE_COLUMNS, `source_seconds` y
    `source_page`. Un video no tiene ninguno de los dos."""
    label = source_label(row["source_kind"], row["source_title"], row["source_seconds"],
                         row["source_page"])
    if label is None:
        return None, None
    url = campus_url(base_url, {"cmid": row["source_cmid"], "module_kind": row["source_module_kind"],
                                "module_id": row["source_module_id"]})
    return label, url
