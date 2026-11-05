#!/usr/bin/env python3
"""In-sandbox tool executor for dbt-background-agent.

This file runs INSIDE the sandbox: in the Docker container it is bind-mounted read-only at
``/opt/toolbox/toolbox.py``; under ``LocalSandbox`` (unit tests only) it runs as a plain
subprocess. It reads one JSON request from stdin and writes one JSON response to stdout:

    request  {"tool": "read_file", "args": {...}, "ctx": {"root": "/work", ...}}
    response {"ok": true, "output": "...", "error": null, "data": {...}, "security": [...]}

Why every tool runs here and not on the host: dbt executes code the model writes (Jinja,
SQL, Python models). Anything that code creates - including a symlink from the work
directory to a host file - must only ever be resolved inside the sandbox. The host never
opens files in the work directory; it only talks to this script.

Only the standard library and ``duckdb`` are imported, so the script works in any image
that has Python and dbt-duckdb. Model-facing tools: list_files, read_file, write_file,
edit_file, delete_file, search_files, apply_patch, run_dbt, query_duckdb, git_diff.
Harness-only tools (never exposed to the model): snapshot, restore, git_patch, git_apply,
git_init, git_commit, dbt_raw, dbt_ls, sql_exec, manifest_summary, sqlfluff, tree_hash, ping.
"""

from __future__ import annotations

import contextlib
import difflib
import fnmatch
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

# ----------------------------------------------------------------------------- constants
DEFAULT_MAX_CHARS = 8000
SKIP_DIRS = {
    ".git",
    "target",
    "logs",
    "dbt_packages",
    ".agent",
    "__pycache__",
    ".venv",
    "node_modules",
    ".pytest_cache",
    ".ruff_cache",
}
PROTECTED_DIRS = {".git", ".agent", "target", "logs", "dbt_packages"}
PROTECTED_SUFFIXES = (".duckdb", ".duckdb.wal", ".db", ".sqlite")
MAX_WRITE_BYTES = 200_000
DBT_ALLOWED = {"build", "run", "test", "compile", "parse", "ls", "seed", "snapshot", "show"}
DBT_BLOCKED_HINTS = {
    "deps": "Packages are already installed in dbt_packages/ and the sandbox has no network.",
    "clean": "`dbt clean` would delete the installed packages (dbt_packages/), which cannot be "
    "re-downloaded without network.",
    "debug": "The connection is already configured; use run_dbt('parse') to check the project.",
    "run-operation": "Arbitrary macros are not available to the agent.",
    "docs": "Docs generation is not needed; read the YAML files instead.",
    "source": "Source freshness is checked by CI, not by the agent.",
}
URL_RE = re.compile(r"\b(?:https?|s3|gs|gcs|az|abfss?|hf|ftp)://", re.IGNORECASE)
NETWORK_CODE_RE = re.compile(
    r"\b(?:import\s+(?:requests|urllib|socket|http\.client|httpx|aiohttp|paramiko|ftplib)"
    r"|from\s+(?:requests|urllib|socket|http|httpx)\b|INSTALL\s+\w+|LOAD\s+httpfs)",
    re.IGNORECASE,
)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
NETWORK_ERROR_RE = re.compile(
    r"Could not resolve host|Name or service not known|Temporary failure in name resolution|"
    r"Network is unreachable|Connection refused|Failed to download extension|"
    r"HTTPS? (?:GET|HEAD|error)|IO Error: .{0,80}https?://",
    re.IGNORECASE,
)
GIT_BASE = [
    "git",
    "-c",
    "safe.directory=*",
    "-c",
    "core.autocrlf=false",
    "-c",
    "core.quotepath=false",
    "-c",
    "user.name=dbt-background-agent",
    "-c",
    "user.email=agent@dbt-background-agent.invalid",
    # code dbt runs can write .git/config or hooks in the work directory; the harness's own
    # git calls must not execute them (the verifier re-checks the patch text anyway)
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
]


class ToolError(Exception):
    """An expected failure: the message is shown to the model, with a hint."""

    def __init__(self, message: str, hint: str | None = None, security: str | None = None):
        super().__init__(message)
        self.hint = hint
        self.security = security


# ----------------------------------------------------------------------------- helpers
def truncate(text: str, max_chars: int) -> str:
    """Keep the head and the tail (errors are usually at the end) of a long output."""
    if len(text) <= max_chars:
        return text
    head = int(max_chars * 0.45)
    tail = max_chars - head
    omitted = text[head : len(text) - tail]
    n_lines = omitted.count("\n")
    return (
        f"{text[:head]}\n... [{len(omitted)} characters / {n_lines} lines truncated] ...\n"
        f"{text[len(text) - tail :]}"
    )


def root_of(ctx: dict[str, Any]) -> Path:
    return Path(os.path.realpath(ctx["root"]))


def rel_display(root: Path, path: Path) -> str:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return str(path)
    return rel.as_posix() or "."


def resolve(ctx: dict[str, Any], rel: str | None, *, for_write: bool = False) -> Path:
    """Resolve a model-supplied path inside the work directory, or raise.

    realpath() follows symlinks, so a link that points outside the work directory is
    caught as well as '../' and absolute paths.
    """
    root = root_of(ctx)
    raw = (rel or ".").strip()
    if "\x00" in raw:
        raise ToolError("Path contains a NUL byte.", security="path_escape_blocked")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / raw
    real = Path(os.path.realpath(candidate))
    if real != root and root not in real.parents:
        raise ToolError(
            f"Path '{raw}' is outside the project directory.",
            hint="Use paths relative to the project root, e.g. 'models/staging/x.sql'.",
            security="path_escape_blocked",
        )
    if for_write:
        parts = real.relative_to(root).parts
        if parts and parts[0] in PROTECTED_DIRS:
            raise ToolError(
                f"'{rel_display(root, real)}' is in a protected directory ({parts[0]}/).",
                hint="Only edit project source files (models, tests, macros, seeds, snapshots, "
                "YAML). Build artefacts and installed packages are read-only.",
                security="protected_path_blocked",
            )
        if real.name.lower().endswith(PROTECTED_SUFFIXES):
            raise ToolError(
                f"'{rel_display(root, real)}' is a database file and cannot be written.",
                hint="Change data only through dbt models; inspect it with query_duckdb.",
                security="protected_path_blocked",
            )
        for extra in ctx.get("read_only_globs") or []:
            if fnmatch.fnmatch(real.relative_to(root).as_posix(), extra):
                raise ToolError(
                    f"'{rel_display(root, real)}' is read-only for this task.",
                    security="protected_path_blocked",
                )
    return real


def is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    with contextlib.suppress(OSError):
        proc.kill()


def run_proc(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None,
    timeout: float,
    stdin: str | None = None,
) -> tuple[int, str, str, bool, float]:
    """Run a command with a hard timeout that kills the whole process tree."""
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
    started = time.monotonic()
    proc = subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **kwargs,
    )
    try:
        out, err = proc.communicate(
            input=stdin.encode() if stdin is not None else None, timeout=timeout
        )
        timed_out = False
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        out, err = proc.communicate()
        timed_out = True
    return (
        proc.returncode if not timed_out else -9,
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
        timed_out,
        time.monotonic() - started,
    )


def git(
    ctx: dict[str, Any],
    args: list[str],
    *,
    index: str | None = None,
    timeout: float = 60,
    stdin: str | None = None,
) -> tuple[int, str, str]:
    root = root_of(ctx)
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if index:
        env["GIT_INDEX_FILE"] = str(root / ".git" / index)
    code, out, err, timed_out, _ = run_proc(
        [*GIT_BASE, *args], cwd=root, env=env, timeout=timeout, stdin=stdin
    )
    if timed_out:
        raise ToolError(f"git {' '.join(args[:2])} timed out")
    return code, out, err


def dbt_executable(ctx: dict[str, Any]) -> str:
    if ctx.get("dbt_bin"):
        return ctx["dbt_bin"]
    exe_dir = Path(sys.executable).parent
    for name in ("dbt", "dbt.exe"):
        if (exe_dir / name).exists():
            return str(exe_dir / name)
    found = shutil.which("dbt")
    if not found:
        raise ToolError("dbt is not installed in the sandbox.")
    return found


def dbt_env(ctx: dict[str, Any]) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "DBT_SEND_ANONYMOUS_USAGE_STATS": "False",
            "DO_NOT_TRACK": "1",
            "DBT_VERSION_CHECK": "False",
            "DBT_USE_COLORS": "False",
            "DBT_PROFILES_DIR": str(root_of(ctx) / ctx.get("profiles_subdir", ".")),
            "PYTHONUNBUFFERED": "1",
        }
    )
    for key, value in (ctx.get("env") or {}).items():
        env[str(key)] = str(value)
    return env


def db_path(ctx: dict[str, Any]) -> Path:
    raw = ctx.get("db_path")
    if not raw:
        raise ToolError("No DuckDB database is configured for this task.")
    path = Path(raw)
    return path if path.is_absolute() else root_of(ctx) / path


def security_scan(text: str) -> list[dict[str, str]]:
    events = []
    if URL_RE.search(text or ""):
        events.append({"kind": "egress_reference", "detail": URL_RE.search(text).group(0)})
    match = NETWORK_CODE_RE.search(text or "")
    if match:
        events.append({"kind": "network_code_reference", "detail": match.group(0)[:60]})
    return events


# ----------------------------------------------------------------------------- model tools
def tool_list_files(ctx: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    root = root_of(ctx)
    base = resolve(ctx, args.get("path") or ".")
    if not base.exists():
        raise ToolError(
            f"'{args.get('path')}' does not exist.",
            hint="Call list_files() with no arguments to see the project layout.",
        )
    if base.is_file():
        return {"output": f"{rel_display(root, base)} ({base.stat().st_size} bytes)"}
    pattern = args.get("pattern")
    max_depth = int(args.get("max_depth") or 6)
    include_hidden = bool(args.get("include_build_dirs"))
    entries: list[str] = []
    limit = 400
    base_depth = len(base.relative_to(root).parts)
    for dirpath, dirnames, filenames in os.walk(base):
        current = Path(dirpath)
        depth = len(current.relative_to(root).parts) - base_depth
        dirnames[:] = sorted(
            d
            for d in dirnames
            if (include_hidden or d not in SKIP_DIRS) and not (current / d).is_symlink()
        )
        if depth >= max_depth:
            dirnames[:] = []
        for name in sorted(filenames):
            rel = (current / name).relative_to(root).as_posix()
            if pattern and not (fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel, pattern)):
                continue
            try:
                size = (current / name).lstat().st_size
            except OSError:
                size = 0
            entries.append(f"{rel}  ({size:,} B)")
            if len(entries) >= limit:
                break
        if len(entries) >= limit:
            break
    header = f"{len(entries)} files under {rel_display(root, base)}/"
    if not include_hidden:
        header += " (build/vendor dirs hidden: target, logs, dbt_packages, .git)"
    if len(entries) >= limit:
        header += f" - first {limit} shown; pass path= or pattern= to narrow"
    if not entries:
        return {"output": header + "\n(no matching files)"}
    return {"output": header + "\n" + "\n".join(entries)}


def tool_read_file(ctx: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    root = root_of(ctx)
    path = resolve(ctx, args.get("path"))
    if not path.exists():
        parent = path.parent
        near = sorted(p.name for p in parent.iterdir())[:30] if parent.is_dir() else []
        raise ToolError(
            f"File not found: {rel_display(root, path)}",
            hint=(
                f"Files in {rel_display(root, parent)}/: {', '.join(near)}"
                if near
                else "Use list_files to find the right path."
            ),
        )
    if path.is_dir():
        raise ToolError(
            f"'{rel_display(root, path)}' is a directory.",
            hint=f"Use list_files(path='{rel_display(root, path)}').",
        )
    data = path.read_bytes()
    if is_binary(data):
        raise ToolError(
            f"'{rel_display(root, path)}' is a binary file.",
            hint="Inspect database contents with query_duckdb instead.",
        )
    text = data.decode("utf-8", "replace")
    lines = text.splitlines()
    start = max(1, int(args.get("start_line") or 1))
    count = max(1, min(int(args.get("num_lines") or 400), 1000))
    end = min(len(lines), start + count - 1)
    width = len(str(end))
    body = []
    for number in range(start, end + 1):
        line = lines[number - 1]
        if len(line) > 600:
            line = line[:600] + " ...[line truncated]"
        body.append(f"{number:>{width}}\t{line}")  # cat -n style: the tab is not file text
    header = f"{rel_display(root, path)} (lines {start}-{end} of {len(lines)})"
    if not lines:
        header = f"{rel_display(root, path)} (empty file)"
    out = header + "\n" + "\n".join(body)
    if end < len(lines):
        out += f"\n... {len(lines) - end} more lines. Call read_file with start_line={end + 1}."
    return {"output": out, "data": {"lines": len(lines)}}


def _check_write_content(content: str) -> None:
    if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
        raise ToolError(
            f"Content is larger than {MAX_WRITE_BYTES} bytes.",
            hint="Split the change across several smaller files.",
        )


def _read_lf(path: Path) -> tuple[str, str]:
    """File text with LF line ends, plus the file's own line ending (CRLF if most lines are)."""
    raw = path.read_bytes().decode("utf-8")
    crlf = raw.count("\r\n")
    eol = "\r\n" if crlf and crlf * 2 >= raw.count("\n") else "\n"
    return raw.replace("\r\n", "\n"), eol


def _write_eol(path: Path, text_lf: str, eol: str) -> None:
    """Write LF text using the file's own line ending, so a one-line edit stays one line in
    the diff (rewriting a CRLF file with LF would show every line as changed)."""
    data = text_lf.replace("\n", eol) if eol != "\n" else text_lf
    path.write_bytes(data.encode("utf-8"))


def tool_write_file(ctx: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    root = root_of(ctx)
    if "content" not in args:
        raise ToolError("write_file needs 'path' and 'content'.")
    path = resolve(ctx, args.get("path"), for_write=True)
    content = str(args["content"]).replace("\r\n", "\n")
    _check_write_content(content)
    existed = path.exists()
    if existed and path.is_dir():
        raise ToolError(f"'{rel_display(root, path)}' is a directory.")
    path.parent.mkdir(parents=True, exist_ok=True)
    if content and not content.endswith("\n"):
        content += "\n"
    eol = _read_lf(path)[1] if existed else "\n"
    _write_eol(path, content, eol)
    n = content.count("\n")
    verb = "Overwrote" if existed else "Created"
    return {
        "output": f"{verb} {rel_display(root, path)} ({n} lines).",
        "data": {"path": rel_display(root, path), "mutated": True},
    }


def tool_search_files(ctx: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """grep: find a text (or regex) in project files, with file:line results."""
    root = root_of(ctx)
    pattern = str(args.get("pattern") or "")
    if not pattern:
        raise ToolError("pattern is empty.")
    flags = re.IGNORECASE if args.get("ignore_case") else 0
    try:
        regex = re.compile(pattern if args.get("regex") else re.escape(pattern), flags)
    except re.error as exc:
        raise ToolError(
            f"Invalid regex: {exc}", hint="Pass regex=false to search literally."
        ) from exc
    base = resolve(ctx, args.get("path") or ".")
    glob = args.get("glob")
    include_vendor = bool(args.get("include_packages"))
    matches: list[str] = []
    files_hit = set()
    limit = 150
    for dirpath, dirnames, filenames in os.walk(base):
        current = Path(dirpath)
        dirnames[:] = sorted(
            d
            for d in dirnames
            if (d not in SKIP_DIRS or (include_vendor and d == "dbt_packages"))
            and not (current / d).is_symlink()
        )
        for name in sorted(filenames):
            path = current / name
            rel = path.relative_to(root).as_posix()
            if glob and not (fnmatch.fnmatch(name, glob) or fnmatch.fnmatch(rel, glob)):
                continue
            if path.is_symlink() or name.endswith(PROTECTED_SUFFIXES):
                continue
            try:
                if path.stat().st_size > 1_000_000:
                    continue
                data = path.read_bytes()
            except OSError:
                continue
            if is_binary(data):
                continue
            for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                if regex.search(line):
                    files_hit.add(rel)
                    if len(matches) < limit:
                        matches.append(f"{rel}:{number}: {line.strip()[:200]}")
    if not matches:
        return {"output": f"No matches for {pattern!r}."}
    header = f"{len(matches)} match(es) in {len(files_hit)} file(s)"
    if len(matches) >= limit:
        header += f" (first {limit} shown; narrow with path= or glob=)"
    return {"output": header + "\n" + "\n".join(matches)}


def tool_delete_file(ctx: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    root = root_of(ctx)
    path = resolve(ctx, args.get("path"), for_write=True)
    if not path.exists():
        raise ToolError(
            f"File not found: {rel_display(root, path)}", hint="Use list_files to check the path."
        )
    if path.is_dir():
        raise ToolError(f"'{rel_display(root, path)}' is a directory; delete files one by one.")
    path.unlink()
    return {
        "output": f"Deleted {rel_display(root, path)}.",
        "data": {"path": rel_display(root, path), "mutated": True},
    }


def _closest_lines(text: str, needle: str) -> str:
    """Where did the model probably mean? Show the most similar line with its number."""
    lines = text.splitlines()
    first = next((ln for ln in needle.splitlines() if ln.strip()), needle).strip()
    if not lines or not first:
        return ""
    scored = sorted(
        (
            (difflib.SequenceMatcher(None, first, ln.strip()).ratio(), i)
            for i, ln in enumerate(lines)
        ),
        reverse=True,
    )[:1]
    ratio, idx = scored[0]
    if ratio < 0.5:
        return ""
    lo, hi = max(0, idx - 2), min(len(lines), idx + 3)
    snippet = "\n".join(f"{n + 1}\t{lines[n]}" for n in range(lo, hi))
    return f"The closest match is around line {idx + 1}:\n{snippet}"


LINE_PREFIX = re.compile(r"^\s*\d+(?:\t|\| ?)")


def _edit_variants(old: str, new: str) -> list[tuple[str, str, str]]:
    """Common copy mistakes from a numbered view, each tried only if it matches uniquely:
    the line numbers themselves, or one extra leading space on every line."""
    variants = []
    nonempty = [ln for ln in old.split("\n") if ln.strip()]

    def without_numbers(s: str) -> str:
        return "\n".join(LINE_PREFIX.sub("", ln, count=1) for ln in s.split("\n"))

    def dedent_one(s: str) -> str:
        return "\n".join(ln[1:] if ln.startswith(" ") else ln for ln in s.split("\n"))

    if nonempty and all(LINE_PREFIX.match(ln) for ln in nonempty):
        variants.append((without_numbers(old), without_numbers(new), "removing line numbers"))
    if nonempty and all(ln.startswith(" ") for ln in nonempty):
        variants.append((dedent_one(old), dedent_one(new), "removing one leading space per line"))
    return variants


def tool_edit_file(ctx: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    root = root_of(ctx)
    path = resolve(ctx, args.get("path"), for_write=True)
    old = args.get("old_string")
    new = args.get("new_string")
    if old is None or new is None:
        raise ToolError("edit_file needs 'path', 'old_string' and 'new_string'.")
    if not path.exists():
        raise ToolError(
            f"File not found: {rel_display(root, path)}",
            hint="Use write_file to create a new file.",
        )
    text, eol = _read_lf(path)  # matching is on LF text; the file keeps its own line ends
    if old == "":
        raise ToolError("old_string is empty.", hint="Use write_file to replace the whole file.")
    old, new = str(old).replace("\r\n", "\n"), str(new).replace("\r\n", "\n")
    if old == new:
        raise ToolError("old_string and new_string are identical; nothing to change.")
    count = text.count(old)
    note = ""
    if count == 0:
        # tolerate trailing-space differences only when the match becomes unique
        normalised = re.sub(r"[ \t]+\n", "\n", text)
        old_norm = re.sub(r"[ \t]+\n", "\n", old)
        if normalised.count(old_norm) == 1:
            text, old = normalised, old_norm
            count = 1
        else:
            for old_v, new_v, why in _edit_variants(old, new):
                if old_v.strip() and text.count(old_v) == 1:
                    old, new, count = old_v, new_v, 1
                    note = (
                        f" Matched after {why}: in read_file output the line number and the "
                        "tab after it are not part of the file."
                    )
                    break
            else:
                raise ToolError(
                    f"old_string was not found in {rel_display(root, path)}.",
                    hint=(
                        "Copy the exact current text (whitespace matters) from read_file, "
                        "without the line number and the tab after it. " + _closest_lines(text, old)
                    ).strip(),
                )
    replace_all = bool(args.get("replace_all"))
    if count > 1 and not replace_all:
        raise ToolError(
            f"old_string occurs {count} times in {rel_display(root, path)}.",
            hint="Include more surrounding lines to make it unique, or pass replace_all=true.",
        )
    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    _check_write_content(updated)
    _write_eol(path, updated, eol)
    return {
        "output": f"Edited {rel_display(root, path)}: replaced {count if replace_all else 1} "
        f"occurrence(s).{note}",
        "data": {"path": rel_display(root, path), "mutated": True},
    }


def tool_apply_patch(ctx: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    patch = str(args.get("patch") or "")
    if not patch.strip():
        raise ToolError("patch is empty.")
    if "120000" in patch and re.search(
        r"^(?:new|old) mode 120000|^new file mode 120000", patch, re.MULTILINE
    ):
        raise ToolError("Patches may not create symlinks.", security="symlink_blocked")
    for target in re.findall(r"^\+\+\+ b/(.+)$", patch, re.MULTILINE):
        resolve(ctx, target.strip(), for_write=True)
    code, out, err = git(
        ctx, ["apply", "--recount", "--whitespace=nowarn", "--check", "-"], stdin=patch
    )
    if code != 0:
        raise ToolError(
            "The patch does not apply: " + truncate((err or out).strip(), 1500),
            hint="Re-read the file and regenerate the hunk, or use edit_file for a small change.",
        )
    code, out, err = git(ctx, ["apply", "--recount", "--whitespace=nowarn", "-"], stdin=patch)
    if code != 0:
        raise ToolError("git apply failed: " + truncate((err or out).strip(), 1500))
    files = re.findall(r"^\+\+\+ b/(.+)$", patch, re.MULTILINE)
    return {
        "output": f"Patch applied to {len(files)} file(s): {', '.join(files)}",
        "data": {"mutated": True},
    }


def _summarise_run_results(
    root: Path, started: float, max_items: int = 15
) -> dict[str, Any] | None:
    path = root / "target" / "run_results.json"
    if not path.exists() or path.stat().st_mtime < started - 1:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    counts: dict[str, int] = {}
    failures: list[dict[str, Any]] = []
    skipped: list[str] = []
    passed: list[str] = []
    warned: list[str] = []
    for item in payload.get("results", []):
        status = str(item.get("status"))
        uid = str(item.get("unique_id"))
        counts[status] = counts.get(status, 0) + 1
        if status in {"error", "fail", "runtime error"}:
            failures.append(
                {
                    "unique_id": uid,
                    "status": status,
                    "message": (item.get("message") or "").strip(),
                    "failures": item.get("failures"),
                }
            )
        elif status == "skipped":
            skipped.append(uid)
        elif status in {"success", "pass"}:
            passed.append(uid)
        elif status == "warn":
            warned.append(uid)
    return {
        "warned": warned,
        "counts": counts,
        "failures": failures,
        "skipped": skipped,
        "passed": passed,
        "elapsed": payload.get("elapsed_time"),
    }


def _error_lines(stdout: str, limit: int = 40) -> str:
    lines = [ANSI_RE.sub("", ln) for ln in stdout.splitlines()]
    keep: list[str] = []
    for i, line in enumerate(lines):
        if re.search(r"error|exception|not found|could not|failed|warning:", line, re.I):
            keep.extend(lines[i : i + 4])
    if not keep:
        keep = lines[-limit:]
    deduped: list[str] = []
    for line in keep:
        if not deduped or deduped[-1] != line:
            deduped.append(line)
    return "\n".join(deduped[-limit:])


def _short(uid: str) -> str:
    parts = uid.split(".")
    return (
        parts[2]
        if len(parts) >= 3 and parts[0] in {"model", "seed", "snapshot"}
        else (".".join(parts[2:]) if len(parts) >= 3 else uid)
    )


def tool_run_dbt(ctx: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    root = root_of(ctx)
    command = str(args.get("command") or "build").strip().lower()
    if command == "list":
        command = "ls"
    if command not in DBT_ALLOWED:
        hint = DBT_BLOCKED_HINTS.get(
            command, f"Allowed commands: {', '.join(sorted(DBT_ALLOWED))}."
        )
        raise ToolError(
            f"dbt {command} is not available in this sandbox.",
            hint=hint,
            security="blocked_command" if command in {"deps", "run-operation"} else None,
        )
    argv = [dbt_executable(ctx), command]
    select = (args.get("select") or "").strip()
    exclude = (args.get("exclude") or "").strip()
    for flag, value in (("--select", select), ("--exclude", exclude)):
        if value:
            if re.search(r"[;&|`$<>]", value):
                raise ToolError(
                    f"Invalid characters in {flag[2:]}: {value!r}",
                    hint="Use dbt node selection syntax, e.g. 'my_model+' or 'tag:nightly'.",
                )
            argv += [flag, *value.split()]
    if "state:" in select + " " + exclude:
        state_dir = root / ctx.get("state_dir", ".agent/baseline_state")
        if not (state_dir / "manifest.json").exists():
            raise ToolError(
                "state: selectors need a baseline manifest, which this task lacks.",
                hint="Select models by name instead, e.g. select='my_model+'.",
            )
        argv += ["--state", str(state_dir)]
    if args.get("full_refresh") and command in {"build", "run", "seed"}:
        argv.append("--full-refresh")
    if command == "show":
        argv += ["--limit", str(min(int(args.get("limit") or 20), 100))]
    if command == "ls":
        argv += ["--output", "name", "--quiet"]
    timeout = float(ctx.get("dbt_timeout_s") or 300)
    started = time.time()
    code, out, err, timed_out, seconds = run_proc(argv, cwd=root, env=dbt_env(ctx), timeout=timeout)
    shown = "dbt " + " ".join(a for a in argv[1:] if not a.startswith(str(root)))
    if timed_out:
        raise ToolError(
            f"`{shown}` timed out after {timeout:.0f}s and was stopped.",
            hint="Build a narrower selection (select='model_name') or look for an expensive "
            "query, e.g. a join without a condition.",
        )
    max_chars = int(ctx.get("max_chars") or DEFAULT_MAX_CHARS)
    clean_out = ANSI_RE.sub("", out)
    net = NETWORK_ERROR_RE.search(clean_out + "\n" + err)
    security = [{"kind": "egress_attempt_failed", "detail": net.group(0)[:120]}] if net else []
    if command in {"ls", "show", "compile", "parse"}:
        status = "OK" if code == 0 else f"FAILED (exit {code})"
        body = clean_out.strip()
        if command == "parse" and code == 0:
            body = "Project parsed without errors."
        elif command == "compile" and code == 0:
            body = (
                _error_lines(clean_out, 15) if "warning" in clean_out.lower() else ""
            ) + "\nCompiled SQL is under target/compiled/ (read it with read_file)."
        elif code != 0:
            body = _error_lines(clean_out + "\n" + err)
        return {
            "output": truncate(f"{shown}: {status} in {seconds:.1f}s\n{body.strip()}", max_chars),
            "data": {
                "exit_code": code,
                "command": command,
                "select": select,
                "exclude": exclude,
                "seconds": round(seconds, 2),
            },
            "security": security,
        }
    summary = _summarise_run_results(root, started)
    lines = [f"{shown}: {'PASSED' if code == 0 else f'FAILED (exit {code})'} in {seconds:.1f}s"]
    data: dict[str, Any] = {
        "exit_code": code,
        "command": command,
        "select": select,
        "exclude": exclude,
        "seconds": round(seconds, 2),
        "full_refresh": bool(args.get("full_refresh")),
    }
    if summary is None:
        lines.append("dbt stopped before running any node. Error output:")
        lines.append(_error_lines(clean_out + "\n" + err))
        lines.append(
            "Hint: this is usually a YAML, Jinja or ref() error. Fix it, then run "
            "run_dbt('parse') to check quickly."
        )
        data["counts"] = {}
        data["failures"] = []
        data["passed"] = []
    else:
        counts = summary["counts"]
        lines.append("  " + " | ".join(f"{k.upper()} {v}" for k, v in sorted(counts.items())))
        if summary["failures"]:
            lines.append("Failures:")
            for item in summary["failures"][:15]:
                msg = item["message"].replace("\n", "\n      ")
                if len(msg) > 900:
                    msg = msg[:900] + " ..."
                lines.append(f"  [{item['status'].upper()}] {item['unique_id']}\n      {msg}")
            if len(summary["failures"]) > 15:
                lines.append(f"  ... and {len(summary['failures']) - 15} more failures")
            lines.append(
                "Hint: fix the first failure; compiled SQL of a failing node is in "
                "target/run/<project>/<path>.sql. Re-run with select='<model>+' to rebuild it "
                "and its children."
            )
        if summary["skipped"]:
            names = ", ".join(_short(u) for u in summary["skipped"][:12])
            more = (
                f" (+{len(summary['skipped']) - 12} more)" if len(summary["skipped"]) > 12 else ""
            )
            lines.append(f"Skipped because an upstream node failed: {names}{more}")
        data["counts"] = counts
        data["failures"] = [
            {"unique_id": f["unique_id"], "status": f["status"]} for f in summary["failures"]
        ]
        data["passed"] = summary["passed"]
        data["skipped"] = summary["skipped"]
    return {"output": truncate("\n".join(lines), max_chars), "data": data, "security": security}


_TABLE_CELL = 40


def _format_table(columns: list[str], rows: list[tuple], total_hint: str) -> str:
    def cell(value: Any) -> str:
        text = "NULL" if value is None else str(value)
        text = text.replace("\n", "\\n")
        return text if len(text) <= _TABLE_CELL else text[: _TABLE_CELL - 3] + "..."

    grid = [[cell(c) for c in columns]] + [[cell(v) for v in row] for row in rows]
    widths = [max(len(r[i]) for r in grid) for i in range(len(columns))]
    out = [
        " | ".join(v.ljust(widths[i]) for i, v in enumerate(grid[0])),
        "-+-".join("-" * w for w in widths),
    ]
    out += [" | ".join(v.ljust(widths[i]) for i, v in enumerate(r)) for r in grid[1:]]
    return "\n".join(out) + f"\n({total_hint})"


def _read_only_connection(ctx: dict[str, Any]):
    import duckdb  # imported lazily: not every tool needs it

    path = db_path(ctx)
    if not path.exists():
        raise ToolError(
            "The DuckDB database does not exist yet.",
            hint="Run run_dbt('build') first to create it.",
        )
    config = {
        "enable_external_access": False,
        "autoinstall_known_extensions": False,
        "autoload_known_extensions": False,
        "threads": 2,
        "max_memory": str(ctx.get("query_max_memory") or "1GB"),
        "lock_configuration": True,
    }
    try:
        return duckdb.connect(str(path), read_only=True, config=config)
    except Exception as exc:  # e.g. lock held by a running dbt process
        raise ToolError(
            f"Could not open the database read-only: {exc}",
            hint="Wait for dbt to finish, then query again.",
        ) from exc


def tool_query_duckdb(ctx: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    import duckdb

    sql = str(args.get("sql") or "").strip().rstrip(";").strip()
    if not sql:
        raise ToolError("sql is empty.")
    try:
        statements = duckdb.extract_statements(sql)
    except Exception as exc:
        raise ToolError(
            f"SQL does not parse: {exc}", hint="Check the syntax; DuckDB SQL dialect."
        ) from exc
    if len(statements) != 1:
        raise ToolError("Send exactly one statement per call.")
    kind = str(statements[0].type).split(".")[-1]
    if kind not in {"SELECT", "EXPLAIN"}:
        raise ToolError(
            f"Only read-only queries are allowed (got a {kind} statement).",
            hint="query_duckdb is for inspecting data. Change tables by editing dbt models and "
            "running run_dbt.",
            security="write_attempt_blocked"
            if kind
            in {
                "INSERT",
                "UPDATE",
                "DELETE",
                "CREATE",
                "DROP",
                "ALTER",
                "COPY",
                "ATTACH",
                "EXPORT",
                "LOAD",
                "SET",
            }
            else None,
        )
    max_rows = max(1, min(int(args.get("max_rows") or 50), 200))
    con = _read_only_connection(ctx)
    timeout = float(ctx.get("query_timeout_s") or 60)
    timer = threading.Timer(timeout, con.interrupt)
    timer.start()
    try:
        cursor = con.execute(sql)
        columns = [d[0] for d in (cursor.description or [])]
        rows = cursor.fetchmany(max_rows + 1)
    except duckdb.PermissionException as exc:
        kind = "egress_attempt_blocked" if URL_RE.search(sql) else "file_access_blocked"
        raise ToolError(
            f"Blocked: {exc}", hint="Files and network are not reachable from SQL.", security=kind
        ) from exc
    except duckdb.InterruptException as exc:
        raise ToolError(
            f"Query cancelled after {timeout:.0f}s.",
            hint="Add filters or LIMIT; avoid joins without conditions.",
        ) from exc
    except duckdb.Error as exc:
        raise ToolError(
            f"{type(exc).__name__}: {exc}",
            hint="Use 'show all tables' or 'describe <schema>.<table>' to check names and columns.",
        ) from exc
    finally:
        timer.cancel()
        con.close()
    more = len(rows) > max_rows
    rows = rows[:max_rows]
    hint = f"first {max_rows} rows shown; more exist" if more else f"{len(rows)} rows"
    output = _format_table(columns, rows, hint) if columns else "Statement returned no columns."
    return {
        "output": truncate(output, int(ctx.get("max_chars") or DEFAULT_MAX_CHARS)),
        "data": {"rows": len(rows), "truncated": more},
    }


def _stage_all(ctx: dict[str, Any], index: str) -> None:
    code, out, err = git(ctx, ["add", "-A", "--", "."], index=index, timeout=120)
    if code != 0:
        raise ToolError("git add failed: " + (err or out)[:500])


def tool_git_diff(ctx: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    _stage_all(ctx, "agent-diff-index")
    argv = ["diff", "--cached", "--no-color", "HEAD"]
    if args.get("stat_only"):
        argv.insert(3, "--stat")
    if args.get("path"):
        target = resolve(ctx, args["path"])
        argv += ["--", rel_display(root_of(ctx), target)]
    code, out, err = git(ctx, argv, index="agent-diff-index")
    if code != 0:
        raise ToolError("git diff failed: " + (err or out)[:500])
    if not out.strip():
        return {"output": "No changes compared with the starting commit."}
    return {"output": truncate(out, int(ctx.get("max_chars") or DEFAULT_MAX_CHARS))}


# ----------------------------------------------------------------------------- harness tools
def tool_ping(ctx: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    info = {"python": sys.version.split()[0], "root": str(root_of(ctx))}
    try:
        info["dbt"] = dbt_executable(ctx)
    except ToolError:
        info["dbt"] = None
    # harness-only: lets tests simulate a hung tool (capped at 120 s)
    time.sleep(min(float(args.get("sleep_s") or 0), 120.0))
    return {"output": "pong", "data": info}


def tool_snapshot(ctx: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    _stage_all(ctx, "agent-snap-index")
    code, out, err = git(ctx, ["write-tree"], index="agent-snap-index")
    if code != 0:
        raise ToolError("git write-tree failed: " + (err or out)[:500])
    return {"output": out.strip(), "data": {"tree": out.strip()}}


def tool_restore(ctx: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    tree = str(args.get("tree") or "")
    if not re.fullmatch(r"[0-9a-f]{40,64}", tree):
        raise ToolError("restore needs a tree hash")
    for argv in (
        ["read-tree", tree],
        ["checkout-index", "-a", "-f"],
        ["clean", "-fdq", "-e", ".agent"],
    ):
        code, out, err = git(ctx, argv, index="agent-snap-index", timeout=120)
        if code != 0:
            raise ToolError(f"git {argv[0]} failed: " + (err or out)[:500])
    return {"output": f"restored {tree}"}


def tool_git_patch(ctx: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    _stage_all(ctx, "agent-patch-index")
    code, out, err = git(
        ctx,
        ["diff", "--cached", "--binary", "--no-color", "HEAD"],
        index="agent-patch-index",
        timeout=120,
    )
    if code != 0:
        raise ToolError("git diff failed: " + (err or out)[:500])
    _code, stat, _ = git(ctx, ["diff", "--cached", "--numstat", "HEAD"], index="agent-patch-index")
    files = []
    for line in stat.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            files.append({"path": parts[2], "added": parts[0], "deleted": parts[1]})
    return {"output": out, "data": {"files": files}}


def tool_git_init(ctx: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """Harness-only: make the work directory a git repo with one baseline commit (used when
    the agent works in a container someone else prepared, e.g. ADE-bench)."""
    root = root_of(ctx)
    fresh = not (root / ".git").exists()
    if fresh:
        code, out, err = git(ctx, ["init", "-q"], timeout=60)
        if code != 0:
            raise ToolError("git init failed: " + (err or out)[:500])
    # excludes first, so databases, build output and packages never enter the baseline
    exclude = root / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    if ".agent/" not in (exclude.read_text() if exclude.exists() else ""):
        with exclude.open("a", encoding="utf-8") as handle:
            handle.write("\n.agent/\ntarget/\nlogs/\ndbt_packages/\n*.duckdb\n*.duckdb.wal\n")
    if fresh:
        for argv in (
            ["add", "-A", "--", "."],
            ["commit", "-q", "--allow-empty", "-m", "baseline before the agent"],
        ):
            code, out, err = git(ctx, argv, timeout=300)
            if code != 0:
                raise ToolError(f"git {argv[0]} failed: " + (err or out)[:500])
    code, out, _err = git(ctx, ["rev-parse", "HEAD"])
    return {"output": out.strip(), "data": {"head": out.strip() if code == 0 else None}}


def tool_git_apply(ctx: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    patch = str(args.get("patch") or "")
    if not patch.strip():
        return {"output": "empty patch", "data": {"applied": True, "empty": True}}
    if re.search(r"^(?:new|old) mode 120000|^new file mode 120000", patch, re.MULTILINE):
        raise ToolError("patch creates a symlink; refused", security="symlink_blocked")
    code, out, err = git(
        ctx, ["apply", "--binary", "--whitespace=nowarn", "-"], stdin=patch, timeout=120
    )
    if code != 0:
        raise ToolError("git apply failed: " + (err or out)[:1500])
    return {"output": "applied", "data": {"applied": True}}


def tool_git_commit(ctx: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """Create a branch with one commit holding the agent's change (main index)."""
    branch = str(args.get("branch") or "")
    message = str(args.get("message") or "agent change")
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,120}", branch):
        raise ToolError("invalid branch name")
    steps = [
        ["checkout", "-q", "-B", branch],
        ["add", "-A", "--", "."],
        ["commit", "-q", "--allow-empty", "-m", message],
        ["rev-parse", "HEAD"],
    ]
    out = ""
    for argv in steps:
        code, out, err = git(ctx, argv, timeout=120)
        if code != 0:
            raise ToolError(f"git {argv[0]} failed: " + (err or out)[:500])
    return {"output": out.strip(), "data": {"commit": out.strip(), "branch": branch}}


def tool_dbt_raw(ctx: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """Harness-only dbt call with arbitrary arguments; returns full run results."""
    root = root_of(ctx)
    argv = [dbt_executable(ctx), *[str(a) for a in args.get("argv", [])]]
    timeout = float(args.get("timeout_s") or ctx.get("dbt_timeout_s") or 600)
    started = time.time()
    code, out, err, timed_out, seconds = run_proc(argv, cwd=root, env=dbt_env(ctx), timeout=timeout)
    summary = None if timed_out else _summarise_run_results(root, started, max_items=10_000)
    clean = ANSI_RE.sub("", out + "\n" + err)
    return {
        "output": truncate(clean, 6000),
        "data": {
            "exit_code": code,
            "timed_out": timed_out,
            "seconds": round(seconds, 2),
            "results": summary,
            "error_excerpt": _error_lines(clean, 30),
        },
    }


def tool_dbt_ls(ctx: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """Harness-only `dbt ls` returning every selected node (no truncation)."""
    root = root_of(ctx)
    argv = [
        dbt_executable(ctx),
        "ls",
        "--output",
        "json",
        "--output-keys",
        "unique_id",
        "resource_type",
        "name",
        "--quiet",
    ]
    for flag in ("select", "exclude"):
        value = str(args.get(flag) or "").strip()
        if value:
            argv += [f"--{flag}", *value.split()]
    if args.get("state"):
        argv += ["--state", str(args["state"])]
    for rtype in args.get("resource_types") or []:
        argv += ["--resource-type", str(rtype)]
    code, out, err, timed_out, _seconds = run_proc(
        argv, cwd=root, env=dbt_env(ctx), timeout=float(args.get("timeout_s") or 300)
    )
    nodes = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                nodes.append(json.loads(line))
            except ValueError:
                continue
    return {
        "output": f"{len(nodes)} nodes",
        "data": {
            "exit_code": code,
            "nodes": nodes,
            "timed_out": timed_out,
            "error_excerpt": _error_lines(out + err, 20) if code else "",
        },
    }


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def tool_sql_exec(ctx: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """Harness-only SQL with write access (task setup, fingerprints). Trusted input."""
    import duckdb

    path = db_path(ctx)
    con = duckdb.connect(str(path), read_only=bool(args.get("read_only")))
    results = []
    try:
        for statement in args.get("statements", []):
            cursor = con.execute(statement)
            try:
                rows = cursor.fetchall()
                cols = [d[0] for d in (cursor.description or [])]
            except duckdb.Error:
                rows, cols = [], []
            results.append(
                {"columns": cols, "rows": [[_json_value(v) for v in row] for row in rows[:5000]]}
            )
    finally:
        con.close()
    return {"output": f"{len(results)} statement(s) executed", "data": {"results": results}}


def tool_manifest_summary(ctx: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """Compact view of target/manifest.json (or a given path) for the verifier/grader."""
    root = root_of(ctx)
    path = resolve(ctx, args.get("path") or "target/manifest.json")
    if not path.exists():
        raise ToolError(f"{rel_display(root, path)} not found")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    project = manifest.get("metadata", {}).get("project_name")
    nodes = {}
    for uid, node in {**manifest.get("nodes", {}), **manifest.get("sources", {})}.items():
        if node.get("package_name") not in (None, project):
            continue
        config = node.get("config") or {}
        nodes[uid] = {
            "resource_type": node.get("resource_type"),
            "name": node.get("name"),
            "path": node.get("original_file_path"),
            "checksum": (node.get("checksum") or {}).get("checksum"),
            "materialized": config.get("materialized"),
            "enabled": config.get("enabled", True),
            "severity": config.get("severity"),
            "where": config.get("where"),
            "contract": bool((config.get("contract") or {}).get("enforced")),
            "schema": node.get("schema"),
            "alias": node.get("alias") or node.get("identifier"),
            "description": node.get("description") or "",
            "columns": {
                c: (v.get("description") or "") for c, v in (node.get("columns") or {}).items()
            },
            "depends_on": (node.get("depends_on") or {}).get("nodes", []),
            "attached_node": node.get("attached_node"),
            "test_name": (node.get("test_metadata") or {}).get("name"),
            "column_name": node.get("column_name")
            or ((node.get("test_metadata") or {}).get("kwargs") or {}).get("column_name"),
            "unique_key": config.get("unique_key"),
        }
    unit_tests = list((manifest.get("unit_tests") or {}).keys())
    exposures = {
        uid: {"name": e.get("name"), "depends_on": (e.get("depends_on") or {}).get("nodes", [])}
        for uid, e in (manifest.get("exposures") or {}).items()
    }
    return {
        "output": f"{len(nodes)} nodes",
        "data": {
            "project": project,
            "nodes": nodes,
            "unit_tests": unit_tests,
            "exposures": exposures,
        },
    }


def tool_sqlfluff(ctx: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    root = root_of(ctx)
    files = [rel_display(root, resolve(ctx, f)) for f in args.get("files", [])]
    files = [f for f in files if (root / f).exists()]
    if not files:
        return {"output": "no files", "data": {"files": {}}}
    exe_dir = Path(sys.executable).parent
    sqlfluff = next(
        (str(exe_dir / n) for n in ("sqlfluff", "sqlfluff.exe") if (exe_dir / n).exists()),
        shutil.which("sqlfluff") or "sqlfluff",
    )
    argv = [sqlfluff, "lint", *files, "--format", "json", "--nofail", "--disable-progress-bar"]
    _code, out, err, timed_out, seconds = run_proc(
        argv, cwd=root, env=dbt_env(ctx), timeout=float(args.get("timeout_s") or 600)
    )
    if timed_out:
        raise ToolError("sqlfluff timed out")
    try:
        start = out.index("[")
        payload = json.loads(out[start:])
    except ValueError as exc:
        raise ToolError("sqlfluff output was not JSON: " + truncate(out + err, 1500)) from exc
    result = {}
    for entry in payload:
        violations = [
            {
                "code": v.get("code"),
                "line": v.get("start_line_no") or v.get("line_no"),
                "description": v.get("description"),
            }
            for v in entry.get("violations", [])
        ]
        result[entry.get("filepath", "").replace("\\", "/")] = violations
    return {
        "output": f"linted {len(result)} files in {seconds:.1f}s",
        "data": {"files": result, "seconds": round(seconds, 2)},
    }


def tool_tree_hash(ctx: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    code, out, _err = git(ctx, ["rev-parse", "HEAD"])
    return {"output": out.strip(), "data": {"head": out.strip() if code == 0 else None}}


MODEL_TOOLS = {
    "list_files": tool_list_files,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "edit_file": tool_edit_file,
    "delete_file": tool_delete_file,
    "search_files": tool_search_files,
    "apply_patch": tool_apply_patch,
    "run_dbt": tool_run_dbt,
    "query_duckdb": tool_query_duckdb,
    "git_diff": tool_git_diff,
}
HARNESS_TOOLS = {
    "ping": tool_ping,
    "snapshot": tool_snapshot,
    "restore": tool_restore,
    "git_patch": tool_git_patch,
    "git_apply": tool_git_apply,
    "git_init": tool_git_init,
    "git_commit": tool_git_commit,
    "dbt_raw": tool_dbt_raw,
    "dbt_ls": tool_dbt_ls,
    "sql_exec": tool_sql_exec,
    "manifest_summary": tool_manifest_summary,
    "sqlfluff": tool_sqlfluff,
    "tree_hash": tool_tree_hash,
}


def handle(request: dict[str, Any]) -> dict[str, Any]:
    name = request.get("tool")
    args = request.get("args") or {}
    ctx = request.get("ctx") or {}
    fn = MODEL_TOOLS.get(name) or HARNESS_TOOLS.get(name)
    response: dict[str, Any] = {
        "ok": False,
        "output": "",
        "error": None,
        "hint": None,
        "data": {},
        "security": [],
    }
    if fn is None:
        response["error"] = f"Unknown tool {name!r}."
        return response
    if not isinstance(args, dict):
        response["error"] = "Tool arguments must be a JSON object."
        return response
    # every model-supplied string is scanned for network references before execution
    if name in MODEL_TOOLS:
        response["security"].extend(security_scan(json.dumps(args)))
    try:
        result = fn(ctx, args)
        response["ok"] = True
        response["output"] = result.get("output", "")
        response["data"] = result.get("data", {})
        for event in result.get("security", []):
            if event not in response["security"]:
                response["security"].append(event)
    except ToolError as exc:
        response["error"] = str(exc)
        response["hint"] = exc.hint
        if exc.security:
            response["security"].append({"kind": exc.security, "detail": str(exc)[:200]})
    except Exception as exc:  # unexpected: report, never crash the harness
        response["error"] = f"Internal tool error: {type(exc).__name__}: {exc}"
    return response


def main() -> int:
    raw = sys.stdin.buffer.read().decode("utf-8")
    try:
        request = json.loads(raw)
    except ValueError as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": f"bad request: {exc}"}))
        return 0
    response = handle(request)
    sys.stdout.write(json.dumps(response, default=str))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
