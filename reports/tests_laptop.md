# Tests and the CI steps, run on the laptop

Laptop: Windows 11, Python 3.11.9, no Docker, no GPU, no network needed at test time, no API
keys. These are the steps of `.github/workflows/ci.yml`, run by hand (the workflow itself has
not run on GitHub: there is no repository yet). Date: 2026-10-15.

| CI job | Command | Result |
|---|---|---|
| lint | `ruff check .` and `ruff format --check .` (ruff 0.16.10) | clean (76 files) |
| test | `pytest -m "not docker and not llm"` | **110 passed, 2 skipped** in 529 s: 101 unit tests (2 skipped: creating symlinks needs extra rights on Windows; they run on Linux) and 11 dbt integration tests (real dbt + DuckDB, including a whole job from agent to PR with a scripted model) |
| smoke | `python -m dbt_agent bench sales --llm oracle --reviewer none --sandbox local --tasks B19 F07` | 2/2 resolved through agent, checklist, fresh-sandbox verifier, grader and PR packaging (`smoke ok: 2 of 2`) |
| docker | `pytest -m docker tests/docker` | not runnable on this laptop (no Docker); run on the GPU box: 9/9, see `reports/docker_sandbox_tests.md` |
| audit | `pip-audit -r requirements.txt -r requirements-dbt.txt` (pip-audit 2.10.1) | no known vulnerabilities found |
