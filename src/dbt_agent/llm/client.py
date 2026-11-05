"""OpenAI-compatible chat client (vLLM, llama.cpp server, Ollama, LiteLLM, OpenAI, ...).

Plain httpx instead of a vendor SDK, so one code path serves every provider and every
retry/timeout decision is visible here:

* retries with exponential backoff and jitter on transport errors, 408/409/425/429/5xx,
  honouring ``Retry-After``;
* a process-wide semaphore per endpoint caps concurrent requests (the shared local
  model server allows at most 4);
* the API key is read from an environment variable named in the config - keys never
  appear in config files, logs or the sandbox;
* tool calls that a local model printed as text (Hermes ``<tool_call>{json}</tool_call>``
  or Qwen3-Coder ``<function=...>`` XML) are recovered, and ``<think>`` blocks are moved
  out of the content.
"""

from __future__ import annotations

import contextlib
import json
import os
import random
import re
import threading
import time
import uuid
from typing import Any

import httpx

from dbt_agent.llm.types import ChatResponse, ContextLengthError, LLMError, ToolCall, Usage
from dbt_agent.util import estimate_tokens

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
CONTEXT_ERROR_RE = re.compile(
    r"maximum context length|context_length_exceeded|context length|prompt is too long|"
    r"too many tokens|exceeds the model's context|input tokens exceed",
    re.IGNORECASE,
)
THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)
TOOL_CALL_BLOCK_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)
FUNCTION_XML_RE = re.compile(r"<function=([\w.-]+)>\s*(.*?)\s*</function>", re.DOTALL)
PARAMETER_XML_RE = re.compile(r"<parameter=([\w.-]+)>\n?(.*?)\n?</parameter>", re.DOTALL)

_SEMAPHORES: dict[str, threading.BoundedSemaphore] = {}
_SEMAPHORE_LOCK = threading.Lock()


def endpoint_semaphore(base_url: str, limit: int) -> threading.BoundedSemaphore:
    with _SEMAPHORE_LOCK:
        if base_url not in _SEMAPHORES:
            _SEMAPHORES[base_url] = threading.BoundedSemaphore(max(1, limit))
        return _SEMAPHORES[base_url]


def split_thinking(content: str | None) -> tuple[str | None, str | None]:
    """Move <think>...</think> (or a dangling '...</think>' prefix) out of the content."""
    if not content:
        return content, None
    thoughts = THINK_RE.findall(content)
    text = THINK_RE.sub("", content)
    if "</think>" in text:  # template opened <think> in the prompt
        head, _, text = text.partition("</think>")
        thoughts.insert(0, head)
    reasoning = "\n".join(t.strip() for t in thoughts if t.strip()) or None
    return text.strip(), reasoning


def _coerce_param(value: str) -> Any:
    stripped = value.strip()
    if (
        stripped[:1] in "{["
        or stripped in {"true", "false", "null"}
        or re.fullmatch(r"-?\d+(\.\d+)?", stripped)
    ):
        try:
            return json.loads(stripped)
        except ValueError:
            return value
    return value


def parse_text_tool_calls(content: str) -> tuple[list[ToolCall], str]:
    """Recover tool calls a model wrote into its text. Returns (calls, remaining text)."""
    calls: list[ToolCall] = []
    remaining = content

    def add(name: str, arguments: dict[str, Any]) -> None:
        calls.append(
            ToolCall(id=f"call_{uuid.uuid4().hex[:10]}", name=name, arguments=json.dumps(arguments))
        )

    for body in TOOL_CALL_BLOCK_RE.findall(content):
        body = body.strip()
        if body.startswith("{"):
            try:
                obj = json.loads(body)
            except ValueError:
                continue
            name = obj.get("name") or (obj.get("function") or {}).get("name")
            args = obj.get("arguments", obj.get("parameters", {}))
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            if name:
                add(str(name), args if isinstance(args, dict) else {})
        else:
            for fname, fbody in FUNCTION_XML_RE.findall(body):
                add(fname, {k: _coerce_param(v) for k, v in PARAMETER_XML_RE.findall(fbody)})
    if calls:
        remaining = TOOL_CALL_BLOCK_RE.sub("", content).strip()
        return calls, remaining
    for fname, fbody in FUNCTION_XML_RE.findall(content):
        add(fname, {k: _coerce_param(v) for k, v in PARAMETER_XML_RE.findall(fbody)})
    if calls:
        remaining = FUNCTION_XML_RE.sub("", content).strip()
    return calls, remaining


class OpenAICompatClient:
    """Chat client for any OpenAI-compatible ``/chat/completions`` endpoint."""

    def __init__(self, config: Any, *, transport: httpx.BaseTransport | None = None) -> None:
        self.config = config
        self.model_name = config.model
        self.base_url = config.base_url.rstrip("/")
        api_key = os.environ.get(config.api_key_env, "") if config.api_key_env else ""
        headers = {"Content-Type": "application/json", **(config.headers or {})}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.Client(
            headers=headers,
            timeout=httpx.Timeout(config.timeout_s, connect=config.connect_timeout_s),
            transport=transport,
        )
        self._semaphore = endpoint_semaphore(self.base_url, config.max_concurrency)

    def close(self) -> None:
        self._client.close()

    def _body(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        overrides: dict[str, Any],
    ) -> dict[str, Any]:
        cfg = self.config
        body: dict[str, Any] = {
            "model": cfg.model,
            "messages": messages,
            "temperature": overrides.get("temperature", cfg.temperature),
            "max_tokens": overrides.get("max_tokens", cfg.max_tokens),
        }
        if cfg.top_p is not None:
            body["top_p"] = cfg.top_p
        if cfg.seed is not None:
            body["seed"] = overrides.get("seed", cfg.seed)
        if tools:
            body["tools"] = tools
            body["tool_choice"] = overrides.get("tool_choice", "auto")
        body.update(cfg.extra_body or {})
        if overrides.get("response_format"):
            body["response_format"] = overrides["response_format"]
        return body

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **overrides: Any,
    ) -> ChatResponse:
        body = self._body(messages, tools, overrides)
        url = f"{self.base_url}/chat/completions"
        last_error = "no attempt made"
        for attempt in range(self.config.max_retries + 1):
            started = time.monotonic()
            try:
                with self._semaphore:
                    response = self._client.post(url, json=body)
            except httpx.TransportError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                self._sleep(attempt, None)
                continue
            latency = time.monotonic() - started
            if response.status_code in RETRYABLE_STATUS:
                last_error = f"HTTP {response.status_code}: {response.text[:300]}"
                self._sleep(attempt, response.headers.get("retry-after"))
                continue
            if response.status_code >= 400:
                text = response.text[:2000]
                if CONTEXT_ERROR_RE.search(text):
                    raise ContextLengthError(text)
                raise LLMError(f"HTTP {response.status_code}: {text}")
            try:
                payload = response.json()
            except ValueError:
                last_error = "response was not JSON"
                self._sleep(attempt, None)
                continue
            return self._parse(payload, messages, latency)
        raise LLMError(
            f"model endpoint failed after {self.config.max_retries + 1} attempts: {last_error}"
        )

    def _sleep(self, attempt: int, retry_after: str | None) -> None:
        if attempt >= self.config.max_retries:
            return
        delay = min(60.0, 2.0 ** (attempt + 1)) + random.uniform(0, 1)  # 2,4,..,60 s
        if retry_after:
            with contextlib.suppress(ValueError):
                delay = max(delay, min(120.0, float(retry_after)))
        time.sleep(delay * self.config.backoff_scale)

    def _parse(
        self, payload: dict[str, Any], messages: list[dict[str, Any]], latency: float
    ) -> ChatResponse:
        choices = payload.get("choices") or []
        if not choices:
            raise LLMError(f"response has no choices: {str(payload)[:300]}")
        choice = choices[0]
        message = choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, list):  # content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        reasoning = message.get("reasoning_content") or message.get("reasoning")
        if self.config.strip_thinking:
            content, inline_reasoning = split_thinking(content)
            reasoning = reasoning or inline_reasoning
        calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            function = raw.get("function") or {}
            arguments = function.get("arguments")
            if isinstance(arguments, (dict, list)):
                arguments = json.dumps(arguments)
            calls.append(
                ToolCall(
                    id=raw.get("id") or f"call_{uuid.uuid4().hex[:10]}",
                    name=str(function.get("name") or ""),
                    arguments=arguments or "",
                )
            )
        parsed_from_text = False
        if not calls and content and self.config.parse_tool_calls_from_text:
            calls, rest = parse_text_tool_calls(content)
            if calls:
                content, parsed_from_text = rest, True
        usage_raw = payload.get("usage") or {}
        if usage_raw.get("prompt_tokens") is not None:
            usage = Usage(
                int(usage_raw.get("prompt_tokens") or 0),
                int(usage_raw.get("completion_tokens") or 0),
            )
        else:
            prompt_chars = sum(len(json.dumps(m)) for m in messages)
            usage = Usage(
                estimate_tokens("x" * prompt_chars),
                estimate_tokens((content or "") + json.dumps([c.as_message_part() for c in calls])),
                estimated=True,
            )
        return ChatResponse(
            content=content,
            tool_calls=calls,
            usage=usage,
            finish_reason=choice.get("finish_reason"),
            reasoning=reasoning,
            model=payload.get("model"),
            latency_s=latency,
            parsed_from_text=parsed_from_text,
        )
