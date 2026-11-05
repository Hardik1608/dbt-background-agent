"""The agent loop (runs on the host; tools run in the sandbox).

    plan/act turn:  LLM call -> tool calls -> sandbox -> results -> next LLM call
    stop when:      finish accepted by the checklist | a budget runs out | loop detected |
                    the checklist rejects too often | the model stops using tools

Run state is *event-sourced*: ``RunState.replay(events)`` rebuilds the conversation,
budgets, loop-detector state and the last workspace snapshot from the JSONL log. A run
killed at any point (power cut, OOM, Ctrl-C) resumes by constructing a new ``AgentRunner``
on the same run directory:

* an LLM request without a response is simply sent again;
* tool calls of the last model message without a logged result are re-executed after the
  workspace is restored to the last snapshot (``git write-tree`` after every mutating call),
  so each edit takes effect exactly once.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dbt_agent.config import RunConfig
from dbt_agent.harness import prompts
from dbt_agent.harness.budgets import Budget, Usage
from dbt_agent.harness.checklist import Checklist
from dbt_agent.harness.events import EventLog
from dbt_agent.harness.loopdetect import LoopDetector
from dbt_agent.harness.progress import render_progress
from dbt_agent.harness.security import summarise as summarise_security
from dbt_agent.harness.tools import MUTATING_TOOLS, tool_schemas
from dbt_agent.llm.types import ChatModel, ChatResponse, ContextLengthError, LLMError, ToolCall
from dbt_agent.sandbox.base import Sandbox, SandboxError
from dbt_agent.util import atomic_write_json, atomic_write_text, parse_ts, sha256_text, short

TERMINAL_STATUSES = {
    "completed",
    "budget_exhausted",
    "loop_detected",
    "checklist_failed",
    "llm_error",
    "sandbox_error",
    "no_progress",
}


@dataclass
class AgentOutcome:
    status: str
    reason: str | None = None
    summary: str | None = None
    tests_changed_reason: str | None = None
    turns: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tokens_estimated: bool = False
    tool_calls: int = 0
    wall_seconds: float = 0.0
    cost_eur: float = 0.0
    sessions: int = 1
    checklist: dict[str, Any] | None = None
    security: dict[str, Any] = field(default_factory=dict)
    loop_warnings: int = 0
    compactions: int = 0
    tool_errors: int = 0
    text_tool_calls: int = 0

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentOutcome:
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class RunState:
    messages: list[dict[str, Any]] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finished: AgentOutcome | None = None
    start_tree: str | None = None
    last_tree: str | None = None
    pending: list[ToolCall] = field(default_factory=list)
    checklist_rejections: int = 0
    checklist_history: list[dict[str, Any]] = field(default_factory=list)
    text_only_streak: int = 0
    dbt_runs: list[dict[str, Any]] = field(default_factory=list)
    files: dict[str, int] = field(default_factory=dict)
    security_events: list[dict[str, Any]] = field(default_factory=list)
    recent_calls: list[str] = field(default_factory=list)
    last_note: str = ""
    budget_warned: bool = False
    sessions: int = 0
    tool_errors: int = 0
    text_tool_calls: int = 0
    compactions: int = 0
    token_ratio: float = 1.0

    # ---------------------------------------------------------------- replay
    @classmethod
    def replay(cls, events: list[dict[str, Any]], detector: LoopDetector) -> RunState:
        state = cls()
        session_start: float | None = None
        session_last: float | None = None
        for event in events:
            kind = event["type"]
            ts = parse_ts(event["ts"])
            if kind == "session_started":
                if session_start is not None and session_last is not None:
                    state.usage.wall_seconds_previous_sessions += session_last - session_start
                session_start, session_last = ts, ts
                state.sessions += 1
                continue
            session_last = ts
            if kind == "run_started":
                state.messages = [
                    {"role": "system", "content": event["system_prompt"]},
                    {"role": "user", "content": event["user_message"]},
                ]
            elif kind == "workspace_snapshot":
                if event.get("reason") == "start":
                    state.start_tree = event["tree"]
                    detector.seed_tree(event["tree"])
                state.last_tree = event["tree"]
            elif kind == "llm_response":
                state.apply_response(ChatResponse.from_dict(event["response"]))
            elif kind == "tool_result":
                state.apply_tool_result(event, detector)
            elif kind == "harness_message":
                state.messages.append({"role": "user", "content": event["content"]})
                if event.get("reason") == "budget_warning":
                    state.budget_warned = True
            elif kind == "checklist":
                state.checklist_history.append(event)
                if not event.get("passed"):
                    state.checklist_rejections += 1
            elif kind == "context_compacted":
                state.compactions += 1
            elif kind == "run_finished":
                state.finished = AgentOutcome.from_dict(event["outcome"])
        if session_start is not None and session_last is not None:
            state.usage.wall_seconds_previous_sessions += session_last - session_start
        return state

    def apply_response(self, response: ChatResponse) -> None:
        self.messages.append(response.assistant_message())
        self.usage.turns += 1
        self.usage.prompt_tokens += response.usage.prompt_tokens
        self.usage.completion_tokens += response.usage.completion_tokens
        self.usage.estimated_tokens |= response.usage.estimated
        self.pending = list(response.tool_calls)
        if response.parsed_from_text:
            self.text_tool_calls += 1
        if response.content:
            self.last_note = response.content
        self.text_only_streak = 0 if response.tool_calls else self.text_only_streak + 1

    def apply_tool_result(self, event: dict[str, Any], detector: LoopDetector | None) -> None:
        self.messages.append(
            {"role": "tool", "tool_call_id": event["call_id"], "content": event["text"]}
        )
        self.pending = [c for c in self.pending if c.id != event["call_id"]]
        self.usage.tool_calls += 1
        if not event.get("ok"):
            self.tool_errors += 1
        if event.get("tree"):
            self.last_tree = event["tree"]
        name = event.get("name", "?")
        args = event.get("args") or {}
        data = event.get("data") or {}
        self.security_events.extend(event.get("security") or [])
        brief = ", ".join(
            f"{k}={short(str(v), 40)}"
            for k, v in args.items()
            if k not in {"content", "new_string", "old_string", "patch"}
        )
        self.recent_calls.append(f"{name}({brief}) -> {'ok' if event.get('ok') else 'error'}")
        if name in MUTATING_TOOLS and event.get("ok") and data.get("path"):
            self.files[data["path"]] = self.files.get(data["path"], 0) + 1
        if name == "run_dbt" and "exit_code" in data:
            self.dbt_runs.append(
                {
                    "turn": event.get("turn"),
                    **{
                        k: data.get(k)
                        for k in ("command", "select", "exit_code", "counts", "failures")
                    },
                }
            )
        if detector is not None and "loop_out" in event:
            detector.observe(
                name,
                args,
                ok=bool(event.get("ok")),
                output=event["loop_out"],
                error=event.get("loop_err"),
                tree=event.get("tree"),
            )


class AgentRunner:
    def __init__(
        self,
        *,
        run_dir: Path,
        task: prompts.AgentTask,
        config: RunConfig,
        llm: ChatModel,
        sandbox: Sandbox,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.task = task
        self.config = config
        self.llm = llm
        self.sandbox = sandbox
        self.log = EventLog(self.run_dir / "events.jsonl")
        self.budget = Budget(config.agent.budgets, config.llm)
        self.detector = LoopDetector(config.agent.loop)
        self.checklist = Checklist(
            sandbox,
            run_build=config.agent.checklist_build,
            command=config.agent.checklist_command,
            select=config.agent.checklist_select,
            full_refresh=config.agent.checklist_full_refresh,
            build_timeout_s=config.agent.tool_timeout_s * 2,
        )
        self.tools = tool_schemas(config.agent.tools)
        self.enabled = set(config.agent.tools) | {"finish"}
        self.state = RunState()
        # tool output size is a harness decision, not something the model controls
        sandbox.context.max_chars = config.agent.context.max_tool_output_chars
        self._session_started = time.monotonic()
        self._compact_level = 0

    # ---------------------------------------------------------------- public
    def run(self) -> AgentOutcome:
        events = self.log.read()
        if events:
            self.state = RunState.replay(events, self.detector)
            if self.state.finished is not None:
                return self.state.finished
        self.state.sessions += 1
        self._session_started = time.monotonic()
        resumed = bool(events)
        self.log.append(
            "session_started",
            session=self.state.sessions,
            resumed=resumed,
            log_repaired=self.log.repaired,
            sandbox=self.sandbox.describe(),
        )
        try:
            if resumed:
                outcome = self._resume()
            else:
                self._start()
                outcome = None
            if outcome is None:
                outcome = self._loop()
        except SandboxError as exc:
            outcome = self._finish("sandbox_error", str(exc))
        except KeyboardInterrupt:
            self.log.append(
                "session_ended",
                reason="interrupted",
                seconds=round(time.monotonic() - self._session_started, 2),
            )
            raise
        return outcome

    # ---------------------------------------------------------------- lifecycle
    def _start(self) -> None:
        user_message = self.task.user_message()
        self.log.append(
            "run_started",
            task_id=self.task.task_id,
            task_kind=self.task.kind,
            title=self.task.title,
            system_prompt=prompts.SYSTEM_PROMPT,
            user_message=user_message,
            tools=[t["function"]["name"] for t in self.tools],
            model=self.llm.model_name,
            config_digest=sha256_text(self.config.model_dump_json())[:16],
            budgets=self.config.agent.budgets.model_dump(),
        )
        self.state.messages = [
            {"role": "system", "content": prompts.SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]
        tree = self.sandbox.require("snapshot").data["tree"]
        self.state.start_tree = self.state.last_tree = tree
        self.detector.seed_tree(tree)
        self.log.append("workspace_snapshot", tree=tree, reason="start")

    def _resume(self) -> AgentOutcome | None:
        if self.state.last_tree:
            self.sandbox.require("restore", {"tree": self.state.last_tree})
            self.log.append("workspace_restored", tree=self.state.last_tree)
        # tool results must directly follow their assistant message, so finish the
        # interrupted calls before adding the resume note
        for call in list(self.state.pending):
            self.log.append("tool_call_replayed", call_id=call.id, name=call.name)
            if self._execute(call):
                return self.state.finished
        self._harness_message(
            prompts.RESUME_NOTE.format(progress=self._progress_text()), reason="resume"
        )
        return None

    def _finish(
        self,
        status: str,
        reason: str | None,
        *,
        summary: str | None = None,
        tests_changed_reason: str | None = None,
        checklist: dict[str, Any] | None = None,
    ) -> AgentOutcome:
        usage = self._usage()
        outcome = AgentOutcome(
            status=status,
            reason=reason,
            summary=summary,
            tests_changed_reason=tests_changed_reason,
            turns=usage.turns,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            tokens_estimated=usage.estimated_tokens,
            tool_calls=usage.tool_calls,
            wall_seconds=round(usage.wall_seconds, 2),
            cost_eur=round(self.budget.cost_eur(usage), 6),
            sessions=self.state.sessions,
            checklist=checklist,
            security=summarise_security(self.state.security_events),
            loop_warnings=self.detector.warnings,
            compactions=self.state.compactions,
            tool_errors=self.state.tool_errors,
            text_tool_calls=self.state.text_tool_calls,
        )
        self.state.finished = outcome
        self.log.append("run_finished", outcome=outcome.as_dict())
        self.log.append(
            "session_ended",
            reason=status,
            seconds=round(time.monotonic() - self._session_started, 2),
        )
        self._write_progress(status)
        atomic_write_json(self.run_dir / "outcome.json", outcome.as_dict())
        return outcome

    def _usage(self) -> Usage:
        usage = self.state.usage
        usage.session_seconds = time.monotonic() - self._session_started
        return usage

    # ---------------------------------------------------------------- loop
    def _loop(self) -> AgentOutcome:
        while True:
            usage = self._usage()
            exhausted = self.budget.exceeded(usage)
            if exhausted:
                return self._finish("budget_exhausted", exhausted)
            if not self.state.budget_warned and self.budget.nearly_exhausted(usage):
                self.state.budget_warned = True
                self._harness_message(
                    prompts.BUDGET_WARNING.format(note=self.budget.remaining_note(usage)),
                    reason="budget_warning",
                )

            messages = self._context()
            self.log.append(
                "llm_request",
                turn=usage.turns + 1,
                messages=len(messages),
                est_prompt_tokens=self._estimate(messages),
            )
            try:
                response = self.llm.chat(messages, self.tools)
            except ContextLengthError as exc:
                self._compact_level += 1
                self.log.append(
                    "context_length_error", level=self._compact_level, error=short(str(exc), 300)
                )
                if self._compact_level > 3:
                    return self._finish("llm_error", "context window exceeded after compaction")
                continue
            except LLMError as exc:
                return self._finish("llm_error", short(str(exc), 500))
            self._calibrate(messages, response)
            self.state.apply_response(response)
            self.log.append(
                "llm_response", turn=self.state.usage.turns, response=response.as_dict()
            )

            if not response.tool_calls:
                if self.state.text_only_streak >= self.config.agent.max_text_only_turns:
                    return self._finish("no_progress", "the model stopped calling tools")
                nudge = prompts.NUDGE_USE_TOOLS
                if response.finish_reason == "length":
                    nudge = (
                        "Your reply was cut off by the output limit. Keep replies short "
                        "and call one tool at a time. " + nudge
                    )
                self._harness_message(nudge, reason="nudge")
                continue

            for call in list(response.tool_calls):
                if self._execute(call):
                    return self.state.finished  # type: ignore[return-value]
            self._write_progress("running")

    # ---------------------------------------------------------------- tools
    def _execute(self, call: ToolCall) -> bool:
        """Execute one tool call; returns True when the run has finished."""
        args, error = call.parsed()
        if error is not None:
            self._record(
                call,
                {},
                ok=False,
                text=f"ERROR: {error} Call {call.name} again with a JSON object that "
                "matches its schema.",
                loop_out="badjson",
                loop_err=error,
            )
            return False
        assert args is not None
        if call.name == "finish":
            return self._finish_call(call, args)
        if call.name not in self.enabled:
            names = ", ".join(sorted(self.enabled))
            self._record(
                call,
                args,
                ok=False,
                text=f"ERROR: unknown tool '{call.name}'. Available tools: {names}.",
                loop_out="unknown",
                loop_err=f"unknown:{call.name}",
            )
            return False

        self.log.append("tool_call", call_id=call.id, name=call.name, args=args)
        timeout = (
            self.config.agent.tool_timeout_s
            if call.name == "run_dbt"
            else self.config.agent.file_tool_timeout_s
        )
        response = self.sandbox.call(call.name, args, timeout=timeout)
        tree = None
        if call.name in MUTATING_TOOLS and response.ok:
            tree = self.sandbox.require("snapshot").data["tree"]
        loop_out = LoopDetector._digest(response.output if response.ok else "")
        loop_err = LoopDetector._digest(response.error) if response.error else None
        verdict = self.detector.observe(
            call.name, args, ok=response.ok, output=loop_out, error=loop_err, tree=tree
        )
        text = response.model_text()
        if verdict.signal == "warn" and verdict.message:
            text += "\n\n" + verdict.message
        self._record(
            call,
            args,
            ok=response.ok,
            text=text,
            data=response.data,
            security=response.security,
            tree=tree,
            seconds=response.seconds,
            loop_out=loop_out,
            loop_err=loop_err,
            loop_signal=verdict.signal,
            replay_detector=False,
        )
        if verdict.signal == "stop":
            self._finish("loop_detected", verdict.message)
            return True
        return False

    def _record(
        self,
        call: ToolCall,
        args: dict[str, Any],
        *,
        ok: bool,
        text: str,
        data: dict[str, Any] | None = None,
        security: list[dict[str, Any]] | None = None,
        tree: str | None = None,
        seconds: float = 0.0,
        loop_out: str | None = None,
        loop_err: str | None = None,
        loop_signal: str = "ok",
        replay_detector: bool = True,
    ) -> None:
        event = self.log.append(
            "tool_result",
            call_id=call.id,
            name=call.name,
            args=args,
            ok=ok,
            text=text,
            data=data or {},
            security=security or [],
            tree=tree,
            seconds=round(seconds, 3),
            turn=self.state.usage.turns,
            loop_out=loop_out,
            loop_err=loop_err,
            loop_signal=loop_signal,
        )
        self.state.apply_tool_result(event, self.detector if replay_detector else None)

    def _finish_call(self, call: ToolCall, args: dict[str, Any]) -> bool:
        summary = str(args.get("summary") or "")
        reason = args.get("tests_changed_reason")
        self.log.append("tool_call", call_id=call.id, name="finish", args=args)
        result = self.checklist.run(
            summary=summary, tests_changed_reason=reason, allow_no_change=self.task.allow_no_change
        )
        self.log.append("checklist", **result.as_dict())
        self.state.checklist_history.append(result.as_dict())
        if result.passed:
            self._record(call, args, ok=True, text=result.model_text(), loop_out="finish-ok")
            self._finish(
                "completed",
                None,
                summary=summary,
                tests_changed_reason=reason,
                checklist=result.as_dict(),
            )
            return True
        self.state.checklist_rejections += 1
        self._record(
            call,
            args,
            ok=False,
            text=result.model_text(),
            loop_out=f"finish-rejected-{self.state.checklist_rejections}",
            loop_err=None,
        )
        if self.state.checklist_rejections >= self.config.agent.max_checklist_rejections:
            self._finish(
                "checklist_failed",
                f"finish rejected {self.state.checklist_rejections} times",
                summary=summary,
                tests_changed_reason=reason,
                checklist=result.as_dict(),
            )
            return True
        return False

    def _harness_message(self, content: str, *, reason: str) -> None:
        self.log.append("harness_message", content=content, reason=reason)
        self.state.messages.append({"role": "user", "content": f"[harness] {content}"})

    # ---------------------------------------------------------------- context
    def _estimate(self, messages: list[dict[str, Any]]) -> int:
        chars = len(json.dumps(messages)) + len(json.dumps(self.tools))
        return int(chars / 3.6 * self.state.token_ratio)

    def _calibrate(self, messages: list[dict[str, Any]], response: ChatResponse) -> None:
        if response.usage.estimated or response.usage.prompt_tokens <= 0:
            return
        raw = (len(json.dumps(messages)) + len(json.dumps(self.tools))) / 3.6
        if raw > 0:
            self.state.token_ratio = min(2.0, max(0.5, response.usage.prompt_tokens / raw))

    def _context(self) -> list[dict[str, Any]]:
        """The message list to send, compacted when it would not fit the window.

        Stage 1 clears the bodies of old tool results (keeping the latest ones); stage 2
        drops the oldest turns and inserts the progress file instead. The full history is
        never lost: it stays in the event log.
        """
        cfg = self.config.agent.context
        window = self.config.llm.context_window
        reserve = min(self.config.llm.max_tokens, window // 4)
        cap = min(cfg.target_tokens, int((window - reserve) * cfg.compact_at_fraction))
        budget = int(cap * (0.8**self._compact_level))
        messages = self.state.messages
        if self._estimate(messages) <= budget:
            return messages

        keep = max(2, cfg.keep_recent_tool_results >> self._compact_level)
        tool_positions = [i for i, m in enumerate(messages) if m["role"] == "tool"]
        recent = set(tool_positions[-keep:])
        cleared = []
        for i, message in enumerate(messages):
            if message["role"] == "tool" and i not in recent and len(message["content"]) > 240:
                message = {
                    **message,
                    "content": message["content"][:200]
                    + "\n[... older output cleared to save context; re-run the tool "
                    "if you need it ...]",
                }
            cleared.append(message)
        self.state.compactions += 1
        self.log.append(
            "context_compacted",
            stage=1,
            level=self._compact_level,
            est_before=self._estimate(messages),
            est_after=self._estimate(cleared),
        )
        if self._estimate(cleared) <= budget:
            return cleared

        head = cleared[:2]
        note = {
            "role": "user",
            "content": "[harness] "
            + prompts.COMPACTION_NOTE.format(progress=self._progress_text()),
        }
        starts = [i for i, m in enumerate(cleared) if i >= 2 and m["role"] == "assistant"]
        for start in starts:
            candidate = [*head, note, *cleared[start:]]
            if self._estimate(candidate) <= budget or start == starts[-1]:
                self.log.append(
                    "context_compacted",
                    stage=2,
                    dropped=start - 2,
                    est_after=self._estimate(candidate),
                )
                return candidate
        return [*head, note]

    # ---------------------------------------------------------------- progress
    def _progress_text(self) -> str:
        usage = self._usage()
        status = self.state.finished.status if self.state.finished else "running"
        return render_progress(
            task_id=self.task.task_id,
            title=self.task.title,
            status=status,
            budget_note=self.budget.remaining_note(usage),
            files=self.state.files,
            dbt_runs=self.state.dbt_runs,
            checklist=self.state.checklist_history,
            recent_calls=self.state.recent_calls,
            last_note=self.state.last_note,
            security=summarise_security(self.state.security_events),
        )

    def _write_progress(self, status: str) -> None:
        atomic_write_text(self.run_dir / "progress.md", self._progress_text())
