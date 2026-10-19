"""Archivos que llegan de afuera: tipo por bytes, nombres seguros y fotos sin EXIF.

El tipo se decide siempre por el contenido, nunca por la extensión ni por el mimetype.
"""

import io
import re
import unicodedata
import zipfile
from pathlib import Path

from PIL import Image, ImageOps

PDF, PPTX, JPEG, PNG = "pdf", "pptx", "jpeg", "png"

MAX_CAMPUS_FILE_BYTES = 50 * 1024 * 1024
MAX_PAST_EXAM_BYTES = 20 * 1024 * 1024
MAX_PAST_EXAMS_PER_EXAM = 5

IMAGE_MAX_PIXELS = 40_000_000
IMAGE_MAX_SIDE = 4000

ZIP_MAX_ENTRIES = 5000
ZIP_MAX_TOTAL_BYTES = 300 * 1024 * 1024
ZIP_MAX_RATIO = 200
_ZIP_RATIO_MIN_SIZE = 1024 * 1024

# Textos fijos de la spec para un archivo del campus que no se procesa, y el motivo de un
# archivo que no se pudo bajar (`material.fail_kind`).
TOO_LARGE_TEXT = "Archivo demasiado grande"
UNREADABLE_TEXT = "No se pudo leer"
NOT_READABLE_YET = "No se puede leer todavía"
DOWNLOAD_FAILED_TEXT = "No se pudo bajar del campus. Podés reintentarlo más tarde."
FAIL_DOWNLOAD = "download"

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Lo único de `img.info` que se conserva al volver a guardar: la transparencia (de un PNG con
# paleta). Todo lo demás (EXIF, comentario COM, XMP, chunks de texto, ICC, MPF) se tira.
_KEPT_INFO_KEYS = ("transparency",)
# Formatos de Pillow que se aceptan por cada tipo. MPO son las fotos de algunos celulares:
# varios JPEG unidos; se guarda solo el primer cuadro.
_ACCEPTED_FORMATS = {JPEG: ("JPEG", "MPO"), PNG: ("PNG",)}
_DISPLAY_NAME_MAX = 120


class UnreadableFile(ValueError):
    """El archivo no se puede leer (roto, hostil o de un tipo que no aceptamos)."""


class TooLarge(ValueError):
    """El archivo pasa los topes de tamaño."""


def sniff(head: bytes) -> str | None:
    """Tipo según los primeros 1.024 bytes: pdf, jpeg, png, "zip" o None."""
    head = head[:1024]
    if b"%PDF-" in head:
        return PDF
    if head.startswith(b"\xff\xd8\xff"):
        return JPEG
    if head.startswith(_PNG_SIGNATURE):
        return PNG
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    return None


def sniff_file(path: Path) -> str | None:
    """Como `sniff`, pero un zip solo es `pptx` si pasa `check_pptx_zip`."""
    with open(path, "rb") as fh:
        kind = sniff(fh.read(1024))
    if kind != "zip":
        return kind
    try:
        check_pptx_zip(path)
    except UnreadableFile:
        return None
    return PPTX


def check_pptx_zip(path: Path) -> None:
    """Revisa el índice del zip sin descomprimir nada."""
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
    except (zipfile.BadZipFile, OSError, ValueError, NotImplementedError) as exc:
        raise UnreadableFile("No se pudo leer") from exc
    if len(infos) > ZIP_MAX_ENTRIES:
        raise UnreadableFile("No se pudo leer")
    names = {info.filename for info in infos}
    if "[Content_Types].xml" not in names or "ppt/presentation.xml" not in names:
        raise UnreadableFile("No se pudo leer")
    total = 0
    for info in infos:
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or ".." in name.split("/"):
            raise UnreadableFile("No se pudo leer")
        total += info.file_size
        if total > ZIP_MAX_TOTAL_BYTES:
            raise UnreadableFile("No se pudo leer")
        if info.file_size > _ZIP_RATIO_MIN_SIZE and (
            info.file_size / max(info.compress_size, 1) > ZIP_MAX_RATIO
        ):
            raise UnreadableFile("No se pudo leer")


# Control, formato (RLO, ancho cero), surrogates sueltos y separadores de línea/párrafo.
_HIDDEN_CATEGORIES = ("Cc", "Cf", "Cs", "Zl", "Zp")


def _visible_text(raw: str) -> str:
    """NFC y sin los caracteres que no se ven o que dan vuelta el texto."""
    text = unicodedata.normalize("NFC", raw)
    return "".join(ch for ch in text if unicodedata.category(ch) not in _HIDDEN_CATEGORIES)


def safe_display_name(raw: str | None, fallback: str) -> str:
    """Nombre apto para mostrar: sin carpetas, sin control, normalizado y corto."""
    if not raw:
        return fallback
    name = _visible_text(re.split(r"[/\\]", raw)[-1])
    name = name.strip(" .")[:_DISPLAY_NAME_MAX].strip(" .")
    return name or fallback


def safe_display_text(raw: str | None, fallback: str) -> str:
    """Texto apto para mostrar (un título de capítulo): como `safe_display_name`, pero conserva
    las barras ("Unidad 1/2") porque no es un nombre de archivo."""
    if not raw:
        return fallback
    text = _visible_text(raw).strip()[:_DISPLAY_NAME_MAX].strip()
    return text or fallback


def _drop_icc(img: Image.Image) -> Image.Image:
    """Pasa a sRGB si trae perfil ICC (cuando se puede) y siempre lo saca de la salida.

    Un perfil ICC es metadata opaca que no queremos conservar; los colores se
    preservan convirtiendo a sRGB antes de tirarlo. Si ImageCms falla o el modo
    no es RGB, se descarta el perfil sin convertir.
    """
    icc = img.info.pop("icc_profile", None)
    if icc and img.mode == "RGB":
        try:
            from PIL import ImageCms

            src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            img = ImageCms.profileToProfile(
                img, src, ImageCms.createProfile("sRGB"), outputMode="RGB"
            )
        except Exception:  # perfil roto o ImageCms ausente: alcanza con descartarlo
            pass
    img.info.pop("icc_profile", None)
    return img


def _only_pixels(img: Image.Image) -> Image.Image:
    """Deja en `info` solo lo que hace falta para los píxeles (ver `_KEPT_INFO_KEYS`)."""
    img.info = {k: v for k, v in img.info.items() if k in _KEPT_INFO_KEYS}
    return img


def reencode_image(data: bytes, kind: str) -> bytes:
    """Vuelve a guardar la foto sin EXIF, ICC, comentarios ni texto, con el lado mayor <= 4000.

    Una foto MPO (de celular) se guarda como un JPEG con solo su primer cuadro.

    No toca `Image.MAX_IMAGE_PIXELS`: el tope de píxeles se revisa acá, con el
    tamaño declarado, antes de decodificar nada.
    """
    try:
        img = Image.open(io.BytesIO(data))
        if img.format not in _ACCEPTED_FORMATS.get(kind, ()):
            raise UnreadableFile("No se pudo leer")
        if img.size[0] * img.size[1] > IMAGE_MAX_PIXELS:
            raise UnreadableFile("No se pudo leer")
        img.load()
        img = ImageOps.exif_transpose(img)
        if max(img.size) > IMAGE_MAX_SIDE:
            img.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
        out = io.BytesIO()
        if kind == JPEG:
            img = _only_pixels(_drop_icc(img.convert("RGB")))
            img.save(out, "JPEG", quality=90)
        else:
            img = _only_pixels(_drop_icc(img))
            img.save(out, "PNG")
        return out.getvalue()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning, OSError) as exc:
        raise UnreadableFile("No se pudo leer") from exc


# La copia local de un documento que se sube a Gemini (`ai.gemini`): va en la carpeta de datos
# (no en el temporal del sistema) con este prefijo, y el arranque borra las que quedaron de una
# corrida cortada (`sweep_upload_copies`).
UPLOAD_COPY_PREFIX = ".gemini-subida-"


def sweep_upload_copies(base_dir: Path) -> None:
    """Borra las copias para subir que quedaron de una corrida cortada. Solo al arrancar:
    ninguna subida está en curso."""
    try:
        leftovers = [p for p in Path(base_dir).glob(f"{UPLOAD_COPY_PREFIX}*") if p.is_file()]
    except OSError:
        return
    for path in leftovers:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass   # se reintenta en el próximo arranque
