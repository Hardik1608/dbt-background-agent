"""ADE-bench adapter (github.com/dbt-labs/ade-bench, Apache-2.0).

ADE-bench runs each trial in its own Docker container (tmux session, dbt-core 1.10 +
DuckDB) and grades it with hidden dbt tests. Its agents implement
``BaseAgent.perform_task(task_prompt, session, ...)``. We plug our harness in without
forking ADE: ``scripts/run_adebench.py`` registers ``make_agent_class()`` under ADE's
``none`` agent slot and then runs ADE's own CLI, so setup, timeouts and grading are ADE's.

Inside ``perform_task`` the agent loop runs on the host, as always; tools run in the trial
container through ``docker exec`` (``AttachedContainerSandbox``). Because ADE creates the
container, we tighten it at the start of the agent phase: network disconnected (setup,
including ``dbt deps``, has already happened), memory capped, toolbox copied in.

The driver (``bench adebench run``) calls ADE once per task under the box's RAM admission
and heavy-job lock, and checkpoints each task's ``results.json``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from dbt_agent.util import atomic_write_json, read_json, utc_now

log = logging.getLogger(__name__)
REPO_URL = "https://github.com/dbt-labs/ade-bench.git"
DB_RELEASE = "https://github.com/dbt-labs/ade-bench/releases/download/databases"

NOTES = """\
This task comes from ADE-bench. The dbt project (DuckDB) is in the current directory and
its database file is `{db}`. Do exactly what the task asks, nothing more. Inspect the data
with query_duckdb when needed, run dbt to check that the affected models build, and call
finish. Hidden dbt tests grade the result after you finish.\
"""


def ade_agent_timeout_s() -> float:
    """ADE's agent-execution timeout (``DEFAULT_AGENT_TIMEOUT_SEC``, from its .env)."""
    try:
        from ade_bench.config import config

        return float(config.default_agent_timeout_sec)
    except Exception:  # outside ADE's venv
        return float(os.environ.get("DEFAULT_AGENT_TIMEOUT_SEC", 900))


def make_agent_class(config_paths: list[str]):
    """Our harness as an ADE-bench agent class (imports ADE lazily)."""
    from ade_bench.agents.agent_name import AgentName
    from ade_bench.agents.base_agent import AgentResult, BaseAgent
    from ade_bench.harness_models import FailureMode

    class DbtBackgroundAgentForAde(BaseAgent):
        NAME = AgentName.NONE

        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)

        def perform_task(
            self,
            task_prompt: str,
            session,
            logging_dir: Path | None = None,
            task_name: str | None = None,
        ) -> AgentResult:
            from dbt_agent.config import load_config
            from dbt_agent.harness.agent import AgentRunner
            from dbt_agent.harness.prompts import AgentTask
            from dbt_agent.llm.client import OpenAICompatClient
            from dbt_agent.sandbox.base import SandboxContext
            from dbt_agent.sandbox.docker import AttachedContainerSandbox
            from dbt_agent.workspace import write_baseline_manifest

            started = time.monotonic()
            cfg = load_config([Path(p) for p in config_paths])
            # stop on our own well before ADE's agent timeout: ADE cannot kill this thread,
            # and an agent still editing files while ADE grades would corrupt the result
            budget = cfg.agent.budgets
            budget.max_wall_seconds = min(
                budget.max_wall_seconds, max(120, ade_agent_timeout_s() - 90)
            )
            run_dir = Path(logging_dir or ".") / "dbt_agent"
            run_dir.mkdir(parents=True, exist_ok=True)
            box = AttachedContainerSandbox(
                session.container.name,
                root="/app",
                context=SandboxContext(dbt_timeout_s=cfg.sandbox.dbt_timeout_s),
                memory=cfg.sandbox.memory,
            )
            isolation: dict[str, Any] = {"agent_wall_budget_s": budget.max_wall_seconds}
            try:
                box.start()
                listing = box.call("list_files", {"pattern": "*.duckdb", "max_depth": 1})
                dbs = [
                    ln.split("  (")[0]
                    for ln in listing.output.splitlines()[1:]
                    if ln.strip().endswith("B)")
                ]
                box.context.db_path = dbs[0] if dbs else None
                box.require("git_init")
                try:
                    write_baseline_manifest(box)
                except Exception:  # some tasks start from a project that does not parse
                    log.info("no baseline manifest for %s", task_name)
                isolation.update(
                    {
                        "during_agent": box.network_mode(),
                        "disconnected": list(box.disconnected),
                        "db_path": box.context.db_path,
                    }
                )
                atomic_write_json(run_dir / "isolation.json", isolation)
                task = AgentTask(
                    task_id=task_name or "ade-task",
                    title=f"ADE-bench {task_name}",
                    body=task_prompt,
                    kind="benchmark",
                    project_notes=NOTES.format(db=box.context.db_path),
                )
                outcome = AgentRunner(
                    run_dir=run_dir,
                    task=task,
                    config=cfg,
                    llm=OpenAICompatClient(cfg.llm),
                    sandbox=box,
                ).run()
                failure = FailureMode.NONE
            except Exception as exc:  # report, do not crash ADE's harness
                log.exception("agent failed on %s", task_name)
                atomic_write_json(run_dir / "error.json", {"error": f"{type(exc).__name__}: {exc}"})
                return AgentResult(
                    input_tokens=0,
                    output_tokens=0,
                    runtime_ms=int(1000 * (time.monotonic() - started)),
                    failure_mode=FailureMode.UNKNOWN_AGENT_ERROR,
                )
            finally:  # the agent is done: ADE grades in the environment it built
                try:
                    isolation["restored_for_grading"] = box.restore_network()
                except Exception as exc:  # never break ADE's flow here
                    isolation["restored_for_grading"] = f"failed: {exc}"
                atomic_write_json(run_dir / "isolation.json", isolation)
            return AgentResult(
                input_tokens=outcome.prompt_tokens,
                output_tokens=outcome.completion_tokens,
                num_turns=outcome.turns,
                runtime_ms=int(1000 * outcome.wall_seconds),
                cost_usd=0.0,
                model_name=cfg.llm.model,
                failure_mode=failure,
            )

    return DbtBackgroundAgentForAde


# --------------------------------------------------------------------------- setup / driver
COMPOSE_FILE = "shared/defaults/docker-compose-duckdb-dbt.yaml"
COMPOSE_ANCHOR = "    container_name: ${T_BENCH_TASK_DOCKER_CLIENT_CONTAINER_NAME}"


def cap_compose_resources(repo: Path, memory: str = "1536m", cpus: str = "2") -> bool:
    """Resource caps for ADE's trial container (box rule: every container is capped).

    A limit only; nothing about the task changes. Returns True when the file was changed.
    """
    path = repo / COMPOSE_FILE
    text = path.read_text(encoding="utf-8")
    if "mem_limit:" in text:
        return False
    if COMPOSE_ANCHOR not in text:
        raise RuntimeError(f"unexpected layout of {COMPOSE_FILE}; not patched")
    caps = "\n".join(
        [
            COMPOSE_ANCHOR,
            f"    mem_limit: {memory}",
            f"    memswap_limit: {memory}",
            f"    cpus: {cpus}",
        ]
    )
    path.write_text(text.replace(COMPOSE_ANCHOR, caps, 1), encoding="utf-8")
    return True


def write_env(repo: Path) -> dict[str, str]:
    """ADE's own .env.example settings (timeouts included), with non-interactive logging."""
    lines, settings = [], {}
    for line in (repo / ".env.example").read_text(encoding="utf-8").splitlines():
        key = line.split("=", 1)[0].strip()
        if key == "USE_DYNAMIC_LOGGING":
            line = "USE_DYNAMIC_LOGGING=FALSE"
        if "=" in line and not line.lstrip().startswith("#"):
            settings[key] = line.split("=", 1)[1]
        lines.append(line)
    (repo / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {k: v for k, v in settings.items() if k.endswith("_SEC") or k == "USE_DYNAMIC_LOGGING"}


def needed_databases(repo: Path) -> list[str]:
    import yaml

    names = set()
    for task_yaml in sorted((repo / "tasks").glob("*/task.yaml")):
        spec = yaml.safe_load(task_yaml.read_text(encoding="utf-8")) or {}
        if spec.get("status") != "ready":
            continue
        for variant in spec.get("variants") or []:
            if variant.get("db_type") == "duckdb" and variant.get("project_type") == "dbt":
                names.add(str(variant.get("db_name")))
    return sorted(names)


def setup(data_dir: Path, memory: str = "1536m") -> dict[str, Any]:
    """Clone ADE-bench, install its locked dependencies plus our package, fetch the DBs."""
    repo = (data_dir / "ade-bench").resolve()  # absolute: subprocesses run with cwd=repo
    if not repo.exists():
        subprocess.run(["git", "clone", "--depth", "1", REPO_URL, str(repo)], check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    uv = shutil.which("uv") or str(Path("~/.local/bin/uv").expanduser())
    if not (repo / ".venv" / "bin" / "python").exists():  # ADE's own lockfile
        subprocess.run([uv, "sync", "--frozen", "--no-dev"], cwd=repo, check=True)
    python = str(repo / ".venv" / "bin" / "python")
    ours = Path(__file__).resolve().parents[3]
    subprocess.run(
        [uv, "pip", "install", "-q", "--python", python, "--no-deps", "-e", str(ours)], check=True
    )
    missing = (
        subprocess.run(
            [python, "-c", "import httpx, pydantic, yaml"], capture_output=True, check=False
        ).returncode
        != 0
    )
    if missing:
        subprocess.run(
            [uv, "pip", "install", "-q", "--python", python, "-r", str(ours / "requirements.txt")],
            check=True,
        )
    db_dir = repo / "shared" / "databases" / "duckdb"
    db_dir.mkdir(parents=True, exist_ok=True)
    fetched = {}
    for name in needed_databases(repo):
        target = db_dir / f"{name}.duckdb"
        if not target.exists():
            partial = target.with_suffix(".part")
            subprocess.run(
                [
                    "curl",
                    "-sSfL",
                    "--retry",
                    "3",
                    "-o",
                    str(partial),
                    f"{DB_RELEASE}/{name}.duckdb",
                ],
                check=True,
            )
            partial.replace(target)
        fetched[name] = target.stat().st_size
    env = write_env(repo)
    capped = cap_compose_resources(repo, memory=memory)
    tasks = list_tasks(repo)
    summary = {
        "repo_commit": commit,
        "tasks_ready": len(tasks),
        "databases": fetched,
        "env": env,
        "compose_resource_caps": {"memory": memory, "cpus": "2", "changed_now": capped},
        "set_up_at": utc_now(),
    }
    atomic_write_json(data_dir / "setup.json", summary)
    return summary


def list_tasks(repo: Path) -> list[str]:
    import yaml

    ready = []
    for task_yaml in sorted((repo / "tasks").glob("*/task.yaml")):
        spec = yaml.safe_load(task_yaml.read_text(encoding="utf-8")) or {}
        variants = spec.get("variants") or []
        duckdb_dbt = any(
            v.get("db_type") == "duckdb" and v.get("project_type") == "dbt" for v in variants
        )
        if spec.get("status") == "ready" and duckdb_dbt:
            ready.append(spec.get("task_id") or task_yaml.parent.name)
    return ready


def ade_run_dir(out_dir: Path, run_id: str) -> Path | None:
    """ADE appends the agent name to the run id (``<run id>__none``)."""
    matches = sorted(p for p in out_dir.glob(f"{run_id}__*") if p.is_dir())
    if (out_dir / run_id).is_dir():
        matches.insert(0, out_dir / run_id)
    return matches[0] if matches else None


def run_tasks(
    data_dir: Path,
    run_name: str,
    tasks: list[str],
    *,
    config_paths: list[str],
    heavy_lock: bool,
    required_mb: int | None,
) -> list[dict[str, Any]]:
    from dbt_agent.bench.boxguard import admission

    repo = (data_dir / "ade-bench").resolve()  # absolute: the subprocess runs with cwd=repo
    out_dir = (data_dir / "runs" / run_name).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    launcher = Path(__file__).resolve().parents[3] / "scripts" / "run_adebench.py"
    results = []
    for task in tasks:
        marker = out_dir / f"{task}.result.json"
        cached = read_json(marker)
        if cached:
            results.append(cached)
            continue
        run_id = f"{run_name}__{task}"
        run_dir = ade_run_dir(out_dir, run_id)
        started = time.monotonic()
        returncode, stderr_tail = None, ""
        if run_dir is None or not (run_dir / "results.json").exists():
            if run_dir is not None:  # a trial cut short (power cut): keep it, start afresh
                aside = out_dir / "_interrupted"
                aside.mkdir(exist_ok=True)
                run_dir.rename(aside / f"{run_dir.name}-{int(time.time())}")
            with admission(
                "dbt-background-agent", required_mb=required_mb, lock=heavy_lock, log=log.info
            ):
                env = {**os.environ, "DBT_AGENT_CONFIGS": os.pathsep.join(config_paths)}
                proc = subprocess.run(
                    [
                        str(repo / ".venv" / "bin" / "python"),
                        str(launcher),
                        "run",
                        task,
                        "--db",
                        "duckdb",
                        "--project-type",
                        "dbt",
                        "--agent",
                        "none",
                        "--n-concurrent-trials",
                        "1",
                        "--no-diffs",
                        "--run-id",
                        run_id,
                        "--output-path",
                        str(out_dir),
                    ],
                    cwd=repo,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=4 * 3600,
                )
            returncode, stderr_tail = proc.returncode, proc.stderr[-800:]
            run_dir = ade_run_dir(out_dir, run_id)
        payload = (read_json(run_dir / "results.json") if run_dir else None) or {}
        trials = payload.get("results") or []
        trial = trials[0] if trials else {}
        result = {
            "task_id": task,
            "resolved": bool(trial.get("is_resolved")),
            "failure_mode": trial.get("failure_mode"),
            "parser_results": trial.get("parser_results"),
            "input_tokens": trial.get("total_input_tokens") or trial.get("input_tokens"),
            "output_tokens": trial.get("total_output_tokens") or trial.get("output_tokens"),
            "seconds": round(time.monotonic() - started, 1),
            "returncode": returncode,
            "stderr_tail": stderr_tail,
            "ade_run_dir": str(run_dir) if run_dir else None,
        }
        agent_dir = next(run_dir.glob("**/dbt_agent"), None) if run_dir else None
        if agent_dir is not None:
            outcome = read_json(agent_dir / "outcome.json") or {}
            result.update(
                {
                    "status": outcome.get("status"),
                    "turns": outcome.get("turns"),
                    "prompt_tokens": outcome.get("prompt_tokens"),
                    "completion_tokens": outcome.get("completion_tokens"),
                    "agent_seconds": outcome.get("wall_seconds"),
                    "security": outcome.get("security", {}),
                    "isolation": read_json(agent_dir / "isolation.json"),
                }
            )
        if payload:  # only checkpoint tasks ADE actually graded
            atomic_write_json(marker, result)
        results.append(result)
        print(
            f"{task}: {'RESOLVED' if result['resolved'] else 'unresolved'} "
            f"[{result.get('status')}] {result.get('failure_mode')}",
            flush=True,
        )
    return results


def summarise(results: list[dict[str, Any]]) -> dict[str, Any]:
    graded = [r for r in results if r.get("failure_mode") is not None or r.get("resolved")]
    resolved = [r for r in results if r.get("resolved")]
    return {
        "tasks": len(results),
        "graded": len(graded),
        "resolved": len(resolved),
        "resolve_rate": round(len(resolved) / len(results), 4) if results else None,
        "resolved_tasks": sorted(r["task_id"] for r in resolved),
        "status_counts": {
            s: sum(1 for r in results if r.get("status") == s)
            for s in sorted({str(r.get("status")) for r in results})
        },
        "escape_attempts": sum(
            (r.get("security") or {}).get("escape_attempts", 0) for r in results
        ),
        "egress_attempts": sum(
            (r.get("security") or {}).get("egress_attempts", 0) for r in results
        ),
        "network_isolated_trials": sum(
            1
            for r in results
            if ((r.get("isolation") or {}).get("during_agent") or {}).get("networks") == []
        ),
        "mean_tokens_per_resolved": (
            round(
                sum(
                    (r.get("prompt_tokens") or 0) + (r.get("completion_tokens") or 0)
                    for r in resolved
                )
                / len(resolved)
            )
            if resolved
            else None
        ),
    }


def _cmd_setup(args: argparse.Namespace) -> int:
    print(json.dumps(setup(Path(args.data_dir)), indent=2))
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    from dbt_agent.cli import _config, _parse_mb, _write_report

    cfg = _config(args)
    data_dir = Path(args.data_dir)
    tasks = args.tasks or list_tasks(data_dir / "ade-bench")
    required = _parse_mb(cfg.sandbox.memory) + 1024
    results = run_tasks(
        data_dir,
        args.run_name,
        tasks,
        config_paths=[str(Path(p).resolve()) for p in (args.config or [])],
        heavy_lock=args.heavy_lock,
        required_mb=required,
    )
    summary = summarise(results)
    md = [
        "# ADE-bench (DuckDB, dbt)",
        "",
        f"- Model: `{cfg.llm.model}` through dbt-background-agent; one trial per task; "
        "graded by ADE-bench's own dbt tests.",
        f"- Resolved: **{summary['resolved']}/{summary['tasks']}**"
        + (
            f" ({100 * summary['resolve_rate']:.1f}%)"
            if summary["resolve_rate"] is not None
            else ""
        ),
        f"- Agent loop status: {summary['status_counts']}; trials with the network removed "
        f"during the agent phase: {summary['network_isolated_trials']}/{summary['tasks']}; "
        f"escape / egress attempts: {summary['escape_attempts']} / {summary['egress_attempts']} "
        "(an egress attempt is a refused network-dependent command such as `dbt deps`).",
        "- ADE settings: its `.env.example` timeouts (agent 900 s; our agent stops itself "
        "90 s earlier); trial container capped at the sandbox memory and 2 CPUs.",
        "- Context (external): dbt Labs reported 56% for OpenAI Codex with GPT-5.1 and the same "
        "for Claude Sonnet 4.5 (getdbt.com blog, 2026-01-21), on an earlier task set.",
        "",
        "| Task | Resolved | Agent status | Failure mode |",
        "|---|---|---|---|",
    ]
    md += [
        f"| {r['task_id']} | {'yes' if r.get('resolved') else 'no'} | {r.get('status')} | "
        f"{r.get('failure_mode')} |"
        for r in results
    ]
    _write_report(
        Path(args.report or "reports/adebench"),
        {"summary": summary, "runs": results},
        "\n".join(md) + "\n",
    )
    return 0


def trial_image(task: str) -> str:
    return f"ade-bench__{task}__client".replace(".", "-")  # ADE's TrialHandler naming


def _cmd_cleanup(args: argparse.Namespace) -> int:
    """Remove the trial images ADE built for our tasks (one shared image, many tags)."""
    tasks = args.tasks or list_tasks(Path(args.data_dir) / "ade-bench")
    removed = []
    for task in tasks:
        proc = subprocess.run(
            ["docker", "image", "rm", trial_image(task)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            removed.append(trial_image(task))
    print(json.dumps({"removed": removed}, indent=2))
    return 0


def register(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    ade = sub.add_parser("adebench", help="ADE-bench (dbt Labs)")
    asub = ade.add_subparsers(dest="adebench", required=True)
    for name, func in (("setup", _cmd_setup), ("run", _cmd_run), ("cleanup", _cmd_cleanup)):
        sp = asub.add_parser(name, parents=[common])
        sp.add_argument("--data-dir", default="bench_data/adebench")
        sp.add_argument("--run-name", default="ade_local_agent")
        sp.add_argument("--tasks", nargs="*")
        sp.add_argument("--heavy-lock", action="store_true")
        sp.add_argument("--report")
        sp.set_defaults(func=func)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(0)
