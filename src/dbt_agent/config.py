"""Configuration (YAML + environment overrides), validated with pydantic.

Swapping the model is a config change only: point ``llm.base_url`` / ``llm.model`` at any
OpenAI-compatible endpoint and, for a hosted API, name the environment variable that holds
the key in ``llm.api_key_env``. Keys themselves never go into config files.

Environment overrides (handy on the GPU box): DBT_AGENT_LLM_BASE_URL, DBT_AGENT_LLM_MODEL,
DBT_AGENT_LLM_API_KEY_ENV, DBT_AGENT_SANDBOX (docker|local), DBT_AGENT_IMAGE.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field


class LLMConfig(BaseModel):
    base_url: str = "http://localhost:8000/v1"
    model: str = "local-model"
    api_key_env: str | None = None
    temperature: float = 0.7  # Qwen non-thinking defaults; greedy decoding loops
    top_p: float | None = 0.8
    max_tokens: int = 2048
    seed: int | None = None
    timeout_s: float = 300.0
    connect_timeout_s: float = 10.0
    max_retries: int = 7  # ~3 min of backoff: a server restart takes ~60 s
    backoff_scale: float = 1.0
    max_concurrency: int = 4
    extra_body: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    context_window: int = 32768
    # EUR per million tokens; 0 for a local model (no marginal API cost)
    price_input_per_mtok: float = 0.0
    price_output_per_mtok: float = 0.0
    parse_tool_calls_from_text: bool = True
    strip_thinking: bool = True

    def cost_eur(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (
            prompt_tokens * self.price_input_per_mtok
            + completion_tokens * self.price_output_per_mtok
        ) / 1_000_000


class BudgetConfig(BaseModel):
    max_turns: int = 40
    max_tokens: int = 1_200_000  # prompt + completion, summed over all calls
    max_wall_seconds: float = 1800.0
    max_tool_calls: int = 120
    max_cost_eur: float | None = None


class LoopConfig(BaseModel):
    window: int = 8
    repeat_threshold: int = 3  # identical call + identical result within the window
    error_repeat_threshold: int = 3  # the same error in a row
    oscillation_threshold: int = 2  # returning to an earlier workspace state
    stop_after_warnings: int = 2  # hard stop after this many warnings


class ContextConfig(BaseModel):
    # The shared model server's KV cache (~80k tokens) serves every client, so each request
    # is kept near `target_tokens` (12k) even though the window is 32k: old tool outputs are
    # cleared first, then old turns are replaced by the progress file.
    target_tokens: int = 12000
    compact_at_fraction: float = 0.70  # cap relative to (window - output reserve)
    keep_recent_tool_results: int = 4
    max_tool_output_chars: int = 5000


class AgentConfig(BaseModel):
    budgets: BudgetConfig = Field(default_factory=BudgetConfig)
    loop: LoopConfig = Field(default_factory=LoopConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    tools: list[str] = Field(
        default_factory=lambda: [
            "list_files",
            "read_file",
            "search_files",
            "write_file",
            "edit_file",
            "delete_file",
            "run_dbt",
            "query_duckdb",
            "git_diff",
        ]
    )
    max_checklist_rejections: int = 3
    max_text_only_turns: int = 3
    checklist_build: bool = True  # run dbt before accepting finish (see below)
    # what the checklist runs: `dbt <command> [-s <select>] [--full-refresh]`; an empty select
    # (or a project without a baseline manifest) runs the whole project
    checklist_command: str = "build"
    checklist_select: str = "state:modified+"
    checklist_full_refresh: bool = True
    require_change: bool = True
    tool_timeout_s: float = 300.0
    file_tool_timeout_s: float = 60.0


class SandboxConfig(BaseModel):
    kind: Literal["docker", "local"] = "docker"
    image: str = "dbt-agent-sandbox:0.1"
    memory: str = "2g"
    cpus: str = "2"
    pids_limit: int = 512
    network: str = "none"
    runtime: str | None = None
    dbt_timeout_s: float = 300.0
    query_timeout_s: float = 60.0


class VerifyConfig(BaseModel):
    enabled: bool = True
    slim_ci: bool = True
    reconciliation_tests: list[str] = Field(default_factory=list)
    # model name -> watermark column used by the incremental = full-refresh check
    incremental_models: dict[str, str] = Field(default_factory=dict)
    sqlfluff: bool = True
    fingerprint_exclude_columns: list[str] = Field(default_factory=list)
    timeout_s: float = 900.0


class ReviewerConfig(BaseModel):
    enabled: bool = True
    llm: LLMConfig | None = None  # defaults to the agent's model
    max_diff_chars: int = 12000


class RunConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    verify: VerifyConfig = Field(default_factory=VerifyConfig)
    reviewer: ReviewerConfig = Field(default_factory=ReviewerConfig)
    runs_dir: str = "runs"

    @property
    def reviewer_llm(self) -> LLMConfig:
        return self.reviewer.llm or self.llm


def _deep_update(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_update(out[key], value)
        else:
            out[key] = value
    return out


def load_config(
    paths: list[Path] | None = None,
    overrides: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
) -> RunConfig:
    """Merge YAML files (later wins), then environment overrides, then explicit overrides."""
    env = dict(os.environ if env is None else env)
    data: dict[str, Any] = {}
    for path in paths or []:
        loaded = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        data = _deep_update(data, loaded)
    env_map = {
        "DBT_AGENT_LLM_BASE_URL": ("llm", "base_url"),
        "DBT_AGENT_LLM_MODEL": ("llm", "model"),
        "DBT_AGENT_LLM_API_KEY_ENV": ("llm", "api_key_env"),
        "DBT_AGENT_SANDBOX": ("sandbox", "kind"),
        "DBT_AGENT_IMAGE": ("sandbox", "image"),
    }
    for var, (section, key) in env_map.items():
        if env.get(var):
            data.setdefault(section, {})[key] = env[var]
    if overrides:
        data = _deep_update(data, overrides)
    return RunConfig.model_validate(data)
