import json
import logging

import pytest

from estudia.db import connect
from estudia.generation.pipeline import dedupe_key
from estudia.generation.renormalize import normalize_existing


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    c.execute("INSERT INTO topic(id, course_id, slug, name) VALUES"
              " (1, 10, 'definicion-y-grafica-de-la-funcion-cuadratica',"
              " 'Definición y Gráfica de la Función Cuadrática'),"
              " (2, 10, 'ingreso-a-la-utn', 'Ingreso a la UTN')")
    c.execute("INSERT INTO material(id, section_id, cmid, kind, title, ref, status, summary_md)"
              " VALUES (1, 1, 1001, 'youtube', 'V', 'v1', 'done', '**Forma**: `y = x**2 + 3`')")
    c.commit()
    yield c
    c.close()


def add_question(conn, qid, stem, options, check_expr=None):
    conn.execute(
        "INSERT INTO question(id, material_id, topic_id, stem, options_json, correct_index,"
        " explanation, common_error, source_seconds, check_expr, verified, retired, dedupe_key)"
        " VALUES (?, 1, 1, ?, ?, 0, 'y = x**2', 'usar a*b', 0, ?, 1, 0, ?)",
        (qid, stem, json.dumps(options), check_expr, dedupe_key(1, stem)),
    )
    conn.commit()


def test_normalizes_questions_summaries_and_topics(conn):
    add_question(conn, 1, "¿Cuál es `f: R -> R`?", ["x**2", "-1", "a1 * a2", "2"], "2**3")
    result = normalize_existing(conn)
    assert (result.questions, result.summaries, result.topics, result.skipped) == (1, 1, 1, [])
    row = conn.execute("SELECT * FROM question WHERE id=1").fetchone()
    assert row["stem"] == "¿Cuál es f: R → R?"
    assert json.loads(row["options_json"]) == ["x²", "−1", "a1 · a2", "2"]
    assert (row["explanation"], row["common_error"]) == ("y = x²", "usar a·b")
    assert row["check_expr"] == "2**3"
    assert row["dedupe_key"] == dedupe_key(1, "¿Cuál es f: R → R?")
    summary = conn.execute("SELECT summary_md FROM material").fetchone()[0]
    assert summary == "**Forma**: y = x² + 3"
    names = [tuple(r) for r in conn.execute("SELECT slug, name FROM topic ORDER BY id")]
    assert names == [
        ("definicion-y-grafica-de-la-funcion-cuadratica",
         "Definición y gráfica de la función cuadrática"),
        ("ingreso-a-la-utn", "Ingreso a la UTN"),
    ]


def test_is_idempotent(conn):
    add_question(conn, 1, "y = x**2", ["1", "2", "3", "4"])
    normalize_existing(conn)
    before = [tuple(r) for r in conn.execute("SELECT * FROM question")]
    again = normalize_existing(conn)
    assert (again.questions, again.summaries, again.topics, again.skipped) == (0, 0, 0, [])
    assert [tuple(r) for r in conn.execute("SELECT * FROM question")] == before


def test_skips_question_whose_options_would_collide(conn, caplog):
    add_question(conn, 7, "Secreto: y = x**2", ["x**2", "x^2", "2x", "x"])
    with caplog.at_level(logging.WARNING):
        result = normalize_existing(conn)
    assert result.skipped == [7] and result.questions == 0
    row = conn.execute("SELECT stem, options_json FROM question WHERE id=7").fetchone()
    assert row["stem"] == "Secreto: y = x**2"
    assert json.loads(row["options_json"]) == ["x**2", "x^2", "2x", "x"]
    assert "7" in caplog.text and "Secreto" not in caplog.text and "x**2" not in caplog.text


def test_skips_question_whose_stem_would_repeat_another(conn):
    add_question(conn, 1, "y = x²", ["1", "2", "3", "4"])
    add_question(conn, 2, "y = x**2", ["1", "2", "3", "4"])
    result = normalize_existing(conn)
    assert result.skipped == [2]
    assert conn.execute("SELECT stem FROM question WHERE id=2").fetchone()[0] == "y = x**2"


def test_rewritten_options_forget_the_typed_check(conn):
    add_question(conn, 1, "¿Cuánto?", ["x**2", "2", "3", "4"], "2")
    add_question(conn, 2, "¿Otra?", ["1", "2", "3", "4"], "2")       # opciones sin cambios
    conn.execute("UPDATE question SET typed_safe = 1")
    conn.commit()
    normalize_existing(conn)
    flags = dict(conn.execute("SELECT id, typed_safe FROM question").fetchall())
    assert flags == {1: None, 2: 1}
