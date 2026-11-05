"""OpenAI-compatible client: parsing, retries, context errors, text tool-call recovery."""

from __future__ import annotations

import json

import httpx
import pytest

from dbt_agent.config import LLMConfig
from dbt_agent.llm.client import OpenAICompatClient, parse_text_tool_calls, split_thinking
from dbt_agent.llm.types import ContextLengthError, LLMError, ToolCall


def make_client(handler, **overrides) -> OpenAICompatClient:
    config = LLMConfig(
        base_url=f"http://test-{id(handler)}/v1",
        model="m",
        backoff_scale=0.0,
        max_retries=3,
        **overrides,
    )
    return OpenAICompatClient(config, transport=httpx.MockTransport(handler))


def completion(message: dict, usage: dict | None = None) -> dict:
    return {
        "choices": [{"message": message, "finish_reason": "stop"}],
        "usage": usage or {"prompt_tokens": 100, "completion_tokens": 20},
        "model": "m",
    }


def test_native_tool_calls_are_parsed():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json=completion(
                {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": '{"path": "a.sql"}'},
                        }
                    ],
                }
            ),
        )

    client = make_client(handler)
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]
    response = client.chat([{"role": "user", "content": "hi"}], tools)
    assert response.tool_calls[0].name == "read_file"
    assert response.tool_calls[0].parsed() == ({"path": "a.sql"}, None)
    assert response.usage.prompt_tokens == 100 and not response.usage.estimated
    assert seen["body"]["tool_choice"] == "auto" and seen["body"]["model"] == "m"


def test_api_key_comes_from_the_named_env_var(monkeypatch):
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=completion({"content": "ok"}))

    monkeypatch.setenv("MY_TEST_KEY", "secret-123")
    make_client(handler, api_key_env="MY_TEST_KEY").chat([{"role": "user", "content": "x"}])
    assert seen["auth"] == "Bearer secret-123"


def test_retries_transient_errors_then_succeeds():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json=completion({"content": "done"}))

    response = make_client(handler).chat([{"role": "user", "content": "x"}])
    assert response.content == "done" and attempts["n"] == 3


def test_gives_up_after_max_retries():
    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(LLMError, match="after 4 attempts"):
        make_client(handler).chat([{"role": "user", "content": "x"}])


def test_context_length_error_is_typed():
    def handler(request):
        return httpx.Response(
            400, json={"error": {"message": "This model's maximum context length is 32768 tokens."}}
        )

    with pytest.raises(ContextLengthError):
        make_client(handler).chat([{"role": "user", "content": "x"}])


def test_other_client_errors_are_not_retried():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        return httpx.Response(401, text="unauthorised")

    with pytest.raises(LLMError, match="401"):
        make_client(handler).chat([{"role": "user", "content": "x"}])
    assert attempts["n"] == 1


def test_tool_calls_written_as_text_are_recovered():
    hermes = (
        'I will read it.\n<tool_call>\n{"name": "read_file", "arguments": '
        '{"path": "models/a.sql"}}\n</tool_call>'
    )

    def handler(request):
        return httpx.Response(200, json=completion({"content": hermes}))

    response = make_client(handler).chat([{"role": "user", "content": "x"}])
    assert response.parsed_from_text
    assert response.tool_calls[0].parsed()[0] == {"path": "models/a.sql"}
    assert response.content == "I will read it."


def test_qwen_xml_tool_calls_are_recovered():
    text = (
        "<tool_call>\n<function=run_dbt>\n<parameter=command>\nbuild\n</parameter>\n"
        "<parameter=full_refresh>\ntrue\n</parameter>\n</function>\n</tool_call>"
    )
    calls, rest = parse_text_tool_calls(text)
    assert calls[0].name == "run_dbt"
    assert calls[0].parsed()[0] == {"command": "build", "full_refresh": True}
    assert rest == ""


def test_thinking_is_moved_out_of_content():
    content, reasoning = split_thinking("<think>plan: read file</think>Reading now.")
    assert content == "Reading now." and reasoning == "plan: read file"
    content, reasoning = split_thinking("dangling plan</think>Answer")
    assert content == "Answer" and reasoning == "dangling plan"


def test_missing_usage_is_estimated():
    def handler(request):
        body = completion({"content": "hello"})
        body.pop("usage")
        return httpx.Response(200, json=body)

    response = make_client(handler).chat([{"role": "user", "content": "x" * 400}])
    assert response.usage.estimated and response.usage.prompt_tokens > 50


def test_tool_call_argument_errors_are_explained():
    assert ToolCall("1", "x", "{bad json").parsed()[1].startswith("Arguments are not valid JSON")
    assert ToolCall("1", "x", "[1, 2]").parsed()[1].startswith("Arguments must be a JSON object")
    assert ToolCall("1", "x", json.dumps(json.dumps({"a": 1}))).parsed() == ({"a": 1}, None)
    assert ToolCall("1", "x", "").parsed() == ({}, None)
