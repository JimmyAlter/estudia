"""La cita de un documento: "Apunte 3, pág. 14" y el enlace a su página del campus."""
import pytest

from estudia.catalog import Site
from estudia.db import connect
from estudia.study import citation
from estudia.study.citation import campus_url, source_label

SITE = "https://campus.example.edu"


def test_source_label_pdf_and_slides():
    assert source_label("pdf", "Apunte 3", None, 14) == "Apunte 3, pág. 14"
    assert source_label("slides", "Clase 2", None, 5) == "Clase 2, diap. 5"
    assert source_label("youtube", "Video 1", 42, None) is None
    assert source_label(None, "Video viejo", 42, None) is None


def test_source_label_document_without_page_is_its_title():
    assert source_label("pdf", "Apunte 3", None, None) == "Apunte 3"
    assert source_label("slides", "Clase 2", None, None) == "Clase 2"
    assert source_label("pdf", None, None, 4) is None
    assert source_label("pdf", "   ", None, 4) is None


def test_campus_url_per_module_kind():
    assert campus_url(SITE, {"cmid": 77, "module_kind": None, "module_id": None}) == (
        f"{SITE}/mod/page/view.php?id=77")
    assert campus_url(SITE, {"cmid": None, "module_kind": "resource", "module_id": 9}) == (
        f"{SITE}/mod/resource/view.php?id=9")
    assert campus_url(SITE, {"cmid": None, "module_kind": "folder", "module_id": 12}) == (
        f"{SITE}/mod/folder/view.php?id=12")
    assert campus_url(SITE, {"cmid": None, "module_kind": None, "module_id": None}) is None
    assert campus_url(None, {"cmid": 77, "module_kind": None, "module_id": None}) is None


def test_campus_url_never_uses_file_url():
    row = {"cmid": None, "module_kind": "resource", "module_id": 9,
           "remote_url": f"{SITE}/webservice/pluginfile.php/1/x.pdf?token=SECRETO"}
    url = campus_url(SITE, row)
    assert "pluginfile" not in url and "token" not in url


@pytest.mark.parametrize("module_id", ["9 OR 1", "9&token=x", -1, 0])
def test_campus_url_only_positive_int_ids(module_id):
    assert campus_url(SITE, {"cmid": None, "module_kind": "resource", "module_id": module_id}) is None


def test_site_url_only_http(tmp_path):
    conn = connect(tmp_path / "t.db")
    assert citation.site_url(conn) is None
    conn.execute("INSERT INTO kv(key, value) VALUES ('site_id', 'x')")
    conn.commit()
    assert citation.site_url(conn, lambda sid: Site(sid, "X", "X", SITE + "/", "browser")) == SITE
    assert citation.site_url(conn, lambda sid: Site(sid, "X", "X", "javascript:alert(1)", "browser")) is None

    def missing(sid):
        raise KeyError(sid)

    assert citation.site_url(conn, missing) is None
