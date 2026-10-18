import json
import time
from types import SimpleNamespace

import pytest

from estudia.ai.base import (
    AIAuthError,
    AIFormatError,
    AIQuotaError,
    AIUnavailableError,
    AIVideoUnavailableError,
    GenQuestion,
    Segment,
    VideoAnalysis,
)
from estudia.ai.gemini import GeminiProvider
from estudia.db import connect
from estudia.generation.pipeline import (
    GENERIC_ERROR_TEXT,
    NEEDS_LINK_TEXT,
    VIDEO_ERROR_TEXT,
    process_material,
    process_pending,
    retry_failed,
    slugify,
)
from estudia.material.video import VideoDownloadError
from fakes import DeniedYoutubeUrlClient, FakeProvider


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    c.execute("INSERT INTO section(id, course_id, num, name, visible) VALUES (1, 10, 1, 'S1', 1)")
    c.execute("INSERT INTO section(id, course_id, num, name, visible) VALUES (2, 10, 2, 'S2', 1)")
    c.execute("INSERT INTO section(id, course_id, num, name, visible) VALUES (3, 10, 0, 'Oculta', 0)")
    c.commit()
    return c


def add_material(conn, mid, ref, section_id=1):
    conn.execute(
        "INSERT INTO material(id, section_id, cmid, kind, title, ref, status)"
        " VALUES (?, ?, ?, 'youtube', ?, ?, 'pending')",
        (mid, section_id, 1000 + mid, f"Video {mid}", ref),
    )
    conn.commit()


def q(stem, correct=1, check_expr=None, topic="Funciones lineales"):
    return GenQuestion(
        stem=stem,
        options=["1", "2", "3", "4"],
        correct_index=correct,
        explanation="porque sí",
        common_error="error común",
        topic=topic,
        source_seconds=5,
        check_expr=check_expr,
    )


def analysis(questions, topics=("Funciones lineales",)):
    return VideoAnalysis(summary_md="# Resumen", topics=list(topics), questions=questions)


def blind_by_stem(answers):
    return lambda stem, options: answers[stem]


def statuses(conn):
    return {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM material")}


def test_keeps_only_verified(conn):
    add_material(conn, 1, "v1")
    a = analysis([
        q("A", correct=1, check_expr="2"),
        q("B", correct=1, check_expr="3"),  # check_expr no coincide con la opción marcada
        q("C", correct=1),  # a ciegas responde otra
    ])
    prov = FakeProvider({"v1": a}, blind_by_stem({"A": 1, "B": 1, "C": 3}))
    res = process_material(conn, prov, 1)
    assert (res.kept, res.discarded) == (1, 2)
    rows = conn.execute("SELECT stem, verified FROM question").fetchall()
    assert [(r["stem"], r["verified"]) for r in rows] == [("A", 1)]
    m = conn.execute("SELECT status, summary_md FROM material WHERE id=1").fetchone()
    assert m["status"] == "done" and m["summary_md"] == "# Resumen"


def test_question_without_check_expr_needs_blind_match(conn):
    add_material(conn, 1, "v1")
    a = analysis([q("ok", correct=2), q("mal", correct=2)])
    prov = FakeProvider({"v1": a}, blind_by_stem({"ok": 2, "mal": 0}))
    res = process_material(conn, prov, 1)
    assert (res.kept, res.discarded) == (1, 1)
    assert [r["stem"] for r in conn.execute("SELECT stem FROM question")] == ["ok"]


def test_topics_are_deduplicated(conn):
    add_material(conn, 1, "v1")
    add_material(conn, 2, "v2")
    prov = FakeProvider(
        {
            "v1": analysis([q("a")], topics=["Funciones lineales"]),
            "v2": analysis([q("b", topic="funciones  lineales ")], topics=["funciones  lineales "]),
        },
        lambda stem, options: 1,
    )
    process_material(conn, prov, 1)
    process_material(conn, prov, 2)
    assert conn.execute("SELECT COUNT(*) FROM topic").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM material_topic").fetchone()[0] == 2
    topic_ids = {r["topic_id"] for r in conn.execute("SELECT topic_id FROM question")}
    assert len(topic_ids) == 1


class ScriptedProvider(FakeProvider):
    """FakeProvider que lanza una excepción para ciertos videos."""

    def __init__(self, analyses, blind, raises=None, blind_raises=None):
        super().__init__(analyses, blind)
        self.raises = raises or {}
        self.blind_raises = blind_raises

    def analyze_video(self, youtube_id, title):
        if youtube_id in self.raises:
            raise self.raises[youtube_id]
        return super().analyze_video(youtube_id, title)

    def answer_blind(self, stem, options):
        if self.blind_raises is not None:
            raise self.blind_raises
        return super().answer_blind(stem, options)


def test_quota_mid_run_resumes(conn):
    for i in (1, 2, 3):
        add_material(conn, i, f"v{i}")
    analyses = {f"v{i}": analysis([q(f"pregunta {i}")]) for i in (1, 2, 3)}
    bad = ScriptedProvider(analyses, lambda s, o: 1, raises={"v2": AIQuotaError()})
    assert process_pending(conn, bad) == 1
    assert statuses(conn) == {1: "done", 2: "pending", 3: "pending"}
    assert conn.execute("SELECT value FROM kv WHERE key='ai_stop_reason'").fetchone()[0] == "quota"

    good = FakeProvider(analyses, lambda s, o: 1)
    assert process_pending(conn, good) == 2
    assert statuses(conn) == {1: "done", 2: "done", 3: "done"}
    assert conn.execute("SELECT COUNT(*) FROM question").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM kv WHERE key='ai_stop_reason'").fetchone()[0] == 0


@pytest.mark.parametrize(
    "exc,reason",
    [(AIQuotaError(), "quota"), (AIUnavailableError(), "unavailable"), (AIAuthError(), "auth")],
)
def test_stop_errors_leave_pending_and_record_reason(conn, exc, reason):
    add_material(conn, 1, "v1")
    add_material(conn, 2, "v2")
    analyses = {"v1": analysis([q("x")]), "v2": analysis([q("y")])}
    prov = ScriptedProvider(analyses, lambda s, o: 1, raises={"v1": exc})
    assert process_pending(conn, prov) == 0
    assert statuses(conn) == {1: "pending", 2: "pending"}
    assert conn.execute("SELECT value FROM kv WHERE key='ai_stop_reason'").fetchone()[0] == reason


@pytest.mark.parametrize(
    "exc,reason",
    [(AIQuotaError(), "quota"), (AIUnavailableError(), "unavailable"), (AIAuthError(), "auth")],
)
def test_blind_stop_error_saves_nothing(conn, exc, reason):
    add_material(conn, 1, "v1")
    prov = ScriptedProvider({"v1": analysis([q("x")])}, None, blind_raises=exc)
    assert process_pending(conn, prov) == 0
    assert statuses(conn) == {1: "pending"}
    assert conn.execute("SELECT COUNT(*) FROM question").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM topic").fetchone()[0] == 0
    assert conn.execute("SELECT value FROM kv WHERE key='ai_stop_reason'").fetchone()[0] == reason


def test_blind_format_error_discards_only_that_question(conn):
    add_material(conn, 1, "v1")

    def blind(stem, options):
        if stem == "roto":
            raise AIFormatError("secreto-sk-123")
        return 1

    prov = FakeProvider({"v1": analysis([q("roto"), q("sano")])}, blind)
    res = process_material(conn, prov, 1)
    assert (res.kept, res.discarded) == (1, 1)
    assert [r["stem"] for r in conn.execute("SELECT stem FROM question")] == ["sano"]


def test_format_error_marks_failed(conn):
    add_material(conn, 1, "v1")
    add_material(conn, 2, "v2")
    prov = ScriptedProvider(
        {"v2": analysis([q("ok")])}, lambda s, o: 1,
        raises={"v1": AIFormatError("clave sk-SECRETA")},
    )
    assert process_pending(conn, prov) == 1
    m1 = conn.execute("SELECT status, error FROM material WHERE id=1").fetchone()
    assert m1["status"] == "failed" and m1["error"]
    assert "SECRETA" not in m1["error"]
    assert statuses(conn)[2] == "done"


def test_order_visible_sections_by_num_then_id_and_budget(conn):
    add_material(conn, 5, "v5", section_id=2)
    add_material(conn, 3, "v3", section_id=1)
    add_material(conn, 4, "v4", section_id=1)
    add_material(conn, 9, "v9", section_id=3)  # sección oculta
    order = []

    class Rec(FakeProvider):
        def analyze_video(self, youtube_id, title):
            order.append(youtube_id)
            return analysis([])

    assert process_pending(conn, Rec({}, lambda s, o: 0), budget=2) == 2
    assert order == ["v3", "v4"]
    assert statuses(conn)[9] == "pending" and statuses(conn)[5] == "pending"


def test_dedupe_key_prevents_duplicates_on_reprocess(conn):
    add_material(conn, 1, "v1")
    prov = FakeProvider({"v1": analysis([q("  Hola   Mundo ")])}, lambda s, o: 1)
    process_material(conn, prov, 1)
    process_material(conn, prov, 1)
    assert conn.execute("SELECT COUNT(*) FROM question").fetchone()[0] == 1
    opts = conn.execute("SELECT options_json FROM question").fetchone()[0]
    assert json.loads(opts) == ["1", "2", "3", "4"]


def test_unknown_topic_does_not_crash(conn):
    add_material(conn, 1, "v1")
    prov = FakeProvider({"v1": analysis([q("a", topic="Otro tema")])}, lambda s, o: 1)
    assert process_material(conn, prov, 1).kept == 1


def test_question_topic_outside_topics_is_linked_to_material(conn):
    add_material(conn, 1, "v1")
    prov = FakeProvider({"v1": analysis([q("a", topic="Otro tema")])}, lambda s, o: 1)
    process_material(conn, prov, 1)
    linked = {
        r["slug"]
        for r in conn.execute(
            "SELECT t.slug FROM material_topic mt JOIN topic t ON t.id = mt.topic_id"
            " WHERE mt.material_id = 1"
        )
    }
    assert linked == {"funciones-lineales", "otro-tema"}
    qtopic = conn.execute(
        "SELECT t.slug FROM question q JOIN topic t ON t.id = q.topic_id"
    ).fetchone()[0]
    assert qtopic == "otro-tema"


@pytest.mark.parametrize("blank", ["", "   ", "¿?", "—"])
def test_blank_question_topic_falls_back_to_first_topic(conn, blank):
    add_material(conn, 1, "v1")
    prov = FakeProvider({"v1": analysis([q("a", topic=blank)], topics=("Límites", "Derivadas"))},
                        lambda s, o: 1)
    assert process_material(conn, prov, 1).kept == 1
    slug = conn.execute(
        "SELECT t.slug FROM question q JOIN topic t ON t.id = q.topic_id"
    ).fetchone()[0]
    assert slug == "limites"


def test_question_without_any_usable_topic_is_not_stored(conn):
    add_material(conn, 1, "v1")
    prov = FakeProvider({"v1": analysis([q("a", topic="?")], topics=("¿?",))}, lambda s, o: 1)
    res = process_material(conn, prov, 1)
    assert (res.status, res.kept, res.discarded) == ("done", 0, 1)
    assert conn.execute("SELECT COUNT(*) FROM question WHERE topic_id IS NULL").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM question").fetchone()[0] == 0


def test_slugify():
    assert slugify("Funciones  Lineales ") == "funciones-lineales"
    assert slugify("Límites") == "limites"


def fail_kinds(conn):
    return {r["id"]: r["fail_kind"] for r in conn.execute("SELECT id, fail_kind FROM material")}


def test_failures_record_their_kind(conn):
    for i in (1, 2, 3):
        add_material(conn, i, f"v{i}")
    prov = ScriptedProvider({}, lambda s, o: 1, raises={
        "v1": AIVideoUnavailableError("x"), "v2": AIFormatError("x"), "v3": ValueError("x")})
    process_pending(conn, prov)
    assert fail_kinds(conn) == {1: "video", 2: "format", 3: "error"}


def test_retry_failed_with_kinds_keeps_unavailable_videos(conn):
    from estudia.generation.pipeline import RETRY_ON_NEW_KEY, retry_failed

    for i, kind in enumerate(["video", "format", "error", None], start=1):
        add_material(conn, i, f"v{i}")
        conn.execute("UPDATE material SET status='failed', error='e', fail_kind=? WHERE id=?", (kind, i))
    conn.commit()
    assert retry_failed(conn, RETRY_ON_NEW_KEY) == 3
    assert statuses(conn) == {1: "failed", 2: "pending", 3: "pending", 4: "pending"}
    assert fail_kinds(conn) == {1: "video", 2: None, 3: None, 4: None}
    assert retry_failed(conn) == 1  # el botón manual reintenta todo
    assert statuses(conn)[1] == "pending"


def test_retry_failed_resets_only_failed(conn):
    from estudia.generation.pipeline import retry_failed

    add_material(conn, 1, "v1")
    add_material(conn, 2, "v2")
    conn.execute("UPDATE material SET status='failed', error='algo' WHERE id=1")
    conn.execute("UPDATE material SET status='done' WHERE id=2")
    conn.commit()
    assert retry_failed(conn) == 1
    rows = {r["id"]: (r["status"], r["error"]) for r in conn.execute("SELECT id, status, error FROM material")}
    assert rows == {1: ("pending", None), 2: ("done", None)}


def test_video_unavailable_gets_its_own_error_text(conn):
    add_material(conn, 1, "v1")
    prov = ScriptedProvider({}, lambda s, o: 1, raises={"v1": AIVideoUnavailableError("x")})
    assert process_pending(conn, prov) == 0
    m = conn.execute("SELECT status, error FROM material WHERE id=1").fetchone()
    assert (m["status"], m["error"]) == ("failed", VIDEO_ERROR_TEXT)


@pytest.mark.parametrize("where", ["analyze", "blind"])
def test_unexpected_error_fails_only_that_material(conn, where):
    add_material(conn, 1, "v1")
    add_material(conn, 2, "v2")
    boom = ValueError("detalle interno sk-SECRETA")
    analyses = {"v1": analysis([q("x")]), "v2": analysis([q("y")])}

    def blind(stem, options):
        if where == "blind" and stem == "x":
            raise boom
        return 1

    raises = {"v1": boom} if where == "analyze" else {}
    prov = ScriptedProvider(analyses, blind, raises=raises)
    assert process_pending(conn, prov) == 1
    m1 = conn.execute("SELECT status, error FROM material WHERE id=1").fetchone()
    assert (m1["status"], m1["error"]) == ("failed", GENERIC_ERROR_TEXT)
    assert "SECRETA" not in m1["error"]
    assert statuses(conn)[2] == "done"
    assert [r["stem"] for r in conn.execute("SELECT stem FROM question")] == ["y"]
    assert conn.execute("SELECT COUNT(*) FROM kv WHERE key='ai_stop_reason'").fetchone()[0] == 0


def test_blind_pass_429_keeps_the_paid_analysis(conn):
    """Con Gemini real (cliente falso): un 429 por minuto en la pasada ciega no repite el análisis."""
    from types import SimpleNamespace

    from google.genai import errors

    from estudia.ai.gemini import GeminiProvider

    add_material(conn, 1, "v1")
    payload = analysis([q("A", correct=1)]).model_dump_json()
    rate_limited = errors.ClientError(429, {"error": {"status": "RESOURCE_EXHAUSTED", "details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "30s"}]}})
    outcomes = [payload, rate_limited, '{"answers": [{"q": 1, "answer": 1}]}']
    calls, sleeps = [], []

    class Client:
        models = None

        def list(self):
            return []

        def generate_content(self, model, contents, config):
            calls.append(contents)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return SimpleNamespace(text=outcome)

    client = Client()
    client.models = client
    prov = GeminiProvider("k", analyze_models=["m"], blind_models=["b"], client=client,
                          sleep=sleeps.append)
    res = process_material(conn, prov, 1)
    assert (res.status, res.kept) == ("done", 1)
    assert sleeps == [30.0]
    assert len(calls) == 3  # un análisis, dos intentos de la pasada ciega


def q_with(stem, options, correct=0, check_expr=None):
    return q(stem, correct=correct, check_expr=check_expr).model_copy(update={"options": options})


def test_rejects_distractor_equivalent_to_the_answer(conn):
    add_material(conn, 1, "v1")
    asked = []

    def blind(stem, options):
        asked.append(stem)
        return 0

    a = analysis([
        q_with("equivalente", ["1/2", "0,5", "3", "4"], check_expr="1/2"),
        q_with("sana", ["1/2", "0,25", "3", "4"], check_expr="1/2"),
    ])
    res = process_material(conn, FakeProvider({"v1": a}, blind), 1)
    assert (res.kept, res.discarded) == (1, 1)
    assert [r["stem"] for r in conn.execute("SELECT stem FROM question")] == ["sana"]
    assert "equivalente" not in asked  # se descarta sin gastar la pasada ciega


@pytest.mark.parametrize("options", [
    ["Hola mundo", "hola  Mundo", "x", "y"],
    ["x + 1", "X+1", "2", "3"],
    ["5", " 5 ", "6", "7"],
])
def test_rejects_options_duplicated_after_normalization(conn, options):
    add_material(conn, 1, "v1")
    a = analysis([q_with("dup", options), q_with("ok", ["a", "b", "c", "d"])])
    res = process_material(conn, FakeProvider({"v1": a}, lambda s, o: 0), 1)
    assert (res.kept, res.discarded) == (1, 1)
    assert [r["stem"] for r in conn.execute("SELECT stem FROM question")] == ["ok"]


def test_distractor_check_timeout_discards_the_question(conn, monkeypatch):
    """Si el chequeo de un distractor se pasa del tiempo, no se sabe si es otra respuesta correcta."""
    from estudia.generation import pipeline, verify

    real = verify.check_math_tristate

    def slow_on_distractors(expr, option):
        real_start = verify._start_worker
        if option != "1/2":
            verify._stop_worker()   # sin worker tibio que pueda contestar a tiempo
            monkeypatch.setattr(verify, "_start_worker", lambda: False)
        try:
            return real(expr, option)
        finally:
            monkeypatch.setattr(verify, "_start_worker", real_start)

    monkeypatch.setattr(pipeline, "check_math_tristate", slow_on_distractors)
    add_material(conn, 1, "v1")
    a = analysis([
        q_with("con timeout", ["1/2", "0,25", "3", "4"], check_expr="1/2"),
        q_with("sin check", ["a", "b", "c", "d"]),
    ])
    res = process_material(conn, FakeProvider({"v1": a}, lambda s, o: 0), 1)
    assert (res.kept, res.discarded) == (1, 1)
    assert [r["stem"] for r in conn.execute("SELECT stem FROM question")] == ["sin check"]


def test_correct_option_needs_a_definite_true(conn, monkeypatch):
    from estudia.generation import pipeline

    monkeypatch.setattr(pipeline, "check_math_tristate", lambda expr, option: None)
    add_material(conn, 1, "v1")
    a = analysis([q_with("indeciso", ["1/2", "0,25", "3", "4"], check_expr="1/2"),
                  q_with("ok", ["a", "b", "c", "d"])])
    res = process_material(conn, FakeProvider({"v1": a}, lambda s, o: 0), 1)
    assert (res.kept, res.discarded) == (1, 1)


class CountingProvider(FakeProvider):
    def __init__(self, analyses, blind):
        super().__init__(analyses, blind)
        self.analyzed = 0

    def analyze_video(self, youtube_id, title):
        self.analyzed += 1
        return super().analyze_video(youtube_id, title)


def cached_analysis(conn, mid=1):
    return conn.execute("SELECT analysis_json FROM material WHERE id=?", (mid,)).fetchone()[0]


def test_blind_quota_keeps_the_analysis_for_the_next_run(conn):
    add_material(conn, 1, "v1")
    analyses = {"v1": analysis([q("A")])}
    quota = {"on": True}

    def blind(stem, options):
        if quota["on"]:
            raise AIQuotaError()
        return 1

    prov = CountingProvider(analyses, blind)
    assert process_pending(conn, prov) == 0
    assert statuses(conn) == {1: "pending"}
    assert cached_analysis(conn) is not None

    quota["on"] = False
    assert process_pending(conn, prov) == 1
    assert prov.analyzed == 1  # la segunda corrida no vuelve a pagar el análisis
    assert statuses(conn) == {1: "done"}
    assert cached_analysis(conn) is None
    assert conn.execute("SELECT COUNT(*) FROM question").fetchone()[0] == 1


def test_cached_analysis_is_cleared_when_the_material_fails(conn):
    add_material(conn, 1, "v1")
    calls = {"n": 0}

    def blind(stem, options):
        calls["n"] += 1
        raise AIQuotaError() if calls["n"] == 1 else ValueError("roto")

    prov = CountingProvider({"v1": analysis([q("A")])}, blind)
    process_pending(conn, prov)
    assert cached_analysis(conn) is not None
    process_pending(conn, prov)
    assert statuses(conn) == {1: "failed"}
    assert cached_analysis(conn) is None


def test_broken_cached_analysis_is_requested_again(conn):
    add_material(conn, 1, "v1")
    conn.execute("UPDATE material SET analysis_json='{roto' WHERE id=1")
    conn.commit()
    prov = CountingProvider({"v1": analysis([q("A")])}, lambda s, o: 1)
    assert process_pending(conn, prov) == 1
    assert prov.analyzed == 1


def test_blank_check_expr_is_stored_as_null(conn):
    add_material(conn, 1, "v1")
    prov = FakeProvider({"v1": analysis([q("a", check_expr="   ")])}, lambda s, o: 1)
    assert process_material(conn, prov, 1).kept == 1
    assert conn.execute("SELECT check_expr FROM question").fetchone()[0] is None


def test_repeated_stem_counts_once(conn):
    add_material(conn, 1, "v1")
    prov = FakeProvider({"v1": analysis([q("Igual"), q("igual ")])}, lambda s, o: 1)
    res = process_material(conn, prov, 1)
    assert (res.kept, res.discarded) == (1, 1)
    assert conn.execute("SELECT COUNT(*) FROM question").fetchone()[0] == 1


@pytest.mark.parametrize("budget", [0, -1])
def test_non_positive_budget_processes_nothing(conn, budget):
    add_material(conn, 1, "v1")
    prov = FakeProvider({"v1": analysis([q("a")])}, lambda s, o: 1)
    assert process_pending(conn, prov, budget=budget) == 0
    assert statuses(conn) == {1: "pending"}


@pytest.mark.parametrize("distractor", ["½", "50%", "x = 1/2", "1/2 cm", "1/2 aprox."])
def test_unreadable_distractor_discards_the_question(conn, distractor):
    """Una opción que SymPy no lee puede ser la misma respuesta escrita de otra forma."""
    add_material(conn, 1, "v1")
    a = analysis([
        q_with("ilegible", ["0,5", distractor, "3", "4"], check_expr="1/2"),
        q_with("sana", ["1/2", "0,25", "3", "4"], check_expr="1/2"),
    ])
    res = process_material(conn, FakeProvider({"v1": a}, lambda s, o: 0), 1)
    assert (res.kept, res.discarded) == (1, 1)
    assert [r["stem"] for r in conn.execute("SELECT stem FROM question")] == ["sana"]


# --- Gemini no acepta el enlace de YouTube (403): nunca es una clave mala ------------

def _gemini(tmp_path, client, download_error=None, calls=None):
    def download(youtube_id, dest_dir):
        if calls is not None:
            calls.append(youtube_id)
        if download_error:
            raise VideoDownloadError(download_error)
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / f"{youtube_id}.mp4"
        path.write_bytes(b"video")
        return path

    return GeminiProvider("clave-falsa", analyze_models=["m"], blind_models=["b"], client=client,
                          sleep=lambda s: None,
                          download=download, video_dir=tmp_path / "videos")


def kv(conn, key):
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def allow_download(conn):
    conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES ('allow_download', '1')")
    conn.commit()


def test_video_403_with_failed_download_never_records_auth(conn, tmp_path):
    add_material(conn, 1, "aaaaaaaaaaa")
    add_material(conn, 2, "bbbbbbbbbbb")
    allow_download(conn)
    provider = _gemini(tmp_path, DeniedYoutubeUrlClient(), download_error="privado")
    assert process_pending(conn, provider) == 0
    assert statuses(conn) == {1: "failed", 2: "failed"}
    rows = conn.execute("SELECT error, fail_kind FROM material").fetchall()
    assert {(r["error"], r["fail_kind"]) for r in rows} == {(VIDEO_ERROR_TEXT, "video")}
    assert kv(conn, "ai_stop_reason") is None
    assert float(kv(conn, "youtube_url_input")) > time.time() + 6 * 86400  # vence en 7 días


def test_video_403_falls_back_to_upload_and_finishes(conn, tmp_path):
    add_material(conn, 1, "aaaaaaaaaaa")
    allow_download(conn)
    a = analysis([q("¿Cuánto es 1+1?", correct=1)])
    client = DeniedYoutubeUrlClient(analysis_text=a.model_dump_json())
    assert process_pending(conn, _gemini(tmp_path, client)) == 1
    assert statuses(conn) == {1: "done"}
    assert kv(conn, "ai_stop_reason") is None
    assert client.deleted == ["files/x"]
    assert not (tmp_path / "videos" / "aaaaaaaaaaa.mp4").exists()


def test_remembered_url_denial_is_passed_to_the_provider(conn):
    add_material(conn, 1, "aaaaaaaaaaa")
    until = time.time() + 3 * 86400
    conn.execute("INSERT INTO kv(key, value) VALUES ('youtube_url_input', ?)", (str(int(until)),))
    conn.commit()
    seen = []

    class Recording(FakeProvider):
        def analyze_video(self, youtube_id, title):
            seen.append((self.youtube_url_denied_until, self.allow_download))
            return super().analyze_video(youtube_id, title)

    process_pending(conn, Recording({"aaaaaaaaaaa": analysis([q("x")])}, lambda s, o: 1))
    assert seen == [(int(until), False)]
    assert kv(conn, "youtube_url_input") == str(int(until))  # la marca no se corre


def test_legacy_permanent_denial_becomes_a_7_day_denial(conn):
    add_material(conn, 1, "aaaaaaaaaaa")
    conn.execute("INSERT INTO kv(key, value) VALUES ('youtube_url_input', 'denied')")
    conn.commit()
    seen = []

    class Recording(FakeProvider):
        def analyze_video(self, youtube_id, title):
            seen.append(self.youtube_url_denied_until)
            return super().analyze_video(youtube_id, title)

    process_pending(conn, Recording({"aaaaaaaaaaa": analysis([q("x")])}, lambda s, o: 1),
                    clock=lambda: 1_000_000.0)
    assert seen == [1_000_000.0 + 7 * 86400]
    assert kv(conn, "youtube_url_input") == str(1_000_000 + 7 * 86400)


def test_url_input_is_not_marked_denied_without_a_denial(conn):
    add_material(conn, 1, "aaaaaaaaaaa")
    process_pending(conn, FakeProvider({"aaaaaaaaaaa": analysis([q("x")])}, lambda s, o: 1))
    assert kv(conn, "youtube_url_input") is None


# --- Pasada a ciegas en un solo pedido, con otro modelo y rotación por cuota ------

class BatchProvider(FakeProvider):
    """Proveedor cuya pasada a ciegas devuelve la lista indicada tal cual."""

    def __init__(self, analyses, answers):
        super().__init__(analyses, None)
        self.answers = answers

    def answer_blind_batch(self, items, exclude_models=()):
        self.blind_batches.append((items, tuple(exclude_models)))
        return self.answers


@pytest.mark.parametrize("answers", [[1, None, 0], [1, 9, "x"]])
def test_batch_answers_map_back_and_none_or_invalid_discards(conn, answers):
    add_material(conn, 1, "v1")
    prov = BatchProvider({"v1": analysis([q("A"), q("B"), q("C")])}, answers)
    res = process_material(conn, prov, 1)
    assert (res.kept, res.discarded) == (1, 2)
    assert [r[0] for r in conn.execute("SELECT stem FROM question")] == ["A"]


@pytest.mark.parametrize("answers", [[1], [1, 1], [1, 1, 1, 1]])
def test_batch_of_the_wrong_length_keeps_nothing(conn, answers):
    add_material(conn, 1, "v1")
    prov = BatchProvider({"v1": analysis([q("A"), q("B"), q("C")])}, answers)
    res = process_material(conn, prov, 1)
    assert (res.status, res.kept) == ("done", 0)


def test_blind_pass_is_one_batch_without_answers_or_explanations(conn):
    add_material(conn, 1, "v1")
    prov = FakeProvider({"v1": analysis([q("A"), q("B")])}, lambda s, o: 1)
    process_material(conn, prov, 1)
    assert len(prov.blind_batches) == 1
    items, _ = prov.blind_batches[0]
    assert items == [("A", ["1", "2", "3", "4"]), ("B", ["1", "2", "3", "4"])]
    assert "porque sí" not in repr(items) and "error común" not in repr(items)


def test_no_blind_request_when_nothing_passes_the_local_checks(conn):
    add_material(conn, 1, "v1")
    a = analysis([q_with("dup", ["1", "1", "2", "3"])])
    prov = FakeProvider({"v1": a}, lambda s, o: 0)
    process_material(conn, prov, 1)
    assert prov.blind_batches == []


def test_blind_excludes_the_analysis_model_even_from_the_cache(conn):
    add_material(conn, 1, "v1")
    quota = {"on": True}

    class Prov(FakeProvider):
        def answer_blind_batch(self, items, exclude_models=()):
            self.blind_batches.append((items, tuple(exclude_models)))
            if quota["on"]:
                raise AIQuotaError()
            return [1] * len(items)

    first = Prov({"v1": analysis([q("A")])}, None, analysis_model="gemini-2.5-flash")
    process_pending(conn, first)
    assert conn.execute("SELECT analysis_model FROM material").fetchone()[0] == "gemini-2.5-flash"
    quota["on"] = False
    second = Prov({}, None, analysis_model="otro")  # no puede volver a analizar
    assert process_pending(conn, second) == 1
    assert [ex for _, ex in first.blind_batches + second.blind_batches] == [("gemini-2.5-flash",)] * 2
    # Queda anotado en el material terminado: sirve para volver a verificar con otro modelo.
    assert conn.execute("SELECT analysis_model FROM material").fetchone()[0] == "gemini-2.5-flash"


def test_cached_analysis_without_a_model_is_taken_as_the_legacy_one(conn):
    add_material(conn, 1, "v1")
    conn.execute("UPDATE material SET analysis_json=? WHERE id=1",
                 (analysis([q("A")]).model_dump_json(),))
    conn.commit()
    prov = FakeProvider({}, lambda s, o: 1)
    assert process_pending(conn, prov) == 1
    assert prov.blind_batches[0][1] == ("gemini-2.5-flash",)


def test_failed_material_forgets_its_analysis_model(conn):
    add_material(conn, 1, "v1")
    prov = FakeProvider({"v1": analysis([q("A")])}, lambda s, o: 1)

    def boom(items, exclude_models=()):
        raise AIFormatError("nada legible")

    prov.answer_blind_batch = boom
    process_pending(conn, prov)
    assert statuses(conn) == {1: "failed"}
    assert conn.execute("SELECT analysis_model FROM material").fetchone()[0] is None


def test_quota_reset_and_exhausted_models_are_kept_in_kv(conn):
    add_material(conn, 1, "v1")
    future = 4_000_000_000.0

    class Prov(FakeProvider):
        def analyze_video(self, youtube_id, title):
            self.exhausted_until["m"] = future
            raise AIQuotaError("agotado", resets_at=future)

    conn.execute("INSERT INTO kv(key, value) VALUES ('model_exhausted:fake:viejo', '1')")
    conn.execute("INSERT INTO kv(key, value) VALUES ('model_exhausted:sinclave', '4000000000')")
    conn.commit()
    process_pending(conn, Prov({}, None))
    assert kv(conn, "ai_stop_reason") == "quota"
    assert kv(conn, "ai_quota_resets_at") == str(int(future))
    assert kv(conn, "model_exhausted:fake:m") is not None
    assert kv(conn, "model_exhausted:fake:viejo") is None  # vencida: se borra
    assert kv(conn, "model_exhausted:sinclave") is None  # formato viejo, sin clave

    seen = []

    class Later(FakeProvider):
        def analyze_video(self, youtube_id, title):
            seen.append(dict(self.exhausted_until))
            return super().analyze_video(youtube_id, title)

    process_pending(conn, Later({"v1": analysis([q("A")])}, lambda s, o: 1))
    assert len(seen) == 1 and list(seen[0]) == ["m"]
    assert kv(conn, "ai_quota_resets_at") is None


def test_exhaustion_marks_are_clamped_on_save_and_ignored_beyond_the_cap_on_load(conn):
    import time as _time

    add_material(conn, 1, "v1")
    now = _time.time()
    conn.execute("INSERT INTO kv(key, value) VALUES ('model_exhausted:fake:lejos', ?)",
                 (str(int(now + 10 * 86400)),))
    conn.execute("INSERT INTO kv(key, value) VALUES ('model_exhausted:fake:cerca', ?)",
                 (str(int(now + 3600)),))
    conn.commit()
    seen = []

    class Prov(FakeProvider):
        def analyze_video(self, youtube_id, title):
            seen.append(dict(self.exhausted_until))
            self.exhausted_until["nuevo"] = now + 10 * 86400  # se acota al guardar
            raise AIQuotaError()

    process_pending(conn, Prov({}, None))
    assert list(seen[0]) == ["cerca"]
    assert float(kv(conn, "model_exhausted:fake:nuevo")) <= _time.time() + 86400 + 3600
    assert kv(conn, "model_exhausted:fake:lejos") is None


def test_marks_are_kept_per_key(conn):
    add_material(conn, 1, "v1")
    conn.execute("INSERT INTO kv(key, value) VALUES ('model_exhausted:otraclave:m', '4000000000')")
    conn.commit()
    seen = []

    class Prov(FakeProvider):
        def analyze_video(self, youtube_id, title):
            seen.append(dict(self.exhausted_until))
            return super().analyze_video(youtube_id, title)

    process_pending(conn, Prov({"v1": analysis([q("A")])}, lambda s, o: 1))
    assert seen == [{}]  # las marcas de otra clave no cuentan
    assert kv(conn, "model_exhausted:otraclave:m") == "4000000000"  # ni se pisan


class RotatingClient:
    """Gemini falso por modelo: `script[modelo]` es la lista de respuestas de ese modelo."""

    def __init__(self, script):
        self.script, self.calls = script, []
        self.models = self

    def list(self):
        return []

    def generate_content(self, model, contents, config):
        self.calls.append(model)
        outcome = self.script[model].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(text=outcome)


def _daily_limit():
    from google.genai import errors

    return errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "25000s"}]}})


def test_gemini_rotation_marks_kv_and_the_next_run_respects_it(conn):
    add_material(conn, 1, "v1")
    add_material(conn, 2, "v2")
    payload = analysis([q("A", correct=1)]).model_dump_json()
    client = RotatingClient({
        "m": [payload, payload],
        "b": [_daily_limit()],
        "c": ['{"answers": [{"q": 1, "answer": 1}]}'] * 2,
    })

    def provider():
        return GeminiProvider("k", analyze_models=["m"], blind_models=["m", "b", "c"], client=client,
                              sleep=lambda s: None)

    assert process_pending(conn, provider(), budget=1) == 1
    assert client.calls == ["m", "b", "c"]  # la ciega nunca usa "m", que analizó
    key_id = provider().key_id
    assert len(key_id) == 12 and "k" != key_id
    assert float(kv(conn, f"model_exhausted:{key_id}:b")) > 0
    assert process_pending(conn, provider(), budget=1) == 1
    assert client.calls == ["m", "b", "c", "m", "c"]  # "b" agotado: ni se prueba


def test_stored_text_uses_student_notation(conn):
    add_material(conn, 1, "v1")
    question = GenQuestion(
        stem="¿Cuál es el vértice de `y = (x - 3)**2`?",
        options=["(3, 0)", "(-3, 0)", "(0, 9)", "(3, 9)"],
        correct_index=0,
        explanation="Con a*x0 ...: y = x**2 corrida 3",
        common_error="Tomar -3 en vez de 3",
        topic="Función Cuadrática",
        source_seconds=5,
        check_expr=None,
    )
    a = VideoAnalysis(summary_md="**Vértice**: `y = a*(x - h)**2 + k`",
                      topics=["Gráfica de la Función Cuadrática", "Función Cuadrática"],
                      questions=[question])
    prov = FakeProvider({"v1": a}, lambda s, o: 0)
    assert process_material(conn, prov, 1).kept == 1
    # La pasada a ciegas vio el texto original.
    assert prov.blind_batches[0][0] == [(question.stem, question.options)]
    row = conn.execute("SELECT * FROM question").fetchone()
    assert row["stem"] == "¿Cuál es el vértice de y = (x − 3)²?"
    assert json.loads(row["options_json"]) == ["(3, 0)", "(−3, 0)", "(0, 9)", "(3, 9)"]
    assert row["explanation"] == "Con a·x0 ...: y = x² corrida 3"
    assert row["common_error"] == "Tomar −3 en vez de 3"
    summary = conn.execute("SELECT summary_md FROM material").fetchone()[0]
    assert summary == "**Vértice**: y = a·(x − h)² + k"
    names = [r[0] for r in conn.execute("SELECT name FROM topic ORDER BY id")]
    assert names == ["Gráfica de la función cuadrática", "Función cuadrática"]


def test_check_expr_keeps_sympy_syntax(conn):
    add_material(conn, 1, "v1")
    question = GenQuestion(stem="¿Cuánto vale 2**3?", options=["6", "8", "9", "5"],
                           correct_index=1, explanation="2**3 = 8", common_error="2*3",
                           topic="Potencias", source_seconds=1, check_expr="2**3")
    prov = FakeProvider({"v1": analysis([question], topics=("Potencias",))}, lambda s, o: 1)
    assert process_material(conn, prov, 1).kept == 1
    row = conn.execute("SELECT stem, check_expr FROM question").fetchone()
    assert (row["stem"], row["check_expr"]) == ("¿Cuánto vale 2³?", "2**3")


def test_question_discarded_when_notation_merges_options(conn):
    add_material(conn, 1, "v1")
    clash = GenQuestion(stem="¿Cuál?", options=["x**2", "x^2", "2x", "x"], correct_index=2,
                        explanation="e", common_error="c", topic="Funciones lineales",
                        source_seconds=1)
    prov = FakeProvider({"v1": analysis([clash, q("ok")])}, lambda s, o: 2 if s == "¿Cuál?" else 1)
    res = process_material(conn, prov, 1)
    assert (res.kept, res.discarded) == (1, 1)
    assert [r[0] for r in conn.execute("SELECT stem FROM question")] == ["ok"]


def test_topic_dedup_across_title_and_sentence_case(conn):
    add_material(conn, 1, "v1")
    add_material(conn, 2, "v2")
    prov = FakeProvider(
        {
            "v1": analysis([q("a", topic="Función Lineal")], topics=["Función Lineal"]),
            "v2": analysis([q("b", topic="Función lineal")], topics=["Función lineal"]),
        },
        lambda stem, options: 1,
    )
    process_material(conn, prov, 1)
    process_material(conn, prov, 2)
    assert [tuple(r) for r in conn.execute("SELECT slug, name FROM topic")] == [
        ("funcion-lineal", "Función lineal")
    ]


# --- D2: enlace por defecto, descarga opcional y tope diario de video ---------------

def test_download_off_by_default_never_calls_ytdlp(conn, tmp_path):
    """Sin activar la descarga: el enlace rechazado deja cada material pidiendo un enlace
    público y nunca baja nada. Con 403 en dos videos la clave queda marcada y el tercero
    ya no gasta ningún pedido."""
    for n, ref in enumerate(["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"], start=1):
        add_material(conn, n, ref)
    downloads, client = [], DeniedYoutubeUrlClient()
    assert process_pending(conn, _gemini(tmp_path, client, calls=downloads)) == 0
    assert downloads == []
    assert client.url_calls == 2
    assert statuses(conn) == {1: "needs_public_link", 2: "needs_public_link", 3: "needs_public_link"}
    rows = conn.execute("SELECT error, fail_kind FROM material").fetchall()
    assert {(r["error"], r["fail_kind"]) for r in rows} == {(NEEDS_LINK_TEXT, None)}
    assert NEEDS_LINK_TEXT == ("Este video no es público. Pedile a la cátedra el archivo en el campus,"
                               " o activá la descarga en Ajustes")
    assert kv(conn, "ai_stop_reason") is None


def test_one_private_video_does_not_block_the_rest(conn, tmp_path):
    """Un 403 en un solo video deja ese material pidiendo enlace; los demás van por el enlace."""
    add_material(conn, 1, "aaaaaaaaaaa")
    add_material(conn, 2, "bbbbbbbbbbb")
    a = analysis([q("¿Cuánto es 1+1?", correct=1)])
    client = DeniedYoutubeUrlClient(analysis_text=a.model_dump_json(), denied={"aaaaaaaaaaa"})
    assert process_pending(conn, _gemini(tmp_path, client)) == 1
    assert statuses(conn) == {1: "needs_public_link", 2: "done"}
    assert kv(conn, "youtube_url_input") is None  # la clave no quedó marcada


def test_one_private_video_with_download_on_falls_back_to_download(conn, tmp_path):
    add_material(conn, 1, "aaaaaaaaaaa")
    add_material(conn, 2, "bbbbbbbbbbb")
    allow_download(conn)
    a = analysis([q("¿Cuánto es 1+1?", correct=1)])
    downloads = []
    client = DeniedYoutubeUrlClient(analysis_text=a.model_dump_json(), denied={"aaaaaaaaaaa"})
    assert process_pending(conn, _gemini(tmp_path, client, calls=downloads)) == 2
    assert downloads == ["aaaaaaaaaaa"]
    assert client.url_calls == 2
    assert kv(conn, "youtube_url_input") is None


def test_rejected_videos_add_up_across_runs_of_the_same_day(conn, tmp_path):
    add_material(conn, 1, "aaaaaaaaaaa")
    add_material(conn, 2, "bbbbbbbbbbb")
    client = DeniedYoutubeUrlClient()
    process_pending(conn, _gemini_at(tmp_path, client, BA_MORNING), budget=1, clock=lambda: BA_MORNING)
    assert kv(conn, "youtube_url_input") is None
    later = BA_MORNING + 3600
    process_pending(conn, _gemini_at(tmp_path, client, later), budget=1, clock=lambda: later)
    assert kv(conn, "youtube_url_input") == str(int(later + 7 * 86400))


def test_rejected_videos_of_another_day_do_not_count(conn, tmp_path):
    add_material(conn, 1, "aaaaaaaaaaa")
    add_material(conn, 2, "bbbbbbbbbbb")
    client = DeniedYoutubeUrlClient()
    process_pending(conn, _gemini_at(tmp_path, client, BA_MORNING), budget=1, clock=lambda: BA_MORNING)
    tomorrow = BA_MORNING + 86400
    process_pending(conn, _gemini_at(tmp_path, client, tomorrow), budget=1, clock=lambda: tomorrow)
    assert kv(conn, "youtube_url_input") is None
    assert kv(conn, "youtube_denied_videos:2026-10-17") is None  # se limpia


def test_download_on_is_passed_to_the_provider(conn, tmp_path):
    add_material(conn, 1, "aaaaaaaaaaa")
    allow_download(conn)
    a = analysis([q("¿Cuánto es 1+1?", correct=1)])
    downloads = []
    client = DeniedYoutubeUrlClient(analysis_text=a.model_dump_json())
    assert process_pending(conn, _gemini(tmp_path, client, calls=downloads)) == 1
    assert downloads == ["aaaaaaaaaaa"]


def test_needs_public_link_is_retried_by_retry_failed(conn):
    add_material(conn, 1, "aaaaaaaaaaa")
    add_material(conn, 2, "bbbbbbbbbbb")
    conn.execute("UPDATE material SET status='needs_public_link', error=? WHERE id=1", (NEEDS_LINK_TEXT,))
    conn.execute("UPDATE material SET status='failed', fail_kind='video' WHERE id=2")
    conn.commit()
    assert retry_failed(conn, ("format", "error")) == 1  # clave nueva: el enlace se vuelve a probar
    assert statuses(conn) == {1: "pending", 2: "failed"}
    assert conn.execute("SELECT error FROM material WHERE id=1").fetchone()[0] is None


def test_denied_expires_after_7_days(conn, tmp_path):
    """La marca de "enlace rechazado" vence a los 7 días: después se vuelve a probar el enlace."""
    for n in range(1, 5):
        add_material(conn, n, f"video{n:06d}")
    t0 = BA_MORNING
    client = DeniedYoutubeUrlClient()
    process_pending(conn, _gemini_at(tmp_path, client, t0), budget=2, clock=lambda: t0)
    assert kv(conn, "youtube_url_input") == str(int(t0 + 7 * 86400))
    assert client.url_calls == 2

    # Seis días después sigue rechazado: no se prueba el enlace.
    later = t0 + 6 * 86400
    process_pending(conn, _gemini_at(tmp_path, client, later), budget=1, clock=lambda: later)
    assert client.url_calls == 2

    # Pasados los 7 días se prueba de nuevo; un solo 403 ya no marca la clave.
    week = t0 + 7 * 86400 + 1
    process_pending(conn, _gemini_at(tmp_path, client, week), budget=1, clock=lambda: week)
    assert client.url_calls == 3
    assert kv(conn, "youtube_url_input") is None


def test_expired_denial_is_removed(conn):
    conn.execute("INSERT INTO kv(key, value) VALUES ('youtube_url_input', '1000')")
    conn.commit()
    process_pending(conn, FakeProvider({}, lambda s, o: 1), clock=lambda: 5000.0)
    assert kv(conn, "youtube_url_input") is None


def _gemini_at(tmp_path, client, now):
    return GeminiProvider("clave-falsa", analyze_models=["m"], blind_models=["b"], client=client,
                          sleep=lambda s: None, clock=lambda: now,
                          download=lambda *a: (_ for _ in ()).throw(AssertionError("no baja")),
                          video_dir=tmp_path / "videos")


# 2026-10-17 10:00 en Buenos Aires (UTC-3).
BA_MORNING = 1_792_242_000.0


def test_queue_stops_at_8h_per_day(conn):
    """Con 8 h de video mandadas por enlace en el día local, la cola para y sigue al día siguiente."""
    refs = [f"video{n:06d}" for n in range(1, 8)]
    for n, ref in enumerate(refs, start=1):
        add_material(conn, n, ref)
    analyses = {ref: analysis([q(f"P{ref}")]) for ref in refs}
    provider = FakeProvider(analyses, lambda s, o: 1, video_seconds=2 * 3600)

    assert process_pending(conn, provider, clock=lambda: BA_MORNING) == 4  # 4 x 2 h = 8 h
    assert provider.analyzed_refs == refs[:4]
    assert kv(conn, "ai_stop_reason") == "youtube_cap"
    # Vuelve a la medianoche del Pacífico (el día de la cuota de Gemini):
    # 2026-10-18 00:00 -07:00 = 04:00 en Buenos Aires.
    assert kv(conn, "ai_quota_resets_at") == str(int(BA_MORNING + 18 * 3600))
    assert kv(conn, "youtube_seconds:2026-10-17") == str(8 * 3600)

    # Pasada la medianoche de Buenos Aires sigue siendo el mismo día del Pacífico: nada.
    assert process_pending(conn, provider, clock=lambda: BA_MORNING + 14 * 3600 + 1) == 0
    assert provider.analyzed_refs == refs[:4]

    # Al día siguiente del Pacífico sigue la cola.
    assert process_pending(conn, provider, clock=lambda: BA_MORNING + 18 * 3600 + 1) == 3
    assert provider.analyzed_refs == refs
    assert kv(conn, "ai_stop_reason") is None


def test_cap_counter_uses_the_pacific_day(conn):
    """A las 2:00 de Buenos Aires todavía es el día anterior en el Pacífico: cuenta ahí."""
    add_material(conn, 1, "aaaaaaaaaaa")
    provider = FakeProvider({"aaaaaaaaaaa": analysis([q("x")])}, lambda s, o: 1, video_seconds=1800)
    early = BA_MORNING - 8 * 3600                    # 2026-10-17 02:00 -03:00 = 10-16 22:00 -07:00
    assert process_pending(conn, provider, clock=lambda: early) == 1
    assert kv(conn, "youtube_seconds:2026-10-16") == "1800"
    assert kv(conn, "youtube_measured:2026-10-16") == "1:1800"
    assert kv(conn, "youtube_seconds:2026-10-17") is None


def test_daily_cap_uses_one_hour_without_measurements(conn):
    """Sin duraciones medidas hoy se supone una hora: con 7 h 30 min usadas no se manda otro."""
    add_material(conn, 1, "aaaaaaaaaaa")
    conn.execute("INSERT INTO kv(key, value) VALUES ('youtube_seconds:2026-10-17', ?)", (str(7 * 3600 + 1800),))
    conn.commit()
    provider = FakeProvider({"aaaaaaaaaaa": analysis([q("x")])}, lambda s, o: 1)
    assert process_pending(conn, provider, clock=lambda: BA_MORNING) == 0
    assert provider.analyzed_refs == []
    assert kv(conn, "ai_stop_reason") == "youtube_cap"


def test_daily_cap_estimates_with_the_average_of_today(conn):
    """Con duraciones medidas hoy, se supone su promedio (acá 2 h 30 min, no 1 h)."""
    add_material(conn, 1, "aaaaaaaaaaa")
    conn.execute("INSERT INTO kv(key, value) VALUES ('youtube_seconds:2026-10-17', ?)", (str(6 * 3600),))
    conn.execute("INSERT INTO kv(key, value) VALUES ('youtube_measured:2026-10-17', ?)", (f"2:{5 * 3600}",))
    conn.commit()
    provider = FakeProvider({"aaaaaaaaaaa": analysis([q("x")])}, lambda s, o: 1)
    assert process_pending(conn, provider, clock=lambda: BA_MORNING) == 0
    assert kv(conn, "ai_stop_reason") == "youtube_cap"


def test_measured_durations_are_recorded(conn):
    add_material(conn, 1, "aaaaaaaaaaa")
    add_material(conn, 2, "bbbbbbbbbbb")
    provider = FakeProvider({"aaaaaaaaaaa": analysis([q("x")]), "bbbbbbbbbbb": analysis([q("y")])},
                            lambda s, o: 1, video_seconds=1800)
    assert process_pending(conn, provider, clock=lambda: BA_MORNING) == 2
    assert kv(conn, "youtube_measured:2026-10-17") == "2:3600"
    assert kv(conn, "youtube_seconds:2026-10-17") == "3600"


def test_unknown_duration_counts_the_estimate(conn):
    add_material(conn, 1, "aaaaaaaaaaa")
    provider = FakeProvider({"aaaaaaaaaaa": analysis([q("x")])}, lambda s, o: 1)  # sin duración
    assert process_pending(conn, provider, clock=lambda: BA_MORNING) == 1
    assert kv(conn, "youtube_seconds:2026-10-17") == "3600"
    assert kv(conn, "youtube_measured:2026-10-17") is None


def test_uploaded_videos_do_not_count_toward_the_cap(conn):
    """El tope copia el límite de YouTube por enlace: un video subido no cuenta."""
    add_material(conn, 1, "aaaaaaaaaaa")
    provider = FakeProvider({"aaaaaaaaaaa": analysis([q("x")])}, lambda s, o: 1,
                            video_seconds=7200, by_link=False)
    assert process_pending(conn, provider, clock=lambda: BA_MORNING) == 1
    assert kv(conn, "youtube_seconds:2026-10-17") is None


def test_with_the_link_denied_the_cap_does_not_block_uploads(conn):
    add_material(conn, 1, "aaaaaaaaaaa")
    conn.execute("INSERT INTO kv(key, value) VALUES ('youtube_seconds:2026-10-17', ?)", (str(8 * 3600),))
    conn.execute("INSERT INTO kv(key, value) VALUES ('youtube_url_input', ?)", (str(int(BA_MORNING + 86400)),))
    conn.commit()
    allow_download(conn)
    provider = FakeProvider({"aaaaaaaaaaa": analysis([q("x")])}, lambda s, o: 1, by_link=False)
    assert process_pending(conn, provider, clock=lambda: BA_MORNING) == 1


def test_link_read_with_an_unusable_answer_still_counts(conn):
    """Gemini leyó el video aunque la respuesta no sirva: cuenta lo supuesto."""
    add_material(conn, 1, "aaaaaaaaaaa")

    class Unusable(FakeProvider):
        def analyze_video(self, youtube_id, title):
            self.last_link_sent = True
            raise AIFormatError("la respuesta no es un análisis válido")

    process_pending(conn, Unusable({}, lambda s, o: 1), clock=lambda: BA_MORNING)
    assert statuses(conn) == {1: "failed"}
    assert kv(conn, "youtube_seconds:2026-10-17") == "3600"


def test_cap_still_finishes_paid_materials(conn):
    """Con el tope alcanzado se saltean los que mandarían video, pero los ya analizados terminan."""
    add_material(conn, 1, "aaaaaaaaaaa")
    add_material(conn, 2, "bbbbbbbbbbb")
    a = analysis([q("x")])
    conn.execute("UPDATE material SET analysis_json=?, analysis_model='m' WHERE id=2", (a.model_dump_json(),))
    conn.execute("INSERT INTO kv(key, value) VALUES ('youtube_seconds:2026-10-17', ?)", (str(8 * 3600),))
    conn.commit()
    provider = FakeProvider({}, lambda s, o: 1)
    assert process_pending(conn, provider, clock=lambda: BA_MORNING) == 1
    assert provider.analyzed_refs == []
    assert statuses(conn) == {1: "pending", 2: "done"}
    assert kv(conn, "ai_stop_reason") == "youtube_cap"
    assert kv(conn, "youtube_seconds:2026-10-17") == str(8 * 3600)


def test_old_daily_counters_are_cleaned(conn):
    conn.execute("INSERT INTO kv(key, value) VALUES ('youtube_seconds:2026-10-10', '100')")
    conn.execute("INSERT INTO kv(key, value) VALUES ('youtube_measured:2026-10-10', '1:100')")
    conn.commit()
    process_pending(conn, FakeProvider({}, lambda s, o: 1), clock=lambda: BA_MORNING)
    assert kv(conn, "youtube_seconds:2026-10-10") is None
    assert kv(conn, "youtube_measured:2026-10-10") is None


@pytest.mark.parametrize("options", [
    ["√2", "1.41", "2", "1"],
    ["sqrt(2)", "1,414", "2", "1"],
])
def test_rejects_distractor_the_typed_rule_would_accept(conn, options):
    """Opción múltiple dice "1.41" mal; escrita lo acepta: la pregunta se contradice y se descarta."""
    add_material(conn, 1, "v1")
    a = analysis([
        q_with("contradictoria", options, check_expr="sqrt(2)"),
        q_with("sana", ["√2", "1.4", "2", "1"], check_expr="sqrt(2)"),
    ])
    res = process_material(conn, FakeProvider({"v1": a}, lambda s, o: 0), 1)
    assert (res.kept, res.discarded) == (1, 1)
    assert [r["stem"] for r in conn.execute("SELECT stem FROM question")] == ["sana"]


def test_typed_acceptance_check_timeout_discards_the_question(conn, monkeypatch):
    from estudia.generation import pipeline

    monkeypatch.setattr(pipeline, "typed_accepts_option", lambda expr, option: None)
    add_material(conn, 1, "v1")
    a = analysis([q_with("indeciso", ["√2", "1.4", "2", "1"], check_expr="sqrt(2)"),
                  q_with("sin check", ["a", "b", "c", "d"])])
    res = process_material(conn, FakeProvider({"v1": a}, lambda s, o: 0), 1)
    assert [r["stem"] for r in conn.execute("SELECT stem FROM question")] == ["sin check"]


def test_question_without_check_expr_skips_the_typed_check(conn, monkeypatch):
    from estudia.generation import pipeline

    def boom(expr, option):
        raise AssertionError("no debería llamarse")

    monkeypatch.setattr(pipeline, "typed_accepts_option", boom)
    add_material(conn, 1, "v1")
    a = analysis([q_with("mcq", ["1.41", "√2", "2", "1"])])
    res = process_material(conn, FakeProvider({"v1": a}, lambda s, o: 0), 1)
    assert res.kept == 1


def test_old_analysis_json_without_segments_still_loads(conn):
    add_material(conn, 1, "v1")
    old = json.loads(analysis([q("A")]).model_dump_json())
    del old["segments"]
    conn.execute("UPDATE material SET analysis_json=? WHERE id=1", (json.dumps(old),))
    conn.commit()
    prov = FakeProvider({}, lambda s, o: 1)
    assert process_pending(conn, prov) == 1
    assert prov.analyzed_refs == []  # se reusó el guardado, no se pidió de nuevo
    row = conn.execute("SELECT status, segments_json FROM material WHERE id=1").fetchone()
    assert (row["status"], row["segments_json"]) == ("done", None)


def test_segments_saved_on_done(conn):
    add_material(conn, 1, "v1")
    add_material(conn, 2, "v2")
    segs = [Segment(start_s=0, end_s=60, title="Intro"), Segment(start_s=60, end_s=200, title="Recta")]
    full = analysis([q("A")]).model_copy(update={"segments": segs})
    prov = FakeProvider({"v1": full, "v2": analysis([q("B")])}, lambda s, o: 1)
    assert process_pending(conn, prov) == 2
    rows = {r["id"]: r["segments_json"] for r in conn.execute("SELECT id, segments_json FROM material")}
    assert json.loads(rows[1]) == [{"start_s": 0, "end_s": 60, "title": "Intro"},
                                   {"start_s": 60, "end_s": 200, "title": "Recta"}]
    assert rows[2] is None
