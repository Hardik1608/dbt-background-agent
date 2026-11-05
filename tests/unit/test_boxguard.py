"""Shared-box guards: heavy-job lock and RAM checks."""

from __future__ import annotations

import json
import os
import time

import pytest

from dbt_agent.bench.boxguard import (
    HeavyJobLock,
    LockPacer,
    admission,
    sandbox_slots,
    wait_for_memory,
)


def test_lock_is_exclusive_and_released(tmp_path):
    path = tmp_path / ".heavy_job.lock"
    first = HeavyJobLock("p1", path=path)
    second = HeavyJobLock("p2", path=path)
    assert first.try_acquire()
    assert not second.try_acquire()
    assert json.loads((path / "owner.json").read_text())["project"] == "p1"
    first.release()
    assert not path.exists()
    assert second.try_acquire()
    second.release()


def test_release_never_removes_someone_elses_lock(tmp_path):
    path = tmp_path / ".heavy_job.lock"
    mine = HeavyJobLock("me", path=path)
    assert mine.try_acquire()
    (path / "owner.json").write_text(json.dumps({"project": "other", "token": "x"}))
    mine.release()
    assert path.exists()


def test_stale_lock_is_broken(tmp_path):
    path = tmp_path / ".heavy_job.lock"
    path.mkdir()
    (path / "owner.json").write_text(
        json.dumps(
            {
                "project": "dead",
                "pid": 999_999_9,
                "host": os.uname().nodename
                if hasattr(os, "uname")
                else __import__("socket").gethostname(),
                "started_epoch": time.time() - 4 * 3600,
            }
        )
    )
    lock = HeavyJobLock("p", path=path, sleep=lambda s: None, log=lambda m: None)
    assert lock.is_stale()
    lock.acquire(timeout_s=5)
    assert lock.held
    lock.release()


def test_fresh_lock_is_waited_for(tmp_path):
    path = tmp_path / ".heavy_job.lock"
    holder = HeavyJobLock("busy", path=path)
    assert holder.try_acquire()
    waits = []
    waiter = HeavyJobLock(
        "me",
        path=path,
        poll_s=1,
        log=lambda m: None,
        sleep=lambda s: (waits.append(s), holder.release()),
    )
    waiter.acquire(timeout_s=30)
    assert waits == [1] and waiter.held
    waiter.release()


def test_wait_for_memory_polls_until_enough():
    readings = iter([1000, 2000, 4000])
    seen = wait_for_memory(
        3072, reader=lambda: next(readings), poll_s=0, log=lambda m: None, sleep=lambda s: None
    )
    assert seen == 4000


def test_wait_for_memory_times_out():
    with pytest.raises(TimeoutError):
        wait_for_memory(
            3072,
            reader=lambda: 100,
            poll_s=0,
            timeout_s=0,
            log=lambda m: None,
            sleep=lambda s: None,
        )


def test_sandbox_slots():
    assert sandbox_slots(3500, requested=2) == 1
    assert sandbox_slots(6000, requested=2) == 2
    assert sandbox_slots(None, requested=1) == 1


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_pacer_yields_after_a_long_streak_of_back_to_back_tasks():
    clock = FakeClock()
    pacer = LockPacer(max_hold_s=600, yield_s=310, clock=clock, sleep=clock.sleep)
    for _ in range(5):  # five 4-minute tasks, lock re-taken immediately each time
        pacer.before_acquire(log=lambda m: None)
        pacer.acquired(waited=False)
        clock.now += 240
        pacer.released()
    assert clock.slept == [310]  # one yield after 12 minutes of holding
    assert pacer.yields == 1


def test_pacer_streak_restarts_after_waiting_or_a_gap():
    clock = FakeClock()
    pacer = LockPacer(max_hold_s=600, yield_s=310, clock=clock, sleep=clock.sleep)
    pacer.before_acquire(log=lambda m: None)
    pacer.acquired(waited=False)
    clock.now += 590
    pacer.released()
    clock.now += 60  # a gap: others could take the lock meanwhile
    pacer.before_acquire(log=lambda m: None)
    pacer.acquired(waited=False)
    clock.now += 590
    pacer.released()
    pacer.before_acquire(log=lambda m: None)
    pacer.acquired(waited=True)  # someone else held it in between
    clock.now += 500
    pacer.released()
    pacer.before_acquire(log=lambda m: None)
    assert clock.slept == []


def test_admission_releases_the_lock_and_records_the_release(tmp_path, monkeypatch):
    from dbt_agent.bench import boxguard

    clock = FakeClock()
    pacer = LockPacer(max_hold_s=600, yield_s=310, clock=clock, sleep=clock.sleep)
    lock_path = tmp_path / "lock"

    class Lock(HeavyJobLock):
        def __init__(self, project, **kwargs):
            super().__init__(project, path=lock_path, **kwargs)

    monkeypatch.setattr(boxguard, "HeavyJobLock", Lock)
    with admission("p", required_mb=None, lock=True, log=lambda m: None, pacer=pacer):
        assert lock_path.exists()
    assert not lock_path.exists() and pacer.last_release == 0.0
