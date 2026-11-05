# ADE-bench (DuckDB, dbt)

- Model: `local-agent` through dbt-background-agent; one trial per task; graded by ADE-bench's own dbt tests.
- Resolved: **22/60** (36.7%)
- Agent loop status: {'budget_exhausted': 28, 'completed': 28, 'loop_detected': 4}; trials with the network removed during the agent phase: 60/60; escape / egress attempts: 1 / 8 (an egress attempt is a refused network-dependent command such as `dbt deps`).
- ADE settings: its `.env.example` timeouts (agent 900 s; our agent stops itself 90 s earlier); trial container capped at the sandbox memory and 2 CPUs.
- Context (external): dbt Labs reported 56% for OpenAI Codex with GPT-5.1 and the same for Claude Sonnet 4.5 (getdbt.com blog, 2026-01-21), on an earlier task set.

| Task | Resolved | Agent status | Failure mode |
|---|---|---|---|
| airbnb001 | no | budget_exhausted | unset |
| airbnb002 | no | loop_detected | unset |
| airbnb003 | yes | completed | unset |
| airbnb004 | no | budget_exhausted | unset |
| airbnb005 | no | completed | unset |
| airbnb006 | yes | completed | unset |
| airbnb007 | no | budget_exhausted | unset |
| airbnb008 | no | budget_exhausted | unset |
| airbnb009 | no | completed | unset |
| airbnb010 | no | budget_exhausted | unset |
| airbnb011 | no | loop_detected | unset |
| airbnb012 | no | completed | unset |
| airbnb013 | no | budget_exhausted | unset |
| analytics_engineering001 | yes | loop_detected | unset |
| analytics_engineering002 | yes | completed | unset |
| analytics_engineering003 | yes | completed | unset |
| analytics_engineering004 | no | completed | unset |
| analytics_engineering005 | no | completed | unset |
| analytics_engineering006 | no | completed | unset |
| analytics_engineering007 | yes | completed | unset |
| analytics_engineering008 | no | budget_exhausted | unset |
| asana001 | no | budget_exhausted | unset |
| asana002 | no | budget_exhausted | unset |
| asana003 | no | budget_exhausted | unset |
| asana004 | no | completed | unset |
| asana005 | no | budget_exhausted | unset |
| f1001 | no | budget_exhausted | unset |
| f1002 | no | budget_exhausted | unset |
| f1003 | no | budget_exhausted | unset |
| f1004 | yes | budget_exhausted | unset |
| f1005 | no | budget_exhausted | unset |
| f1006 | no | budget_exhausted | unset |
| f1007 | yes | budget_exhausted | unset |
| f1009 | no | budget_exhausted | unset |
| f1010 | yes | completed | unset |
| f1011 | no | completed | unset |
| helixops_saas001 | yes | completed | unset |
| helixops_saas002 | yes | completed | unset |
| helixops_saas003 | yes | completed | unset |
| helixops_saas004 | yes | completed | unset |
| helixops_saas005 | yes | completed | unset |
| helixops_saas006 | yes | completed | unset |
| helixops_saas007 | no | completed | unset |
| helixops_saas008 | yes | completed | unset |
| helixops_saas009 | no | completed | unset |
| helixops_saas010 | no | budget_exhausted | unset |
| helixops_saas011 | yes | completed | unset |
| helixops_saas012 | no | completed | unset |
| helixops_saas013 | yes | completed | unset |
| helixops_saas015 | no | budget_exhausted | unset |
| helixops_saas016 | yes | completed | unset |
| helixops_saas017 | yes | budget_exhausted | unset |
| helixops_saas018 | yes | completed | unset |
| intercom001 | no | budget_exhausted | unset |
| intercom002 | no | budget_exhausted | unset |
| intercom003 | no | budget_exhausted | unset |
| quickbooks001 | no | loop_detected | unset |
| quickbooks002 | yes | budget_exhausted | unset |
| quickbooks003 | no | budget_exhausted | unset |
| quickbooks004 | no | budget_exhausted | unset |
