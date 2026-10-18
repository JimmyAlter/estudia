import io
import time
import zipfile

import pytest
from docs_factory import blank_pdf, make_pdf, make_pptx, zip_bomb_pptx

from estudia.material import documents, sandbox
from estudia.material.documents import (
    PAST_EXAM_MAX_PAGES,
    DocInfo,
    DocumentTimeout,
    Part,
    TooManyPages,
    inspect_document,
    pdf_pages_bytes,
    plan_parts,
    slides_text,
)
from estudia.material.files import PDF, PPTX, TooLarge, UnreadableFile


def _write(tmp_path, data: bytes, name="doc.bin"):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _raw_pdf(objs: dict[int, bytes]) -> bytes:
    """PDF a mano con los objetos dados (1 es el catálogo) y un xref correcto."""
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for num in sorted(objs):
        offsets[num] = len(out)
        out += b"%d 0 obj\n" % num + objs[num] + b"\nendobj\n"
    xref = len(out)
    size = max(objs) + 1
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    for num in range(1, size):
        out += b"%010d 00000 n \n" % offsets[num]
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, xref)
    return bytes(out)


_CATALOG = b"<< /Type /Catalog /Pages 2 0 R >>"
_PAGE = b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>"


# --- inspect_document ---------------------------------------------------------------------


def test_inspect_pdf_counts_pages_text_and_outline(tmp_path):
    outline = [("Cap. 1", 1), ("Cap. 2", 3)]
    path = _write(tmp_path, make_pdf(["Límites " * 20] * 3, outline=outline))
    info = inspect_document(path, PDF)
    assert info == DocInfo(kind=PDF, pages=3, has_text=True, outline=outline)


def test_scanned_pdf_has_no_text(tmp_path):
    info = inspect_document(_write(tmp_path, blank_pdf(4)), PDF)
    assert info.pages == 4
    assert info.has_text is False


def test_half_short_pages_count_as_scanned(tmp_path):
    long_text = "Derivadas " * 10
    path = _write(tmp_path, make_pdf([long_text, "", long_text, "pág. 4"]))
    assert inspect_document(path, PDF).has_text is False
    path = _write(tmp_path, make_pdf([long_text, "", long_text]), "b.pdf")
    assert inspect_document(path, PDF).has_text is True


def test_page_bomb_rejected(tmp_path):
    with pytest.raises(TooManyPages):
        inspect_document(_write(tmp_path, blank_pdf(1001)), PDF)
    with pytest.raises(TooManyPages):
        inspect_document(_write(tmp_path, blank_pdf(31), "b.pdf"), PDF, max_pages=PAST_EXAM_MAX_PAGES)
    assert issubclass(TooManyPages, TooLarge)


def test_pdf_claiming_huge_page_count_is_bounded(tmp_path):
    data = _raw_pdf({1: _CATALOG, 2: b"<< /Type /Pages /Kids [3 0 R] /Count 2000000000 >>", 3: _PAGE})
    start = time.monotonic()
    try:
        info = inspect_document(_write(tmp_path, data), PDF, timeout_s=10)
    except (TooManyPages, UnreadableFile):
        pass
    else:
        assert info.pages <= documents.PDF_MAX_PAGES
    assert time.monotonic() - start < 15


def test_looping_page_tree_does_not_hang(tmp_path):
    data = _raw_pdf({
        1: _CATALOG,
        2: b"<< /Type /Pages /Kids [2 0 R 3 0 R 4 0 R] /Count 3 >>",
        3: _PAGE,
        4: b"<< /Type /Pages /Parent 2 0 R /Kids [2 0 R 4 0 R] /Count 5 >>",
    })
    start = time.monotonic()
    try:
        info = inspect_document(_write(tmp_path, data), PDF, timeout_s=10)
    except (TooManyPages, UnreadableFile):
        pass
    else:
        assert info.pages <= documents.PDF_MAX_PAGES
    assert time.monotonic() - start < 15


def test_broken_pdf_is_unreadable(tmp_path):
    start = time.monotonic()
    with pytest.raises(UnreadableFile):
        inspect_document(_write(tmp_path, b"%PDF-1.7\n basura"), PDF, timeout_s=10)
    assert time.monotonic() - start < 15


def test_password_pdf_is_unreadable(tmp_path):
    data = _raw_pdf({
        1: _CATALOG,
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: _PAGE,
        4: b"<< /Filter /Standard /V 1 /R 2 /O <" + b"00" * 32 + b"> /U <" + b"00" * 32 + b"> /P -4 >>",
    }).replace(b"/Root 1 0 R", b"/Root 1 0 R /Encrypt 4 0 R /ID [<00> <00>]")
    with pytest.raises(UnreadableFile):
        inspect_document(_write(tmp_path, data), PDF, timeout_s=10)


def test_toc_cap_counts_items_without_destination(tmp_path, monkeypatch):
    data = _raw_pdf({
        1: b"<< /Type /Catalog /Pages 2 0 R /Outlines 5 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: _PAGE,
        4: b"null",
        5: b"<< /Type /Outlines /First 6 0 R /Last 8 0 R /Count 3 >>",
        6: b"<< /Title (Sin destino 1) /Parent 5 0 R /Next 7 0 R >>",
        7: b"<< /Title (Sin destino 2) /Parent 5 0 R /Prev 6 0 R /Next 8 0 R >>",
        8: b"<< /Title (Con destino) /Parent 5 0 R /Prev 7 0 R /Dest [3 0 R /Fit] >>",
    })
    path = _write(tmp_path, data)
    # Se llama directo (sin hijo) para que el tope cambiado se vea.
    assert documents._inspect_pdf(path, 10).outline == [("Con destino", 1)]
    monkeypatch.setattr(documents, "TOC_MAX_ITEMS", 2)
    assert documents._inspect_pdf(path, 10).outline == []


def test_decompression_bomb_contained(tmp_path):
    path = _write(tmp_path, make_pdf(["x"], inflate_mb=300))
    start = time.monotonic()
    try:
        info = inspect_document(path, PDF, timeout_s=10)
    except UnreadableFile:
        pass
    else:
        assert info.pages == 1
    assert time.monotonic() - start < 20


def test_inspect_timeout_maps_to_document_timeout(tmp_path, monkeypatch):
    real_run_limited = documents.run_limited
    seen = {}

    def sleeping(target, *args, timeout_s, **kwargs):
        seen["timeout_s"] = timeout_s
        return real_run_limited(sandbox._test_sleep, 30, timeout_s=timeout_s)

    monkeypatch.setattr(documents, "PARSE_TIMEOUT_S", 0.5)
    monkeypatch.setattr(documents, "run_limited", sleeping)
    start = time.monotonic()
    with pytest.raises(DocumentTimeout):
        inspect_document(_write(tmp_path, blank_pdf(1)), PDF)
    assert seen["timeout_s"] == 0.5
    assert time.monotonic() - start < 6
    assert issubclass(DocumentTimeout, UnreadableFile)

    # Después de matar un hijo colgado, el siguiente llamado anda.
    monkeypatch.setattr(documents, "run_limited", real_run_limited)
    assert inspect_document(_write(tmp_path, blank_pdf(2), "b.pdf"), PDF, timeout_s=20).pages == 2


# --- diapositivas --------------------------------------------------------------------------


def test_inspect_pptx_counts_slides(tmp_path):
    path = _write(tmp_path, make_pptx(["Derivadas", "Regla de la cadena", "Ejemplos"]))
    info = inspect_document(path, PPTX)
    assert info.kind == PPTX
    assert info.pages == 3
    assert info.outline == []


def test_too_many_slides_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "SLIDES_MAX", 2)
    path = _write(tmp_path, make_pptx(["a", "b", "c"]))
    with pytest.raises(TooManyPages):
        inspect_document(path, PPTX)


def test_pptx_text_and_notes(tmp_path):
    path = _write(tmp_path, make_pptx(["Derivadas", "Regla de la cadena"], notes=["", "ojo"]))
    text = slides_text(path)
    assert "Diapositiva 1:\nDerivadas" in text
    assert "Diapositiva 2:" in text
    assert "Regla de la cadena" in text
    assert "Notas: ojo" in text
    assert text.index("Diapositiva 1:") < text.index("Diapositiva 2:")


def test_pptx_text_page_range_and_cap(tmp_path, monkeypatch):
    path = _write(tmp_path, make_pptx(["uno", "dos", "tres"]))
    text = slides_text(path, 2, 2)
    # Un tramo se numera desde 1, como el PDF de un tramo: la tubería lo corre después.
    assert text.startswith("Diapositiva 1:\ndos")
    assert "Diapositiva 2:" not in text
    assert "uno" not in text and "tres" not in text

    monkeypatch.setattr(documents, "PPTX_MAX_CHARS", 10)
    assert len(slides_text(path)) == 10


def test_slides_text_checks_slide_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "SLIDES_MAX", 2)
    path = _write(tmp_path, make_pptx(["a", "b", "c"]))
    with pytest.raises(TooManyPages):
        slides_text(path)


def test_pptx_zip_bomb_unreadable(tmp_path):
    path = _write(tmp_path, zip_bomb_pptx())
    with pytest.raises(UnreadableFile):
        inspect_document(path, PPTX)
    with pytest.raises(UnreadableFile):
        slides_text(path)


def test_corrupt_pptx_unreadable(tmp_path):
    good = zipfile.ZipFile(io.BytesIO(make_pptx(["x"])))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in good.namelist():
            data = good.read(name)
            if name == "ppt/presentation.xml":
                data = b"<p:presentation esto no es xml"
            zf.writestr(name, data)
    path = _write(tmp_path, buf.getvalue())
    start = time.monotonic()
    with pytest.raises(UnreadableFile):
        inspect_document(path, PPTX, timeout_s=10)
    with pytest.raises(UnreadableFile):
        slides_text(path, timeout_s=10)
    assert time.monotonic() - start < 25


# --- plan_parts ----------------------------------------------------------------------------


def _parts(pages, outline=()):
    return plan_parts(DocInfo(kind=PDF, pages=pages, has_text=True, outline=list(outline)))


def _ranges(parts):
    return [(p.page_from, p.page_to) for p in parts]


@pytest.mark.parametrize(
    "pages, outline, expected",
    [
        (60, [("A", 1), ("B", 30)], []),
        (85, [], [(None, 1, 40), (None, 41, 85)]),
        (
            400,
            [],
            [(None, 1 + 40 * i, 40 * (i + 1)) for i in range(10)],
        ),
        (
            # Intro (2 páginas) se une a Límites: 1-69, que se corta en tramos. Derivadas
            # (70-130) son 61 páginas, más de 60: también se corta.
            130,
            [("Intro", 1), ("Límites", 3), ("Derivadas", 70)],
            [
                ("Límites (1)", 1, 40),
                ("Límites (2)", 41, 69),
                ("Derivadas (1)", 70, 109),
                ("Derivadas (2)", 110, 130),
            ],
        ),
        (
            129,
            [("Intro", 1), ("Límites", 3), ("Derivadas", 70)],
            [("Límites (1)", 1, 40), ("Límites (2)", 41, 69), ("Derivadas", 70, 129)],
        ),
        (
            # Repetidos en la misma página (queda el primero), fuera de rango y desordenados.
            100,
            [("Segunda", 50), ("Otra", 50), ("Fuera", 0), ("Lejos", 101), ("Primera", 1)],
            [("Primera", 1, 49), ("Segunda", 50, 100)],
        ),
        (
            # Un marcador sin título: los tramos se llaman "Parte (n)".
            130,
            [("", 1), ("B", 100)],
            [("Parte (1)", 1, 40), ("Parte (2)", 41, 80), ("Parte (3)", 81, 99), ("B", 100, 130)],
        ),
        (100, [("Solo", 1)], [(None, 1, 40), (None, 41, 80), (None, 81, 100)]),
        (100, [("Fuera", 500), ("Solo", 20)], [(None, 1, 40), (None, 41, 80), (None, 81, 100)]),
        (
            # El primer marcador no está en la página 1: la primera parte arranca igual en la 1.
            90,
            [("A", 10), ("B", 50)],
            [("A", 1, 49), ("B", 50, 90)],
        ),
        (
            # La última parte corta se une a la anterior.
            90,
            [("A", 1), ("B", 50), ("C", 88)],
            [("A", 1, 49), ("B", 50, 90)],
        ),
    ],
)
def test_plan_parts(pages, outline, expected):
    parts = _parts(pages, outline)
    assert [(p.title, p.page_from, p.page_to) for p in parts] == expected


def test_plan_parts_short_tail_chunk_joins_previous():
    assert _ranges(_parts(85)) == [(1, 40), (41, 85)]
    assert _ranges(_parts(89)) == [(1, 40), (41, 89)]
    assert _ranges(_parts(90)) == [(1, 40), (41, 80), (81, 90)]


def test_plan_parts_covers_every_page_once():
    for pages, outline in [(61, []), (333, [("a", 1), ("b", 7), ("c", 9), ("d", 200)]), (999, [])]:
        parts = _parts(pages, outline)
        covered = [n for p in parts for n in range(p.page_from, p.page_to + 1)]
        assert covered == list(range(1, pages + 1))
        assert all(p.page_to - p.page_from + 1 <= documents.SPLIT_OVER_PAGES for p in parts)


def test_plan_parts_slides_use_chunks():
    info = DocInfo(kind=PPTX, pages=120, has_text=True, outline=[])
    assert plan_parts(info) == [Part(None, 1, 40), Part(None, 41, 80), Part(None, 81, 120)]


# --- pdf_pages_bytes -----------------------------------------------------------------------


def test_pdf_slice_has_requested_pages(tmp_path):
    pages = [f"Pagina {n} " * 10 for n in range(1, 6)]
    path = _write(tmp_path, make_pdf(pages))
    data = pdf_pages_bytes(path, 2, 4)
    assert data.startswith(b"%PDF-")
    sliced = _write(tmp_path, data, "slice.pdf")
    assert inspect_document(sliced, PDF).pages == 3

    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(data)
    try:
        first = pdf[0].get_textpage().get_text_range()
    finally:
        pdf.close()
    assert first.startswith("Pagina 2")


@pytest.mark.parametrize("page_from, page_to", [(0, 2), (3, 2), (4, 6)])
def test_pdf_slice_bad_range_is_rejected(tmp_path, page_from, page_to):
    path = _write(tmp_path, make_pdf(["a", "b", "c", "d", "e"]))
    with pytest.raises((ValueError, UnreadableFile)):
        pdf_pages_bytes(path, page_from, page_to)


def test_pdf_slice_rejects_long_range_and_huge_source(tmp_path):
    path = _write(tmp_path, blank_pdf(70))
    with pytest.raises(ValueError):
        pdf_pages_bytes(path, 1, documents.SPLIT_OVER_PAGES + 1)
    assert pdf_pages_bytes(path, 1, documents.SPLIT_OVER_PAGES).startswith(b"%PDF-")
    with pytest.raises(TooManyPages):
        pdf_pages_bytes(_write(tmp_path, blank_pdf(1001), "big.pdf"), 1, 2)


def test_busy_sandbox_is_retryable_not_unreadable(tmp_path, monkeypatch):
    from estudia.material.documents import DocumentBusy

    def busy(*args, **kwargs):
        raise sandbox.SandboxBusy()

    monkeypatch.setattr(documents, "run_limited", busy)
    path = tmp_path / "a.pdf"
    path.write_bytes(make_pdf(["Hola"]))
    with pytest.raises(DocumentBusy) as info:
        documents.inspect_document(path, PDF)
    assert not isinstance(info.value, (UnreadableFile, TooLarge))
