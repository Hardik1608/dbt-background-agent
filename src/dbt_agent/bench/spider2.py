"""Spider 2.0-DBT adapter (github.com/xlang-ai/Spider2, `spider2-dbt`, MIT licence).

68 tasks: each is an unfinished dbt project on DuckDB plus a natural-language instruction.
The official evaluation (``evaluation_suite/evaluate.py``) compares selected tables of the
result ``.duckdb`` file with a gold ``.duckdb`` file (``duckdb_match``).

    bench spider2 setup     sparse-clone the repo and download the two Drive archives
                            (resumable, verified). The .duckdb files total 3.9 GB unpacked,
                            so by default they stay in the archives and each task extracts
                            its own start database (``--extract-all`` gives the official
                            layout instead, when disk allows)
    bench spider2 run       every task through our harness in the Docker sandbox
    bench spider2 evaluate  the official evaluate.py, unmodified, run once per instance with
                            that instance's gold database extracted next to it (scores are
                            per instance, so the sum equals one run over all instances)

Disk: after each run only the tables named in the gold spec are kept (copied inside the
sandbox into a small DuckDB file); the working database and the template are deleted.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from dbt_agent.util import atomic_write_json, atomic_write_text, force_rmtree, read_json, utc_now

log = logging.getLogger(__name__)
REPO_URL = "https://github.com/xlang-ai/Spider2.git"
DRIVE_FILES = {
    "DBT_start_db.zip": "1N3f7BSWC4foj-V-1C9n8M2XmgV7FOcqL",
    "dbt_gold.zip": "1s0USV_iQLo4oe05QqAMnhGGp5jeejCzp",
}

NOTES = """\
This is a Spider 2.0-DBT task. The benchmark's own hints:
- The project is unfinished. Compare the YAML files with the SQL files: models that are
  defined in YAML but have no SQL (or clearly unfinished SQL) must be written.
- All data is in the DuckDB file `{db}`; explore it with query_duckdb (schema main).
- Do not modify the YAML files; write SQL that matches them. Usually you only create new
  SQL files; change existing ones only if they are clearly unfinished.
- When the SQL is written, run run_dbt(command="run") so the models are created in `{db}`,
  then check the new tables with query_duckdb.
- The result is graded by comparing tables in `{db}` with a hidden gold database.\
"""


@dataclass
class Spider2Task:
    instance_id: str
    instruction: str
    db_file: str  # the gold database name (spider2_eval.jsonl)
    condition_tabs: list[str]
    result_db: str = ""  # the project's own DuckDB file (profiles.yml); usually == db_file

    def __post_init__(self) -> None:
        self.result_db = self.result_db or self.db_file

    @property
    def title(self) -> str:
        return f"Spider2-DBT {self.instance_id}"


def project_database(project: Path) -> str | None:
    """The DuckDB path a project's profiles.yml points at, relative to the project."""
    profiles = project / "profiles.yml"
    if not profiles.exists():
        return None
    data = yaml.safe_load(profiles.read_text(encoding="utf-8")) or {}
    for profile in data.values():
        for output in ((profile or {}).get("outputs") or {}).values():
            path = str((output or {}).get("path") or "")
            if (output or {}).get("type") == "duckdb" and path and ":" not in path:
                clean = Path(path.removeprefix("./"))
                if not clean.is_absolute() and ".." not in clean.parts:
                    return clean.as_posix()
    return None


# --------------------------------------------------------------------------- setup
def _run(argv: list[str], cwd: Path | None = None, timeout: float = 1800) -> str:
    proc = subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(argv[:3])} failed: {proc.stderr[-800:]}")
    return proc.stdout


def free_gb(path: Path) -> float:
    usage = shutil.disk_usage(path)
    return usage.free / 1e9


START_ARCHIVE = "DBT_start_db.zip"
GOLD_ARCHIVE = "dbt_gold.zip"


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def data_issues(data_dir: Path) -> dict[str, list[str]]:
    """Instances that cannot pass with the public data, whatever the agent does."""
    gold_archive = data_dir / "downloads" / GOLD_ARCHIVE
    if not gold_archive.exists():
        return {}
    present: dict[str, set[str]] = {}
    with zipfile.ZipFile(gold_archive) as archive:
        for info in archive.infolist():
            if _wanted(info):
                parts = Path(info.filename).parts
                present.setdefault(parts[0], set()).add(parts[-1])
    missing, misnamed = [], []
    for task in load_tasks(data_dir):
        if task.instance_id not in present:
            missing.append(task.instance_id)
        elif task.db_file not in present[task.instance_id]:
            misnamed.append(task.instance_id)
    return {"no_gold_database": missing, "gold_name_not_in_archive": misnamed}


def setup(
    data_dir: Path,
    *,
    min_free_gb: float = 15.0,
    extract_all: bool = False,
    max_extract_gb: float = 3.0,
) -> dict[str, Any]:
    data_dir.mkdir(parents=True, exist_ok=True)
    repo = data_dir / "repo"
    if not (repo / "spider2-dbt").exists():
        _run(
            ["git", "clone", "--depth", "1", "--filter=blob:none", "--sparse", REPO_URL, str(repo)]
        )
        _run(["git", "sparse-checkout", "set", "spider2-dbt"], cwd=repo)
    root = repo / "spider2-dbt"
    commit = _run(["git", "rev-parse", "HEAD"], cwd=repo).strip()
    downloads = data_dir / "downloads"
    downloads.mkdir(exist_ok=True)
    for name, file_id in DRIVE_FILES.items():
        target = downloads / name
        if target.exists():
            continue
        if free_gb(data_dir) < min_free_gb + 1.5:
            raise RuntimeError(f"not enough disk for {name}: {free_gb(data_dir):.1f} GB free")
        partial = downloads / f"{name}.part"  # resumable after a power cut (--continue)
        _run(
            [
                sys.executable,
                "-m",
                "gdown",
                "--quiet",
                "--no-cookies",
                "--continue",
                "--retries",
                "5",
                file_id,
                "-O",
                str(partial),
            ]
        )
        with zipfile.ZipFile(partial) as archive:  # complete and readable, or raise
            if archive.testzip() is not None:
                raise RuntimeError(f"{name}: corrupt archive member")
        partial.replace(target)
    sizes = {}
    for name in DRIVE_FILES:
        with zipfile.ZipFile(downloads / name) as archive:
            sizes[name] = sum(i.file_size for i in archive.infolist() if _wanted(i))
    summary: dict[str, Any] = {
        "repo_commit": commit,
        "uncompressed_bytes": sizes,
        "archive_bytes": {
            n: (downloads / n).stat().st_size for n in DRIVE_FILES if (downloads / n).exists()
        },
        "archive_sha256": {
            n: _sha256_file(downloads / n) for n in DRIVE_FILES if (downloads / n).exists()
        },
        "data_issues": data_issues(data_dir),
        "tasks": len(load_tasks(data_dir)),
        "layout": "archives (extracted per task)",
    }
    if extract_all:
        needed_gb = sum(sizes.values()) / 1e9
        if needed_gb > max_extract_gb:
            raise RuntimeError(
                f"extracting needs {needed_gb:.2f} GB, over the "
                f"{max_extract_gb} GB allowed; archives kept in downloads/"
            )
        if free_gb(data_dir) - needed_gb < min_free_gb:
            raise RuntimeError(
                f"extracting needs {needed_gb:.1f} GB; only "
                f"{free_gb(data_dir):.1f} GB free (floor {min_free_gb} GB)"
            )
        _extract(downloads / START_ARCHIVE, root / "examples")
        _extract(downloads / GOLD_ARCHIVE, root / "evaluation_suite" / "gold")
        for name in DRIVE_FILES:  # the extracted files are all we need
            (downloads / name).unlink()
        summary["layout"] = "official (extracted)"
    summary["set_up_at"] = utc_now()
    atomic_write_json(data_dir / "setup.json", summary)
    return summary


def _wanted(info: zipfile.ZipInfo) -> bool:
    parts = Path(info.filename).parts
    if info.is_dir() or not info.filename.endswith(".duckdb") or "__MACOSX" in parts:
        return False
    # the official setup.py extracts every top-level folder (its bq/ga/local/sf prefix filter
    # only protects folders from its clean-up step); path traversal is refused here
    return len(parts) >= 2 and not any(p in ("..", "", "/") or p.startswith("/") for p in parts)


def _extract(archive_path: Path, target_root: Path) -> None:
    """As the official setup.py: <archive>/<instance>/**.duckdb -> target/<instance>/."""
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            if not _wanted(info):
                continue
            parts = Path(info.filename).parts
            instance = parts[0]
            dest = target_root / instance / Path(info.filename).name
            dest.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, dest.open("wb") as out:
                shutil.copyfileobj(src, out, length=1024 * 1024)


def _extract_instance(archive_path: Path, instance: str, dest: Path) -> list[str]:
    """One instance's .duckdb files from an archive, flattened into ``dest``."""
    names = []
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            if not _wanted(info) or Path(info.filename).parts[0] != instance:
                continue
            dest.mkdir(parents=True, exist_ok=True)
            target = dest / Path(info.filename).name
            with archive.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out, length=1024 * 1024)
            names.append(target.name)
    return names


def instance_databases(data_dir: Path, instance: str, kind: str, dest: Path) -> list[str]:
    """Start (``kind="start"``) or gold databases of one instance, copied into ``dest``."""
    archive = data_dir / "downloads" / (START_ARCHIVE if kind == "start" else GOLD_ARCHIVE)
    if archive.exists():
        return _extract_instance(archive, instance, dest)
    suite = data_dir / "repo" / "spider2-dbt"
    folder = suite / "examples" if kind == "start" else suite / "evaluation_suite" / "gold"
    names = []
    for db in sorted((folder / instance).glob("*.duckdb")):  # the official layout
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(db, dest / db.name)
        names.append(db.name)
    return names


def load_tasks(data_dir: Path) -> list[Spider2Task]:
    root = data_dir / "repo" / "spider2-dbt"
    tasks_file = root / "examples" / "spider2-dbt.jsonl"
    gold_file = root / "evaluation_suite" / "gold" / "spider2_eval.jsonl"
    gold = {}
    for line in gold_file.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entry = json.loads(line)
            gold[entry["instance_id"]] = entry["evaluation"]["parameters"]
    tasks = []
    for line in tasks_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        params = gold[entry["instance_id"]]
        own = project_database(root / "examples" / entry["instance_id"])
        tasks.append(
            Spider2Task(
                entry["instance_id"],
                entry["instruction"],
                params["gold"],
                list(params.get("condition_tabs") or []),
                own or "",
            )
        )
    return tasks


# --------------------------------------------------------------------------- per task
def prepare_template(task: Spider2Task, data_dir: Path, make_sandbox, context) -> Path:
    from dbt_agent.workspace import init_git_baseline, write_baseline_manifest

    template = data_dir / "templates" / task.instance_id / "start"
    marker = template.parent / "ready.json"
    if marker.exists():
        return template
    force_rmtree(template.parent)
    source = data_dir / "repo" / "spider2-dbt" / "examples" / task.instance_id
    # symlinks stay links (the sandbox refuses to follow them out of the project)
    shutil.copytree(source, template, symlinks=True, ignore=shutil.ignore_patterns("*.duckdb"))
    databases = instance_databases(data_dir, task.instance_id, "start", template)
    if Path(task.result_db).name not in databases:
        log.warning(
            "%s: start database %s not found (got %s)", task.instance_id, task.result_db, databases
        )
    profiles = template / "profiles.yml"
    data = yaml.safe_load(profiles.read_text(encoding="utf-8"))
    for profile in data.values():  # resource limits only; the path is unchanged
        for output in (profile.get("outputs") or {}).values():
            if output.get("type") == "duckdb":
                output.setdefault("settings", {}).update({"memory_limit": "1GB"})
                output["threads"] = 2
    profiles.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8", newline="\n")
    init_git_baseline(template, f"Spider2-DBT {task.instance_id}")
    box = make_sandbox(template, context)
    baseline = "start"
    try:
        write_baseline_manifest(box)
    except Exception:  # unfinished projects may not parse; the checklist then runs all
        baseline = "none"
    finally:
        box.stop()
    atomic_write_json(marker, {"baseline": baseline, "prepared_at": utc_now()})
    return template


def _safe_copy(src: Path, dst: Path, root: Path) -> bool:
    """Copy a file the sandbox produced, refusing symlinks or paths outside ``root``."""
    try:
        info = os.lstat(src)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return False
    if Path(os.path.realpath(src)).parent != Path(os.path.realpath(root)):
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(src, flags)
    with os.fdopen(fd, "rb") as handle, dst.open("wb") as out:
        shutil.copyfileobj(handle, out, length=1024 * 1024)
    return True


def collect_result(task: Spider2Task, box, workdir: Path, submission: Path) -> dict[str, Any]:
    """Inside the sandbox, copy the gold-spec tables into a small DuckDB; then fetch it."""
    out_name = ".spider2_result.duckdb"
    statements = [f"attach '{out_name}' as result_db"]
    for table in task.condition_tabs:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
            continue
        statements.append(
            f"create or replace table result_db.main.{table} as select * from {table}"
        )
    found, missing = [], []
    # one connection per table, so one missing table does not hide the others
    for table, statement in zip(task.condition_tabs, statements[1:], strict=False):
        result = box.call("sql_exec", {"statements": [statements[0], statement]})
        (found if result.ok else missing).append(table)
    dest = submission / task.instance_id / Path(task.result_db).name
    copied = _safe_copy(workdir / out_name, dest, workdir) if found else False
    return {"tables_found": found, "tables_missing": missing, "copied": copied}


def run_task(
    task: Spider2Task,
    data_dir: Path,
    cfg,
    *,
    llm,
    make_sandbox,
    run_root: Path,
    submission: Path,
    trial: int = 0,
) -> dict[str, Any]:
    from dbt_agent.config import VerifyConfig
    from dbt_agent.harness.prompts import AgentTask
    from dbt_agent.pipeline import JobSpec, run_job
    from dbt_agent.sandbox.base import SandboxContext
    from dbt_agent.workspace import remove_databases

    run_id = f"{task.instance_id}__t{trial}"
    run_dir = run_root / run_id
    cached = read_json(run_dir / "bench_result.json")
    if cached:
        return cached
    started = time.monotonic()
    context = SandboxContext(db_path=task.result_db, dbt_timeout_s=cfg.sandbox.dbt_timeout_s)
    template = prepare_template(task, data_dir, make_sandbox, context)
    spec = JobSpec(
        job_id=run_id,
        task=AgentTask(
            task_id=task.instance_id,
            title=task.title,
            body=task.instruction,
            kind="benchmark",
            project_notes=NOTES.format(db=task.result_db),
        ),
        template_dir=template,
        context=context,
        verify=VerifyConfig(enabled=False),
    )
    job = run_job(spec, cfg, llm=llm, make_sandbox=make_sandbox, runs_root=run_root)
    workdir = run_dir / "workdir"
    box = make_sandbox(workdir, context)
    try:
        collected = collect_result(task, box, workdir, submission)
    finally:
        box.stop()
    outcome = job.outcome
    result = {
        "instance_id": task.instance_id,
        "trial": trial,
        "status": outcome["status"],
        "turns": outcome.get("turns"),
        "tool_calls": outcome.get("tool_calls"),
        "prompt_tokens": outcome.get("prompt_tokens"),
        "completion_tokens": outcome.get("completion_tokens"),
        "agent_seconds": outcome.get("wall_seconds"),
        "security": outcome.get("security", {}),
        "patch_files": [f["path"] for f in job.patch_files],
        "collected": collected,
        "total_seconds": round(time.monotonic() - started, 1),
    }
    atomic_write_json(run_dir / "bench_result.json", result)
    remove_databases(workdir)
    force_rmtree(template.parent)  # templates hold a full DB copy; rebuilt if ever needed
    return result


# --------------------------------------------------------------------------- evaluation
SUMMARY_RE = re.compile(r"([0-9.]+) (\d+) (\d+)")


def _official_eval(
    python: str, suite: Path, result_dir: Path, gold_dir: Path, timeout: float = 900
) -> dict[str, Any]:
    """Run the unmodified evaluate.py; stdin is closed so its pdb fallback cannot hang."""
    proc = subprocess.run(
        [
            python,
            str(suite / "evaluate.py"),
            "--result_dir",
            str(result_dir),
            "--gold_dir",
            str(gold_dir),
        ],
        cwd=suite,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    lines = [ln.strip() for ln in proc.stdout.splitlines()]
    summary = next(
        (SUMMARY_RE.fullmatch(ln) for ln in reversed(lines) if SUMMARY_RE.fullmatch(ln)), None
    )
    return {
        "returncode": proc.returncode,
        "lines": lines,
        "passed": int(summary.group(2)) if summary else None,
        "evaluated": int(summary.group(3)) if summary else None,
        "stdout_tail": proc.stdout[-1500:],
        "stderr_tail": proc.stderr[-1500:],
    }


def evaluate(
    data_dir: Path,
    submission: Path,
    results: list[dict[str, Any]],
    python: str,
    work: Path | None = None,
) -> dict[str, Any]:
    """Official evaluate.py, once per instance, with that instance's gold DB beside it."""
    tasks = {t.instance_id: t for t in load_tasks(data_dir)}
    suite = (data_dir / "repo" / "spider2-dbt" / "evaluation_suite").resolve()  # cwd changes
    gold_spec = suite / "gold" / "spider2_eval.jsonl"
    work = work or data_dir / "eval_work"
    lines, per_instance = [], {}
    for result in results:
        task = tasks[result["instance_id"]]
        entry = {
            "instance_id": task.instance_id,
            "answer_type": "file",
            "answer_or_path": Path(task.result_db).name,
        }
        lines.append(json.dumps(entry))
        tmp = work / task.instance_id
        force_rmtree(tmp)
        gold_dir, result_dir = tmp / "gold", tmp / "result"
        gold_dir.mkdir(parents=True)
        shutil.copyfile(gold_spec, gold_dir / "spider2_eval.jsonl")
        gold_dbs = instance_databases(
            data_dir, task.instance_id, "gold", gold_dir / task.instance_id
        )
        (result_dir / task.instance_id).mkdir(parents=True)
        answer = submission / task.instance_id / Path(task.result_db).name
        if answer.exists():
            shutil.copyfile(answer, result_dir / task.instance_id / answer.name)
        atomic_write_text(result_dir / "results_metadata.jsonl", json.dumps(entry) + "\n")
        try:
            out = _official_eval(python, suite, result_dir.resolve(), gold_dir.resolve())
        finally:
            force_rmtree(tmp)
        per_instance[task.instance_id] = {
            "passed": out["passed"] == 1,
            "id_printed": task.instance_id in out["lines"],
            "evaluated": out["evaluated"],
            "returncode": out["returncode"],
            "gold_databases": gold_dbs,
            "had_result": answer.exists(),
            "stderr_tail": out["stderr_tail"][-400:],
        }
    atomic_write_text(submission / "results_metadata.jsonl", "\n".join(lines) + "\n")
    passed = sorted(i for i, r in per_instance.items() if r["passed"])
    evaluated = sum(1 for r in per_instance.values() if r["evaluated"] == 1)
    return {
        "official_passed": len(passed),
        "official_evaluated": evaluated,
        "official_ratio": round(len(passed) / evaluated, 4) if evaluated else None,
        "passed_instances": passed,
        "per_instance": per_instance,
        "eval_errors": sorted(i for i, r in per_instance.items() if r["evaluated"] != 1),
    }


# --------------------------------------------------------------------------- CLI
def _cmd_setup(args: argparse.Namespace) -> int:
    print(json.dumps(setup(Path(args.data_dir), extract_all=args.extract_all), indent=2))
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    from dbt_agent.cli import _config, heavy_lock, sandbox_factory
    from dbt_agent.llm.client import OpenAICompatClient

    cfg = _config(args)
    cfg.reviewer.enabled = False
    data_dir = Path(args.data_dir)
    tasks = load_tasks(data_dir)
    if args.tasks:
        tasks = [t for t in tasks if t.instance_id in set(args.tasks)]
    if args.limit:
        tasks = tasks[: args.limit]
    run_root = Path(args.runs_dir) / args.run_name
    submission = run_root / "_submission"
    make = sandbox_factory(cfg, labels={"dbt-agent.job": args.run_name})
    llm = OpenAICompatClient(cfg.llm)
    for task in tasks:
        cached = read_json(run_root / f"{task.instance_id}__t0" / "bench_result.json")
        if cached:  # done before a restart: no RAM wait, no lock
            print(f"{task.instance_id}: {cached.get('status')} (cached)", flush=True)
            continue
        with heavy_lock(args.heavy_lock, cfg):
            try:
                result = run_task(
                    task,
                    data_dir,
                    cfg,
                    llm=llm,
                    make_sandbox=make,
                    run_root=run_root,
                    submission=submission,
                )
            except Exception as exc:
                log.exception("spider2 %s crashed", task.instance_id)
                result = {
                    "instance_id": task.instance_id,
                    "status": "infrastructure_error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
        print(
            f"{task.instance_id}: {result.get('status')} "
            f"tables {result.get('collected', {}).get('tables_found')}",
            flush=True,
        )
    return 0


def _cmd_evaluate(args: argparse.Namespace) -> int:
    from dbt_agent.cli import _write_report

    data_dir = Path(args.data_dir)
    run_root = Path(args.runs_dir) / args.run_name
    results = [read_json(p) for p in sorted(run_root.glob("*/bench_result.json"))]
    official = evaluate(data_dir, run_root / "_submission", results, args.eval_python)
    all_tasks = load_tasks(data_dir)
    statuses: dict[str, int] = {}
    for r in results:
        statuses[r.get("status")] = statuses.get(r.get("status"), 0) + 1
    passed = set(official["passed_instances"])
    resolved_runs = [r for r in results if r["instance_id"] in passed]
    summary = {
        "benchmark": "Spider 2.0-DBT",
        "tasks_total": len(all_tasks),
        "runs": len(results),
        "official": official,
        "score_over_all_68": (
            round(official["official_passed"] / len(all_tasks), 4)
            if official["official_passed"] is not None
            else None
        ),
        "status_counts": statuses,
        "tokens_total": sum(
            (r.get("prompt_tokens") or 0) + (r.get("completion_tokens") or 0) for r in results
        ),
        "mean_tokens_per_resolved": (
            round(
                sum(
                    (r.get("prompt_tokens") or 0) + (r.get("completion_tokens") or 0)
                    for r in resolved_runs
                )
                / len(resolved_runs)
            )
            if resolved_runs
            else None
        ),
        "mean_agent_seconds_per_resolved": (
            round(sum(r.get("agent_seconds") or 0 for r in resolved_runs) / len(resolved_runs), 1)
            if resolved_runs
            else None
        ),
        "escape_attempts": sum(
            (r.get("security") or {}).get("escape_attempts", 0) for r in results
        ),
        "egress_attempts": sum(
            (r.get("security") or {}).get("egress_attempts", 0) for r in results
        ),
        "runs_with_all_gold_tables": sum(
            1
            for r in results
            if (r.get("collected") or {}).get("tables_found")
            and not (r.get("collected") or {}).get("tables_missing")
        ),
        "public_data_issues": (read_json(data_dir / "setup.json") or {}).get("data_issues"),
        "evaluated_at": utc_now(),
        "model": args.model_label,
    }
    md = [
        "# Spider 2.0-DBT",
        "",
        f"- Model: `{args.model_label}`; harness: dbt-background-agent; one trial per task.",
        f"- Official evaluate.py: **{official['official_passed']}/{len(all_tasks)} "
        f"({100 * (summary['score_over_all_68'] or 0):.1f}%)** "
        f"(evaluated {official['official_evaluated']} submissions; the unmodified script "
        "was run once per instance with that instance's gold database).",
        "- Context (external, not ours): leaderboard top reported 65.6% (SignalPilot, "
        "vendor-reported).",
        "",
        "| Status of the agent loop | Runs |",
        "|---|---|",
    ]
    md += [f"| {k} | {v} |" for k, v in sorted(statuses.items(), key=lambda kv: str(kv[0]))]
    issues = summary["public_data_issues"] or {}
    md += [
        "",
        f"Passed instances: {', '.join(official['passed_instances']) or 'none'}",
        "",
        f"Runs that produced every table the gold spec compares: "
        f"{summary['runs_with_all_gold_tables']}/{len(results)}.",
        "",
    ]
    if any(issues.values()):
        md += [
            "Instances no agent can pass with the public data (they still count in the "
            "denominator, as in the official script):",
            f"- no gold database in `dbt_gold.zip`: "
            f"{', '.join(issues.get('no_gold_database') or []) or 'none'}",
            f"- gold file name in `spider2_eval.jsonl` not in the archive: "
            f"{', '.join(issues.get('gold_name_not_in_archive') or []) or 'none'}",
            "",
        ]
    stem = Path(args.report or "reports/spider2_dbt")
    _write_report(stem, {"summary": summary, "runs": results}, "\n".join(md))
    return 0


def register(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    spider = sub.add_parser("spider2", help="Spider 2.0-DBT")
    ssub = spider.add_subparsers(dest="spider2", required=True)
    for name, func in (("setup", _cmd_setup), ("run", _cmd_run), ("evaluate", _cmd_evaluate)):
        sp = ssub.add_parser(name, parents=[common])
        sp.add_argument("--data-dir", default="bench_data/spider2")
        sp.add_argument("--run-name", default="spider2_local_agent")
        sp.add_argument("--tasks", nargs="*")
        sp.add_argument("--limit", type=int)
        sp.add_argument("--heavy-lock", action="store_true")
        sp.add_argument("--report")
        sp.add_argument("--eval-python", default=sys.executable)
        sp.add_argument("--model-label", default="local-agent (Qwen3.5-9B int4)")
        sp.add_argument(
            "--extract-all",
            action="store_true",
            help="setup: unpack all databases (official layout, 3.9 GB)",
        )
        sp.set_defaults(func=func)
