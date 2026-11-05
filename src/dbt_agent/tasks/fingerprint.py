"""Order-independent table fingerprints (row count + sum of row hashes).

Used for the "incremental = full refresh" check and for collateral-change detection.
Floating-point columns are rounded to 6 decimals first, because parallel aggregation can
change the last bits of a double between two otherwise identical builds.
"""

from __future__ import annotations

from typing import Any

from dbt_agent.sandbox.base import Sandbox

FLOAT_TYPES = {"DOUBLE", "FLOAT", "REAL", "FLOAT4", "FLOAT8"}


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def column_listing_sql(schemas: list[str]) -> str:
    quoted = ", ".join("'" + s.replace("'", "''") + "'" for s in schemas)
    return (
        "select c.table_schema, c.table_name, c.column_name, c.data_type "
        "from information_schema.columns as c "
        "join information_schema.tables as t "
        "on c.table_schema = t.table_schema and c.table_name = t.table_name "
        f"where t.table_type = 'BASE TABLE' and c.table_schema in ({quoted}) "
        "order by 1, 2, 3"
    )


def fingerprint_sql(schema: str, table: str, columns: list[tuple[str, str]]) -> str:
    exprs = []
    for name, data_type in sorted(columns):
        col = _quote(name)
        exprs.append(f"round({col}, 6)" if data_type.upper() in FLOAT_TYPES else col)
    row_hash = f"hash({', '.join(exprs)})" if exprs else "0"
    return (
        f"select count(*) as n, coalesce(sum({row_hash})::hugeint, 0)::varchar as h "
        f"from {_quote(schema)}.{_quote(table)}"
    )


def table_fingerprints(
    sandbox: Sandbox,
    schemas: list[str],
    only_columns: dict[str, list[str]] | None = None,
    exclude: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """{"schema.table": {"rows": n, "hash": "..."}} computed inside the sandbox.

    ``only_columns`` restricts a table to a column set (the reference's columns), so extra
    columns added by an agent do not count as a change; missing ones do.
    """
    listing = sandbox.require(
        "sql_exec", {"statements": [column_listing_sql(schemas)], "read_only": True}
    ).data["results"][0]["rows"]
    tables: dict[str, list[tuple[str, str]]] = {}
    for schema, table, column, data_type in listing:
        tables.setdefault(f"{schema}.{table}", []).append((column, data_type))
    out: dict[str, Any] = {}
    names = sorted(tables)
    statements = []
    for key in names:
        schema, table = key.split(".", 1)
        cols = tables[key]
        if only_columns and key in only_columns:
            wanted = set(only_columns[key])
            present = {c for c, _ in cols}
            if not wanted <= present:
                out[key] = {"rows": None, "hash": None, "missing_columns": sorted(wanted - present)}
                continue
            cols = [(c, t) for c, t in cols if c in wanted]
        if exclude and key in exclude:
            cols = [(c, t) for c, t in cols if c not in set(exclude[key])]
        statements.append((key, fingerprint_sql(schema, table, cols), [c for c, _ in cols]))
    if statements:
        results = sandbox.require(
            "sql_exec", {"statements": [s for _, s, _ in statements], "read_only": True}
        ).data["results"]
        for (key, _, cols), result in zip(statements, results, strict=True):
            rows, digest = result["rows"][0]
            out[key] = {"rows": rows, "hash": digest, "columns": cols}
    return out
