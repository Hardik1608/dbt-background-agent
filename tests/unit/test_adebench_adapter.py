"""ADE-bench driver: finds ADE's run directory and checkpoints results without re-running."""

from __future__ import annotations

import json
from pathlib import Path

from dbt_agent.bench import adebench


def _ade_output(out_dir: Path, run_id: str, resolved: bool) -> Path:
    run_dir = out_dir / f"{run_id}__none"  # ADE appends the agent name
    agent = run_dir / "airbnb001" / "airbnb001.base.1-of-1" / "agent-logs" / "dbt_agent"
    agent.mkdir(parents=True)
    (run_dir / "results.json").write_text(
        json.dumps(
            {
                "results": [
                    {
                        "is_resolved": resolved,
                        "failure_mode": "unset",
                        "input_tokens": 1000,
                        "output_tokens": 50,
                        "parser_results": {"dbt_compile": "passed"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (agent / "outcome.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "turns": 12,
                "prompt_tokens": 1000,
                "completion_tokens": 50,
                "wall_seconds": 61.5,
                "security": {"escape_attempts": 0, "egress_attempts": 1},
            }
        ),
        encoding="utf-8",
    )
    (agent / "isolation.json").write_text(
        json.dumps(
            {
                "during_agent": {"networks": [], "memory_bytes": 1610612736},
                "restored_for_grading": ["x_default"],
            }
        ),
        encoding="utf-8",
    )
    return run_dir


def test_ade_run_dir_handles_the_agent_suffix(tmp_path):
    assert adebench.ade_run_dir(tmp_path, "r__airbnb001") is None
    run_dir = _ade_output(tmp_path, "r__airbnb001", True)
    assert adebench.ade_run_dir(tmp_path, "r__airbnb001") == run_dir


def test_graded_trials_are_parsed_and_checkpointed_without_running_ade(tmp_path):
    data_dir = tmp_path / "adebench"
    out_dir = data_dir / "runs" / "r"
    out_dir.mkdir(parents=True)
    _ade_output(out_dir.resolve(), "r__airbnb001", True)
    results = adebench.run_tasks(
        data_dir, "r", ["airbnb001"], config_paths=[], heavy_lock=False, required_mb=None
    )
    assert results[0]["resolved"] is True and results[0]["status"] == "completed"
    assert results[0]["returncode"] is None  # ADE was not started again
    assert (out_dir / "airbnb001.result.json").exists()
    summary = adebench.summarise(results)
    assert summary["resolved"] == 1 and summary["network_isolated_trials"] == 1
    assert summary["egress_attempts"] == 1
