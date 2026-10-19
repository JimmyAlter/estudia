"""Análisis de PDF y diapositivas con Gemini (sin red: cliente falso)."""
import hashlib
import json
from pathlib import Path

import pytest
from google.genai import errors

from estudia.ai import gemini
from estudia.ai.base import (
    AIAuthError,
    AIDocumentUnreadableError,
    AIFormatError,
    AIQuotaError,
    DocumentAnalysis,
    DocumentInput,
)
from estudia.generation.prompts import ANALYSIS_PROMPT, DOCUMENT_PROMPT

from test_gemini import FakeFiles, _provider

# sha256 de ANALYSIS_PROMPT al empezar la Tarea 5 (etapa 3b): el prompt de video no cambia.
VIDEO_PROMPT_SHA256 = "5fc8b4ffeb86bdf89173278ddb548012787e9f189b641f70ad5a7cb738eec373"

PDF_BYTES = b"%PDF-1.4 apunte de prueba"


def _doc_question(**over):
    q = {
        "stem": "Si f(x) = 2x + 1, cuánto vale f(3)?",
        "options": ["5", "6", "7", "8"],
        "correct_index": 2,
        "explanation": "2·3 + 1 = 7",
        "common_error": "Olvidar sumar 1",
        "topic": "Funciones",
        "source_page": 2,
        "check_expr": "7",
    }
    q.update(over)
    return q


def _doc_analysis(*questions):
    return json.dumps({
        "summary_md": "## Funciones (pág. 1)\n- f(x) = ax + b",
        "topics": ["Funciones"],
        "questions": list(questions) or [_doc_question()],
    })


def _pdf(pages=3, data=PDF_BYTES):
    return DocumentInput(kind="pdf", title="Apunte 3", pages=pages, data=data)


def _client_error(code, status):
    return errors.ClientError(code, {"error": {"code": code, "status": status, "message": "x"}})


def test_small_pdf_sent_inline_counted_as_pdf():
    p, client = _provider(text=_doc_analysis())
    result = p.analyze_document(_pdf())
    assert isinstance(result, DocumentAnalysis)
    assert result.questions[0].source_page == 2
    call = client.calls[0]
    doc_part, prompt_part = call.contents.parts
    assert doc_part.inline_data.mime_type == "application/pdf"
    assert doc_part.inline_data.data == PDF_BYTES
    assert "el apunte en PDF" in prompt_part.text and "Apunte 3" in prompt_part.text
    assert call.config.response_schema is DocumentAnalysis
    assert call.config.media_resolution is None
    assert p.requests == {"analysis": 0, "blind": 0, "pdf": 1}
    assert client.files.uploaded == []
    assert p.last_analysis_model == "m"


def test_large_pdf_uploaded_and_remote_deleted(monkeypatch):
    monkeypatch.setattr(gemini, "INLINE_DOCUMENT_MAX_BYTES", 10)
    files = FakeFiles(polls=0)
    p, client = _provider(text=_doc_analysis(), files=files)
    p.analyze_document(_pdf())
    (path, mime), = files.uploaded
    assert mime == "application/pdf"
    assert not path.exists()  # el temporal local se borra
    assert client.calls[0].contents.parts[0].file_data.file_uri == "https://gemini/files/abc"
    assert files.deleted == ["files/abc"]
    assert p.requests["pdf"] == 1

    # También se borra cuando el análisis falla.
    files = FakeFiles(polls=0)
    p, client = _provider(text="esto no es JSON", files=files)
    with pytest.raises(AIFormatError):
        p.analyze_document(_pdf())
    assert files.deleted == ["files/abc"]
    assert not files.uploaded[0][0].exists()


def test_large_pdf_deleted_when_processing_fails(monkeypatch):
    monkeypatch.setattr(gemini, "INLINE_DOCUMENT_MAX_BYTES", 10)
    from google.genai import types
    files = FakeFiles(polls=0, final_state=types.FileState.FAILED)
    p, client = _provider(text=_doc_analysis(), files=files)
    with pytest.raises(AIDocumentUnreadableError):
        p.analyze_document(_pdf())
    assert files.deleted == ["files/abc"]
    assert client.calls == []


def test_slides_sent_as_text():
    p, client = _provider(text=_doc_analysis())
    doc = DocumentInput(kind="pptx", title="Clase 2", pages=4,
                        text="Diapositiva 1:\nFunciones\n\nDiapositiva 2:\nf(x) = 2x + 1")
    p.analyze_document(doc)
    text_part, prompt_part = client.calls[0].contents.parts
    assert text_part.text.startswith("Diapositiva 1:")
    assert text_part.inline_data is None and text_part.file_data is None
    assert "las diapositivas" in prompt_part.text and "Clase 2" in prompt_part.text
    assert client.files.uploaded == []
    assert p.requests == {"analysis": 0, "blind": 0, "pdf": 1}


def test_source_page_out_of_range_becomes_none():
    p, _ = _provider(text=_doc_analysis(
        _doc_question(source_page=0),
        _doc_question(source_page=4),
        _doc_question(source_page=3),
        _doc_question(source_page=None),
    ))
    result = p.analyze_document(_pdf(pages=3))
    assert [q.source_page for q in result.questions] == [None, None, 3, None]


def test_document_validation_matches_video_rules():
    p, _ = _provider(text=_doc_analysis(_doc_question(options=["1", "1", "2", "3"]), _doc_question()))
    assert len(p.analyze_document(_pdf()).questions) == 1
    p, _ = _provider(text=json.dumps({"summary_md": "  ", "topics": ["A"], "questions": [_doc_question()]}))
    with pytest.raises(AIFormatError):
        p.analyze_document(_pdf())


def test_document_403_is_not_auth_error():
    p, _ = _provider(error=_client_error(403, "PERMISSION_DENIED"))
    with pytest.raises(AIDocumentUnreadableError) as info:
        p.analyze_document(_pdf())
    assert not isinstance(info.value, AIAuthError)
    assert str(info.value) == "Gemini no pudo leer el archivo"
    assert p.requests["pdf"] == 1  # un 403 gasta el pedido


def test_document_400_is_unreadable():
    p, _ = _provider(error=_client_error(400, "INVALID_ARGUMENT"))
    with pytest.raises(AIDocumentUnreadableError):
        p.analyze_document(_pdf())


def test_document_400_invalid_key_is_auth_error():
    """Un 400 que dice que la clave no vale es AIAuthError: se juzga antes que "no pude leer el archivo"."""
    err = errors.ClientError(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                             "message": "API key not valid. Please pass a valid API key."}})
    p, _ = _provider(error=err)
    with pytest.raises(AIAuthError) as info:
        p.analyze_document(_pdf())
    assert not isinstance(info.value, AIDocumentUnreadableError)


def test_document_429_not_counted():
    p, _ = _provider(error=_client_error(429, "RESOURCE_EXHAUSTED"))
    with pytest.raises(AIQuotaError):
        p.analyze_document(_pdf())
    assert p.requests["pdf"] == 0


def test_document_prompt_rules():
    text = DOCUMENT_PROMPT.format(title="Apunte 3", source="el apunte en PDF", pages_note="")
    assert ("Contá las páginas desde la primera hoja de este archivo (la primera es la 1);"
            " ignorá los números impresos en las hojas.") in text
    assert ('- "source_page": número de página del archivo (la primera es 1) donde se explica '
            'el tema de la pregunta; en diapositivas, el número de diapositiva.') in text
    assert '- En "summary_md", cada título termina con la página donde empieza, así: (pág. 14).' in text
    assert "source_seconds" not in text and "segments" not in text
    assert '"check_expr": completalo SOLO si' in text
    assert "Notación:" in text and "Los distractores" in text


def test_video_prompt_unchanged():
    assert hashlib.sha256(ANALYSIS_PROMPT.encode()).hexdigest() == VIDEO_PROMPT_SHA256


def test_failed_temp_write_removes_partial_file(monkeypatch):
    from estudia.ai.base import AIUnavailableError
    monkeypatch.setattr(gemini, "INLINE_DOCUMENT_MAX_BYTES", 10)
    real_open = open
    written = []

    def failing(path, mode="r", *args, **kwargs):
        fh = real_open(path, mode, *args, **kwargs)
        written.append(Path(path))

        class Broken:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                fh.close()

            def write(self, data):
                raise OSError("disco lleno")
        return Broken()

    monkeypatch.setattr(gemini, "open", failing, raising=False)
    files = FakeFiles(polls=0)
    p, client = _provider(text=_doc_analysis(), files=files)
    with pytest.raises(AIUnavailableError):
        p.analyze_document(_pdf())
    assert written and not written[0].exists()
    assert files.uploaded == [] and client.calls == []


def test_upload_copy_goes_to_data_dir_with_sweepable_prefix(monkeypatch):
    """La copia para subir va en la carpeta de datos (no en el temporal del sistema), con un
    prefijo que el barrido del arranque reconoce."""
    from estudia.config import data_dir
    from estudia.material.files import UPLOAD_COPY_PREFIX, sweep_upload_copies
    monkeypatch.setattr(gemini, "INLINE_DOCUMENT_MAX_BYTES", 10)
    files = FakeFiles(polls=0)
    p, _ = _provider(text=_doc_analysis(), files=files)
    p.analyze_document(_pdf())
    (path, _), = files.uploaded
    assert path.parent == data_dir() and path.name.startswith(UPLOAD_COPY_PREFIX)
    assert not path.exists()
    (data_dir() / f"{UPLOAD_COPY_PREFIX}viejo.pdf").write_bytes(b"x")
    (data_dir() / "estudia.txt").write_bytes(b"x")
    sweep_upload_copies(data_dir())
    assert not (data_dir() / f"{UPLOAD_COPY_PREFIX}viejo.pdf").exists()
    assert (data_dir() / "estudia.txt").exists()


def test_document_upload_errors(monkeypatch):
    import httpx
    from estudia.ai.base import AIUnavailableError
    monkeypatch.setattr(gemini, "INLINE_DOCUMENT_MAX_BYTES", 10)
    p, client = _provider(text=_doc_analysis(),
                          files=FakeFiles(upload_error=_client_error(400, "INVALID_ARGUMENT")))
    with pytest.raises(AIDocumentUnreadableError):
        p.analyze_document(_pdf())
    p, client = _provider(text=_doc_analysis(), files=FakeFiles(upload_error=httpx.ConnectError("x")))
    with pytest.raises(AIUnavailableError):
        p.analyze_document(_pdf())
    assert client.calls == [] and p.requests["pdf"] == 0


# --- Estilo de un parcial viejo (Tarea 9) ---------------------------------------------

STYLE_JSON = json.dumps({
    "exercises": [{"kind": "opcion_multiple", "points": 1}, {"kind": "desarrollo", "points": None}],
    "minutes": 120, "max_grade": 10, "topics": ["Función lineal"], "formula_sheet": True,
})


def test_exam_style_photo_inline_counted_as_pdf():
    from estudia.ai.base import ExtractedStyle, PastExamInput

    p, client = _provider(text=STYLE_JSON)
    result = p.extract_exam_style(PastExamInput(kind="jpeg", data=b"\xff\xd8foto"),
                                  ["Función lineal", "Límites"])
    assert isinstance(result, ExtractedStyle)
    assert [e.kind for e in result.exercises] == ["opcion_multiple", "desarrollo"]
    doc_part, prompt_part = client.calls[0].contents.parts
    assert doc_part.inline_data.mime_type == "image/jpeg"
    assert "- Función lineal" in prompt_part.text and "- Límites" in prompt_part.text
    assert "no transcribas ningún enunciado" in prompt_part.text.lower()
    assert client.calls[0].config.response_schema is ExtractedStyle
    assert p.requests == {"analysis": 0, "blind": 0, "pdf": 1}


def test_exam_style_large_pdf_uploaded_and_deleted(monkeypatch):
    from estudia.ai.base import PastExamInput

    monkeypatch.setattr(gemini, "INLINE_DOCUMENT_MAX_BYTES", 10)
    files = FakeFiles(polls=0)
    p, _ = _provider(text="no es JSON", files=files)
    with pytest.raises(AIFormatError):
        p.extract_exam_style(PastExamInput(kind="pdf", data=PDF_BYTES), [])
    (path, mime), = files.uploaded
    assert mime == "application/pdf" and not path.exists()
    assert files.deleted == ["files/abc"]


def test_exam_style_errors():
    from estudia.ai.base import PastExamInput

    exam = PastExamInput(kind="png", data=b"\x89PNG")
    p, _ = _provider(error=_client_error(400, "INVALID_ARGUMENT"))
    with pytest.raises(AIDocumentUnreadableError):
        p.extract_exam_style(exam, [])
    p, _ = _provider(text=json.dumps({"exercises": [{"kind": "ensayo"}], "minutes": None,
                                      "max_grade": None, "topics": [], "formula_sheet": False}))
    with pytest.raises(AIFormatError):
        p.extract_exam_style(exam, [])
    p, client = _provider(text=STYLE_JSON)
    with pytest.raises(AIFormatError):
        p.extract_exam_style(PastExamInput(kind="gif", data=b"GIF89a"), [])
    assert client.calls == []


def _style_request(**over):
    from estudia.ai.base import StyleRequest

    values = {"course": "Análisis", "kinds": {"opcion_multiple": 2, "desarrollo": 1},
              "topics": ["Función lineal"], "material_text": "## Apunte\nLa pendiente {m}.",
              "formula_sheet": True}
    return StyleRequest(**{**values, **over})


def _style_question(**over):
    q = {"stem": "¿Pendiente de y = 2x?", "options": ["2", "1", "0", "4"], "correct_index": 0,
         "explanation": "e", "common_error": "c", "topic": "Función lineal",
         "kind": "opcion_multiple", "check_expr": "2"}
    return {**q, **over}


def test_style_questions_counted_as_analysis_with_material_only():
    from estudia.ai.base import StyleQuestions

    good = _style_question()
    bad = _style_question(stem="tres opciones", options=["1", "2", "3"])
    p, client = _provider(text=json.dumps({"questions": [good, bad]}), analyze_models=("gen",),
                          blind_models=("b",))
    result = p.generate_style_questions(_style_request())
    assert [q.stem for q in result] == ["¿Pendiente de y = 2x?"]  # la mal armada se descarta
    assert p.last_analysis_model == "gen"
    assert p.requests == {"analysis": 1, "blind": 0, "pdf": 0}
    call, = client.calls
    assert call.model == "gen" and call.config.response_schema is StyleQuestions
    prompt = call.contents
    assert "Usá solo el material de la clase que va abajo; no inventes contenido que no esté" in prompt
    assert "¿Cuál de estas afirmaciones es verdadera?" in prompt
    assert '- 2 de tipo "opcion_multiple"' in prompt and '- 1 de tipo "desarrollo"' in prompt
    assert "- Función lineal" in prompt and prompt.rstrip().endswith("La pendiente {m}.")
    assert "hoja de fórmulas" in prompt
    assert "no copies enunciados de parciales" in prompt


def test_style_questions_without_valid_questions_is_format_error():
    p, _ = _provider(text=json.dumps({"questions": [_style_question(kind="ensayo")]}))
    with pytest.raises(AIFormatError):
        p.generate_style_questions(_style_request())
    assert p.last_analysis_model is None
    p, _ = _provider(text=json.dumps({"questions": [_style_question(correct_index=4)]}))
    with pytest.raises(AIFormatError):
        p.generate_style_questions(_style_request())


def test_part_prompt_states_page_count():
    p, client = _provider(text=_doc_analysis())
    p.analyze_document(DocumentInput(kind="pdf", title="Libro (2)", pages=26, data=PDF_BYTES,
                                     part=True))
    assert "Este archivo tiene 26 páginas." in client.calls[0].contents.parts[1].text
    p, client = _provider(text=_doc_analysis())
    p.analyze_document(_pdf())
    assert "Este archivo tiene" not in client.calls[0].contents.parts[1].text


def test_document_prompt_says_apunte_where_video_says_clase():
    text = DOCUMENT_PROMPT.format(title="Apunte 3", source="el apunte en PDF", pages_note="")
    assert "notación matemática del apunte" in text and "temas del material" in text
    assert "de clase, nunca" not in text and "temas de la clase" not in text
