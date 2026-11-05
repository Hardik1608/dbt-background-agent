"""Package an agent run as a reviewable pull request: branch + commit, patch, PR.md.

PR.md carries what a reviewer needs to decide quickly: the summary, the deterministic test
evidence from a fresh sandbox, the lineage impact, the sceptical LLM review, the risk class
and the cost. A human merges; nothing here pushes unless ``pr create`` is run without
``--dry-run``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dbt_agent.sandbox.base import Sandbox
from dbt_agent.util import atomic_write_text, short, slug

STATUS_LABEL = {"pass": "PASS", "fail": "FAIL", "warn": "WARN", "skip": "skipped", "error": "ERROR"}
CHECK_TITLES = {
    "patch_applies": "Patch applies to the starting commit",
    "parses": "Project parses",
    "tests_intact": "No test or contract removed or weakened",
    "slim_ci": "Slim CI: `dbt build -s state:modified+ --full-refresh`",
    "reconciliation": "Reconciliation tests",
    "sqlfluff_new_violations": "sqlfluff (new violations only)",
    "verifier": "Verifier",
}


@dataclass
class PullRequest:
    branch: str
    commit: str | None
    title: str
    body_path: Path
    patch_path: Path

    def as_dict(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "commit": self.commit,
            "title": self.title,
            "body": str(self.body_path),
            "patch": str(self.patch_path),
        }


def _fmt_seconds(seconds: float) -> str:
    return f"{seconds / 60:.1f} min" if seconds >= 90 else f"{seconds:.0f} s"


def render_pr(
    *,
    task_id: str,
    title: str,
    kind: str,
    summary: str,
    files: list[dict[str, Any]],
    verification: dict[str, Any] | None,
    review: dict[str, Any] | None,
    risk: dict[str, Any],
    outcome: dict[str, Any],
    model: str,
    reviewer_model: str,
    cost_note: str,
) -> str:
    lines = [
        f"# [agent] {title}",
        "",
        f"> Opened by dbt-background-agent for `{task_id}` ({kind}). **A human must review "
        f"and merge.** Risk class: **{risk['level'].upper()}**.",
        "",
        "## Summary",
        "",
        summary.strip() or "_The agent gave no summary._",
        "",
        "## Changes",
        "",
        "| File | Added | Removed |",
        "|---|---:|---:|",
    ]
    lines += [
        f"| `{f['path']}` | {f.get('added', '?')} | {f.get('deleted', '?')} |" for f in files
    ] or ["| (none) | | |"]
    lines += [
        "",
        "## Test evidence",
        "",
        "Re-run from the starting commit in a fresh sandbox with only this patch applied.",
        "",
        "| Check | Result | Details |",
        "|---|---|---|",
    ]
    if verification:
        for check in verification["checks"]:
            name = check["name"]
            title_ = CHECK_TITLES.get(name, name.replace("_", " "))
            if name.startswith("incremental_equals_full_refresh:"):
                title_ = f"Incremental = full refresh (`{name.split(':', 1)[1]}`)"
            detail = short(str(check.get("detail") or ""), 220).replace("|", "/")
            lines.append(
                f"| {title_} | {STATUS_LABEL.get(check['status'], check['status'])} | {detail} |"
            )
        lines.append(f"\nVerification took {_fmt_seconds(verification.get('seconds', 0))}.")
    else:
        lines.append("| Verification | not run | the agent did not finish |")

    lines += ["", "## Lineage impact", ""]
    if verification:
        modified = verification.get("modified", [])
        downstream = verification.get("downstream", [])
        mod_nodes = [u for u in modified if not u.startswith(("test.", "unit_test."))]
        mod_tests = len(modified) - len(mod_nodes)
        lines.append(
            "- Modified (dbt `state:modified`, incl. macro changes): "
            + (", ".join(f"`{u}`" for u in mod_nodes[:15]) or "no models")
            + (f"; plus {mod_tests} test(s) on them" if mod_tests else "")
        )
        models = [u for u in downstream if u.startswith("model.")]
        tests = [u for u in downstream if u.startswith(("test.", "unit_test."))]
        lines.append(
            f"- Downstream, rebuilt and tested: {len(models)} model(s), "
            f"{len(tests)} test(s)"
            + (": " + ", ".join(f"`{u.split('.')[-1]}`" for u in models[:15]) if models else "")
        )
        exposures = verification.get("exposures", [])
        lines.append("- Exposures: " + (", ".join(f"`{e}`" for e in exposures) or "none"))
    else:
        lines.append("- not computed")

    lines += ["", "## Review", ""]
    if review and review.get("available"):
        lines.append(
            f"Sceptical LLM review (`{reviewer_model}`): **{review['verdict']}**, "
            f"risk **{review['risk']}**."
        )
        if review.get("summary"):
            lines += ["", f"> {short(review['summary'], 800)}"]
        for finding in review.get("findings", [])[:8]:
            where = f" (`{finding.get('file')}`)" if finding.get("file") else ""
            lines.append(
                f"- [{finding.get('severity', '?')}] "
                f"{short(str(finding.get('issue', '')), 300)}{where}"
            )
    else:
        lines.append(
            "LLM review unavailable"
            + (f": {review.get('error')}" if review and review.get("error") else ".")
        )

    lines += ["", "## Risk", "", f"**{risk['level'].upper()}**", ""]
    lines += [f"- {reason}" for reason in risk.get("reasons", [])]

    lines += [
        "",
        "## Cost",
        "",
        "| | Turns | Tool calls | Prompt tokens | Completion tokens | Wall time |",
        "|---|---:|---:|---:|---:|---:|",
        f"| Agent (`{model}`) | {outcome.get('turns', 0)} | {outcome.get('tool_calls', 0)} "
        f"| {outcome.get('prompt_tokens', 0):,} | {outcome.get('completion_tokens', 0):,} "
        f"| {_fmt_seconds(outcome.get('wall_seconds', 0))} |",
    ]
    if review:
        lines.append(
            f"| Reviewer | 1 | 0 | {review.get('prompt_tokens', 0):,} "
            f"| {review.get('completion_tokens', 0):,} "
            f"| {_fmt_seconds(review.get('seconds', 0))} |"
        )
    lines += [
        "",
        cost_note,
        "",
        "## Reproduce",
        "",
        "```bash",
        "git apply patch.diff",
        "dbt build --select state:modified+ --state <production manifest> --full-refresh",
        "```",
        "",
    ]
    return "\n".join(lines)


def package(
    *, sandbox: Sandbox, run_dir: Path, task_id: str, title: str, run_id: str, body: str, patch: str
) -> PullRequest:
    out = run_dir / "pr"
    out.mkdir(parents=True, exist_ok=True)
    patch_path = out / "patch.diff"
    atomic_write_text(patch_path, patch)
    body_path = out / "PR.md"
    atomic_write_text(body_path, body)
    branch = f"agent/{slug(task_id, 40)}-{run_id[-8:]}"
    commit = None
    if patch.strip():
        response = sandbox.call(
            "git_commit", {"branch": branch, "message": f"[agent] {title}\n\n{task_id}"}
        )
        commit = response.data.get("commit") if response.ok else None
    return PullRequest(
        branch=branch,
        commit=commit,
        title=f"[agent] {title}",
        body_path=body_path,
        patch_path=patch_path,
    )
