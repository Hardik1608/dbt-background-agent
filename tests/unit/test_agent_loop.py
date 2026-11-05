"""The agent loop with a scripted fake LLM and an in-memory sandbox."""

from __future__ import annotations

import pytest

from dbt_agent.config import RunConfig
from dbt_agent.harness.agent import AgentRunner
from dbt_agent.harness.events import EventLog
from dbt_agent.harness.prompts import AgentTask
from dbt_agent.llm.fake import FakeLLM, last_tool_output, raw_args, say, tool, tools
from dbt_agent.sandbox.base import ToolResponse
from tests.helpers import Crash, MemorySandbox, tool_messages_well_formed

TASK = AgentTask(task_id="t1", title="Fix the model", body="stg_orders fails", kind="ticket")
FILES = {"models/stg_orders.sql": "select id, amout as amount from raw.orders\n"}


def config(**agent) -> RunConfig:
    cfg = RunConfig()
    cfg.agent.checklist_build = agent.pop("checklist_build", True)
    for key, value in agent.items():
        setattr(cfg.agent, key, value)
    return cfg


def runner(tmp_path, script, sandbox=None, cfg=None) -> AgentRunner:
    return AgentRunner(
        run_dir=tmp_path / "run",
        task=TASK,
        config=cfg or config(),
        llm=FakeLLM(script),
        sandbox=sandbox or MemorySandbox(FILES),
    )


FIX = tool("edit_file", path="models/stg_orders.sql", old_string="amout", new_string="amt")
FINISH = tool("finish", summary="Column was renamed upstream; select amt. Build passes.")


def test_happy_path_completes(tmp_path):
    sandbox = MemorySandbox(FILES)
    run = runner(
        tmp_path,
        [
            tool("read_file", path="models/stg_orders.sql"),
            FIX,
            tool("run_dbt", command="build", select="stg_orders+"),
            FINISH,
        ],
        sandbox,
    )
    outcome = run.run()
    assert outcome.status == "completed", outcome
    assert outcome.turns == 4 and outcome.tool_calls == 4
    assert "amt" in sandbox.files["models/stg_orders.sql"]
    assert outcome.checklist["passed"]
    # the checklist ran the build itself
    assert (
        "run_dbt",
        {"command": "build", "select": "state:modified+", "full_refresh": True},
    ) in sandbox.calls
    assert (tmp_path / "run" / "progress.md").exists()
    assert tool_messages_well_formed(run.state.messages)


def test_finish_without_changes_is_rejected_then_accepted(tmp_path):
    sandbox = MemorySandbox(FILES)
    llm_script = [tool("finish", summary="Nothing needed, all good here really."), FIX, FINISH]
    outcome = runner(tmp_path, llm_script, sandbox).run()
    assert outcome.status == "completed"
    events = EventLog(tmp_path / "run" / "events.jsonl").of_type("checklist")
    assert [e["passed"] for e in events] == [False, True]
    assert "not changed any file" in events[0]["messages"][0]


def test_checklist_rejects_failing_build(tmp_path):
    sandbox = MemorySandbox(FILES)
    sandbox.handlers["run_dbt"] = lambda args: ToolResponse(
        ok=True,
        output="dbt build: FAILED (exit 1)\n[ERROR] model.p.stg_orders",
        data={"exit_code": 1, "counts": {"error": 1}, "failures": []},
    )
    outcome = runner(
        tmp_path, [FIX, FINISH, FINISH, FINISH], sandbox, config(max_checklist_rejections=3)
    ).run()
    assert outcome.status == "checklist_failed"
    assert "state:modified+ --full-refresh fails" in outcome.checklist["messages"][0]


def test_test_weakening_needs_a_reason(tmp_path):
    sandbox = MemorySandbox(FILES)
    base = {
        "nodes": {
            "test.p.not_null_x": {
                "resource_type": "test",
                "test_name": "not_null",
                "attached_node": "model.p.x",
                "name": "nn",
            }
        }
    }
    sandbox.handlers["manifest_summary"] = lambda args: ToolResponse(
        ok=True, data=base if "baseline" in args.get("path", "") else {"nodes": {}}
    )
    reason = "The ticket asks to drop the obsolete not_null test on x."
    outcome = runner(
        tmp_path,
        [
            FIX,
            FINISH,
            tool(
                "finish",
                summary="Dropped the test as requested by the ticket.",
                tests_changed_reason=reason,
            ),
        ],
        sandbox,
    ).run()
    assert outcome.status == "completed"
    assert outcome.tests_changed_reason == reason
    first = EventLog(tmp_path / "run" / "events.jsonl").of_type("checklist")[0]
    assert first["test_flags"][0]["kind"] == "test_removed_or_weakened"


def test_budget_exhaustion(tmp_path):
    cfg = config()
    cfg.agent.budgets.max_turns = 3
    script = [tool("list_files", path=f"models{i}") for i in range(10)]
    outcome = runner(tmp_path, script, cfg=cfg).run()
    assert outcome.status == "budget_exhausted" and "max_turns" in outcome.reason
    assert outcome.turns == 3


def test_budget_warning_is_injected(tmp_path):
    cfg = config()
    cfg.agent.budgets.max_turns = 5
    fake = FakeLLM([tool("list_files", path=f"d{i}") for i in range(10)])
    run = AgentRunner(
        run_dir=tmp_path / "run", task=TASK, config=cfg, llm=fake, sandbox=MemorySandbox(FILES)
    )
    run.run()
    warnings = [m for m in run.state.messages if "BUDGET WARNING" in str(m.get("content"))]
    assert len(warnings) == 1


def test_loop_detection_stops_a_stuck_model(tmp_path):
    cfg = config()
    cfg.agent.loop.repeat_threshold = 2
    cfg.agent.loop.stop_after_warnings = 1
    script = [tool("read_file", path="models/stg_orders.sql")] * 20
    run = runner(tmp_path, script, cfg=cfg)
    outcome = run.run()
    assert outcome.status == "loop_detected"
    warned = [m for m in run.state.messages if "LOOP WARNING" in str(m.get("content"))]
    assert warned, "the model must be warned before the run is stopped"


def test_malformed_arguments_and_unknown_tools_get_actionable_errors(tmp_path):
    run = runner(
        tmp_path,
        [
            raw_args("read_file", "{not json"),
            tool("delete_everything"),
            FIX,
            FINISH,
        ],
    )
    outcome = run.run()
    assert outcome.status == "completed"
    tool_texts = [m["content"] for m in run.state.messages if m["role"] == "tool"]
    assert "not valid JSON" in tool_texts[0] and "again" in tool_texts[0]
    assert "unknown tool 'delete_everything'" in tool_texts[1]
    assert outcome.tool_errors == 2


def test_text_only_replies_are_nudged_then_stopped(tmp_path):
    outcome = runner(
        tmp_path, [say("thinking..."), say("still thinking"), say("I think it is done")]
    ).run()
    assert outcome.status == "no_progress"


def test_reactive_script_sees_tool_output(tmp_path):
    def decide(messages):
        if "amout" in last_tool_output(messages):
            return FIX
        return say("?")

    outcome = runner(
        tmp_path, [tool("read_file", path="models/stg_orders.sql"), decide, FINISH]
    ).run()
    assert outcome.status == "completed"


def test_resume_after_crash_is_exactly_once(tmp_path):
    """Crash right after an edit was applied but before its result was logged."""
    sandbox = MemorySandbox(FILES)
    sandbox.crash_on = lambda name, args: name == "edit_file"
    append = tool(
        "edit_file",
        path="models/stg_orders.sql",
        old_string="from raw.orders",
        new_string="from raw.orders -- fixed",
    )
    first = runner(tmp_path, [tool("read_file", path="models/stg_orders.sql"), append], sandbox)
    with pytest.raises(Crash):
        first.run()
    # the crashed process applied the edit; the log does not know it
    assert "-- fixed" in sandbox.files["models/stg_orders.sql"]

    second = runner(tmp_path, [tool("run_dbt", command="build"), FINISH], sandbox)
    outcome = second.run()
    assert outcome.status == "completed", outcome
    assert sandbox.files["models/stg_orders.sql"].count("-- fixed") == 1  # exactly once
    assert outcome.sessions == 2
    assert outcome.turns == 4  # 2 turns before the crash + 2 after
    events = EventLog(tmp_path / "run" / "events.jsonl").read()
    types = [e["type"] for e in events]
    assert "workspace_restored" in types and "tool_call_replayed" in types
    assert tool_messages_well_formed(second.state.messages)
    resumed = [m for m in second.state.messages if "has resumed" in str(m.get("content"))]
    assert len(resumed) == 1


def test_finished_run_is_idempotent(tmp_path):
    sandbox = MemorySandbox(FILES)
    first = runner(tmp_path, [FIX, FINISH], sandbox).run()
    again = runner(tmp_path, [], sandbox).run()  # empty script: must not call the model
    assert again.status == first.status == "completed"


def test_context_compaction_keeps_conversation_valid(tmp_path):
    cfg = config()
    cfg.llm.context_window = 4000
    cfg.llm.max_tokens = 500
    sandbox = MemorySandbox({"models/big.sql": "-- big\n" * 400, **FILES})
    script = [tool("read_file", path="models/big.sql")]
    script += [tool("list_files", path=f"p{i}") for i in range(6)]
    script += [FIX, FINISH]
    fake = FakeLLM(script)
    run = AgentRunner(run_dir=tmp_path / "run", task=TASK, config=cfg, llm=fake, sandbox=sandbox)
    outcome = run.run()
    assert outcome.status == "completed"
    assert outcome.compactions >= 1
    last_request = fake.requests[-1]["messages"]
    assert len(str(last_request)) < len(str(run.state.messages))
    assert tool_messages_well_formed(last_request)


def test_multiple_tool_calls_in_one_turn(tmp_path):
    sandbox = MemorySandbox(FILES)
    outcome = runner(
        tmp_path,
        [
            tools(("read_file", {"path": "models/stg_orders.sql"}), ("list_files", {})),
            FIX,
            FINISH,
        ],
        sandbox,
    ).run()
    assert outcome.status == "completed" and outcome.tool_calls == 4
