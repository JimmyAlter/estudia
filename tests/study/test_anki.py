import json
import sqlite3
import zipfile
from io import BytesIO

import pytest

from estudia.db import connect
from estudia.study.anki import build_apkg, course_slug


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    c.execute("INSERT INTO course(id, site_id, fullname, shortname) VALUES (10, 's', 'Álgebra', 'ALG')")
    c.execute("INSERT INTO course(id, site_id, fullname, shortname) VALUES (20, 's', 'Física', 'FIS')")
    c.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (1, 10, 'r', 'Rectas')")
    c.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (2, 20, 'c', 'Cinemática')")
    c.execute("INSERT INTO section(id, course_id, num, name, visible) VALUES (1, 10, 1, 'S1', 1)")
    c.execute("INSERT INTO material(id, section_id, cmid, kind, title, ref, status) "
              "VALUES (1, 1, 1, 'youtube', 'Video 1', 'abcdefghijk', 'done')")
    c.commit()
    return c


def add_q(conn, qid, topic=1, verified=1, retired=0, stem="¿Pendiente?", seconds=125):
    conn.execute(
        "INSERT INTO question(id, material_id, topic_id, stem, options_json, correct_index, explanation,"
        " common_error, source_seconds, verified, retired, dedupe_key) VALUES (?, 1, ?, ?, ?, 2, 'Porque sí',"
        " 'Confundir m con b', ?, ?, ?, ?)",
        (qid, topic, stem, json.dumps(["1", "2", "3", "4"]), seconds, verified, retired, f"k{qid}"),
    )
    conn.commit()


def open_collection(data: bytes, tmp_path):
    with zipfile.ZipFile(BytesIO(data)) as z:
        assert "collection.anki2" in z.namelist()
        z.extract("collection.anki2", tmp_path)
    return sqlite3.connect(tmp_path / "collection.anki2")


def test_apkg_is_valid_zip_with_collection(conn, tmp_path):
    add_q(conn, 1)
    col = open_collection(build_apkg(conn, None), tmp_path)
    flds = col.execute("SELECT flds FROM notes").fetchone()[0].split("\x1f")
    assert flds[0] == "¿Pendiente?"
    assert "<b>C. 3</b>" in flds[2]
    assert "Clase: Video 1, minuto 02:05" in flds[5]
    assert "https://youtu.be/abcdefghijk?t=125" in flds[5]
    assert flds[6:] == ["Rectas", "Álgebra"]


def test_only_verified_unretired(conn, tmp_path):
    add_q(conn, 1)
    add_q(conn, 2, verified=0)
    add_q(conn, 3, retired=1)
    add_q(conn, 4, topic=2)
    col = open_collection(build_apkg(conn, None), tmp_path)
    assert col.execute("SELECT count(*) FROM notes").fetchone()[0] == 2
    only = tmp_path / "solo"
    only.mkdir()
    col2 = open_collection(build_apkg(conn, 10), only)
    assert col2.execute("SELECT count(*) FROM notes").fetchone()[0] == 1


def test_no_questions_raises(conn):
    add_q(conn, 1, verified=0)
    with pytest.raises(LookupError):
        build_apkg(conn, None)
    add_q(conn, 2)
    with pytest.raises(LookupError):
        build_apkg(conn, 20)


def test_stable_ids(conn, tmp_path):
    add_q(conn, 1)
    add_q(conn, 4, topic=2)
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    ca = open_collection(build_apkg(conn, None), a)
    cb = open_collection(build_apkg(conn, None), b)
    assert ca.execute("SELECT guid FROM notes ORDER BY guid").fetchall() == \
        cb.execute("SELECT guid FROM notes ORDER BY guid").fetchall()
    assert ca.execute("SELECT mid FROM notes").fetchone() == cb.execute("SELECT mid FROM notes").fetchone()
    decks = lambda c: sorted(json.loads(c.execute("SELECT decks FROM col").fetchone()[0]))
    assert decks(ca) == decks(cb)
    assert len(decks(ca)) == 3  # los dos cursos y el mazo por defecto


def test_html_is_escaped(conn, tmp_path):
    add_q(conn, 1, stem="¿Qué hace <script>alert(1)</script> & más?")
    conn.execute("UPDATE material SET title = '<b>Clase</b>', ref = 'bad ref'")
    conn.commit()
    col = open_collection(build_apkg(conn, None), tmp_path)
    flds = col.execute("SELECT flds FROM notes").fetchone()[0].split("\x1f")
    assert "<script>" not in flds[0] and "&lt;script&gt;" in flds[0] and "&amp;" in flds[0]
    assert "<b>Clase</b>" not in flds[5] and "&lt;b&gt;" in flds[5]
    assert "youtu.be" not in flds[5]


def test_course_slug():
    assert course_slug("Álgebra y Cálculo I") == "algebra-y-calculo-i"
    assert course_slug("???") == "mazo"


@pytest.mark.parametrize("fullname, deck", [
    ("Álgebra::Parcial", "Álgebra:Parcial"),       # "::" haría un submazo en Anki
    ("A:::B", "A:B"),
    ("Física: ondas", "Física: ondas"),
])
def test_deck_name_never_nests(conn, tmp_path, fullname, deck):
    conn.execute("UPDATE course SET fullname = ? WHERE id = 10", (fullname,))
    add_q(conn, 1)
    col = open_collection(build_apkg(conn, 10), tmp_path)
    decks = json.loads(col.execute("SELECT decks FROM col").fetchone()[0])
    names = {d["name"] for d in decks.values()}
    assert deck in names
    assert not any("::" in n for n in names)


# --- Mazo de la semana ---------------------------------------------------------------------------

from estudia.study.anki import WEEK_DAYS, weekly_question_ids  # noqa: E402

NOW = 1_800_000_000
DAY = 86400


def set_due(conn, qid, due):
    conn.execute("INSERT INTO review_state(card_key, due) VALUES (?, ?)", (f"q:{qid}", due))
    conn.commit()


def add_exam(conn, at, topics, kind="parcial"):
    conn.execute("INSERT INTO exam(course_id, name, opens_at, source, topics_json, kind) "
                 "VALUES (10, 'Parcial', ?, 'manual', ?, ?)", (at, json.dumps(topics), kind))
    conn.commit()


def test_weekly_deck_due_in_7_days_plus_next_exam_topics(conn):
    assert WEEK_DAYS == 7
    add_q(conn, 1)                      # vence en 3 días
    add_q(conn, 2)                      # sin review_state, tema fuera del examen
    add_q(conn, 3, topic=2)             # tema del examen, sin review_state
    set_due(conn, 1, NOW + 3 * DAY)
    add_exam(conn, NOW + 20 * DAY, [2])
    assert weekly_question_ids(conn, NOW) == {1, 3}


def test_weekly_deck_excludes_retired_and_far_due(conn):
    add_q(conn, 1)
    add_q(conn, 2, retired=1)
    add_q(conn, 3, verified=0)
    add_q(conn, 4)
    add_q(conn, 5)
    set_due(conn, 1, NOW + 7 * DAY - 1)
    set_due(conn, 2, NOW)
    set_due(conn, 3, NOW)
    set_due(conn, 4, NOW + 7 * DAY)     # justo en el borde: no entra
    set_due(conn, 5, NOW + 30 * DAY)
    assert weekly_question_ids(conn, NOW) == {1}


def test_weekly_deck_ignores_enrollment_as_next_exam(conn):
    add_q(conn, 1)
    add_exam(conn, NOW + 2 * DAY, [1], kind="inscripcion")
    assert weekly_question_ids(conn, NOW) == set()


def test_weekly_deck_no_duplicates(conn, tmp_path):
    add_q(conn, 1)
    set_due(conn, 1, NOW + DAY)         # vence y además es del examen
    add_exam(conn, NOW + 3 * DAY, [1])
    ids = weekly_question_ids(conn, NOW)
    assert ids == {1}
    col = open_collection(build_apkg(conn, None, ids), tmp_path)
    assert col.execute("SELECT count(*) FROM notes").fetchone()[0] == 1


def test_weekly_deck_same_guids_as_full_deck(conn, tmp_path):
    for i in (1, 2, 3):
        add_q(conn, i)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    full = open_collection(build_apkg(conn, None), tmp_path / "a")
    weekly = open_collection(build_apkg(conn, None, {2}), tmp_path / "b")
    full_notes = {r[0]: r[1] for r in full.execute("SELECT guid, flds FROM notes")}
    ((guid, flds),) = weekly.execute("SELECT guid, flds FROM notes").fetchall()
    assert full_notes[guid] == flds           # mismo guid: reimportar no duplica
    def deck_ids(col):
        return set(json.loads(col.execute("SELECT decks FROM col").fetchone()[0])) - {"1"}
    assert deck_ids(full) == deck_ids(weekly) and len(deck_ids(weekly)) == 1


def test_weekly_deck_empty_raises(conn):
    add_q(conn, 1)
    assert weekly_question_ids(conn, NOW) == set()
    with pytest.raises(LookupError):
        build_apkg(conn, None, set())
