"""Rewrite the Results section of README.md from reports/*.json (no number is typed by hand).

    python scripts/readme_results.py          # rewrites the block between the markers
    python scripts/readme_results.py --print  # prints it instead

Only reports that exist are used; anything missing is shown as "not measured".
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
START, END = "<!-- results:start -->", "<!-- results:end -->"
# Runs still going when the README was generated (reports/run_status.json, written by hand
# from the box queue; it holds no numbers, only what is pending).
_status = REPORTS / "run_status.json"
PENDING: dict[str, str] = (
    json.loads(_status.read_text(encoding="utf-8")) if _status.exists() else {}
)

SPIDER2_CONTEXT = (
    "Leaderboard context (vendor-reported, not re-run here; "
    "[spider2-sql.github.io](https://spider2-sql.github.io/), read 2026-10-14): SignalPilot "
    "Agent 65.6, Databao Agent 60.29, Shadowfax-DBT-Agent + GPT-5 41.18, Spider-Agent-DBT + "
    "GPT-5.4 35.29."
)
ADE_CONTEXT = (
    "Published context: dbt Labs reported a 56% pass rate for OpenAI Codex with GPT-5.1 and the "
    "same for Claude Sonnet 4.5, and cautioned that the absolute number is not meaningful "
    "([blog, 2026-01-21](https://www.getdbt.com/blog/ade-bench-dbt-data-benchmarking)); the "
    "task set has changed since."
)


def load(name: str) -> dict[str, Any] | None:
    path = REPORTS / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def pct(x: float | None, digits: int = 0) -> str:
    return "n/a" if x is None else f"{100 * x:.{digits}f}%"


def ci(pair: list[float] | None) -> str:
    return "" if not pair else f" (95% CI {100 * pair[0]:.0f}-{100 * pair[1]:.0f}%)"


def frac(num: int | None, den: int | None) -> str:
    return "n/a" if num is None or not den else f"{num}/{den}"


def secs(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"{x / 60:.1f} min" if x >= 90 else f"{x:.0f} s"


def risk_split(report: dict[str, Any]) -> str:
    """Risk classes of the PRs that were opened, split by whether they were right."""
    done = [r for r in report["runs"] if r.get("status") == "completed"]

    def counts(rows: list[dict[str, Any]]) -> str:
        return "/".join(
            str(sum(1 for r in rows if r.get("risk") == lvl)) for lvl in ("high", "medium", "low")
        )

    return (
        f"{counts([r for r in done if r.get('resolved')])}; "
        f"{counts([r for r in done if not r.get('resolved')])}"
    )


def sales_column(report: dict[str, Any] | None, *, scripted: bool = False) -> dict[str, str]:
    if not report:
        return {}
    s = report["summary"]
    kind = s.get("by_kind", {})
    ver, rev, cost, sec = s["verifier"], s["reviewer"], s["cost"], s["security"]
    wrong = s.get("completed_but_unresolved") or 0
    return {
        "Resolved": f"**{frac(s['resolved_runs'], s['runs'])}** = {pct(s['resolve_rate'])}"
        + ci(s.get("resolve_rate_wilson_95")),
        "Breakages; feature tickets": "; ".join(
            frac(kind.get(k, {}).get("resolved"), kind.get(k, {}).get("runs"))
            for k in ("breakage", "feature")
        ),
        "Correct patch, but out of budget before finishing (no PR)": str(
            s.get("correct_but_unfinished", 0)
        ),
        "Finished but wrong (hidden tests failed)": str(wrong),
        "... of those, stopped by the deterministic verifier": str(
            ver.get("wrong_completions_failed_verification", 0)
        ),
        "... flagged by the LLM reviewer": (
            frac(rev.get("wrong_completions_flagged"), rev.get("wrong_completions_reviewed"))
            if rev.get("wrong_completions_reviewed")
            else "n/a"
        ),
        "Final patch broke a test or changed other data": (
            f"{s.get('runs_with_regressions', 0)} runs; "
            f"{rev.get('regression_runs_reviewed', 0)} of them became PRs"
            + (
                f" (reviewer flagged {rev.get('regression_runs_flagged', 0)})"
                if rev.get("regression_runs_reviewed")
                else ""
            )
        ),
        "Risk class of correct PRs; of wrong PRs (high/medium/low)": risk_split(report),
        "Reviewer false alarms on correct changes": (
            frac(rev.get("false_alarms"), rev.get("correct_completions_reviewed"))
            if rev.get("correct_completions_reviewed")
            else "n/a"
        ),
        "Correct changes failing verification": str(
            ver.get("correct_completions_failed_verification", 0)
        ),
        "Median agent time per resolved task": secs(
            cost.get("median_agent_seconds_per_resolved_run")
        ),
        "Mean tokens per resolved task": (
            "n/a (no model)"
            if scripted
            else f"{cost['mean_tokens_per_resolved_run']:,.0f}"
            if cost.get("mean_tokens_per_resolved_run")
            else "n/a"
        ),
        "Prompt-injection canaries followed": frac(
            sec.get("injection_followed"), sec.get("canary_runs")
        ),
        "Sandbox escape / egress attempts": f"{sec.get('escape_attempts', 0)} / "
        f"{sec.get('egress_attempts', 0)}",
    }


def sales_section() -> list[str]:
    validation = load("suite_validation")
    oracle, model = load("sales_oracle"), load("sales_local_agent_k1")
    lines = ["### Sales suite (50 tasks)", ""]
    if validation:
        v = validation["summary"]
        lines += [
            f"Every task was validated first: {v['fail_before']}/{v['tasks']} fail with an empty "
            f"patch and {v['pass_with_reference']}/{v['tasks']} pass with their reference fix "
            "(`reports/suite_validation.md`).",
            "",
        ]
    cols = {
        "Oracle (scripted reference fix)": sales_column(oracle, scripted=True),
        "Local model, 1 trial": sales_column(model),
    }
    if not any(cols.values()):
        return [*lines, "Not measured yet.", ""]
    keys = list(next(c for c in cols.values() if c))
    lines += ["| | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
    for key in keys:
        lines.append(
            f"| {key} | " + " | ".join(c.get(key, "not measured") for c in cols.values()) + " |"
        )
    if model:
        meta = model.get("meta", {})
        cfg = meta.get("config", {})
        lines += [
            "",
            f"Model: `{meta.get('model', 'local-agent')}` = Qwen3.5-9B int4 on vLLM (one RTX "
            "3060 shared with other projects), sampling as in `configs/local_agent.yaml` "
            f"(temperature {cfg.get('temperature', 'n/a')}, top_p {cfg.get('top_p', 'n/a')}, "
            "top_k 20, presence_penalty 1.0); budgets per task: "
            f"{cfg.get('budgets', {}).get('max_turns', 'n/a')} turns, "
            f"{secs(cfg.get('budgets', {}).get('max_wall_seconds'))}. The same model is the "
            "reviewer. Every agent ran in the Docker sandbox. Sources: "
            "`reports/sales_local_agent_k1.md`, `reports/sales_oracle.md`.",
        ]
    k3 = load("sales_local_agent_k3")
    if k3:
        s = k3["summary"]
        lines += [
            "",
            f"Reliability over {s['trials_per_task']} trials per task ({s['tasks']} tasks): "
            f"pass^{s['k']} = {pct(s['pass_hat_k'])}, pass@{s['k']} = {pct(s['pass_at_k'])} "
            "(`reports/sales_local_agent_k3.md`).",
        ]
    else:
        lines += ["", "pass^3: not measured."]
    return [*lines, ""]


def spider2_section() -> list[str]:
    report = load("spider2_dbt")
    lines = ["### Spider 2.0-DBT (68 tasks, official `evaluate.py`)", ""]
    if not report:
        return [*lines, f"{PENDING.get('spider2', 'Not measured.')}", "", SPIDER2_CONTEXT, ""]
    s = report["summary"]
    off = s["official"]
    issues = s.get("public_data_issues") or {}
    unscoreable = sum(len(v) for v in issues.values())
    status = s.get("status_counts") or {}
    kinds: dict[str, int] = {}
    for run in report.get("runs", []):
        for kind, n in ((run.get("security") or {}).get("by_kind") or {}).items():
            kinds[kind] = kinds.get(kind, 0) + n
    lines += [
        f"**{off['official_passed']}/{s['tasks_total']} = {pct(s['score_over_all_68'], 1)}** "
        f"with the local model, one trial per task (`reports/spider2_dbt.md`). "
        f"{unscoreable} instances cannot pass with the public gold data (listed in the report) "
        "and still count in the denominator.",
        "",
        f"The agent finished {status.get('completed', 0)} runs; "
        f"{status.get('budget_exhausted', 0)} ran out of budget, "
        f"{status.get('loop_detected', 0)} were stopped by the loop detector and "
        f"{status.get('llm_error', 0)} ended on a model-server error. "
        f"{s.get('runs_with_all_gold_tables', 0)} of {s['runs']} runs produced every table the "
        f"gold spec compares. Escape attempts: {s.get('escape_attempts', 0)}; "
        f"network-dependent commands refused: {kinds.get('blocked_command', 0)} "
        "(all `dbt deps`, `reports/spider2_security_events.json`; the sandbox has no network).",
        "",
        SPIDER2_CONTEXT,
        "",
    ]
    return lines


def ade_section() -> list[str]:
    report = load("adebench")
    lines = ["### ADE-bench (dbt Labs; DuckDB + dbt tasks, ADE's own harness and tests)", ""]
    if not report:
        return [*lines, f"{PENDING.get('adebench', 'Not measured.')}", "", ADE_CONTEXT, ""]
    s = report["summary"]
    lines += [
        f"**{s['resolved']}/{s['tasks']} = {pct(s['resolve_rate'], 1)}** with the local model, "
        "one trial per task (`reports/adebench.md`).",
        "",
        ADE_CONTEXT,
        "",
    ]
    return lines


def render() -> str:
    lines = [
        START,
        "## Results",
        "",
        "Generated by `scripts/readme_results.py` from `reports/` (JSON + Markdown written by "
        "the runs). Anything not in a report says so. *Resolved* means the agent finished, so "
        "a verified PR exists, and the hidden acceptance tests pass.",
        "",
        *sales_section(),
        *spider2_section(),
        *ade_section(),
        END,
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--print", action="store_true")
    args = parser.parse_args()
    block = render()
    if args.print:
        print(block)
        return 0
    readme = ROOT / "README.md"
    text = readme.read_text(encoding="utf-8")
    if START in text and END in text:
        head, rest = text.split(START, 1)
        _, tail = rest.split(END, 1)
        text = head + block + tail
    else:
        raise SystemExit("README.md has no results markers")
    readme.write_text(text, encoding="utf-8", newline="\n")
    print("README.md results updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
