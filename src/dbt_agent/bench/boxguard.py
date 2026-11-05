"""Good-citizen guards for the shared GPU box (rules in PROGRESS.md / _shared/BOX_USAGE.md).

* ``HeavyJobLock`` - the box-wide lock for heavy jobs: ``mkdir`` of
  ``~/Hardik/Extras/.heavy_job.lock`` is atomic, so success means we hold it. An ``owner``
  file inside names the project, PID, host and start time. A lock older than 3 hours whose
  PID is gone is stale and may be broken. Released on exit (context manager, atexit and
  SIGTERM), and only by its owner.
* ``wait_for_memory`` - before starting a sandbox container, "available" RAM must be at
  least the container cap + 1024 MB; otherwise wait.
* ``sandbox_slots`` - one sandbox at a time unless at least 5 GB is available.
* ``LockPacer`` - fair sharing: a batch takes the lock per task and would re-take it within
  milliseconds, so a job that polls every few minutes would never see it free. After
  ``max_hold_s`` of consecutive holding the batch stays out for ``yield_s`` (longer than the
  slowest allowed poll, 5 min) before it asks again.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import signal
import socket
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_LOCK = Path.home() / "Hardik" / "Extras" / ".heavy_job.lock"
STALE_AFTER_S = 3 * 3600
MAX_HOLD_S = 40 * 60
YIELD_S = 310.0


def mem_available_mb(meminfo: Path = Path("/proc/meminfo")) -> int | None:
    """MemAvailable in MB (Linux), None where /proc/meminfo does not exist."""
    try:
        for line in meminfo.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except OSError:
        return None
    return None


def wait_for_memory(
    required_mb: int,
    *,
    poll_s: float = 60.0,
    timeout_s: float = 6 * 3600,
    reader: Callable[[], int | None] = mem_available_mb,
    log: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> int | None:
    """Block until MemAvailable >= required_mb. Returns the available MB seen last."""
    deadline = time.monotonic() + timeout_s
    while True:
        available = reader()
        if available is None or available >= required_mb:
            return available
        if time.monotonic() >= deadline:
            raise TimeoutError(f"RAM stayed below {required_mb} MB for {timeout_s:.0f}s")
        log(f"waiting for RAM: {available} MB available, need {required_mb} MB")
        sleep(poll_s)


def sandbox_slots(available_mb: int | None, requested: int, threshold_mb: int = 5120) -> int:
    """How many sandboxes may run at once right now (rule: 1 unless >= 5 GB available)."""
    if available_mb is None:
        return max(1, requested)
    return max(1, requested) if available_mb >= threshold_mb else 1


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class HeavyJobLock:
    def __init__(
        self,
        project: str,
        *,
        path: Path = DEFAULT_LOCK,
        poll_s: float = 120.0,
        log: Callable[[str], None] = print,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.project = project
        self.path = Path(path)
        self.poll_s = poll_s
        self.log = log
        self.sleep = sleep
        self.token = f"{project}:{os.getpid()}:{time.time_ns()}"
        self.held = False

    @property
    def owner_file(self) -> Path:
        return self.path / "owner.json"

    def owner(self) -> dict[str, Any] | None:
        try:
            return json.loads(self.owner_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def is_stale(self) -> bool:
        info = self.owner()
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return False
        if info is None:
            return age > STALE_AFTER_S
        pid = int(info.get("pid") or 0)
        same_host = info.get("host") == socket.gethostname()
        started = float(info.get("started_epoch") or self.path.stat().st_mtime)
        old = time.time() - started > STALE_AFTER_S
        return old and (not same_host or not _pid_alive(pid))

    def try_acquire(self) -> bool:
        try:
            self.path.mkdir(parents=False)
        except FileExistsError:
            return False
        info = {
            "project": self.project,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started": datetime.now(UTC).isoformat(timespec="seconds"),
            "started_epoch": time.time(),
            "token": self.token,
        }
        self.owner_file.write_text(json.dumps(info, indent=2), encoding="utf-8")
        self.held = True
        atexit.register(self.release)
        return True

    def acquire(self, timeout_s: float = 24 * 3600) -> None:
        deadline = time.monotonic() + timeout_s
        while not self.try_acquire():
            if self.is_stale():
                self.log(f"breaking stale heavy-job lock: {self.owner()}")
                self._remove()
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError("could not get the heavy-job lock")
            info = self.owner() or {}
            self.log(
                f"heavy-job lock held by {info.get('project')} (pid {info.get('pid')}, "
                f"since {info.get('started')}); retrying in {self.poll_s:.0f}s"
            )
            self.sleep(self.poll_s)

    def _remove(self) -> None:
        with contextlib.suppress(OSError):
            self.owner_file.unlink()
        with contextlib.suppress(OSError):
            self.path.rmdir()

    def release(self) -> None:
        if not self.held:
            return
        info = self.owner()
        if info is None or info.get("token") == self.token:
            self._remove()
        self.held = False

    def __enter__(self) -> HeavyJobLock:
        self.acquire()
        previous = signal.getsignal(signal.SIGTERM)

        def _on_term(signum, frame):  # release, then behave as before
            self.release()
            if callable(previous):
                previous(signum, frame)
            raise SystemExit(128 + signum)

        self._previous_handler = previous
        try:
            signal.signal(signal.SIGTERM, _on_term)
        except ValueError:  # not in the main thread
            self._previous_handler = None
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
        previous = getattr(self, "_previous_handler", None)
        if previous is not None:
            with contextlib.suppress(ValueError, TypeError):
                signal.signal(signal.SIGTERM, previous)


class LockPacer:
    """Fair sharing for batches that take the heavy-job lock once per task."""

    def __init__(
        self,
        max_hold_s: float = MAX_HOLD_S,
        yield_s: float = YIELD_S,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.max_hold_s = max_hold_s
        self.yield_s = yield_s
        self.clock = clock
        self.sleep = sleep
        self.streak_started: float | None = None
        self.last_release: float | None = None
        self.yields = 0

    def before_acquire(self, log: Callable[[str], None] = print) -> None:
        if self.streak_started is None or self.last_release is None:
            return
        now = self.clock()
        if now - self.last_release > 5:  # we were away, so others had their chance
            self.streak_started = None
        elif now - self.streak_started >= self.max_hold_s:
            log(
                f"held the heavy-job lock for {(now - self.streak_started) / 60:.0f} min in a "
                f"row; staying out for {self.yield_s:.0f}s so waiting jobs can take it"
            )
            self.sleep(self.yield_s)
            self.yields += 1
            self.streak_started = None

    def acquired(self, waited: bool) -> None:
        if self.streak_started is None or waited:
            self.streak_started = self.clock()

    def released(self) -> None:
        self.last_release = self.clock()


PACER = LockPacer()


@contextlib.contextmanager
def admission(
    project: str,
    *,
    required_mb: int | None,
    lock: bool,
    log: Callable[[str], None] = print,
    pacer: LockPacer | None = None,
) -> Iterator[None]:
    """Wait for RAM *before* taking the shared lock (never hold the lock while idle)."""
    pacer = pacer or PACER
    if lock:
        pacer.before_acquire(log)
    if required_mb:
        wait_for_memory(required_mb, log=log)
    if not lock:
        yield
        return
    started = time.monotonic()
    try:
        with HeavyJobLock(project, log=log):
            pacer.acquired(waited=time.monotonic() - started > 2)
            yield
    finally:
        pacer.released()
