"""Test helpers: an in-memory sandbox for harness tests and a git-repo factory."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from dbt_agent.sandbox.base import ExecResult, Sandbox, SandboxContext, ToolResponse

GIT = [
    "git",
    "-c",
    "user.name=test",
    "-c",
    "user.email=test@example.invalid",
    "-c",
    "core.autocrlf=false",
    "-c",
    "init.defaultBranch=main",
]


def make_repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    subprocess.run([*GIT, "init", "-q"], cwd=root, check=True)
    subprocess.run([*GIT, "add", "-A"], cwd=root, check=True)
    subprocess.run([*GIT, "commit", "-q", "-m", "baseline"], cwd=root, check=True)
    (root / ".git" / "info").mkdir(exist_ok=True)
    (root / ".git" / "info" / "exclude").write_text(".agent/\n", encoding="utf-8")
    return root


class Crash(BaseException):
    """Simulates the process dying (not an Exception, so nothing catches it)."""


Handler = Callable[[dict[str, Any]], ToolResponse]


class MemorySandbox(Sandbox):
    """A sandbox whose 'filesystem' is a dict. File tools behave like the real toolbox;
    dbt/query tools return canned responses set by the test."""

    kind = "memory"

    def __init__(self, files: dict[str, str] | None = None) -> None:
        super().__init__(Path(), SandboxContext())
        self.files: dict[str, str] = dict(files or {})
        self.base: dict[str, str] = dict(self.files)
        self.trees: dict[str, dict[str, str]] = {}
        self.handlers: dict[str, Handler] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.crash_on: Callable[[str, dict[str, Any]], bool] | None = None
        self.started = True

    # Sandbox ABC ---------------------------------------------------------
    @property
    def root(self) -> str:
        return "/mem"

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def exec(self, argv: list[str], *, timeout: float, stdin: str | None = None) -> ExecResult:
        raise NotImplementedError

    def toolbox_argv(self) -> list[str]:
        return []

    # tools ----------------------------------------------------------------
    def call(
        self,
        tool: str,
        args: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        context: SandboxContext | None = None,
    ) -> ToolResponse:
        args = args or {}
        self.calls.append((tool, args))
        if tool in self.handlers:
            response = self.handlers[tool](args)
        else:
            response = getattr(self, f"_t_{tool}")(args)
        if self.crash_on is not None and self.crash_on(tool, args):
            self.crash_on = None
            raise Crash(f"crash after {tool}")
        return response

    def _tree(self) -> str:
        digest = hashlib.sha1(json.dumps(self.files, sort_keys=True).encode()).hexdigest()
        self.trees[digest] = dict(self.files)
        return digest

    def _t_snapshot(self, args):
        return ToolResponse(ok=True, output="", data={"tree": self._tree()})

    def _t_restore(self, args):
        self.files = dict(self.trees[args["tree"]])
        return ToolResponse(ok=True, output="restored")

    def _t_list_files(self, args):
        return ToolResponse(ok=True, output="\n".join(sorted(self.files)))

    def _t_read_file(self, args):
        path = args.get("path")
        if path not in self.files:
            return ToolResponse(
                ok=False,
                error=f"File not found: {path}",
                hint="Use list_files to find the right path.",
            )
        return ToolResponse(ok=True, output=self.files[path])

    def _t_write_file(self, args):
        self.files[args["path"]] = args["content"]
        return ToolResponse(
            ok=True, output=f"Wrote {args['path']}", data={"path": args["path"], "mutated": True}
        )

    def _t_edit_file(self, args):
        path = args["path"]
        text = self.files.get(path)
        if text is None or args["old_string"] not in text:
            return ToolResponse(
                ok=False,
                error=f"old_string was not found in {path}.",
                hint="Copy the exact current text from read_file.",
            )
        self.files[path] = text.replace(args["old_string"], args["new_string"], 1)
        return ToolResponse(ok=True, output=f"Edited {path}", data={"path": path, "mutated": True})

    def _t_git_patch(self, args):
        changed = [
            {"path": p, "added": "1", "deleted": "0"}
            for p in sorted(self.files)
            if self.base.get(p) != self.files[p]
        ]
        body = "".join(f"--- a/{c['path']}\n+++ b/{c['path']}\n" for c in changed)
        return ToolResponse(ok=True, output=body, data={"files": changed})

    def _t_git_diff(self, args):
        return ToolResponse(ok=True, output=self._t_git_patch(args).output or "No changes.")

    def _t_dbt_raw(self, args):
        return ToolResponse(ok=True, output="", data={"exit_code": 0})

    def _t_manifest_summary(self, args):
        return ToolResponse(ok=True, output="", data={"nodes": {}, "unit_tests": []})

    def _t_run_dbt(self, args):
        return ToolResponse(
            ok=True,
            output="dbt build: PASSED",
            data={
                "exit_code": 0,
                "command": args.get("command"),
                "select": args.get("select", ""),
                "counts": {"success": 1},
                "failures": [],
            },
        )

    def _t_query_duckdb(self, args):
        return ToolResponse(ok=True, output="x\n-\n1\n(1 rows)", data={"rows": 1})


def tool_messages_well_formed(messages: list[dict[str, Any]]) -> bool:
    """Every assistant tool call is answered by tool messages directly after it."""
    i = 0
    while i < len(messages):
        message = messages[i]
        calls = message.get("tool_calls") or []
        if message.get("role") == "assistant" and calls:
            ids = {c["id"] for c in calls}
            answered = set()
            j = i + 1
            while j < len(messages) and messages[j].get("role") == "tool":
                answered.add(messages[j]["tool_call_id"])
                j += 1
            # the last assistant message may be the finishing one with no results needed
            if answered != ids and j < len(messages):
                return False
            i = j
        else:
            i += 1
    return True
