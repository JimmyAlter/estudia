"""Parciales propios: tipo por bytes, fotos sin EXIF, topes y nombres que salen de un id."""
import io

import pytest
from PIL import Image

from docs_factory import blank_pdf, make_image_pdf, make_jpeg, make_mpo, make_pdf, make_png, png_claiming
from estudia.db import connect
from estudia.study import past_exams
from estudia.study.exams import create_manual_exam, delete_manual_exam, list_exams
from estudia.material.files import reencode_image
from estudia.material.sandbox import SandboxBusy, SandboxFailed, SandboxTimeout
from estudia.study.past_exams import (
    BUSY_TEXT,
    DUPLICATE_TEXT,
    LIMIT_TEXT,
    ORIGIN,
    TOO_BIG_TEXT,
    TOO_MANY_PAGES_TEXT,
    UNREADABLE_TEXT,
    UPLOAD_UNREADABLE_TEXT,
    WRONG_TYPE_TEXT,
    PastExamError,
    add_past_exam,
    delete_past_exam,
    list_past_exams,
)

NOW = 1_790_000_000


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "estudia.db")
    c.execute("INSERT INTO course(id, site_id, fullname, shortname) VALUES (10, 's', 'Análisis', 'AM')")
    c.commit()
    yield c
    c.close()


@pytest.fixture
def exam_id(conn):
    return create_manual_exam(conn, 10, "Primer parcial", NOW + 86400, [], None, now=NOW)


def files_in(base):
    return sorted(p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file())


def test_pdf_and_photo_stored_by_id(conn, exam_id, tmp_path):
    pdf_id = add_past_exam(conn, exam_id, make_pdf(["Ejercicio 1"]), "parcial 2024.pdf", NOW, tmp_path)
    jpg_id = add_past_exam(conn, exam_id, make_jpeg(), "foto.jpg", NOW, tmp_path)
    rows = conn.execute("SELECT id, origin, kind, path, status FROM past_exam ORDER BY id").fetchall()
    assert [(r["id"], r["origin"], r["kind"], r["status"]) for r in rows] == [
        (pdf_id, ORIGIN, "pdf", "pending"), (jpg_id, ORIGIN, "jpeg", "pending")]
    assert ORIGIN == "parcial_propio"
    assert (tmp_path / "parciales" / f"{pdf_id}.pdf").is_file()
    stored = tmp_path / "parciales" / f"{jpg_id}.jpg"
    assert not Image.open(stored).getexif()
    assert b"CamaraSecreta" not in stored.read_bytes()


def test_png_text_chunk_dropped(conn, exam_id, tmp_path):
    pid = add_past_exam(conn, exam_id, make_png(), "x.png", NOW, tmp_path)
    data = (tmp_path / "parciales" / f"{pid}.png").read_bytes()
    assert b"secreto" not in data
    assert Image.open(io.BytesIO(data)).format == "PNG"


def test_type_by_bytes_not_name(conn, exam_id, tmp_path):
    pid = add_past_exam(conn, exam_id, make_pdf(["x"]), "foto.jpg", NOW, tmp_path)
    assert list_past_exams(conn, exam_id)[0]["kind"] == "pdf"
    assert (tmp_path / "parciales" / f"{pid}.pdf").is_file()
    with pytest.raises(PastExamError) as err:
        add_past_exam(conn, exam_id, b"MZ\x90\x00" + b"\0" * 200, "parcial.pdf", NOW, tmp_path)
    assert (err.value.status, str(err.value)) == (415, WRONG_TYPE_TEXT)


def test_too_big_rejected(conn, exam_id, tmp_path, monkeypatch):
    monkeypatch.setattr(past_exams, "MAX_PAST_EXAM_BYTES", 1000)
    with pytest.raises(PastExamError) as err:
        add_past_exam(conn, exam_id, make_pdf(["x" * 2000]), "a.pdf", NOW, tmp_path)
    assert (err.value.status, str(err.value)) == (413, TOO_BIG_TEXT)


def test_page_bomb_rejected(conn, exam_id, tmp_path):
    with pytest.raises(PastExamError) as err:
        add_past_exam(conn, exam_id, blank_pdf(31), "largo.pdf", NOW, tmp_path)
    assert (err.value.status, str(err.value)) == (413, TOO_MANY_PAGES_TEXT)
    assert files_in(tmp_path / "parciales") == []
    assert conn.execute("SELECT COUNT(*) FROM past_exam").fetchone()[0] == 0


def test_thirty_pages_accepted(conn, exam_id, tmp_path):
    add_past_exam(conn, exam_id, blank_pdf(30), "justo.pdf", NOW, tmp_path)


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
def test_broken_pdf_and_image_bomb_unreadable(conn, exam_id, tmp_path):
    for data in (b"%PDF-1.4\n" + b"basura" * 100, png_claiming(10000, 10000), b"\xff\xd8\xff" + b"x" * 50):
        with pytest.raises(PastExamError) as err:
            add_past_exam(conn, exam_id, data, "x", NOW, tmp_path)
        assert (err.value.status, str(err.value)) == (400, UPLOAD_UNREADABLE_TEXT)
    assert files_in(tmp_path / "parciales") == []


def test_path_traversal_name(conn, exam_id, tmp_path):
    pid = add_past_exam(conn, exam_id, make_pdf(["x"]), "../../../estudia.db", NOW, tmp_path)
    assert list_past_exams(conn, exam_id)[0]["name"] == "estudia.db"
    assert files_in(tmp_path) == ["estudia.db", f"parciales/{pid}.pdf"]


def test_limit_and_duplicate(conn, exam_id, tmp_path):
    for i in range(5):
        add_past_exam(conn, exam_id, make_pdf([f"Parcial {i}"]), f"p{i}.pdf", NOW, tmp_path)
    with pytest.raises(PastExamError) as err:
        add_past_exam(conn, exam_id, make_pdf(["Parcial 6"]), "p6.pdf", NOW, tmp_path)
    assert (err.value.status, str(err.value)) == (409, LIMIT_TEXT)

    other = create_manual_exam(conn, 10, "Segundo parcial", NOW + 86400, [], None, now=NOW)
    add_past_exam(conn, other, make_pdf(["Parcial 0"]), "p0.pdf", NOW, tmp_path)  # otro examen: vale
    with pytest.raises(PastExamError) as err:
        add_past_exam(conn, other, make_pdf(["Parcial 0"]), "otro nombre.pdf", NOW, tmp_path)
    assert (err.value.status, str(err.value)) == (409, DUPLICATE_TEXT)
    assert len(files_in(tmp_path / "parciales")) == 6


def test_missing_exam_and_enrollment(conn, tmp_path):
    with pytest.raises(LookupError):
        add_past_exam(conn, 999, make_pdf(["x"]), "a.pdf", NOW, tmp_path)
    enrollment = create_manual_exam(conn, 10, "Inscripción", NOW, [], None, kind="inscripcion",
                                    closes_at=NOW + 3600, now=NOW)
    with pytest.raises(PastExamError) as err:
        add_past_exam(conn, enrollment, make_pdf(["x"]), "a.pdf", NOW, tmp_path)
    assert err.value.status == 400


def test_list_never_has_path_or_hash(conn, exam_id, tmp_path):
    add_past_exam(conn, exam_id, make_pdf(["x"]), "a.pdf", NOW, tmp_path)
    (item,) = list_past_exams(conn, exam_id)
    assert set(item) == {"id", "name", "kind", "status", "error", "created_at"}
    assert item["created_at"] == NOW


def test_delete_removes_file_and_row(conn, exam_id, tmp_path):
    pid = add_past_exam(conn, exam_id, make_pdf(["x"]), "a.pdf", NOW, tmp_path)
    delete_past_exam(conn, exam_id, pid, tmp_path)
    assert files_in(tmp_path / "parciales") == []
    assert list_past_exams(conn, exam_id) == []
    with pytest.raises(LookupError):
        delete_past_exam(conn, exam_id, pid, tmp_path)


def test_delete_exam_removes_its_past_exams(conn, exam_id, tmp_path):
    other = create_manual_exam(conn, 10, "Otro", NOW + 86400, [], None, now=NOW)
    add_past_exam(conn, exam_id, make_pdf(["x"]), "a.pdf", NOW, tmp_path)
    keep = add_past_exam(conn, other, make_pdf(["y"]), "b.pdf", NOW, tmp_path)
    counts = {e.id: e.past_exams for e in list_exams(conn, NOW)}
    assert counts == {exam_id: 1, other: 1}
    delete_manual_exam(conn, exam_id, base_dir=tmp_path)
    assert files_in(tmp_path / "parciales") == [f"{keep}.pdf"]
    assert [r[0] for r in conn.execute("SELECT exam_id FROM past_exam")] == [other]


def test_photo_reencoded_in_sandbox(conn, exam_id, tmp_path, monkeypatch):
    calls = []
    real = past_exams.run_limited

    def spy(target, *args, **kwargs):
        calls.append(target)
        return real(target, *args, **kwargs)

    monkeypatch.setattr(past_exams, "run_limited", spy)
    add_past_exam(conn, exam_id, make_jpeg(), "foto.jpg", NOW, tmp_path)
    assert reencode_image in calls


@pytest.mark.parametrize("exc, status, text", [
    (SandboxBusy(), 503, BUSY_TEXT),
    (SandboxTimeout(), 400, UPLOAD_UNREADABLE_TEXT),
    (SandboxFailed("MemoryError"), 400, UPLOAD_UNREADABLE_TEXT),
])
def test_photo_sandbox_errors(conn, exam_id, tmp_path, monkeypatch, exc, status, text):
    def fail(*args, **kwargs):
        raise exc

    monkeypatch.setattr(past_exams, "run_limited", fail)
    with pytest.raises(PastExamError) as err:
        add_past_exam(conn, exam_id, make_png(), "foto.png", NOW, tmp_path)
    assert (err.value.status, str(err.value)) == (status, text)
    assert files_in(tmp_path / "parciales") == []


_XMP = (b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><dc:creator>XmpSecreto</dc:creator></x:xmpmeta>')
_INFO = {"Author": "Ana Secreta", "Creator": "Escaner Secreto", "Producer": "Programa Secreto",
         "Title": "Parcial de Ana Secreta"}


def test_pdf_metadata_stripped_before_storing(conn, exam_id, tmp_path):
    """Autor, programa, título y XMP del PDF no se guardan: se reescribe con pdfium en el sandbox."""
    original = make_pdf(["Ejercicio 1: derivar"], info=_INFO, xmp=_XMP)
    assert b"/Author" in original and b"XmpSecreto" in original
    pid = add_past_exam(conn, exam_id, original, "parcial.pdf", NOW, tmp_path)
    stored = (tmp_path / "parciales" / f"{pid}.pdf").read_bytes()
    for secret in (b"Ana Secreta", b"Escaner Secreto", b"Programa Secreto", b"XmpSecreto",
                   b"/Author", b"/Creator", b"/Producer", b"/Title", b"/Metadata", b"/CreationDate"):
        assert secret not in stored
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(stored)
    try:
        assert len(pdf) == 1
        # Se guarda rasterizado: el texto sigue a la vista (píxeles oscuros donde estaba).
        img = pdf[0].render(scale=1).to_pil().convert("L")
        assert img.crop((60, 55, 250, 85)).getextrema()[0] < 100
        assert all(not value for value in pdf.get_metadata_dict().values())
    finally:
        pdf.close()
    row = conn.execute("SELECT size FROM past_exam WHERE id = ?", (pid,)).fetchone()
    assert row["size"] == len(stored)


# Lo que delataría la foto de adentro: EXIF, su GPS, la marca de cámara o un XMP.
_PHOTO_SECRETS = (b"Exif", b"GPS", b"CamaraSecreta", b"xmpmeta", b"ns.adobe.com/xap", b"XmpSecreto",
                  b"ComentarioSecreto", b"/Metadata")


def test_pdf_embedded_photo_loses_exif_and_gps(conn, exam_id, tmp_path):
    original = make_image_pdf(make_jpeg(gps=True))
    assert b"Exif" in original and b"CamaraSecreta" in original
    pid = add_past_exam(conn, exam_id, original, "parcial.pdf", NOW, tmp_path)
    stored = (tmp_path / "parciales" / f"{pid}.pdf").read_bytes()
    for secret in _PHOTO_SECRETS:
        assert secret not in stored
    _assert_readable_page(stored)


def test_scanner_pdf_with_image_xmp_is_cleaned_not_rejected(conn, exam_id, tmp_path):
    original = make_image_pdf(make_jpeg(gps=False, comment=None), metadata=_XMP)
    pid = add_past_exam(conn, exam_id, original, "escaneado.pdf", NOW, tmp_path)
    stored = (tmp_path / "parciales" / f"{pid}.pdf").read_bytes()
    for secret in _PHOTO_SECRETS:
        assert secret not in stored
    _assert_readable_page(stored)


def _assert_readable_page(stored: bytes) -> None:
    """Una página, del tamaño de la original, con la foto todavía visible (no en blanco)."""
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(stored)
    try:
        assert len(pdf) == 1
        page = pdf[0]
        assert (round(page.get_width()), round(page.get_height())) == (612, 792)
        img = page.render(scale=0.5).to_pil().convert("RGB")
        # La foto (200, 80, 40) ocupa x 100..500 e y 92..392 pt desde arriba: a escala 0,5,
        # el punto (150, 120) cae adentro.
        r, g, b = img.getpixel((150, 120))
        assert r > 150 and g < 130 and b < 100
    finally:
        pdf.close()


def test_pdf_rewritten_in_sandbox(conn, exam_id, tmp_path, monkeypatch):
    from estudia.material.documents import strip_pdf_metadata
    calls = []
    real = past_exams.run_limited

    def spy(target, *args, **kwargs):
        calls.append(target)
        return real(target, *args, **kwargs)

    monkeypatch.setattr(past_exams, "run_limited", spy)
    add_past_exam(conn, exam_id, make_pdf(["x"], info=_INFO), "p.pdf", NOW, tmp_path)
    assert strip_pdf_metadata in calls


@pytest.mark.parametrize("exc, status, text", [
    (SandboxBusy(), 503, BUSY_TEXT),
    (SandboxTimeout(), 400, UPLOAD_UNREADABLE_TEXT),
    (SandboxFailed("ValueError"), 400, UPLOAD_UNREADABLE_TEXT),
])
def test_pdf_never_stored_when_rewrite_fails(conn, exam_id, tmp_path, monkeypatch, exc, status, text):
    def fail(*args, **kwargs):
        raise exc

    monkeypatch.setattr(past_exams, "run_limited", fail)
    with pytest.raises(PastExamError) as err:
        add_past_exam(conn, exam_id, make_pdf(["x"], info=_INFO), "p.pdf", NOW, tmp_path)
    assert (err.value.status, str(err.value)) == (status, text)
    assert files_in(tmp_path / "parciales") == []
    assert conn.execute("SELECT COUNT(*) FROM past_exam").fetchone()[0] == 0


def test_same_pdf_twice_still_duplicate(conn, exam_id, tmp_path):
    """La reescritura no es idéntica byte a byte cada vez: el duplicado se mira en lo que subió."""
    data = make_pdf(["Parcial"], info=_INFO)
    add_past_exam(conn, exam_id, data, "a.pdf", NOW, tmp_path)
    with pytest.raises(PastExamError) as err:
        add_past_exam(conn, exam_id, data, "b.pdf", NOW, tmp_path)
    assert (err.value.status, str(err.value)) == (409, DUPLICATE_TEXT)


def test_jpeg_comment_not_stored(conn, exam_id, tmp_path):
    pid = add_past_exam(conn, exam_id, make_jpeg(), "foto.jpg", NOW, tmp_path)
    data = (tmp_path / "parciales" / f"{pid}.jpg").read_bytes()
    assert b"ComentarioSecreto" not in data
    assert "comment" not in Image.open(io.BytesIO(data)).info


def test_mpo_phone_photo_accepted(conn, exam_id, tmp_path):
    pid = add_past_exam(conn, exam_id, make_mpo(), "IMG_0001.jpg", NOW, tmp_path)
    img = Image.open(tmp_path / "parciales" / f"{pid}.jpg")
    assert img.format == "JPEG" and getattr(img, "n_frames", 1) == 1
    assert list_past_exams(conn, exam_id)[0]["kind"] == "jpeg"


def test_pdf_must_start_with_header(conn, exam_id, tmp_path):
    pdf = make_pdf(["x"])
    with pytest.raises(PastExamError) as err:
        add_past_exam(conn, exam_id, b"<html><body>hola</body></html>\n" + pdf, "a.pdf", NOW, tmp_path)
    assert (err.value.status, str(err.value)) == (415, WRONG_TYPE_TEXT)
    add_past_exam(conn, exam_id, b" \r\n\t" + pdf, "a.pdf", NOW, tmp_path)  # solo espacios: vale


def test_encrypted_pdf_unreadable(conn, exam_id, tmp_path):
    with pytest.raises(PastExamError) as err:
        add_past_exam(conn, exam_id, make_pdf(["x"], encrypt=True), "clave.pdf", NOW, tmp_path)
    assert (err.value.status, str(err.value)) == (400, UPLOAD_UNREADABLE_TEXT)
    assert files_in(tmp_path / "parciales") == []


def test_delete_survives_unlink_error(conn, exam_id, tmp_path, monkeypatch, caplog):
    pid = add_past_exam(conn, exam_id, make_pdf(["x"]), "a.pdf", NOW, tmp_path)
    from pathlib import Path

    def boom(self, missing_ok=False):
        raise PermissionError(f"no se puede borrar {self}")

    monkeypatch.setattr(Path, "unlink", boom)
    delete_past_exam(conn, exam_id, pid, tmp_path)
    assert list_past_exams(conn, exam_id) == []
    assert str(tmp_path) not in caplog.text
    assert "PermissionError" in caplog.text


def test_upload_texts_say_what_to_do():
    assert TOO_BIG_TEXT == "Ese archivo pesa más de 20 MB. Probá con una foto de cada hoja."
    assert TOO_MANY_PAGES_TEXT == "Ese parcial tiene más de 30 páginas. Cargá solo las del examen."
    assert UPLOAD_UNREADABLE_TEXT == "No se pudo leer ese archivo. Probá con un PDF o una foto más nítida."
    # El estado de un parcial que no se pudo leer después de cargarlo no cambia.
    assert UNREADABLE_TEXT == "No se pudo leer"
