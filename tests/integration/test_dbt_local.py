"""Real dbt + DuckDB through LocalSandbox on a tiny project (no network, no Docker)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from dbt_agent.config import RunConfig
from dbt_agent.harness.agent import AgentRunner
from dbt_agent.harness.prompts import AgentTask
from dbt_agent.llm.fake import FakeLLM, last_tool_output, say, tool
from dbt_agent.sandbox import LocalSandbox, SandboxContext
from dbt_agent.workspace import copy_template, init_git_baseline, write_baseline_manifest

pytestmark = pytest.mark.dbt
MINI = Path(__file__).parents[1] / "fixtures" / "mini_project"


@pytest.fixture(scope="session")
def mini_template(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("mini") / "template"
    shutil.copytree(MINI, root)
    box = LocalSandbox(root, SandboxContext(db_path="warehouse.duckdb"))
    built = box.call("dbt_raw", {"argv": ["build"]}, timeout=300)
    assert built.data["exit_code"] == 0, built.output
    init_git_baseline(root)
    write_baseline_manifest(box)
    return root


@pytest.fixture
def work(mini_template, tmp_path) -> LocalSandbox:
    root = copy_template(mini_template, tmp_path / "work")
    box = LocalSandbox(root, SandboxContext(db_path="warehouse.duckdb"))
    box.start()
    return box


BROKEN = "select\n    id,\n    amout as amount,\n    customer\nfrom {{ ref('raw_orders') }}\n"


def test_build_failure_is_summarised_with_next_steps(work):
    work.call("write_file", {"path": "models/stg_orders.sql", "content": BROKEN})
    result = work.call("run_dbt", {"command": "build"})
    assert result.ok and result.data["exit_code"] != 0
    assert "FAILED" in result.output
    assert "amout" in result.output  # the binder error names the column
    assert "Skipped because an upstream node failed" in result.output
    assert "fct_customer" in result.output
    assert any(f["unique_id"] == "model.mini.stg_orders" for f in result.data["failures"])


def test_state_modified_selection(work):
    work.call(
        "edit_file",
        {
            "path": "models/fct_customer.sql",
            "old_string": "count(*) as n_orders",
            "new_string": "count(*) as n_orders,\n    max(amount) as max_amount",
        },
    )
    listed = work.call("run_dbt", {"command": "ls", "select": "state:modified"})
    assert listed.ok and "fct_customer" in listed.output and "stg_orders" not in listed.output
    built = work.call("run_dbt", {"command": "build", "select": "state:modified+"})
    assert built.data["exit_code"] == 0, built.output


def test_query_and_show(work):
    query = work.call(
        "query_duckdb",
        {"sql": "select customer, total_amount from main.fct_customer order by customer"},
    )
    assert query.ok and "alice" in query.output and "15.75" in query.output
    show = work.call("run_dbt", {"command": "show", "select": "fct_customer"})
    assert show.ok and "alice" in show.output


def test_compile_error_is_reported_before_execution(work):
    work.call(
        "write_file",
        {"path": "models/stg_orders.sql", "content": "select * from {{ ref('does_not_exist') }}"},
    )
    result = work.call("run_dbt", {"command": "build"})
    assert result.data["exit_code"] != 0
    assert "does_not_exist" in result.output


def test_agent_fixes_a_broken_model_end_to_end(mini_template, tmp_path):
    """Fake LLM + real dbt + real checklist: the full loop on a real project."""
    template = copy_template(mini_template, tmp_path / "broken_template")
    (template / "models" / "stg_orders.sql").write_text(BROKEN, encoding="utf-8")
    init_git_baseline(template, "broken start")
    write_baseline_manifest(LocalSandbox(template, SandboxContext(db_path="warehouse.duckdb")))
    work = copy_template(template, tmp_path / "work")
    box = LocalSandbox(work, SandboxContext(db_path="warehouse.duckdb"))

    def fix_if_needed(messages):
        if "amout" in last_tool_output(messages):
            return tool(
                "edit_file",
                path="models/stg_orders.sql",
                old_string="amout as amount",
                new_string="amount",
            )
        return say("unexpected output")

    script = [
        tool("run_dbt", command="build"),
        fix_if_needed,
        tool("run_dbt", command="build", select="state:modified+"),
        tool("git_diff"),
        tool(
            "finish",
            summary="stg_orders selected a misspelt column (amout). Fixed the "
            "select; dbt build of state:modified+ passes.",
        ),
    ]
    cfg = RunConfig()
    outcome = AgentRunner(
        run_dir=tmp_path / "run",
        llm=FakeLLM(script),
        sandbox=box,
        config=cfg,
        task=AgentTask(
            task_id="mini-1", title="CI failed", body="dbt build failed", kind="ci_failure"
        ),
    ).run()
    assert outcome.status == "completed", outcome
    assert outcome.checklist["build"]["exit_code"] == 0
    patch = box.call("git_patch")
    assert [f["path"] for f in patch.data["files"]] == ["models/stg_orders.sql"]
