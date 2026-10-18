"""PDF y diapositivas en la tubería: el mismo camino que un video, con la misma verificación.

Una pregunta de un documento solo se guarda si pasa SymPy, la regla de la respuesta escrita
y la pasada a ciegas con un modelo que no la generó. Nunca llega a `analyze_video`.
"""
import time

import pypdfium2 as pdfium
import pytest

from estudia.ai.base import (
    AIDocumentUnreadableError,
    AIFormatError,
    AIQuotaError,
    DocQuestion,
    DocumentAnalysis,
    GenQuestion,
    VideoAnalysis,
)
from estudia.ai import gemini
from estudia.ai.quota import used_today
from estudia.config import DOCUMENTS_DIRNAME, data_dir
from estudia.db import connect
from estudia.generation import pipeline
from estudia.generation.pipeline import (
    DOC_UNREADABLE_TEXT,
    FAIL_DOCUMENT,
    FORMAT_ERROR_TEXT,
    RETRY_ON_NEW_KEY,
    VIDEO_DAY_PREFIX,
    _shift_pages,
    document_input,
    load_analysis,
    pending_queue,
    process_pending,
    verify_batch,
)
from docs_factory import make_pdf, make_pptx
from fakes import FakeProvider

DAY = 86400


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    c.execute("INSERT INTO section(id, course_id, num, name, visible) VALUES (1, 10, 1, 'S1', 1)")
    c.execute("INSERT INTO section(id, course_id, num, name, visible) VALUES (2, 20, 1, 'T1', 1)")
    c.commit()
    return c


def write_file(mid: int, data: bytes, suffix: str = ".pdf") -> str:
    folder = data_dir() / DOCUMENTS_DIRNAME
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{mid}{suffix}").write_bytes(data)
    return f"{DOCUMENTS_DIRNAME}/{mid}{suffix}"


def add_doc(conn, mid, title=None, kind="pdf", pages=3, section_id=1, data=None,
            analysis_json=None, analysis_model=None):
    """Un documento entero `pending`, con su archivo en la carpeta de documentos."""
    if data is None:
        data = make_pdf([f"Pagina {i}" for i in range(1, pages + 1)])
    ref = write_file(mid, data, ".pdf" if kind == "pdf" else ".pptx")
    conn.execute(
        "INSERT INTO material(id, section_id, kind, title, ref, status, pages, analysis_json,"
        " analysis_model) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
        (mid, section_id, kind, title or f"Apunte {mid}", ref, pages, analysis_json, analysis_model),
    )
    conn.commit()


def add_part(conn, mid, parent_id, page_from, page_to, title=None):
    """Un tramo `pending` de un documento ya partido (como lo deja `fetch._store`)."""
    conn.execute("UPDATE material SET status='split' WHERE id=?", (parent_id,))
    conn.execute(
        "INSERT INTO material(id, section_id, kind, title, ref, status, parent_id, page_from,"
        " page_to) SELECT ?, section_id, kind, ?, ref, 'pending', id, ?, ? FROM material"
        " WHERE id=?",
        (mid, title or f"Parte {mid}", page_from, page_to, parent_id),
    )
    conn.commit()


def add_video(conn, mid, section_id=1, analysis_json=None, analysis_model=None):
    conn.execute(
        "INSERT INTO material(id, section_id, cmid, kind, title, ref, status, analysis_json,"
        " analysis_model) VALUES (?, ?, ?, 'youtube', ?, ?, 'pending', ?, ?)",
        (mid, section_id, 1000 + mid, f"Video {mid}", f"v{mid}", analysis_json, analysis_model),
    )
    conn.commit()


def dq(stem, correct=1, page=2, options=None, check_expr=None):
    return DocQuestion(stem=stem, options=options or ["1", "2", "3", "4"], correct_index=correct,
                       explanation="porque sí", common_error="error común",
                       topic="Límites", source_page=page, check_expr=check_expr)


def doc(questions, summary="# Resumen (pág. 1)"):
    return DocumentAnalysis(summary_md=summary, topics=["Límites"], questions=questions)


def vq(stem, correct=1):
    return GenQuestion(stem=stem, options=["1", "2", "3", "4"], correct_index=correct,
                       explanation="porque sí", common_error="error común",
                       topic="Funciones lineales", source_seconds=5)


def video(questions):
    return VideoAnalysis(summary_md="# Resumen", topics=["Funciones lineales"], questions=questions)


def always_right(stem, options):
    return 1


def always_wrong(stem, options):
    return 0


def questions(conn, mid=None):
    sql = "SELECT * FROM question" + (" WHERE material_id=?" if mid is not None else "")
    return conn.execute(sql + " ORDER BY id", (mid,) if mid is not None else ()).fetchall()


def material(conn, mid):
    return conn.execute("SELECT * FROM material WHERE id=?", (mid,)).fetchone()


def stop_reason(conn):
    row = conn.execute("SELECT value FROM kv WHERE key='ai_stop_reason'").fetchone()
    return row[0] if row else None


# --- el camino feliz ------------------------------------------------------------------------


def test_pdf_material_becomes_verified_questions_with_page(conn):
    add_doc(conn, 1, title="Apunte 3")
    analysis = doc([dq("a", page=1), dq("b", page=3), dq("c", page=2)])
    fake = FakeProvider({}, lambda stem, options: 0 if stem == "c" else 1,
                        documents={"Apunte 3": analysis})
    assert process_pending(conn, fake) == 1
    rows = questions(conn)
    assert [(r["stem"], r["source_page"], r["verified"]) for r in rows] == [("a", 1, 1), ("b", 3, 1)]
    assert all(r["source_seconds"] is None for r in rows)
    assert material(conn, 1)["status"] == "done"
    now = time.time()
    assert used_today(conn, "pdf", now) == 1
    assert used_today(conn, "blind", now) == 1
    assert used_today(conn, "analysis", now) == 0
    # El PDF entero va con los bytes del archivo y su cantidad de páginas.
    sent = fake.analyzed_docs[0]
    assert sent.kind == "pdf" and sent.title == "Apunte 3" and sent.pages == 3
    assert sent.data == (data_dir() / material(conn, 1)["ref"]).read_bytes()
    assert sent.text is None
    assert fake.analyzed_refs == []


def test_slides_go_as_text(conn):
    add_doc(conn, 1, title="Clase 2", kind="slides", pages=2,
            data=make_pptx(["Límite lateral", "Continuidad"], notes=["", "ojo con esto"]))
    fake = FakeProvider({}, always_right, documents={"Clase 2": doc([dq("a", page=2)])})
    assert process_pending(conn, fake) == 1
    sent = fake.analyzed_docs[0]
    assert sent.kind == "slides" and sent.pages == 2 and sent.data is None
    assert "Límite lateral" in sent.text and "ojo con esto" in sent.text
    assert [r["source_page"] for r in questions(conn)] == [2]


# --- Review Focus 1: ninguna pregunta de documento sin verificar -----------------------------


def test_document_question_needs_blind_match(conn):
    add_doc(conn, 1)
    fake = FakeProvider({}, always_wrong, documents={"Apunte 1": doc([dq("a"), dq("b")])})
    process_pending(conn, fake)
    assert questions(conn) == []
    assert material(conn, 1)["status"] == "done"
    assert len(fake.blind_batches) == 1


def test_document_blind_without_answer_discards(conn):
    def unsure(stem, options):
        raise AIFormatError("no sé")

    add_doc(conn, 1)
    fake = FakeProvider({}, unsure, documents={"Apunte 1": doc([dq("a")])})
    process_pending(conn, fake)
    assert questions(conn) == []


def test_document_distractor_inside_typed_tolerance_is_discarded(conn):
    """La escrita aceptaría "1.41" por √2: la pregunta se contradice y no se guarda."""
    add_doc(conn, 1)
    fake = FakeProvider({}, lambda stem, options: 0, documents={"Apunte 1": doc([
        dq("contradictoria", correct=0, options=["√2", "1.41", "2", "1"], check_expr="sqrt(2)"),
        dq("sana", correct=0, options=["√2", "1.4", "2", "1"], check_expr="sqrt(2)"),
    ])})
    process_pending(conn, fake)
    assert [r["stem"] for r in questions(conn)] == ["sana"]
    # La contradictoria ni siquiera va a ciegas.
    assert [stem for stem, _ in fake.blind_batches[0][0]] == ["sana"]


def test_document_check_expr_rejected_by_sympy_is_discarded(conn):
    add_doc(conn, 1)
    fake = FakeProvider({}, always_right, documents={"Apunte 1": doc([
        dq("mal marcada", correct=1, options=["4", "5", "6", "7"], check_expr="2+2"),
        dq("equivalente", correct=0, options=["1/2", "0,5", "3", "4"], check_expr="1/2"),
        dq("bien", correct=0, options=["4", "5", "6", "7"], check_expr="2+2"),
    ])})
    fake.blind = lambda stem, options: 0
    process_pending(conn, fake)
    assert [r["stem"] for r in questions(conn)] == ["bien"]


def test_document_blind_quota_keeps_pending_without_questions(conn):
    add_doc(conn, 1)
    fake = FakeProvider({}, always_right, documents={"Apunte 1": doc([dq("a"), dq("b")])},
                        blind_errors=[AIQuotaError("sin cuota")])
    assert process_pending(conn, fake) == 0
    row = material(conn, 1)
    assert row["status"] == "pending"
    assert row["analysis_json"] is not None
    assert questions(conn) == []
    assert stop_reason(conn) == "quota"
    # La segunda corrida reusa el análisis guardado y cierra con preguntas.
    assert process_pending(conn, fake) == 1
    assert len(fake.analyzed_docs) == 1
    assert len(questions(conn)) == 2


def test_document_never_verified_by_its_own_analysis_model(conn):
    """Si el único modelo a ciegas es el que analizó el documento, no se guarda nada."""
    add_doc(conn, 1)
    fake = FakeProvider({}, always_right, documents={"Apunte 1": doc([dq("a")])},
                        analysis_model="flash", blind_models=("flash",))
    assert process_pending(conn, fake) == 0
    assert material(conn, 1)["status"] == "pending"
    assert material(conn, 1)["analysis_model"] == "flash"
    assert questions(conn) == []


def test_document_and_video_share_blind_batch_with_same_analysis_model(conn):
    add_video(conn, 1)
    add_doc(conn, 2)
    fake = FakeProvider({"v1": video([vq("del video")])}, always_right,
                        documents={"Apunte 2": doc([dq("del apunte")])})
    assert process_pending(conn, fake) == 2
    assert len(fake.blind_batches) == 1
    items, exclude = fake.blind_batches[0]
    assert [stem for stem, _ in items] == ["del video", "del apunte"]
    assert exclude == ("analizador",)


def test_document_and_video_share_blind_batch_excluding_both_models(conn):
    """Un lote mezclado (video de A, apunte de B) excluye los dos modelos en un solo pedido."""
    add_video(conn, 1, analysis_json=video([vq("del video")]).model_dump_json(), analysis_model="A")
    add_doc(conn, 2, analysis_json=doc([dq("del apunte")]).model_dump_json(), analysis_model="B")
    fake = FakeProvider({}, always_right)
    results = verify_batch(conn, fake, [1, 2])
    assert [r.kept for r in results] == [1, 1]
    assert len(fake.blind_batches) == 1
    assert set(fake.blind_batches[0][1]) == {"A", "B"}
    assert fake.analyzed_refs == [] and fake.analyzed_docs == []
    assert [r["source_page"] for r in questions(conn, 2)] == [2]
    assert [r["source_seconds"] for r in questions(conn, 1)] == [5]


def test_pending_queue_groups_documents_by_analysis_model(conn):
    add_video(conn, 1, analysis_json=video([vq("v")]).model_dump_json(), analysis_model="A")
    add_doc(conn, 2, analysis_json=doc([dq("d")]).model_dump_json(), analysis_model="B")
    add_doc(conn, 3, analysis_json=doc([dq("e")]).model_dump_json(), analysis_model="A")
    fake = FakeProvider({}, always_right)
    assert process_pending(conn, fake) == 3
    assert [(len(items), exclude) for items, exclude in fake.blind_batches] == [
        (2, ("A",)), (1, ("B",))]


# --- un documento nunca va por el camino del video, y viceversa ------------------------------


def test_pdf_never_calls_analyze_video_and_video_never_calls_analyze_document(conn):
    add_video(conn, 1)
    add_doc(conn, 2, title="Guía")
    add_doc(conn, 3, title="Clase", kind="slides", pages=1, data=make_pptx(["Derivadas"]))
    add_doc(conn, 4, title="Guía larga", pages=12)
    add_part(conn, 5, 4, 1, 6, title="Guía larga (1)")
    fake = FakeProvider({"v1": video([vq("v")])}, always_right, documents={
        "Guía": doc([dq("g")]), "Clase": doc([dq("c", page=1)]),
        "Guía larga (1)": doc([dq("p")])})
    assert process_pending(conn, fake) == 4
    assert fake.analyzed_refs == ["v1"]
    assert sorted(d.title for d in fake.analyzed_docs) == ["Clase", "Guía", "Guía larga (1)"]
    assert material(conn, 4)["status"] == "split"


# --- tramos ---------------------------------------------------------------------------------


def test_part_pages_shifted_to_document_numbering(conn):
    add_doc(conn, 1, title="Libro", pages=100)
    add_part(conn, 2, 1, 41, 80, title="Libro (2)")
    analysis = doc([dq("a", page=3), dq("b", page=None), dq("c", page=45)],
                   summary="## Límites (pág. 2)\n\n## Continuidad (pág. 10)")
    fake = FakeProvider({}, always_right, documents={"Libro (2)": analysis})
    assert process_pending(conn, fake) == 1
    # Fuera del tramo queda sin página, pero la pregunta se guarda.
    assert [(r["stem"], r["source_page"]) for r in questions(conn)] == [
        ("a", 43), ("b", None), ("c", None)]
    assert material(conn, 2)["summary_md"] == "## Límites (pág. 42)\n\n## Continuidad (pág. 50)"


def test_part_sends_only_its_pages(conn):
    add_doc(conn, 1, title="Libro", pages=100)
    add_part(conn, 2, 1, 41, 80, title="Libro (2)")
    fake = FakeProvider({}, always_right, documents={"Libro (2)": doc([dq("a")])})
    process_pending(conn, fake)
    sent = fake.analyzed_docs[0]
    assert sent.pages == 40
    pdf = pdfium.PdfDocument(sent.data)
    try:
        assert len(pdf) == 40
        first = pdf[0].get_textpage().get_text_range()
        assert "Pagina 41" in first and "Pagina 40" not in first
    finally:
        pdf.close()


def test_whole_document_page_out_of_range_kept_without_page(conn):
    add_doc(conn, 1, pages=3)
    fake = FakeProvider({}, always_right, documents={"Apunte 1": doc([dq("a", page=9)])})
    process_pending(conn, fake)
    assert [(r["stem"], r["source_page"]) for r in questions(conn)] == [("a", None)]


class ValidatingProvider(FakeProvider):
    """Como Gemini: el análisis pasa por el `_validate_document` real con las páginas mandadas."""

    def analyze_document(self, doc_input):
        analysis = super().analyze_document(doc_input)
        return gemini._validate_document(analysis, doc_input.pages)


def test_slides_part_numbered_locally_and_shifted(conn):
    """Un tramo de diapositivas se numera desde 1, como el PDF de un tramo, y se corre."""
    add_doc(conn, 1, title="Clase", kind="slides", pages=12,
            data=make_pptx([f"Diapositiva de prueba {i}" for i in range(1, 13)]))
    add_part(conn, 2, 1, 7, 12, title="Clase (2)")
    fake = ValidatingProvider({}, always_right, documents={"Clase (2)": doc(
        [dq("a", page=2), dq("b", page=9)], summary="## Tema (pág. 3)\n\n## Otro (pág. 9)")})
    process_pending(conn, fake)
    sent = fake.analyzed_docs[0]
    assert sent.pages == 6
    assert sent.text.startswith("Diapositiva 1:\nDiapositiva de prueba 7")
    assert "Diapositiva 7:" not in sent.text
    # La 2 del tramo es la 8 de la clase; la 9 no está en el tramo (6 diapositivas): sin cita.
    assert [(r["stem"], r["source_page"]) for r in questions(conn)] == [("a", 8), ("b", None)]
    assert material(conn, 2)["summary_md"] == "## Tema (diap. 9)\n\n## Otro"


def test_pdf_part_through_real_validation(conn):
    add_doc(conn, 1, title="Libro", pages=100)
    add_part(conn, 2, 1, 41, 80, title="Libro (2)")
    fake = ValidatingProvider({}, always_right, documents={"Libro (2)": doc(
        [dq("a", page=40), dq("b", page=41)])})
    process_pending(conn, fake)
    assert [(r["stem"], r["source_page"]) for r in questions(conn)] == [("a", 80), ("b", None)]


def test_summary_marks_outside_part_are_dropped(conn):
    add_doc(conn, 1, title="Libro", pages=100)
    add_part(conn, 2, 1, 41, 80, title="Libro (2)")
    fake = FakeProvider({}, always_right, documents={"Libro (2)": doc(
        [dq("a")], summary="## Límites (pág. 2)\n\n## Más allá (pág. 45)")})
    process_pending(conn, fake)
    assert material(conn, 2)["summary_md"] == "## Límites (pág. 42)\n\n## Más allá"


def test_whole_document_without_page_count_stores_no_page(conn):
    add_doc(conn, 1, pages=3)
    conn.execute("UPDATE material SET pages=NULL WHERE id=1")
    conn.commit()
    fake = FakeProvider({}, always_right, documents={"Apunte 1": doc(
        [dq("a", page=2)], summary="## A (pág. 2)")})
    process_pending(conn, fake)
    assert [r["source_page"] for r in questions(conn)] == [None]
    assert material(conn, 1)["summary_md"] == "## A"


def test_part_page_that_may_be_a_printed_folio_is_dropped(conn):
    """Un tramo 15-40: una cita 20 puede ser la hoja 20 del tramo o el folio impreso 20.
    Ante la duda no se cita: una página que falta vale, una equivocada no."""
    add_doc(conn, 1, title="Libro", pages=40)
    add_part(conn, 2, 1, 15, 40, title="Libro (2)")
    fake = FakeProvider({}, always_right, documents={"Libro (2)": doc(
        [dq("a", page=20), dq("b", page=14), dq("c", page=15)],
        summary="## Límites (pág. 20)\n\n## Antes (pág. 14)")})
    assert process_pending(conn, fake) == 1
    assert [(r["stem"], r["source_page"]) for r in questions(conn)] == [
        ("a", None), ("b", 28), ("c", None)]
    assert material(conn, 2)["summary_md"] == "## Límites\n\n## Antes (pág. 28)"


def test_first_part_pages_are_not_ambiguous(conn):
    add_doc(conn, 1, title="Libro", pages=80)
    add_part(conn, 2, 1, 1, 40, title="Libro (1)")
    fake = FakeProvider({}, always_right, documents={"Libro (1)": doc(
        [dq("a", page=20)], summary="## Límites (pág. 20)")})
    process_pending(conn, fake)
    assert [r["source_page"] for r in questions(conn)] == [20]
    assert material(conn, 2)["summary_md"] == "## Límites (pág. 20)"


def test_slides_part_number_that_may_be_printed_is_dropped(conn):
    add_doc(conn, 1, title="Clase", kind="slides", pages=12,
            data=make_pptx([f"Diapositiva de prueba {i}" for i in range(1, 13)]))
    add_part(conn, 2, 1, 4, 12, title="Clase (2)")
    fake = FakeProvider({}, always_right, documents={"Clase (2)": doc(
        [dq("a", page=5), dq("b", page=3)], summary="## Tema (pág. 5)\n\n## Otro (pág. 2)")})
    process_pending(conn, fake)
    assert [(r["stem"], r["source_page"]) for r in questions(conn)] == [("a", None), ("b", 6)]
    assert material(conn, 2)["summary_md"] == "## Tema\n\n## Otro (diap. 5)"


def test_slides_summary_marks_say_diap(conn):
    add_doc(conn, 1, title="Clase", kind="slides", pages=4,
            data=make_pptx([f"Diapositiva de prueba {i}" for i in range(1, 5)]))
    add_doc(conn, 2, title="Apunte", pages=3)
    fake = FakeProvider({}, always_right, documents={
        "Clase": doc([dq("a")], summary="## Tema (pág. 3)\n\n## Otro (diap. 2)"),
        "Apunte": doc([dq("b")], summary="## Tema (pág. 3)\n\n## Otro (diap. 2)")})
    process_pending(conn, fake)
    assert material(conn, 1)["summary_md"] == "## Tema (diap. 3)\n\n## Otro (diap. 2)"
    assert material(conn, 2)["summary_md"] == "## Tema (pág. 3)\n\n## Otro (pág. 2)"


def test_part_is_told_its_page_count(conn):
    add_doc(conn, 1, title="Libro", pages=40)
    add_part(conn, 2, 1, 15, 40, title="Libro (2)")
    add_doc(conn, 3, title="Guía", pages=3)
    fake = FakeProvider({}, always_right, documents={
        "Libro (2)": doc([dq("a")]), "Guía": doc([dq("b")])})
    process_pending(conn, fake)
    sent = {d.title: d for d in fake.analyzed_docs}
    assert sent["Libro (2)"].part is True and sent["Libro (2)"].pages == 26
    assert sent["Guía"].part is False


def test_shift_pages():
    assert _shift_pages("## A (pág. 2)\ntexto (pág. 3) y pág. 4", 40, 41, 80) == (
        "## A (pág. 42)\ntexto (pág. 43) y pág. 4")
    assert _shift_pages("## A (pág. 2)", 0, 1, 3) == "## A (pág. 2)"
    assert _shift_pages("## A (pág. 45) fin", 40, 41, 80) == "## A fin"
    assert _shift_pages("## A (pág. 0)", 0, 1, 3) == "## A"
    assert _shift_pages("## A (pág. 2)", 0, 1, None) == "## A"
    # Un tramo 15-40: lo citado desde la 15 puede ser un folio impreso.
    assert _shift_pages("## A (pág. 15) ## B (pág. 14)", 14, 15, 40) == "## A ## B (pág. 28)"
    assert _shift_pages("## A (diap. 2)", 0, 1, 3, unit="diap.") == "## A (diap. 2)"
    assert _shift_pages("## A (diap. 2)", 0, 1, 3) == "## A (pág. 2)"


# --- presupuesto y tope ---------------------------------------------------------------------


def test_pdf_budget_stops_with_reason_budget(conn):
    conn.execute("INSERT INTO kv(key, value) VALUES ('ai_budget:pdf', '1')")
    add_doc(conn, 1)
    add_doc(conn, 2)
    add_video(conn, 3)
    fake = FakeProvider({"v3": video([vq("v")])}, always_right,
                        documents={"Apunte 1": doc([dq("a")]), "Apunte 2": doc([dq("b")])})
    assert process_pending(conn, fake) == 2
    assert [d.title for d in fake.analyzed_docs] == ["Apunte 1"]
    assert fake.analyzed_refs == ["v3"]
    assert material(conn, 2)["status"] == "pending"
    assert stop_reason(conn) == "budget"
    now = time.time()
    assert used_today(conn, "pdf", now) == 1
    assert used_today(conn, "analysis", now) == 1


def test_spent_pdf_budget_does_not_starve_videos_behind(conn):
    """12 PDFs sin analizar adelante y el presupuesto "pdf" gastado: el video de atrás igual
    entra en la primera corrida (la ventana no se llena de lo que no se puede analizar)."""
    conn.execute("INSERT INTO kv(key, value) VALUES ('ai_budget:pdf', '0')")
    for mid in range(1, 13):
        add_doc(conn, mid)
    add_video(conn, 13)
    fake = FakeProvider({"v13": video([vq("v")])}, always_right, documents={})
    assert process_pending(conn, fake) == 1
    assert fake.analyzed_refs == ["v13"] and fake.analyzed_docs == []
    assert material(conn, 13)["status"] == "done"
    assert all(material(conn, mid)["status"] == "pending" for mid in range(1, 13))
    assert stop_reason(conn) == "budget"


def test_spent_analysis_budget_does_not_starve_documents_behind(conn):
    conn.execute("INSERT INTO kv(key, value) VALUES ('ai_budget:analysis', '0')")
    for mid in range(1, 13):
        add_video(conn, mid)
    add_doc(conn, 13)
    fake = FakeProvider({}, always_right, documents={"Apunte 13": doc([dq("a")])})
    assert process_pending(conn, fake) == 1
    assert fake.analyzed_refs == [] and [d.title for d in fake.analyzed_docs] == ["Apunte 13"]
    assert stop_reason(conn) == "budget"


def test_spent_budget_still_verifies_paid_items_of_that_kind(conn):
    """Lo ya analizado no gasta análisis: entra en la ventana aunque su presupuesto esté gastado."""
    conn.execute("INSERT INTO kv(key, value) VALUES ('ai_budget:pdf', '0')")
    add_doc(conn, 1, analysis_json=doc([dq("a")]).model_dump_json(), analysis_model="otro")
    fake = FakeProvider({}, always_right, documents={})
    assert process_pending(conn, fake) == 1
    assert material(conn, 1)["status"] == "done"


def test_documents_do_not_use_video_analysis_budget(conn):
    conn.execute("INSERT INTO kv(key, value) VALUES ('ai_budget:analysis', '0')")
    add_doc(conn, 1)
    add_video(conn, 2)
    fake = FakeProvider({"v2": video([vq("v")])}, always_right,
                        documents={"Apunte 1": doc([dq("a")])})
    assert process_pending(conn, fake) == 1
    assert fake.analyzed_refs == []
    assert material(conn, 1)["status"] == "done"


def test_documents_ignore_youtube_cap(conn):
    now = time.time()
    conn.execute("INSERT INTO kv(key, value) VALUES (?, ?)",
                 (pipeline._day_key(VIDEO_DAY_PREFIX, now), str(10 * 3600)))
    conn.commit()
    add_video(conn, 1)
    add_doc(conn, 2)
    fake = FakeProvider({"v1": video([vq("v")])}, always_right,
                        documents={"Apunte 2": doc([dq("a")])})
    assert process_pending(conn, fake) == 1
    assert fake.analyzed_refs == []
    assert material(conn, 2)["status"] == "done"
    assert stop_reason(conn) == "youtube_cap"


def test_youtube_cap_does_not_starve_documents_behind(conn):
    """12 videos sin analizar adelante con el tope de YouTube ya alcanzado: el PDF de atrás
    entra en la primera corrida y el motivo sigue siendo el tope."""
    now = time.time()
    conn.execute("INSERT INTO kv(key, value) VALUES (?, ?)",
                 (pipeline._day_key(VIDEO_DAY_PREFIX, now), str(10 * 3600)))
    conn.commit()
    for mid in range(1, 13):
        add_video(conn, mid)
    add_doc(conn, 13)
    fake = FakeProvider({}, always_right, documents={"Apunte 13": doc([dq("a")])})
    assert process_pending(conn, fake) == 1
    assert fake.analyzed_refs == [] and [d.title for d in fake.analyzed_docs] == ["Apunte 13"]
    assert material(conn, 13)["status"] == "done"
    assert stop_reason(conn) == "youtube_cap"


def test_youtube_cap_with_link_denied_keeps_videos_in_window(conn):
    """Con el enlace rechazado no sale nada por enlace: el tope no aplica y el video entra."""
    now = time.time()
    conn.execute("INSERT INTO kv(key, value) VALUES (?, ?)",
                 (pipeline._day_key(VIDEO_DAY_PREFIX, now), str(10 * 3600)))
    conn.execute("INSERT INTO kv(key, value) VALUES ('youtube_url_input', ?)", (str(int(now) + 3600),))
    conn.commit()
    add_video(conn, 1)
    fake = FakeProvider({"v1": video([vq("v")])}, always_right)
    assert process_pending(conn, fake, budget=1) == 1
    assert fake.analyzed_refs == ["v1"]


def test_document_analysis_does_not_count_youtube_seconds(conn):
    add_doc(conn, 1)
    fake = FakeProvider({}, always_right, documents={"Apunte 1": doc([dq("a")])})
    fake.last_link_sent = True   # lo que quedó de un video anterior
    fake.last_video_seconds = 3000
    process_pending(conn, fake)
    assert pipeline.video_seconds_today(conn, time.time()) == 0


# --- errores --------------------------------------------------------------------------------


class UnreadableDocProvider(FakeProvider):
    def analyze_document(self, doc_input):
        self.analyzed_docs.append(doc_input)
        raise AIDocumentUnreadableError("Gemini no pudo leerlo")


def test_unreadable_document_fails_with_text(conn):
    add_doc(conn, 1)
    add_doc(conn, 2)
    fake = UnreadableDocProvider({}, always_right)
    assert process_pending(conn, fake) == 0
    for mid in (1, 2):
        row = material(conn, mid)
        assert (row["status"], row["error"], row["fail_kind"]) == (
            "failed", "No se pudo leer", FAIL_DOCUMENT)
    assert DOC_UNREADABLE_TEXT == "No se pudo leer"
    assert FAIL_DOCUMENT not in RETRY_ON_NEW_KEY


def test_broken_or_missing_file_fails_with_text(conn):
    add_doc(conn, 1, data=b"%PDF-1.4 esto no es un PDF")
    add_part(conn, 2, 1, 1, 2)
    add_doc(conn, 3)
    (data_dir() / material(conn, 3)["ref"]).unlink()
    fake = FakeProvider({}, always_right, documents={"Apunte 3": doc([dq("a")])})
    process_pending(conn, fake)
    for mid in (2, 3):
        row = material(conn, mid)
        assert (row["status"], row["error"], row["fail_kind"]) == (
            "failed", DOC_UNREADABLE_TEXT, FAIL_DOCUMENT)
    assert fake.analyzed_docs == []


def test_document_format_error_fails_as_format(conn):
    add_doc(conn, 1)
    fake = FakeProvider({}, always_right)   # sin análisis simulado: AIFormatError
    process_pending(conn, fake)
    row = material(conn, 1)
    assert (row["status"], row["error"], row["fail_kind"]) == ("failed", FORMAT_ERROR_TEXT, "format")


def test_document_input_refuses_ref_outside_base_dir(conn, tmp_path):
    add_doc(conn, 1)
    conn.execute("UPDATE material SET ref='../../etc/passwd' WHERE id=1")
    with pytest.raises(pipeline.UnreadableFile):
        document_input(conn, 1, data_dir())


def test_load_analysis_by_kind():
    raw_doc = doc([dq("a")]).model_dump_json()
    raw_video = video([vq("v")]).model_dump_json()
    assert isinstance(load_analysis("pdf", raw_doc), DocumentAnalysis)
    assert isinstance(load_analysis("slides", raw_doc), DocumentAnalysis)
    assert isinstance(load_analysis("youtube", raw_video), VideoAnalysis)
    assert isinstance(load_analysis(None, raw_video), VideoAnalysis)
    # Un análisis de video nunca se lee como de documento ni al revés.
    with pytest.raises(ValueError):
        load_analysis("pdf", raw_video)
    with pytest.raises(ValueError):
        load_analysis("youtube", raw_doc)


@pytest.mark.parametrize("page_from,page_to", [(5, 2), (1, 70)])
def test_bad_part_range_fails_as_unreadable(conn, page_from, page_to):
    add_doc(conn, 1, pages=3)
    add_part(conn, 2, 1, page_from, page_to)
    fake = FakeProvider({}, always_right, documents={"Parte 2": doc([dq("a")])})
    process_pending(conn, fake)
    row = material(conn, 2)
    assert (row["status"], row["error"], row["fail_kind"]) == (
        "failed", DOC_UNREADABLE_TEXT, FAIL_DOCUMENT)
    assert fake.analyzed_docs == []


def test_too_many_pages_fails_as_too_large(conn, monkeypatch):
    from estudia.material import documents

    add_doc(conn, 1, pages=3)
    add_part(conn, 2, 1, 1, 2)
    add_doc(conn, 3, title="Clase", kind="slides", pages=3, data=make_pptx(["a", "b", "c"]))
    monkeypatch.setattr(documents, "PDF_MAX_PAGES", 2)
    monkeypatch.setattr(documents, "SLIDES_MAX", 2)
    fake = FakeProvider({}, always_right)
    process_pending(conn, fake)
    for mid in (2, 3):
        row = material(conn, mid)
        assert (row["status"], row["error"], row["fail_kind"]) == (
            "failed", "Archivo demasiado grande", FAIL_DOCUMENT)
    assert fake.analyzed_docs == []


def test_video_value_error_still_generic(conn):
    class Broken(FakeProvider):
        def analyze_video(self, youtube_id, title):
            raise ValueError("algo raro")

    add_video(conn, 1)
    process_pending(conn, Broken({}, always_right))
    assert material(conn, 1)["fail_kind"] == "error"


# --- cola -----------------------------------------------------------------------------------


def test_queue_orders_documents_by_exam_like_videos(conn):
    now = int(time.time())
    add_doc(conn, 1, section_id=1)
    add_video(conn, 2, section_id=1)
    add_doc(conn, 3, section_id=2)
    conn.execute(
        "INSERT INTO exam(id, course_id, cmid, name, opens_at, closes_at, attempts, time_limit_s,"
        " max_grade) VALUES (1, 20, 1, 'Parcial', ?, 0, 1, 3600, 10)", (now + 5 * DAY,))
    conn.commit()
    assert [item.material_id for item in pending_queue(conn, now, 10)] == [3, 1, 2]


def test_queue_skips_split_parent_and_other_kinds(conn):
    add_doc(conn, 1, pages=12)
    add_part(conn, 2, 1, 1, 6)
    conn.execute("INSERT INTO material(id, section_id, kind, title, status) VALUES"
                 " (3, 1, 'page', 'Una página', 'pending')")
    conn.commit()
    assert [item.material_id for item in pending_queue(conn, int(time.time()), 10)] == [2]
