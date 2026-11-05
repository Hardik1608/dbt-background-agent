"""DockerSandbox on Linux: isolation settings, tools, egress and escape probes.

Run on a Linux host with Docker and the image built (`make image`):
    pytest -m docker tests/docker
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from dbt_agent.sandbox import DockerSandbox, SandboxContext, docker_available
from dbt_agent.workspace import copy_template, init_git_baseline, remove_databases

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not docker_available(), reason="needs a Linux Docker daemon"),
]
IMAGE = os.environ.get("DBT_AGENT_IMAGE", "dbt-agent-sandbox:0.1")
MINI = Path(__file__).parents[1] / "fixtures" / "mini_project"


@pytest.fixture
def box(tmp_path):
    root = copy_template(MINI, tmp_path / "work")
    init_git_baseline(root)
    sandbox = DockerSandbox(
        root,
        SandboxContext(db_path="warehouse.duckdb"),
        image=IMAGE,
        labels={"dbt-agent.test": "1"},
    )
    sandbox.start()
    yield sandbox
    sandbox.stop()


def test_isolation_settings_are_applied(box):
    sec = box.inspect_security()
    assert sec["network_mode"] == "none"
    assert sec["memory_bytes"] == 2 * 1024**3
    assert sec["memory_swap_bytes"] == sec["memory_bytes"]  # no swap
    assert sec["read_only_rootfs"] is True
    assert sec["cap_drop"] == ["ALL"]
    assert any("no-new-privileges" in s for s in sec["security_opt"])
    assert sec["pids_limit"] == 512
    assert sec["user"] and not sec["user"].startswith("0")
    writable = [m["destination"] for m in sec["mounts"] if m["rw"]]
    assert writable == ["/work"]


def test_tools_and_dbt_run_inside(box):
    ping = box.call("ping")
    assert ping.ok and ping.data["python"].startswith("3.11") and ping.data["dbt"]
    built = box.call("run_dbt", {"command": "build"})
    assert built.data["exit_code"] == 0, built.output
    query = box.call("query_duckdb", {"sql": "select count(*) as n from main.stg_orders"})
    assert query.ok and "4" in query.output


def _probe(box, code: str) -> str:
    result = box.exec(["python", "-c", code], timeout=60)
    return (result.stdout + result.stderr).strip()


def test_no_network_egress(box):
    out = _probe(
        box,
        (
            "import socket\n"
            "for host, port in [('1.1.1.1', 53), ('192.168.1.66', 8100), ('pypi.org', 443)]:\n"
            "    try:\n"
            "        socket.create_connection((host, port), timeout=3); print('OPEN', host)\n"
            "    except OSError as e:\n"
            "        print('BLOCKED', host, type(e).__name__)\n"
        ),
    )
    assert "OPEN" not in out
    assert out.count("BLOCKED") == 3


def test_filesystem_is_read_only_outside_work(box):
    out = _probe(
        box,
        (
            "import os\n"
            "for p in ['/etc/agent_was_here', '/opt/toolbox/toolbox.py', '/usr/local/x']:\n"
            "    try:\n"
            "        open(p, 'a').write('x'); print('WROTE', p)\n"
            "    except OSError as e:\n"
            "        print('DENIED', p)\n"
            "open('/work/ok.txt', 'w').write('fine'); print('WORK OK')\n"
        ),
    )
    assert "WROTE" not in out and out.count("DENIED") == 3 and "WORK OK" in out


def test_tmp_is_noexec(box):
    out = _probe(
        box,
        (
            "import os, stat, subprocess\n"
            "p = '/tmp/x.sh'; open(p, 'w').write('#!/bin/sh\\necho RAN\\n')\n"
            "os.chmod(p, 0o755)\n"
            "try:\n"
            "    print(subprocess.run([p], capture_output=True, text=True).stdout or 'NO OUTPUT')\n"
            "except PermissionError:\n"
            "    print('NOEXEC')\n"
        ),
    )
    assert "NOEXEC" in out and "RAN" not in out


def test_symlink_planted_by_sandboxed_code_cannot_escape(box, tmp_path):
    # simulate a malicious dbt Python model creating links to host paths
    _probe(
        box,
        "import os; os.symlink('/etc/passwd', '/work/models/leak.sql'); "
        "os.symlink('/etc', '/work/hostdir')",
    )
    leak = box.call("read_file", {"path": "models/leak.sql"})
    assert not leak.ok and "outside the project" in leak.error
    # the host-side cleanup must not follow links either
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.duckdb").write_text("host data", encoding="utf-8")
    (Path(box.host_workdir) / "victimlink").symlink_to(victim, target_is_directory=True)
    remove_databases(Path(box.host_workdir))
    assert (victim / "keep.duckdb").exists()


def test_hang_recovery_recreates_the_container(box):
    """A tool that outlives the host deadline: docker exec cannot kill it, so the
    container is recreated; the work directory (bind mount) survives."""
    box.HOST_GRACE_S = 1.0
    first_name = box.name
    (Path(box.host_workdir) / "marker.txt").write_text("kept", encoding="utf-8")
    response = box.call("ping", {"sleep_s": 30}, timeout=1)
    assert not response.ok and "restarted" in response.error
    assert box.name == first_name and box.call("ping").ok
    assert (Path(box.host_workdir) / "marker.txt").read_text(encoding="utf-8") == "kept"


def test_stop_removes_only_our_container(box):
    name = box.name
    box.stop()
    out = shutil.which("docker") and os.popen(f"docker ps -a --filter name={name} -q").read()
    assert not (out or "").strip()


def test_attached_container_is_isolated_at_agent_start(tmp_path):
    """The ADE-bench path: a container someone else created, with a network."""
    import subprocess
    import uuid

    from dbt_agent.sandbox.docker import AttachedContainerSandbox

    name = f"dbt-agent-attach-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "--label",
            "dbt-agent.managed=true",
            "--entrypoint",
            "sleep",
            IMAGE,
            "300",
        ],
        check=True,
        capture_output=True,
    )
    try:
        subprocess.run(
            ["docker", "exec", "-u", "0", name, "mkdir", "-p", "/app"],
            check=True,
            capture_output=True,
        )
        box = AttachedContainerSandbox(name, root="/app", memory="1g", python="python")
        before = box.network_mode()
        assert before["networks"], "the test container starts with a network"
        box.start()
        after = box.network_mode()
        assert after["networks"] == [] and after["memory_bytes"] == 1024**3
        assert box.call("ping").ok
        out = box.exec(
            [
                "python",
                "-c",
                "import socket\ntry:\n socket.create_connection(("
                "'1.1.1.1', 53), timeout=3); print('OPEN')\nexcept OSError:\n "
                "print('BLOCKED')",
            ],
            timeout=30,
        )
        assert "BLOCKED" in out.stdout
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
