import logging
import multiprocessing
import operator
import sys
import threading
import time

import pytest

from estudia.material import sandbox
from estudia.material.sandbox import SandboxFailed, SandboxTimeout, run_limited


def _sandbox_children():
    return [p for p in multiprocessing.active_children() if p.name == sandbox.PROCESS_NAME]


def test_run_limited_returns_result():
    assert run_limited(operator.add, 2, 3, timeout_s=20) == 5


def test_run_limited_kills_on_timeout():
    start = time.monotonic()
    with pytest.raises(SandboxTimeout):
        run_limited(sandbox._test_sleep, 30, timeout_s=0.5)
    assert time.monotonic() - start < 6
    assert _sandbox_children() == []


def test_next_call_works_after_timeout():
    with pytest.raises(SandboxTimeout):
        run_limited(sandbox._test_sleep, 30, timeout_s=0.5)
    assert run_limited(operator.mul, 6, 7, timeout_s=20) == 42


def test_child_exception_becomes_failed_with_type_name_only():
    with pytest.raises(SandboxFailed) as info:
        run_limited(operator.truediv, 1, 0, timeout_s=20)
    assert str(info.value) == "ZeroDivisionError"


def test_child_that_dies_is_failed():
    with pytest.raises(SandboxFailed) as info:
        run_limited(sandbox._test_die, timeout_s=20)
    assert str(info.value) == "muerto"
    assert run_limited(operator.add, 1, 1, timeout_s=20) == 2


@pytest.mark.skipif(sys.platform == "win32", reason="el tope de memoria es solo POSIX")
def test_memory_cap_applies_in_child():
    with pytest.raises(SandboxFailed) as info:
        run_limited(bytearray, 2_000_000_000, timeout_s=20, memory_mb=768)
    assert str(info.value) == "MemoryError"
    # El proceso del test no quedó limitado.
    assert len(bytearray(300_000_000)) == 300_000_000


@pytest.mark.skipif(sys.platform == "win32", reason="el tope de memoria es solo POSIX")
def test_memory_cap_respects_system_hard_limit(monkeypatch):
    import resource

    calls = []
    hard = 512 * 1024 * 1024
    monkeypatch.setattr(resource, "getrlimit", lambda which: (hard, hard))
    monkeypatch.setattr(resource, "setrlimit", lambda which, limits: calls.append((which, limits)))
    sandbox._limit_memory(1536)
    assert (resource.RLIMIT_AS, (hard, hard)) in calls

    calls.clear()
    monkeypatch.setattr(
        resource, "getrlimit", lambda which: (resource.RLIM_INFINITY, resource.RLIM_INFINITY)
    )
    sandbox._limit_memory(1536)
    cap = 1536 * 1024 * 1024
    assert (resource.RLIMIT_AS, (cap, cap)) in calls


@pytest.mark.skipif(sys.platform == "win32", reason="el tope de memoria es solo POSIX")
def test_memory_cap_that_cannot_be_set_only_warns(monkeypatch, caplog):
    import resource

    def refuse(which, limits):
        raise ValueError("no")

    monkeypatch.setattr(resource, "getrlimit", lambda which: (resource.RLIM_INFINITY,) * 2)
    monkeypatch.setattr(resource, "setrlimit", refuse)
    with caplog.at_level(logging.WARNING, logger=sandbox.__name__):
        sandbox._limit_memory(1536)  # no lanza
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_at_most_two_children_at_once():
    results = []

    def job():
        results.append(run_limited(sandbox._test_sleep, 1, timeout_s=20))

    threads = [threading.Thread(target=job) for _ in range(3)]
    start = time.monotonic()
    for t in threads:
        t.start()
    peak = 0
    while any(t.is_alive() for t in threads):
        peak = max(peak, len(_sandbox_children()))
        time.sleep(0.02)
    for t in threads:
        t.join()
    assert results == [None, None, None]
    assert peak <= sandbox.MAX_CONCURRENT
    assert time.monotonic() - start >= 2  # el tercero esperó un lugar


def test_waiting_for_a_slot_counts_toward_timeout():
    threads = [
        threading.Thread(target=run_limited, args=(sandbox._test_sleep, 3), kwargs={"timeout_s": 20})
        for _ in range(sandbox.MAX_CONCURRENT)
    ]
    for t in threads:
        t.start()
    time.sleep(0.3)  # los dos lugares quedan ocupados
    start = time.monotonic()
    with pytest.raises(SandboxTimeout):
        run_limited(operator.add, 1, 1, timeout_s=0.5)
    assert time.monotonic() - start < 1.5
    for t in threads:
        t.join()


def test_no_free_slot_raises_sandbox_busy():
    """Sin lugar libre no corrió ningún hijo: es `SandboxBusy`, distinto de un hijo colgado."""
    from estudia.material.sandbox import SandboxBusy

    taken = 0
    try:
        while sandbox._slots.acquire(blocking=False):
            taken += 1
        start = time.monotonic()
        with pytest.raises(SandboxBusy):
            run_limited(operator.add, 1, 1, timeout_s=0.2)
        assert time.monotonic() - start < 1
    finally:
        for _ in range(taken):
            sandbox._slots.release()
    assert run_limited(operator.add, 1, 1, timeout_s=20) == 2
