"""Command line: ``dbt-agent <command>`` (or ``python -m dbt_agent``).

fixture build                   build the sales-warehouse base fixture
suite list | validate           the 50-task sales suite (fail before / pass after)
run                             one job: ticket or failed CI run -> sandbox -> PR package
bench sales | spider2 | adebench | report
pr create                       open the PR on GitHub (dry run unless --no-dry-run)
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from dbt_agent.config import RunConfig, load_config
from dbt_agent.sandbox.base import Sandbox, SandboxContext

REPO_ROOT = Path(__file__).resolve().parents[2]
log = logging.getLogger("dbt_agent")


# --------------------------------------------------------------------------- shared helpers
def sandbox_factory(cfg: RunConfig, *, labels: dict[str, str] | None = None):
    """Build sandboxes from config. Docker sandboxes wait for RAM (cap + 1 GB) first."""
    kind = cfg.sandbox.kind

    def make(root: Path, context: SandboxContext) -> Sandbox:
        if kind == "local":
            from dbt_agent.sandbox.local import LocalSandbox

            return LocalSandbox(root, context)
        from dbt_agent.sandbox.docker import DockerSandbox

        cap_mb = _parse_mb(cfg.sandbox.memory)
        return DockerSandbox(
            root,
            context,
            image=cfg.sandbox.image,
            memory=cfg.sandbox.memory,
            cpus=cfg.sandbox.cpus,
            pids_limit=cfg.sandbox.pids_limit,
            network=cfg.sandbox.network,
            runtime=cfg.sandbox.runtime,
            labels=labels,
            memory_guard_mb=cap_mb + 1024,
        )

    return make


def _parse_mb(value: str) -> int:
    value = value.strip().lower()
    if value.endswith("g"):
        return int(float(value[:-1]) * 1024)
    if value.endswith("m"):
        return int(float(value[:-1]))
    return int(value) // (1024 * 1024)


@contextlib.contextmanager
def heavy_lock(
    enabled: bool, cfg: RunConfig | None = None, project: str = "dbt-background-agent"
) -> Iterator[None]:
    """Admission for one unit of box work: RAM first (cap + 1 GB), then the shared lock."""
    from dbt_agent.bench.boxguard import admission

    required = (
        _parse_mb(cfg.sandbox.memory) + 1024
        if cfg is not None and cfg.sandbox.kind == "docker"
        else None
    )
    with admission(project, required_mb=required, lock=enabled, log=lambda m: log.info(m)):
        yield


def _config(args: argparse.Namespace) -> RunConfig:
    paths = [Path(p) for p in (getattr(args, "config", None) or [])]
    overrides: dict[str, Any] = {}
    if getattr(args, "sandbox", None):
        overrides.setdefault("sandbox", {})["kind"] = args.sandbox
    return load_config(paths, overrides)


def _suite_env(args: argparse.Namespace, cfg: RunConfig):
    from dbt_agent.fixtures.sales import DEFAULT_BASE
    from dbt_agent.tasks.templates import SuiteEnv

    return SuiteEnv(
        base_dir=Path(args.base_dir or DEFAULT_BASE),
        data_dir=Path(args.data_dir),
        make_sandbox=sandbox_factory(cfg, labels={"dbt-agent.job": "suite"}),
    )


def _select_tasks(args: argparse.Namespace):
    from dbt_agent.tasks.model import load_suite

    tasks = load_suite(Path(args.tasks_dir))
    if getattr(args, "tasks", None):
        wanted = set(args.tasks)
        tasks = [t for t in tasks if t.id in wanted or t.id.split("_")[0] in wanted]
    return tasks


def _write_report(stem: Path, payload: dict[str, Any], markdown: str) -> None:
    from dbt_agent.util import atomic_write_json, atomic_write_text

    atomic_write_json(stem.with_suffix(".json"), payload)
    atomic_write_text(stem.with_suffix(".md"), markdown)
    print(f"wrote {stem.with_suffix('.json')} and {stem.with_suffix('.md')}")


# --------------------------------------------------------------------------- commands
def cmd_fixture_build(args: argparse.Namespace) -> int:
    from dbt_agent.fixtures.sales import DEFAULT_BASE, build_base

    summary = build_base(Path(args.base_dir or DEFAULT_BASE), force=args.force)
    print(json.dumps({k: v for k, v in summary.items() if k != "statuses"}, indent=2))
    return 0


def cmd_suite_list(args: argparse.Namespace) -> int:
    for task in _select_tasks(args):
        print(f"{task.id:48s} {task.kind:9s} {task.category:16s} {task.difficulty}")
    return 0


def cmd_suite_validate(args: argparse.Namespace) -> int:
    from dbt_agent.tasks.validate import render_markdown, summarise, validate_task

    cfg = _config(args)
    env = _suite_env(args, cfg)
    results = []
    for task in _select_tasks(args):
        with heavy_lock(args.heavy_lock, cfg):
            try:
                result = validate_task(task, env, stability=args.stability, force=args.force)
            except Exception as exc:  # keep going; the report shows the broken task
                log.exception("validation of %s crashed", task.id)
                result = {
                    "task_id": task.id,
                    "kind": task.kind,
                    "category": task.category,
                    "valid": False,
                    "fails_before": False,
                    "passes_with_reference": False,
                    "empty_failed_checks": [],
                    "reference_failed_checks": [],
                    "error": f"{type(exc).__name__}: {exc}",
                    "seconds": 0,
                    "stability": None,
                }
        status = "VALID" if result["valid"] else "INVALID"
        print(
            f"{task.id}: {status} ({result.get('seconds')} s) "
            f"empty fails: {result.get('empty_failed_checks')} "
            f"ref fails: {result.get('reference_failed_checks')} "
            f"{result.get('error') or result.get('reference_error') or ''}",
            flush=True,
        )
        results.append(result)
    summary = summarise(results)
    if args.report:
        _write_report(
            Path(args.report),
            {"summary": summary, "tasks": results},
            render_markdown(results, summary),
        )
    print(json.dumps(summary, indent=2))
    return 0 if summary["valid"] == summary["tasks"] else 1


def cmd_suite_debug(args: argparse.Namespace) -> int:
    """Grade the reference (or empty) patch of tasks and print every check (diagnosis)."""
    from dbt_agent.tasks.grader import grade
    from dbt_agent.tasks.templates import build_template, reference_patch

    cfg = _config(args)
    env = _suite_env(args, cfg)
    for task in _select_tasks(args):
        build_template(task, env, force=args.force)
        patch = (
            ""
            if args.empty
            else reference_patch(task, env, env.data_dir / "scratch" / f"{task.id}_dbg_src")
        )
        result = grade(
            task,
            env,
            patch,
            env.data_dir / "scratch" / f"{task.id}_dbg",
            record_reference=args.record,
            keep_workdir=args.keep,
        )
        out = result.as_dict()
        out.pop("recorded", None)
        print(json.dumps(out, indent=1, default=str)[:12000])
    return 0


def cmd_suite_compact(args: argparse.Namespace) -> int:
    """Slim existing templates (compact DuckDB files, drop target/ and logs/)."""
    from dbt_agent.tasks.templates import slim_template
    from dbt_agent.util import force_rmtree

    cfg = _config(args)
    env = _suite_env(args, cfg)
    wanted = {t.id for t in _select_tasks(args)}
    for template in sorted((env.data_dir / "templates").iterdir()):
        if template.name not in wanted:
            force_rmtree(template)  # stale task id
            print(f"removed stale template {template.name}")
            continue
        before = sum(f.stat().st_size for f in template.rglob("*") if f.is_file())
        slim_template(template / "start", env.db_file)
        after = sum(f.stat().st_size for f in template.rglob("*") if f.is_file())
        print(f"{template.name}: {before / 1e6:.1f} MB -> {after / 1e6:.1f} MB")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dbt-agent", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    fixture = sub.add_parser("fixture", help="build the sales base fixture")
    fsub = fixture.add_subparsers(dest="action", required=True)
    fbuild = fsub.add_parser("build")
    fbuild.add_argument("--base-dir")
    fbuild.add_argument("--force", action="store_true")
    fbuild.set_defaults(func=cmd_fixture_build)

    suite = sub.add_parser("suite", help="the sales task suite")
    ssub = suite.add_subparsers(dest="action", required=True)
    for name, func in (
        ("list", cmd_suite_list),
        ("validate", cmd_suite_validate),
        ("debug", cmd_suite_debug),
        ("compact", cmd_suite_compact),
    ):
        sp = ssub.add_parser(name)
        sp.add_argument("--tasks-dir", default=str(REPO_ROOT / "tasks" / "sales"))
        sp.add_argument("--tasks", nargs="*", help="task ids or prefixes (B01, F03)")
        sp.add_argument("--config", nargs="*", help="YAML config files")
        sp.add_argument("--sandbox", choices=["docker", "local"])
        sp.add_argument("--base-dir")
        sp.add_argument("--data-dir", default=str(REPO_ROOT / "bench_data" / "sales"))
        sp.add_argument(
            "--stability",
            action="store_true",
            help="grade the reference twice (determinism of fingerprints)",
        )
        sp.add_argument("--force", action="store_true", help="rebuild templates")
        sp.add_argument("--report", help="report path stem, e.g. reports/suite_validation")
        sp.add_argument(
            "--heavy-lock",
            action="store_true",
            help="take the shared GPU-box heavy-job lock per task",
        )
        sp.add_argument("--empty", action="store_true", help="debug: grade the empty patch")
        sp.add_argument("--record", action="store_true", help="debug: record mode")
        sp.add_argument("--keep", action="store_true", help="debug: keep the grading workdir")
        sp.set_defaults(func=func)

    from dbt_agent import cli_run

    cli_run.register(sub)
    return parser


def _import_everything() -> None:
    """Load every module now. Long runs wait hours for the box lock while the code on disk
    may be updated; importing lazily later would mix old and new modules in one process."""
    import importlib
    import pkgutil

    import dbt_agent

    for module in pkgutil.walk_packages(dbt_agent.__path__, "dbt_agent."):
        if ".fixtures.sales_source" in module.name or module.name.endswith("__main__"):
            continue
        importlib.import_module(module.name)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _import_everything()
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
