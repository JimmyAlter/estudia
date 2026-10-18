import re
from pathlib import Path
from importlib.resources import files

import pytest
import sympy

from estudia.demo import DEMO_ASSET, load_demo_subject
from estudia.generation import pipeline, verify


def test_demo_subject_shape():
    subject = load_demo_subject()
    assert subject.course_name == "Matemática de ejemplo"
    assert subject.topics == ["Funciones lineales", "Porcentajes"]
    assert len(subject.questions) == 12
    for topic in subject.topics:
        assert sum(q.topic == topic for q in subject.questions) == 6
    for q in subject.questions:
        assert len(q.options) == 4
        assert 0 <= q.correct_index <= 3
        assert q.check_expr and q.check_expr.strip()
        assert q.explanation.strip()
        assert q.common_error.strip()
    assert len({q.stem for q in subject.questions}) == 12


def _every_shown_question():
    """Todo lo que la demo puede mostrar: cada pregunta y cada variante de su grupo."""
    out = []
    for q in load_demo_subject().questions:
        out.append(q)
        out.extend(q.variants)
    return out


# 12 preguntas y las 2 variantes del grupo.
@pytest.mark.parametrize("index", range(14))
def test_every_demo_question_passes_local_checks(index):
    # Con el worker de SymPy de verdad: la demo nunca muestra una respuesta mal.
    question = _every_shown_question()[index]
    assert pipeline._passes_local_checks(question) is True, question.stem


def test_demo_correct_options_recompute():
    # Segunda llave, sin el worker: SymPy directo en el proceso del test.
    for q in _every_shown_question():
        correct = sympy.nsimplify(verify._normalize_option(q.options[q.correct_index]))
        assert correct == sympy.sympify(q.check_expr), q.stem


def test_demo_has_one_variant_group_with_hand_checked_values():
    # f(x) = a·x − b, f(c): clave a·c − b; distractores a·c + b (el signo), a·(c − b) y a·c (sin el − b).
    groups = [q for q in load_demo_subject().questions if q.variants]
    assert len(groups) == 1
    head = groups[0]
    assert head.variant_values == {"a": 2, "b": 5, "c": 4}
    assert [v.values for v in head.variants] == [{"a": 3, "b": 4, "c": 5}, {"a": 4, "b": 7, "c": 3}]
    stems = {head.stem}
    for q, values in [(head, head.variant_values)] + [(v, v.values) for v in head.variants]:
        a, b, c = values["a"], values["b"], values["c"]
        assert q.stem == f"Si f(x) = {a}x − {b}, ¿cuánto vale f({c})?"
        key = a * c - b
        expected = {key, a * c + b, a * (c - b), a * c}
        assert len(expected) == 4   # ningún distractor coincide con la clave ni con otro
        shown = [int(verify._normalize_option(o)) for o in q.options]
        assert set(shown) == expected
        assert shown[q.correct_index] == key
        assert q.check_expr == str(key)
        if q is head:
            assert q.explanation == f"Se reemplaza x por {c}: f({c}) = {a}·{c} − {b} = {a * c} − {b} = {key}."
            assert q.common_error == f"Olvidarse del − {b} y quedarse con {a}·{c} = {a * c}."
        else:
            # Como las variantes de la tubería (`verify.variant_texts`): solo la línea fija, y
            # sin error común (el de la original tiene números).
            assert q.explanation == f"Con a = {a}, b = {b} y c = {c}, la respuesta es {key}."
            assert q.explanation == verify.variant_texts(
                "Se reemplaza x por {c}: f({c}) = {a}·{c} − {b} = {respuesta}.",
                "Olvidarse del − {b} y quedarse con {a}·{c}.", ["a", "b", "c"], values, str(key))[0]
            assert q.common_error == ""
        stems.add(q.stem)
    assert len(stems) == 3


def test_demo_json_has_no_punishing_words():
    text = (files("estudia") / "assets" / DEMO_ASSET).read_text(encoding="utf-8")
    assert not re.search(r"fracas|racha rota", text, re.IGNORECASE)


# --- La base de ejemplo (demo.db) ------------------------------------------------------------

def _build(tmp_path, now):
    from estudia import demo
    from estudia.db import connect

    path = tmp_path / demo.DEMO_FILE
    demo.build_demo_db(path, load_demo_subject(), now)
    return connect(path)


def test_build_demo_db_has_subject_exam_and_no_answers(tmp_path):
    from datetime import datetime, timedelta

    from estudia.demo import DEMO_SITE
    from estudia.study.schedule import TZ

    now = 1_790_000_000
    conn = _build(tmp_path, now)
    courses = conn.execute("SELECT id, site_id, fullname, shortname FROM course").fetchall()
    assert [tuple(r) for r in courses] == [(1, DEMO_SITE, "Matemática de ejemplo", "Ejemplo")]
    topics = conn.execute("SELECT id, slug, name FROM topic ORDER BY id").fetchall()
    assert [tuple(r) for r in topics] == [
        (1, pipeline.slugify("Funciones lineales"), "Funciones lineales"),
        (2, pipeline.slugify("Porcentajes"), "Porcentajes"),
    ]
    assert conn.execute("SELECT COUNT(*) FROM material_topic WHERE material_id = 1").fetchone()[0] == 2
    rows = conn.execute("SELECT verified, retired, origin, dedupe_key, material_id FROM question").fetchall()
    assert len(rows) == 14   # 12 preguntas y 2 variantes
    assert all(r["verified"] == 1 and r["retired"] == 0 and r["origin"] is None and r["material_id"] == 1
               for r in rows)
    assert {r["dedupe_key"] for r in rows} == {f"demo:{i}" for i in range(12)} | {"demo:1:1", "demo:1:2"}
    material = conn.execute("SELECT kind, status, updated_at, cmid FROM material").fetchone()
    assert (material["kind"], material["status"], material["cmid"]) == ("demo", "done", None)
    assert material["updated_at"] == now - 30 * 86400
    exams = conn.execute("SELECT * FROM exam").fetchall()
    assert len(exams) == 1
    exam = exams[0]
    assert (exam["source"], exam["kind"], exam["name"], exam["max_grade"]) == ("manual", "parcial",
                                                                               "Parcial de ejemplo", 10)
    assert exam["topics_json"] == "[1, 2]" and exam["created_at"] == now
    day = datetime.fromtimestamp(now, TZ).date() + timedelta(days=14)
    assert exam["opens_at"] == int(datetime(day.year, day.month, day.day, 18, tzinfo=TZ).timestamp())
    assert conn.execute("SELECT COUNT(*) FROM answer").fetchone()[0] == 0
    conn.close()


def test_demo_db_stores_the_variant_group_like_the_pipeline(tmp_path):
    import json

    conn = _build(tmp_path, 1_790_000_000)
    group = conn.execute(
        "SELECT id, stem, variant_group, variant_values_json FROM question"
        " WHERE variant_group IS NOT NULL ORDER BY id").fetchall()
    head = group[0]
    assert head["stem"] == "Si f(x) = 2x − 5, ¿cuánto vale f(4)?"
    assert [r["variant_group"] for r in group] == [head["id"]] * 3
    assert [json.loads(r["variant_values_json"]) for r in group] == [
        {"a": 2, "b": 5, "c": 4}, {"a": 3, "b": 4, "c": 5}, {"a": 4, "b": 7, "c": 3}]
    conn.close()


def test_demo_shows_another_member_after_a_miss(tmp_path):
    from estudia.study.practice import build_quiz, record_answer

    now = 1_790_000_000
    conn = _build(tmp_path, now)
    quiz = build_quiz(conn, 1, topic_id=1, size=20, now=now)
    # Las 6 tarjetas del tema: el grupo cuenta una vez.
    assert len(quiz) == 6
    shown = next(item for item in quiz if item.card_key.startswith("v:"))
    correct = conn.execute("SELECT correct_index FROM question WHERE id = ?",
                           (shown.question_id,)).fetchone()[0]
    fb = record_answer(conn, shown.question_id, (correct + 1) % 4, now + 60)
    assert fb.correct is False and fb.requeue is True
    again = fb.requeue_item
    assert again is not None
    assert again.card_key == shown.card_key
    assert again.question_id != shown.question_id
    assert again.stem != shown.stem
    assert fb.next_label == "Vuelve al final de la sesión, con otros números"
    conn.close()


def test_demo_db_mock_short_has_six_questions(tmp_path):
    from estudia.study import mock_exam

    now = 1_790_000_000
    conn = _build(tmp_path, now)
    start = mock_exam.start_mock(conn, 1, now, short=True)
    assert len(start.items) == 6
    conn.close()


def _dump(path):
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        return list(conn.iterdump())
    finally:
        conn.close()


def test_start_and_remove_demo_leave_real_db_identical_but_the_flag(tmp_path):
    from estudia import demo
    from estudia.db import connect

    real = tmp_path / "estudia.db"
    conn = connect(real)
    conn.execute("INSERT INTO kv(key, value) VALUES ('age_gate', 'adult')")
    conn.commit()
    before = _dump(real)

    demo.start_demo(conn, real, 1_790_000_000)
    assert demo.demo_active(conn, real)
    flag = "INSERT INTO \"kv\" VALUES('demo_active','1');"
    assert sorted(_dump(real)) == sorted(before + [flag])
    assert not (tmp_path / "demo.db.tmp").exists()

    demo.remove_demo(conn, real)
    assert not demo.demo_active(conn, real)
    assert _dump(real) == before
    for name in ("demo.db", "demo.db-journal", "demo.db-wal", "demo.db-shm", "demo.db.tmp"):
        assert not (tmp_path / name).exists()
    conn.close()


def test_start_demo_refused_with_real_courses(tmp_path):
    from estudia import demo
    from estudia.db import connect

    real = tmp_path / "estudia.db"
    conn = connect(real)
    conn.execute("INSERT INTO course(id, site_id, fullname) VALUES (5, 'utn-frba', 'Física')")
    conn.commit()
    with pytest.raises(demo.DemoUnavailable):
        demo.start_demo(conn, real, 1_790_000_000)
    assert not demo.demo_path(real).exists()
    conn.close()


def test_cleanup_leftover_demo_keeps_an_active_demo(tmp_path):
    from estudia import demo
    from estudia.db import connect

    real = tmp_path / "estudia.db"
    conn = connect(real)
    demo.start_demo(conn, real, 1_790_000_000)
    demo.cleanup_leftover_demo(conn, real)
    assert demo.demo_path(real).exists()
    conn.close()


def test_secret_names_live_in_one_place():
    from estudia import demo
    from estudia.moodle import names
    from estudia.server import app

    assert app.moodle_token_name is names.moodle_token_name
    assert app.moodle_private_name is names.moodle_private_name
    assert names.moodle_token_name("utn-frba") == "moodle_token:utn-frba"
    assert names.moodle_private_name("utn-frba") == "moodle_private:utn-frba"
    # Nadie arma el nombre a mano fuera de `estudia.moodle.names`.
    for module in (demo, app):
        text = Path(module.__file__).read_text(encoding="utf-8")
        assert '"moodle_token:' not in text and "f\"moodle_token:" not in text
        assert '"moodle_private:' not in text and "f\"moodle_private:" not in text


def test_cleanup_removes_every_leftover_of_the_demo(tmp_path):
    from estudia import demo
    from estudia.db import connect

    real = tmp_path / "estudia.db"
    conn = connect(real)
    names = ["demo.db", "demo.db-journal", "demo.db-wal", "demo.db-shm",
             "demo.db.tmp", "demo.db.tmp-journal", "demo.db.tmp-wal", "demo.db.tmp-shm"]
    for name in names:
        (tmp_path / name).write_bytes(b"resto")
    demo.cleanup_leftover_demo(conn, real)
    conn.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["estudia.db"]


def test_handler_takes_secret_names_from_the_names_module():
    from estudia.moodle import names
    from estudia.server import handler

    text = Path(handler.__file__).read_text(encoding="utf-8")
    assert "from estudia.moodle.names import" in text
    assert handler.moodle_token_name is names.moodle_token_name
    assert handler.moodle_private_name is names.moodle_private_name
