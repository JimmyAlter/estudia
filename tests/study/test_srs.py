import json
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fsrs import Rating

from estudia.db import connect
from estudia.study.exams import create_manual_exam, delete_manual_exam, update_manual_exam
from estudia.study.mastery import topic_probability
from estudia.study.practice import record_answer
from estudia.study.srs import (
    STATE_RELEARNING,
    STATE_REVIEW,
    EXAMS_KEY,
    WATERMARK_KEY,
    _pulled_due,
    local_day,
    rating_for,
    sync_review_state,
)

TZ = ZoneInfo("America/Argentina/Buenos_Aires")
DAY = 86400


def local(day: int, hour: int = 0, minute: int = 0) -> int:
    """Epoch de una hora local de Buenos Aires en octubre de 2026."""
    return int(datetime(2026, 10, day, hour, minute, tzinfo=TZ).timestamp())


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    c.execute("INSERT INTO course(id, site_id, fullname, shortname) VALUES (10, 's', 'Análisis', 'AM')")
    c.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (1, 10, 'a', 'Tema A')")
    c.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (2, 10, 'b', 'Tema B')")
    for qid in range(1, 11):
        c.execute(
            "INSERT INTO question(id, topic_id, stem, options_json, correct_index, verified, check_expr) "
            "VALUES (?, ?, 'p', '[\"x\",\"y\"]', 0, 1, '1')",
            (qid, 1 if qid <= 8 else 2),
        )
    c.commit()
    return c


def ans(conn, qid, correct, at, **extras):
    if extras.get("mode") == "typed":   # escrita: se corrige contra check_expr = 1
        return record_answer(conn, qid, None, at, typed_text="1" if correct else "2", **extras)
    return record_answer(conn, qid, 0 if correct else 1, at, **extras)


def state(conn, qid):
    row = conn.execute("SELECT * FROM review_state WHERE card_key = ?", (f"q:{qid}",)).fetchone()
    return dict(row) if row else None


def test_rating_matrix():
    assert rating_for(False, 2, "typed") == Rating.Again
    assert rating_for(True, 1, "mcq") == Rating.Hard
    assert rating_for(True, 2, "typed_then_mcq") == Rating.Hard
    assert rating_for(True, 2, "mcq") == Rating.Good
    assert rating_for(True, 2, "mock") == Rating.Good
    assert rating_for(True, None, None) == Rating.Good   # respuestas viejas: "Seguro"
    assert rating_for(True, 1, "typed") == Rating.Hard


def test_three_correct_same_day_not_learned(conn):
    ans(conn, 1, False, local(17, 10))
    for minute in (5, 10, 15):
        ans(conn, 1, True, local(17, 10, minute))
    s = state(conn, 1)
    assert (s["state"], s["relearn_days"], s["relearn_last_day"]) == (STATE_RELEARNING, 1, "2026-10-17")
    # sale de la cola del día y vuelve al empezar el día siguiente
    assert s["due"] == local(18)


def test_relearn_graduates_on_third_distinct_local_day(conn):
    ans(conn, 1, False, local(17, 20))
    ans(conn, 1, True, local(17, 20, 30))      # 23:30 UTC del 17
    assert state(conn, 1)["relearn_days"] == 1
    late = local(17, 23, 30)                   # 02:30 UTC del 18: otro día en UTC, el mismo en Buenos Aires
    assert datetime.fromtimestamp(late, ZoneInfo("UTC")).date() > local_day(late)
    ans(conn, 1, True, late)
    assert state(conn, 1)["relearn_days"] == 1
    ans(conn, 1, True, local(18, 0, 30))       # 03:30 UTC del 18, mismo día UTC que el anterior: día local nuevo
    s = state(conn, 1)
    assert (s["state"], s["relearn_days"]) == (STATE_RELEARNING, 2)
    assert s["due"] == local(20)               # 2 días después del 2.º acierto
    ans(conn, 1, True, local(20, 23, 30), confidence=1)   # "Me parece" (Hard) también vale
    s = state(conn, 1)
    assert (s["state"], s["relearn_days"]) == (STATE_REVIEW, 3)
    assert s["due"] > local(21)                # graduada: manda FSRS


def test_error_resets_relearning(conn):
    ans(conn, 1, False, local(17, 10))
    ans(conn, 1, True, local(17, 11))
    ans(conn, 1, True, local(18, 11))
    assert state(conn, 1)["relearn_days"] == 2
    ans(conn, 1, False, local(20, 9))
    s = state(conn, 1)
    assert (s["state"], s["relearn_days"], s["relearn_last_day"]) == (STATE_RELEARNING, 0, None)
    assert s["due"] == local(20, 9)            # vuelve en la misma sesión
    ans(conn, 1, True, local(20, 9, 5))
    assert state(conn, 1)["relearn_days"] == 1


def test_guess_correct_is_again(conn):
    fb = ans(conn, 1, True, local(17, 10), confidence=0)
    assert fb.correct is True
    ans(conn, 2, False, local(17, 10))
    guessed, wrong = state(conn, 1), state(conn, 2)
    assert guessed["state"] == STATE_RELEARNING and guessed["relearn_days"] == 0
    assert guessed["stability"] == wrong["stability"]
    # suma como un error: aporta como mucho 0,5 aunque recién se haya respondido
    assert topic_probability(conn, 1, local(17, 10)) == pytest.approx(2 * 0.5 / 8)


def test_typed_sure_is_easy(conn):
    ans(conn, 1, True, local(17, 10), confidence=2, mode="typed")
    ans(conn, 2, True, local(17, 10), confidence=2, mode="mcq")
    ans(conn, 3, True, local(17, 10), confidence=2, mode="typed_then_mcq")
    easy, good, hard = state(conn, 1), state(conn, 2), state(conn, 3)
    assert easy["stability"] > good["stability"] > hard["stability"]
    assert easy["due"] > good["due"] > hard["due"]
    stored = conn.execute("SELECT confidence, mode FROM answer WHERE question_id = 1").fetchone()
    assert tuple(stored) == (2, "typed")


def test_answer_extras_are_stored_and_validated(conn):
    ans(conn, 1, True, local(17, 10), confidence=1, mode="mcq_think_first", latency_ms=4200,
        option_order=[1, 0], typed_text="2*x")
    row = conn.execute("SELECT * FROM answer").fetchone()
    assert (row["confidence"], row["mode"], row["latency_ms"], json.loads(row["option_order_json"]),
            row["typed_text"]) == (1, "mcq_think_first", 4200, [1, 0], "2*x")
    for bad in ({"confidence": 3}, {"mode": "otro"}, {"latency_ms": -1}, {"option_order": ["a"]}):
        with pytest.raises(ValueError):
            ans(conn, 2, True, local(17, 10), **bad)
    assert conn.execute("SELECT COUNT(*) FROM answer").fetchone()[0] == 1


def test_pretest_ignored(conn):
    ans(conn, 1, False, local(17, 10), mode="pretest")
    ans(conn, 1, True, local(17, 10, 1), mode="pretest")
    assert state(conn, 1) is None
    assert topic_probability(conn, 1, local(17, 11)) == 0
    ans(conn, 2, True, local(17, 10))
    before = state(conn, 2)
    ans(conn, 2, False, local(17, 12), mode="pretest")
    assert state(conn, 2) == before


def test_due_after_exam_pulled_forward(conn):
    ans(conn, 1, True, local(17, 10), confidence=2, mode="typed")   # Easy: vence en más de dos semanas
    ans(conn, 9, True, local(17, 10), confidence=2, mode="typed")   # tema 2, fuera del examen
    ans(conn, 2, False, local(17, 10))                              # vence ya: no se toca
    due = {q: state(conn, q)["due"] for q in (1, 2, 9)}
    exam_at = local(25, 9)
    assert due[1] > exam_at
    create_manual_exam(conn, 10, "Parcial", exam_at, [1], None)
    # El examen cambió: se rehacen las tarjetas del tema 1. R(examen) ≈ 0,91 < 0,92.
    assert sync_review_state(conn) == 2
    pulled = state(conn, 1)["due"]
    assert pulled == local(21)                # 4 días antes del examen
    assert local_day(exam_at) - timedelta(days=4) <= local_day(pulled) <= local_day(exam_at) - timedelta(days=2)
    assert state(conn, 2)["due"] == due[2] and state(conn, 9)["due"] == due[9]
    # idempotente: sin respuestas ni exámenes nuevos no rehace nada
    assert sync_review_state(conn) == 0


def test_pull_forward_never_before_next_day_and_only_inside_window():
    # Con el examen ya cargado al responder, FSRS usa 0,92 y casi nunca vence después del examen;
    # la regla se prueba sola. Estabilidad 2,3 días (un Good) y vencimiento después del examen.
    exam_at = local(24, 9)
    # la ventana empieza el 20, pero nunca antes del día siguiente al último repaso
    assert _pulled_due(local(25, 10), 2.3, local(21, 10), [exam_at]) == local(22)
    # repasada el 22: el día siguiente (23) ya es la víspera, fuera de la ventana; no se adelanta
    assert _pulled_due(local(26, 8), 2.3, local(22, 8), [exam_at]) == local(26, 8)
    # un examen anterior al último repaso no cuenta
    assert _pulled_due(local(26, 8), 2.3, local(22, 8), [local(20, 9)]) == local(26, 8)


def test_moved_or_deleted_exam_leaves_no_stale_pull(conn):
    ans(conn, 1, True, local(17, 10), confidence=2, mode="typed")
    base = state(conn, 1)["due"]
    exam = create_manual_exam(conn, 10, "Parcial", local(25, 9), [1], None)
    sync_review_state(conn)
    assert state(conn, 1)["due"] == local(21)
    # se mueve después del vencimiento: ya no hace falta adelantar
    update_manual_exam(conn, exam, at=base + 3 * DAY)
    sync_review_state(conn)
    assert state(conn, 1)["due"] == base
    update_manual_exam(conn, exam, at=local(25, 9))
    sync_review_state(conn)
    assert state(conn, 1)["due"] == local(21)
    delete_manual_exam(conn, exam)
    sync_review_state(conn)
    assert state(conn, 1)["due"] == base


def test_full_replay_equals_incremental_with_exam(conn, tmp_path):
    """Registrar respuesta por respuesta (con el examen cargado a mitad de camino) y reproducir
    toda la historia de una vez dejan la misma `review_state`."""
    history = [(1, True, local(10, 10), 2, "typed"), (2, False, local(10, 11), 2, "mcq"),
               (2, True, local(11, 9), 1, "mcq"), (3, True, local(12, 20), 2, "mcq"),
               (2, True, local(13, 9), 2, "mcq"), (9, True, local(14, 10), 2, "typed"),
               (1, True, local(18, 10), 2, "mcq"), (3, False, local(19, 8), 0, "mcq")]
    for i, (qid, ok, at, confidence, mode) in enumerate(history):
        if i == 3:
            create_manual_exam(conn, 10, "Parcial", local(25, 9), [1, 2], None)
        ans(conn, qid, ok, at, confidence=confidence, mode=mode)
    incremental = {r["card_key"]: dict(r) for r in conn.execute("SELECT * FROM review_state")}
    assert any(r["due"] == local(21) for r in incremental.values())   # hay al menos un adelanto
    conn.execute("DELETE FROM review_state")
    conn.execute("DELETE FROM kv WHERE key IN (?, ?)", (WATERMARK_KEY, EXAMS_KEY))
    assert sync_review_state(conn) == 4
    assert {r["card_key"]: dict(r) for r in conn.execute("SELECT * FROM review_state")} == incremental


def _old_database(path):
    """Una base de antes de esta tarea: `answer` sin las columnas nuevas y sin review_state."""
    raw = sqlite3.connect(path)
    raw.executescript(
        "CREATE TABLE topic(id INTEGER PRIMARY KEY, course_id INTEGER, slug TEXT, name TEXT,"
        " UNIQUE(course_id, slug));"
        "CREATE TABLE question(id INTEGER PRIMARY KEY, material_id INTEGER, topic_id INTEGER,"
        " stem TEXT, options_json TEXT, correct_index INTEGER, explanation TEXT, common_error TEXT,"
        " source_seconds INTEGER, check_expr TEXT, verified INTEGER DEFAULT 0,"
        " retired INTEGER DEFAULT 0, dedupe_key TEXT UNIQUE);"
        "CREATE TABLE answer(id INTEGER PRIMARY KEY, question_id INTEGER, chosen_index INTEGER,"
        " correct INTEGER, at INTEGER);"
        "INSERT INTO topic VALUES (1, 10, 'a', 'A');"
        "INSERT INTO question(id, topic_id, stem, options_json, correct_index, verified)"
        " VALUES (1, 1, 'p', '[\"x\",\"y\"]', 0, 1), (2, 1, 'p', '[\"x\",\"y\"]', 0, 1);"
    )
    # en desorden de id a propósito: se reproduce por fecha
    raw.executemany(
        "INSERT INTO answer(question_id, chosen_index, correct, at) VALUES (?, ?, ?, ?)",
        [(1, 0, 1, local(17, 11)), (1, 1, 0, local(17, 10)), (2, 0, 1, local(16, 10)),
         (2, 0, 1, local(18, 10))],
    )
    raw.commit()
    raw.close()


def test_migration_replays_history(tmp_path):
    path = tmp_path / "old.db"
    _old_database(path)
    conn = connect(path)
    assert {"confidence", "latency_ms", "mode", "option_order_json", "typed_text"} <= {
        r[1] for r in conn.execute("PRAGMA table_info(answer)")}
    first = {r["card_key"]: dict(r) for r in conn.execute("SELECT * FROM review_state")}
    assert set(first) == {"q:1", "q:2"}
    # q1: error y después acierto (Again, Good) → en reaprendizaje, 1 día
    assert (first["q:1"]["state"], first["q:1"]["relearn_days"]) == (STATE_RELEARNING, 1)
    # q2: dos aciertos (Good, Good) en días distintos
    assert first["q:2"]["state"] == STATE_REVIEW and first["q:2"]["last_review"] == local(18, 10)
    # lo mismo que registrar esas respuestas hoy, con la nota Good/Again
    fresh = connect(tmp_path / "new.db")
    fresh.execute("INSERT INTO topic VALUES (1, 10, 'a', 'A')")
    fresh.execute("INSERT INTO question(id, topic_id, stem, options_json, correct_index, verified) "
                  "VALUES (1, 1, 'p', '[\"x\",\"y\"]', 0, 1), (2, 1, 'p', '[\"x\",\"y\"]', 0, 1)")
    for qid, chosen, at in ((2, 0, local(16, 10)), (1, 1, local(17, 10)), (1, 0, local(17, 11)),
                            (2, 0, local(18, 10))):
        record_answer(fresh, qid, chosen, at)
    assert {r["card_key"]: dict(r) for r in fresh.execute("SELECT * FROM review_state")} == first
    conn.close()
    # segunda vez sobre la misma base: nada cambia
    again = connect(path)
    assert {r["card_key"]: dict(r) for r in again.execute("SELECT * FROM review_state")} == first
    assert sync_review_state(again) == 0
    assert again.execute("SELECT value FROM kv WHERE key = ?", (WATERMARK_KEY,)).fetchone()[0] == "4"
