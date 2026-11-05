"""Model-agnostic LLM layer: any OpenAI-compatible endpoint, plus a scripted fake."""

from dbt_agent.llm.client import OpenAICompatClient
from dbt_agent.llm.fake import FakeLLM
from dbt_agent.llm.types import (
    ChatModel,
    ChatResponse,
    ContextLengthError,
    LLMError,
    ToolCall,
    Usage,
)

__all__ = [
    "ChatModel",
    "ChatResponse",
    "ContextLengthError",
    "FakeLLM",
    "LLMError",
    "OpenAICompatClient",
    "ToolCall",
    "Usage",
]
