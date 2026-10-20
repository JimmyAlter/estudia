"""Pasada a ciegas en lote: hasta 4 clases por pedido, nunca con un modelo que analizó
alguna de ellas, y ningún error saltea ni afloja la verificación."""
import time

import pytest

from estudia.ai.base import (
    AIAuthError,
    AIFormatError,
    AIQuotaError,
    AIUnavailableError,
    GenQuestion,
    VideoAnalysis,
)
from estudia.ai.quota import used_today
from estudia.config import LEGACY_ANALYSIS_MODEL
from estudia.db import connect
from estudia.generation.pipeline import (
    FAIL_FORMAT,
    paid_batches,
    process_pending,
    verify_batch,
)
from fakes import FakeProvider


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    c.execute("INSERT INTO section(id, course_id, num, name, visible) VALUES (1, 10, 1, 'S1', 1)")
    c.commit()
    return c


def add_material(conn, mid, ref=None, analysis_json=None, analysis_model=None):
    conn.execute(
        "INSERT INTO material(id, section_id, cmid, kind, title, ref, status, analysis_json,"
        " analysis_model) VALUES (?, 1, ?, 'youtube', ?, ?, 'pending', ?, ?)",
        (mid, 1000 + mid, f"Video {mid}", ref or f"v{mid}", analysis_json, analysis_model),
    )
    conn.commit()


def q(stem, correct=1):
    return GenQuestion(stem=stem, options=["1", "2", "3", "4"], correct_index=correct,
                       explanation="porque sí", common_error="error común",
                       topic="Funciones lineales", source_seconds=5)


def analysis(questions):
    return VideoAnalysis(summary_md="# Resumen", topics=["Funciones lineales"], questions=questions)


def analyses_for(ids):
    return {f"v{i}": analysis([q(f"pregunta {i}a"), q(f"pregunta {i}b")]) for i in ids}


def statuses(conn):
    return {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM material")}


def stop_reason(conn):
    row = conn.execute("SELECT value FROM kv WHERE key='ai_stop_reason'").fetchone()
    return row[0] if row else None


def always_right(stem, options):
    return 1


def test_batch_of_four_classes_uses_one_blind_request(conn):
    for i in range(1, 5):
        add_material(conn, i)
    fake = FakeProvider(analyses_for(range(1, 5)), always_right)
    assert process_pending(conn, fake) == 4
    assert len(fake.blind_batches) == 1
    items, exclude = fake.blind_batches[0]
    assert len(items) == 8
    assert exclude == ("analizador",)
    # Los pedidos del proveedor pasan al uso del día de la cuota.
    assert used_today(conn, "blind", time.time()) == 1
    assert set(statuses(conn).values()) == {"done"}
    assert conn.execute("SELECT COUNT(*) FROM question WHERE verified=1").fetchone()[0] == 8


def test_fifth_class_goes_to_second_batch(conn):
    for i in range(1, 6):
        add_material(conn, i)
    fake = FakeProvider(analyses_for(range(1, 6)), always_right)
    assert process_pending(conn, fake) == 5
    assert [len(items) for items, _ in fake.blind_batches] == [8, 2]
    assert fake.blind_batches[1][0][0][0] == "pregunta 5a"


def test_batch_groups_by_analysis_model_and_excludes_it(conn):
    for i in range(1, 5):
        add_material(conn, i)
    fake = FakeProvider(analyses_for(range(1, 5)), always_right,
                        models_by_ref={"v1": "A", "v2": "B", "v3": "A", "v4": "B"})
    assert process_pending(conn, fake) == 4
    assert len(fake.blind_batches) == 2
    (items_a, exclude_a), (items_b, exclude_b) = fake.blind_batches
    assert exclude_a == ("A",) and exclude_b == ("B",)
    assert [s for s, _ in items_a] == ["pregunta 1a", "pregunta 1b", "pregunta 3a", "pregunta 3b"]
    assert [s for s, _ in items_b] == ["pregunta 2a", "pregunta 2b", "pregunta 4a", "pregunta 4b"]


def test_batch_never_uses_any_analysis_model_even_legacy(conn):
    stored = analysis([q("vieja")]).model_dump_json()
    add_material(conn, 1, analysis_json=stored, analysis_model=None)
    fake = FakeProvider({}, always_right)
    assert process_pending(conn, fake) == 1
    assert fake.analyzed_refs == []
    assert [exclude for _, exclude in fake.blind_batches] == [(LEGACY_ANALYSIS_MODEL,)]


def test_mixed_batch_excludes_every_analysis_model(conn):
    add_material(conn, 1, analysis_json=analysis([q("a")]).model_dump_json(), analysis_model="A")
    add_material(conn, 2, analysis_json=analysis([q("b")]).model_dump_json(), analysis_model=None)
    fake = FakeProvider({}, always_right)
    results = verify_batch(conn, fake, [1, 2])
    assert [r.status for r in results] == ["done", "done"]
    assert len(fake.blind_batches) == 1
    assert set(fake.blind_batches[0][1]) == {"A", LEGACY_ANALYSIS_MODEL}


def test_no_blind_model_left_keeps_batch_pending_with_its_analysis(conn):
    for i in (1, 2):
        add_material(conn, i)
    fake = FakeProvider(analyses_for((1, 2)), always_right, blind_models=("analizador",))
    assert process_pending(conn, fake) == 0
    assert fake.blind_batches == []
    assert statuses(conn) == {1: "pending", 2: "pending"}
    assert conn.execute("SELECT COUNT(*) FROM material WHERE analysis_json IS NOT NULL"
                        ).fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM question").fetchone()[0] == 0
    assert stop_reason(conn) == "quota"


def test_answers_are_split_back_to_their_class(conn):
    for i in (1, 2, 3):
        add_material(conn, i)
    # La clase 2 tiene una pregunta mal marcada: el modelo a ciegas responde otra.
    blind = {"pregunta 1a": 1, "pregunta 1b": 1, "pregunta 2a": 1, "pregunta 2b": 3,
             "pregunta 3a": 1, "pregunta 3b": 1}
    fake = FakeProvider(analyses_for((1, 2, 3)), lambda s, o: blind[s])
    assert process_pending(conn, fake) == 3
    assert len(fake.blind_batches) == 1
    rows = conn.execute("SELECT material_id, stem FROM question ORDER BY id").fetchall()
    assert [(r["material_id"], r["stem"]) for r in rows] == [
        (1, "pregunta 1a"), (1, "pregunta 1b"), (2, "pregunta 2a"),
        (3, "pregunta 3a"), (3, "pregunta 3b"),
    ]


def test_short_answer_list_verifies_nothing(conn):
    for i in (1, 2):
        add_material(conn, i)

    class Short(FakeProvider):
        def answer_blind_batch(self, items, exclude_models=()):
            return super().answer_blind_batch(items, exclude_models)[:-1]

    fake = Short(analyses_for((1, 2)), always_right)
    process_pending(conn, fake)
    assert conn.execute("SELECT COUNT(*) FROM question").fetchone()[0] == 0


def test_failed_batch_retries_without_reanalysis(conn):
    for i in range(1, 5):
        add_material(conn, i)
    fake = FakeProvider(analyses_for(range(1, 5)), always_right, blind_errors=[AIQuotaError()])
    assert process_pending(conn, fake) == 0
    assert set(statuses(conn).values()) == {"pending"}
    assert conn.execute("SELECT COUNT(*) FROM material WHERE analysis_json IS NOT NULL"
                        ).fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM question").fetchone()[0] == 0
    assert stop_reason(conn) == "quota"
    analyzed = list(fake.analyzed_refs)

    assert process_pending(conn, fake) == 4
    assert fake.analyzed_refs == analyzed
    assert set(statuses(conn).values()) == {"done"}


@pytest.mark.parametrize("exc,reason", [(AIUnavailableError(), "unavailable"), (AIAuthError(), "auth")])
def test_blind_stop_error_writes_nothing(conn, exc, reason):
    for i in (1, 2):
        add_material(conn, i)
    fake = FakeProvider(analyses_for((1, 2)), always_right, blind_errors=[exc])
    assert process_pending(conn, fake) == 0
    assert statuses(conn) == {1: "pending", 2: "pending"}
    assert conn.execute("SELECT COUNT(*) FROM question").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM topic").fetchone()[0] == 0
    assert stop_reason(conn) == reason


def test_unreadable_batch_retries_each_class_alone(conn):
    for i in (1, 2, 3):
        add_material(conn, i)
    fake = FakeProvider(analyses_for((1, 2, 3)), always_right, blind_errors=[AIFormatError()])
    assert process_pending(conn, fake) == 3
    assert [len(items) for items, _ in fake.blind_batches] == [6, 2, 2, 2]
    assert all(exclude == ("analizador",) for _, exclude in fake.blind_batches)
    assert set(statuses(conn).values()) == {"done"}
    assert conn.execute("SELECT COUNT(*) FROM question").fetchone()[0] == 6


def test_unreadable_single_class_fails_with_format(conn):
    add_material(conn, 1)
    add_material(conn, 2)
    fake = FakeProvider(analyses_for((1, 2)), always_right,
                        blind_errors=[AIFormatError(), AIFormatError()])
    assert process_pending(conn, fake) == 1
    rows = {r["id"]: (r["status"], r["fail_kind"])
            for r in conn.execute("SELECT id, status, fail_kind FROM material")}
    assert rows == {1: ("failed", FAIL_FORMAT), 2: ("done", None)}
    assert [r["material_id"] for r in conn.execute("SELECT material_id FROM question")] == [2, 2]


def test_quota_while_retrying_alone_keeps_the_rest_pending(conn):
    for i in (1, 2, 3):
        add_material(conn, i)
    fake = FakeProvider(analyses_for((1, 2, 3)), always_right,
                        blind_errors=[AIFormatError(), None, AIQuotaError()])
    assert process_pending(conn, fake) == 1
    assert statuses(conn) == {1: "done", 2: "pending", 3: "pending"}
    assert stop_reason(conn) == "quota"


def test_class_without_local_candidates_needs_no_blind_request(conn):
    add_material(conn, 1)
    # Opciones repetidas: no pasa lo local, no hay nada que preguntar a ciegas.
    broken = q("repetida").model_copy(update={"options": ["1", "1", "2", "3"]})
    fake = FakeProvider({"v1": analysis([broken])}, always_right)
    assert process_pending(conn, fake) == 1
    assert fake.blind_batches == []
    assert used_today(conn, "blind", time.time()) == 0
    assert statuses(conn) == {1: "done"}
    assert conn.execute("SELECT COUNT(*) FROM question").fetchone()[0] == 0


def test_class_without_candidates_does_not_join_the_request(conn):
    add_material(conn, 1)
    add_material(conn, 2)
    broken = q("repetida").model_copy(update={"options": ["1", "1", "2", "3"]})
    fake = FakeProvider({"v1": analysis([broken]), "v2": analysis([q("sana")])}, always_right)
    assert process_pending(conn, fake) == 2
    assert [[s for s, _ in items] for items, _ in fake.blind_batches] == [["sana"]]


class AnalysisRaises(FakeProvider):
    def __init__(self, *args, raises, **kwargs):
        super().__init__(*args, **kwargs)
        self.raises = raises

    def analyze_video(self, youtube_id, title):
        if youtube_id in self.raises:
            raise self.raises[youtube_id]
        return super().analyze_video(youtube_id, title)


def test_blind_phase_runs_after_analysis_quota(conn):
    for i in (1, 2, 3):
        add_material(conn, i)
    fake = AnalysisRaises(analyses_for((1, 2, 3)), always_right, raises={"v3": AIQuotaError()})
    assert process_pending(conn, fake) == 2
    assert len(fake.blind_batches) == 1
    assert statuses(conn) == {1: "done", 2: "done", 3: "pending"}
    assert stop_reason(conn) == "quota"


def test_blind_phase_skipped_after_analysis_auth(conn):
    for i in (1, 2):
        add_material(conn, i)
    fake = AnalysisRaises(analyses_for((1, 2)), always_right, raises={"v2": AIAuthError()})
    assert process_pending(conn, fake) == 0
    assert fake.blind_batches == []
    assert statuses(conn) == {1: "pending", 2: "pending"}
    assert stop_reason(conn) == "auth"


def test_blind_phase_runs_after_analysis_unavailable(conn):
    # La cadena de análisis caída no deja sin verificar lo ya analizado.
    for i in (1, 2, 3):
        add_material(conn, i)
    fake = AnalysisRaises(analyses_for((1, 2, 3)), always_right,
                          raises={"v3": AIUnavailableError()})
    assert process_pending(conn, fake) == 2
    assert len(fake.blind_batches) == 1
    assert statuses(conn) == {1: "done", 2: "done", 3: "pending"}
    assert stop_reason(conn) == "unavailable"


def test_blind_unavailable_after_analysis_unavailable_writes_nothing(conn):
    for i in (1, 2):
        add_material(conn, i)
    fake = AnalysisRaises(analyses_for((1, 2)), always_right, raises={"v2": AIUnavailableError()},
                          blind_errors=[AIUnavailableError()])
    assert process_pending(conn, fake) == 0
    assert statuses(conn) == {1: "pending", 2: "pending"}
    assert conn.execute("SELECT COUNT(*) FROM question").fetchone()[0] == 0
    assert conn.execute("SELECT analysis_json IS NOT NULL FROM material WHERE id=1").fetchone()[0]
    assert stop_reason(conn) == "unavailable"


def test_unexpected_error_after_unreadable_batch_costs_no_extra_request(conn):
    for i in range(1, 5):
        add_material(conn, i)
    # Lote ilegible, después cada clase sola; la 2 rompe de forma inesperada.
    fake = FakeProvider(analyses_for(range(1, 5)), always_right,
                        blind_errors=[AIFormatError(), None, ValueError("roto")])
    assert process_pending(conn, fake) == 3
    assert len(fake.blind_batches) == 1 + 4
    assert used_today(conn, "blind", time.time()) == 1 + 4
    assert statuses(conn) == {1: "done", 2: "failed", 3: "done", 4: "done"}


def test_class_without_candidates_in_the_middle_keeps_answers_aligned(conn):
    broken = ["1", "1", "2", "3"]  # opciones repetidas: no pasa lo local
    plan = {1: [0, 1, 2], 2: None, 3: [3, 0], 4: [1, 2, 3, 0]}
    wrong = {"c1q1", "c3q0", "c4q2"}  # el modelo a ciegas responde otra
    analyses = {}
    for mid, corrects in plan.items():
        add_material(conn, mid)
        if corrects is None:
            questions = [q(f"c{mid}q{i}").model_copy(update={"options": broken}) for i in range(2)]
        else:
            questions = [q(f"c{mid}q{i}", correct=c) for i, c in enumerate(corrects)]
        analyses[f"v{mid}"] = analysis(questions)
    correct_by_stem = {qq.stem: qq.correct_index for a in analyses.values() for qq in a.questions}

    def blind(stem, options):
        right = correct_by_stem[stem]
        return (right + 1) % 4 if stem in wrong else right

    fake = FakeProvider(analyses, blind)
    assert process_pending(conn, fake) == 4
    assert [len(items) for items, _ in fake.blind_batches] == [9]
    rows = conn.execute("SELECT material_id, stem, correct_index FROM question ORDER BY id").fetchall()
    expected = [(mid, f"c{mid}q{i}", c) for mid, corrects in plan.items() if corrects
                for i, c in enumerate(corrects) if f"c{mid}q{i}" not in wrong]
    assert [(r["material_id"], r["stem"], r["correct_index"]) for r in rows] == expected


def test_last_stop_reason_wins(conn):
    for i in (1, 2):
        add_material(conn, i)
    fake = AnalysisRaises(analyses_for((1, 2)), always_right, raises={"v2": AIQuotaError()},
                          blind_errors=[AIUnavailableError()])
    assert process_pending(conn, fake) == 0
    assert stop_reason(conn) == "unavailable"


def test_paid_batches_groups_by_model_in_queue_order(conn):
    blob = analysis([]).model_dump_json()
    models = {1: "B", 2: "A", 3: "B", 4: None, 5: "B", 6: "B", 7: "B", 8: "A"}
    for mid, model in models.items():
        add_material(conn, mid, analysis_json=blob, analysis_model=model)
    order = [1, 2, 3, 4, 5, 6, 7, 8]
    assert paid_batches(conn, order) == [[1, 3, 5, 6], [7], [2, 8], [4]]


# --- Las opciones van mezcladas: el lugar de la correcta no da pistas (revisión final 3c) ------

from estudia.generation import pipeline  # noqa: E402


class _Reverse:
    """Un sorteo que da vuelta cada lista: el cambio de orden es seguro y fácil de seguir."""

    def shuffle(self, items):
        items.reverse()


def _by_text(answers):
    """Contesta por el texto de la opción, no por su lugar."""
    return lambda stem, options: options.index(answers[stem])


def test_blind_options_are_shuffled_and_answers_mapped_back():
    questions = [q("a", correct=1), q("b", correct=3), q("c", correct=0), q("d", correct=2)]
    # El modelo acierta a, b y c por el texto; erra d (elige "1", la de lugar 0 en la original).
    prov = FakeProvider({}, _by_text({"a": "2", "b": "4", "c": "1", "d": "1"}))
    assert pipeline.blind_matches(prov, questions, (), rng=_Reverse()) == [True, True, True, False]
    items, _ = prov.blind_batches[0]
    assert [options for _, options in items] == [["4", "3", "2", "1"]] * 4


def test_blind_answer_by_original_position_fails_after_the_shuffle():
    # Un modelo que contesta el lugar de la correcta en la original ya no acierta por posición.
    questions = [q("a", correct=1)]
    prov = FakeProvider({}, lambda stem, options: 1)
    assert pipeline.blind_matches(prov, questions, (), rng=_Reverse()) == [False]


def test_blind_out_of_range_or_odd_answers_never_match():
    questions = [q("a", correct=0)] * 4
    answers = iter([4, -1, True, "0"])
    prov = FakeProvider({}, lambda stem, options: next(answers))
    assert pipeline.blind_matches(prov, questions, (), rng=_Reverse()) == [False] * 4


@pytest.mark.real_blind_shuffle
def test_default_blind_shuffle_is_reproducible_per_request():
    questions = [q(f"pregunta {i}", correct=i % 4) for i in range(8)]
    sent = []
    for _ in range(2):
        prov = FakeProvider({}, lambda stem, options: None)
        pipeline.blind_matches(prov, questions, ())
        sent.append(prov.blind_batches[0][0])
    assert sent[0] == sent[1]
    assert any(options != ["1", "2", "3", "4"] for _, options in sent[0])
    assert all(sorted(options) == ["1", "2", "3", "4"] for _, options in sent[0])


@pytest.mark.real_blind_shuffle
def test_process_pending_maps_shuffled_answers_back(conn):
    # De punta a punta con el sorteo de verdad: acierta por el texto, guarda las dos.
    add_material(conn, 1)
    prov = FakeProvider({"v1": analysis([q("pregunta 1a", correct=1), q("pregunta 1b", correct=2)])},
                        _by_text({"pregunta 1a": "2", "pregunta 1b": "3"}))
    process_pending(conn, prov)
    assert statuses(conn) == {1: "done"}
    assert conn.execute("SELECT COUNT(*) FROM question").fetchone()[0] == 2
    items, _ = prov.blind_batches[0]
    assert [options for _, options in items] != [["1", "2", "3", "4"]] * 2

