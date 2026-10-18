"""Bajar los PDF y diapositivas del campus, revisarlos y partir los largos.

Corre después de la sincronización, que deja en `downloading` los archivos nuevos. Cada
descarga va fuera de toda transacción: antes de tocar la red se hace commit.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import time
from pathlib import Path

from estudia.config import DOCUMENTS_DIRNAME
from estudia.material.documents import DocumentBusy, inspect_document, plan_parts
from estudia.material.files import (
    DOWNLOAD_FAILED_TEXT,
    FAIL_DOWNLOAD,
    MAX_CAMPUS_FILE_BYTES,
    NOT_READABLE_YET,
    PDF,
    PPTX,
    TOO_LARGE_TEXT,
    UNREADABLE_TEXT,
    TooLarge,
    UnreadableFile,
    safe_display_text,
    sniff_file,
)
from estudia.moodle.client import FileTooLarge, ForeignFileUrl, TokenInvalid

log = logging.getLogger(__name__)

# Descargas por sincronización: el resto queda `downloading` para la próxima.
DOWNLOADS_PER_RUN = 10
# Intentos (uno por sincronización) antes de dejar un archivo `failed`. "Reintentar las que
# fallaron" (`pipeline.retry_failed`) lo vuelve a poner para bajar, de cero.
MAX_DOWNLOAD_ATTEMPTS = 3

# kind del material -> (tipo por bytes que se espera, extensión en disco)
_DOC_KINDS = {"pdf": (PDF, ".pdf"), "slides": (PPTX, ".pptx")}
_READY = ("pending", "split")
# Lo que deja una descarga a medias: `{id}.bin` y el temporal `.{id}.bin.<azar>.part`.
_LEFTOVER = re.compile(r"^(?:(\d+)\.bin|\.(\d+)\.bin\..*\.part|\..*\.part|.*\.bin)$")


def fetch_documents(conn: sqlite3.Connection, client, dest_dir: Path,
                    limit: int = DOWNLOADS_PER_RUN) -> int:
    """Baja hasta `limit` documentos `downloading`. Devuelve cuántos quedaron listos.

    Listo es `pending` (para la tubería) o `split` (partido en tramos `pending`). Primero van
    los que menos fallaron, y después por id: los rotos no tapan a los nuevos. Cualquier error
    (red, disco, lectura) lo deja `downloading` para la próxima sincronización, y a los
    MAX_DOWNLOAD_ATTEMPTS queda `failed`; con la lectura ocupada se reintenta sin contar.
    Solo `TokenInvalid("invalidtoken")` (el token ya no sirve) se propaga.
    """
    _sweep_leftovers(conn, dest_dir)
    if limit <= 0:
        return 0
    rows = conn.execute(
        "SELECT id, kind, title, section_id, remote_url FROM material"
        " WHERE status='downloading' AND parent_id IS NULL"
        " ORDER BY COALESCE(download_attempts, 0), id LIMIT ?",
        (limit,),
    ).fetchall()
    if not rows:
        return 0
    dest_dir.mkdir(parents=True, exist_ok=True)
    ready = 0
    for row in rows:
        conn.commit()   # nada abierto mientras se baja
        status = _fetch_one(conn, client, dest_dir, row)
        conn.commit()
        if status in _READY:
            ready += 1
    return ready


def _sweep_leftovers(conn, dest_dir: Path) -> None:
    """Borra `.bin` y `.part` sueltos (de una corrida cortada) que no son de un `downloading`."""
    if not dest_dir.is_dir():
        return
    in_use = {str(r[0]) for r in conn.execute("SELECT id FROM material WHERE status='downloading'")}
    for path in dest_dir.iterdir():
        match = _LEFTOVER.match(path.name)
        if match is None or not path.is_file():
            continue
        owner = match.group(1) or match.group(2)
        if owner is not None and owner in in_use:
            continue
        try:
            path.unlink()
        except OSError:
            log.warning("No se pudo borrar un archivo a medias de la carpeta de documentos")


def _set(conn, material_id: int, status: str, error: str | None = None, **fields) -> str:
    """Cambia el estado (solo si seguía `downloading`) y los campos dados. Devuelve el estado."""
    columns = ", ".join(f"{name}=?" for name in fields)
    conn.execute(
        f"UPDATE material SET status=?, error=?, updated_at=?{', ' + columns if columns else ''}"
        " WHERE id=? AND status='downloading'",
        (status, error, int(time.time()), *fields.values(), material_id),
    )
    return status


def _fetch_one(conn, client, dest_dir: Path, row) -> str:
    material_id = row["id"]
    expected = _DOC_KINDS.get(row["kind"])
    if expected is None or not row["remote_url"]:
        # No debería pasar: la sincronización solo deja `downloading` PDF y diapositivas del campus.
        return _set(conn, material_id, "unsupported", NOT_READABLE_YET, kind="file")
    sniffed_kind, suffix = expected
    download = dest_dir / f"{material_id}.bin"
    final = dest_dir / f"{material_id}{suffix}"
    try:
        client.download_file(row["remote_url"], download, max_bytes=MAX_CAMPUS_FILE_BYTES)
        return _store(conn, row, download, final, sniffed_kind)
    except TokenInvalid as exc:
        if str(exc) == "invalidtoken":
            raise
        # `accessexception` en un archivo es un permiso de ese archivo, no la sesión.
        return _retry_later(conn, material_id, exc, download, final)
    except FileTooLarge:
        return _set(conn, material_id, "too_large", TOO_LARGE_TEXT)
    except ForeignFileUrl:
        return _set(conn, material_id, "unsupported", NOT_READABLE_YET, kind="file")
    except DocumentBusy as exc:
        # Los lugares para leer estaban ocupados: no es culpa del archivo y no cuenta.
        return _retry_later(conn, material_id, exc, download, final, count=False)
    except Exception as exc:  # noqa: BLE001 - un archivo raro no traba la cola
        return _retry_later(conn, material_id, exc, download, final)


def _retry_later(conn, material_id: int, exc: Exception, *files: Path, count: bool = True) -> str:
    """Deshace lo de este material y borra sus archivos. Queda `downloading` para la próxima,
    o `failed` si ya falló MAX_DOWNLOAD_ATTEMPTS veces."""
    conn.rollback()   # solo lo de este material: antes de bajarlo se hizo commit
    for path in files:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    # Solo el tipo: el mensaje podría traer la URL o una ruta.
    log.warning("No se pudo bajar o leer el material %s (%s)", material_id, type(exc).__name__)
    if not count:
        return "downloading"
    conn.execute(
        "UPDATE material SET download_attempts = COALESCE(download_attempts, 0) + 1"
        " WHERE id=? AND status='downloading'", (material_id,))
    attempts = conn.execute(
        "SELECT download_attempts FROM material WHERE id=?", (material_id,)).fetchone()[0]
    if attempts is not None and attempts >= MAX_DOWNLOAD_ATTEMPTS:
        return _set(conn, material_id, "failed", DOWNLOAD_FAILED_TEXT, fail_kind=FAIL_DOWNLOAD)
    return "downloading"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _store(conn, row, download: Path, final: Path, sniffed_kind: str) -> str:
    """Confirma el tipo por los bytes, lo deja con su nombre final, lo revisa y lo parte."""
    material_id = row["id"]
    if sniff_file(download) != sniffed_kind:
        download.unlink(missing_ok=True)
        return _set(conn, material_id, "unreadable", UNREADABLE_TEXT)
    os.replace(download, final)
    sha = _sha256(final)
    try:
        info = inspect_document(final, sniffed_kind)
    except TooLarge:
        final.unlink(missing_ok=True)
        return _set(conn, material_id, "too_large", TOO_LARGE_TEXT, file_sha256=sha)
    except UnreadableFile:
        final.unlink(missing_ok=True)
        return _set(conn, material_id, "unreadable", UNREADABLE_TEXT, file_sha256=sha)

    ref = f"{DOCUMENTS_DIRNAME}/{final.name}"   # relativa a data_dir()
    parts = plan_parts(info)
    status = _set(conn, material_id, "split" if parts else "pending", None, ref=ref,
                  file_sha256=sha, pages=info.pages, has_text=int(info.has_text))
    for part in parts:
        # El título de un capítulo sale del PDF: se limpia para mostrarlo.
        title = safe_display_text(part.title, "") or safe_display_text(
            f"{row['title']}, págs. {part.page_from}–{part.page_to}", "Documento")
        conn.execute(
            "INSERT INTO material(section_id, kind, title, ref, status, parent_id, page_from,"
            " page_to, updated_at) SELECT section_id, kind, ?, ref, 'pending', id, ?, ?, updated_at"
            " FROM material WHERE id=? AND status='split'",
            (title, part.page_from, part.page_to, material_id),
        )
    return status
