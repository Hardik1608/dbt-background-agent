"""Shared fixtures. Laptop tests need no Docker, GPU, network or API keys."""

from __future__ import annotations

import os

# never let a test reach a real model endpoint by accident
os.environ.pop("DBT_AGENT_LLM_BASE_URL", None)
