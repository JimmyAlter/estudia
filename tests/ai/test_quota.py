import datetime as dt

import pytest

from estudia.ai import quota
from estudia.db import connect
from fakes import FakeProvider


def _ts(iso: str) -> float:
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def _set(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)", (key, value))


def _keys(conn):
    return sorted(r[0] for r in conn.execute("SELECT key FROM kv WHERE key LIKE 'ai_used:%'"))


def test_budget_defaults_and_kv_override(conn):
    assert quota.daily_budget(conn, "analysis") == 40
    _set(conn, "ai_budget:analysis", "5")
    assert quota.daily_budget(conn, "analysis") == 5
    _set(conn, "ai_budget:analysis", "x")
    assert quota.daily_budget(conn, "analysis") == 40
    _set(conn, "ai_budget:analysis", "-3")
    assert quota.daily_budget(conn, "analysis") == 0


def test_default_pdf_budget_is_10(conn):
    assert quota.daily_budget(conn, "pdf") == 10
    _set(conn, "ai_budget:pdf", "3")
    assert quota.daily_budget(conn, "pdf") == 3


def test_unknown_function_raises(conn):
    with pytest.raises(ValueError):
        quota.daily_budget(conn, "nope")
    with pytest.raises(ValueError):
        quota.add_used(conn, "nope", 1, 0.0)


def test_usage_day_is_pacific(conn):
    quota.add_used(conn, "analysis", 1, _ts("2026-10-18T06:59:59Z"))
    quota.add_used(conn, "analysis", 1, _ts("2026-10-18T07:00:00Z"))
    assert _keys(conn) == ["ai_used:analysis:2026-10-17", "ai_used:analysis:2026-10-18"]


def test_usage_day_is_pacific_across_dst(conn):
    quota.add_used(conn, "analysis", 1, _ts("2026-11-02T07:59:59Z"))
    quota.add_used(conn, "analysis", 1, _ts("2026-11-02T08:00:00Z"))
    assert _keys(conn) == ["ai_used:analysis:2026-11-01", "ai_used:analysis:2026-11-02"]


def test_can_analyze_reserves_blind_for_already_analyzed(conn):
    now = _ts("2026-10-18T12:00:00Z")
    _set(conn, "ai_budget:blind", "2")
    quota.add_used(conn, "blind", 1, now)
    assert quota.remaining(conn, "blind", now) == 1
    assert quota.can_analyze(conn, now, awaiting_blind=4) is False
    assert quota.can_analyze(conn, now, awaiting_blind=3) is True


def test_can_analyze_needs_analysis_budget(conn):
    now = _ts("2026-10-18T12:00:00Z")
    _set(conn, "ai_budget:analysis", "0")
    assert quota.can_analyze(conn, now, awaiting_blind=0) is False


def test_blind_requests_needed():
    assert [quota.blind_requests_needed(n) for n in (1, 4, 5, 8, 9)] == [1, 1, 2, 2, 3]


def test_record_usage_adds_and_clears_provider_counts(conn):
    now = _ts("2026-10-18T12:00:00Z")
    p = FakeProvider([], [])
    p.requests = {"analysis": 3, "blind": 1}
    quota.record_usage(conn, p, now)
    assert quota.used_today(conn, "analysis", now) == 3
    assert quota.used_today(conn, "blind", now) == 1
    assert p.requests == {"analysis": 0, "blind": 0}
    p.requests["analysis"] = 2
    quota.record_usage(conn, p, now)
    assert quota.used_today(conn, "analysis", now) == 5
    quota.record_usage(conn, object(), now)  # sin `requests`: no falla


def test_prune_keeps_only_today(conn):
    today = _ts("2026-10-18T12:00:00Z")
    quota.add_used(conn, "analysis", 1, today - 86400)
    quota.add_used(conn, "blind", 2, today)
    _set(conn, "ai_budget:analysis", "5")
    quota.prune_usage(conn, today)
    assert _keys(conn) == ["ai_used:blind:2026-10-18"]
    assert quota.daily_budget(conn, "analysis") == 5


def test_can_analyze_document_needs_pdf_budget(conn):
    now = _ts("2026-10-18T12:00:00Z")
    _set(conn, "ai_budget:pdf", "1")
    assert quota.can_analyze_document(conn, now, awaiting_blind=0) is True
    quota.add_used(conn, "pdf", 1, now)
    assert quota.can_analyze_document(conn, now, awaiting_blind=0) is False


def test_can_analyze_document_ignores_video_analysis_budget(conn):
    now = _ts("2026-10-18T12:00:00Z")
    _set(conn, "ai_budget:analysis", "0")
    assert quota.can_analyze_document(conn, now, awaiting_blind=0) is True


def test_can_analyze_document_reserves_blind_for_already_analyzed(conn):
    now = _ts("2026-10-18T12:00:00Z")
    _set(conn, "ai_budget:blind", "2")
    quota.add_used(conn, "blind", 1, now)
    assert quota.can_analyze_document(conn, now, awaiting_blind=4) is False
    assert quota.can_analyze_document(conn, now, awaiting_blind=3) is True
