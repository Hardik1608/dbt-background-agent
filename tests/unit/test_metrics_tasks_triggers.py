"""Benchmark metrics, the task suite definitions, triggers and the CI-log ticket."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from pathlib import Path

import pytest
from pydantic import ValidationError

from dbt_agent.bench.metrics import pass_at_k, pass_hat_k, summarise, wilson
from dbt_agent.cli_run import ticket_from_run_results
from dbt_agent.tasks.model import Op, TaskSpec, load_suite
from dbt_agent.tasks.ops import OpError, apply_file_op
from dbt_agent.triggers import FileQueue, job_from_github_event, verify_signature

SUITE = Path(__file__).parents[2] / "tasks" / "sales"


def test_pass_hat_k_matches_tau_bench_definition():
    assert pass_hat_k(3, 3, 3) == 1.0
    assert pass_hat_k(2, 3, 3) == 0.0
    assert math.isclose(pass_hat_k(2, 4, 2), 1 / 6)
    assert pass_at_k(1, 3, 3) == 1.0 and pass_at_k(0, 3, 3) == 0.0
    low, high = wilson(5, 10)
    assert 0.2 < low < 0.5 < high < 0.8


def _run(task, trial, resolved, **extra):
    return {
        "task_id": task,
        "trial": trial,
        "resolved": resolved,
        "kind": "breakage",
        "category": "c",
        "difficulty": "easy",
        "status": "completed",
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "agent_seconds": 60,
        **extra,
    }


def test_summarise_counts_and_reviewer_catch_rate():
    runs = [
        _run("A", 0, True, review_available=True, review_flags_problem=False),
        _run("A", 1, True, review_available=True, review_flags_problem=True),
        _run("A", 2, True, review_available=True, review_flags_problem=False),
        _run(
            "B",
            0,
            False,
            review_available=True,
            review_flags_problem=True,
            regressions=["test.x: fail"],
        ),
        _run("B", 1, True, review_available=True, review_flags_problem=False),
        _run("B", 2, False, review_available=True, review_flags_problem=False),
        {"task_id": "C", "trial": 0, "resolved": False, "status": "infrastructure_error"},
    ]
    summary = summarise(runs, k=3)
    assert summary["runs"] == 6 and summary["infrastructure_errors"] == 1
    assert summary["resolve_rate"] == round(4 / 6, 4)
    assert summary["pass_hat_k"] == 0.5  # A: 3/3 -> 1, B: 1/3 -> 0
    assert summary["reviewer"]["wrong_completions_reviewed"] == 2
    assert summary["reviewer"]["catch_rate_wrong"] == 0.5
    assert summary["reviewer"]["false_alarm_rate"] == 0.25
    assert summary["runs_with_regressions"] == 1
    assert summary["cost"]["mean_tokens_per_resolved_run"] == 110


def test_a_correct_patch_without_a_finished_run_is_not_resolved():
    runs = [
        _run("A", 0, True),
        _run("B", 0, True, status="budget_exhausted"),  # right patch, but no PR
        _run("C", 0, False),
    ]
    summary = summarise(runs)
    assert summary["resolved_runs"] == 1 and summary["resolve_rate"] == round(1 / 3, 4)
    assert summary["patch_correct_runs"] == 2 and summary["correct_but_unfinished"] == 1
    assert summary["by_kind"]["breakage"]["resolved"] == 1


def test_suite_has_50_valid_task_specs():
    tasks = load_suite(SUITE)
    assert len(tasks) == 50
    assert sum(t.kind == "breakage" for t in tasks) == 30
    assert sum(t.kind == "feature" for t in tasks) == 20
    assert len({t.id for t in tasks}) == 50
    for task in tasks:
        assert task.reference_fix, task.id
        assert (
            task.hidden.assertions
            or task.hidden.dbt_tests
            or task.hidden.manifest
            or task.hidden.mutation_tests
            or task.hidden.exposures
        ), task.id
    assert sum(1 for t in tasks if t.canary) == 5


def test_reference_fix_may_not_change_data():
    with pytest.raises(ValidationError):
        TaskSpec(
            id="x",
            kind="breakage",
            category="c",
            title="t",
            trigger="ci_failure",
            reference_fix=[{"op": "sql", "sql": "delete from t"}],
        )


def test_ops_fail_loudly(tmp_path):
    (tmp_path / "m.sql").write_text("a\na\n", encoding="utf-8")
    with pytest.raises(OpError, match="expected 1"):
        apply_file_op(tmp_path, Op(op="replace", path="m.sql", old="a", new="b"))
    apply_file_op(tmp_path, Op(op="replace", path="m.sql", old="a", new="b", count=2))
    assert (tmp_path / "m.sql").read_text() == "b\nb\n"
    with pytest.raises(OpError, match="escapes"):
        apply_file_op(tmp_path, Op(op="write", path="../x.sql", content="x"))


def test_webhook_signature():
    body = b'{"a": 1}'
    good = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert verify_signature("s3cret", body, good)
    assert not verify_signature("s3cret", body + b" ", good)
    assert not verify_signature("s3cret", body, None)


def test_github_events_become_jobs():
    issue = {
        "action": "labeled",
        "label": {"name": "dbt-agent"},
        "repository": {"full_name": "acme/warehouse"},
        "issue": {"number": 12, "title": "Add weekend flag", "body": "Please add..."},
    }
    job = job_from_github_event("issues", issue)
    assert job.kind == "ticket" and job.job_id == "gh-acme-warehouse-issue-12"
    assert job_from_github_event("issues", {**issue, "label": {"name": "bug"}}) is None
    run = {
        "action": "completed",
        "repository": {"full_name": "acme/warehouse"},
        "workflow_run": {
            "id": 99,
            "name": "dbt CI",
            "conclusion": "failure",
            "head_branch": "main",
            "head_sha": "abc",
            "html_url": "u",
        },
    }
    ci = job_from_github_event("workflow_run", run)
    assert ci.kind == "ci_failure" and "dbt CI" in ci.title
    passed = {**run, "workflow_run": {**run["workflow_run"], "conclusion": "success"}}
    assert job_from_github_event("workflow_run", passed) is None


def test_queue_is_idempotent_and_recovers(tmp_path):
    queue = FileQueue(tmp_path / "q")
    job = job_from_github_event(
        "issues",
        {
            "action": "labeled",
            "label": {"name": "dbt-agent"},
            "repository": {"full_name": "a/b"},
            "issue": {"number": 1, "title": "t", "body": "b"},
        },
    )
    assert queue.submit(job) and not queue.submit(job)  # redelivered webhook
    claimed = queue.claim()
    assert claimed.job_id == job.job_id and queue.claim() is None
    assert queue.recover() == 1  # crash while running -> back to pending
    queue.finish(queue.claim().job_id, ok=True, result={"status": "completed"})
    assert queue.state_of(job.job_id) == "done" and not queue.submit(job)


def test_ticket_from_failed_run_results(tmp_path):
    path = tmp_path / "run_results.json"
    path.write_text(
        json.dumps(
            {
                "args": {"which": "build"},
                "results": [
                    {"unique_id": "model.p.stg", "status": "error", "message": "Binder Error: x"},
                    {"unique_id": "model.p.fct", "status": "skipped", "message": None},
                    {"unique_id": "test.p.u", "status": "pass", "message": None},
                ],
            }
        ),
        encoding="utf-8",
    )
    body = ticket_from_run_results(path)
    assert "1 failure(s) and 1 skipped" in body
    assert "[ERROR] model.p.stg: Binder Error: x" in body
