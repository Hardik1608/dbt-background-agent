"""One background job, end to end:

    template -> work dir -> agent (sandbox) -> patch -> deterministic verification
    (fresh sandbox) -> sceptical LLM review -> risk class -> branch + PR.md

Every stage writes its result to the run directory and is skipped when that file already
exists, so ``run_job`` can be called again after a crash and continues where it stopped
(the agent stage itself resumes from its event log).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dbt_agent.config import RunConfig, VerifyConfig
from dbt_agent.harness.agent import AgentOutcome, AgentRunner
from dbt_agent.harness.prompts import AgentTask
from dbt_agent.llm.types import ChatModel
from dbt_agent.pr.package import package, render_pr
from dbt_agent.sandbox.base import SandboxContext
from dbt_agent.util import (
    atomic_write_json,
    atomic_write_text,
    read_json,
    read_text_exact,
    short,
)
from dbt_agent.verify.deterministic import Verification, Verifier
from dbt_agent.verify.reviewer import ReviewResult, build_review_prompt, review
from dbt_agent.verify.risk import assess
from dbt_agent.workspace import copy_template


@dataclass
class JobSpec:
    job_id: str
    task: AgentTask
    template_dir: Path
    context: SandboxContext
    verify: VerifyConfig = field(default_factory=VerifyConfig)


@dataclass
class JobResult:
    run_dir: Path
    outcome: dict[str, Any]
    patch_files: list[dict[str, Any]]
    verification: dict[str, Any] | None
    review: dict[str, Any] | None
    risk: dict[str, Any] | None
    pr: dict[str, Any] | None
    seconds: float

    def as_dict(self) -> dict[str, Any]:
        out = dict(self.__dict__)
        out["run_dir"] = str(self.run_dir)
        return out


def _evidence_text(verification: dict[str, Any]) -> str:
    lines = [
        f"- {c['name']}: {c['status'].upper()}"
        + (f" - {short(str(c.get('detail', '')), 300)}" if c.get("detail") else "")
        for c in verification["checks"]
    ]
    return "\n".join(lines) or "- no checks ran"


def _lineage_text(verification: dict[str, Any]) -> str:
    modified = verification.get("modified", [])
    downstream = [u for u in verification.get("downstream", []) if u.startswith("model.")]
    return (
        f"- modified: {', '.join(modified[:20]) or 'none'}\n"
        f"- downstream models: {', '.join(d.split('.')[-1] for d in downstream[:25]) or 'none'}"
        f"\n- exposures: {', '.join(verification.get('exposures', [])) or 'none'}"
    )


def run_job(
    spec: JobSpec,
    config: RunConfig,
    *,
    llm: ChatModel,
    make_sandbox,
    runs_root: Path,
    reviewer_llm: ChatModel | None = None,
) -> JobResult:
    started = time.monotonic()
    run_dir = Path(runs_root) / spec.job_id
    run_dir.mkdir(parents=True, exist_ok=True)
    workdir = run_dir / "workdir"
    if not workdir.exists():
        staging = run_dir / "workdir.tmp"
        copy_template(spec.template_dir, staging)
        staging.rename(workdir)

    box = make_sandbox(workdir, spec.context)
    try:
        outcome_path = run_dir / "outcome.json"
        if outcome_path.exists():
            outcome = AgentOutcome.from_dict(read_json(outcome_path))
        else:
            outcome = AgentRunner(
                run_dir=run_dir, task=spec.task, config=config, llm=llm, sandbox=box
            ).run()

        patch_path = run_dir / "patch.diff"
        stats_path = run_dir / "patch_files.json"
        if not patch_path.exists():
            diff = box.call("git_patch")
            atomic_write_text(patch_path, diff.output if diff.ok else "")
            atomic_write_json(stats_path, diff.data.get("files", []) if diff.ok else [])
        patch = read_text_exact(patch_path)
        patch_files = read_json(stats_path, []) or []
        box.stop()  # one sandbox at a time: the verifier starts its own (packaging restarts it)

        verification: dict[str, Any] | None = read_json(run_dir / "verification.json")
        if verification is None and outcome.status == "completed" and spec.verify.enabled:
            result: Verification = Verifier(spec.verify, make_sandbox, spec.context).verify(
                start_dir=spec.template_dir,
                patch=patch,
                workdir=run_dir / "verify_work",
                tests_changed_reason=outcome.tests_changed_reason,
            )
            verification = result.as_dict()
            atomic_write_json(run_dir / "verification.json", verification)

        review_data: dict[str, Any] | None = read_json(run_dir / "review.json")
        if review_data is None and verification is not None and config.reviewer.enabled:
            prompt = build_review_prompt(
                ticket_title=spec.task.title,
                ticket_body=spec.task.body,
                agent_summary=outcome.summary or "",
                evidence=_evidence_text(verification),
                lineage=_lineage_text(verification),
                diff=patch,
                max_diff_chars=config.reviewer.max_diff_chars,
            )
            reviewed: ReviewResult = review(reviewer_llm or llm, prompt)
            review_data = reviewed.as_dict()
            atomic_write_json(run_dir / "review.json", review_data)

        risk_data: dict[str, Any] | None = None
        pr_data: dict[str, Any] | None = read_json(run_dir / "pr.json")
        if verification is not None:
            changed = sum(
                int(f.get("added") or 0) + int(f.get("deleted") or 0)
                for f in patch_files
                if str(f.get("added", "")).isdigit()
            )
            verification_obj = _verification_from_dict(verification)
            review_obj = (
                ReviewResult(
                    **{
                        k: v
                        for k, v in (review_data or {}).items()
                        if k in ReviewResult.__dataclass_fields__
                    }
                )
                if review_data
                else None
            )
            risk_data = assess(
                verification_obj,
                review_obj,
                changed_lines=changed,
                tests_changed_reason=outcome.tests_changed_reason,
                incremental_models=list(spec.verify.incremental_models),
            ).as_dict()
            atomic_write_json(run_dir / "risk.json", risk_data)
            if pr_data is None:
                cost = config.llm.cost_eur(outcome.prompt_tokens, outcome.completion_tokens)
                note = (
                    f"Estimated API cost: EUR {cost:.4f} at the configured prices."
                    if cost > 0
                    else "Local model: no API charge (energy use not measured)."
                )
                body = render_pr(
                    task_id=spec.task.task_id,
                    title=spec.task.title,
                    kind=spec.task.kind,
                    summary=outcome.summary or "",
                    files=patch_files,
                    verification=verification,
                    review=review_data,
                    risk=risk_data,
                    outcome=outcome.as_dict(),
                    model=llm.model_name,
                    reviewer_model=(reviewer_llm or llm).model_name,
                    cost_note=note,
                )
                pr = package(
                    sandbox=box,
                    run_dir=run_dir,
                    task_id=spec.task.task_id,
                    title=spec.task.title,
                    run_id=spec.job_id,
                    body=body,
                    patch=patch,
                )
                pr_data = pr.as_dict()
                atomic_write_json(run_dir / "pr.json", pr_data)
    finally:
        box.stop()

    result = JobResult(
        run_dir=run_dir,
        outcome=outcome.as_dict(),
        patch_files=patch_files,
        verification=verification,
        review=review_data,
        risk=risk_data,
        pr=pr_data,
        seconds=round(time.monotonic() - started, 2),
    )
    atomic_write_json(run_dir / "result.json", result.as_dict())
    return result


def _verification_from_dict(data: dict[str, Any]) -> Verification:
    from dbt_agent.verify.deterministic import Check

    return Verification(
        checks=[Check(**c) for c in data.get("checks", [])],
        modified=data.get("modified", []),
        downstream=data.get("downstream", []),
        exposures=data.get("exposures", []),
        test_flags=data.get("test_flags", []),
        new_lint=data.get("new_lint", {}),
        patch_files=data.get("patch_files", []),
        seconds=data.get("seconds", 0.0),
    )
