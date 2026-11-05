"""Reviewer parsing, risk rules, PR rendering and the GitHub publisher (dry run + mocked API)."""

from __future__ import annotations

import json
import subprocess

import httpx
import pytest

from dbt_agent.llm.fake import FakeLLM, say
from dbt_agent.pr.github import create_pull_request, plan_requests, prepare_publish_repo
from dbt_agent.pr.package import render_pr
from dbt_agent.verify.deterministic import Check, Verification
from dbt_agent.verify.reviewer import ReviewResult, build_review_prompt, review
from dbt_agent.verify.risk import assess
from tests.helpers import make_repo

APPROVE = json.dumps(
    {"verdict": "approve", "risk": "low", "findings": [], "summary": "Small, verified fix."}
)


def test_reviewer_parses_plain_and_fenced_json():
    plain = review(FakeLLM([say(APPROVE)]), "prompt")
    assert plain.available and plain.verdict == "approve" and plain.risk == "low"
    fenced = review(FakeLLM([say("Here you go:\n```json\n" + APPROVE + "\n```")]), "prompt")
    assert fenced.available and fenced.verdict == "approve"


def test_reviewer_retries_once_then_gives_up():
    second = review(FakeLLM([say("I think it is fine"), say(APPROVE)]), "prompt")
    assert second.available and second.error == "first answer was not JSON"
    never = review(FakeLLM([say("no"), say("still no")]), "prompt")
    assert not never.available and not never.flags_problem


def test_reviewer_normalises_unknown_values():
    odd = json.dumps({"verdict": "LGTM!!", "risk": "spicy", "findings": "none"})
    result = review(FakeLLM([say(odd)]), "prompt")
    assert result.verdict == "request_changes" and result.risk == "medium"
    assert result.findings == []


def test_review_prompt_truncates_long_diffs():
    prompt = build_review_prompt(
        ticket_title="t",
        ticket_body="b",
        agent_summary="s",
        evidence="- x: PASS",
        lineage="- none",
        diff="+x\n" * 9000,
        max_diff_chars=1000,
    )
    assert "diff truncated" in prompt and len(prompt) < 3000


def _verification(**overrides) -> Verification:
    v = Verification(
        checks=[Check("slim_ci", "pass", True)],
        modified=["model.p.stg"],
        downstream=["model.p.a"],
        patch_files=["models/stg.sql"],
    )
    for key, value in overrides.items():
        setattr(v, key, value)
    return v


def test_risk_low_for_small_verified_approved_change():
    review_ok = ReviewResult(available=True, verdict="approve", risk="low")
    risk = assess(
        _verification(),
        review_ok,
        changed_lines=4,
        tests_changed_reason=None,
        incremental_models=["fct"],
    )
    assert risk.level == "low"


def test_risk_rules_raise_the_class():
    review_ok = ReviewResult(available=True, verdict="approve", risk="low")
    failing = _verification(checks=[Check("slim_ci", "fail", True)])
    assert (
        assess(
            failing, review_ok, changed_lines=1, tests_changed_reason=None, incremental_models=[]
        ).level
        == "high"
    )
    flags = _verification(test_flags=[{"kind": "test_removed_or_weakened"}])
    assert (
        assess(
            flags, review_ok, changed_lines=1, tests_changed_reason="asked", incremental_models=[]
        ).level
        == "high"
    )
    incremental = _verification(modified=["model.p.fct"])
    assert (
        assess(
            incremental,
            review_ok,
            changed_lines=1,
            tests_changed_reason=None,
            incremental_models=["fct"],
        ).level
        == "high"
    )
    seeds = _verification(patch_files=["seeds/x.csv"])
    assert (
        assess(
            seeds, review_ok, changed_lines=1, tests_changed_reason=None, incremental_models=[]
        ).level
        == "medium"
    )
    unavailable = assess(
        _verification(), None, changed_lines=1, tests_changed_reason=None, incremental_models=[]
    )
    assert unavailable.level == "medium"
    blocker = ReviewResult(
        available=True,
        verdict="request_changes",
        risk="low",
        findings=[{"severity": "blocker", "issue": "drops rows"}],
    )
    assert (
        assess(
            _verification(),
            blocker,
            changed_lines=1,
            tests_changed_reason=None,
            incremental_models=[],
        ).level
        == "high"
    )


def test_pr_body_has_evidence_lineage_review_risk_and_cost():
    verification = _verification(
        checks=[
            Check("patch_applies", "pass", True, "1 file(s)"),
            Check("incremental_equals_full_refresh:fct", "pass", True, "identical"),
            Check("sqlfluff_new_violations", "warn", False, "2 new violation(s)"),
        ]
    ).as_dict()
    body = render_pr(
        task_id="T1",
        title="Fix stg",
        kind="ci_failure",
        summary="Renamed col.",
        files=[{"path": "models/stg.sql", "added": "1", "deleted": "1"}],
        verification=verification,
        review={
            "available": True,
            "verdict": "approve",
            "risk": "low",
            "summary": "ok",
            "findings": [],
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "seconds": 2,
        },
        risk={"level": "medium", "reasons": ["medium: new sqlfluff violations"]},
        outcome={
            "turns": 5,
            "tool_calls": 9,
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "wall_seconds": 95,
        },
        model="m",
        reviewer_model="r",
        cost_note="Local model.",
    )
    for needle in (
        "A human must review",
        "Incremental = full refresh (`fct`)",
        "WARN",
        "Lineage impact",
        "**approve**",
        "**MEDIUM**",
        "| Agent (`m`) | 5 | 9",
        "Reproduce",
    ):
        assert needle in body, needle


def test_github_dry_run_only_plans():
    result = create_pull_request(
        repo="o/r", publish_dir=None, branch="agent/x", base="main", title="t", body="b", risk="low"
    )
    assert result.dry_run and result.url is None
    assert [step.get("method") or step["action"] for step in result.plan] == [
        "git push",
        "POST",
        "POST",
    ]
    assert result.plan[1]["json"]["draft"] is True


def test_github_real_mode_with_mocked_api(monkeypatch, tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url), request.headers.get("authorization")))
        if request.url.path.endswith("/pulls"):
            return httpx.Response(201, json={"number": 7, "html_url": "https://gh/pr/7"})
        return httpx.Response(200, json={})

    monkeypatch.setenv("GITHUB_TOKEN", "tok-123")
    result = create_pull_request(
        repo="o/r",
        publish_dir=tmp_path,
        branch="agent/x",
        base="main",
        title="t",
        body="b",
        risk="high",
        dry_run=False,
        transport=httpx.MockTransport(handler),
        push=False,
    )
    assert result.url == "https://gh/pr/7" and result.number == 7
    assert calls[0][2] == "Bearer tok-123"
    assert calls[1][1].endswith("/issues/7/labels")


def test_github_without_token_fails_cleanly(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    result = create_pull_request(
        repo="o/r",
        publish_dir=None,
        branch="b",
        base="main",
        title="t",
        body="b",
        risk="low",
        dry_run=False,
    )
    assert result.error and "GITHUB_TOKEN" in result.error


def test_publish_repo_is_built_from_the_trusted_template(tmp_path):
    template = make_repo(tmp_path / "template", {"models/a.sql": "select 1\n"})
    patch = "--- a/models/a.sql\n+++ b/models/a.sql\n@@ -1 +1 @@\n-select 1\n+select 2\n"
    sha = prepare_publish_repo(template, patch, tmp_path / "publish", "agent/x", "msg")
    assert len(sha) == 40
    assert (tmp_path / "publish" / "models" / "a.sql").read_text() == "select 2\n"
    branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=tmp_path / "publish",
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branch == "agent/x"
    with pytest.raises(ValueError):
        prepare_publish_repo(template, "new file mode 120000\n", tmp_path / "p2", "b", "m")


def test_plan_requests_label_includes_risk():
    plan = plan_requests("o/r", "b", "main", "t", "body", "medium")
    assert plan[2]["json"]["labels"] == ["agent", "risk:medium"]
