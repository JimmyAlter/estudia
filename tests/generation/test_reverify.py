import json
import time

import pytest

from estudia.ai.base import AIFormatError, AIQuotaError
from estudia import config
from estudia.db import connect
from estudia.generation.reverify import reverify_all
from fakes import FakeProvider


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    c.execute("INSERT INTO section(id, course_id, num, name, visible) VALUES (1, 10, 1, 'S1', 1)")
    c.commit()
    return c


def add_material(conn, mid, analysis_model=None):
    conn.execute(
        "INSERT INTO material(id, section_id, cmid, kind, title, ref, status, analysis_model)"
        " VALUES (?, 1, ?, 'youtube', 't', ?, 'done', ?)",
        (mid, 1000 + mid, f"v{mid}", analysis_model),
    )


def add_question(conn, qid, mid, stem, correct=1, retired=0):
    conn.execute(
        "INSERT INTO question(id, material_id, topic_id, stem, options_json, correct_index,"
        " explanation, common_error, source_seconds, verified, retired, dedupe_key)"
        " VALUES (?, ?, 1, ?, ?, ?, 'secreta', 'error', 0, 1, ?, ?)",
        (qid, mid, stem, json.dumps(["1", "2", "3", "4"]), correct, retired, f"k{qid}"),
    )


def retired(conn):
    return {r[0]: r[1] for r in conn.execute("SELECT id, retired FROM question")}


class Scripted(FakeProvider):
    """Responde por material lo que diga `script[primer enunciado]`: lista o excepción."""

    def __init__(self, script):
        super().__init__({}, None)
        self.script = script

    def answer_blind_batch(self, items, exclude_models=()):
        self.blind_batches.append((items, tuple(exclude_models)))
        outcome = self.script[items[0][0]]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_retires_questions_that_fail_and_keeps_the_rest(conn):
    add_material(conn, 1, "gemini-3.5-flash")
    add_question(conn, 1, 1, "A", correct=1)
    add_question(conn, 2, 1, "B", correct=2)
    add_question(conn, 3, 1, "C", correct=0)
    add_question(conn, 4, 1, "Ya retirada", retired=1)
    conn.commit()
    prov = Scripted({"A": [1, 0, None]})
    result = reverify_all(conn, prov)
    assert (result.materials_checked, result.kept, result.retired) == (1, 1, 2)
    assert retired(conn) == {1: 0, 2: 1, 3: 1, 4: 1}
    items, exclude = prov.blind_batches[0]
    assert [stem for stem, _ in items] == ["A", "B", "C"]  # la retirada no se manda
    assert "secreta" not in repr(items)
    assert exclude == ("gemini-3.5-flash",)


def test_wrong_length_retires_the_whole_material(conn):
    add_material(conn, 1, "m")
    add_question(conn, 1, 1, "A")
    add_question(conn, 2, 1, "B")
    conn.commit()
    result = reverify_all(conn, Scripted({"A": [1]}))
    assert result.retired == 2
    assert retired(conn) == {1: 1, 2: 1}


def test_unknown_analysis_model_excludes_the_whole_analysis_chain(conn, monkeypatch):
    monkeypatch.setitem(config.MODEL_CHAINS, "analysis", [*config.MODEL_CHAINS["analysis"], "gemini-3.6-flash"])
    add_material(conn, 1, None)
    add_question(conn, 1, 1, "A")
    conn.commit()
    prov = Scripted({"A": [1]})
    reverify_all(conn, prov)
    assert set(prov.blind_batches[0][1]) == set(config.MODEL_CHAINS["analysis"])
    assert "gemini-3.6-flash" in prov.blind_batches[0][1]


def test_quota_stops_without_retiring_and_the_next_run_resumes(conn):
    for mid, stem in ((1, "A"), (2, "B"), (3, "C")):
        add_material(conn, mid, "m")
        add_question(conn, mid, mid, stem)
    conn.commit()
    script = {"A": [1], "B": AIQuotaError(), "C": [0]}
    result = reverify_all(conn, Scripted(script))
    assert (result.materials_checked, result.materials_left, result.stop_reason) == (1, 2, "quota")
    assert retired(conn) == {1: 0, 2: 0, 3: 0}

    script["B"] = [1]
    prov = Scripted(script)
    result = reverify_all(conn, prov)
    assert [items[0][0] for items, _ in prov.blind_batches] == ["B", "C"]  # "A" ya estaba
    assert (result.materials_checked, result.kept, result.retired) == (2, 1, 1)
    assert retired(conn) == {1: 0, 2: 0, 3: 1}


def test_unreadable_material_is_left_for_later_without_retiring(conn):
    add_material(conn, 1, "m")
    add_question(conn, 1, 1, "A")
    conn.commit()
    result = reverify_all(conn, Scripted({"A": AIFormatError("ilegible")}))
    assert (result.materials_checked, result.materials_left) == (0, 1)
    assert retired(conn) == {1: 0}


def test_exhaustion_marks_are_loaded_and_saved(conn):
    add_material(conn, 1, "m")
    add_question(conn, 1, 1, "A")
    conn.execute("INSERT INTO kv(key, value) VALUES ('model_exhausted:fake:b', ?)",
                 (str(int(time.time() + 3600)),))
    conn.commit()
    seen = []

    class Prov(Scripted):
        def answer_blind_batch(self, items, exclude_models=()):
            seen.append(dict(self.exhausted_until))
            self.exhausted_until["c"] = time.time() + 600
            raise AIQuotaError()

    reverify_all(conn, Prov({}))
    assert list(seen[0]) == ["b"]
    assert conn.execute("SELECT COUNT(*) FROM kv WHERE key='model_exhausted:fake:c'").fetchone()[0] == 1


def test_requests_are_added_to_the_daily_usage(conn):
    """Los pedidos de la reverificación cuentan para la cuota del día, también si se corta."""
    from estudia.ai.quota import used_today
    for mid, stem in ((1, "A"), (2, "B"), (3, "C")):
        add_material(conn, mid, "m")
        add_question(conn, mid, mid, stem)
    conn.commit()

    class Counting(Scripted):
        def answer_blind_batch(self, items, exclude_models=()):
            self.requests["blind"] += 1
            return super().answer_blind_batch(items, exclude_models)

    prov = Counting({"A": [1], "B": [1], "C": AIQuotaError()})
    reverify_all(conn, prov)
    assert used_today(conn, "blind", time.time()) == 3
    assert prov.requests == {"analysis": 0, "blind": 0, "pdf": 0}

    boom = Counting({"A": RuntimeError("se rompió")})
    conn.execute("DELETE FROM kv WHERE key LIKE 'reverified:%'")
    conn.commit()
    with pytest.raises(RuntimeError):
        reverify_all(conn, boom)
    assert used_today(conn, "blind", time.time()) == 4
