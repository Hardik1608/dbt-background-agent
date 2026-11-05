"""Build a task's starting state ("template") from the base warehouse.

    templates/<task_id>/start/     what every agent run, verification and grading copies
    templates/<task_id>/meta.json  hidden from the agent: ticket text, start statuses,
                                   expected values, reference patch and fingerprints

For a ``ci_failure`` task the broken project is actually run (``dbt build`` in a sandbox)
and the tail of its output becomes the ticket, exactly what a failed CI job would show.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dbt_agent.sandbox.base import Sandbox, SandboxContext, SandboxError
from dbt_agent.tasks.model import TaskSpec
from dbt_agent.tasks.ops import apply_ops
from dbt_agent.util import atomic_write_json, force_rmtree, read_json, sha256_text, utc_now
from dbt_agent.workspace import BASELINE_STATE, init_git_baseline, write_baseline_manifest

SandboxFactory = Callable[[Path, SandboxContext], Sandbox]
TEMPLATE_VERSION = "1"


def task_context() -> SandboxContext:
    return SandboxContext(
        db_path="warehouse.duckdb",
        dbt_timeout_s=900,
        read_only_globs=["profiles.yml", "tests/_hidden/*"],
    )


@dataclass
class SuiteEnv:
    base_dir: Path
    data_dir: Path
    make_sandbox: SandboxFactory
    db_file: str = "warehouse.duckdb"

    def template_dir(self, task: TaskSpec) -> Path:
        return self.data_dir / "templates" / task.id

    def start_dir(self, task: TaskSpec) -> Path:
        return self.template_dir(task) / "start"

    def meta_path(self, task: TaskSpec) -> Path:
        return self.template_dir(task) / "meta.json"

    def meta(self, task: TaskSpec) -> dict[str, Any]:
        return read_json(self.meta_path(task), {}) or {}

    def base_key(self) -> str:
        return sha256_text((self.base_dir / "base_build.json").read_text(encoding="utf-8"))[:16]


def template_key(task: TaskSpec, env: SuiteEnv) -> str:
    relevant = {
        "mutation": [op.model_dump() for op in task.mutation],
        "trigger": task.trigger,
        "trigger_command": task.trigger_command,
        "ticket": task.ticket,
        "canary": task.canary,
        "base": env.base_key(),
        "version": TEMPLATE_VERSION,
        "base_assertions": [
            a.model_dump(by_alias=True) for a in task.hidden.assertions if a.expect.from_ == "base"
        ],
    }
    return sha256_text(json.dumps(relevant, sort_keys=True, default=str))[:16]


def format_ci_log(command: list[str], output: str, limit: int = 4500) -> str:
    lines = output.replace("\r\n", "\n").splitlines()
    start = 0
    for i, line in enumerate(lines):
        if "Finished running" in line:
            start = i
    kept = lines[start:] if start else lines[-80:]
    text = "\n".join(kept).strip()
    if len(text) > limit:
        text = text[:limit] + "\n... (log truncated)"
    return f"$ dbt {' '.join(command)}\n{text}"


def _base_values(task: TaskSpec, env: SuiteEnv) -> dict[str, Any]:
    queries = [a for a in task.hidden.assertions if a.expect.from_ == "base"]
    if not queries:
        return {}
    import duckdb

    con = duckdb.connect(str(env.base_dir / env.db_file), read_only=True)
    try:
        return {a.name: [list(r) for r in con.execute(a.sql).fetchall()] for a in queries}
    finally:
        con.close()


def compact_duckdb(path: Path) -> None:
    """Rewrite a DuckDB file into a fresh one (dbt rebuilds leave free blocks; ~2x smaller).

    Only for trusted files (templates before any agent run). The file keeps its name, so
    dbt's catalog-qualified view definitions still resolve.
    """
    import duckdb

    staging = path.parent / ".compact"
    force_rmtree(staging)
    staging.mkdir()
    fresh = staging / path.name
    con = duckdb.connect()
    try:
        con.execute(f"attach '{path.as_posix()}' as src (read_only)")
        con.execute(f"attach '{fresh.as_posix()}' as dst")
        con.execute("copy from database src to dst")
    finally:
        con.close()
    fresh.replace(path)
    force_rmtree(staging)


def slim_template(start: Path, db_file: str) -> None:
    """Compact the database and drop build output nobody copies (target/, logs/)."""
    for leftover in ("target", "logs"):
        force_rmtree(start / leftover)
    if (start / db_file).exists():
        compact_duckdb(start / db_file)


def build_template(task: TaskSpec, env: SuiteEnv, *, force: bool = False) -> dict[str, Any]:
    key = template_key(task, env)
    meta = env.meta(task)
    if not force and meta.get("template_key") == key and env.start_dir(task).exists():
        return meta
    template = env.template_dir(task)
    start = env.start_dir(task)
    force_rmtree(template)
    template.mkdir(parents=True)
    shutil.copytree(
        env.base_dir,
        start,
        symlinks=True,
        ignore=shutil.ignore_patterns("target", "logs", "base_build.json"),
    )
    apply_ops(start, task.mutation, env.db_file)

    box = env.make_sandbox(start, task_context())
    try:
        trigger = box.call("dbt_raw", {"argv": task.trigger_command, "timeout_s": 900}, timeout=900)
        output = trigger.output if trigger.ok else (trigger.error or "")
        exit_code = trigger.data.get("exit_code")
        results = trigger.data.get("results") or {}
        statuses = dict.fromkeys(results.get("passed", []), "pass")
        for item in results.get("failures", []):
            statuses[item["unique_id"]] = item["status"]
        for uid in results.get("skipped", []):
            statuses[uid] = "skipped"
        if task.trigger == "ci_failure" and exit_code == 0:
            raise RuntimeError(f"{task.id}: the CI trigger passed, so there is no failure")
        init_git_baseline(start, f"main: state before {task.id}")
        try:
            write_baseline_manifest(box)
            baseline = "start"
        except SandboxError:
            # the broken project does not even parse: compare against production instead,
            # i.e. the healthy base project (what Slim CI uses as --state)
            base_state = ensure_base_state(env)
            target = start / BASELINE_STATE
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(base_state / "manifest.json", target / "manifest.json")
            baseline = "base"
    finally:
        box.stop()

    if task.trigger == "ci_failure":
        log = format_ci_log(task.trigger_command, output)
        body = (task.ticket.strip() + "\n\n" if task.ticket.strip() else "") + log
    else:
        body = task.ticket.strip()
    slim_template(start, env.db_file)
    meta = {
        "task_id": task.id,
        "template_key": key,
        "built_at": utc_now(),
        "ticket": body,
        "trigger": {"command": task.trigger_command, "exit_code": exit_code},
        "start_statuses": statuses,
        "baseline_manifest": baseline,
        "base_values": _base_values(task, env),
        "reference": None,
    }
    atomic_write_json(env.meta_path(task), meta)
    return meta


def ensure_base_state(env: SuiteEnv) -> Path:
    """Manifest of the healthy base project (parsed once, in a sandbox)."""
    state = env.data_dir / "base_state"
    if (state / "manifest.json").exists():
        return state
    scratch = env.data_dir / "scratch" / "base_parse"
    force_rmtree(scratch)
    shutil.copytree(
        env.base_dir,
        scratch,
        symlinks=True,
        ignore=shutil.ignore_patterns("*.duckdb", "target", "logs", "base_build.json"),
    )
    box = env.make_sandbox(scratch, task_context())
    try:
        parsed = box.call(
            "dbt_raw", {"argv": ["parse", "--target-path", "base_state_out"]}, timeout=600
        )
        if parsed.data.get("exit_code") != 0:
            raise RuntimeError("base project does not parse: " + parsed.output[-1500:])
    finally:
        box.stop()
    state.mkdir(parents=True, exist_ok=True)
    shutil.copy2(scratch / "base_state_out" / "manifest.json", state / "manifest.json")
    force_rmtree(scratch)
    return state


def reference_patch(task: TaskSpec, env: SuiteEnv, scratch: Path) -> str:
    """Apply the reference fix to a trusted copy of the start state; return the git diff."""
    force_rmtree(scratch)
    shutil.copytree(
        env.start_dir(task),
        scratch,
        symlinks=True,
        ignore=shutil.ignore_patterns("*.duckdb", "*.duckdb.wal", "dbt_packages", "target", "logs"),
    )
    apply_ops(scratch, task.reference_fix, env.db_file)
    git = ["git", "-c", "core.autocrlf=false", "-c", "safe.directory=*"]
    subprocess.run([*git, "add", "-A"], cwd=scratch, check=True, capture_output=True)
    proc = subprocess.run(
        [*git, "diff", "--cached", "--binary", "HEAD"], cwd=scratch, check=True, capture_output=True
    )
    force_rmtree(scratch)
    return proc.stdout.decode("utf-8")
