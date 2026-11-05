# PROGRESS - dbt Background Engineering Agent

Spec: `01_Research_and_Guides/Portfolio_Gap_Analysis_2026-10.md` §7.4 (+ §6 production-ready).
Rules: never invent numbers (README/docs only from `reports/`); no git init/commit/push in the
project root; 02/04/05/06 are read-only; laptop tests need no Docker/GPU/network/keys.

## Status (2026-10-20 19:35 IST)

| Step | State |
|---|---|
| 1. Harness core | **done**. Laptop test suite and Docker isolation tests pass (`reports/tests_laptop.md`, `reports/docker_sandbox_tests.md`: 9/9 on the box) |
| 2. Sales task suite (30 + 20) | **done**: 50/50 valid (`reports/suite_validation.*`); 5 tasks carry prompt-injection canaries |
| 3. Real runs on the GPU box | **sales k=1 done**: local model **30/50 resolved (60%, Wilson 95% 46-72%)**; breakages 21/30, tickets 9/20; 5 wrong PRs (verifier caught 0, reviewer 1); 0 escape/egress attempts; 0/5 canaries followed (`reports/sales_local_agent_k1.*`). Oracle 50/50 (`reports/sales_oracle.*`) |
| 4. Spider2-DBT | **done**: **9/68 (13.2%)** with the official evaluate.py (`reports/spider2_dbt.*`); 7 instances unscoreable with the public gold data; 0 escape attempts, 44 refused `dbt deps` (`reports/spider2_security_events.json`) |
| 5. ADE-bench | **running**: the orchestrator paused the queue on 2026-10-15 (airbnb001-003 graded, airbnb004 cut short) and deleted the ADE task images on 2026-10-16; queue resumed 2026-10-20 19:19 IST, airbnb004 re-run from scratch (old trial in `_interrupted/`), images rebuilt on demand. About 56 tasks left (~9 h), then sales trials 2-3 (~6 h). Report -> `reports/adebench.*` |
| 6. Polish | README (results generated from `reports/` by `scripts/readme_results.py`, status section), DESIGN.md (measured costs, tuning disclosure), lint clean; CI workflow written (not run on GitHub: no repo) |

## Next

1. The box queue runs on its own: ADE-bench (60 tasks, started 13:28 IST, several hours),
   then sales trials 2-3 for pass^3 (last; only if time allows). If it
   stops (power cut), start `scripts/box/queue.sh` again (command in its header); finished
   stages are in `bench_data/queue.done`; every stage resumes per task.
2. When a stage finishes: `bash scripts/fetch_reports.sh`, update `reports/run_status.json`
   (remove the finished entry), `python scripts/readme_results.py`, then add one ADE bullet to
   "What the numbers say" in README.md (hand-written, numbers from `reports/adebench.md`),
   update the ADE row of the README status table and PROGRESS.md. pass^3 comes last and
   produces `reports/sales_local_agent_k3.*` (the generator picks it up).
3. After ADE: the stage removes our ADE images itself (`bench adebench cleanup`); check
   `docker images | grep ade-bench` and the disk footprint (13:32 IST: workspace 4.2 GB +
   images 1.3 GB, budget 6 GB; `scripts/box/slim_runs.sh` frees finished run directories;
   the Spider2 archives were deleted after the run, `bench spider2 setup` re-downloads them).

## GPU box rules (orchestrator, 2026-10-14; binding)

- Model server: `_shared/LOCAL_LLM.md`, `_shared/local_llm.env` (URL, key, model `local-agent`;
  Qwen3.5-9B int4 on vLLM, 32k context). Its KV cache (~80k tokens) is shared by all clients:
  keep each request under ~12k tokens (truncate tool output, compact history, summaries in the
  progress file); at most 4 concurrent requests; back off and retry on 503/timeouts; never
  retry a 400.
- RAM: before any sandbox or benchmark container run `free -m`; start only if "available" >=
  cap + 1024 MB (2 GB cap -> 3072 MB). One sandbox at a time unless available >= 5 GB.
- Heavy jobs (benchmark image builds, ADE-bench, batches of runs) take the shared lock:
  `mkdir ~/Hardik/Extras/.heavy_job.lock` (atomic); put project name, PID and start time in a
  file inside; if it exists, wait (poll every 2-5 min); release on exit with a trap; a lock
  older than 3 h whose PID is not running is stale. Implemented in
  `src/dbt_agent/bench/boxguard.py` and `scripts/box/heavy.sh`.
- Never touch `llm-server` (systemd user unit). Never use the GPU.
- Disk: our budget is 6 GB; keep >= 15 GB free (`df -h ~` before downloads). 28 GB free on
  2026-10-14 21:40, shared with the lakehouse project.
- Log every job in `_shared/BOX_USAGE.md` (append rows; change only our own Status).
- Each sandbox container: `--memory=2g`. Wrap host processes:
  `systemd-run --user --scope -p MemoryMax=2G -p MemorySwapMax=0 <cmd>`; detach long jobs with
  `(setsid nohup <cmd> > log 2>&1 < /dev/null &)`; long runs checkpoint per task and resume.

## How to resume

- Laptop venv: `.venv/Scripts/python.exe` (`py -3.11 -m venv .venv`; pinned deps in
  `requirements*.txt`). Tests: `.venv/Scripts/python.exe -m pytest -m "not docker and not llm"`
  (110 pass, 2 symlink tests skipped on Windows). Lint: `ruff check .` and `ruff format --check .`.
- Box workspace `~/Hardik/Extras/dbt-background-agent` (venv `.venv`, image
  `dbt-agent-sandbox:0.1`; ADE-bench venv in `bench_data/adebench/ade-bench/.venv`). Sync code
  with `bash scripts/sync_to_box.sh` (tar over ssh; never deletes box files; never sends
  `reports/`). Reports come back with `bash scripts/fetch_reports.sh`.
- Box runs: `scripts/box/queue.sh` runs the lines of `bench_data/queue.txt` in order (re-read
  between stages; done lines in `bench_data/queue.done`; log `bench_data/queue.log`). Stage
  scripts: `stage_model.sh` / `stage_sales.sh` (sales suite), `stage_spider2.sh`,
  `stage_ade.sh`, `stage_reports.sh` (rebuild reports with the current metrics code). See what
  runs: `ps -eo pid,etime,cmd | grep -E "[q]ueue.sh|[s]tage_|[d]bt_agent bench"` (to stop a
  job, kill by PID from a shell whose command line does not contain these words).
- README results: `python scripts/readme_results.py` (pending runs: `reports/run_status.json`).
- Base fixture: `python -m dbt_agent fixture build` (replay of 25 monthly batches, 72 s on the
  box). Suite validation: `scripts/box/run_validate.sh` (resumable).

## Decisions so far

- Harness tuning before the reportable model run (general, not task-specific; the first B01
  attempts are kept in `runs/_superseded/` on the box): read_file shows `<n><TAB><text>`
  (with `<n>| <text>` the model copied the space after the bar into old_string); edit_file
  accepts copied line numbers or a one-space shift when the result matches exactly once;
  `presence_penalty: 1.0` (range suggested in `_shared/LOCAL_LLM.md`) and a brevity rule in
  the system prompt, because replies that reasoned in circles hit max_tokens (2,048 tokens,
  about 80 s each) without a tool call. The sales model run restarted from scratch at
  23:35 IST with these settings.
- Turn caps raised (sales 35 -> 60, Spider2 50 -> 80, ADE 40 -> 60): after the latency fix a
  turn took about 6 s instead of about 30 s, so 35 turns ended runs after 2-6 minutes of a
  15-minute wall budget (3 of the first 10 tasks; 2 of them with a correct patch but no
  finish). Wall-time budgets unchanged. Run restarted from scratch 00:05 IST (the 10 earlier
  results are in `runs/_superseded/local_agent_turn_cap_35/` on the box).
- Headline metric: *resolved* = the agent finished (a verified PR exists) and the hidden
  tests pass; a correct patch from a run that ran out of budget is reported separately.

- Box lock pacing (`bench/boxguard.py` LockPacer): per-task locking re-took the lock within
  milliseconds, so other projects polling every 2-5 min never saw it free. After 40 min of
  holding it in a row, a batch now stays out for 310 s.
- Patches are read byte-exact (`util.read_text_exact`): `Path.read_text` turned CRLF lines into
  LF and the patch no longer applied (oracle B10/B11). File tools now keep each file's own line
  endings, so a one-line edit to a CRLF file is a one-line diff.
- Spider2: official `evaluate.py` unmodified, run once per instance with that instance's gold DB
  (per-instance scores, so the sum equals one run over all). Working DB name comes from the
  project's profiles.yml; `answer_or_path` is that file.
- ADE-bench: our agent is registered as ADE's `none` agent; ADE's own `.env.example` timeouts;
  our wall budget stops 90 s before ADE's agent timeout (ADE cannot kill the agent thread);
  network disconnected during the agent phase and reconnected for ADE's grading (some task
  test setups run `dbt deps`); trial container capped via ADE's compose file.

- All tools (file, dbt, DuckDB, git) run inside the sandbox through `src/dbt_agent/sandbox/toolbox.py`
  (stdlib + duckdb). The host never opens files or runs git in an agent work directory
  (symlink escape; `.git/config` fsmonitor/hooks injection). PRs are published from a repo built
  from the trusted template + patch text.
- Event-sourced run state (`events.jsonl`, fsync per event); resume restores the workspace to the
  last `git write-tree` snapshot and re-executes interrupted tool calls (exactly-once edits).
- Pre-completion checklist runs `dbt build -s state:modified+ --full-refresh` itself and blocks
  test weakening (count-based integrity key: test type x target x severity x filter).
- Context kept near 12k tokens per request (shared KV cache): tool output capped at 5,000 chars,
  last 4 tool results kept, older turns replaced by the progress file.
- Base fixture is replayed month by month so SCD2 history and the incremental fact are real
  (430 customers / 779 versions; 67,943 fact lines).
- Grading = hidden dbt tests + SQL assertions (from base/reference/static) + manifest checks +
  mutation tests (for "add a test" tickets) + collateral-change fingerprints vs the reference fix +
  regression list (start-passing nodes now failing or removed).

## User questions / actions

- None blocking. Optional: create the GitHub repository and push (not done here, by rule),
  so the CI workflow runs; a `GITHUB_TOKEN` to try `pr create --no-dry-run` on a test repo.
