"""Reintento del procesamiento cuando Gemini está saturado o sin cuota del día."""
import pytest

from estudia.ai.base import AIQuotaError, AIUnavailableError, GenQuestion, VideoAnalysis
from estudia.secrets import set_secret
from estudia.server.app import POLL_S, SYNC_EVERY_S, Jobs, _kv_set, periodic_sync
from fakes import FakeProvider

SITE_ID = "utn-frba"


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def analysis():
    q = GenQuestion(stem="¿2 + 2?", options=["3", "4", "5", "6"], correct_index=1,
                    explanation="e", common_error="c", topic="Sumas", source_seconds=1)
    return VideoAnalysis(summary_md="# R", topics=["Sumas"], questions=[q])


class ScriptedProvider(FakeProvider):
    """Cada análisis toma el próximo resultado del guion: una excepción o éxito."""

    def __init__(self, script):
        super().__init__({}, lambda s, o: 1)
        self.script = script

    def analyze_video(self, youtube_id, title):
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        self.last_analysis_model = "analizador"
        return analysis()


@pytest.fixture
def setup(tmp_path):
    clock = Clock()
    state = {"script": [], "moodle_calls": 0}

    def moodle_factory(url, token):
        state["moodle_calls"] += 1
        raise RuntimeError("sin Moodle en este test")

    jobs = Jobs(tmp_path / "estudia.db", moodle_factory,
                lambda key: ScriptedProvider(state["script"]), clock=clock)
    conn = jobs.connect()
    conn.execute("INSERT INTO section(id, course_id, num, name, visible) VALUES (1, 10, 1, 'S', 1)")
    for mid in range(1, 6):
        conn.execute("INSERT INTO material(id, section_id, cmid, kind, title, ref, status)"
                     " VALUES (?, 1, ?, 'youtube', 'V', ?, 'pending')", (mid, 100 + mid, f"v{mid}"))
    _kv_set(conn, "site_id", SITE_ID)
    _kv_set(conn, "last_sync_at", str(int(clock())))  # la sincronización no toca todavía
    conn.close()
    set_secret(f"moodle_token:{SITE_ID}", "tok")
    set_secret("gemini_key", "clave")
    state.update(jobs=jobs, clock=clock)
    return state


def test_backoff_doubles_up_to_thirty_minutes(setup):
    jobs, clock = setup["jobs"], setup["clock"]
    waits = []
    for _ in range(5):
        setup["script"].append(AIUnavailableError("saturado"))
        assert jobs.run_process_now()
        waits.append(jobs.retry_at - clock())
        clock.now = jobs.retry_at
    assert waits == [300, 600, 1200, 1800, 1800]
    assert setup["moodle_calls"] == 0  # el reintento no sincroniza


def test_backoff_restarts_after_a_successful_material(setup):
    jobs, clock = setup["jobs"], setup["clock"]
    setup["script"].extend([AIUnavailableError(), AIUnavailableError()])
    jobs.run_process_now()
    jobs.run_process_now()
    assert jobs.retry_at - clock() == 600
    setup["script"].extend([analysis(), AIUnavailableError()])  # sale una, se satura otra vez
    jobs.run_process_now()
    assert jobs.retry_at - clock() == 300


def test_nothing_scheduled_when_processing_ends_fine(setup):
    jobs = setup["jobs"]
    setup["script"].extend([analysis()] * 5)
    jobs.run_process_now()
    assert jobs.retry_at is None


def test_daily_quota_waits_until_the_first_model_comes_back(setup):
    jobs, clock = setup["jobs"], setup["clock"]
    setup["script"].append(AIQuotaError(resets_at=clock() + 7200))
    jobs.run_process_now()
    assert jobs.retry_at == clock() + 7200


def test_per_minute_quota_keeps_waiting_for_the_next_sync(setup):
    jobs = setup["jobs"]
    setup["script"].append(AIQuotaError())  # sin hora de vuelta
    jobs.run_process_now()
    assert jobs.retry_at is None


def test_rejected_key_schedules_nothing(setup):
    from estudia.ai.base import AIAuthError

    jobs = setup["jobs"]
    setup["script"].append(AIAuthError())
    jobs.run_process_now()
    assert jobs.retry_at is None


def test_retry_does_not_run_while_another_run_holds_the_lock(setup):
    jobs = setup["jobs"]
    assert jobs.try_start()
    assert jobs.run_process_now() is False
    jobs.finish()


def run_loop(setup, max_sleeps):
    """Corre el ciclo periódico con un sleep falso que adelanta el reloj."""
    jobs, clock = setup["jobs"], setup["clock"]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock.now += seconds
        return len(sleeps) >= max_sleeps

    import threading

    periodic_sync(jobs, threading.Event(), sleep=sleep)
    return sleeps


def test_periodic_loop_retries_processing_at_the_scheduled_time(setup):
    jobs, clock = setup["jobs"], setup["clock"]
    start = clock()
    setup["script"].append(AIUnavailableError())
    jobs.run_process_now()  # una corrida pedida desde la interfaz se cortó
    assert jobs.retry_at == start + 300
    setup["script"].extend([analysis()] * 5)
    sleeps = run_loop(setup, max_sleeps=7)
    # Se despierta cada POLL_S hasta la hora del reintento y procesa sin sincronizar.
    assert sleeps[:5] == [POLL_S] * 5
    assert setup["script"] == []  # se procesaron los 5 materiales pendientes
    assert jobs.retry_at is None
    assert setup["moodle_calls"] == 0


def test_periodic_loop_still_syncs_when_due(setup):
    jobs, clock = setup["jobs"], setup["clock"]
    conn = jobs.connect()
    _kv_set(conn, "last_sync_at", str(int(clock() - SYNC_EVERY_S)))
    conn.close()
    setup["script"].extend([analysis()] * 5)
    run_loop(setup, max_sleeps=1)
    assert setup["moodle_calls"] == 1


def test_periodic_loop_waits_while_busy(setup):
    jobs, clock = setup["jobs"], setup["clock"]
    jobs.retry_at = clock()  # ya toca
    assert jobs.try_start()  # otra corrida en curso
    sleeps = run_loop(setup, max_sleeps=2)
    jobs.finish()
    assert sleeps == [POLL_S, POLL_S]
    assert setup["moodle_calls"] == 0
