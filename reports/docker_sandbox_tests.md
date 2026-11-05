# Docker sandbox isolation tests (GPU box)

- Command: `pytest -m docker tests/docker -q`, started through `scripts/box/heavy.sh`
  (RAM check, then the shared heavy-job lock)
- Machine: the GPU box (Linux, Docker 29.7.2), image `dbt-agent-sandbox:0.1`
- Finished: 2026-10-15 02:02:55 IST (log `bench_data/docker_tests.log` on the box)
- Result: **9 passed in 7.47 s**

| Test (`tests/docker/test_docker_sandbox.py`) | What it asserts |
|---|---|
| `test_isolation_settings_are_applied` | read back with `docker inspect`: network mode `none`; memory cap equal to the memory+swap cap (no swap); read-only root filesystem; `--cap-drop ALL`; `no-new-privileges`; pids limit 512; non-root user; `/work` is the only writable mount |
| `test_tools_and_dbt_run_inside` | the toolbox answers inside the container (Python 3.11, dbt present); `dbt build` of a small project exits 0; `query_duckdb` reads the result |
| `test_no_network_egress` | from inside, TCP connections to 1.1.1.1:53, the model server (192.168.1.66:8100) and pypi.org:443 all fail |
| `test_filesystem_is_read_only_outside_work` | writes to `/etc`, `/opt/toolbox/toolbox.py` and `/usr/local` fail; a write to `/work` succeeds |
| `test_tmp_is_noexec` | a script written to `/tmp` and made executable cannot be run |
| `test_symlink_planted_by_sandboxed_code_cannot_escape` | links created inside the container to `/etc/passwd` and `/etc` are refused by `read_file`; host-side clean-up does not follow a link to a host directory |
| `test_hang_recovery_recreates_the_container` | a tool call that outlives the host deadline returns an error, the container is recreated under the same name, and the work directory survives |
| `test_stop_removes_only_our_container` | `stop()` leaves no container with this sandbox's name |
| `test_attached_container_is_isolated_at_agent_start` | ADE-bench path: a container started by someone else, with a network, has no networks and a 1 GB memory cap after `start()`; the toolbox works and an outbound connection is blocked |
