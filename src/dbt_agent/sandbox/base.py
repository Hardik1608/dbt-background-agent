"""Sandbox interface.

The agent loop runs on the host. A sandbox only executes tools: the host sends a JSON
request to ``toolbox.py`` inside the sandbox and gets a JSON response back. Two
implementations exist:

* ``DockerSandbox`` - the real boundary: ``--network none``, memory cap, read-only root
  filesystem, all capabilities dropped, non-root user, one writable work directory.
* ``LocalSandbox`` - a subprocess in a temp directory, for unit tests and laptop
  development. It is NOT a security boundary.
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TOOLBOX_PATH = Path(__file__).with_name("toolbox.py")


class SandboxError(RuntimeError):
    """The sandbox itself failed (not a tool error the model should see)."""


@dataclass
class ExecResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    seconds: float


@dataclass
class ToolResponse:
    ok: bool
    output: str = ""
    error: str | None = None
    hint: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    security: list[dict[str, str]] = field(default_factory=list)
    seconds: float = 0.0

    def model_text(self) -> str:
        """What the model sees. Errors always say what to do next."""
        if self.ok:
            return self.output
        text = f"ERROR: {self.error}"
        if self.hint:
            text += f"\nHint: {self.hint}"
        return text

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "output": self.output,
            "error": self.error,
            "hint": self.hint,
            "data": self.data,
            "security": self.security,
            "seconds": round(self.seconds, 3),
        }


@dataclass
class SandboxContext:
    """Host-trusted settings passed to the toolbox with every call.

    These never come from the work directory (which the agent can write), so the agent
    cannot change its own limits.
    """

    db_path: str | None = None
    dbt_timeout_s: float = 300.0
    query_timeout_s: float = 60.0
    max_chars: int = 8000
    state_dir: str = ".agent/baseline_state"
    read_only_globs: list[str] = field(default_factory=lambda: ["profiles.yml"])
    env: dict[str, str] = field(default_factory=dict)
    profiles_subdir: str = "."
    query_max_memory: str = "1GB"

    def to_dict(self, root: str) -> dict[str, Any]:
        return {
            "root": root,
            "db_path": self.db_path,
            "dbt_timeout_s": self.dbt_timeout_s,
            "query_timeout_s": self.query_timeout_s,
            "max_chars": self.max_chars,
            "state_dir": self.state_dir,
            "read_only_globs": self.read_only_globs,
            "env": self.env,
            "profiles_subdir": self.profiles_subdir,
            "query_max_memory": self.query_max_memory,
        }


class Sandbox(ABC):
    """Executes toolbox calls against one work directory."""

    kind: str = "abstract"
    #: extra seconds the host waits beyond the in-sandbox timeout before declaring a hang
    HOST_GRACE_S = 30.0

    def __init__(self, host_workdir: Path, context: SandboxContext | None = None) -> None:
        self.host_workdir = Path(host_workdir)
        self.context = context or SandboxContext()
        self.started = False

    # ------------------------------------------------------------------ lifecycle
    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @property
    @abstractmethod
    def root(self) -> str:
        """The work directory as seen from inside the sandbox."""

    @abstractmethod
    def exec(self, argv: list[str], *, timeout: float, stdin: str | None = None) -> ExecResult:
        """Run a command inside the sandbox."""

    @abstractmethod
    def toolbox_argv(self) -> list[str]: ...

    def describe(self) -> dict[str, Any]:
        return {"kind": self.kind, "root": self.root}

    def __enter__(self) -> Sandbox:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # ------------------------------------------------------------------ tools
    def call(
        self,
        tool: str,
        args: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        context: SandboxContext | None = None,
    ) -> ToolResponse:
        """Run one toolbox tool. Tool-level failures come back as ok=False, never raise."""
        if not self.started:
            self.start()
        ctx = (context or self.context).to_dict(self.root)
        inner = timeout if timeout is not None else max(ctx["dbt_timeout_s"], 60.0)
        ctx["dbt_timeout_s"] = inner
        request = json.dumps({"tool": tool, "args": args or {}, "ctx": ctx})
        started = time.monotonic()
        result = self.exec(self.toolbox_argv(), timeout=inner + self.HOST_GRACE_S, stdin=request)
        seconds = time.monotonic() - started
        if result.timed_out:
            self.on_hang()
            return ToolResponse(
                ok=False,
                error=f"The tool did not return within {inner + self.HOST_GRACE_S:.0f}s; the "
                "sandbox was restarted.",
                hint="Try a smaller step (narrower dbt selection, simpler query).",
                seconds=seconds,
            )
        try:
            payload = json.loads(result.stdout)
        except ValueError:
            tail = (result.stderr or result.stdout)[-1500:]
            return ToolResponse(
                ok=False, error=f"Sandbox returned no valid response: {tail}", seconds=seconds
            )
        return ToolResponse(
            ok=bool(payload.get("ok")),
            output=payload.get("output") or "",
            error=payload.get("error"),
            hint=payload.get("hint"),
            data=payload.get("data") or {},
            security=payload.get("security") or [],
            seconds=seconds,
        )

    def on_hang(self) -> None:
        """Called when a tool exceeded the host-side deadline. Default: restart."""
        self.stop()
        self.start()

    def require(
        self, tool: str, args: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> ToolResponse:
        """Harness-side call that must succeed."""
        response = self.call(tool, args, timeout=timeout)
        if not response.ok:
            raise SandboxError(f"{tool} failed: {response.error}")
        return response
