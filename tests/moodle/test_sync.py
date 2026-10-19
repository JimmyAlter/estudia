import copy
import json
from pathlib import Path

import pytest

from estudia.db import connect
from estudia.moodle.client import FileTooLarge, ForeignFileUrl, MoodleError, TooManyRedirects
from estudia.moodle.sync import sync_all

FIXTURES = Path(__file__).parent.parent / "fixtures" / "moodle"
SITE = "utn-frba"
PAGE = (FIXTURES / "page_video_clase_1.html").read_text("utf-8")
CONTENTS = json.loads((FIXTURES / "core_course_get_contents_101.json").read_text("utf-8"))
QUIZZES = json.loads((FIXTURES / "mod_quiz_get_quizzes_by_courses_101.json").read_text("utf-8"))


class FakeClient:
    site_url = "https://aulasvirtuales.frba.utn.edu.ar"

    def __init__(self, contents=CONTENTS):
        self.contents = contents
        self.calls: list[str] = []
        self.fetched: list[str] = []

    def call(self, fn, **params):
        self.calls.append(fn)
        if fn == "core_webservice_get_site_info":
            return {"userid": 7, "fullname": "Ana Alumna"}
        if fn == "core_enrol_get_users_courses":
            return [{"id": 101, "fullname": "AM1 - 8.30 HS", "shortname": "AM1 - 8.30 HS"}]
        if fn == "core_course_get_contents":
            return self.contents
        if fn == "mod_quiz_get_quizzes_by_courses":
            return QUIZZES
        if fn == "mod_assign_get_assignments":
            return {"courses": []}
        if fn == "core_calendar_get_action_events_by_timesort":
            return {"events": []}
        raise AssertionError(f"función inesperada: {fn}")

    def fetch_file(self, fileurl):
        self.fetched.append(fileurl)
        # id de YouTube distinto (11 caracteres) por cada fileurl
        n = fileurl.split("mod_page")[0].rstrip("/").rsplit("/", 1)[-1]
        return PAGE.replace("aaaaaaaaaa1", f"vid{int(n):08d}").encode()


@pytest.fixture
def conn():
    return connect(Path(":memory:"))


def test_sync_from_fixtures(conn):
    cliente = FakeClient()
    res = sync_all(conn, cliente, SITE)
    mats = conn.execute("SELECT * FROM material").fetchall()
    assert len(mats) == 8
    assert {(m["kind"], m["status"]) for m in mats} == {("youtube", "pending")}
    assert len({m["ref"] for m in mats}) == 8
    primero = conn.execute("SELECT * FROM material WHERE cmid=1798494").fetchone()
    assert primero["title"] == "Video-clase 1: Rectas."
    assert conn.execute("SELECT COUNT(*) FROM section").fetchone()[0] == 8
    assert conn.execute("SELECT COUNT(*) FROM section WHERE visible=1").fetchone()[0] == 3
    ex = conn.execute("SELECT * FROM exam").fetchall()
    assert len(ex) == 1
    assert (ex[0]["name"], ex[0]["attempts"], ex[0]["time_limit_s"], ex[0]["max_grade"]) == (
        "PRIMER CUESTIONARIO", 1, 7200, 12)
    assert (ex[0]["opens_at"], ex[0]["cmid"], ex[0]["course_id"]) == (1805061600, 5001, 101)
    assert (res.new_materials, res.new_sections) == (8, 8)
    assert conn.execute("SELECT value FROM kv WHERE key='last_sync_at'").fetchone() is not None
    assert conn.execute("SELECT fullname FROM course").fetchone()[0] == "AM1 - 8.30 HS"
    assert set(cliente.calls) == {
        "core_webservice_get_site_info", "core_enrol_get_users_courses",
        "core_course_get_contents", "mod_quiz_get_quizzes_by_courses",
        "mod_assign_get_assignments", "core_calendar_get_action_events_by_timesort"}


def test_sync_is_incremental(conn):
    sync_all(conn, FakeClient(), SITE)
    conn.execute("UPDATE material SET status='done', summary_md='resumen'")
    cliente = FakeClient()
    res = sync_all(conn, cliente, SITE)
    assert (res.new_materials, res.new_sections) == (0, 0)
    assert cliente.fetched == []
    estados = conn.execute("SELECT DISTINCT status, summary_md FROM material").fetchall()
    assert [tuple(e) for e in estados] == [("done", "resumen")]
    assert conn.execute("SELECT COUNT(*) FROM exam").fetchone()[0] == 1


def test_sync_adds_newly_visible_section(conn):
    sync_all(conn, FakeClient(), SITE)
    nuevo = copy.deepcopy(CONTENTS)
    sec = nuevo[3]
    sec["uservisible"] = True
    sec["visible"] = 1
    modulo = copy.deepcopy(CONTENTS[2]["modules"][0])
    modulo.update(id=1900001)
    modulo["contents"][0]["fileurl"] = (
        "https://aulasvirtuales.frba.utn.edu.ar/webservice/pluginfile.php/99/mod_page/content/index.html")
    sec["modules"] = [modulo]
    res = sync_all(conn, FakeClient(nuevo), SITE)
    assert res.new_materials == 1
    assert res.new_sections == 0
    assert conn.execute("SELECT visible FROM section WHERE id=?", (sec["id"],)).fetchone()[0] == 1


def test_page_without_youtube_is_unsupported(conn):
    class SinVideo(FakeClient):
        def fetch_file(self, fileurl):
            return b"<p>sin video</p>"

    sync_all(conn, SinVideo(), SITE)
    filas = conn.execute("SELECT DISTINCT kind, status, ref FROM material").fetchall()
    assert [tuple(f) for f in filas] == [("page", "unsupported", None)]


def test_unsupported_page_is_rechecked_only_when_edited(conn):
    class SinVideo(FakeClient):
        def fetch_file(self, fileurl):
            self.fetched.append(fileurl)
            return b"<p>sin video</p>"

    sync_all(conn, SinVideo(), SITE)
    pages = conn.execute("SELECT COUNT(*) FROM material").fetchone()[0]

    # Sin cambios en el campus: no se vuelve a bajar nada.
    cliente = FakeClient()
    assert sync_all(conn, cliente, SITE).new_materials == 0
    assert cliente.fetched == []

    # El profe editó una page y agregó el video.
    editado = copy.deepcopy(CONTENTS)
    modulo = next(m for sec in editado for m in sec.get("modules", []) if m["modname"] == "page"
                  and sec.get("uservisible", sec.get("visible")))
    modulo["contentsinfo"]["lastmodified"] = 4_000_000_000
    cliente = FakeClient(editado)
    assert sync_all(conn, cliente, SITE).new_materials == 1
    assert len(cliente.fetched) == 1
    fila = conn.execute("SELECT kind, status, ref FROM material WHERE cmid=?", (modulo["id"],)).fetchone()
    assert (fila["kind"], fila["status"]) == ("youtube", "pending")
    assert len(fila["ref"]) == 11
    assert conn.execute("SELECT COUNT(*) FROM material").fetchone()[0] == pages


def test_recheck_fetch_error_keeps_the_page_and_the_sync(conn, caplog):
    from estudia.moodle.client import MoodleError

    class SinVideo(FakeClient):
        def fetch_file(self, fileurl):
            return b"<p>sin video</p>"

    sync_all(conn, SinVideo(), SITE)
    editado = copy.deepcopy(CONTENTS)
    modulo = next(m for sec in editado for m in sec.get("modules", []) if m["modname"] == "page"
                  and sec.get("uservisible", sec.get("visible")))
    modulo["contentsinfo"]["lastmodified"] = 4_000_000_000
    antes = conn.execute("SELECT updated_at FROM material WHERE cmid=?", (modulo["id"],)).fetchone()[0]
    conn.execute("UPDATE material SET updated_at=? WHERE cmid=?", (antes - 10, modulo["id"]))
    conn.commit()

    class Falla(FakeClient):
        def fetch_file(self, fileurl):
            raise MoodleError(f"no se pudo bajar {fileurl}?token=SECRETO")

    res = sync_all(conn, Falla(editado), SITE)
    assert res.new_materials == 0
    fila = conn.execute("SELECT status, updated_at FROM material WHERE cmid=?", (modulo["id"],)).fetchone()
    assert fila["status"] == "unsupported"
    assert fila["updated_at"] > antes - 10
    assert conn.execute("SELECT COUNT(*) FROM exam").fetchone()[0] == 1  # el resto se sincronizó
    assert "SECRETO" not in caplog.text


def test_failed_sync_leaves_no_partial_state(conn):
    class Falla(FakeClient):
        def call(self, fn, **params):
            if fn == "mod_quiz_get_quizzes_by_courses":
                raise RuntimeError("caída")
            return super().call(fn, **params)

    with pytest.raises(RuntimeError):
        sync_all(conn, Falla(), SITE)
    assert not conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM course").fetchone()[0] == 0


def test_fetch_phase_does_not_hold_the_write_lock(tmp_path):
    """Mientras se baja todo del campus, otra conexión puede escribir (busy_timeout=0)."""
    import sqlite3

    path = tmp_path / "t.db"
    conn = connect(path)
    escrituras = []

    class EscribeMientras(FakeClient):
        def _otra_escribe(self, fn):
            otra = sqlite3.connect(path, timeout=0)
            try:
                otra.execute("PRAGMA busy_timeout = 0")
                otra.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?, '1')", (f"durante:{fn}",))
                otra.commit()
                escrituras.append(fn)
            finally:
                otra.close()

        def call(self, fn, **params):
            out = super().call(fn, **params)
            self._otra_escribe(fn)
            return out

        def fetch_file(self, fileurl):
            out = super().fetch_file(fileurl)
            self._otra_escribe("fetch_file")
            return out

    cliente = EscribeMientras()
    res = sync_all(conn, cliente, SITE)
    assert res.new_materials == 8
    assert set(escrituras) == set(cliente.calls) | {"fetch_file"}
    assert conn.execute("SELECT COUNT(*) FROM material").fetchone()[0] == 8
    assert conn.execute("SELECT COUNT(*) FROM kv WHERE key LIKE 'durante:%'").fetchone()[0] == 7
    assert not conn.in_transaction


def test_failure_after_fetch_writes_nothing(tmp_path, monkeypatch):
    from estudia.moodle import sync

    conn = connect(tmp_path / "t.db")

    def boom(*a, **k):
        raise RuntimeError("falla al escribir")

    monkeypatch.setattr(sync, "_sync_exam", boom)
    with pytest.raises(RuntimeError):
        sync_all(conn, FakeClient(), SITE)
    assert not conn.in_transaction
    for table in ("site", "course", "section", "material", "exam"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table


@pytest.mark.parametrize("error", [ForeignFileUrl, TooManyRedirects, FileTooLarge])
def test_bad_page_does_not_stop_sync(conn, caplog, error):
    """Una page con URL ajena, bucle de redirecciones o enorme queda sin leer; el resto sigue."""
    paginas = [m["id"] for sec in CONTENTS for m in sec.get("modules", [])
               if m["modname"] == "page" and sec.get("uservisible", sec.get("visible"))]
    mala = paginas[1]
    url_mala = next(m["contents"][0]["fileurl"] for sec in CONTENTS
                    for m in sec.get("modules", []) if m["id"] == mala)

    class UnaMala(FakeClient):
        def fetch_file(self, fileurl):
            if fileurl == url_mala:
                self.fetched.append(fileurl)
                raise error("algo raro")
            return super().fetch_file(fileurl)

    cliente = UnaMala()
    sync_all(conn, cliente, SITE)
    assert len(cliente.fetched) == len(paginas)            # se siguió con las demás
    fila = conn.execute("SELECT kind, status, ref FROM material WHERE cmid=?", (mala,)).fetchone()
    assert tuple(fila) == ("page", "unsupported", None)
    buenas = conn.execute(
        "SELECT COUNT(*) FROM material WHERE kind='youtube' AND status='pending'").fetchone()[0]
    assert buenas == len(paginas) - 1
    assert conn.execute("SELECT COUNT(*) FROM exam").fetchone()[0] == 1   # el sync terminó
    assert "token" not in caplog.text.lower() and url_mala not in caplog.text


def test_other_moodle_error_on_new_page_still_raises(conn):
    class Rota(FakeClient):
        def fetch_file(self, fileurl):
            raise MoodleError("nopermissions")

    with pytest.raises(MoodleError):
        sync_all(conn, Rota(), SITE)
