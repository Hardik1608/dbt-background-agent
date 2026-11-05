"""Risk class of an agent PR: low / medium / high, with the reasons.

Rules first (deterministic signals), then the reviewer can only raise the class, never
lower it. Like Zalando's "auto-approvable low-risk PRs", *low* means: small, isolated,
fully verified and approved by the reviewer. Nothing is ever merged automatically here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from dbt_agent.verify.deterministic import Verification
from dbt_agent.verify.reviewer import ReviewResult

ORDER = {"low": 0, "medium": 1, "high": 2}
SENSITIVE_PATHS = ("dbt_project.yml", "packages.yml", "profiles.yml", "snapshots/", "seeds/")


@dataclass
class Risk:
    level: str = "low"
    reasons: list[str] = field(default_factory=list)

    def raise_to(self, level: str, reason: str) -> None:
        if ORDER[level] > ORDER[self.level]:
            self.level = level
        self.reasons.append(f"{level}: {reason}")

    def as_dict(self) -> dict[str, Any]:
        return {"level": self.level, "reasons": self.reasons}


def assess(
    verification: Verification | None,
    review: ReviewResult | None,
    *,
    changed_lines: int,
    tests_changed_reason: str | None,
    incremental_models: list[str],
) -> Risk:
    risk = Risk()
    if verification is None:
        risk.raise_to("high", "not verified")
        return risk
    for check in verification.checks:
        if check.blocking and check.status in {"fail", "error"}:
            risk.raise_to("high", f"deterministic check failed: {check.name}")
    if verification.test_flags:
        risk.raise_to(
            "high",
            "tests or contracts were removed or weakened"
            + (" (justified by the agent)" if tests_changed_reason else ""),
        )
    touched_incremental = [
        m for m in incremental_models if any(u.endswith(f".{m}") for u in verification.modified)
    ]
    if touched_incremental:
        risk.raise_to("high", f"changes incremental model(s): {', '.join(touched_incremental)}")
    sensitive = [p for p in verification.patch_files if p.startswith(SENSITIVE_PATHS)]
    if sensitive:
        risk.raise_to(
            "medium", f"touches project config, seeds or snapshots: {', '.join(sensitive)}"
        )
    downstream_models = [u for u in verification.downstream if u.startswith("model.")]
    if len(downstream_models) > 10:
        risk.raise_to("high", f"{len(downstream_models)} downstream models")
    elif len(downstream_models) > 3:
        risk.raise_to("medium", f"{len(downstream_models)} downstream models")
    if verification.exposures:
        risk.raise_to("medium", f"feeds exposures: {', '.join(verification.exposures)}")
    if verification.new_lint:
        risk.raise_to("medium", "new sqlfluff violations")
    if changed_lines > 150:
        risk.raise_to("medium", f"large diff ({changed_lines} changed lines)")
    if review is None or not review.available:
        risk.raise_to("medium", "LLM review unavailable")
    else:
        if review.risk in ORDER:
            risk.raise_to(review.risk, f"reviewer risk {review.risk}")
        if review.verdict == "request_changes":
            risk.raise_to("medium", "reviewer requested changes")
        if any(f.get("severity") == "blocker" for f in review.findings):
            risk.raise_to("high", "reviewer found a blocker")
    if not risk.reasons:
        risk.reasons.append("low: small verified change, reviewer approved")
    return risk
