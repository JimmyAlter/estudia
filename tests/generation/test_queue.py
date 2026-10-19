"""Cola por fecha de examen y presupuesto diario de pedidos en la tubería."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from estudia.ai.base import AIQuotaError, AIUnavailableError, GenQuestion, VideoAnalysis
from estudia.ai.quota import quota_day
from estudia.db import connect
from estudia.generation.pipeline import (
    BUDGET,
    QueueItem,
    next_cap_reset,
    pending_queue,
    process_pending,
)
from fakes import FakeProvider

BA = ZoneInfo("America/Argentina/Buenos_Aires")
# 18/10/2026 03:00 en Buenos Aires: en el Pacífico todavía es el 17.
NOW = int(datetime(2026, 10, 18, 3, 0, tzinfo=BA).timestamp())
DAY = 86400


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    c.execute("INSERT INTO section(id, course_id, num, name, visible) VALUES (1, 10, 1, 'A1', 1)")
    c.execute("INSERT INTO section(id, course_id, num, name, visible) VALUES (2, 20, 5, 'B5', 1)")
    c.execute("INSERT INTO section(id, course_id, num, name, visible) VALUES (3, 10, 2, 'A2', 1)")
    c.execute("INSERT INTO section(id, course_id, num, name, visible) VALUES (4, 20, 0, 'Oculta', 0)")
    c.commit()
    return c


def add_material(conn, mid, section_id=1, analysis_json=None):
    conn.execute(
        "INSERT INTO material(id, section_id, cmid, kind, title, ref, status, analysis_json,"
        " analysis_model) VALUES (?, ?, ?, 'youtube', ?, ?, 'pending', ?, ?)",
        (mid, section_id, 1000 + mid, f"Video {mid}", f"v{mid}", analysis_json,
         "analizador" if analysis_json else None),
    )
    conn.commit()


def add_exam(conn, eid, course_id, opens_at):
    conn.execute(
        "INSERT INTO exam(id, course_id, cmid, name, opens_at, closes_at, attempts, time_limit_s,"
        " max_grade) VALUES (?, ?, ?, 'Parcial', ?, 0, 1, 3600, 10)",
        (eid, course_id, eid, opens_at),
    )
    conn.commit()


def set_kv(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)", (key, value))
    conn.commit()


def kv(conn, key):
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def analysis(stem):
    q = GenQuestion(stem=stem, options=["1", "2", "3", "4"], correct_index=1, explanation="e",
                    common_error="c", topic="Funciones", source_seconds=5)
    return VideoAnalysis(summary_md="# R", topics=["Funciones"], questions=[q])


def statuses(conn):
    return {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM material")}


def clock():
    return float(NOW)


def test_queue_next_exam_course_first(conn):
    add_material(conn, 1, section_id=1)   # curso A (10), sección 1
    add_material(conn, 2, section_id=2)   # curso B (20), sección 5
    add_material(conn, 3, section_id=3)   # curso A, sección 2
    add_exam(conn, 1, 20, NOW + 5 * DAY)
    add_exam(conn, 2, 10, NOW + 20 * DAY)
    queue = pending_queue(conn, NOW, 10)
    assert [item.material_id for item in queue] == [2, 1, 3]
    assert queue[0] == QueueItem(2, False, NOW + 5 * DAY)


def test_queue_course_with_exam_before_course_without(conn):
    add_material(conn, 1, section_id=1)
    add_material(conn, 2, section_id=2)
    add_exam(conn, 1, 20, NOW + 5 * DAY)
    assert [item.material_id for item in pending_queue(conn, NOW, 10)] == [2, 1]


def test_queue_without_exams_keeps_section_order(conn):
    add_material(conn, 2, section_id=2)
    add_material(conn, 3, section_id=3)
    add_material(conn, 1, section_id=1)
    add_material(conn, 9, section_id=4)   # sección oculta
    queue = pending_queue(conn, NOW, 10)
    assert [item.material_id for item in queue] == [1, 3, 2]
    assert all(item.exam_at is None for item in queue)


def test_queue_ignores_past_exams(conn):
    add_material(conn, 1, section_id=1)
    add_material(conn, 2, section_id=2)
    add_exam(conn, 1, 20, NOW - DAY)
    add_exam(conn, 2, 20, NOW)            # empieza ahora: ya no es futuro
    assert [(i.material_id, i.exam_at) for i in pending_queue(conn, NOW, 10)] == [(1, None), (2, None)]


def test_queue_counts_an_open_exam_as_next(conn):
    """Un examen abierto (empezó pero cierra después de `now`) sigue siendo el próximo, como en el inicio."""
    add_material(conn, 1, section_id=1)
    add_material(conn, 2, section_id=2)
    add_exam(conn, 1, 10, NOW + 5 * DAY)
    add_exam(conn, 2, 20, NOW - 3600)
    conn.execute("UPDATE exam SET closes_at = ? WHERE id = 2", (NOW + DAY,))
    conn.commit()
    assert [(i.material_id, i.exam_at) for i in pending_queue(conn, NOW, 10)] == \
        [(2, NOW - 3600), (1, NOW + 5 * DAY)]


def test_queue_uses_the_nearest_future_exam_and_limit(conn):
    add_material(conn, 1, section_id=1)
    add_material(conn, 2, section_id=2, analysis_json=analysis("x").model_dump_json())
    add_exam(conn, 1, 20, NOW + 30 * DAY)
    add_exam(conn, 2, 20, NOW + 3 * DAY)
    assert pending_queue(conn, NOW, 1) == [QueueItem(2, True, NOW + 3 * DAY)]


def test_process_pending_follows_the_exam_queue(conn):
    add_material(conn, 1, section_id=1)
    add_material(conn, 2, section_id=2)
    add_exam(conn, 1, 20, NOW + 5 * DAY)
    fake = FakeProvider({"v1": analysis("a"), "v2": analysis("b")}, lambda s, o: 1)
    assert process_pending(conn, fake, budget=1, clock=clock) == 1
    assert fake.analyzed_refs == ["v2"]


def test_analysis_budget_stops_with_reason_budget(conn):
    set_kv(conn, "ai_budget:analysis", "1")
    add_material(conn, 1)
    add_material(conn, 3, section_id=3)
    fake = FakeProvider({"v1": analysis("a"), "v3": analysis("b")}, lambda s, o: 1)
    # La pasada a ciegas igual corre: para eso está la reserva.
    assert process_pending(conn, fake, clock=clock) == 1
    assert fake.analyzed_refs == ["v1"]
    assert statuses(conn) == {1: "done", 3: "pending"}
    assert kv(conn, "ai_stop_reason") == BUDGET == "budget"
    assert int(kv(conn, "ai_quota_resets_at")) == int(next_cap_reset(NOW))
    pacific_midnight = datetime(2026, 10, 18, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert int(kv(conn, "ai_quota_resets_at")) == int(pacific_midnight.timestamp())


def test_budget_already_spent_today_counts(conn):
    set_kv(conn, "ai_budget:analysis", "3")
    set_kv(conn, f"ai_used:analysis:{quota_day(NOW).isoformat()}", "3")
    add_material(conn, 1)
    fake = FakeProvider({"v1": analysis("a")}, lambda s, o: 1)
    assert process_pending(conn, fake, clock=clock) == 0
    assert fake.analyzed_refs == []
    assert kv(conn, "ai_stop_reason") == "budget"


def test_analysis_held_back_to_keep_blind_reserve(conn):
    set_kv(conn, "ai_budget:blind", "0")
    paid = analysis("pagada").model_dump_json()
    add_material(conn, 1, analysis_json=paid)
    add_material(conn, 3, section_id=3)
    fake = FakeProvider({"v3": analysis("b")}, lambda s, o: 1)
    assert process_pending(conn, fake, clock=clock) == 0
    assert fake.analyzed_refs == []
    assert fake.blind_batches == []
    row = conn.execute("SELECT status, analysis_json FROM material WHERE id=1").fetchone()
    assert (row["status"], row["analysis_json"]) == ("pending", paid)
    assert statuses(conn) == {1: "pending", 3: "pending"}
    assert kv(conn, "ai_stop_reason") == "budget"


def test_reserve_counts_classes_awaiting_blind(conn):
    # Con 1 pedido a ciegas: 3 pagados + 1 nuevo entran en un lote; con 4 pagados, no.
    set_kv(conn, "ai_budget:blind", "1")
    blob = analysis("pagada").model_dump_json()
    for mid in (1, 2, 4):
        add_material(conn, mid, analysis_json=blob)
    add_material(conn, 5)
    add_material(conn, 6)
    fake = FakeProvider({"v5": analysis("c"), "v6": analysis("d")}, lambda s, o: 1)
    assert process_pending(conn, fake, clock=clock) == 4
    assert fake.analyzed_refs == ["v5"]
    assert len(fake.blind_batches) == 1
    assert statuses(conn)[6] == "pending"


def test_blind_budget_stops_between_batches(conn):
    set_kv(conn, "ai_budget:blind", "1")
    blob = analysis("pagada").model_dump_json()
    for mid in range(1, 7):
        add_material(conn, mid, analysis_json=blob)
    fake = FakeProvider({}, lambda s, o: 1)
    assert process_pending(conn, fake, clock=clock) == 4
    assert len(fake.blind_batches) == 1
    assert kv(conn, "ai_stop_reason") == "budget"
    assert conn.execute("SELECT COUNT(*) FROM material WHERE status='pending'"
                        " AND analysis_json IS NOT NULL").fetchone()[0] == 2


def test_usage_recorded_per_pacific_day(conn):
    old_day = "2026-10-16"
    set_kv(conn, f"ai_used:analysis:{old_day}", "7")
    add_material(conn, 1)
    add_material(conn, 3, section_id=3)
    fake = FakeProvider({"v1": analysis("a"), "v3": analysis("b")}, lambda s, o: 1)
    assert process_pending(conn, fake, clock=clock) == 2
    assert quota_day(NOW).isoformat() == "2026-10-17"   # en Buenos Aires ya es el 18
    assert kv(conn, "ai_used:analysis:2026-10-17") == "2"
    assert kv(conn, "ai_used:blind:2026-10-17") == "1"
    assert kv(conn, "ai_used:analysis:2026-10-18") is None
    assert kv(conn, f"ai_used:analysis:{old_day}") is None
    assert fake.requests == {"analysis": 0, "blind": 0, "pdf": 0}


def test_queue_ignores_enrollment(conn):
    add_material(conn, 1, section_id=1)
    add_material(conn, 2, section_id=2)
    conn.execute("INSERT INTO exam(id, course_id, name, opens_at, source, kind) "
                 "VALUES (9, 20, 'Inscripción', ?, 'manual', 'inscripcion')", (NOW + DAY,))
    conn.commit()
    items = pending_queue(conn, NOW, 10)
    assert [(i.material_id, i.exam_at) for i in items] == [(1, None), (2, None)]
    add_exam(conn, 8, 20, NOW + 5 * DAY)
    assert [i.material_id for i in pending_queue(conn, NOW, 10)] == [2, 1]


@pytest.mark.parametrize("exc, reason", [
    (AIUnavailableError(), "unavailable"),
    (AIQuotaError(resets_at=NOW + 777), "quota"),
])
def test_phase2_budget_does_not_overwrite_a_phase1_stop(conn, exc, reason):
    paid = analysis("pagada").model_dump_json()
    add_material(conn, 1, analysis_json=paid)
    add_material(conn, 3, section_id=3)

    class Failing(FakeProvider):
        def analyze_video(self, youtube_id, title):
            self.requests["blind"] += 1   # el pedido que gastó lo que quedaba a ciegas del día
            raise exc

    set_kv(conn, "ai_budget:blind", "1")
    fake = Failing({}, lambda s, o: 1)
    process_pending(conn, fake, clock=clock)
    assert fake.blind_batches == []
    assert kv(conn, "ai_stop_reason") == reason
    if reason == "quota":
        assert int(kv(conn, "ai_quota_resets_at")) == NOW + 777


def test_usage_is_recorded_when_the_run_exits_through_ai_blocked(conn):
    from estudia.ai.gate import AIBlocked
    from estudia.ai.quota import used_today

    add_material(conn, 1)

    class Blocked(FakeProvider):
        def analyze_video(self, youtube_id, title):
            self.requests["analysis"] += 1   # el pedido salió y después la puerta se cerró
            raise AIBlocked()

    with pytest.raises(AIBlocked):
        process_pending(conn, Blocked({}, lambda s, o: 1), clock=clock)
    assert used_today(conn, "analysis", NOW) == 1
    assert statuses(conn) == {1: "pending"}


def test_usage_is_recorded_when_the_run_exits_through_an_unexpected_error(conn, monkeypatch):
    from estudia.ai.quota import used_today
    from estudia.generation import pipeline

    add_material(conn, 1)

    def boom(*args, **kwargs):
        raise RuntimeError("inesperado")

    monkeypatch.setattr(pipeline, "paid_batches", boom)
    fake = FakeProvider({"v1": analysis("a")}, lambda s, o: 1)
    with pytest.raises(RuntimeError):
        process_pending(conn, fake, clock=clock)
    assert fake.requests == {"analysis": 0, "blind": 0, "pdf": 0}   # ya se pasó a kv
    assert used_today(conn, "analysis", NOW) == 1
