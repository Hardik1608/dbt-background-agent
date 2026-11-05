"""Run budgets: turns, tokens, tool calls, wall time and (for paid APIs) euros.

Wall time counts *active* time summed over sessions, so a run resumed after a power cut
gets only what it had left - not a fresh budget.
"""

from __future__ import annotations

from dataclasses import dataclass

from dbt_agent.config import BudgetConfig, LLMConfig


@dataclass
class Usage:
    turns: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls: int = 0
    wall_seconds_previous_sessions: float = 0.0
    session_seconds: float = 0.0
    estimated_tokens: bool = False

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def wall_seconds(self) -> float:
        return self.wall_seconds_previous_sessions + self.session_seconds


class Budget:
    def __init__(self, config: BudgetConfig, llm: LLMConfig) -> None:
        self.config = config
        self.llm = llm

    def cost_eur(self, usage: Usage) -> float:
        return self.llm.cost_eur(usage.prompt_tokens, usage.completion_tokens)

    def exceeded(self, usage: Usage) -> str | None:
        """Name of the first exhausted budget, or None."""
        c = self.config
        if usage.turns >= c.max_turns:
            return f"max_turns ({c.max_turns})"
        if usage.tokens >= c.max_tokens:
            return f"max_tokens ({c.max_tokens:,})"
        if usage.tool_calls >= c.max_tool_calls:
            return f"max_tool_calls ({c.max_tool_calls})"
        if usage.wall_seconds >= c.max_wall_seconds:
            return f"max_wall_seconds ({c.max_wall_seconds:.0f})"
        if c.max_cost_eur is not None and self.cost_eur(usage) >= c.max_cost_eur:
            return f"max_cost_eur ({c.max_cost_eur})"
        return None

    def remaining_note(self, usage: Usage) -> str:
        c = self.config
        return (
            f"turn {usage.turns}/{c.max_turns}, tokens {usage.tokens:,}/{c.max_tokens:,}, "
            f"time {usage.wall_seconds / 60:.1f}/{c.max_wall_seconds / 60:.0f} min"
        )

    def nearly_exhausted(self, usage: Usage) -> bool:
        c = self.config
        return (
            usage.turns >= c.max_turns - 3
            or usage.wall_seconds >= 0.85 * c.max_wall_seconds
            or usage.tokens >= 0.85 * c.max_tokens
        )
