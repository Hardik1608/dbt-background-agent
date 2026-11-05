# Copied verbatim from 05_sales-analytics-platform/src/sales_analytics/ingestion/landing.py on 2026-10-14
# (provenance: fixtures/sales_warehouse/PROVENANCE.md). Only the import path was changed.
"""Landing zone contract shared by the UCI downloader and the synthetic generator.

The landing zone imitates what an EL tool (Fivetran/Airbyte) would deliver: one parquet
file per invoice month with an enforced, typed schema and snake_case columns. Nothing is
cleaned here - whitespace, casing, duplicates, cancellations and nulls are preserved so
the silver layer (dbt staging) owns every cleaning rule and can be tested.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

LANDING_FILE_PREFIX = "online_retail_"

#: Original UCI column -> landing column.
SOURCE_COLUMN_MAP: dict[str, str] = {
    "Invoice": "invoice",
    "StockCode": "stock_code",
    "Description": "description",
    "Quantity": "quantity",
    "InvoiceDate": "invoice_date",
    "Price": "price",
    "Customer ID": "customer_id",
    "Country": "country",
}

LANDING_SCHEMA = pa.schema(
    [
        ("invoice", pa.string()),
        ("stock_code", pa.string()),
        ("description", pa.string()),
        ("quantity", pa.int64()),
        ("invoice_date", pa.timestamp("us")),
        ("price", pa.float64()),
        ("customer_id", pa.string()),
        ("country", pa.string()),
        # lineage: which source sheet/row the line came from (not business data)
        ("_source_sheet", pa.string()),
        ("_source_row_number", pa.int64()),
    ]
)
LANDING_COLUMNS: list[str] = LANDING_SCHEMA.names


def normalize_code(value: object) -> str | None:
    """Excel stores codes as numbers when they look numeric (489434 -> 489434.0).

    Convert integral floats back to their integer string and keep strings untouched, so
    ``Invoice``/``StockCode``/``Customer ID`` are consistently text. No trimming/casing:
    that is a silver-layer rule.
    """
    if value is None:
        return None
    if isinstance(value, float):
        if np.isnan(value):
            return None
        if value.is_integer():
            return str(int(value))
        return repr(value)
    if isinstance(value, (int, np.integer)):
        return str(int(value))
    return str(value)


def to_landing_table(df: pd.DataFrame) -> pa.Table:
    """Coerce a frame with landing columns into the enforced Arrow schema."""
    missing = [c for c in LANDING_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"landing frame is missing columns: {missing}")
    out = df[LANDING_COLUMNS].copy()
    for col in ("invoice", "stock_code", "description", "customer_id", "country", "_source_sheet"):
        out[col] = out[col].map(normalize_code).astype("object")
    out["quantity"] = out["quantity"].astype("int64")
    out["price"] = out["price"].astype("float64")
    out["_source_row_number"] = out["_source_row_number"].astype("int64")
    out["invoice_date"] = pd.to_datetime(out["invoice_date"]).astype("datetime64[us]")
    return pa.Table.from_pandas(out, schema=LANDING_SCHEMA, preserve_index=False)


def write_monthly_partitions(df: pd.DataFrame, landing_dir: Path) -> list[Path]:
    """Write one parquet file per invoice month (``online_retail_YYYY-MM.parquet``).

    Existing landing files are replaced so the landing zone always mirrors the source.
    """
    landing_dir.mkdir(parents=True, exist_ok=True)
    for stale in landing_dir.glob(f"{LANDING_FILE_PREFIX}*.parquet"):
        stale.unlink()

    months = pd.to_datetime(df["invoice_date"]).dt.strftime("%Y-%m")
    written: list[Path] = []
    for month in sorted(months.unique()):
        part = df.loc[months == month]
        path = landing_dir / f"{LANDING_FILE_PREFIX}{month}.parquet"
        pq.write_table(to_landing_table(part), path, compression="zstd")
        written.append(path)
    return written


def batch_id_from_path(path: Path) -> str:
    """``online_retail_2010-12.parquet`` -> ``2010-12``."""
    stem = path.stem
    if not stem.startswith(LANDING_FILE_PREFIX):
        raise ValueError(f"not a landing file: {path.name}")
    return stem[len(LANDING_FILE_PREFIX) :]


def list_landing_files(landing_dir: Path) -> list[Path]:
    """Landing files sorted chronologically (batch ids sort lexicographically)."""
    return sorted(landing_dir.glob(f"{LANDING_FILE_PREFIX}*.parquet"), key=batch_id_from_path)
