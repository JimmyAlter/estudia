"""API de "Cargá tus parciales viejos": cada archivo es hostil hasta que se demuestre lo contrario."""
import hashlib
import io
import time

import anyio
import pytest
import respx
from fastapi.testclient import TestClient
from PIL import Image

from docs_factory import blank_pdf, make_jpeg, make_pdf, png_claiming
from estudia.config import data_dir
from estudia.db import connect
from estudia.material.files import MAX_PAST_EXAM_BYTES
from estudia.server.app import create_app
from estudia.study.exams import create_manual_exam
from estudia.study.mock_exam import style_not_ready_text
from estudia.study.past_exams import (
    DUPLICATE_TEXT,
    LIMIT_TEXT,
    TOO_BIG_TEXT,
    TOO_MANY_PAGES_TEXT,
    UPLOAD_FAILED_TEXT,
    UPLOAD_UNREADABLE_TEXT,
    WRONG_TYPE_TEXT,
)
from test_app import BASE, PORT, FakeMoodle, make_provider

NOW = 1_790_000_000
MB = 1024 * 1024


@pytest.fixture
def env():
    base = data_dir()
    db_path = base / "estudia.db"
    app = create_app(
        db_path=db_path,
        moodle_client_factory=lambda url, token: FakeMoodle(url, token),
        ai_provider_factory=make_provider,
        open_browser=lambda url: None,
        web_dist=base / "no-existe",
        port=PORT,
    )
    conn = connect(db_path)
    conn.execute("INSERT INTO course(id, site_id, fullname, shortname) VALUES (10, 's', 'Análisis', 'AM')")
    conn.commit()
    exam_id = create_manual_exam(conn, 10, "Primer parcial", NOW + 86400, [], None, now=NOW)
    yield {"app": app, "client": TestClient(app, base_url=BASE), "conn": conn, "base": base,
           "exam_id": exam_id, "url": f"/api/exams/{exam_id}/past-exams"}
    conn.close()


def upload(env, data, filename="parcial.pdf", **kwargs):
    return env["client"].post(env["url"], files={"file": (filename, data, "application/octet-stream")},
                              **kwargs)


def files_in(base):
    return sorted(p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file())


def call_raw(app, url, headers, chunks):
    """Llama a la app ASGI directo: cuenta cuántas veces se leyó el cuerpo."""
    reads = []
    sent = []
    pending = list(chunks)

    async def receive():
        reads.append(1)
        if not pending:
            return {"type": "http.disconnect"}
        body = pending.pop(0)
        return {"type": "http.request", "body": body, "more_body": bool(pending)}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": url, "raw_path": url.encode(), "query_string": b"",
        "root_path": "", "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", PORT),
        "headers": [(b"host", f"127.0.0.1:{PORT}".encode())]
        + [(k.encode(), v.encode()) for k, v in headers.items()],
    }
    anyio.run(app, scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body, len(reads)


def multipart(data: bytes, boundary="limite") -> tuple[dict, bytes]:
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.pdf\"\r\n"
            "Content-Type: application/pdf\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return {"content-type": f"multipart/form-data; boundary={boundary}"}, body


def test_startup_sweeps_orphan_upload_copies():
    """Una carga cortada (la app se cerró a mitad) deja `parciales/.subiendo-*`, y una subida a
    Gemini cortada deja `.gemini-subida-*` en la carpeta de datos: al arrancar se borran."""
    base = data_dir()
    folder = base / "parciales"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".subiendo-abcd.pdf").write_bytes(b"x")
    (folder / "3.pdf").write_bytes(b"parcial guardado")
    (base / ".gemini-subida-abcd.pdf").write_bytes(b"x")
    (base / "otro.txt").write_bytes(b"no se toca")
    create_app(db_path=base / "estudia.db", moodle_client_factory=lambda url, token: FakeMoodle(url, token),
               ai_provider_factory=make_provider, open_browser=lambda url: None,
               web_dist=base / "no-existe", port=PORT)
    assert not (folder / ".subiendo-abcd.pdf").exists()
    assert not (base / ".gemini-subida-abcd.pdf").exists()
    assert (folder / "3.pdf").exists() and (base / "otro.txt").exists()


def test_upload_pdf_and_photo(env):
    r1 = upload(env, make_pdf(["Ejercicio 1: derivar"]), "parcial 2024.pdf")
    r2 = upload(env, make_jpeg(), "foto.jpg")
    assert r1.status_code == r2.status_code == 201
    assert r1.json()["name"] == "parcial 2024.pdf" and r1.json()["status"] == "pending"
    rows = env["conn"].execute("SELECT id, origin, kind FROM past_exam ORDER BY id").fetchall()
    assert [(r["origin"], r["kind"]) for r in rows] == [("parcial_propio", "pdf"), ("parcial_propio", "jpeg")]
    pdf_id, jpg_id = r1.json()["id"], r2.json()["id"]
    assert [r["id"] for r in rows] == [pdf_id, jpg_id]
    folder = env["base"] / "parciales"
    assert files_in(folder) == sorted([f"{pdf_id}.pdf", f"{jpg_id}.jpg"])
    stored = (folder / f"{jpg_id}.jpg").read_bytes()
    assert not Image.open(io.BytesIO(stored)).getexif()
    assert b"CamaraSecreta" not in stored
    listed = env["client"].get(env["url"]).json()
    assert [item["id"] for item in listed] == [pdf_id, jpg_id]
    assert env["client"].get("/api/exams").json()[0]["past_exams"] == 2


def test_type_checked_by_bytes(env):
    r = upload(env, make_pdf(["x"]), "foto.jpg")
    assert r.status_code == 201
    assert env["client"].get(env["url"]).json()[0]["kind"] == "pdf"
    r = upload(env, b"MZ\x90\x00" + b"\0" * 500, "parcial.pdf")
    assert r.status_code == 415
    assert r.json()["detail"] == WRONG_TYPE_TEXT


def test_upload_over_20mb_rejected_before_reading(env):
    headers = {"content-type": "multipart/form-data; boundary=x", "content-length": str(25 * MB)}
    status, body, reads = call_raw(env["app"], env["url"], headers, [b"x" * 1024] * 3)
    assert status == 413
    assert TOO_BIG_TEXT.encode() in body
    assert reads == 0


def test_body_longer_than_declared_is_cut_while_streaming(env):
    headers, body = multipart(make_pdf(["x"]) + b"\0" * (2 * MB))
    headers["content-length"] = "1000"
    chunks = [body[i:i + 64 * 1024] for i in range(0, len(body), 64 * 1024)]
    status, _, reads = call_raw(env["app"], env["url"], headers, chunks)
    assert status == 413
    assert reads < len(chunks)
    assert env["conn"].execute("SELECT COUNT(*) FROM past_exam").fetchone()[0] == 0


def test_file_over_20mb_with_honest_length_rejected(env):
    r = upload(env, b"%PDF-1.4\n" + b"\0" * (MAX_PAST_EXAM_BYTES + 10))
    assert r.status_code == 413
    assert r.json()["detail"] == TOO_BIG_TEXT
    assert files_in(env["base"] / "parciales") == []


def test_upload_without_content_length_411(env):
    headers, body = multipart(make_pdf(["x"]))
    status, out, reads = call_raw(env["app"], env["url"], headers, [body])
    assert status == 411
    assert reads == 0
    assert UPLOAD_FAILED_TEXT.encode() in out


@pytest.mark.parametrize("length", ["mucho", "-5"])
def test_upload_with_bad_content_length_says_try_again(env, length):
    headers, body = multipart(make_pdf(["x"]))
    headers["content-length"] = length
    status, out, _ = call_raw(env["app"], env["url"], headers, [body])
    assert status == 400
    assert UPLOAD_FAILED_TEXT.encode() in out
    assert b"inv" not in out


def test_upload_to_a_bad_exam_id_says_try_again(env):
    r = env["client"].post("/api/exams/abc/past-exams",
                           files={"file": ("a.pdf", make_pdf(["x"]), "application/pdf")})
    assert r.status_code == 422
    assert r.json()["detail"] == UPLOAD_FAILED_TEXT


def test_past_exam_page_bomb_rejected(env):
    r = upload(env, blank_pdf(31), "largo.pdf")
    assert r.status_code == 413
    assert r.json()["detail"] == TOO_MANY_PAGES_TEXT


def test_broken_pdf_rejected_as_unreadable(env):
    r = upload(env, b"%PDF-1.4\n" + b"no es un pdf " * 50)
    assert r.status_code == 400
    assert r.json()["detail"] == UPLOAD_UNREADABLE_TEXT


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
def test_image_bomb_rejected(env):
    r = upload(env, png_claiming(10000, 10000), "bomba.png")
    assert r.status_code == 400
    assert r.json()["detail"] == UPLOAD_UNREADABLE_TEXT
    assert files_in(env["base"] / "parciales") == []


def test_path_traversal_name_stays_in_folder(env):
    db = env["base"] / "estudia.db"
    before = db.read_bytes()
    before_files = set(files_in(env["base"]))
    r = upload(env, make_pdf(["x"]), "../../../estudia.db")
    assert r.status_code == 201
    assert r.json()["name"] == "estudia.db"
    new = set(files_in(env["base"])) - before_files
    assert new == {f"parciales/{r.json()['id']}.pdf"}
    # La base solo cambió por la fila nueva: sigue siendo SQLite y no es el PDF.
    assert db.read_bytes()[:16] == before[:16] == b"SQLite format 3\x00"


def test_sixth_past_exam_rejected(env):
    for i in range(5):
        assert upload(env, make_pdf([f"Parcial {i}"])).status_code == 201
    r = upload(env, make_pdf(["Parcial 6"]))
    assert r.status_code == 409
    assert r.json()["detail"] == LIMIT_TEXT


def test_duplicate_rejected(env):
    data = make_pdf(["Parcial"])
    assert upload(env, data, "a.pdf").status_code == 201
    r = upload(env, data, "b.pdf")
    assert r.status_code == 409
    assert r.json()["detail"] == DUPLICATE_TEXT


def test_two_files_in_one_form_rejected(env):
    pdf = make_pdf(["x"])
    r = env["client"].post(env["url"], files=[("file", ("a.pdf", pdf)), ("file", ("b.pdf", pdf))])
    assert r.status_code == 400
    assert env["conn"].execute("SELECT COUNT(*) FROM past_exam").fetchone()[0] == 0


def test_missing_exam_404(env):
    r = env["client"].post("/api/exams/999/past-exams", files={"file": ("a.pdf", make_pdf(["x"]))})
    assert r.status_code == 404


def test_upload_rejects_foreign_origin(env):
    r = upload(env, make_pdf(["x"]), headers={"origin": "http://evil.example"})
    assert r.status_code == 403
    pid = upload(env, make_pdf(["y"])).json()["id"]
    r = env["client"].delete(f"{env['url']}/{pid}", headers={"origin": "http://evil.example"})
    assert r.status_code == 403
    assert env["conn"].execute("SELECT COUNT(*) FROM past_exam").fetchone()[0] == 1


def test_json_url_body_rejected_without_network(env):
    with respx.mock(assert_all_called=False) as mock:
        r = env["client"].post(env["url"], json={"url": "https://altillo.com/x.pdf"})
    assert r.status_code in (415, 422)
    assert mock.calls.call_count == 0
    assert env["conn"].execute("SELECT COUNT(*) FROM past_exam").fetchone()[0] == 0


def test_past_exam_path_never_in_responses(env):
    data = make_pdf(["x"])
    sha = hashlib.sha256(data).hexdigest()
    texts = [upload(env, data).text, env["client"].get(env["url"]).text,
             env["client"].get("/api/exams").text]
    for text in texts:
        assert "parciales/" not in text and "parciales\\" not in text
        assert str(env["base"]) not in text
        assert sha not in text
        assert '"path"' not in text


def test_delete_removes_file(env):
    pid = upload(env, make_pdf(["x"])).json()["id"]
    r = env["client"].delete(f"{env['url']}/{pid}")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert files_in(env["base"] / "parciales") == []
    assert env["client"].get(env["url"]).json() == []
    assert env["client"].delete(f"{env['url']}/{pid}").status_code == 404


def test_delete_exam_removes_its_past_exams(env):
    upload(env, make_pdf(["x"]))
    assert env["client"].delete(f"/api/exams/{env['exam_id']}").status_code == 200
    assert files_in(env["base"] / "parciales") == []
    assert env["conn"].execute("SELECT COUNT(*) FROM past_exam").fetchone()[0] == 0


def test_multipart_temp_files_removed(env, monkeypatch):
    """Los temporales del formulario se cierran (y así se borran) al terminar, también al fallar."""
    from starlette import formparsers

    opened = []
    real = formparsers.SpooledTemporaryFile

    def tracking(*args, **kwargs):
        f = real(*args, **kwargs)
        opened.append(f)
        return f

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", tracking)
    upload(env, make_pdf(["x"]))
    upload(env, b"MZ" + b"\0" * 100)
    assert len(opened) == 2
    assert all(f.closed for f in opened)


def test_missing_exam_404_before_reading(env):
    headers, body = multipart(make_pdf(["x"]))
    headers["content-length"] = str(len(body))
    status, _, reads = call_raw(env["app"], "/api/exams/999/past-exams", headers, [body])
    assert status == 404
    assert reads == 0


def test_sixth_409_before_reading(env):
    for i in range(5):
        assert upload(env, make_pdf([f"Parcial {i}"])).status_code == 201
    headers, body = multipart(make_pdf(["Parcial 6"]))
    headers["content-length"] = str(len(body))
    status, resp, reads = call_raw(env["app"], env["url"], headers, [body])
    assert status == 409
    assert LIMIT_TEXT.encode() in resp
    assert reads == 0


def test_enrollment_400_before_reading(env):
    exam = create_manual_exam(env["conn"], 10, "Inscripción", NOW, [], None, kind="inscripcion",
                              closes_at=NOW + 3600, now=NOW)
    headers, body = multipart(make_pdf(["x"]))
    headers["content-length"] = str(len(body))
    status, _, reads = call_raw(env["app"], f"/api/exams/{exam}/past-exams", headers, [body])
    assert status == 400
    assert reads == 0


def test_html_pdf_polyglot_rejected(env):
    r = upload(env, b"<html><script>x</script></html>\n" + make_pdf(["x"]), "a.pdf")
    assert r.status_code == 415
    assert r.json()["detail"] == WRONG_TYPE_TEXT


# --- Estilo del examen (Tarea 9) --------------------------------------------------------


STYLE_BODY = {
    "groups": [{"kind": "opcion_multiple", "count": 8, "points_each": 1},
               {"kind": "desarrollo", "count": 2, "points_each": 1}],
    "minutes": 120, "max_grade": 10, "topic_ids": [1], "topics_unmatched": [], "formula_sheet": False,
}


def extracted_style():
    from estudia.ai.base import ExtractedExercise, ExtractedStyle

    return ExtractedStyle(
        exercises=[ExtractedExercise(kind="opcion_multiple", points=1)] * 8
        + [ExtractedExercise(kind="desarrollo", points=1)] * 2,
        minutes=120, max_grade=10, topics=["Funciones lineales"], formula_sheet=False)


def pdf_pages(data: bytes) -> int | None:
    import pypdfium2 as pdfium
    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError:
        return None
    try:
        return len(pdf)
    finally:
        pdf.close()


class ByPdfPages(dict):
    """Estilos simulados que reconocen un parcial guardado (rasterizado: otros bytes y sin texto)
    por su cantidad de páginas."""

    def _key(self, data):
        if dict.__contains__(self, data):
            return data
        pages = pdf_pages(data)
        return next((k for k in self if pages is not None and pdf_pages(k) == pages), data)

    def __contains__(self, data):
        return dict.__contains__(self, self._key(data))

    def __getitem__(self, data):
        return dict.__getitem__(self, self._key(data))


@pytest.fixture
def style_env():
    """App con un proveedor espía: cada parcial que se manda a Gemini queda anotado."""
    from fakes import FakeProvider
    from test_retry import analysis

    from estudia.secrets import set_secret

    pdf = make_pdf(["Ejercicio 1"])
    # El parcial se guarda rasterizado (sin metadatos): no se reconoce por sus bytes.
    spy = FakeProvider({"abcdefghijk": analysis()}, lambda s, o: 1,
                       styles=ByPdfPages({pdf: extracted_style()}))
    factory_calls = []

    def factory(key):
        factory_calls.append(key)
        return spy

    base = data_dir()
    db_path = base / "estudia.db"
    app = create_app(
        db_path=db_path,
        moodle_client_factory=lambda url, token: FakeMoodle(url, token),
        ai_provider_factory=factory,
        open_browser=lambda url: None,
        web_dist=base / "no-existe",
        port=PORT,
    )
    conn = connect(db_path)
    conn.execute("INSERT INTO course(id, site_id, fullname, shortname) VALUES (10, 's', 'Análisis', 'AM')")
    conn.execute("INSERT INTO course(id, site_id, fullname, shortname) VALUES (20, 's', 'Física', 'FI')")
    conn.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (1, 10, 'funcion-lineal', 'Función lineal')")
    conn.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (9, 20, 'cinematica', 'Cinemática')")
    conn.execute("INSERT INTO section(id, course_id, num, name, visible) VALUES (1, 10, 1, 'S', 1)")
    conn.execute("INSERT INTO material(id, section_id, cmid, kind, title, ref, status)"
                 " VALUES (1, 1, 101, 'youtube', 'V', 'abcdefghijk', 'pending')")
    conn.commit()
    exam_id = create_manual_exam(conn, 10, "Primer parcial", NOW + 86400, [], None, now=NOW)
    set_secret("gemini_key", "clave-guardada")
    yield {"client": TestClient(app, base_url=BASE), "conn": conn, "spy": spy, "pdf": pdf,
           "factory_calls": factory_calls, "jobs": app.state.jobs, "exam_id": exam_id,
           "url": f"/api/exams/{exam_id}/past-exams", "style": f"/api/exams/{exam_id}/style"}
    conn.close()


def set_age(env, age):
    from estudia.server.app import AGE_KEY, _kv_set

    _kv_set(env["conn"], AGE_KEY, age)
    env["conn"].commit()


def past_exam_statuses(env):
    return [r[0] for r in env["conn"].execute("SELECT status FROM past_exam ORDER BY id")]


def test_minor_never_extracts(style_env):
    env = style_env
    set_age(env, "minor")
    r = upload(env, env["pdf"])  # cargar un parcial no necesita la IA
    assert r.status_code == 201
    assert env["jobs"].run_process_now()
    assert env["spy"].style_calls == [] and env["factory_calls"] == []
    assert past_exam_statuses(env) == ["pending"]
    # El estilo se puede escribir a mano igual.
    assert env["client"].put(env["style"], json=STYLE_BODY).status_code == 200
    view = env["client"].get(env["style"]).json()
    assert view["saved"]["groups"] == [{"kind": "opcion_multiple", "count": 8, "points_each": 1.0},
                                       {"kind": "desarrollo", "count": 2, "points_each": 1.0}]
    assert view["ai"] is False and view["proposed"] is None
    assert env["spy"].style_calls == [] and env["factory_calls"] == []


def test_adult_extracts_before_classes_and_proposes(style_env):
    env = style_env
    set_age(env, "adult")
    assert upload(env, env["pdf"]).status_code == 201  # la carga ya arranca el procesamiento
    assert len(env["spy"].style_calls) == 1
    assert past_exam_statuses(env) == ["done"]
    assert env["conn"].execute("SELECT status FROM material WHERE id = 1").fetchone()[0] == "done"
    view = env["client"].get(env["style"]).json()
    assert view["ai"] is True and view["saved"] is None and view["missing"] == 0
    assert view["proposed"]["groups"] == [{"kind": "opcion_multiple", "count": 8, "points_each": 1.0},
                                          {"kind": "desarrollo", "count": 2, "points_each": 1.0}]
    assert view["proposed"]["topic_ids"] == [1]
    assert view["sources"] == [{"past_exam_id": 1, "name": "parcial.pdf", "questions": 10,
                                "minutes": 120, "max_grade": 10.0, "formula_sheet": False}]
    # Los temas del curso para elegir (el de la clase recién procesada también).
    assert {"id": 1, "name": "Función lineal"} in view["topics"]
    assert all(t["name"] != "Cinemática" for t in view["topics"])
    # Del parcial no se guarda nada más que el estilo.
    stored = env["conn"].execute("SELECT style_json FROM past_exam").fetchone()[0]
    assert "Ejercicio" not in stored


def test_style_budget_stop_reason_survives_class_processing(style_env):
    from estudia.generation.pipeline import STOP_KEY

    env = style_env
    set_age(env, "minor")
    assert upload(env, env["pdf"]).status_code == 201
    env["conn"].execute("INSERT INTO kv(key, value) VALUES ('ai_budget:pdf', '0')")
    env["conn"].commit()
    set_age(env, "adult")
    assert env["jobs"].run_process_now()
    assert past_exam_statuses(env) == ["pending"] and env["spy"].style_calls == []
    # Las clases se procesan igual (no gastan del presupuesto "pdf").
    assert env["conn"].execute("SELECT status FROM material WHERE id = 1").fetchone()[0] == "done"
    assert env["conn"].execute("SELECT value FROM kv WHERE key = ?", (STOP_KEY,)).fetchone()[0] == "budget"
    assert env["jobs"].retry_at is not None


def test_style_failure_never_blocks_classes(style_env):
    from estudia.ai.base import AIUnavailableError
    from estudia.generation.pipeline import STOP_KEY

    env = style_env
    set_age(env, "minor")
    assert upload(env, env["pdf"]).status_code == 201
    set_age(env, "adult")

    def down(exam, topics):
        env["spy"].requests["pdf"] += 1
        raise AIUnavailableError("caído")

    env["spy"].extract_exam_style = down
    assert env["jobs"].run_process_now()
    assert past_exam_statuses(env) == ["pending"]
    assert env["conn"].execute("SELECT status FROM material WHERE id = 1").fetchone()[0] == "done"
    assert env["conn"].execute("SELECT value FROM kv WHERE key = ?", (STOP_KEY,)).fetchone()[0] == "unavailable"
    assert env["conn"].execute("SELECT value FROM kv WHERE key LIKE 'ai_used:pdf:%'").fetchone()[0] == "1"


def test_style_auth_error_stops_everything(style_env):
    from estudia.ai.base import AIAuthError
    from estudia.generation.pipeline import STOP_KEY

    env = style_env
    set_age(env, "minor")
    assert upload(env, env["pdf"]).status_code == 201
    set_age(env, "adult")

    def rejected(exam, topics):
        raise AIAuthError("clave")

    env["spy"].extract_exam_style = rejected
    assert env["jobs"].run_process_now()
    assert env["conn"].execute("SELECT status FROM material WHERE id = 1").fetchone()[0] == "pending"
    assert env["conn"].execute("SELECT value FROM kv WHERE key = ?", (STOP_KEY,)).fetchone()[0] == "auth"


def test_style_editable_and_validated(style_env):
    env = style_env
    c = env["client"]
    too_many = {**STYLE_BODY, "groups": [{"kind": "opcion_multiple", "count": 31, "points_each": 1}]}
    assert c.put(env["style"], json=too_many).status_code == 422
    for bad in [{"minutes": 0}, {"minutes": 601}, {"max_grade": 0},
                {"groups": [{"kind": "desarrollo", "count": 2, "points_each": 101}]},
                {"groups": [{"kind": "desarrollo", "count": 20, "points_each": 1},
                            {"kind": "opcion_multiple", "count": 11, "points_each": 1}]},
                {"groups": [{"kind": "ensayo", "count": 2, "points_each": 1}]},
                {"groups": [{"kind": "desarrollo", "count": -2, "points_each": 1}]},
                {"groups": [{"kind": "desarrollo", "count": 2, "points_each": -1}]},
                {"groups": [{"kind": "desarrollo", "count": "dos", "points_each": 1}]},
                {"groups": "desarrollo"}, {"minutes": "una hora"},
                {"topics_unmatched": ["x" * 81]}]:
        assert c.put(env["style"], json={**STYLE_BODY, **bad}).status_code == 422, bad
    r = c.put(env["style"], json={**STYLE_BODY, "groups": []})
    assert r.status_code == 400 and r.json()["detail"] == "El examen necesita al menos un ejercicio"
    r = c.put(env["style"], json={**STYLE_BODY, "topic_ids": [9]})
    assert r.status_code == 400 and r.json()["detail"] == "Hay temas que no son del curso del examen"
    assert c.put(env["style"], json=STYLE_BODY).status_code == 200
    saved = c.get(env["style"]).json()["saved"]
    assert saved == {**STYLE_BODY, "groups": [{**g, "points_each": float(g["points_each"])}
                                              for g in STYLE_BODY["groups"]], "max_grade": 10.0}
    assert c.get("/api/exams").json()[0]["has_style"] is True
    assert c.delete(env["style"]).json() == {"ok": True}
    assert c.get(env["style"]).json()["saved"] is None
    assert c.get("/api/exams").json()[0]["has_style"] is False
    assert c.get("/api/exams/999/style").status_code == 404
    assert c.put("/api/exams/999/style", json=STYLE_BODY).status_code == 404
    assert c.delete("/api/exams/999/style").status_code == 404


def test_style_routes_reject_foreign_origin(style_env):
    env = style_env
    c = env["client"]
    evil = {"origin": "https://evil.com"}
    assert c.put(env["style"], json=STYLE_BODY, headers=evil).status_code == 403
    assert c.get(env["style"]).json()["saved"] is None
    assert c.put(env["style"], json=STYLE_BODY).status_code == 200
    assert c.delete(env["style"], headers=evil).status_code == 403
    assert c.get(env["style"]).json()["saved"] is not None
    assert c.put(env["style"], json=STYLE_BODY, headers={"origin": f"http://localhost:{PORT}"}).status_code == 200


def test_deleting_a_past_exam_updates_the_proposal(style_env):
    from estudia.study.exam_style import ExamStyle, StyleGroup

    env = style_env
    set_age(env, "minor")
    ids = [upload(env, make_pdf([f"Ejercicio {i}"]), f"p{i}.pdf").json()["id"] for i in (1, 2)]
    for pid, count in zip(ids, (8, 6)):
        s = ExamStyle(groups=[StyleGroup(kind="opcion_multiple", count=count, points_each=1)])
        env["conn"].execute("UPDATE past_exam SET status='done', style_json=? WHERE id=?",
                            (s.model_dump_json(), pid))
    env["conn"].commit()
    view = env["client"].get(env["style"]).json()
    assert view["proposed"]["groups"][0]["count"] == 7 and len(view["sources"]) == 2
    assert env["client"].delete(f"{env['url']}/{ids[0]}").status_code == 200
    view = env["client"].get(env["style"]).json()
    assert view["proposed"]["groups"][0]["count"] == 6
    assert [s["past_exam_id"] for s in view["sources"]] == [ids[1]]
    assert env["client"].delete(f"{env['url']}/{ids[1]}").status_code == 200
    view = env["client"].get(env["style"]).json()
    assert view["proposed"] is None and view["sources"] == []


def add_campus_pdf(env, title="Apunte 1"):
    """Un PDF del campus pendiente, con su análisis simulado en el espía."""
    from estudia.ai.base import DocQuestion, DocumentAnalysis
    from estudia.config import DOCUMENTS_DIRNAME

    folder = data_dir() / DOCUMENTS_DIRNAME
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "2.pdf").write_bytes(make_pdf(["Pagina 1", "Pagina 2"]))
    env["conn"].execute(
        "INSERT INTO material(id, section_id, kind, title, ref, status, pages)"
        " VALUES (2, 1, 'pdf', ?, ?, 'pending', 2)", (title, f"{DOCUMENTS_DIRNAME}/2.pdf"))
    env["conn"].commit()
    q = DocQuestion(stem="¿Cuánto es 3 + 4?", options=["6", "7", "8", "9"], correct_index=1,
                    explanation="3 + 4 = 7", common_error="Restar", topic="Sumas", source_page=1)
    env["spy"].documents[title] = DocumentAnalysis(summary_md="## Sumas (pág. 1)", topics=["Sumas"],
                                                   questions=[q])


def test_two_past_exams_per_run_and_the_rest_retried_soon(style_env):
    from estudia.server.app import POLL_S

    env = style_env
    set_age(env, "minor")
    pdfs = [make_pdf([f"Ejercicio {i}"]) for i in (1, 2, 3)]
    for i, pdf in enumerate(pdfs):
        env["spy"].styles[pdf] = extracted_style()
        assert upload(env, pdf, f"p{i}.pdf").status_code == 201
    add_campus_pdf(env)
    set_age(env, "adult")
    before = time.time()
    assert env["jobs"].run_process_now()
    assert past_exam_statuses(env) == ["done", "done", "pending"]
    assert len(env["spy"].style_calls) == 2
    # El apunte del campus tuvo presupuesto "pdf" en la misma corrida.
    assert env["conn"].execute("SELECT status FROM material WHERE id = 2").fetchone()[0] == "done"
    # Sin motivo de corte: el que quedó se toma enseguida, sin esperar a la sincronización.
    assert env["jobs"].retry_at is not None
    assert env["jobs"].retry_at <= before + POLL_S + 5
    assert env["jobs"].run_process_now()
    assert past_exam_statuses(env) == ["done"] * 3
    assert len(env["spy"].style_calls) == 3
    assert env["jobs"].retry_at is None


def test_no_quick_retry_without_progress(style_env, monkeypatch):
    from estudia.server import app as app_module

    env = style_env
    set_age(env, "minor")
    assert upload(env, env["pdf"]).status_code == 201
    set_age(env, "adult")

    def broken(*args, **kwargs):
        raise RuntimeError("base trabada")

    monkeypatch.setattr(app_module, "extract_pending_styles", broken)
    assert env["jobs"].run_process_now()
    assert past_exam_statuses(env) == ["pending"]
    assert env["jobs"].retry_at is None


def add_topic_material(env):
    """Un apunte listo del tema 1 (el del estilo): de ahí salen las preguntas con el estilo."""
    env["conn"].execute("INSERT INTO material(id, section_id, cmid, kind, title, ref, status, summary_md)"
                        " VALUES (2, 1, 102, 'pdf', 'Apunte', 'a.pdf', 'done', 'La pendiente.')")
    env["conn"].execute("INSERT INTO material_topic(material_id, topic_id) VALUES (2, 1)")
    env["conn"].commit()


def style_question(stem, kind="opcion_multiple"):
    from estudia.ai.base import StyleQuestion

    return StyleQuestion(stem=stem, options=[f"{stem} a", f"{stem} b", f"{stem} c", f"{stem} d"],
                         correct_index=1, explanation="e", common_error="c", topic="Función lineal",
                         kind=kind)


def test_style_view_reports_missing_and_can_generate(style_env):
    env = style_env
    set_age(env, "minor")
    view = env["client"].get(env["style"]).json()
    assert view["missing"] == 0 and view["can_generate"] is False and view["missing_text"] is None
    assert env["client"].put(env["style"], json=STYLE_BODY).status_code == 200
    view = env["client"].get(env["style"]).json()
    assert view["missing"] == 10 and view["can_generate"] is False  # el tema no tiene material
    # El aviso es el mismo texto del 409 al empezar con el formato: la pantalla no lo arma.
    assert view["missing_text"] == style_not_ready_text(10)
    started = env["client"].post("/api/mock/start", json={"exam_id": env["exam_id"], "style": True})
    assert started.status_code == 409 and started.json()["detail"] == view["missing_text"]
    add_topic_material(env)
    assert env["client"].get(env["style"]).json()["can_generate"] is True


def test_jobs_generate_style_questions_after_classes(style_env):
    env = style_env
    set_age(env, "minor")
    add_topic_material(env)
    assert env["client"].put(env["style"], json=STYLE_BODY).status_code == 200
    env["jobs"].clock = lambda: NOW
    env["spy"].style_batches = [[style_question(f"om {i}") for i in range(8)]
                                + [style_question(f"d {i}", "desarrollo") for i in range(2)]]
    set_age(env, "adult")
    assert env["jobs"].run_process_now()
    assert env["conn"].execute("SELECT status FROM material WHERE id = 1").fetchone()[0] == "done"
    assert env["conn"].execute(
        "SELECT COUNT(*) FROM question WHERE origin = 'parcial_propio' AND style_exam_id = ?",
        (env["exam_id"],)).fetchone()[0] == 10
    # La pasada a ciegas de las de estilo no usa el modelo que las generó.
    assert env["spy"].blind_batches[-1][1] == ("analizador",)
    assert env["client"].get(env["style"]).json()["missing"] == 0
