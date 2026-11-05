"""Deterministic verifier on a real (tiny) dbt project: each check bites when it should."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from dbt_agent.config import VerifyConfig
from dbt_agent.sandbox import LocalSandbox, SandboxContext
from dbt_agent.verify.deterministic import Verifier
from dbt_agent.workspace import copy_template, init_git_baseline, write_baseline_manifest

pytestmark = [pytest.mark.dbt, pytest.mark.slow]
MINI = Path(__file__).parents[1] / "fixtures" / "mini_project"
CONFIG = VerifyConfig(
    reconciliation_tests=["assert_totals_reconcile"],
    incremental_models={"fct_events": "loaded_at"},
    sqlfluff=False,
)


@pytest.fixture(scope="module")
def template(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("verify") / "template"
    shutil.copytree(MINI, root)
    box = LocalSandbox(root, SandboxContext(db_path="warehouse.duckdb"))
    assert box.call("dbt_raw", {"argv": ["build"]}, timeout=300).data["exit_code"] == 0
    init_git_baseline(root)
    write_baseline_manifest(box)
    return root


def _patch(template: Path, tmp_path: Path, edits: dict[str, str | None]) -> str:
    work = copy_template(template, tmp_path / "edit")
    for rel, content in edits.items():
        if content is None:
            (work / rel).unlink()
        else:
            (work / rel).write_text(content, encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True, capture_output=True)
    return subprocess.run(
        ["git", "diff", "--cached", "HEAD"], cwd=work, check=True, capture_output=True
    ).stdout.decode()


def _verify(template: Path, tmp_path: Path, patch: str):
    verifier = Verifier(
        CONFIG,
        lambda root, ctx: LocalSandbox(root, ctx),
        SandboxContext(db_path="warehouse.duckdb"),
    )
    return verifier.verify(start_dir=template, patch=patch, workdir=tmp_path / "v")


def _status(result, name: str) -> str:
    check = next((c for c in result.checks if c.name.startswith(name)), None)
    return check.status if check else "missing"


def test_good_change_passes(template, tmp_path):
    sql = (
        (MINI / "models" / "fct_customer.sql")
        .read_text()
        .replace(
            "sum(amount) as total_amount",
            "sum(amount) as total_amount,\n    max(amount) as max_amount",
        )
    )
    result = _verify(
        template, tmp_path, _patch(template, tmp_path, {"models/fct_customer.sql": sql})
    )
    assert result.passed, [c.__dict__ for c in result.checks]
    assert _status(result, "incremental_equals_full_refresh") == "skip"  # not affected
    assert any(u.endswith(".fct_customer") for u in result.modified)


def test_broken_watermark_is_caught_by_incremental_check(template, tmp_path):
    sql = (
        (MINI / "models" / "fct_events.sql")
        .read_text()
        .replace(
            "where loaded_at > (select coalesce(max(loaded_at), '1900-01-01') from {{ this }})",
            "where id > (select coalesce(max(id), 0) from {{ this }})",
        )
    )
    result = _verify(template, tmp_path, _patch(template, tmp_path, {"models/fct_events.sql": sql}))
    assert _status(result, "slim_ci") == "pass"  # a full refresh hides the bug...
    assert _status(result, "incremental_equals_full_refresh") == "fail"  # ...this does not
    assert not result.passed


def test_deleted_test_is_flagged(template, tmp_path):
    schema = (
        (MINI / "models" / "schema.yml")
        .read_text()
        .replace(
            "      - name: customer\n        data_tests: [unique, not_null]\n",
            "      - name: customer\n        data_tests: [not_null]\n",
        )
    )
    result = _verify(template, tmp_path, _patch(template, tmp_path, {"models/schema.yml": schema}))
    assert _status(result, "tests_intact") == "fail"
    assert result.test_flags and not result.passed


def test_build_failure_fails_slim_ci(template, tmp_path):
    result = _verify(
        template,
        tmp_path,
        _patch(
            template,
            tmp_path,
            {
                "models/stg_orders.sql": (
                    "select id, amout as amount, customer from {{ ref('raw_orders') }}"
                )
            },
        ),
    )
    assert _status(result, "slim_ci") == "fail" and not result.passed


def test_unappliable_patch(template, tmp_path):
    result = _verify(template, tmp_path, "--- a/nope.sql\n+++ b/nope.sql\n@@ -1 +1 @@\n-x\n+y\n")
    assert _status(result, "patch_applies") == "fail"
