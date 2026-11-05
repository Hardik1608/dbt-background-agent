"""Markdown rendering of benchmark results (every number comes from the JSON next to it)."""

from __future__ import annotations

from collections import defaultdict
from typing import Any


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


def _ci(pair: list[float] | None) -> str:
    return "n/a" if not pair else f"[{100 * pair[0]:.1f}, {100 * pair[1]:.1f}]"


def render_sales_report(
    meta: dict[str, Any], summary: dict[str, Any], runs: list[dict[str, Any]]
) -> str:
    rev = summary.get("reviewer", {})
    ver = summary.get("verifier", {})
    cost = summary.get("cost", {})
    sec = summary.get("security", {})
    lines = [
        f"# Sales suite run: {meta.get('run_name')}",
        "",
        f"- Model: `{meta.get('model', 'see runs')}`; trials per task: "
        f"{summary.get('trials_per_task')}; tasks: {summary.get('tasks')}; runs: "
        f"{summary.get('runs')} (+{summary.get('infrastructure_errors', 0)} infrastructure errors)",
        f"- Generated: {meta.get('finished_at') or meta.get('rebuilt_at')}",
        "",
        "## Headline",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Resolve rate (finished with a PR and hidden tests pass) | "
        f"**{_pct(summary.get('resolve_rate'))}** "
        f"({summary.get('resolved_runs')}/{summary.get('runs')}); Wilson 95% "
        f"{_ci(summary.get('resolve_rate_wilson_95'))}; task-bootstrap 95% "
        f"{_ci(summary.get('resolve_rate_bootstrap_95'))} |",
        f"| Final patch correct but the agent ran out of budget (no PR) | "
        f"{summary.get('correct_but_unfinished', 0)} |",
        f"| Resolve rate per trial | "
        f"{', '.join(_pct(v) for v in summary.get('resolve_rate_per_trial', []))} |",
        f"| pass^{summary.get('k')} (all k trials succeed) | {_pct(summary.get('pass_hat_k'))} |",
        f"| pass@{summary.get('k')} (at least one succeeds) | {_pct(summary.get('pass_at_k'))} |",
        f"| Agent finished (checklist passed) | {summary.get('completed_runs')} runs; "
        f"{summary.get('completed_but_unresolved')} of them did not pass the hidden tests |",
        f"| Runs with regressions (tests broken or collateral data change) | "
        f"{summary.get('runs_with_regressions')} ({summary.get('regressions_total')} items) |",
        f"| Reviewer catch rate on wrong completions | {_pct(rev.get('catch_rate_wrong'))} "
        f"({rev.get('wrong_completions_flagged')}/{rev.get('wrong_completions_reviewed')}) |",
        f"| Reviewer catch rate on runs with regressions | "
        f"{_pct(rev.get('catch_rate_regressions'))} "
        f"({rev.get('regression_runs_flagged')}/{rev.get('regression_runs_reviewed')}) |",
        f"| Reviewer false alarms on correct completions | {_pct(rev.get('false_alarm_rate'))} "
        f"({rev.get('false_alarms')}/{rev.get('correct_completions_reviewed')}) |",
        f"| Wrong completions failing deterministic verification | "
        f"{ver.get('wrong_completions_failed_verification')} |",
        f"| Mean tokens per resolved run | {cost.get('mean_tokens_per_resolved_run')} |",
        f"| Median agent time per resolved run | "
        f"{cost.get('median_agent_seconds_per_resolved_run')} s |",
        f"| Escape attempts / egress attempts (all blocked) | {sec.get('escape_attempts')} / "
        f"{sec.get('egress_attempts')} |",
        f"| Prompt-injection canaries followed | {sec.get('injection_followed')} of "
        f"{sec.get('canary_runs')} canary runs (egress {sec.get('injection_egress_attempts')}, "
        f"marker {sec.get('injection_marker_written')}) |",
        "",
        "## Outcome of the agent loop",
        "",
        "| Status | Runs |",
        "|---|---|",
    ]
    lines += [f"| {k} | {v} |" for k, v in sorted(summary.get("status_counts", {}).items())]
    for title, key in (
        ("By kind", "by_kind"),
        ("By category", "by_category"),
        ("By difficulty", "by_difficulty"),
    ):
        lines += ["", f"## {title}", "", "| Group | Runs | Resolved | Rate |", "|---|---|---|---|"]
        for group, row in summary.get(key, {}).items():
            lines.append(f"| {group} | {row['runs']} | {row['resolved']} | {_pct(row['rate'])} |")
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        by_task[run["task_id"]].append(run)
    lines += [
        "",
        "## Per task",
        "",
        "| Task | Resolved / trials | Statuses | Failing checks (first trial) | Risk | Reviewer |",
        "|---|---|---|---|---|---|",
    ]
    for task_id in sorted(by_task):
        group = sorted(by_task[task_id], key=lambda r: r.get("trial", 0))
        ok = sum(1 for r in group if r.get("resolved"))
        statuses = ", ".join(str(r.get("status")) for r in group)
        failing = ", ".join((group[0].get("failed_checks") or [])[:3])
        reviewer = group[0].get("review_verdict") or "-"
        lines.append(
            f"| {task_id} | {ok}/{len(group)} | {statuses} | {failing} | "
            f"{group[0].get('risk') or '-'} | {reviewer} |"
        )
    return "\n".join(lines) + "\n"
