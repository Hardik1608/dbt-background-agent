"""Pre-completion checklist: the harness verifies "done" before believing it.

When the model calls ``finish``, the harness (not the model) checks, in the agent's own
sandbox, so the agent can still fix what fails:

1. the summary explains the change;
2. there is a change at all (unless the task allows none);
3. the project parses;
4. no test was removed, disabled, downgraded, filtered or edited and no contract dropped
   (unless the agent gives ``tests_changed_reason``, which raises the PR's risk class);
5. ``dbt build --select state:modified+ --full-refresh`` passes against the starting
   manifest (like Slim CI building a fresh schema; the incremental path is checked
   separately by the verifier and the grader).

A failed check is returned as the ``finish`` tool result with what to do next.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from dbt_agent.harness.integrity import compare_tests, describe_flags
from dbt_agent.sandbox.base import Sandbox

MIN_SUMMARY_CHARS = 20


@dataclass
class ChecklistResult:
    passed: bool
    messages: list[str] = field(default_factory=list)
    changed_files: list[dict[str, Any]] = field(default_factory=list)
    test_flags: list[dict[str, Any]] = field(default_factory=list)
    build: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "messages": self.messages,
            "changed_files": self.changed_files,
            "test_flags": self.test_flags,
            "build": self.build,
        }

    def model_text(self) -> str:
        if self.passed:
            return "Checklist passed. The task is complete."
        lines = ["CHECKLIST FAILED - the task is not complete yet:"]
        lines += [f"- {m}" for m in self.messages]
        lines.append("Fix these points, then call finish again.")
        return "\n".join(lines)


class Checklist:
    def __init__(
        self,
        sandbox: Sandbox,
        *,
        run_build: bool = True,
        command: str = "build",
        select: str = "state:modified+",
        full_refresh: bool = True,
        baseline_manifest: str = ".agent/baseline_state/manifest.json",
        build_timeout_s: float = 600,
    ) -> None:
        self.sandbox = sandbox
        self.run_build = run_build
        self.command = command
        self.select = select
        self.full_refresh = full_refresh
        self.baseline_manifest = baseline_manifest
        self.build_timeout_s = build_timeout_s
        self._baseline: dict[str, Any] | None = None

    def baseline(self) -> dict[str, Any] | None:
        if self._baseline is None:
            response = self.sandbox.call("manifest_summary", {"path": self.baseline_manifest})
            self._baseline = response.data if response.ok else None
        return self._baseline

    def _build_args(self) -> dict[str, Any]:
        args: dict[str, Any] = {"command": self.command, "full_refresh": self.full_refresh}
        select = self.select
        if "state:" in select and self.baseline() is None:
            select = ""  # no baseline manifest: check the whole project
        if select:
            args["select"] = select
        return args

    def run(
        self, *, summary: str, tests_changed_reason: str | None, allow_no_change: bool
    ) -> ChecklistResult:
        result = ChecklistResult(passed=True)

        if len(summary.strip()) < MIN_SUMMARY_CHARS:
            result.messages.append(
                "Give a real summary in finish(summary=...): the root cause or requirement, "
                "what you changed, and the test evidence."
            )

        patch = self.sandbox.call("git_patch")
        result.changed_files = patch.data.get("files", []) if patch.ok else []
        if not result.changed_files and not allow_no_change:
            result.messages.append(
                "You have not changed any file. Make the fix with edit_file or write_file "
                "first (or explain in the summary why no change is possible)."
            )
            result.passed = False
            return result

        parse = self.sandbox.call("dbt_raw", {"argv": ["parse"]}, timeout=self.build_timeout_s)
        if not parse.ok or parse.data.get("exit_code") != 0:
            excerpt = parse.data.get("error_excerpt") or parse.error or parse.output
            result.messages.append(
                "The project does not parse. Fix this first (run_dbt('parse') shows it):\n"
                + str(excerpt)[-1500:]
            )
            result.passed = False
            return result

        baseline = self.baseline()
        if baseline is not None:
            current = self.sandbox.call("manifest_summary", {"path": "target/manifest.json"})
            if current.ok:
                result.test_flags = compare_tests(baseline, current.data)
        if result.test_flags and not (tests_changed_reason or "").strip():
            details = "; ".join(describe_flags(result.test_flags))
            result.messages.append(
                f"Your change removes or weakens the project's tests: {details}. Restore them "
                "and fix the model or data instead. Only if the task explicitly requires this, "
                "call finish again with tests_changed_reason explaining why."
            )

        if self.run_build:
            args = self._build_args()
            build = self.sandbox.call(
                "run_dbt",
                args,
                timeout=self.build_timeout_s,
            )
            result.build = {
                "ok": build.ok,
                "exit_code": build.data.get("exit_code"),
                "counts": build.data.get("counts"),
                "failures": build.data.get("failures"),
            }
            if not build.ok:
                result.messages.append("Could not run the build check: " + (build.error or ""))
            elif build.data.get("exit_code") != 0:
                shown = " ".join(
                    ["dbt", self.command]
                    + (["--select", args["select"]] if args.get("select") else [])
                    + (["--full-refresh"] if args.get("full_refresh") else [])
                )
                result.messages.append(f"{shown} fails:\n" + build.output[-3000:])

        result.passed = not result.messages
        return result
