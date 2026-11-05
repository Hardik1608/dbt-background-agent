"""Loop detection.

Small models get stuck in three recognisable ways:

1. **repeat** - the same call with the same result, again and again (re-reading a file,
   re-running an unchanged build);
2. **error streak** - the same error several times in a row (an edit that never matches);
3. **oscillation** - edits that return the workspace to a state it was already in
   (apply a fix, revert it, apply it again).

The first detection injects a warning into the tool result telling the model what to do
differently; after ``stop_after_warnings`` warnings the run stops with ``loop_detected``.
State is rebuilt from the event log on resume (``observe`` is replayed).
"""

from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass, field
from typing import Literal

from dbt_agent.config import LoopConfig
from dbt_agent.util import canonical_json

Signal = Literal["ok", "warn", "stop"]


@dataclass
class LoopVerdict:
    signal: Signal
    kind: str | None = None
    message: str | None = None


@dataclass
class _Record:
    call_key: str
    result_key: str
    ok: bool
    error_key: str | None


@dataclass
class LoopDetector:
    config: LoopConfig
    history: deque = field(default_factory=deque)
    trees_seen: dict[str, int] = field(default_factory=dict)
    last_tree: str | None = None
    warnings: int = 0
    oscillations: int = 0

    @staticmethod
    def _digest(text: str) -> str:
        return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]

    def observe(
        self,
        name: str,
        args: dict,
        *,
        ok: bool,
        output: str,
        error: str | None,
        tree: str | None = None,
    ) -> LoopVerdict:
        record = _Record(
            call_key=f"{name}:{self._digest(canonical_json(args))}",
            result_key=self._digest(output if ok else (error or "")),
            ok=ok,
            error_key=self._digest(error) if error else None,
        )
        self.history.append(record)
        while len(self.history) > self.config.window:
            self.history.popleft()

        verdict = LoopVerdict("ok")
        same = [
            r
            for r in self.history
            if r.call_key == record.call_key and r.result_key == record.result_key
        ]
        if len(same) >= self.config.repeat_threshold:
            verdict = LoopVerdict(
                "warn",
                "repeat",
                f"LOOP WARNING: you have made this exact {name} call {len(same)} times and got "
                "the same result each time. Repeating it will not help. Change something "
                "first (edit a file, use a different query or selection), or call finish if "
                "you are done or blocked.",
            )
            self.history = deque(r for r in self.history if r.call_key != record.call_key)
        elif record.error_key:
            streak = 0
            for r in reversed(self.history):
                if r.error_key == record.error_key:
                    streak += 1
                else:
                    break
            if streak >= self.config.error_repeat_threshold:
                verdict = LoopVerdict(
                    "warn",
                    "error_streak",
                    f"LOOP WARNING: the same error came back {streak} times in a row. Read the "
                    "error and the hint carefully and try a different approach (for edit_file: "
                    "read_file first and copy the exact text).",
                )
                self.history.clear()

        if tree is not None and tree != self.last_tree:
            if tree in self.trees_seen and verdict.signal == "ok":
                self.oscillations += 1
                if self.oscillations >= self.config.oscillation_threshold:
                    verdict = LoopVerdict(
                        "warn",
                        "oscillation",
                        "LOOP WARNING: your edits keep returning the files to a state you "
                        "already tried. Stop undoing and redoing; use git_diff to see where you "
                        "are, decide on one approach, and verify it with run_dbt.",
                    )
                    self.oscillations = 0
            self.trees_seen[tree] = self.trees_seen.get(tree, 0) + 1
            self.last_tree = tree

        if verdict.signal == "warn":
            self.warnings += 1
            if self.warnings > self.config.stop_after_warnings:
                return LoopVerdict(
                    "stop",
                    verdict.kind,
                    f"Stopped: loop detected ({verdict.kind}) after "
                    f"{self.config.stop_after_warnings} warnings.",
                )
        return verdict

    def seed_tree(self, tree: str) -> None:
        """Register the starting workspace state (so reverting to it counts)."""
        self.trees_seen.setdefault(tree, 1)
        self.last_tree = tree
