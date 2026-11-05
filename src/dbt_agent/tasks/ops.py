"""Apply scripted ops to a *trusted* copy (a template before any agent touched it).

Every op fails loudly when its precondition does not hold (e.g. the text to replace is not
there exactly ``count`` times), so a task can never silently stop testing what it claims.
"""

from __future__ import annotations

from pathlib import Path

from dbt_agent.tasks.model import Op


class OpError(RuntimeError):
    pass


def _safe_path(root: Path, rel: str) -> Path:
    path = (root / rel).resolve()
    if root.resolve() not in path.parents:
        raise OpError(f"op path escapes the project: {rel}")
    return path


def apply_file_op(root: Path, op: Op) -> None:
    path = _safe_path(root, op.path or "")
    if op.op == "replace":
        text = path.read_text(encoding="utf-8")
        found = text.count(op.old or "")
        if found != op.count:
            raise OpError(
                f"{op.path}: expected {op.count} occurrence(s) of {op.old!r}, found {found}"
            )
        path.write_text(text.replace(op.old or "", op.new or ""), encoding="utf-8", newline="\n")
    elif op.op == "write":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(op.content or "", encoding="utf-8", newline="\n")
    elif op.op == "append":
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        path.write_text(text + (op.content or ""), encoding="utf-8", newline="\n")
    elif op.op == "delete":
        if not path.exists():
            raise OpError(f"cannot delete missing file {op.path}")
        path.unlink()
    else:
        raise OpError(f"not a file op: {op.op}")


def apply_ops(root: Path, ops: list[Op], db_file: str = "warehouse.duckdb") -> None:
    sql = [op.sql for op in ops if op.op == "sql"]
    for op in ops:
        if op.op != "sql":
            apply_file_op(root, op)
    if sql:
        import duckdb

        con = duckdb.connect(str(root / db_file))
        try:
            for statement in sql:
                con.execute(statement)
            con.execute("checkpoint")
        finally:
            con.close()
