"""Descarga local de un video de YouTube, para subirlo a Gemini cuando no acepta el enlace.

YouTube ahora exige resolver un desafío en JavaScript. yt-dlp lo resuelve con un
runtime externo. En el instalador de Windows se usa solo el `node` que viene
incluido. Si no, el `deno` que viene como dependencia (paquete `deno` de PyPI,
así nadie tiene que instalar nada) y, si está, también `node` (yt-dlp elige el
que sirva).
"""
from __future__ import annotations

import logging
import re
import shutil
import tempfile
from pathlib import Path

from estudia.config import BUNDLED_JS_RUNTIME, bundled_js_runtime

log = logging.getLogger(__name__)

# Las fórmulas están en las diapositivas: con 480p alcanza. El audio suma la
# explicación del docente, así que se prefiere con audio (el de menor calidad
# alcanza para la voz). Unir video y audio separados necesita ffmpeg.
FORMAT_WITH_FFMPEG = "bv*[height<=480]+wa/b[height<=480]/bv*[height<=480]/worst"
FORMAT_WITHOUT_FFMPEG = "b[height<=480]/bv*[height<=480]/worst"
VIDEO_SUFFIXES = (".mp4", ".webm", ".mkv")
MIME_TYPES = {".mp4": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska"}
# Una clase de 2 h a 480p ronda los 150 MB: más que esto no es una clase normal.
MAX_FILESIZE = 500 * 1024 * 1024
SOCKET_TIMEOUT_S = 30
DOWNLOAD_RETRIES = 3


class VideoDownloadError(Exception):
    """No se pudo bajar el video (privado, borrado, sin red o YouTube cambió algo)."""


def mime_type(path: Path) -> str:
    return MIME_TYPES.get(path.suffix.lower(), "video/mp4")


def _target(youtube_id: str, dest_dir: Path, suffix: str) -> Path:
    """Ruta final del video; el id viene del campus, así que se valida antes de usarlo."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", youtube_id):
        raise VideoDownloadError("id de video de YouTube inválido")
    path = dest_dir / f"{youtube_id}{suffix}"
    if path.resolve().parent != dest_dir.resolve():
        raise VideoDownloadError("id de video de YouTube inválido")
    return path


def cached_video(youtube_id: str, dest_dir: Path) -> Path | None:
    """El video ya bajado en una corrida anterior, si quedó."""
    for suffix in VIDEO_SUFFIXES:
        path = _target(youtube_id, dest_dir, suffix)
        if path.is_file():
            return path
    return None


def js_runtimes() -> dict[str, dict]:
    """Runtimes de JavaScript para yt-dlp: el incluido en el instalador o, si no
    hay, el deno de la dependencia y node si está."""
    bundled = bundled_js_runtime()
    if bundled is not None:
        return {BUNDLED_JS_RUNTIME: {"path": str(bundled)}}
    runtimes: dict[str, dict] = {}
    try:
        from deno import find_deno_bin

        runtimes["deno"] = {"path": find_deno_bin()}
    except Exception:  # noqa: BLE001 - sin el paquete, yt-dlp busca deno en el PATH
        runtimes["deno"] = {}
    if shutil.which("node"):
        runtimes["node"] = {}
    return runtimes


class _QuietLogger:
    """Los mensajes de yt-dlp van al log en modo debug, nunca a la consola."""

    def debug(self, msg):
        log.debug("yt-dlp: %s", msg)

    info = warning = debug

    def error(self, msg):
        log.debug("yt-dlp (error): %s", msg)


def download_video(youtube_id: str, dest_dir: Path) -> Path:
    """Baja el video a `dest_dir/<id>.<ext>` (mp4 casi siempre) y devuelve la ruta.

    Si ya estaba bajado, lo reusa. Baja en una subcarpeta temporal que se borra
    siempre: si falla, no quedan pedazos (.part, .ytdl, formatos sin unir).
    Lanza VideoDownloadError si no se pudo.
    """
    cached = cached_video(youtube_id, dest_dir)
    if cached is not None:
        return cached
    import yt_dlp

    dest_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f".{youtube_id}-", dir=dest_dir))
    try:
        has_ffmpeg = shutil.which("ffmpeg") is not None
        options = {
            "format": FORMAT_WITH_FFMPEG if has_ffmpeg else FORMAT_WITHOUT_FFMPEG,
            "outtmpl": str(work / f"{youtube_id}.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "logger": _QuietLogger(),
            "js_runtimes": js_runtimes(),
            "socket_timeout": SOCKET_TIMEOUT_S,
            "max_filesize": MAX_FILESIZE,
            "retries": DOWNLOAD_RETRIES,
        }
        if has_ffmpeg:
            options["merge_output_format"] = "mp4"
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.download([f"https://www.youtube.com/watch?v={youtube_id}"])
        except Exception as exc:  # noqa: BLE001 - yt-dlp lanza de todo
            raise VideoDownloadError(f"no se pudo bajar el video ({type(exc).__name__})") from None
        done = cached_video(youtube_id, work)
        if done is None:
            # p. ej. max_filesize: yt-dlp lo saltea sin error y no deja el video.
            raise VideoDownloadError("yt-dlp terminó sin dejar el video")
        return Path(shutil.move(done, _target(youtube_id, dest_dir, done.suffix)))
    finally:
        shutil.rmtree(work, ignore_errors=True)
