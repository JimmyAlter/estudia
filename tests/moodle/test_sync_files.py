"""Archivos del campus (`resource` y `folder`) en la sincronización."""

import copy
import json
import logging
from pathlib import Path

import httpx
import pytest

from estudia.db import connect
from estudia.material.fetch import fetch_documents
from estudia.material.files import MAX_CAMPUS_FILE_BYTES
from estudia.moodle.client import MoodleClient
from estudia.moodle.sync import sync_all
from docs_factory import make_pdf

FIXTURES = Path(__file__).parent.parent / "fixtures" / "moodle"
SITE = "utn-frba"
SITE_URL = "https://aulasvirtuales.frba.utn.edu.ar"
CONTENTS = json.loads((FIXTURES / "core_course_get_contents_files.json").read_text("utf-8"))


def _contents_call(contents):
    def call(fn, **params):
        if fn == "core_webservice_get_site_info":
            return {"userid": 7, "fullname": "Ana Alumna"}
        if fn == "core_enrol_get_users_courses":
            return [{"id": 101, "fullname": "AM1", "shortname": "AM1"}]
        if fn == "core_course_get_contents":
            return contents
        if fn == "mod_quiz_get_quizzes_by_courses":
            return {"quizzes": []}
        if fn == "mod_assign_get_assignments":
            return {"courses": []}
        if fn == "core_calendar_get_action_events_by_timesort":
            return {"events": []}
        raise AssertionError(f"función inesperada: {fn}")
    return call


class FakeClient:
    site_url = SITE_URL

    def __init__(self, contents=CONTENTS):
        self.call = _contents_call(contents)
        self.downloaded: list[str] = []

    def fetch_file(self, fileurl):
        raise AssertionError("no hay pages en esta fixture")

    def download_file(self, fileurl, dest, *, max_bytes):
        self.downloaded.append(fileurl)
        data = make_pdf(["Hola"])
        dest.write_bytes(data)
        return len(data)


@pytest.fixture
def conn():
    return connect(Path(":memory:"))


def _files(conn):
    return {r["file_key"]: r for r in conn.execute(
        "SELECT * FROM material WHERE file_key IS NOT NULL")}


def _rows(conn):
    return [tuple(r) for r in conn.execute("SELECT * FROM material ORDER BY id")]


def test_resource_and_folder_files_become_materials(conn):
    res = sync_all(conn, FakeClient(), SITE)
    files = _files(conn)
    # El `resource` aporta un solo archivo: el de `sortorder` más alto. Lo oculto no entra.
    assert set(files) == {"7001:/guia1.pdf", "7002:/Apunte 3.pptx", "7002:/extra/Programa.docx"}
    assert res.new_materials == 3

    guia = files["7001:/guia1.pdf"]
    assert (guia["kind"], guia["status"], guia["title"]) == ("pdf", "downloading", "Guía de ejercicios 1")
    assert (guia["module_id"], guia["module_kind"], guia["section_id"]) == (7001, "resource", 9001)
    assert guia["remote_size"] == 240000
    assert guia["remote_url"].startswith(f"{SITE_URL}/webservice/pluginfile.php/3900001/")
    assert guia["cmid"] is None and guia["ref"] is None

    apunte = files["7002:/Apunte 3.pptx"]
    assert (apunte["kind"], apunte["status"], apunte["title"]) == ("slides", "downloading", "Apuntes: Apunte 3")
    assert apunte["module_kind"] == "folder"

    programa = files["7002:/extra/Programa.docx"]
    assert (programa["kind"], programa["status"]) == ("file", "unsupported")
    assert programa["error"] == "No se puede leer todavía"
    assert programa["title"] == "Apuntes: Programa"


def test_campus_file_over_50mb_marked_too_large_without_download(conn, tmp_path):
    contents = copy.deepcopy(CONTENTS)
    contents[0]["modules"][0]["contents"][1]["filesize"] = MAX_CAMPUS_FILE_BYTES + 1
    client = FakeClient(contents)
    sync_all(conn, client, SITE)
    guia = _files(conn)["7001:/guia1.pdf"]
    assert (guia["kind"], guia["status"], guia["error"]) == ("pdf", "too_large", "Archivo demasiado grande")

    fetch_documents(conn, client, tmp_path / "documentos")
    assert all("guia1" not in url for url in client.downloaded)
    assert conn.execute("SELECT status FROM material WHERE id=?", (guia["id"],)).fetchone()[0] == "too_large"


@pytest.mark.parametrize("url", [
    "https://evil.example/webservice/pluginfile.php/1/mod_resource/content/0/guia1.pdf",
    "https://aulasvirtuales.frba.utn.edu.ar.evil.example/webservice/pluginfile.php/1/x/guia1.pdf",
    "https://aulasvirtuales.frba.utn.edu.ar/pluginfile.php/1/mod_resource/content/0/guia1.pdf",
    "javascript:alert(1)",
])
def test_foreign_fileurl_never_stored_or_downloaded(conn, tmp_path, url):
    contents = copy.deepcopy(CONTENTS)
    contents[0]["modules"][0]["contents"][1]["fileurl"] = url
    client = FakeClient(contents)
    sync_all(conn, client, SITE)
    guia = _files(conn)["7001:/guia1.pdf"]
    assert (guia["kind"], guia["status"], guia["remote_url"]) == ("file", "unsupported", None)
    assert url not in "\n".join(conn.iterdump())

    fetch_documents(conn, client, tmp_path / "documentos")
    assert url not in client.downloaded
    assert all("guia1" not in u for u in client.downloaded)


def test_remote_url_saved_without_token(conn, tmp_path, caplog):
    """Review Focus 2: con el cliente real, el token viaja al campus pero no queda en la base."""
    token = "SECRETO-del-token-123"
    contents = copy.deepcopy(CONTENTS)
    # Un campus que ya manda la `fileurl` con un token: tampoco se guarda.
    contents[0]["modules"][0]["contents"][1]["fileurl"] += f"&token={token}"
    pdf = make_pdf(["Hola"])
    seen_tokens = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/webservice/rest/server.php"):
            form = dict(httpx.QueryParams(request.content.decode()))
            assert form["wstoken"] == token
            return httpx.Response(200, json=_contents_call(contents)(form["wsfunction"]))
        seen_tokens.append(request.url.params.get("token"))
        if request.url.path.endswith(".pdf"):
            return httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})
        return httpx.Response(200, content=b"PK\x03\x04no es un pptx")

    caplog.set_level(logging.DEBUG)
    client = MoodleClient(SITE_URL, token, httpx.Client(transport=httpx.MockTransport(handler)))
    sync_all(conn, client, SITE)
    assert fetch_documents(conn, client, tmp_path / "documentos") == 1

    assert seen_tokens and set(seen_tokens) == {token}   # el token sí viajó, al campus
    guia = _files(conn)["7001:/guia1.pdf"]
    assert guia["status"] == "pending"
    assert "token=" not in guia["remote_url"]
    assert guia["remote_url"].endswith("guia1.pdf?forcedownload=1")
    dump = "\n".join(conn.iterdump())
    assert token not in dump
    assert "token=" not in dump
    assert token not in caplog.text


def test_sync_files_idempotent(conn):
    sync_all(conn, FakeClient(), SITE)
    first = _rows(conn)
    res = sync_all(conn, FakeClient(), SITE)
    assert res.new_materials == 0
    assert _rows(conn)[:len(first)] == first
    assert len(_rows(conn)) == len(first)


def test_known_file_only_updates_title(conn):
    sync_all(conn, FakeClient(), SITE)
    guia = _files(conn)["7001:/guia1.pdf"]
    conn.execute("UPDATE material SET status='done' WHERE id=?", (guia["id"],))
    conn.commit()
    contents = copy.deepcopy(CONTENTS)
    contents[0]["modules"][0]["name"] = "Guía 1 (corregida)"
    sync_all(conn, FakeClient(contents), SITE)
    after = _files(conn)["7001:/guia1.pdf"]
    assert (after["id"], after["title"], after["status"]) == (guia["id"], "Guía 1 (corregida)", "done")
    assert (after["remote_size"], after["remote_modified"]) == (240000, 1790000000)


def _done_split_guide(conn):
    """La guía bajada, partida en dos tramos ya hechos, cada uno con una pregunta."""
    sync_all(conn, FakeClient(), SITE)
    guia = _files(conn)["7001:/guia1.pdf"]
    gid = guia["id"]
    conn.execute("UPDATE material SET status='split', ref='documentos/x.pdf', pages=80,"
                 " file_sha256='viejo' WHERE id=?", (gid,))
    conn.execute(
        "INSERT INTO material(id, section_id, kind, title, ref, status, parent_id, page_from,"
        " page_to, summary_md) VALUES (900, 9001, 'pdf', 'Cap 1', 'documentos/x.pdf', 'done', ?,"
        " 1, 40, '# A'), (901, 9001, 'pdf', 'Cap 2', 'documentos/x.pdf', 'pending', ?, 41, 80, NULL)",
        (gid, gid))
    conn.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (1, 101, 'a', 'A')")
    conn.execute("INSERT INTO material_topic(material_id, topic_id) VALUES (900, 1)")
    conn.execute(
        "INSERT INTO question(id, material_id, topic_id, stem, options_json, correct_index,"
        " verified, retired, dedupe_key) VALUES (1, 900, 1, 'Vieja', '[\"1\",\"2\",\"3\",\"4\"]',"
        " 0, 1, 0, 'k900')")
    conn.commit()
    return gid


@pytest.mark.parametrize("field,value", [("timemodified", 1790009999), ("filesize", 250000)])
def test_replaced_campus_file_is_downloaded_again_and_old_questions_retired(conn, field, value):
    gid = _done_split_guide(conn)
    contents = copy.deepcopy(CONTENTS)
    contents[0]["modules"][0]["contents"][1][field] = value
    sync_all(conn, FakeClient(contents), SITE)
    guia = conn.execute("SELECT * FROM material WHERE id=?", (gid,)).fetchone()
    assert (guia["status"], guia["file_sha256"], guia["pages"]) == ("downloading", None, None)
    assert guia["remote_size"] == contents[0]["modules"][0]["contents"][1]["filesize"]
    assert guia["remote_modified"] == contents[0]["modules"][0]["contents"][1]["timemodified"]
    # Los tramos viejos salen de la cola y de la lista; su pregunta queda retirada, no borrada.
    old = {r["id"]: r["status"] for r in conn.execute(
        "SELECT id, status FROM material WHERE parent_id=?", (gid,))}
    assert old == {900: "replaced", 901: "replaced"}
    q = conn.execute("SELECT retired, verified FROM question WHERE id=1").fetchone()
    assert tuple(q) == (1, 1)
    assert conn.execute("SELECT COUNT(*) FROM material_topic WHERE material_id=900").fetchone()[0] == 0


def test_replaced_file_fetched_again_gets_new_parts(conn, tmp_path):
    gid = _done_split_guide(conn)
    contents = copy.deepcopy(CONTENTS)
    contents[0]["modules"][0]["contents"][1]["timemodified"] = 1790009999
    client = FakeClient(contents)
    sync_all(conn, client, SITE)
    fetch_documents(conn, client, tmp_path / "documentos")
    assert conn.execute("SELECT status FROM material WHERE id=?", (gid,)).fetchone()[0] == "pending"
    assert {r[0] for r in conn.execute("SELECT status FROM material WHERE parent_id=?", (gid,))} == {
        "replaced"}


def test_file_without_stored_timemodified_is_not_requeued(conn):
    """Una base de antes de guardar `timemodified`: la primera sincronización solo lo anota."""
    gid = _done_split_guide(conn)
    conn.execute("UPDATE material SET remote_modified=NULL WHERE id=?", (gid,))
    conn.commit()
    sync_all(conn, FakeClient(), SITE)
    guia = conn.execute("SELECT * FROM material WHERE id=?", (gid,)).fetchone()
    assert (guia["status"], guia["remote_modified"]) == ("split", 1790000000)
    assert conn.execute("SELECT retired FROM question WHERE id=1").fetchone()[0] == 0


def test_replaced_whole_document_can_store_the_same_stem_again(conn):
    """Un documento entero vuelve a generar el mismo enunciado: la vieja retirada no lo tapa."""
    sync_all(conn, FakeClient(), SITE)
    gid = _files(conn)["7001:/guia1.pdf"]["id"]
    conn.execute("UPDATE material SET status='done' WHERE id=?", (gid,))
    conn.execute(
        "INSERT INTO question(id, material_id, topic_id, stem, options_json, correct_index,"
        " verified, retired, dedupe_key) VALUES (1, ?, 1, 'Vieja', '[]', 0, 1, 0, 'k')", (gid,))
    conn.commit()
    contents = copy.deepcopy(CONTENTS)
    contents[0]["modules"][0]["contents"][1]["timemodified"] = 1790009999
    sync_all(conn, FakeClient(contents), SITE)
    row = conn.execute("SELECT retired, dedupe_key FROM question WHERE id=1").fetchone()
    assert (row["retired"], row["dedupe_key"]) == (1, None)



@pytest.mark.parametrize("suffix", [
    "&amp;token=SECRETO1",
    "&TOKEN=SECRETO1&wstoken=SECRETO1",
    "&mytoken=SECRETO1",
])
def test_token_query_variants_are_stripped(conn, suffix):
    contents = copy.deepcopy(CONTENTS)
    contents[0]["modules"][0]["contents"][1]["fileurl"] += suffix
    sync_all(conn, FakeClient(contents), SITE)
    guia = _files(conn)["7001:/guia1.pdf"]
    assert guia["status"] == "downloading"
    assert "SECRETO1" not in guia["remote_url"] and "token" not in guia["remote_url"].lower()
    assert guia["remote_url"].startswith(f"{SITE_URL}/webservice/pluginfile.php/3900001/")


def test_token_path_param_is_stripped(conn):
    contents = copy.deepcopy(CONTENTS)
    base = f"{SITE_URL}/webservice/pluginfile.php/3900001/mod_resource/content/0/guia1.pdf"
    contents[0]["modules"][0]["contents"][1]["fileurl"] = base + ";token=SECRETO1?forcedownload=1"
    sync_all(conn, FakeClient(contents), SITE)
    guia = _files(conn)["7001:/guia1.pdf"]
    assert (guia["status"], guia["remote_url"]) == ("downloading", base + "?forcedownload=1")


def test_filename_mentioning_token_is_kept(conn):
    """La regla "la URL todavía dice token" mira la query y los `;parámetros`, no el nombre."""
    contents = copy.deepcopy(CONTENTS)
    url = f"{SITE_URL}/webservice/pluginfile.php/3900001/mod_resource/content/0/tokens.pdf"
    contents[0]["modules"][0]["contents"][1]["fileurl"] = url + "?forcedownload=1&token=SECRETO1"
    sync_all(conn, FakeClient(contents), SITE)
    guia = _files(conn)["7001:/guia1.pdf"]
    assert (guia["status"], guia["remote_url"]) == ("downloading", url + "?forcedownload=1")


@pytest.mark.parametrize("fileurl", [
    f"{SITE_URL}/webservice/pluginfile.php/3900001/mod_resource/content/0/guia1.pdf?x=token:SECRETO1",
    f"{SITE_URL}/webservice/pluginfile.php/3900001/mod_resource/content/0/guia1.pdf?a=1;token=SECRETO1",
])
def test_url_still_mentioning_token_is_not_stored(conn, tmp_path, fileurl):
    contents = copy.deepcopy(CONTENTS)
    contents[0]["modules"][0]["contents"][1]["fileurl"] = fileurl
    client = FakeClient(contents)
    sync_all(conn, client, SITE)
    guia = _files(conn)["7001:/guia1.pdf"]
    assert (guia["kind"], guia["status"], guia["remote_url"]) == ("file", "unsupported", None)
    assert "SECRETO1" not in "\n".join(conn.iterdump())
    fetch_documents(conn, client, tmp_path / "documentos")
    assert all("guia1" not in u for u in client.downloaded)
