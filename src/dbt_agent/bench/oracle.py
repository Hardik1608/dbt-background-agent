"""The oracle agent: a scripted fake LLM that applies a task's reference fix through the real
tools (write_file / delete_file), proves it with run_dbt and calls finish.

Running the whole pipeline with it (harness -> checklist -> verifier -> reviewer -> grader)
checks that the machinery itself can resolve every task - the same idea as ADE-bench's
"sage" agent. Expected resolve rate: 100%.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from dbt_agent.llm.fake import FakeLLM, Step, tool
from dbt_agent.tasks.model import TaskSpec
from dbt_agent.tasks.ops import apply_ops
from dbt_agent.tasks.templates import SuiteEnv

SKIP = {".git", ".agent", "target", "logs", "dbt_packages"}


def _files(root: Path) -> dict[str, str]:
    out = {}
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] in SKIP:
            continue
        if path.is_file() and not path.name.endswith((".duckdb", ".wal")):
            out[rel.as_posix()] = path.read_text(encoding="utf-8")
    return out


def oracle_steps(task: TaskSpec, env: SuiteEnv) -> list[Step]:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "w"
        shutil.copytree(
            env.start_dir(task),
            work,
            symlinks=True,
            ignore=shutil.ignore_patterns("*.duckdb", "dbt_packages", ".git", "target", "logs"),
        )
        before = _files(work)
        apply_ops(work, task.reference_fix)
        after = _files(work)
    steps: list[Step] = []
    for path in sorted(after):
        if before.get(path) != after[path]:
            steps.append(tool("write_file", path=path, content=after[path]))
    for path in sorted(set(before) - set(after)):
        steps.append(tool("delete_file", path=path))
    steps.append(tool("run_dbt", command="build", select="state:modified+", full_refresh=True))
    reason = "The task explicitly requires this test change." if task.allowed_removed else None
    finish_args = {
        "summary": f"Applied the reference fix for {task.id}: {task.title}. "
        "The modified models and their children build and pass tests."
    }
    if reason:
        finish_args["tests_changed_reason"] = reason
    steps.append(tool("finish", **finish_args))
    return steps


def oracle_llm(task: TaskSpec, env: SuiteEnv) -> FakeLLM:
    from dbt_agent.tasks.templates import build_template

    build_template(task, env)  # cached; the script is computed from the starting state
    return FakeLLM(oracle_steps(task, env), model_name="oracle")
