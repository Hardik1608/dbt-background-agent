"""CLI commands for agent runs, benchmarks, reports and PRs (registered by cli.build_parser)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from dbt_agent.util import atomic_write_json, read_json, read_text_exact, slug, utc_now

REPO_ROOT = Path(__file__).resolve().parents[2]
log = logging.getLogger("dbt_agent")


def _llm(args: argparse.Namespace, cfg):
    from dbt_agent.llm.client import OpenAICompatClient

    return OpenAICompatClient(cfg.llm)


# --------------------------------------------------------------------------- run (one job)
def ticket_from_run_results(path: Path, limit: int = 12) -> str:
    """A failed CI run (dbt's target/run_results.json) as a ticket body."""
    data = json.loads(path.read_text(encoding="utf-8"))
    failures = [
        r for r in data.get("results", []) if r.get("status") in {"error", "fail", "runtime error"}
    ]
    skipped = sum(1 for r in data.get("results", []) if r.get("status") == "skipped")
    lines = [
        f"`{data.get('args', {}).get('which', 'dbt')}` finished with {len(failures)} "
        f"failure(s) and {skipped} skipped node(s):",
        "",
    ]
    for item in failures[:limit]:
        message = (item.get("message") or "").strip().replace("\n", " ")[:600]
        lines.append(f"- [{item.get('status', '').upper()}] {item.get('unique_id')}: {message}")
    return "\n".join(lines)


def cmd_run(args: argparse.Namespace) -> int:
    from dbt_agent.cli import _config, sandbox_factory
    from dbt_agent.harness.prompts import AgentTask
    from dbt_agent.pipeline import JobSpec, run_job
    from dbt_agent.sandbox.base import SandboxContext
    from dbt_agent.workspace import copy_template, init_git_baseline, write_baseline_manifest

    cfg = _config(args)
    run_id = args.run_id or f"{slug(args.title or 'job', 30)}-{utc_now()[:19].replace(':', '')}"
    runs_root = Path(args.runs_dir)
    template = runs_root / run_id / "template"
    context = SandboxContext(db_path=args.db, dbt_timeout_s=cfg.sandbox.dbt_timeout_s)
    make = sandbox_factory(cfg, labels={"dbt-agent.job": run_id})
    if not (template / ".agent" / "baseline_state" / "manifest.json").exists():
        copy_template(Path(args.project), template)
        init_git_baseline(template, "starting point")
        box = make(template, context)
        try:
            write_baseline_manifest(box)
        finally:
            box.stop()
    if args.ci_run_results:
        body, kind = ticket_from_run_results(Path(args.ci_run_results)), "ci_failure"
    else:
        body, kind = Path(args.ticket).read_text(encoding="utf-8"), "ticket"
    task = AgentTask(
        task_id=run_id,
        title=args.title or "Background job",
        body=body,
        kind=kind,
        project_notes=args.notes or "",
    )
    from dbt_agent.config import VerifyConfig

    verify = VerifyConfig(**(read_json(Path(args.verify_config)) if args.verify_config else {}))
    result = run_job(
        JobSpec(job_id=run_id, task=task, template_dir=template, context=context, verify=verify),
        cfg,
        llm=_llm(args, cfg),
        make_sandbox=make,
        runs_root=runs_root,
    )
    print(
        json.dumps(
            {
                "run_dir": str(result.run_dir),
                "status": result.outcome["status"],
                "risk": (result.risk or {}).get("level"),
                "pr": (result.pr or {}).get("body"),
            },
            indent=2,
        )
    )
    return 0 if result.outcome["status"] == "completed" else 2


# --------------------------------------------------------------------------- bench sales
def cmd_bench_sales(args: argparse.Namespace) -> int:
    from dbt_agent.bench.metrics import summarise
    from dbt_agent.bench.oracle import oracle_llm
    from dbt_agent.bench.report import render_sales_report
    from dbt_agent.bench.sales import run_suite
    from dbt_agent.cli import _config, _select_tasks, _suite_env, sandbox_factory

    cfg = _config(args)
    if args.llm == "oracle":
        cfg.reviewer.enabled = args.reviewer == "config"
    env = _suite_env(args, cfg)
    tasks = _select_tasks(args)
    run_root = Path(args.runs_dir) / args.run_name
    make = sandbox_factory(cfg, labels={"dbt-agent.job": args.run_name})
    if args.llm == "oracle":

        def llm_for(task):
            return oracle_llm(task, env)

        reviewer = _llm(args, cfg) if args.reviewer == "config" else None
    else:
        model = _llm(args, cfg)

        def llm_for(task):
            return model

        reviewer = model if args.reviewer == "config" else None

    def progress(result: dict[str, Any]) -> None:
        print(
            f"{result['task_id']} t{result.get('trial')}: "
            f"{'RESOLVED' if result.get('resolved') else 'unresolved'} "
            f"[{result.get('status')}] {result.get('failed_checks') or ''} "
            f"{result.get('error') or ''}",
            flush=True,
        )

    from dbt_agent.cli import _parse_mb

    required = _parse_mb(cfg.sandbox.memory) + 1024 if cfg.sandbox.kind == "docker" else None
    results = run_suite(
        tasks,
        env,
        cfg,
        llm_for=llm_for,
        make_sandbox=make,
        run_root=run_root,
        trials=args.trials,
        reviewer_llm=reviewer,
        heavy_lock=args.heavy_lock,
        on_result=progress,
        required_mb=required,
    )
    summary = summarise(results, k=args.trials)
    meta = {
        "run_name": args.run_name,
        "model": "oracle" if args.llm == "oracle" else cfg.llm.model,
        "base_url": None if args.llm == "oracle" else cfg.llm.base_url,
        "trials": args.trials,
        "tasks": len(tasks),
        "finished_at": utc_now(),
        "config": {
            "budgets": cfg.agent.budgets.model_dump(),
            "context": cfg.agent.context.model_dump(),
            "temperature": cfg.llm.temperature,
            "top_p": cfg.llm.top_p,
            "max_tokens": cfg.llm.max_tokens,
            "sandbox": cfg.sandbox.model_dump(),
        },
    }
    payload = {"meta": meta, "summary": summary, "runs": results}
    stem = Path(args.report or (REPO_ROOT / "reports" / f"sales_{args.run_name}"))
    from dbt_agent.cli import _write_report

    _write_report(stem, payload, render_sales_report(meta, summary, results))
    print(json.dumps({k: summary[k] for k in ("runs", "resolve_rate", "pass_hat_k")}, indent=2))
    return 0


def cmd_bench_report(args: argparse.Namespace) -> int:
    """Rebuild a suite report from the per-task results on disk (no runs)."""
    from dbt_agent.bench.metrics import summarise
    from dbt_agent.bench.report import render_sales_report
    from dbt_agent.cli import _write_report

    run_root = Path(args.runs_dir) / args.run_name
    results = [read_json(p) for p in sorted(run_root.glob("*/bench_result.json"))]
    if args.tasks:  # a subset, e.g. the tasks that got extra trials for pass^k
        wanted = set(args.tasks)
        results = [
            r for r in results if r["task_id"] in wanted or r["task_id"].split("_")[0] in wanted
        ]
    if args.trials:
        results = [r for r in results if int(r.get("trial", 0)) < args.trials]
    summary = summarise(results, k=args.k)
    stem = Path(args.report or (REPO_ROOT / "reports" / f"sales_{args.run_name}"))
    previous = read_json(stem.with_suffix(".json")) or {}
    meta = {
        **(previous.get("meta") or {}),
        "run_name": args.run_name,
        "rebuilt_at": utc_now(),
        "runs_found": len(results),
        "task_filter": args.tasks or None,
        "trials_used": args.trials,
    }
    _write_report(
        stem,
        {"meta": meta, "summary": summary, "runs": results},
        render_sales_report(meta, summary, results),
    )
    return 0


# --------------------------------------------------------------------------- pr create
def cmd_pr_create(args: argparse.Namespace) -> int:
    from dbt_agent.pr.github import create_pull_request, prepare_publish_repo

    run_dir = Path(args.run_dir)
    pr = read_json(run_dir / "pr.json")
    risk = read_json(run_dir / "risk.json") or {"level": "high"}
    if not pr:
        print("no pr.json in the run directory: the agent did not finish", file=sys.stderr)
        return 2
    body = Path(pr["body"]).read_text(encoding="utf-8")
    patch = read_text_exact(Path(pr["patch"]))
    publish_dir = run_dir / "publish"
    if not args.dry_run:
        prepare_publish_repo(Path(args.template), patch, publish_dir, pr["branch"], pr["title"])
    result = create_pull_request(
        repo=args.repo,
        publish_dir=publish_dir,
        branch=pr["branch"],
        base=args.base,
        title=pr["title"],
        body=body,
        risk=risk["level"],
        dry_run=args.dry_run,
    )
    atomic_write_json(run_dir / "github.json", result.__dict__)
    print(json.dumps(result.__dict__, indent=2, default=str)[:4000])
    return 0 if not result.error else 1


def register(sub: argparse._SubParsersAction) -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", nargs="*", help="YAML config files (later wins)")
    common.add_argument("--sandbox", choices=["docker", "local"])
    common.add_argument("--runs-dir", default=str(REPO_ROOT / "runs"))

    run = sub.add_parser(
        "run", parents=[common], help="one job: a ticket or a failed CI run on a dbt project"
    )
    run.add_argument("--project", required=True, help="dbt project directory (with its DB)")
    source = run.add_mutually_exclusive_group(required=True)
    source.add_argument("--ticket", help="ticket text file (markdown)")
    source.add_argument("--ci-run-results", help="target/run_results.json of a failed run")
    run.add_argument("--title")
    run.add_argument("--db", default="warehouse.duckdb", help="DuckDB file inside the project")
    run.add_argument("--notes", help="project notes shown to the agent")
    run.add_argument("--verify-config", help="JSON with VerifyConfig fields")
    run.add_argument("--run-id")
    run.set_defaults(func=cmd_run)

    bench = sub.add_parser("bench", help="benchmarks")
    bsub = bench.add_subparsers(dest="bench", required=True)
    sales = bsub.add_parser("sales", parents=[common], help="the 50-task sales suite")
    sales.add_argument("--tasks-dir", default=str(REPO_ROOT / "tasks" / "sales"))
    sales.add_argument("--tasks", nargs="*")
    sales.add_argument("--base-dir")
    sales.add_argument("--data-dir", default=str(REPO_ROOT / "bench_data" / "sales"))
    sales.add_argument("--llm", choices=["config", "oracle"], default="config")
    sales.add_argument("--reviewer", choices=["config", "none"], default="config")
    sales.add_argument("--trials", type=int, default=1)
    sales.add_argument("--run-name", required=True)
    sales.add_argument("--report")
    sales.add_argument("--heavy-lock", action="store_true")
    sales.set_defaults(func=cmd_bench_sales)

    report = bsub.add_parser("report", parents=[common], help="rebuild a suite report")
    report.add_argument("--run-name", required=True)
    report.add_argument("--k", type=int)
    report.add_argument("--tasks", nargs="*", help="only these task ids (or B01-style prefixes)")
    report.add_argument("--trials", type=int, help="only trials 0..N-1")
    report.add_argument("--report")
    report.set_defaults(func=cmd_bench_report)

    from dbt_agent.bench import spider2

    spider2.register(bsub, common)
    from dbt_agent.bench import adebench

    adebench.register(bsub, common)

    pr = sub.add_parser("pr", help="pull requests")
    psub = pr.add_subparsers(dest="pr", required=True)
    create = psub.add_parser("create", help="open the PR on GitHub (dry run by default)")
    create.add_argument("--run-dir", required=True)
    create.add_argument("--template", required=True, help="the run's trusted starting snapshot")
    create.add_argument("--repo", required=True, help="owner/name")
    create.add_argument("--base", default="main")
    create.add_argument("--no-dry-run", dest="dry_run", action="store_false")
    create.set_defaults(func=cmd_pr_create, dry_run=True)
