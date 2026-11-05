# Copied verbatim from 05_sales-analytics-platform/src/sales_analytics/ingestion/load_bronze.py on 2026-10-14
# (provenance: fixtures/sales_warehouse/PROVENANCE.md). Only the import path was changed.
"""Load landing parquet files into the bronze layer of the DuckDB warehouse.

Bronze is append-only and immutable: rows are stored exactly as landed plus ingestion
metadata (``_batch_id``, ``_source_file``, ``_ingested_at``). Loading is idempotent at the
batch level - a ``bronze._load_audit`` table records which monthly batches are loaded, so
re-running the loader is a no-op unless ``force=True`` (which atomically replaces a batch).

``_ingested_at`` is the incremental watermark used by ``fct_order_lines``; it is UTC and
strictly increasing per batch so that ``max(_ingested_at)`` comparisons are unambiguous
even on platforms with coarse clocks.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb

from dbt_agent.fixtures.sales_source.landing import batch_id_from_path, list_landing_files

log = logging.getLogger(__name__)

BRONZE_SCHEMA = "bronze"
BRONZE_TABLE = f"{BRONZE_SCHEMA}.online_retail_lines"
AUDIT_TABLE = f"{BRONZE_SCHEMA}._load_audit"

_DDL = f"""
create schema if not exists {BRONZE_SCHEMA};

create table if not exists {BRONZE_TABLE} (
    invoice             varchar,
    stock_code          varchar,
    description         varchar,
    quantity            bigint,
    invoice_date        timestamp,
    price               double,
    customer_id         varchar,
    country             varchar,
    _source_sheet       varchar,
    _source_row_number  bigint,
    _batch_id           varchar   not null,
    _source_file        varchar   not null,
    _ingested_at        timestamp not null
);

create table if not exists {AUDIT_TABLE} (
    batch_id     varchar primary key,
    source_file  varchar   not null,
    row_count    bigint    not null,
    ingested_at  timestamp not null
);
"""


@dataclass
class LoadSummary:
    loaded_batches: list[str] = field(default_factory=list)
    skipped_batches: list[str] = field(default_factory=list)
    rows_loaded: int = 0
    bronze_rows_total: int = 0
    seconds: float = 0.0


def ensure_bronze(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(_DDL)


def _utc_now_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _next_ingestion_ts(con: duckdb.DuckDBPyConnection) -> datetime:
    """``now()`` but strictly greater than every previously recorded ingestion timestamp."""
    previous = con.execute(f"select max(ingested_at) from {AUDIT_TABLE}").fetchone()[0]
    now = _utc_now_naive()
    if previous is not None and now <= previous:
        now = previous + timedelta(microseconds=1)
    return now


def loaded_batches(con: duckdb.DuckDBPyConnection) -> set[str]:
    return {r[0] for r in con.execute(f"select batch_id from {AUDIT_TABLE}").fetchall()}


def load_batches(
    warehouse_path: Path,
    landing_dir: Path,
    until: str | None = None,
    only: list[str] | None = None,
    force: bool = False,
) -> LoadSummary:
    """Load landing batches into bronze.

    Args:
        warehouse_path: DuckDB file (created if missing).
        landing_dir: directory with ``online_retail_YYYY-MM.parquet`` files.
        until: load only batches ``<= until`` (``YYYY-MM``) - used to replay history.
        only: load only these batch ids.
        force: reload batches that were already loaded (delete + insert, atomically).
    """
    start = time.perf_counter()
    files = list_landing_files(landing_dir)
    if not files:
        raise FileNotFoundError(
            f"no landing files in {landing_dir}; run `download` or `synth` first"
        )

    warehouse_path.parent.mkdir(parents=True, exist_ok=True)
    summary = LoadSummary()
    with duckdb.connect(str(warehouse_path)) as con:
        ensure_bronze(con)
        already = loaded_batches(con)
        for path in files:
            batch_id = batch_id_from_path(path)
            if until is not None and batch_id > until:
                continue
            if only is not None and batch_id not in only:
                continue
            if batch_id in already and not force:
                summary.skipped_batches.append(batch_id)
                continue

            ingested_at = _next_ingestion_ts(con)
            con.execute("begin transaction")
            try:
                con.execute(f"delete from {BRONZE_TABLE} where _batch_id = ?", [batch_id])
                con.execute(f"delete from {AUDIT_TABLE} where batch_id = ?", [batch_id])
                con.execute(
                    f"""
                    insert into {BRONZE_TABLE}
                    select
                        invoice, stock_code, description, quantity, invoice_date, price,
                        customer_id, country, _source_sheet, _source_row_number,
                        ? as _batch_id, ? as _source_file, ? as _ingested_at
                    from read_parquet(?)
                    """,
                    [batch_id, path.name, ingested_at, str(path)],
                )
                rows = con.execute(
                    f"select count(*) from {BRONZE_TABLE} where _batch_id = ?", [batch_id]
                ).fetchone()[0]
                con.execute(
                    f"insert into {AUDIT_TABLE} values (?, ?, ?, ?)",
                    [batch_id, path.name, rows, ingested_at],
                )
                con.execute("commit")
            except Exception:
                con.execute("rollback")
                raise
            summary.loaded_batches.append(batch_id)
            summary.rows_loaded += rows
            log.info("loaded batch %s (%s rows)", batch_id, rows)

        (summary.bronze_rows_total,) = con.execute(
            f"select count(*) from {BRONZE_TABLE}"
        ).fetchone()
    summary.seconds = round(time.perf_counter() - start, 2)
    return summary


def available_batches(landing_dir: Path) -> list[str]:
    return [batch_id_from_path(p) for p in list_landing_files(landing_dir)]
