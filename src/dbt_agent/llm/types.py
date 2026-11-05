"""Provider-neutral chat types (OpenAI chat-completions wire format)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol


class LLMError(RuntimeError):
    """The model endpoint failed after retries (or returned something unusable)."""


class ContextLengthError(LLMError):
    """The request exceeded the model's context window; the caller should compact."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON text as produced by the model

    def parsed(self) -> tuple[dict[str, Any] | None, str | None]:
        """(arguments, None) or (None, error message for the model)."""
        text = (self.arguments or "").strip()
        if not text:
            return {}, None
        try:
            value = json.loads(text)
        except ValueError as exc:
            return None, f"Arguments are not valid JSON ({exc.msg} at position {exc.pos})."
        if isinstance(value, str):  # some models double-encode
            try:
                value = json.loads(value)
            except ValueError:
                return None, "Arguments must be a JSON object, got a string."
        if not isinstance(value, dict):
            return None, f"Arguments must be a JSON object, got {type(value).__name__}."
        return value, None

    def as_message_part(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated: bool = False

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class ChatResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str | None = None
    reasoning: str | None = None
    model: str | None = None
    latency_s: float = 0.0
    parsed_from_text: bool = False  # tool calls recovered from text content

    def assistant_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            message["tool_calls"] = [c.as_message_part() for c in self.tool_calls]
        return message

    def as_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "tool_calls": [c.as_message_part() for c in self.tool_calls],
            "usage": {
                "prompt_tokens": self.usage.prompt_tokens,
                "completion_tokens": self.usage.completion_tokens,
                "estimated": self.usage.estimated,
            },
            "finish_reason": self.finish_reason,
            "reasoning": self.reasoning,
            "model": self.model,
            "latency_s": round(self.latency_s, 3),
            "parsed_from_text": self.parsed_from_text,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChatResponse:
        usage = data.get("usage") or {}
        return cls(
            content=data.get("content"),
            tool_calls=[
                ToolCall(
                    id=c["id"],
                    name=c["function"]["name"],
                    arguments=c["function"].get("arguments") or "",
                )
                for c in data.get("tool_calls") or []
            ],
            usage=Usage(
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or 0),
                bool(usage.get("estimated")),
            ),
            finish_reason=data.get("finish_reason"),
            reasoning=data.get("reasoning"),
            model=data.get("model"),
            latency_s=float(data.get("latency_s") or 0.0),
            parsed_from_text=bool(data.get("parsed_from_text")),
        )


class ChatModel(Protocol):
    """Anything that can answer a chat request with optional tool calls."""

    model_name: str

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **overrides: Any,
    ) -> ChatResponse: ...
