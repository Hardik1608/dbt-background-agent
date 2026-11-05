"""The whole job on a real (tiny) dbt project: agent -> patch -> verifier -> risk -> PR."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from dbt_agent.config import RunConfig, VerifyConfig
from dbt_agent.harness.prompts import AgentTask
from dbt_agent.llm.fake import FakeLLM, tool
from dbt_agent.pipeline import JobSpec, run_job
from dbt_agent.sandbox import LocalSandbox, SandboxContext
from dbt_agent.workspace import init_git_baseline, write_baseline_manifest

pytestmark = [pytest.mark.dbt, pytest.mark.slow]
MINI = Path(__file__).parents[1] / "fixtures" / "mini_project"
CONTEXT = SandboxContext(db_path="warehouse.duckdb")


@pytest.fixture(scope="module")
def template(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("pipeline") / "template"
    shutil.copytree(MINI, root)
    box = LocalSandbox(root, CONTEXT)
    assert box.call("dbt_raw", {"argv": ["build"]}, timeout=300).data["exit_code"] == 0
    init_git_baseline(root)
    write_baseline_manifest(box)
    return root


class CountingFactory:
    """Sandbox factory that records how many sandboxes are alive at once."""

    def __init__(self) -> None:
        self.live = 0
        self.max_live = 0

    def __call__(self, root: Path, context: SandboxContext) -> LocalSandbox:
        factory = self

        class Counted(LocalSandbox):
            def start(self) -> None:
                if not self.started:
                    factory.live += 1
                    factory.max_live = max(factory.max_live, factory.live)
                super().start()

            def stop(self) -> None:
                if self.started:
                    factory.live -= 1
                super().stop()

        return Counted(root, context)


def test_job_end_to_end_with_one_sandbox_at_a_time(template, tmp_path):
    sql = (
        (MINI / "models" / "fct_customer.sql")
        .read_text(encoding="utf-8")
        .replace(
            "sum(amount) as total_amount",
            "sum(amount) as total_amount,\n    max(amount) as max_amount",
        )
    )
    llm = FakeLLM(
        [
            tool("read_file", path="models/fct_customer.sql"),
            tool("write_file", path="models/fct_customer.sql", content=sql),
            tool("run_dbt", command="build", select="state:modified+"),
            tool("finish", summary="Added max_amount to fct_customer; the build passes."),
        ]
    )
    config = RunConfig()
    config.reviewer.enabled = False
    verify = VerifyConfig(
        reconciliation_tests=["assert_totals_reconcile"],
        incremental_models={"fct_events": "loaded_at"},
        sqlfluff=False,
    )
    spec = JobSpec(
        job_id="job1",
        task=AgentTask(
            task_id="T1",
            title="Add max_amount",
            body="Add max(amount) to fct_customer.",
            kind="ticket",
        ),
        template_dir=template,
        context=CONTEXT,
        verify=verify,
    )
    factory = CountingFactory()
    result = run_job(spec, config, llm=llm, make_sandbox=factory, runs_root=tmp_path / "runs")
    assert result.outcome["status"] == "completed"
    assert result.verification and result.verification["passed"]
    assert result.risk and result.pr and result.pr["commit"]
    assert factory.max_live == 1, "the agent's sandbox must stop before the verifier starts"
    assert factory.live == 0
    body = (tmp_path / "runs" / "job1" / "pr" / "PR.md").read_text(encoding="utf-8")
    assert "## Test evidence" in body and "fct_customer" in body
    # idempotent: a second call reuses every stage's result and runs nothing again
    again = run_job(
        spec, config, llm=FakeLLM([]), make_sandbox=factory, runs_root=tmp_path / "runs"
    )
    assert again.pr == result.pr and again.verification == result.verification
