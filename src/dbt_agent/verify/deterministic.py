"""Deterministic verification of an agent's patch, in a FRESH sandbox.

The agent's own sandbox may hold any state; the verifier starts again from the task's
starting snapshot, applies only the patch, and runs the project's own checks:

1. patch applies (no symlinks, nothing outside the project)
2. the project parses
3. no test removed / disabled / downgraded / filtered, no contract dropped
4. Slim CI: ``dbt build --select state:modified+ --state <starting manifest> --full-refresh``
5. reconciliation tests named in the project config
6. "incremental = full refresh": for each configured incremental model in the modified
   graph, delete the latest load batch, re-run it incrementally and compare an
   order-independent fingerprint with the full-refresh build
7. sqlfluff on the changed SQL files - only NEW violations count (non-blocking)

LLM judgement (the reviewer) only comes after these.
"""

from __future__ import annotations

import re
import shutil
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from dbt_agent.config import VerifyConfig
from dbt_agent.harness.integrity import compare_tests, describe_flags
from dbt_agent.sandbox.base import Sandbox, SandboxContext
from dbt_agent.tasks.fingerprint import fingerprint_sql
from dbt_agent.util import force_rmtree
from dbt_agent.workspace import BASELINE_STATE, remove_databases

Status = Literal["pass", "fail", "warn", "skip", "error"]
SQL_DIRS = ("models/", "tests/", "macros/", "snapshots/", "analyses/")


@dataclass
class Check:
    name: str
    status: Status
    blocking: bool
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Verification:
    checks: list[Check] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    downstream: list[str] = field(default_factory=list)
    exposures: list[str] = field(default_factory=list)
    test_flags: list[dict[str, Any]] = field(default_factory=list)
    new_lint: dict[str, list[str]] = field(default_factory=dict)
    patch_files: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def passed(self) -> bool:
        return all(c.status in {"pass", "skip", "warn"} for c in self.checks if c.blocking)

    def check(self, name: str) -> Check | None:
        return next((c for c in self.checks if c.name == name), None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": [c.__dict__ for c in self.checks],
            "modified": self.modified,
            "downstream": self.downstream,
            "exposures": self.exposures,
            "test_flags": self.test_flags,
            "new_lint": self.new_lint,
            "patch_files": self.patch_files,
            "seconds": round(self.seconds, 2),
        }


def patch_paths(patch: str) -> list[str]:
    paths = set(re.findall(r"^\+\+\+ b/(.+)$", patch, re.MULTILINE))
    paths |= set(re.findall(r"^--- a/(.+)$", patch, re.MULTILINE))
    return sorted(p.strip() for p in paths if p.strip() != "/dev/null")


class Verifier:
    def __init__(self, config: VerifyConfig, make_sandbox, context: SandboxContext) -> None:
        self.config = config
        self.make_sandbox = make_sandbox
        self.context = context

    def verify(
        self,
        *,
        start_dir: Path,
        patch: str,
        workdir: Path,
        tests_changed_reason: str | None = None,
        keep: bool = False,
    ) -> Verification:
        started = time.monotonic()
        result = Verification(patch_files=patch_paths(patch))
        force_rmtree(workdir)
        shutil.copytree(start_dir, workdir, symlinks=True)
        box = self.make_sandbox(workdir, self.context)
        try:
            self._run(box, patch, result, tests_changed_reason)
        except Exception as exc:  # infrastructure failure is a failed verification
            result.checks.append(Check("verifier", "error", True, f"{type(exc).__name__}: {exc}"))
        finally:
            box.stop()
            result.seconds = time.monotonic() - started
            if not keep:
                remove_databases(workdir)
                force_rmtree(workdir)
        return result

    # ------------------------------------------------------------------ steps
    def _run(
        self, box: Sandbox, patch: str, result: Verification, tests_changed_reason: str | None
    ) -> None:
        if not patch.strip():
            result.checks.append(Check("patch_applies", "fail", True, "the patch is empty"))
            return
        sql_files = [p for p in result.patch_files if p.endswith(".sql") and p.startswith(SQL_DIRS)]
        before_lint = self._lint(box, sql_files) if self.config.sqlfluff else {}

        applied = box.call("git_apply", {"patch": patch})
        if not applied.ok:
            result.checks.append(Check("patch_applies", "fail", True, applied.error or ""))
            return
        result.checks.append(
            Check("patch_applies", "pass", True, f"{len(result.patch_files)} file(s)")
        )

        parsed = box.call("dbt_raw", {"argv": ["parse"]}, timeout=self.config.timeout_s)
        if parsed.data.get("exit_code") != 0:
            result.checks.append(
                Check("parses", "fail", True, str(parsed.data.get("error_excerpt", ""))[-1500:])
            )
            return
        result.checks.append(Check("parses", "pass", True))

        baseline = box.call("manifest_summary", {"path": f"{BASELINE_STATE}/manifest.json"})
        current = box.call("manifest_summary", {"path": "target/manifest.json"})
        nodes = current.data.get("nodes", {}) if current.ok else {}
        if baseline.ok and current.ok:
            result.test_flags = compare_tests(baseline.data, current.data)
            if not result.test_flags:
                result.checks.append(Check("tests_intact", "pass", True))
            elif (tests_changed_reason or "").strip():
                result.checks.append(
                    Check(
                        "tests_intact",
                        "warn",
                        True,
                        "; ".join(describe_flags(result.test_flags))
                        + f" (justified: {tests_changed_reason.strip()})",
                    )
                )
            else:
                result.checks.append(
                    Check(
                        "tests_intact", "fail", True, "; ".join(describe_flags(result.test_flags))
                    )
                )
        else:
            result.checks.append(Check("tests_intact", "skip", True, "no baseline manifest"))

        state = f"--state {BASELINE_STATE}"
        modified = box.call("dbt_ls", {"select": "state:modified", "state": BASELINE_STATE})
        impact = box.call("dbt_ls", {"select": "state:modified+", "state": BASELINE_STATE})
        mod_ids = [n["unique_id"] for n in modified.data.get("nodes", [])]
        all_ids = [n["unique_id"] for n in impact.data.get("nodes", [])]
        result.modified = mod_ids
        result.downstream = [u for u in all_ids if u not in set(mod_ids)]
        result.exposures = [u for u in all_ids if u.startswith("exposure.")]

        if self.config.slim_ci:
            build = box.call(
                "dbt_raw",
                {
                    "argv": [
                        "build",
                        "--select",
                        "state:modified+",
                        *state.split(),
                        "--full-refresh",
                    ],
                    "timeout_s": self.config.timeout_s,
                },
                timeout=self.config.timeout_s,
            )
            summary = build.data.get("results") or {}
            counts = summary.get("counts") or {}
            failures = [f["unique_id"] for f in summary.get("failures", [])]
            status: Status = "pass" if build.data.get("exit_code") == 0 else "fail"
            detail = ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "nothing selected"
            if failures:
                detail += " | failing: " + ", ".join(failures[:6])
            if build.data.get("exit_code") != 0 and not summary:
                detail = str(build.data.get("error_excerpt", ""))[-800:]
            result.checks.append(
                Check("slim_ci", status, True, detail, {"counts": counts, "failures": failures})
            )

        if self.config.reconciliation_tests:
            names = " ".join(self.config.reconciliation_tests)
            recon = box.call(
                "dbt_raw",
                {"argv": ["test", "--select", *names.split()], "timeout_s": self.config.timeout_s},
                timeout=self.config.timeout_s,
            )
            ok = recon.data.get("exit_code") == 0
            result.checks.append(
                Check(
                    "reconciliation",
                    "pass" if ok else "fail",
                    True,
                    names if ok else str(recon.data.get("error_excerpt", ""))[-600:],
                )
            )

        for model, watermark in self.config.incremental_models.items():
            result.checks.append(self._incremental_check(box, model, watermark, nodes, all_ids))

        if self.config.sqlfluff and sql_files:
            after_lint = self._lint(box, sql_files)
            new: dict[str, list[str]] = {}
            for path, codes in after_lint.items():
                diff = Counter(codes) - Counter(before_lint.get(path, []))
                if diff:
                    new[path] = sorted(diff.elements())
            result.new_lint = new
            total = sum(len(v) for v in new.values())
            result.checks.append(
                Check(
                    "sqlfluff_new_violations",
                    "warn" if total else "pass",
                    False,
                    f"{total} new violation(s)" if total else "no new violations",
                    {"new": new},
                )
            )

    def _lint(self, box: Sandbox, files: list[str]) -> dict[str, list[str]]:
        response = box.call(
            "sqlfluff",
            {"files": files, "timeout_s": self.config.timeout_s},
            timeout=self.config.timeout_s,
        )
        if not response.ok:
            return {}
        return {
            path: [v.get("code") or "?" for v in violations]
            for path, violations in response.data.get("files", {}).items()
        }

    def _incremental_check(
        self, box: Sandbox, model: str, watermark: str, nodes: dict[str, Any], impacted: list[str]
    ) -> Check:
        name = f"incremental_equals_full_refresh:{model}"
        node = next(
            (
                n
                for uid, n in nodes.items()
                if n.get("resource_type") == "model" and n.get("name") == model
            ),
            None,
        )
        if node is None:
            return Check(name, "skip", True, "model not in the project")
        if not any(uid.endswith(f".{model}") for uid in impacted):
            return Check(name, "skip", True, "model not affected by the change")
        if node.get("materialized") != "incremental":
            return Check(
                name, "fail", True, f"{model} is no longer incremental ({node.get('materialized')})"
            )
        relation = f'"{node["schema"]}"."{node.get("alias") or model}"'
        cols = box.require(
            "sql_exec",
            {
                "statements": [
                    "select column_name, data_type from information_schema.columns "
                    f"where table_schema = '{node['schema']}' "
                    f"and table_name = '{node.get('alias') or model}'"
                ],
                "read_only": True,
            },
        )
        columns = [
            tuple(r)
            for r in cols.data["results"][0]["rows"]
            if r[0] not in set(self.config.fingerprint_exclude_columns)
        ]
        fingerprint = fingerprint_sql(node["schema"], node.get("alias") or model, columns)

        def fp() -> list[Any]:
            out = box.require("sql_exec", {"statements": [fingerprint], "read_only": True})
            return out.data["results"][0]["rows"][0]

        full = fp()
        deleted = box.require(
            "sql_exec",
            {
                "statements": [
                    f"delete from {relation} where {watermark} in (select distinct {watermark} "
                    f"from {relation} order by 1 desc limit 2)",
                    f"select count(*) from {relation}",
                ]
            },
        )
        rows_left = deleted.data["results"][-1]["rows"][0][0]
        rerun = box.call(
            "dbt_raw",
            {"argv": ["run", "--select", model], "timeout_s": self.config.timeout_s},
            timeout=self.config.timeout_s,
        )
        if rerun.data.get("exit_code") != 0:
            return Check(
                name,
                "fail",
                True,
                "incremental re-run failed: " + str(rerun.data.get("error_excerpt", ""))[-600:],
            )
        incremental = fp()
        same = incremental == full
        return Check(
            name,
            "pass" if same else "fail",
            True,
            f"rows {incremental[0]} vs {full[0]}, checksum "
            f"{'identical' if same else 'differs'} (replayed the last 2 load batches; "
            f"{full[0] - rows_left} rows)",
            {"full_refresh": full, "incremental": incremental},
        )
