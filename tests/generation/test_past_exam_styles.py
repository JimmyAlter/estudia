"""Lectura del estilo de los parciales viejos: un pedido por parcial, contado como "pdf"."""
import pytest

from estudia.ai.base import (
    AIAuthError,
    AIDocumentUnreadableError,
    AIQuotaError,
    AIUnavailableError,
    ExtractedExercise,
    ExtractedStyle,
)
from estudia.ai.gate import AIBlocked
from estudia.ai.quota import used_today
from estudia.db import connect
from estudia.generation.past_exams import extract_pending_styles
from estudia.generation.pipeline import BUDGET, STOP_KEY
from estudia.study.exam_style import ExamStyle
from estudia.study.exams import create_manual_exam
from estudia.study.past_exams import UNREADABLE_TEXT
from fakes import FakeProvider

NOW = 1_790_000_000
STYLE = ExtractedStyle(
    exercises=[ExtractedExercise(kind="opcion_multiple", points=1)] * 8
    + [ExtractedExercise(kind="desarrollo", points=None)] * 2,
    minutes=120, max_grade=10, topics=["Funciones lineales", "Termodinámica"], formula_sheet=False)


@pytest.fixture
def env(tmp_path):
    conn = connect(tmp_path / "estudia.db")
    conn.execute("INSERT INTO course(id, site_id, fullname, shortname) VALUES (10, 's', 'Análisis', 'AM')")
    conn.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (1, 10, 'funcion-lineal', 'Función lineal')")
    conn.commit()
    exam_id = create_manual_exam(conn, 10, "Primer parcial", NOW + 86400, [], None, now=NOW)
    yield {"conn": conn, "base": tmp_path, "exam_id": exam_id}
    conn.close()


def add(env, data: bytes, kind="pdf") -> int:
    """Un parcial pendiente con su archivo, como lo deja la carga (Tarea 8)."""
    suffix = {"pdf": "pdf", "jpeg": "jpg", "png": "png"}[kind]
    cur = env["conn"].execute(
        "INSERT INTO past_exam(exam_id, name, kind, size, sha256, status, created_at)"
        " VALUES (?, 'Parcial', ?, ?, ?, 'pending', ?)",
        (env["exam_id"], kind, len(data), data.hex(), NOW))
    pid = cur.lastrowid
    folder = env["base"] / "parciales"
    folder.mkdir(exist_ok=True)
    (folder / f"{pid}.{suffix}").write_bytes(data)
    env["conn"].execute("UPDATE past_exam SET path = ? WHERE id = ?", (f"parciales/{pid}.{suffix}", pid))
    env["conn"].commit()
    return pid


def statuses(env):
    return [tuple(r) for r in env["conn"].execute("SELECT status, error FROM past_exam ORDER BY id")]


def provider(**styles):
    return FakeProvider({}, lambda s, o: 1, styles={k.encode(): v for k, v in styles.items()})


def test_extraction_one_request_per_past_exam_counted_as_pdf(env):
    add(env, b"uno")
    add(env, b"dos", kind="jpeg")
    fake = provider(uno=STYLE, dos=STYLE)
    assert extract_pending_styles(env["conn"], fake, NOW, env["base"]) == 2
    assert statuses(env) == [("done", None), ("done", None)]
    # Los dos pedidos ya pasaron al uso del día (y el contador del proveedor quedó en cero).
    assert used_today(env["conn"], "pdf", NOW) == 2 and fake.requests["pdf"] == 0
    assert [(e.kind, topics) for e, topics in fake.style_calls] == [
        ("pdf", ["Función lineal"]), ("jpeg", ["Función lineal"])]
    stored = ExamStyle.model_validate_json(
        env["conn"].execute("SELECT style_json FROM past_exam WHERE id = 1").fetchone()[0])
    assert [(g.kind, g.count) for g in stored.groups] == [("opcion_multiple", 8), ("desarrollo", 2)]
    assert stored.topic_ids == [1] and stored.topics_unmatched == ["Termodinámica"]
    # Lo hecho no se vuelve a pedir.
    assert extract_pending_styles(env["conn"], fake, NOW, env["base"]) == 0
    assert len(fake.style_calls) == 2


def test_extraction_respects_pdf_budget(env):
    env["conn"].execute("INSERT INTO kv(key, value) VALUES ('ai_budget:pdf', '1')")
    env["conn"].commit()
    add(env, b"uno")
    add(env, b"dos")
    fake = provider(uno=STYLE, dos=STYLE)
    assert extract_pending_styles(env["conn"], fake, NOW, env["base"]) == 1
    assert statuses(env) == [("done", None), ("pending", None)]
    assert len(fake.style_calls) == 1
    assert env["conn"].execute("SELECT value FROM kv WHERE key = ?", (STOP_KEY,)).fetchone()[0] == BUDGET


def test_unreadable_past_exam_fails_and_the_rest_go_on(env):
    add(env, b"roto")
    add(env, b"sin respuesta")
    add(env, b"bien")
    gone = add(env, b"borrado")
    (env["base"] / "parciales" / f"{gone}.pdf").unlink()
    fake = provider(bien=STYLE, roto=STYLE.model_copy(update={"exercises": []}))
    original = fake.extract_exam_style

    def extract(exam, topics):
        if exam.data == b"sin respuesta":
            raise AIDocumentUnreadableError("ilegible")
        return original(exam, topics)

    fake.extract_exam_style = extract
    # De a dos intentos por corrida (el que no tiene archivo no llega a Gemini ni cuenta).
    assert extract_pending_styles(env["conn"], fake, NOW, env["base"]) == 0
    assert extract_pending_styles(env["conn"], fake, NOW, env["base"]) == 1
    assert statuses(env) == [("failed", UNREADABLE_TEXT), ("failed", UNREADABLE_TEXT),
                             ("done", None), ("failed", UNREADABLE_TEXT)]
    assert env["conn"].execute("SELECT COUNT(*) FROM past_exam WHERE style_json IS NOT NULL"
                               " AND status != 'done'").fetchone()[0] == 0


@pytest.mark.parametrize("error", [AIQuotaError("cuota"), AIUnavailableError("caído"),
                                   AIAuthError("clave"), AIBlocked("menor")])
def test_stop_errors_propagate_keep_pending_and_record_usage(env, error):
    add(env, b"uno")
    add(env, b"dos")
    fake = provider(uno=STYLE)
    original = fake.extract_exam_style

    def extract(exam, topics):
        if exam.data == b"dos":
            fake.requests["pdf"] += 1  # el pedido se hizo y Google lo cobró
            raise error
        return original(exam, topics)

    fake.extract_exam_style = extract
    with pytest.raises(type(error)):
        extract_pending_styles(env["conn"], fake, NOW, env["base"])
    assert statuses(env) == [("done", None), ("pending", None)]
    assert used_today(env["conn"], "pdf", NOW) == 2


def test_at_most_two_past_exams_per_run(env):
    from estudia.generation.past_exams import MAX_STYLES_PER_RUN

    assert MAX_STYLES_PER_RUN == 2
    for name in ("uno", "dos", "tres"):
        add(env, name.encode())
    fake = provider(uno=STYLE, dos=STYLE, tres=STYLE)
    assert extract_pending_styles(env["conn"], fake, NOW, env["base"]) == 2
    assert statuses(env) == [("done", None), ("done", None), ("pending", None)]
    assert len(fake.style_calls) == 2
    assert env["conn"].execute("SELECT 1 FROM kv WHERE key = ?", (STOP_KEY,)).fetchone() is None
    # La próxima corrida toma el que quedó.
    assert extract_pending_styles(env["conn"], fake, NOW, env["base"]) == 1
    assert [s for s, _ in statuses(env)] == ["done"] * 3


def test_cap_counts_attempts_not_successes(env):
    for name in ("roto", "mal", "bien"):
        add(env, name.encode())
    fake = provider(bien=STYLE)  # los dos primeros no tienen estilo: AIFormatError
    assert extract_pending_styles(env["conn"], fake, NOW, env["base"]) == 0
    assert [s for s, _ in statuses(env)] == ["failed", "failed", "pending"]


def test_cut_topic_names(env):
    many = STYLE.model_copy(update={"topics": [f"Tema raro {i} " + "x" * 200 for i in range(15)]})
    add(env, b"uno")
    assert extract_pending_styles(env["conn"], provider(uno=many), NOW, env["base"]) == 1
    stored = ExamStyle.model_validate_json(
        env["conn"].execute("SELECT style_json FROM past_exam").fetchone()[0])
    assert len(stored.topics_unmatched) == 10
    assert all(len(name) <= 80 for name in stored.topics_unmatched)


def test_extraction_logs_nothing_from_the_past_exam(env, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    env["conn"].execute("UPDATE past_exam SET name = 'x'")  # (ninguno todavía)
    first = add(env, b"contenido-secreto-uno")
    second = add(env, b"contenido-secreto-dos")
    env["conn"].execute("UPDATE past_exam SET name = 'Parcial de Pérez secreto.pdf'")
    env["conn"].commit()
    fake = provider(**{"contenido-secreto-uno": STYLE})
    original = fake.extract_exam_style

    def extract(exam, topics):
        if exam.data == b"contenido-secreto-dos":
            raise RuntimeError(f"no se pudo: {exam.data!r}")
        return original(exam, topics)

    fake.extract_exam_style = extract
    extract_pending_styles(env["conn"], fake, NOW, env["base"])
    assert [s for s, _ in statuses(env)] == ["done", "failed"] and first < second
    assert "secreto" not in caplog.text and "Pérez" not in caplog.text
