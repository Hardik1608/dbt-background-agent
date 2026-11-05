"""Build the sales-warehouse test bed (the base every suite task starts from).

Source: the dbt project of ``05_sales-analytics-platform`` (copied into
``fixtures/sales_warehouse/project`` - see its PROVENANCE.md) and the same project's
synthetic data generator (``fixtures/sales_source``, copied verbatim).

Steps (all deterministic, so every machine builds identical data):

1. generate the synthetic UCI-schema data (seed 42, ~68k lines) -> monthly landing parquet;
2. ``dbt deps`` (needs network; done once here, never inside the sandbox);
3. replay history the way a daily pipeline accumulates it (the sales project's ``replay``):
   load the 19 batches up to 2011-06 one at a time, refreshing silver and the SCD2 snapshot
   after each; one full ``dbt build``; then the last 6 batches one per full ``dbt build``
   (the fact table grows incrementally). Ingestion uses a fixed clock
   (``2012-01-01 00:00`` + one hour per batch) instead of the wall clock;
4. record the final build's node statuses in ``base_build.json``.

The replay gives the warehouse real SCD2 history (customers with several versions) and an
incrementally built fact, which several suite tasks depend on.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from dbt_agent.util import atomic_write_json, force_rmtree

REPO_ROOT = Path(__file__).resolve().parents[3]
PROJECT_SRC = REPO_ROOT / "fixtures" / "sales_warehouse" / "project"
DEFAULT_BASE = REPO_ROOT / "fixtures" / "build" / "sales_base"
DB_FILE = "warehouse.duckdb"
INGEST_EPOCH = datetime(2012, 1, 1, 0, 0, 0)
HISTORY_BATCHES = 19  # 2009-12 .. 2011-06, as in the sales project's replay

PROJECT_NOTES = """\
- dbt project `sales_analytics` on DuckDB; the warehouse file is `warehouse.duckdb`.
- Schemas: `bronze` (raw landed source rows), `silver` (staging + intermediate models),
  `gold` (star schema, marts, ML features), `reference` (seeds), `snapshots` (SCD2).
- Layers: models/staging -> models/intermediate -> models/marts/{core,analytics,ml}.
- Singular tests in tests/ include reconciliation checks (fact vs silver, mart vs fact).
"""


class _FixedClock:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> datetime:
        value = INGEST_EPOCH + timedelta(hours=self.calls)
        self.calls += 1
        return value


def _dbt(project: Path, *args: str, timeout: float = 1800) -> subprocess.CompletedProcess:
    exe_dir = Path(sys.executable).parent
    dbt = next((str(exe_dir / n) for n in ("dbt", "dbt.exe") if (exe_dir / n).exists()), "dbt")
    env = {
        **os.environ,
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "False",
        "DO_NOT_TRACK": "1",
        "DBT_PROFILES_DIR": str(project),
    }
    return subprocess.run(
        [dbt, *args],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def build_base(
    dest: Path = DEFAULT_BASE, *, force: bool = False, packages_from: Path | None = None
) -> dict[str, Any]:
    """Create the base fixture at ``dest`` (idempotent unless ``force``)."""
    marker = dest / "base_build.json"
    if marker.exists() and not force:
        return json.loads(marker.read_text(encoding="utf-8"))
    from dbt_agent.fixtures.sales_source import landing, load_bronze, synthetic

    force_rmtree(dest)
    shutil.copytree(PROJECT_SRC, dest)
    if packages_from is not None:
        shutil.copytree(packages_from, dest / "dbt_packages")
    else:
        deps = _dbt(dest, "deps")
        if deps.returncode != 0:
            raise RuntimeError("dbt deps failed:\n" + deps.stdout[-2000:])

    clock = _FixedClock()
    original_clock = load_bronze._utc_now_naive
    load_bronze._utc_now_naive = clock
    replay: list[dict[str, Any]] = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            landing_dir = Path(tmp) / "landing"
            frame = synthetic.generate()
            files = landing.write_monthly_partitions(frame, landing_dir)
            batches = load_bronze.available_batches(landing_dir)

            def step(batch: str | None, *args: str) -> None:
                if batch is not None:
                    load_bronze.load_batches(dest / DB_FILE, landing_dir, only=[batch])
                proc = _dbt(dest, *args)
                if proc.returncode != 0:
                    raise RuntimeError(
                        f"replay step {batch} {args} failed:\n" + proc.stdout[-3000:]
                    )
                replay.append({"batch": batch, "command": " ".join(args)})

            for batch in batches[:HISTORY_BATCHES]:
                step(
                    batch,
                    "build",
                    "--select",
                    "+snap_customer_state",
                    "--indirect-selection",
                    "cautious",
                )
            step(None, "build")
            for batch in batches[HISTORY_BATCHES:]:
                step(batch, "build")
    finally:
        load_bronze._utc_now_naive = original_clock

    results = json.loads((dest / "target" / "run_results.json").read_text(encoding="utf-8"))
    statuses = {r["unique_id"]: r["status"] for r in results["results"]}
    for leftover in ("target", "logs"):
        force_rmtree(dest / leftover)
    values = list(statuses.values())
    summary = {
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "landing_rows": len(frame),
        "landing_files": len(files),
        "batches": len(batches),
        "replay_steps": len(replay),
        "nodes": len(statuses),
        "status_counts": {s: values.count(s) for s in sorted(set(values))},
        "statuses": statuses,
    }
    atomic_write_json(marker, summary)
    return summary


def main() -> int:  # python -m dbt_agent.fixtures.sales [--force]
    summary = build_base(force="--force" in sys.argv)
    print(json.dumps({k: v for k, v in summary.items() if k != "statuses"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
