"""Work directories: task templates and per-run copies.

A *template* is a ready-to-work snapshot of a project: the dbt project, its installed
packages (``dbt_packages/``, so no network is needed), the DuckDB file, a git repository
with one baseline commit, and ``.agent/baseline_state/manifest.json`` for
``state:modified`` selection. Every run, verification and grading step works on its own
fresh copy of the template, so they never share state.

Host-side operations here only touch trusted content (the template before any agent
action). Anything inside a directory the agent has used is read through the sandbox.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from dbt_agent.sandbox.base import Sandbox, SandboxError
from dbt_agent.util import force_rmtree

AGENT_DIR = ".agent"
BASELINE_STATE = ".agent/baseline_state"
GIT = [
    "git",
    "-c",
    "user.name=dbt-background-agent",
    "-c",
    "user.email=agent@dbt-background-agent.invalid",
    "-c",
    "core.autocrlf=false",
    "-c",
    "init.defaultBranch=main",
]
DEFAULT_IGNORE = [
    "target/",
    "logs/",
    "dbt_packages/",
    "*.duckdb",
    "*.duckdb.wal",
    ".user.yml",
    "__pycache__/",
]


class WorkspaceError(RuntimeError):
    pass


def copy_template(src: Path, dst: Path) -> Path:
    """Copy a template (symlinks are copied as links, never followed)."""
    force_rmtree(dst)
    shutil.copytree(
        src, dst, symlinks=True, ignore=shutil.ignore_patterns("target", "logs", "__pycache__")
    )
    return dst


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run([*GIT, *args], cwd=root, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise WorkspaceError(f"git {args[0]} failed: {proc.stderr.strip()[:500]}")
    return proc.stdout.strip()


def ensure_gitignore(root: Path, extra: list[str] | None = None) -> None:
    path = root / ".gitignore"
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    wanted = [p for p in DEFAULT_IGNORE + (extra or []) if p not in existing]
    if wanted:
        lines = [*existing, *wanted]
        path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8", newline="\n")


def init_git_baseline(root: Path, message: str = "Starting point") -> str:
    """One commit holding the starting project; ``.agent/`` is excluded from git."""
    force_rmtree(root / ".git")
    ensure_gitignore(root)
    _git(root, "init", "-q")
    exclude = root / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text(f"{AGENT_DIR}/\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "--allow-empty", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def write_baseline_manifest(sandbox: Sandbox, timeout: float = 600) -> dict:
    """``dbt parse`` into ``.agent/baseline_state`` (the reference for state:modified)."""
    response = sandbox.call(
        "dbt_raw", {"argv": ["parse", "--target-path", BASELINE_STATE]}, timeout=timeout
    )
    if not response.ok or response.data.get("exit_code") != 0:
        excerpt = response.data.get("error_excerpt") or response.error or response.output
        raise SandboxError(f"baseline dbt parse failed: {str(excerpt)[-1500:]}")
    return response.data


def remove_databases(root: Path) -> int:
    """Delete DuckDB files from a finished run's copy (they are large and reproducible).

    The directory was writable by sandboxed code, so the walk never follows symlinks
    (a link to a host directory must not lead the host to delete files there).
    """
    import os

    removed = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if not (Path(dirpath) / d).is_symlink()]
        for name in filenames:
            path = Path(dirpath) / name
            if name.endswith((".duckdb", ".duckdb.wal")) and not path.is_symlink():
                path.unlink()
                removed += 1
    return removed
