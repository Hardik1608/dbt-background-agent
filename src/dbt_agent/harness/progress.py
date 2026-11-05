"""The progress file: a short, human- and model-readable state of the run.

The harness rewrites ``progress.md`` after every turn (atomically). It is what a reviewer
reads to see where a background run is, and what the model is given when a run resumes
after a crash or when old context is compacted away.
"""

from __future__ import annotations

from typing import Any

from dbt_agent.util import short


def render_progress(
    *,
    task_id: str,
    title: str,
    status: str,
    budget_note: str,
    files: dict[str, int],
    dbt_runs: list[dict[str, Any]],
    checklist: list[dict[str, Any]],
    recent_calls: list[str],
    last_note: str,
    security: dict[str, Any],
) -> str:
    lines = [
        f"# Progress: {task_id}",
        "",
        f"- Task: {title}",
        f"- Status: {status}",
        f"- Budget used: {budget_note}",
        "",
        "## Files changed",
    ]
    if files:
        lines += [
            f"- {path} ({n} edit{'s' if n != 1 else ''})" for path, n in sorted(files.items())
        ]
    else:
        lines.append("- none yet")
    lines += ["", "## dbt runs (latest last)"]
    if dbt_runs:
        for run in dbt_runs[-8:]:
            counts = run.get("counts") or {}
            counts_text = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
            outcome = "passed" if run.get("exit_code") == 0 else "FAILED"
            fails = ", ".join(
                f["unique_id"].split(".")[-1] for f in (run.get("failures") or [])[:4]
            )
            lines.append(
                f"- turn {run['turn']}: dbt {run['command']}"
                f"{' -s ' + run['select'] if run.get('select') else ''}: {outcome}"
                f"{' (' + counts_text + ')' if counts_text else ''}"
                f"{' - failing: ' + fails if fails else ''}"
            )
    else:
        lines.append("- none yet")
    if checklist:
        lines += ["", "## Finish attempts"]
        for item in checklist[-3:]:
            verdict = "accepted" if item.get("passed") else "rejected"
            reasons = "; ".join(short(m, 160) for m in item.get("messages", []))
            lines.append(f"- {verdict}{': ' + reasons if reasons else ''}")
    lines += ["", "## Last actions"]
    lines += [f"- {c}" for c in recent_calls[-8:]] or ["- none"]
    if last_note:
        lines += ["", "## Last note from the agent", short(last_note, 600)]
    if security.get("escape_attempts") or security.get("egress_attempts"):
        lines += [
            "",
            "## Security",
            f"- escape attempts blocked: {security.get('escape_attempts', 0)}",
            f"- egress attempts blocked: {security.get('egress_attempts', 0)}",
        ]
    return "\n".join(lines) + "\n"
