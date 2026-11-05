"""Append-only JSONL event log: the run's source of truth and its resume point.

Every decision is written (and fsynced) *before* the harness acts on it: a ``tool_call``
event precedes the tool's execution, a ``tool_result`` follows it. After a crash, replaying
the log rebuilds the conversation, the budgets and the last workspace snapshot exactly
(see ``AgentRunner._resume``). A torn last line (crash mid-write) is detected and cut off.

The log lives on the host, outside the sandbox work directory, so the agent cannot edit
its own history.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from dbt_agent.util import utc_now


class EventLog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.repaired = self._repair_tail()
        self._seq = self._last_seq()

    def _repair_tail(self) -> bool:
        """Drop a trailing partial line left by a crash. Returns True if it repaired."""
        if not self.path.exists():
            return False
        data = self.path.read_bytes()
        if not data:
            return False
        lines = data.split(b"\n")
        keep = len(data)
        # the file must end with "\n" and every line must be valid JSON
        if not data.endswith(b"\n"):
            keep = len(data) - len(lines[-1])
        else:
            last = lines[-2] if len(lines) >= 2 else b""
            try:
                json.loads(last)
            except ValueError:
                keep = len(data) - len(last) - 1
        if keep != len(data):
            with self.path.open("r+b") as handle:
                handle.truncate(keep)
            return True
        return False

    def _last_seq(self) -> int:
        events = self.read()
        return int(events[-1]["seq"]) if events else 0

    def append(self, event_type: str, **payload: Any) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            event = {"seq": self._seq, "ts": utc_now(), "type": event_type, **payload}
            line = json.dumps(event, ensure_ascii=False, default=str)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return event

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        events = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except ValueError:
                    break  # only possible for a torn tail, which _repair_tail removes
        return events

    def of_type(self, *types: str) -> list[dict[str, Any]]:
        return [e for e in self.read() if e.get("type") in types]
