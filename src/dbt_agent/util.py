"""Small shared helpers: atomic writes, JSON, time, text."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_ts(value: str) -> float:
    """ISO timestamp written by utc_now() -> epoch seconds."""
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC).timestamp()


def read_text_exact(path: Path) -> str:
    """Read text without newline translation: a patch must keep its CRLF lines byte for byte
    (``Path.read_text`` turns CRLF into LF, and the patch then no longer applies)."""
    return path.read_bytes().decode("utf-8")


def atomic_write_text(path: Path, text: str) -> None:
    """Write via a temp file + rename so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        Path(tmp).replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(tmp).unlink()
        raise


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~3.6 characters per token for code and English)."""
    return max(1, int(len(text) / 3.6))


def short(text: str | None, limit: int = 300) -> str:
    if not text:
        return ""
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def slug(text: str, limit: int = 48) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return value[:limit] or "run"


def force_rmtree(path: Path) -> None:
    """rmtree that also removes read-only files (git objects on Windows).

    shutil.rmtree never follows symlinks, so it is safe on directories written by
    sandboxed code.
    """
    import shutil
    import stat

    def _retry(func, target, _exc_info):
        with contextlib.suppress(OSError):
            Path(target).chmod(stat.S_IWRITE)
            func(target)

    if Path(path).exists():
        shutil.rmtree(path, onerror=_retry)


class Stopwatch:
    def __init__(self) -> None:
        self.started = time.monotonic()

    @property
    def seconds(self) -> float:
        return time.monotonic() - self.started
