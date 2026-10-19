import io
import time
import zipfile

import pytest
from docs_factory import (
    make_jpeg,
    make_mpo,
    make_pdf,
    make_png,
    make_pptx,
    png_claiming,
    zip_bomb_pptx,
)
from PIL import Image

from estudia.material import files
from estudia.material.files import (
    UnreadableFile,
    check_pptx_zip,
    reencode_image,
    safe_display_name,
    safe_display_text,
    sniff,
    sniff_file,
)


@pytest.mark.parametrize(
    "head, expected",
    [
        (make_pdf(["hola"])[:1024], "pdf"),
        (make_jpeg()[:1024], "jpeg"),
        (make_png()[:1024], "png"),
        (b"MZ\x90\x00", None),
        (b"<html>", None),
    ],
)
def test_sniff_by_bytes_not_extension(head, expected):
    assert sniff(head) == expected


def test_pdf_header_after_junk_is_still_pdf():
    assert sniff(b"\n\n%PDF-1.4") == "pdf"


def test_pptx_requires_presentation_part(tmp_path):
    cualquiera = tmp_path / "x.pptx"
    with zipfile.ZipFile(cualquiera, "w") as zf:
        zf.writestr("hola.txt", "hola")
    assert sniff_file(cualquiera) is None
    real = tmp_path / "y.bin"
    real.write_bytes(make_pptx(["uno"]))
    assert sniff_file(real) == "pptx"


def test_sniff_file_pdf_and_unknown(tmp_path):
    pdf = tmp_path / "a.txt"
    pdf.write_bytes(make_pdf(["hola"]))
    assert sniff_file(pdf) == "pdf"
    otro = tmp_path / "b.pdf"
    otro.write_bytes(b"MZ\x90\x00")
    assert sniff_file(otro) is None


def test_pptx_zip_bomb_rejected_without_extracting(tmp_path):
    path = tmp_path / "bomba.pptx"
    path.write_bytes(zip_bomb_pptx())
    start = time.monotonic()
    with pytest.raises(UnreadableFile):
        check_pptx_zip(path)
    assert time.monotonic() - start < 1
    assert sniff_file(path) is None


def test_zip_entry_with_dotdot_rejected(tmp_path):
    path = tmp_path / "malo.pptx"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", "<x/>")
        zf.writestr("ppt/presentation.xml", "<x/>")
        zf.writestr("../fuera.txt", "x")
    with pytest.raises(UnreadableFile):
        check_pptx_zip(path)


def test_zip_entry_absolute_rejected(tmp_path):
    path = tmp_path / "malo.pptx"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", "<x/>")
        zf.writestr("ppt/presentation.xml", "<x/>")
        zf.writestr("/etc/x", "x")
    with pytest.raises(UnreadableFile):
        check_pptx_zip(path)


def test_zip_too_many_entries_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "ZIP_MAX_ENTRIES", 3)
    path = tmp_path / "muchos.pptx"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", "<x/>")
        zf.writestr("ppt/presentation.xml", "<x/>")
        zf.writestr("a", "x")
        zf.writestr("b", "x")
    with pytest.raises(UnreadableFile):
        check_pptx_zip(path)


def test_not_a_zip_is_unreadable(tmp_path):
    path = tmp_path / "roto.pptx"
    path.write_bytes(b"PK\x03\x04basura")
    with pytest.raises(UnreadableFile):
        check_pptx_zip(path)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("../../etc/passwd.pdf", "passwd.pdf"),
        ("..\\..\\win.ini", "win.ini"),
        ("\x00ev\nil.pdf", "evil.pdf"),
        ("", "respaldo"),
        ("a" * 300, "a" * 120),
    ],
)
def test_safe_display_name(raw, expected):
    assert safe_display_name(raw, "respaldo") == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("a\u202eb.pdf", "ab.pdf"),
        ("a\u200bb", "ab"),
        ("a\u2028b\u2029c", "abc"),
        ("a\ud800b", "ab"),
    ],
)
def test_safe_display_name_drops_format_and_surrogates(raw, expected):
    out = safe_display_name(raw, "respaldo")
    assert out == expected
    out.encode("utf-8")


def test_safe_display_name_none_and_dots():
    assert safe_display_name(None, "respaldo") == "respaldo"
    assert safe_display_name(" ..  ", "respaldo") == "respaldo"


def test_jpeg_reencoded_without_exif():
    original = make_jpeg()
    assert b"Exif" in original
    out = reencode_image(original, "jpeg")
    assert not Image.open(io.BytesIO(out)).getexif()
    assert b"Exif" not in out


def test_png_text_chunk_removed():
    original = make_png()
    assert b"secreto" in original
    out = reencode_image(original, "png")
    assert b"secreto" not in out
    assert Image.open(io.BytesIO(out)).format == "PNG"


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
def test_image_pixel_bomb_rejected():
    start = time.monotonic()
    with pytest.raises(UnreadableFile):
        reencode_image(png_claiming(10000, 10000), "png")
    assert time.monotonic() - start < 1


def test_garbage_image_unreadable():
    with pytest.raises(UnreadableFile):
        reencode_image(b"no soy una imagen", "jpeg")


def test_large_photo_downscaled_to_4000():
    buf = io.BytesIO()
    Image.new("RGB", (5000, 100), (1, 2, 3)).save(buf, "JPEG")
    out = reencode_image(buf.getvalue(), "jpeg")
    assert max(Image.open(io.BytesIO(out)).size) == 4000


def test_image_over_cap_rejected_without_global_state():
    before = Image.MAX_IMAGE_PIXELS
    with pytest.raises(UnreadableFile):
        reencode_image(png_claiming(7000, 7000), "png")  # 49 M > 40 M, bajo el tope de Pillow
    assert Image.MAX_IMAGE_PIXELS == before


def test_reencode_does_not_touch_pillow_global():
    before = Image.MAX_IMAGE_PIXELS
    reencode_image(make_png(), "png")
    assert Image.MAX_IMAGE_PIXELS == before


def test_reencode_does_not_use_catch_warnings():
    import inspect

    assert "catch_warnings" not in inspect.getsource(files)


def _icc_bytes():
    from PIL import ImageCms

    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def test_jpeg_icc_profile_stripped():
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (10, 20, 30)).save(buf, "JPEG", icc_profile=_icc_bytes())
    assert b"ICC_PROFILE" in buf.getvalue()
    out = reencode_image(buf.getvalue(), "jpeg")
    assert b"ICC_PROFILE" not in out
    assert "icc_profile" not in Image.open(io.BytesIO(out)).info


def test_png_icc_profile_stripped():
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (10, 20, 30)).save(buf, "PNG", icc_profile=_icc_bytes())
    assert b"iCCP" in buf.getvalue()
    out = reencode_image(buf.getvalue(), "png")
    assert b"iCCP" not in out


def test_png_declared_as_jpeg_rejected():
    with pytest.raises(UnreadableFile):
        reencode_image(make_png(), "jpeg")
    with pytest.raises(UnreadableFile):
        reencode_image(make_jpeg(), "png")


def test_sniff_order_pdf_marker_wins_over_image_signature():
    """Decisión: `%PDF-` en los primeros 1.024 bytes gana, incluso tras una firma JPEG/PNG.

    Un archivo así que no sea un PDF de verdad lo rechaza después el parser (Tarea 2).
    """
    assert sniff(b"\xff\xd8\xff\xe0 %PDF-1.4") == "pdf"
    assert sniff(b"\x89PNG\r\n\x1a\n %PDF-1.4") == "pdf"


@pytest.mark.parametrize("raw, expected", [
    ("Unidad 1/2", "Unidad 1/2"),
    ("  Límites‮\x00​  ", "Límites"),
    ("a" * 300, "a" * 120),
    ("\x07", "respaldo"),
    (None, "respaldo"),
])
def test_safe_display_text_keeps_slashes(raw, expected):
    assert safe_display_text(raw, "respaldo") == expected


def test_jpeg_comment_and_text_keys_removed():
    original = make_jpeg()
    assert b"ComentarioSecreto" in original
    out = reencode_image(original, "jpeg")
    assert b"ComentarioSecreto" not in out
    info = Image.open(io.BytesIO(out)).info
    assert "comment" not in info and "exif" not in info


def test_mpo_phone_photo_keeps_only_first_frame():
    original = make_mpo()
    assert sniff(original[:1024]) == "jpeg"
    out = reencode_image(original, "jpeg")
    img = Image.open(io.BytesIO(out))
    assert img.format == "JPEG"
    assert getattr(img, "n_frames", 1) == 1
    assert not img.getexif()
    assert b"CamaraSecreta" not in out and b"MPF" not in out
    assert img.getpixel((10, 10))[0] > 150  # el primer cuadro (rojizo), no el segundo
