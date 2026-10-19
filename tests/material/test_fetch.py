"""Bajar, revisar y partir los PDF y diapositivas del campus."""

import logging
from pathlib import Path

import pytest
from docs_factory import blank_pdf, make_pdf, make_pptx

from estudia.db import connect
from estudia.material import fetch
from estudia.material.documents import DocumentBusy
from estudia.material.fetch import DOWNLOADS_PER_RUN, MAX_DOWNLOAD_ATTEMPTS, fetch_documents
from estudia.material.files import DOWNLOAD_FAILED_TEXT, FAIL_DOWNLOAD
from estudia.moodle.client import (
    FileTooLarge,
    ForeignFileUrl,
    MoodleError,
    MoodleUnreachable,
    TokenInvalid,
)

SITE_URL = "https://aulasvirtuales.frba.utn.edu.ar"
EXE = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 200


class FakeClient:
    site_url = SITE_URL

    def __init__(self, payloads=None, error=None):
        self.payloads = payloads or {}   # url -> bytes
        self.error = error
        self.downloaded: list[tuple[str, Path, int]] = []

    def download_file(self, fileurl, dest, *, max_bytes):
        self.downloaded.append((fileurl, dest, max_bytes))
        if self.error is not None:
            raise self.error
        data = self.payloads[fileurl]
        dest.write_bytes(data)
        return len(data)


@pytest.fixture
def conn():
    c = connect(Path(":memory:"))
    c.execute("INSERT INTO section(id, course_id, num, name, visible) VALUES (1, 101, 1, 'U1', 1)")
    c.commit()
    return c


@pytest.fixture
def dest(tmp_path):
    return tmp_path / "documentos"


def _doc(conn, name="guia.pdf", kind="pdf", title="Guía 1") -> tuple[int, str]:
    url = f"{SITE_URL}/webservice/pluginfile.php/1/mod_resource/content/0/{name}"
    cur = conn.execute(
        "INSERT INTO material(section_id, kind, title, status, file_key, module_id, module_kind,"
        " remote_url) VALUES (1, ?, ?, 'downloading', ?, 70, 'resource', ?)",
        (kind, title, f"70:/{name}", url))
    conn.commit()
    return cur.lastrowid, url


def _row(conn, material_id):
    return conn.execute("SELECT * FROM material WHERE id=?", (material_id,)).fetchone()


def _files(dest: Path) -> list[str]:
    return sorted(p.name for p in dest.iterdir())


def test_fetch_pdf_inspects_and_queues(conn, dest):
    mid, url = _doc(conn)
    pdf = make_pdf(["Límites y continuidad " * 5] * 3)
    client = FakeClient({url: pdf})
    assert fetch_documents(conn, client, dest) == 1
    row = _row(conn, mid)
    assert (row["status"], row["pages"], row["has_text"]) == ("pending", 3, 1)
    assert row["ref"] == f"documentos/{mid}.pdf"
    assert row["file_sha256"] and len(row["file_sha256"]) == 64
    assert _files(dest) == [f"{mid}.pdf"]
    assert (dest / f"{mid}.pdf").read_bytes() == pdf
    assert client.downloaded[0][1] == dest / f"{mid}.bin"
    assert client.downloaded[0][2] == 50 * 1024 * 1024
    assert conn.execute("SELECT COUNT(*) FROM material").fetchone()[0] == 1


def test_fetch_slides_inspects_and_queues(conn, dest):
    mid, url = _doc(conn, "clase.pptx", kind="slides", title="Apuntes: Clase")
    client = FakeClient({url: make_pptx(["Derivadas", "Regla de la cadena"])})
    assert fetch_documents(conn, client, dest) == 1
    row = _row(conn, mid)
    assert (row["status"], row["pages"], row["ref"]) == ("pending", 2, f"documentos/{mid}.pptx")
    assert _files(dest) == [f"{mid}.pptx"]


def test_fetch_long_pdf_splits_into_parts(conn, dest):
    mid, url = _doc(conn)
    assert fetch_documents(conn, FakeClient({url: blank_pdf(130)}), dest) == 1
    parent = _row(conn, mid)
    assert (parent["status"], parent["pages"], parent["has_text"]) == ("split", 130, 0)
    parts = conn.execute(
        "SELECT * FROM material WHERE parent_id=? ORDER BY page_from", (mid,)).fetchall()
    # Tramos de 40; el último, de 10 páginas, no es menor que MIN_TAIL_PAGES y queda solo.
    assert [(p["page_from"], p["page_to"]) for p in parts] == [(1, 40), (41, 80), (81, 120), (121, 130)]
    for p in parts:
        assert p["status"] == "pending"
        assert (p["section_id"], p["kind"], p["ref"]) == (1, "pdf", f"documentos/{mid}.pdf")
        assert p["cmid"] is None and p["file_key"] is None and p["remote_url"] is None
    assert [p["title"] for p in parts] == [
        "Guía 1, págs. 1–40", "Guía 1, págs. 41–80", "Guía 1, págs. 81–120", "Guía 1, págs. 121–130"]
    # Una segunda corrida no vuelve a bajar ni a partir.
    client = FakeClient()
    assert fetch_documents(conn, client, dest) == 0
    assert client.downloaded == []
    assert conn.execute("SELECT COUNT(*) FROM material WHERE parent_id=?", (mid,)).fetchone()[0] == 4


def test_fetch_split_uses_outline_titles(conn, dest):
    mid, url = _doc(conn)
    pdf = make_pdf([""] * 70, outline=[("Funciones", 1), ("Límites", 36)])
    fetch_documents(conn, FakeClient({url: pdf}), dest)
    parts = conn.execute(
        "SELECT title, page_from, page_to FROM material WHERE parent_id=? ORDER BY page_from",
        (mid,)).fetchall()
    assert [tuple(p) for p in parts] == [("Funciones", 1, 35), ("Límites", 36, 70)]


def test_fetch_bytes_not_matching_kind_unreadable(conn, dest):
    mid, url = _doc(conn)
    assert fetch_documents(conn, FakeClient({url: EXE}), dest) == 0
    row = _row(conn, mid)
    assert (row["status"], row["error"], row["ref"]) == ("unreadable", "No se pudo leer", None)
    assert _files(dest) == []


def test_fetch_pdf_arriving_as_slides_unreadable(conn, dest):
    mid, url = _doc(conn, "clase.pptx", kind="slides")
    assert fetch_documents(conn, FakeClient({url: make_pdf(["Hola"])}), dest) == 0
    assert _row(conn, mid)["status"] == "unreadable"
    assert _files(dest) == []


def test_fetch_broken_pdf_unreadable(conn, dest):
    mid, url = _doc(conn)
    assert fetch_documents(conn, FakeClient({url: b"%PDF-1.4\nesto no es un pdf"}), dest) == 0
    row = _row(conn, mid)
    assert (row["status"], row["error"]) == ("unreadable", "No se pudo leer")
    assert _files(dest) == []


def test_fetch_page_bomb_too_large(conn, dest):
    mid, url = _doc(conn)
    assert fetch_documents(conn, FakeClient({url: blank_pdf(1001)}), dest) == 0
    row = _row(conn, mid)
    assert (row["status"], row["error"]) == ("too_large", "Archivo demasiado grande")
    assert conn.execute("SELECT COUNT(*) FROM material WHERE parent_id=?", (mid,)).fetchone()[0] == 0
    assert _files(dest) == []


def test_fetch_file_too_large_from_campus(conn, dest):
    mid, _ = _doc(conn)
    assert fetch_documents(conn, FakeClient(error=FileTooLarge("el archivo pasa el tope")), dest) == 0
    row = _row(conn, mid)
    assert (row["status"], row["error"]) == ("too_large", "Archivo demasiado grande")


def test_fetch_foreign_redirect_unsupported(conn, dest):
    mid, _ = _doc(conn)
    fetch_documents(conn, FakeClient(error=ForeignFileUrl("el campus redirigió fuera")), dest)
    row = _row(conn, mid)
    assert (row["status"], row["error"]) == ("unsupported", "No se puede leer todavía")


@pytest.mark.parametrize("error", [
    MoodleUnreachable("HTTP 503 en https://campus/x"),
    MoodleError("nopermissions"),
    TokenInvalid("accessexception"),   # un archivo restringido no cierra la sesión
    OSError("disco lleno"),
])
def test_fetch_network_error_retries_next_sync(conn, dest, caplog, error):
    mid, url = _doc(conn)
    caplog.set_level(logging.DEBUG)
    assert fetch_documents(conn, FakeClient(error=error), dest) == 0
    assert _row(conn, mid)["status"] == "downloading"
    assert not conn.in_transaction
    assert str(error) not in caplog.text   # solo el tipo
    assert type(error).__name__ in caplog.text
    # La próxima sincronización lo vuelve a intentar y ahora sale.
    assert fetch_documents(conn, FakeClient({url: make_pdf(["Hola"])}), dest) == 1
    assert _row(conn, mid)["status"] == "pending"


def test_fetch_token_invalid_propagates(conn, dest):
    mid, _ = _doc(conn)
    _doc(conn, "otra.pdf")
    client = FakeClient(error=TokenInvalid("invalidtoken"))
    with pytest.raises(TokenInvalid):
        fetch_documents(conn, client, dest)
    assert len(client.downloaded) == 1
    assert _row(conn, mid)["status"] == "downloading"


def test_file_names_on_disk_come_from_ids(conn, dest, tmp_path):
    mid, url = _doc(conn, "../../x.pdf")
    fetch_documents(conn, FakeClient({url: make_pdf(["Hola"])}), dest)
    assert _files(dest) == [f"{mid}.pdf"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["documentos"]
    assert _row(conn, mid)["ref"] == f"documentos/{mid}.pdf"


def test_fetch_is_bounded_per_run_and_in_id_order(conn, dest):
    urls = {}
    ids = []
    for i in range(DOWNLOADS_PER_RUN + 2):
        mid, url = _doc(conn, f"g{i}.pdf")
        ids.append(mid)
        urls[url] = make_pdf(["Hola"])
    client = FakeClient(urls)
    assert fetch_documents(conn, client, dest) == DOWNLOADS_PER_RUN
    assert [d[1].name for d in client.downloaded] == [f"{i}.bin" for i in ids[:DOWNLOADS_PER_RUN]]
    assert fetch_documents(conn, client, dest, limit=1) == 1
    assert _row(conn, ids[-2])["status"] == "pending"
    assert _row(conn, ids[-1])["status"] == "downloading"


def test_fetch_never_holds_a_transaction_during_download(conn, dest):
    mid, url = _doc(conn)
    conn.execute("UPDATE kv SET value=value")    # algo abierto de antes
    conn.execute("INSERT INTO kv(key, value) VALUES ('x', '1')")
    states = []

    class Watching(FakeClient):
        def download_file(self, fileurl, dest, *, max_bytes):
            states.append(conn.in_transaction)
            return super().download_file(fileurl, dest, max_bytes=max_bytes)

    fetch_documents(conn, Watching({url: make_pdf(["Hola"])}), dest)
    assert states == [False]
    assert not conn.in_transaction


class FlakyCampus(FakeClient):
    """Campus donde algunas URLs dan siempre 404."""

    def __init__(self, payloads, broken):
        super().__init__(payloads)
        self.broken = broken

    def download_file(self, fileurl, dest, *, max_bytes):
        if fileurl in self.broken:
            self.downloaded.append((fileurl, dest, max_bytes))
            raise MoodleUnreachable("HTTP 404 en https://campus/x")
        return super().download_file(fileurl, dest, max_bytes=max_bytes)


def test_failing_downloads_do_not_starve_the_queue(conn, dest):
    broken = [_doc(conn, f"roto{i}.pdf") for i in range(12)]
    good_id, good_url = _doc(conn, "bueno.pdf")
    client = FlakyCampus({good_url: make_pdf(["Hola"])}, {url for _, url in broken})
    syncs_until_good = None
    for sync in range(1, 7):
        fetch_documents(conn, client, dest)
        if syncs_until_good is None and _row(conn, good_id)["status"] == "pending":
            syncs_until_good = sync
    assert syncs_until_good is not None and syncs_until_good <= 2
    for mid, _ in broken:
        row = _row(conn, mid)
        assert (row["status"], row["fail_kind"], row["error"]) == (
            "failed", FAIL_DOWNLOAD, DOWNLOAD_FAILED_TEXT)
        assert row["download_attempts"] == MAX_DOWNLOAD_ATTEMPTS
    # Cada archivo roto se intentó exactamente MAX_DOWNLOAD_ATTEMPTS veces, una por sincronización.
    tries = [d[0] for d in client.downloaded]
    assert all(tries.count(url) == MAX_DOWNLOAD_ATTEMPTS for _, url in broken)


def test_retry_failed_puts_failed_downloads_back_in_line(conn, dest):
    from estudia.generation.pipeline import RETRY_ON_NEW_KEY, retry_failed

    mid, url = _doc(conn)
    for _ in range(MAX_DOWNLOAD_ATTEMPTS):
        fetch_documents(conn, FakeClient(error=MoodleUnreachable("HTTP 404")), dest)
    assert _row(conn, mid)["status"] == "failed"
    # Una clave nueva de Gemini no cambia nada para una descarga.
    retry_failed(conn, RETRY_ON_NEW_KEY)
    assert _row(conn, mid)["status"] == "failed"
    # El botón "Reintentar las que fallaron" la vuelve a poner para bajar, de cero.
    assert retry_failed(conn) == 1
    row = _row(conn, mid)
    assert (row["status"], row["error"], row["fail_kind"], row["download_attempts"]) == (
        "downloading", None, None, 0)
    assert fetch_documents(conn, FakeClient({url: make_pdf(["Hola"])}), dest) == 1
    assert _row(conn, mid)["status"] == "pending"


def test_download_accessexception_never_logs_out(conn, dest):
    """Solo un `invalidtoken` de verdad cierra la sesión; un archivo restringido no."""
    mid, _ = _doc(conn)
    fetch_documents(conn, FakeClient(error=TokenInvalid("accessexception")), dest)
    assert _row(conn, mid)["status"] == "downloading"
    with pytest.raises(TokenInvalid):
        fetch_documents(conn, FakeClient(error=TokenInvalid("invalidtoken")), dest)


@pytest.mark.parametrize("outline, expected", [
    ([("Unidad 1/2", 1), ("Unidad 2", 36)], ["Unidad 1/2", "Unidad 2"]),
    ([("Lí‮mites\x00​", 1), ("\x07 ", 36)], ["Límites", "Guía 1, págs. 36–70"]),
    ([("x" * 500, 1), ("Fin", 36)], ["x" * 120, "Fin"]),
])
def test_part_titles_are_sanitized_for_display(conn, dest, outline, expected):
    mid, url = _doc(conn)
    pdf = make_pdf([""] * 70, outline=outline)
    fetch_documents(conn, FakeClient({url: pdf}), dest)
    titles = [r[0] for r in conn.execute(
        "SELECT title FROM material WHERE parent_id=? ORDER BY page_from", (mid,))]
    assert titles == expected


def _failing_inspect(monkeypatch, fail_for: set[str], error: Exception):
    real = fetch.inspect_document

    def inspect(path, kind, **kwargs):
        if Path(path).stem in fail_for:
            raise error
        return real(path, kind, **kwargs)

    monkeypatch.setattr(fetch, "inspect_document", inspect)


def test_unexpected_error_does_not_jam_the_queue(conn, dest, monkeypatch, caplog):
    bad, bad_url = _doc(conn, "raro.pdf")
    good, good_url = _doc(conn, "bueno.pdf")
    _failing_inspect(monkeypatch, {str(bad)}, RuntimeError("secreto /ruta/x"))
    client = FakeClient({bad_url: make_pdf(["Hola"]), good_url: make_pdf(["Hola"])})
    assert fetch_documents(conn, client, dest) == 1
    assert _row(conn, good)["status"] == "pending"
    row = _row(conn, bad)
    assert (row["status"], row["download_attempts"], row["ref"]) == ("downloading", 1, None)
    # Ni el .bin ni el archivo ya renombrado quedan en la carpeta.
    assert _files(dest) == [f"{good}.pdf"]
    assert "RuntimeError" in caplog.text and "secreto" not in caplog.text
    assert not conn.in_transaction
    for _ in range(MAX_DOWNLOAD_ATTEMPTS - 1):
        fetch_documents(conn, client, dest)
    assert (_row(conn, bad)["status"], _row(conn, bad)["fail_kind"]) == ("failed", FAIL_DOWNLOAD)
    assert _files(dest) == [f"{good}.pdf"]


def test_error_while_splitting_rolls_back_and_removes_the_file(conn, dest, monkeypatch):
    mid, url = _doc(conn)

    def broken_plan(info):
        raise RuntimeError("roto")

    monkeypatch.setattr(fetch, "plan_parts", broken_plan)
    fetch_documents(conn, FakeClient({url: blank_pdf(70)}), dest)
    row = _row(conn, mid)
    assert (row["status"], row["ref"], row["pages"]) == ("downloading", None, None)
    assert _files(dest) == []


def test_busy_sandbox_retries_later_without_counting(conn, dest, monkeypatch):
    mid, url = _doc(conn)
    _failing_inspect(monkeypatch, {str(mid)}, DocumentBusy("ocupado"))
    for _ in range(MAX_DOWNLOAD_ATTEMPTS + 1):
        assert fetch_documents(conn, FakeClient({url: make_pdf(["Hola"])}), dest) == 0
    row = _row(conn, mid)
    assert row["status"] == "downloading"
    assert not row["download_attempts"]
    assert _files(dest) == []
    monkeypatch.undo()
    assert fetch_documents(conn, FakeClient({url: make_pdf(["Hola"])}), dest) == 1


def test_sweeps_stray_download_files(conn, dest):
    mid, _ = _doc(conn)
    dest.mkdir()
    keep = [f"{mid}.bin", f".{mid}.bin.abc123.part", "3.pdf", "4.pptx"]
    stray = ["998.bin", ".999.bin.xyz.part", ".raro.part", "x.bin"]
    for name in keep + stray:
        (dest / name).write_bytes(b"x")
    fetch_documents(conn, FakeClient(), dest, limit=0)
    assert _files(dest) == sorted(keep)


@pytest.mark.parametrize("kind, url", [
    ("file", f"{SITE_URL}/webservice/pluginfile.php/1/mod_resource/content/0/a.docx"),
    ("pdf", None),
])
def test_downloading_row_that_cannot_be_fetched_is_unsupported(conn, dest, kind, url):
    cur = conn.execute(
        "INSERT INTO material(section_id, kind, title, status, remote_url)"
        " VALUES (1, ?, 'Raro', 'downloading', ?)", (kind, url))
    conn.commit()
    client = FakeClient()
    assert fetch_documents(conn, client, dest) == 0
    row = _row(conn, cur.lastrowid)
    assert (row["kind"], row["status"], row["error"]) == ("file", "unsupported", "No se puede leer todavía")
    assert client.downloaded == []
