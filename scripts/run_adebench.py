"""Run ADE-bench's own CLI with dbt-background-agent registered as the `none` agent.

Usage (inside ADE-bench's venv, from its repo root):
    DBT_AGENT_CONFIGS=configs/local_agent.yaml:configs/box.yaml:configs/adebench.yaml \
      python /path/to/scripts/run_adebench.py run airbnb001 --db duckdb --project-type dbt \
      --agent none --n-concurrent-trials 1
"""

from __future__ import annotations

import os
import sys

from ade_bench.agents.agent_factory import NamedAgentFactory
from ade_bench.agents.agent_name import AgentName

from dbt_agent.bench.adebench import make_agent_class

configs = [p for p in os.environ.get("DBT_AGENT_CONFIGS", "").split(os.pathsep) if p]
NamedAgentFactory.AGENT_NAME_TO_CLASS[AgentName.NONE] = make_agent_class(configs)

from ade_bench.cli.ab.main import app  # noqa: E402  (after the registration above)

sys.exit(app())
