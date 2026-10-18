"""Parciales viejos que carga el estudiante desde su compu ("Cargá tus parciales viejos").

Cada archivo es hostil hasta que se demuestre lo contrario:
- el tipo sale de los bytes (`sniff`), nunca del nombre ni del mimetype;
- un PDF tiene que empezar con `%PDF-` (solo se admiten espacios antes): un HTML con un PDF
  adentro no pasa;
- una foto se vuelve a codificar sin EXIF (ni GPS) ni comentarios, con el tope de píxeles, en
  `sandbox.run_limited`;
- un PDF se reescribe con pdfium sin metadatos (/Info y XMP: autor, programa, título) y se
  revisa, los dos en `sandbox.run_limited` (otro proceso, con tiempo y memoria limitados); si no
  se puede reescribir, no se acepta;
- el nombre en disco sale del id de la fila; el nombre que se muestra pasa por `safe_display_name`;
- la ruta y el sha256 nunca salen en lo que devuelve este módulo.

Todo queda con `origin='parcial_propio'`: nunca sale de la compu.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
from pathlib import Path
from secrets import token_hex

from estudia.config import PAST_EXAMS_DIRNAME
from estudia.material.documents import (
    PARSE_TIMEOUT_S,
    PAST_EXAM_MAX_PAGES,
    DocumentBusy,
    TooManyPages,
    inspect_document,
    strip_pdf_metadata,
)
from estudia.material.files import (
    JPEG,
    MAX_PAST_EXAM_BYTES,
    MAX_PAST_EXAMS_PER_EXAM,
    PDF,
    PNG,
    UnreadableFile,
    reencode_image,
    safe_display_name,
    sniff,
)
from estudia.material.sandbox import SandboxBusy, SandboxFailed, SandboxTimeout, run_limited
from estudia.study.exams import ENROLLMENT

logger = logging.getLogger(__name__)

ORIGIN = "parcial_propio"

TOO_BIG_TEXT = "Ese archivo pesa más de 20 MB. Probá con una foto de cada hoja."
TOO_MANY_PAGES_TEXT = "Ese parcial tiene más de 30 páginas. Cargá solo las del examen."
WRONG_TYPE_TEXT = "Solo se pueden cargar PDF o fotos (JPG o PNG)."
LIMIT_TEXT = "Ya cargaste 5 parciales para este examen."
DUPLICATE_TEXT = "Ese parcial ya está cargado."
# El estado de un parcial cargado que después no se pudo leer (la pantalla dice qué hacer).
UNREADABLE_TEXT = "No se pudo leer"
# Al cargarlo: el archivo no se pudo revisar.
UPLOAD_UNREADABLE_TEXT = "No se pudo leer ese archivo. Probá con un PDF o una foto más nítida."
# Un pedido de carga roto (sin tamaño, tamaño inválido, datos inválidos): no es culpa del archivo.
UPLOAD_FAILED_TEXT = "No se pudo cargar el archivo. Probá de nuevo."
ENROLLMENT_TEXT = "Una inscripción no tiene parciales."
BUSY_TEXT = "Ahora no se pudo revisar el archivo. Probá de nuevo en un rato."

_SUFFIX = {PDF: "pdf", JPEG: "jpg", PNG: "png"}
# Lo único que se admite antes de `%PDF-` en un parcial (whitespace de PDF, sin el NUL).
_PDF_LEADING_SPACE = b" \t\r\n\f"


class PastExamError(ValueError):
    """El parcial no se guardó. `status` es el código HTTP; el mensaje, el texto para el estudiante."""

    def __init__(self, status: int, text: str):
        super().__init__(text)
        self.status = status


# El temporal de una carga en curso, en la carpeta de los parciales. Una carga cortada (la app
# se cerró a mitad) lo deja: `sweep_uploads` lo borra al arrancar.
UPLOADING_PREFIX = ".subiendo-"


def sweep_uploads(base_dir: Path) -> None:
    """Borra los temporales de cargas cortadas. Solo al arrancar: ninguna carga está en curso."""
    try:
        leftovers = [p for p in _folder(base_dir).glob(f"{UPLOADING_PREFIX}*") if p.is_file()]
    except OSError:
        return
    for path in leftovers:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("No se pudo borrar una carga de parcial a medias")


def _folder(base_dir: Path) -> Path:
    return Path(base_dir) / PAST_EXAMS_DIRNAME


def file_path(base_dir: Path, past_exam_id: int, kind: str) -> Path:
    """El archivo de un parcial: siempre `parciales/{id}.{pdf|jpg|png}`, nunca su nombre."""
    return _folder(base_dir) / f"{int(past_exam_id)}.{_SUFFIX[kind]}"


def check_can_add(conn: sqlite3.Connection, exam_id: int) -> None:
    """Solo lectura: el examen existe, no es una inscripción y tiene lugar para otro parcial.

    `LookupError` o `PastExamError` (400 o 409). La API lo corre antes de leer el cuerpo; el
    tope se vuelve a mirar de forma atómica al insertar.
    """
    row = conn.execute("SELECT kind FROM exam WHERE id = ?", (exam_id,)).fetchone()
    if row is None:
        raise LookupError(f"examen {exam_id} no existe")
    if row["kind"] == ENROLLMENT:
        raise PastExamError(400, ENROLLMENT_TEXT)
    count = conn.execute("SELECT COUNT(*) FROM past_exam WHERE exam_id = ?", (exam_id,)).fetchone()[0]
    if count >= MAX_PAST_EXAMS_PER_EXAM:
        raise PastExamError(409, LIMIT_TEXT)


def _upload_kind(data: bytes) -> str | None:
    """El tipo por los bytes, más estricto que `sniff` con los PDF: tienen que empezar con
    `%PDF-` (los del campus siguen con `sniff`, que lo busca en los primeros 1.024 bytes)."""
    head = data[:1024]
    kind = sniff(head)
    if kind == PDF and not head.lstrip(_PDF_LEADING_SPACE).startswith(b"%PDF-"):
        return None
    return kind


def _reencode(data: bytes, kind: str) -> bytes:
    """`reencode_image` en otro proceso, con tiempo y memoria limitados."""
    try:
        return run_limited(reencode_image, data, kind, timeout_s=PARSE_TIMEOUT_S)
    except SandboxBusy:
        raise PastExamError(503, BUSY_TEXT) from None
    except (SandboxTimeout, SandboxFailed):
        raise PastExamError(400, UPLOAD_UNREADABLE_TEXT) from None


def _strip_pdf(data: bytes) -> bytes:
    """`strip_pdf_metadata` en otro proceso. Si no se puede reescribir, el parcial no se acepta:
    nunca se guarda con sus metadatos."""
    try:
        return run_limited(strip_pdf_metadata, data, PAST_EXAM_MAX_PAGES, timeout_s=PARSE_TIMEOUT_S)
    except SandboxBusy:
        raise PastExamError(503, BUSY_TEXT) from None
    except SandboxFailed as exc:
        if str(exc) == "TooManyPages":
            raise PastExamError(413, TOO_MANY_PAGES_TEXT) from None
        raise PastExamError(400, UPLOAD_UNREADABLE_TEXT) from None
    except SandboxTimeout:
        raise PastExamError(400, UPLOAD_UNREADABLE_TEXT) from None


def _check_pdf(path: Path) -> None:
    try:
        inspect_document(path, PDF, max_pages=PAST_EXAM_MAX_PAGES)
    except TooManyPages:
        raise PastExamError(413, TOO_MANY_PAGES_TEXT) from None
    except DocumentBusy:
        raise PastExamError(503, BUSY_TEXT) from None
    except UnreadableFile:
        raise PastExamError(400, UPLOAD_UNREADABLE_TEXT) from None


def add_past_exam(
    conn: sqlite3.Connection,
    exam_id: int,
    data: bytes,
    filename: str | None,
    now: int,
    base_dir: Path,
) -> int:
    """Revisa y guarda un parcial; devuelve su id. `LookupError` si el examen no existe y
    `PastExamError` (con su código HTTP) si el archivo no se acepta."""
    check_can_add(conn, exam_id)
    if len(data) > MAX_PAST_EXAM_BYTES:
        raise PastExamError(413, TOO_BIG_TEXT)
    kind = _upload_kind(data)
    if kind not in _SUFFIX:
        raise PastExamError(415, WRONG_TYPE_TEXT)

    folder = _folder(base_dir)
    folder.mkdir(parents=True, exist_ok=True)
    # Temporal con nombre al azar en la misma carpeta: el rename final es atómico.
    tmp = folder / f"{UPLOADING_PREFIX}{token_hex(8)}.{_SUFFIX[kind]}"
    final: Path | None = None
    try:
        # El duplicado se mira en lo que subió: la reescritura del PDF no sale igual cada vez.
        sha = hashlib.sha256(data).hexdigest()
        if kind == PDF:
            # Sin metadatos (autor, programa, título, XMP) antes de tocar el disco.
            data = _strip_pdf(data)
            if len(data) > MAX_PAST_EXAM_BYTES:
                raise PastExamError(413, TOO_BIG_TEXT)
            with open(tmp, "xb") as fh:
                fh.write(data)
            _check_pdf(tmp)
        else:
            data = _reencode(data, kind)
            if len(data) > MAX_PAST_EXAM_BYTES:  # la foto vuelta a codificar también tiene tope
                raise PastExamError(413, TOO_BIG_TEXT)
            with open(tmp, "xb") as fh:
                fh.write(data)

        if kind != PDF:
            sha = hashlib.sha256(data).hexdigest()   # la foto ya vuelta a codificar
        if conn.execute("SELECT 1 FROM past_exam WHERE exam_id = ? AND sha256 = ?",
                        (exam_id, sha)).fetchone():
            raise PastExamError(409, DUPLICATE_TEXT)
        name = safe_display_name(filename, "Parcial")
        try:
            # El tope se vuelve a mirar al insertar: dos cargas a la vez no pasan de 5.
            cur = conn.execute(
                "INSERT INTO past_exam(exam_id, origin, name, kind, size, sha256, status, created_at)"
                " SELECT ?, ?, ?, ?, ?, ?, 'pending', ?"
                " WHERE (SELECT COUNT(*) FROM past_exam WHERE exam_id = ?) < ?",
                (exam_id, ORIGIN, name, kind, len(data), sha, now, exam_id, MAX_PAST_EXAMS_PER_EXAM),
            )
        except sqlite3.IntegrityError:
            conn.rollback()
            raise PastExamError(409, DUPLICATE_TEXT) from None
        if cur.rowcount == 0:
            conn.rollback()
            raise PastExamError(409, LIMIT_TEXT)
        past_exam_id = cur.lastrowid
        final = file_path(base_dir, past_exam_id, kind)
        conn.execute("UPDATE past_exam SET path = ? WHERE id = ?",
                     (f"{PAST_EXAMS_DIRNAME}/{final.name}", past_exam_id))
        os.replace(tmp, final)
        conn.commit()
        return past_exam_id
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        if final is not None:
            final.unlink(missing_ok=True)
        raise
    finally:
        tmp.unlink(missing_ok=True)


def list_past_exams(conn: sqlite3.Connection, exam_id: int) -> list[dict]:
    """Los parciales del examen, sin la ruta ni el sha256."""
    return [
        dict(r)
        for r in conn.execute(
            "SELECT id, name, kind, status, error, created_at FROM past_exam"
            " WHERE exam_id = ? ORDER BY id",
            (exam_id,),
        )
    ]


def delete_past_exam(conn: sqlite3.Connection, exam_id: int, past_exam_id: int, base_dir: Path) -> None:
    """Borra la fila y el archivo. `LookupError` si ese parcial no es de ese examen."""
    row = conn.execute("SELECT kind FROM past_exam WHERE id = ? AND exam_id = ?",
                       (past_exam_id, exam_id)).fetchone()
    if row is None:
        raise LookupError(f"parcial {past_exam_id} no existe")
    conn.execute("DELETE FROM past_exam WHERE id = ?", (past_exam_id,))
    conn.commit()
    if row["kind"] in _SUFFIX:
        try:
            file_path(base_dir, past_exam_id, row["kind"]).unlink(missing_ok=True)
        except OSError as exc:
            # La fila ya no está: el archivo suelto lo puede barrer una limpieza después.
            # Solo el tipo: el mensaje traería la ruta.
            logger.warning("No se pudo borrar el archivo de un parcial (%s)", type(exc).__name__)


def delete_rows_for_exam(conn: sqlite3.Connection, exam_id: int, base_dir: Path) -> list[Path]:
    """Borra (sin commit) las filas de los parciales del examen y devuelve sus archivos, para
    borrarlos después del commit de quien llama."""
    paths = [
        file_path(base_dir, r["id"], r["kind"])
        for r in conn.execute("SELECT id, kind FROM past_exam WHERE exam_id = ?", (exam_id,))
        if r["kind"] in _SUFFIX
    ]
    conn.execute("DELETE FROM past_exam WHERE exam_id = ?", (exam_id,))
    return paths

