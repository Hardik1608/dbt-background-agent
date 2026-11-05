"""LocalSandbox: runs the toolbox as a plain subprocess on the host.

WARNING - NOT A SECURITY BOUNDARY. The toolbox still confines file tools to the work
directory and opens DuckDB read-only, but dbt executes model-written SQL, Jinja and Python
with the host user's full permissions and network access. Use it only for unit tests,
CI smoke tests with the scripted fake LLM, and laptop development. Real agent runs use
``DockerSandbox``.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from dbt_agent.sandbox.base import TOOLBOX_PATH, ExecResult, Sandbox, SandboxContext


def _kill_tree(proc: subprocess.Popen) -> None:
    with contextlib.suppress(OSError, ProcessLookupError):
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    with contextlib.suppress(OSError):
        proc.kill()


class LocalSandbox(Sandbox):
    kind = "local"
    is_security_boundary = False

    def __init__(
        self, host_workdir: Path, context: SandboxContext | None = None, python: str | None = None
    ) -> None:
        super().__init__(host_workdir, context)
        self.python = python or sys.executable

    @property
    def root(self) -> str:
        return str(Path(self.host_workdir).resolve())

    def start(self) -> None:
        self.host_workdir.mkdir(parents=True, exist_ok=True)
        self.started = True

    def stop(self) -> None:
        self.started = False

    def toolbox_argv(self) -> list[str]:
        return [self.python, str(TOOLBOX_PATH)]

    def exec(self, argv: list[str], *, timeout: float, stdin: str | None = None) -> ExecResult:
        kwargs: dict = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        else:
            kwargs["start_new_session"] = True
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        started = time.monotonic()
        proc = subprocess.Popen(
            argv,
            cwd=self.root,
            env=env,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **kwargs,
        )
        try:
            out, err = proc.communicate(
                stdin.encode("utf-8") if stdin is not None else None, timeout=timeout
            )
            timed_out = False
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            out, err = proc.communicate()
            timed_out = True
        return ExecResult(
            returncode=proc.returncode if not timed_out else -9,
            stdout=out.decode("utf-8", "replace"),
            stderr=err.decode("utf-8", "replace"),
            timed_out=timed_out,
            seconds=time.monotonic() - started,
        )

    def on_hang(self) -> None:
        """Nothing to restart: the hung subprocess tree was already killed."""
