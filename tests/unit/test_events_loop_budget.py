"""Event log durability, loop detection and budgets."""

from __future__ import annotations

from dbt_agent.config import BudgetConfig, LLMConfig, LoopConfig
from dbt_agent.harness.budgets import Budget, Usage
from dbt_agent.harness.events import EventLog
from dbt_agent.harness.loopdetect import LoopDetector


def test_event_log_appends_and_reads(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    log.append("run_started", task_id="t1")
    log.append("tool_call", name="read_file")
    events = EventLog(tmp_path / "events.jsonl").read()
    assert [e["seq"] for e in events] == [1, 2]
    assert events[1]["name"] == "read_file"


def test_event_log_repairs_a_torn_tail(tmp_path):
    path = tmp_path / "events.jsonl"
    log = EventLog(path)
    log.append("a")
    log.append("b")
    with path.open("ab") as handle:
        handle.write(b'{"seq": 3, "type": "tool_res')  # crash mid-write
    reopened = EventLog(path)
    assert reopened.repaired
    assert [e["type"] for e in reopened.read()] == ["a", "b"]
    event = reopened.append("c")
    assert event["seq"] == 3
    assert [e["type"] for e in EventLog(path).read()] == ["a", "b", "c"]


def test_event_log_repairs_invalid_last_line(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"seq": 1, "ts": "x", "type": "a"}\nnot json\n', encoding="utf-8")
    log = EventLog(path)
    assert log.repaired and len(log.read()) == 1


def detector(**kw) -> LoopDetector:
    return LoopDetector(LoopConfig(**kw))


def test_repeat_warns_then_stops():
    d = detector(repeat_threshold=3, stop_after_warnings=1)
    signals = [
        d.observe("read_file", {"path": "a"}, ok=True, output="same", error=None).signal
        for _ in range(3)
    ]
    assert signals == ["ok", "ok", "warn"]
    signals = [
        d.observe("read_file", {"path": "a"}, ok=True, output="same", error=None).signal
        for _ in range(3)
    ]
    assert signals[-1] == "stop"


def test_different_results_are_not_a_loop():
    d = detector()
    for i in range(10):
        verdict = d.observe("run_dbt", {"command": "build"}, ok=True, output=f"run {i}", error=None)
        assert verdict.signal == "ok"


def test_error_streak_warns():
    d = detector(error_repeat_threshold=3)
    verdicts = [
        d.observe(
            "edit_file",
            {"path": "a", "old_string": str(i)},
            ok=False,
            output="",
            error="old_string was not found",
        )
        for i in range(3)
    ]
    assert verdicts[-1].signal == "warn" and verdicts[-1].kind == "error_streak"


def test_oscillation_warns():
    d = detector(oscillation_threshold=2)
    d.seed_tree("t0")
    kinds = []
    for tree in ["t1", "t0", "t1", "t0"]:
        kinds.append(
            d.observe("edit_file", {"tree": tree}, ok=True, output=tree, error=None, tree=tree).kind
        )
    assert "oscillation" in kinds


def test_budgets():
    budget = Budget(
        BudgetConfig(
            max_turns=5, max_tokens=1000, max_wall_seconds=60, max_tool_calls=10, max_cost_eur=0.01
        ),
        LLMConfig(price_input_per_mtok=1000.0, price_output_per_mtok=1000.0),
    )
    assert budget.exceeded(Usage()) is None
    assert budget.exceeded(Usage(turns=5)).startswith("max_turns")
    assert budget.exceeded(Usage(prompt_tokens=900, completion_tokens=100)).startswith("max_tok")
    assert budget.exceeded(Usage(tool_calls=10)).startswith("max_tool_calls")
    assert budget.exceeded(Usage(wall_seconds_previous_sessions=50, session_seconds=11))
    assert budget.exceeded(Usage(prompt_tokens=10)).startswith("max_cost_eur")
    assert budget.nearly_exhausted(Usage(turns=2))
