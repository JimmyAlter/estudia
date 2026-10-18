"""Una tarjeta FSRS y un dominio por grupo de variantes (`v:<original>`); sin grupo, `q:<id>` como siempre."""
import importlib.metadata
import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from estudia.db import ADDED_COLUMNS, DOWNLOAD_MIGRATED_KEY, SCHEMA, connect
from estudia.study.exams import create_manual_exam
from estudia.study.home import home_view, ready_course
from estudia.study.mastery import topic_probability
from estudia.study.planner import _due_before
from estudia.study.practice import build_quiz, record_answer, record_explain_match
from estudia.study.pretest import pretest_offer
from estudia.study.srs import (
    EXAMS_KEY,
    STATE_RELEARNING,
    STATE_REVIEW,
    WATERMARK_KEY,
    CardRef,
    _replay,
    card_ref,
    day_start,
    exam_schedule_by_topic,
    group_card_key,
    local_day,
    rebuild_card,
    sync_review_state,
)
from test_app import SITE_ID, env, logged_in  # noqa: F401 - `env` es el fixture

TZ = ZoneInfo("America/Argentina/Buenos_Aires")
DAY = 86400
SRC = Path(__file__).resolve().parents[2] / "src" / "estudia"


def local(day: int, hour: int = 0, minute: int = 0) -> int:
    """Epoch de una hora local de Buenos Aires en octubre de 2026."""
    return int(datetime(2026, 10, day, hour, minute, tzinfo=TZ).timestamp())


NOW = local(17, 15)


def seed(conn):
    conn.execute("INSERT INTO course(id, site_id, fullname, shortname) VALUES (10, 's', 'Análisis', 'AM')")
    conn.execute("INSERT INTO section(id, course_id, num, name, visible) VALUES (1, 10, 1, 'S1', 1)")
    conn.execute("INSERT INTO material(id, section_id, cmid, kind, title, ref, status, updated_at) "
                 "VALUES (1, 1, 1, 'youtube', 'Clase 1', 'abcdefghijk', 'done', ?)", (NOW,))
    conn.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (1, 10, 'a', 'Tema A')")
    conn.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (2, 10, 'b', 'Tema B')")
    conn.execute("INSERT INTO material_topic(material_id, topic_id) VALUES (1, 1), (1, 2)")
    conn.commit()


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    seed(c)
    return c


def add_q(conn, qid, topic=1, group=None):
    conn.execute(
        "INSERT INTO question(id, material_id, topic_id, stem, options_json, correct_index, explanation, "
        "verified, retired, dedupe_key, variant_group) VALUES (?, 1, ?, ?, '[\"x\",\"y\"]', 0, 'e', 1, 0, ?, ?)",
        (qid, topic, f"p{qid}", f"k{qid}", group))
    conn.commit()


def add_group(conn, head, members, topic=1):
    """Original `head` y sus variantes `members`: todas con `variant_group = head`."""
    for qid in (head, *members):
        add_q(conn, qid, topic, group=head)


def rows(conn) -> dict[str, dict]:
    return {r["card_key"]: dict(r) for r in conn.execute("SELECT * FROM review_state")}


def without_key(row: dict) -> dict:
    return {k: v for k, v in row.items() if k != "card_key"}


def answers_of(conn, ids):
    return conn.execute(
        f"SELECT correct, confidence, mode, explain_match, at FROM answer WHERE question_id IN "
        f"({','.join('?' * len(ids))}) AND COALESCE(mode, '') != 'pretest' ORDER BY at, id", ids).fetchall()


# --- La tarjeta del grupo ----------------------------------------------------------------------

def test_card_ref(conn):
    add_q(conn, 7)
    add_group(conn, 1, [3, 2])
    assert card_ref(conn, 7) == CardRef("q:7", 7, [7], 1)
    assert card_ref(conn, 3) == CardRef("v:1", 1, [1, 2, 3], 1)
    assert card_ref(conn, 1) == card_ref(conn, 2) == card_ref(conn, 3)
    assert card_ref(conn, 99) is None
    assert card_ref(conn, 3).grouped and not card_ref(conn, 7).grouped
    assert group_card_key(1) == "v:1"


def test_group_card_replays_answers_of_all_members(conn):
    add_group(conn, 1, [2, 3])
    record_answer(conn, 1, 0, local(10, 10))
    record_answer(conn, 2, 1, local(11, 10))
    state = rows(conn)
    assert set(state) == {"v:1"}
    assert state["v:1"]["last_review"] == local(11, 10)
    assert state["v:1"] == _replay("v:1", 1, answers_of(conn, [1, 2]), [])
    # Rehacer desde cualquier miembro da la misma fila.
    rebuild_card(conn, 3)
    assert rows(conn) == state


def test_group_card_matches_a_single_question_with_the_same_history(conn):
    """Reaprendizaje, "Explicá por qué", simulacro, pretest y recuperatorio: la tarjeta del grupo es
    la de una pregunta suelta que recibió las mismas respuestas."""
    add_group(conn, 1, [2, 3])
    add_q(conn, 9)
    create_manual_exam(conn, 10, "Recuperatorio", local(28, 9), [1], None, kind="recuperatorio", now=local(1))
    history = [  # (miembro, correcta, momento, confianza, modo)
        (1, True, local(2, 10), 2, "mcq"),
        (2, False, local(3, 10), 2, "mcq"),
        (3, True, local(4, 10), 1, "mcq"),
        (1, True, local(5, 10), 2, "pretest"),
        (2, True, local(6, 10), 2, "mcq"),
        (3, False, local(7, 10), 2, "mock"),
        (1, True, local(8, 10), 1, "typed_then_mcq"),
    ]
    for member, ok, at, confidence, mode in history:
        for qid in (member, 9):
            record_answer(conn, qid, 0 if ok else 1, at, confidence=confidence, mode=mode)
    conn.execute("UPDATE answer SET explain_prompted = 1, explain_match = 0 WHERE at = ?", (local(4, 10),))
    rebuild_card(conn, 2)
    rebuild_card(conn, 9)
    state = rows(conn)
    assert set(state) == {"v:1", "q:9"}
    assert without_key(state["v:1"]) == without_key(state["q:9"])


def test_variants_created_after_answers_inherit_history(tmp_path):
    """Una original ya contestada que después pasa a tener variantes: la tarjeta del grupo sigue su
    historia, la vieja `q:` se va y ninguna variante aparece como nueva."""
    path = tmp_path / "t.db"
    conn = connect(path)
    seed(conn)
    add_q(conn, 1)
    record_answer(conn, 1, 1, local(10, 10))   # error: reaprendizaje
    record_answer(conn, 1, 0, local(11, 10))   # 1.er día
    before = rows(conn)["q:1"]
    assert (before["state"], before["relearn_days"]) == (STATE_RELEARNING, 1)
    # La original pasa a ser cabeza de un grupo (sin respuestas nuevas: la marca no se mueve).
    conn.execute("UPDATE question SET variant_group = 1 WHERE id = 1")
    add_q(conn, 2, group=1)
    add_q(conn, 3, group=1)
    conn.close()

    conn = connect(path)   # al abrir, `sync_review_state` ya la pasa al grupo
    state = rows(conn)
    assert set(state) == {"v:1"}
    assert without_key(state["v:1"]) == without_key(before)
    assert sync_review_state(conn) == 0
    # Las variantes no son nuevas: salen con el reaprendizaje de la original (día 2).
    (item,) = build_quiz(conn, 10, topic_id=1, now=local(12, 10))
    assert item.card_key == "v:1" and item.question_id in (2, 3)   # otra variante, no la original
    assert item.relearning == {"day": 2}
    # Y contestar una variante sigue la cuenta: 2.º día de reaprendizaje.
    fb = record_answer(conn, 2, 0, local(12, 10))
    assert fb.relearn_progress == "2 de 3 días"
    assert rows(conn)["v:1"]["relearn_days"] == 2


def test_stale_q_row_of_a_member_is_dropped_by_sync(conn):
    add_q(conn, 1)
    record_answer(conn, 1, 0, local(10, 10))
    conn.execute("UPDATE question SET variant_group = 1 WHERE id = 1")
    add_q(conn, 2, group=1)
    assert sync_review_state(conn) == 1
    assert set(rows(conn)) == {"v:1"}
    assert sync_review_state(conn) == 0


# --- Lo que tiene que seguir andando con la tarjeta del grupo ------------------------------------

def test_pretest_answers_never_touch_group_card(conn):
    add_group(conn, 1, [2])
    record_answer(conn, 2, 0, local(10, 10), mode="pretest")
    assert rows(conn) == {}
    record_answer(conn, 1, 0, local(11, 10))
    record_answer(conn, 2, 1, local(12, 10), mode="pretest")
    assert rows(conn)["v:1"]["last_review"] == local(11, 10)


def test_explain_no_on_a_member_caps_group_card(conn):
    add_group(conn, 1, [2])
    fb = record_answer(conn, 2, None, local(10, 10), confidence=2, mode="typed", typed_text="x", typed_ok=True)
    conn.execute("UPDATE answer SET explain_prompted = 1 WHERE id = ?", (fb.answer_id,))
    conn.commit()
    easy = rows(conn)["v:1"]
    record_explain_match(conn, fb.answer_id, same=False)
    capped = rows(conn)["v:1"]
    assert set(rows(conn)) == {"v:1"}
    assert capped["stability"] < easy["stability"]
    assert capped["due"] - local(10, 10) <= DAY + 3600   # vuelve pronto
    assert (capped["state"], capped["relearn_days"]) == (STATE_REVIEW, 0)   # pero no es un error


def test_retake_target_applies_to_group_card(conn):
    add_group(conn, 1, [2])
    exam = create_manual_exam(conn, 10, "Parcial", NOW + 30 * DAY, [1], None, now=NOW)
    record_answer(conn, 2, 0, NOW)
    due_parcial = rows(conn)["v:1"]["due"]
    conn.execute("UPDATE exam SET kind = 'recuperatorio' WHERE id = ?", (exam,))
    assert exam_schedule_by_topic(conn) == {1: [(NOW + 30 * DAY, True)]}
    assert sync_review_state(conn) == 1   # el grupo se rehace una vez
    assert set(rows(conn)) == {"v:1"}
    assert rows(conn)["v:1"]["due"] < due_parcial


def test_relearning_counts_distinct_days_across_members(conn):
    add_group(conn, 1, [2, 3])
    record_answer(conn, 1, 1, local(10, 10))                 # error
    record_answer(conn, 2, 0, local(11, 10))                 # día 1
    record_answer(conn, 3, 0, local(11, 18))                 # mismo día: sigue en 1
    assert rows(conn)["v:1"]["relearn_days"] == 1
    fb = record_answer(conn, 3, 0, local(12, 10))            # día 2
    assert fb.relearn_progress == "2 de 3 días"
    assert rows(conn)["v:1"]["due"] == day_start(local_day(local(12, 10)) + timedelta(days=2))
    record_answer(conn, 1, 0, local(14, 10))                 # día 3: se gradúa
    assert rows(conn)["v:1"]["state"] == STATE_REVIEW


def test_wrong_member_requeues_with_group_card(conn):
    add_group(conn, 1, [2])
    fb = record_answer(conn, 2, 1, local(10, 10))
    assert fb.requeue is True
    assert fb.requeue_item is not None and fb.requeue_item.question_id == 1
    # Vuelve otra variante: se dice que trae otros números.
    assert fb.next_label == "Vuelve al final de la sesión, con otros números"
    assert fb.relearn_progress == "0 de 3 días"


def test_mock_error_on_a_member_is_due_tomorrow(conn):
    add_group(conn, 1, [2])
    record_answer(conn, 1, 0, local(9, 10))
    record_answer(conn, 2, 1, local(10, 22), mode="mock")
    card = rows(conn)["v:1"]
    assert card["state"] == STATE_RELEARNING
    assert card["due"] == local(11)


# --- Bases de antes de la etapa 3c -------------------------------------------------------------

_3C_COLUMNS = {("question", "variant_group"), ("question", "variant_values_json")}

# `review_state` que dejó el código de antes de esta tarea (commit 24101b1) en `_pre3c_database`.
# Generado una vez con ese `srs.py`; los float se guardan con su `repr` (exactos).
PRE3C_REVIEW_STATE = {
    "q:1": {"card_key": "q:1", "stability": 0.6368506992409603, "difficulty": 7.394502741279718,
            "state": 2, "due": 1791378000, "last_review": 1791291600, "relearn_days": 0,
            "relearn_last_day": None},
    "q:2": {"card_key": "q:2", "stability": 4.715158058516964, "difficulty": 8.386575510565644,
            "state": 2, "due": 1791896400, "last_review": 1791637200, "relearn_days": 3,
            "relearn_last_day": "2026-10-10"},
    "q:3": {"card_key": "q:3", "stability": 8.2956, "difficulty": 1.0,
            "state": 2, "due": 1792587600, "last_review": 1791205200, "relearn_days": 0,
            "relearn_last_day": None},
    "q:4": {"card_key": "q:4", "stability": 0.212, "difficulty": 6.4133,
            "state": 3, "due": 1791514800, "last_review": 1791464400, "relearn_days": 0,
            "relearn_last_day": None},
}
PRE3C_KV = {WATERMARK_KEY: "9", EXAMS_KEY: json.dumps({"1": [[local(20, 9), True]]})}


def _pre3c_database(path):
    """Una base de la etapa 3b: todas las columnas de antes, sin las de variantes, con 4 preguntas,
    9 respuestas en días distintos y un recuperatorio; con la `review_state` y las marcas que dejó
    el código de antes."""
    raw = sqlite3.connect(path)
    raw.executescript(SCHEMA)
    for table, column, decl in ADDED_COLUMNS:
        existing = {r[1] for r in raw.execute(f"PRAGMA table_info({table})")}
        if (table, column) not in _3C_COLUMNS and column not in existing:
            raw.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    assert "variant_group" not in {r[1] for r in raw.execute("PRAGMA table_info(question)")}
    raw.execute("INSERT INTO course(id, site_id, fullname, shortname) VALUES (10, 's', 'Análisis', 'AM')")
    raw.executemany("INSERT INTO topic(id, course_id, slug, name) VALUES (?, 10, ?, ?)",
                    [(1, "a", "Tema A"), (2, "b", "Tema B")])
    for qid, topic in ((1, 1), (2, 1), (3, 2), (4, 2)):
        raw.execute(
            "INSERT INTO question(id, topic_id, stem, options_json, correct_index, verified, check_expr,"
            " dedupe_key) VALUES (?, ?, ?, '[\"x\",\"y\"]', 0, 1, '1', ?)",
            (qid, topic, f"p{qid}", f"k{qid}"))
    raw.execute("INSERT INTO exam(id, course_id, name, opens_at, source, topics_json, kind) "
                "VALUES (1, 10, 'Recuperatorio', ?, 'manual', ?, 'recuperatorio')",
                (local(20, 9), json.dumps([1])))
    # (pregunta, correcta, momento, confianza, modo, explain_prompted, explain_match)
    raw.executemany(
        "INSERT INTO answer(question_id, chosen_index, correct, at, confidence, mode, explain_prompted,"
        " explain_match) VALUES (?, 0, ?, ?, ?, ?, ?, ?)",
        [(1, 1, local(3, 10), 2, "mcq", 0, None),
         (2, 0, local(3, 11), 2, "mcq", 0, None),
         (2, 1, local(4, 10), 1, "mcq", 1, None),
         (3, 1, local(5, 10), 2, "typed", 0, None),
         (1, 1, local(6, 10), 1, "mcq", 1, 0),            # "Explicá por qué": no dijo lo mismo
         (2, 1, local(7, 10), 2, "mcq", 0, None),
         (4, 0, local(8, 10), 2, "mock", 0, None),         # error en el simulacro
         (3, 1, local(9, 10), None, "pretest", 0, None),   # pretest: no cuenta
         (2, 1, local(10, 10), 2, "typed_then_mcq", 0, None)])
    cols = list(PRE3C_REVIEW_STATE["q:1"])
    raw.executemany(f"INSERT INTO review_state({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                    [tuple(r[c] for c in cols) for r in PRE3C_REVIEW_STATE.values()])
    raw.executemany("INSERT INTO kv(key, value) VALUES (?, ?)", PRE3C_KV.items())
    # Una instalación de la 3b ya pasó por la migración de la descarga: abrirla no escribe eso.
    raw.execute("INSERT INTO kv(key, value) VALUES (?, '1')", (DOWNLOAD_MIGRATED_KEY,))
    raw.commit()
    raw.close()


# Los valores de PRE3C_REVIEW_STATE salen de esta versión de fsrs (fijada en pyproject.toml).
FSRS_VERSION = "6.3.2"


def test_existing_q_cards_survive_migration_unchanged(tmp_path):
    assert importlib.metadata.version("fsrs") == FSRS_VERSION, (
        "Cambió la versión de fsrs: PRE3C_REVIEW_STATE ya no prueba nada. Regenerarlo con el srs.py "
        "de antes de la etapa 3c (commit 24101b1) y la fsrs nueva: armar la base de `_pre3c_database` "
        "sin las filas de review_state y kv, correr ese `sync_review_state` y copiar las filas "
        "(los float con su repr); después actualizar FSRS_VERSION y el pin de pyproject.toml.")
    path = tmp_path / "vieja.db"
    _pre3c_database(path)
    conn = connect(path)   # migra: suma las columnas de variantes y sincroniza
    # Migrar y sincronizar no escribe ni una fila: las columnas nuevas son DDL y no hay nada que rehacer.
    assert conn.total_changes == 0
    assert rows(conn) == PRE3C_REVIEW_STATE
    assert sync_review_state(conn) == 0
    assert conn.total_changes == 0
    for qid in (1, 2, 3, 4):
        rebuild_card(conn, qid)
    assert rows(conn) == PRE3C_REVIEW_STATE
    # Reproducir toda la historia desde cero con el código nuevo da lo mismo, byte a byte.
    conn.execute("DELETE FROM review_state")
    conn.execute("DELETE FROM kv WHERE key IN (?, ?)", (WATERMARK_KEY, EXAMS_KEY))
    assert sync_review_state(conn) == 4
    assert rows(conn) == PRE3C_REVIEW_STATE
    assert {r[0]: r[1] for r in conn.execute("SELECT key, value FROM kv WHERE key IN (?, ?)",
                                             (WATERMARK_KEY, EXAMS_KEY))} == PRE3C_KV
    assert not conn.execute("SELECT 1 FROM review_state WHERE card_key LIKE 'v:%'").fetchone()
    conn.close()


# --- Conteos y dominio por grupo ---------------------------------------------------------------

def test_probability_counts_group_once(conn):
    add_group(conn, 1, [2, 3, 4, 5], topic=1)
    add_q(conn, 6, topic=1)
    add_q(conn, 7, topic=2)
    add_q(conn, 8, topic=2)
    for a, b, at in ((3, 7, local(10, 10)), (6, 8, local(11, 10))):
        record_answer(conn, a, 0, at)
        record_answer(conn, b, 0, at)
    p1 = topic_probability(conn, 1, NOW)
    assert p1 > 0
    assert p1 == topic_probability(conn, 2, NOW)


def test_counts_are_per_group(conn):
    add_group(conn, 1, [2, 3])
    add_q(conn, 4)
    record_answer(conn, 2, 1, local(10, 10))
    topic = next(t for t in home_view(conn, NOW).topics if t.id == 1)
    assert (topic.seen, topic.total) == (1, 2)
    assert _due_before(conn, [1], NOW + 365 * DAY) == {1: 1}
    # "Ya podés practicar": 10 tarjetas, no 10 filas.
    add_group(conn, 10, [11, 12, 13, 14])
    for qid in range(20, 26):
        add_q(conn, qid)
    assert ready_course(conn, NOW) is None    # 2 + 1 + 6 = 9 tarjetas (15 filas)
    add_q(conn, 26)
    assert ready_course(conn, NOW).questions == 10


def test_pretest_offer_counts_group_once(conn):
    add_group(conn, 1, [2, 3])
    add_q(conn, 4)
    assert pretest_offer(conn, NOW) is None   # 2 tarjetas: no alcanzan para un pretest de 3
    add_q(conn, 5)
    assert pretest_offer(conn, NOW).material_id == 1


def test_materials_count_group_once(env):
    c = logged_in(env)
    conn = connect(env["db_path"])
    seed(conn)
    conn.execute("UPDATE course SET site_id = ?", (SITE_ID,))
    add_group(conn, 1, [2, 3])
    add_q(conn, 4)
    conn.close()
    material = c.get("/api/materials").json()[0]["materials"][0]
    assert material["questions"] == 2


def test_no_raw_card_key_joins():
    offenders = [str(p.relative_to(SRC)) for p in SRC.rglob("*.py")
                 if re.search(r"'q:'\s*\|\|", p.read_text("utf-8"))]
    assert offenders == ["study/srs.py"]
