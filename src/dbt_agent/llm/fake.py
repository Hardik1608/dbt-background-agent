"""Scripted fake LLM for unit tests, CI smoke runs and the oracle agent.

A script is a list of steps. Each step is either a fixed reply or a function of the
conversation so far (so a test can react to tool output, e.g. "if the build failed, edit
the file"). Replies use the same ``ChatResponse`` type as the real client, so the harness
cannot tell them apart.

YAML scripts (for ``dbt-agent run --llm fake --fake-script x.yaml``)::

    - say: "Looking at the project."
      tools:
        - {name: list_files, args: {}}
    - tools:
        - {name: finish, args: {summary: "nothing to do"}}
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from dbt_agent.llm.types import ChatResponse, LLMError, ToolCall, Usage
from dbt_agent.util import estimate_tokens


@dataclass
class Step:
    content: str | None = None
    calls: list[tuple[str, dict[str, Any] | str]] = field(default_factory=list)


def tool(name: str, **args: Any) -> Step:
    return Step(calls=[(name, args)])


def tools(*calls: tuple[str, dict[str, Any]], say: str | None = None) -> Step:
    return Step(content=say, calls=list(calls))


def say(text: str) -> Step:
    return Step(content=text)


def raw_args(name: str, arguments: str) -> Step:
    """A tool call with literal (possibly malformed) JSON arguments."""
    return Step(calls=[(name, arguments)])


ScriptItem = Step | Callable[[list[dict[str, Any]]], Step]


class FakeLLM:
    """Replays a script; raises LLMError when it runs out (like a dead endpoint)."""

    def __init__(
        self,
        script: list[ScriptItem],
        *,
        model_name: str = "fake-llm",
        on_exhausted: Step | None = None,
    ) -> None:
        self.script = list(script)
        self.model_name = model_name
        self.position = 0
        self.requests: list[dict[str, Any]] = []
        self.on_exhausted = on_exhausted
        self._call_counter = 0

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **overrides: Any,
    ) -> ChatResponse:
        self.requests.append(
            {"messages": [dict(m) for m in messages], "tools": tools, "overrides": overrides}
        )
        if self.position >= len(self.script):
            if self.on_exhausted is None:
                raise LLMError("fake LLM script exhausted")
            item: ScriptItem = self.on_exhausted
        else:
            item = self.script[self.position]
            self.position += 1
        step = item(messages) if callable(item) else item
        calls = []
        for name, args in step.calls:
            self._call_counter += 1
            arguments = args if isinstance(args, str) else json.dumps(args)
            calls.append(ToolCall(id=f"call_{self._call_counter}", name=name, arguments=arguments))
        prompt = sum(len(json.dumps(m)) for m in messages)
        completion = len(step.content or "") + sum(len(c.arguments) for c in calls)
        return ChatResponse(
            content=step.content,
            tool_calls=calls,
            usage=Usage(
                estimate_tokens("x" * prompt),
                estimate_tokens("x" * max(completion, 1)),
                estimated=True,
            ),
            finish_reason="tool_calls" if calls else "stop",
            model=self.model_name,
        )

    @classmethod
    def from_yaml(cls, path: Path) -> FakeLLM:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
        steps = []
        for item in raw:
            calls = [(c["name"], c.get("args") or {}) for c in item.get("tools", [])]
            steps.append(Step(content=item.get("say"), calls=calls))
        return cls(steps)


def last_tool_output(messages: list[dict[str, Any]]) -> str:
    """Content of the most recent tool message (handy inside callable steps)."""
    for message in reversed(messages):
        if message.get("role") == "tool":
            return str(message.get("content") or "")
    return ""
