import pytest
import yt_dlp

from estudia.material import video
from estudia.material.video import VideoDownloadError, download_video, js_runtimes, mime_type


class FakeYoutubeDL:
    """YoutubeDL falso: guarda las opciones y "baja" un mp4 vacío (o falla)."""

    instances = []
    error = None
    produce = True

    def __init__(self, options):
        self.options = options
        FakeYoutubeDL.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def download(self, urls):
        self.urls = urls
        partial = self.options["outtmpl"].replace("%(ext)s", "f399.mp4")
        open(partial + ".part", "wb").close()
        open(self.options["outtmpl"].replace("%(ext)s", "mp4.ytdl"), "wb").close()
        if FakeYoutubeDL.error:
            raise FakeYoutubeDL.error
        if FakeYoutubeDL.produce:
            open(self.options["outtmpl"].replace("%(ext)s", "mp4"), "wb").close()


@pytest.fixture
def fake_ytdl(monkeypatch):
    FakeYoutubeDL.instances, FakeYoutubeDL.error, FakeYoutubeDL.produce = [], None, True
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYoutubeDL)
    return FakeYoutubeDL


def test_downloads_to_dest_dir_with_js_runtime(tmp_path, fake_ytdl):
    path = download_video("aaaaaaaaaa1", tmp_path / "videos")
    assert path == tmp_path / "videos" / "aaaaaaaaaa1.mp4"
    (ydl,) = fake_ytdl.instances
    assert ydl.urls == ["https://www.youtube.com/watch?v=aaaaaaaaaa1"]
    assert "deno" in ydl.options["js_runtimes"]
    assert "height<=480" in ydl.options["format"]
    assert ydl.options["socket_timeout"] == 30
    assert ydl.options["max_filesize"] == 500 * 1024 * 1024
    assert ydl.options["retries"] == 3
    # Solo queda el video terminado: nada de pedazos ni carpetas temporales.
    assert list((tmp_path / "videos").iterdir()) == [path]


def test_cached_video_is_reused(tmp_path, fake_ytdl):
    (tmp_path / "aaaaaaaaaa1.mp4").write_bytes(b"ya estaba")
    assert download_video("aaaaaaaaaa1", tmp_path) == tmp_path / "aaaaaaaaaa1.mp4"
    assert fake_ytdl.instances == []


def test_download_error_is_wrapped_and_leaves_no_partial_files(tmp_path, fake_ytdl):
    fake_ytdl.error = yt_dlp.utils.DownloadError("Video unavailable")
    with pytest.raises(VideoDownloadError) as exc:
        download_video("aaaaaaaaaa1", tmp_path)
    assert exc.value.__cause__ is None
    assert list(tmp_path.iterdir()) == []


def test_no_video_produced_is_an_error_without_leftovers(tmp_path, fake_ytdl):
    fake_ytdl.produce = False  # p. ej. superó max_filesize
    with pytest.raises(VideoDownloadError):
        download_video("aaaaaaaaaa1", tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("bad_id", ["../../etc/x", "corto", "aaaaaaaaaa1/", "aaaaaaaaaa.", ".." * 6 + "a"])
def test_invalid_youtube_id_is_rejected(tmp_path, fake_ytdl, bad_id):
    with pytest.raises(VideoDownloadError):
        download_video(bad_id, tmp_path)
    assert fake_ytdl.instances == []


def test_without_ffmpeg_does_not_ask_to_merge(tmp_path, fake_ytdl, monkeypatch):
    monkeypatch.setattr(video.shutil, "which", lambda name: None)
    download_video("aaaaaaaaaa1", tmp_path)
    options = fake_ytdl.instances[0].options
    assert "+" not in options["format"]
    assert "merge_output_format" not in options


def test_bundled_deno_is_used():
    from deno import find_deno_bin

    assert js_runtimes()["deno"] == {"path": find_deno_bin()}


def test_mime_type():
    assert mime_type(video.Path("a.mp4")) == "video/mp4"
    assert mime_type(video.Path("a.webm")) == "video/webm"
