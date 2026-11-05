# ADE-bench (DuckDB, dbt)

- Model: `local-agent` through dbt-background-agent; one trial per task; graded by ADE-bench's own dbt tests.
- Resolved: **1/1** (100.0%)
- Agent loop status: {'budget_exhausted': 1}; trials with the network removed during the agent phase: 1/1; escape / egress attempts: 0 / 1 (an egress attempt is a refused network-dependent command such as `dbt deps`).
- ADE settings: its `.env.example` timeouts (agent 900 s; our agent stops itself 90 s earlier); trial container capped at the sandbox memory and 2 CPUs.
- Context (external): dbt Labs reported 56% for OpenAI Codex with GPT-5.1 and the same for Claude Sonnet 4.5 (getdbt.com blog, 2026-01-21), on an earlier task set.

| Task | Resolved | Agent status | Failure mode |
|---|---|---|---|
| airbnb001 | yes | budget_exhausted | unset |
