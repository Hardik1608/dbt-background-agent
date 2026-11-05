"""Suite validation: every task must FAIL before the fix and PASS with the reference fix.

Per task (checkpointed to ``<data_dir>/validation/<task_id>.json``, so a power cut costs at
most one task):

1. build the template (start state, CI log, base values);
2. grade the reference patch and record its values, fingerprints and statuses;
3. grade the empty patch: it must not resolve;
4. optionally grade the reference again with the collateral check on (determinism).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from dbt_agent.tasks.grader import grade
from dbt_agent.tasks.model import TaskSpec
from dbt_agent.tasks.templates import SuiteEnv, build_template, reference_patch
from dbt_agent.util import atomic_write_json, read_json, sha256_text, utc_now


def validation_path(env: SuiteEnv, task: TaskSpec) -> Path:
    return env.data_dir / "validation" / f"{task.id}.json"


def validate_task(
    task: TaskSpec, env: SuiteEnv, *, stability: bool = False, force: bool = False
) -> dict[str, Any]:
    path = validation_path(env, task)
    started = time.monotonic()
    meta = build_template(task, env, force=force)
    patch = reference_patch(task, env, env.data_dir / "scratch" / f"{task.id}_ref_src")
    key = sha256_text(meta["template_key"] + patch + task.model_dump_json())[:16]
    cached = read_json(path)
    if (
        cached
        and cached.get("key") == key
        and cached.get("valid")
        and not force
        and (cached.get("stability") is not None or not stability)
    ):
        return cached

    work = env.data_dir / "scratch"
    ref = grade(task, env, patch, work / f"{task.id}_ref", record_reference=True)
    meta = env.meta(task)
    meta["reference"] = {"patch": patch, **ref.recorded} if ref.patch_applied else None
    atomic_write_json(env.meta_path(task), meta)

    empty = grade(task, env, "", work / f"{task.id}_empty")
    stable = None
    if stability:
        again = grade(task, env, patch, work / f"{task.id}_ref2")
        stable = {
            "resolved": again.resolved,
            "failed_checks": again.failed_checks(),
            "collateral": again.collateral,
        }

    result = {
        "task_id": task.id,
        "kind": task.kind,
        "category": task.category,
        "key": key,
        "validated_at": utc_now(),
        "valid": (not empty.resolved) and ref.resolved and (stable is None or stable["resolved"]),
        "fails_before": not empty.resolved,
        "empty_failed_checks": empty.failed_checks(),
        "passes_with_reference": ref.resolved,
        "reference_failed_checks": ref.failed_checks(),
        "reference_error": ref.error,
        "reference_patch_lines": patch.count("\n"),
        "start_trigger_exit_code": meta["trigger"]["exit_code"],
        "stability": stable,
        "seconds": round(time.monotonic() - started, 1),
    }
    atomic_write_json(path, result)
    return result


def summarise(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "tasks": len(results),
        "valid": sum(r["valid"] for r in results),
        "fail_before": sum(r["fails_before"] for r in results),
        "pass_with_reference": sum(r["passes_with_reference"] for r in results),
        "stability_checked": sum(r.get("stability") is not None for r in results),
        "stable": sum(bool(r.get("stability") and r["stability"]["resolved"]) for r in results),
        "invalid": [r["task_id"] for r in results if not r["valid"]],
    }


def render_markdown(results: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    lines = [
        "# Sales suite validation",
        "",
        "Every task must fail before the fix (empty patch) and pass with the reference fix.",
        "",
        f"- Tasks: {summary['tasks']}; valid: **{summary['valid']}**",
        f"- Fail before the fix: {summary['fail_before']}/{summary['tasks']}",
        f"- Pass with the reference fix: {summary['pass_with_reference']}/{summary['tasks']}",
        f"- Reference re-graded with the collateral check (determinism): "
        f"{summary['stable']}/{summary['stability_checked']}",
        "",
        "| Task | Kind | Category | Valid | Checks failing before the fix | Seconds |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        failing = ", ".join(r["empty_failed_checks"][:4])
        if len(r["empty_failed_checks"]) > 4:
            failing += f" (+{len(r['empty_failed_checks']) - 4})"
        lines.append(
            f"| {r['task_id']} | {r['kind']} | {r['category']} | "
            f"{'yes' if r['valid'] else '**NO**'} | {failing} | {r['seconds']} |"
        )
    return "\n".join(lines) + "\n"
