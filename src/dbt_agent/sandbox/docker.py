"""DockerSandbox: one locked-down container per job; tools run via ``docker exec``.

Container settings (see docs/DESIGN.md, threat model):

* ``--network none``                 no interface except loopback: no egress, no LLM access
* ``--memory 2g --memory-swap 2g``   hard memory cap, no swap
* ``--cpus``, ``--pids-limit``       CPU share and fork-bomb protection
* ``--read-only`` + tmpfs ``/tmp``   immutable image; ``/tmp`` is noexec
* ``--cap-drop ALL``                 no Linux capabilities
* ``--security-opt no-new-privileges``
* ``--user <host uid>:<gid>``        never root; files in the work dir stay host-owned
* one bind mount, the work directory, at ``/work``; the toolbox script is mounted read-only

The agent loop, the LLM credentials and the event log stay on the host. Nothing secret is
ever passed into the container.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from dbt_agent.sandbox.base import TOOLBOX_PATH, ExecResult, Sandbox, SandboxContext, SandboxError

CONTAINER_TOOLBOX = "/opt/toolbox/toolbox.py"
CONTAINER_ROOT = "/work"
LABEL = "dbt-agent.managed"


class DockerSandbox(Sandbox):
    kind = "docker"
    is_security_boundary = True

    def __init__(
        self,
        host_workdir: Path,
        context: SandboxContext | None = None,
        *,
        image: str = "dbt-agent-sandbox:0.1",
        name: str | None = None,
        memory: str = "2g",
        cpus: str = "2",
        pids_limit: int = 512,
        tmpfs_size: str = "512m",
        network: str = "none",
        runtime: str | None = None,
        docker_bin: str = "docker",
        python: str = "python",
        labels: dict[str, str] | None = None,
        extra_mounts: list[tuple[Path, str]] | None = None,
        memory_guard_mb: int | None = None,
    ) -> None:
        super().__init__(host_workdir, context)
        self.image = image
        self.name = name or f"dbt-agent-{uuid.uuid4().hex[:12]}"
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.tmpfs_size = tmpfs_size
        self.network = network
        self.runtime = runtime
        self.docker_bin = docker_bin
        self.python = python
        self.labels = labels or {}
        self.extra_mounts = extra_mounts or []
        # shared-box rule: start a container only if MemAvailable >= cap + 1024 MB
        self.memory_guard_mb = memory_guard_mb

    @property
    def root(self) -> str:
        return CONTAINER_ROOT

    def toolbox_argv(self) -> list[str]:
        return [self.python, CONTAINER_TOOLBOX]

    # ------------------------------------------------------------------ docker CLI
    def _docker(
        self, *args: str, timeout: float = 120, check: bool = True, stdin: str | None = None
    ) -> subprocess.CompletedProcess:
        if shutil.which(self.docker_bin) is None:
            raise SandboxError("docker CLI not found")
        proc = subprocess.run(
            [self.docker_bin, *args],
            input=stdin.encode() if stdin is not None else None,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if check and proc.returncode != 0:
            raise SandboxError(
                f"docker {args[0]} failed: {proc.stderr.decode('utf-8', 'replace')[-800:]}"
            )
        return proc

    def run_argv(self) -> list[str]:
        """The exact ``docker run`` command (also written to the event log as evidence)."""
        uid = os.getuid() if hasattr(os, "getuid") else 1000
        gid = os.getgid() if hasattr(os, "getgid") else 1000
        argv = [
            "run",
            "-d",
            "--rm",
            "--init",
            "--name",
            self.name,
            "--network",
            self.network,
            "--memory",
            self.memory,
            "--memory-swap",
            self.memory,
            "--cpus",
            str(self.cpus),
            "--pids-limit",
            str(self.pids_limit),
            "--read-only",
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,noexec,size={self.tmpfs_size}",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            f"{uid}:{gid}",
            "--label",
            f"{LABEL}=true",
            "-e",
            "HOME=/tmp",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            "-e",
            "DBT_SEND_ANONYMOUS_USAGE_STATS=False",
            "-e",
            "DO_NOT_TRACK=1",
            "-v",
            f"{Path(self.host_workdir).resolve()}:{CONTAINER_ROOT}:rw",
            "-v",
            f"{TOOLBOX_PATH.resolve()}:{CONTAINER_TOOLBOX}:ro",
            "-w",
            CONTAINER_ROOT,
        ]
        for host_path, container_path in self.extra_mounts:
            argv += ["-v", f"{Path(host_path).resolve()}:{container_path}:ro"]
        for key, value in self.labels.items():
            argv += ["--label", f"{key}={value}"]
        if self.runtime:
            argv += ["--runtime", self.runtime]
        argv += [self.image, "sleep", "infinity"]
        return argv

    def start(self) -> None:
        if self.started:
            return
        self.host_workdir.mkdir(parents=True, exist_ok=True)
        if self.memory_guard_mb:
            from dbt_agent.bench.boxguard import wait_for_memory

            wait_for_memory(self.memory_guard_mb)
        # a container with our exact name can only be ours (e.g. left over by a crash)
        self._remove_own_container()
        self._docker(*self.run_argv(), timeout=180)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            state = self._docker("inspect", "-f", "{{.State.Running}}", self.name, check=False)
            if state.returncode == 0 and state.stdout.decode().strip() == "true":
                self.started = True
                return
            time.sleep(0.5)
        raise SandboxError(f"container {self.name} did not start")

    def _remove_own_container(self) -> None:
        probe = self._docker(
            "inspect", "-f", '{{index .Config.Labels "' + LABEL + '"}}', self.name, check=False
        )
        if probe.returncode == 0 and probe.stdout.decode().strip() == "true":
            self._docker("rm", "-f", self.name, check=False)

    def stop(self) -> None:
        if not self.started:
            return
        self._remove_own_container()
        self.started = False

    def exec(self, argv: list[str], *, timeout: float, stdin: str | None = None) -> ExecResult:
        started = time.monotonic()
        try:
            proc = subprocess.run(
                [self.docker_bin, "exec", "-i", self.name, *argv],
                input=stdin.encode("utf-8") if stdin is not None else None,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return ExecResult(
                -9,
                (exc.stdout or b"").decode("utf-8", "replace"),
                (exc.stderr or b"").decode("utf-8", "replace"),
                True,
                time.monotonic() - started,
            )
        return ExecResult(
            proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"),
            False,
            time.monotonic() - started,
        )

    def on_hang(self) -> None:
        """``docker exec`` timing out does not kill the process inside; recreate instead."""
        self._remove_own_container()
        self.started = False
        self.start()

    # ------------------------------------------------------------------ evidence
    def inspect_security(self) -> dict[str, Any]:
        """The applied isolation settings, read back from Docker (used in tests/reports)."""
        proc = self._docker("inspect", self.name)
        info = json.loads(proc.stdout.decode())[0]
        host = info["HostConfig"]
        return {
            "network_mode": host.get("NetworkMode"),
            "memory_bytes": host.get("Memory"),
            "memory_swap_bytes": host.get("MemorySwap"),
            "nano_cpus": host.get("NanoCpus"),
            "pids_limit": host.get("PidsLimit"),
            "read_only_rootfs": host.get("ReadonlyRootfs"),
            "cap_drop": host.get("CapDrop"),
            "security_opt": host.get("SecurityOpt"),
            "user": info["Config"].get("User"),
            "mounts": [
                {"destination": m.get("Destination"), "rw": m.get("RW")}
                for m in info.get("Mounts", [])
            ],
        }

    def describe(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "root": self.root,
            "image": self.image,
            "name": self.name,
            "run_argv": self.run_argv(),
        }


class AttachedContainerSandbox(Sandbox):
    """Tools run in a container that another harness created (e.g. an ADE-bench trial).

    We cannot set its run flags, so on ``start`` we tighten what can still be tightened at
    run time: disconnect it from every network (no egress during the agent phase), cap its
    memory, and copy the toolbox in. The container itself is never stopped or removed.
    """

    kind = "docker-attached"
    is_security_boundary = True
    CONTAINER_TOOLBOX = "/tmp/dbt_agent_toolbox.py"

    def __init__(
        self,
        container: str,
        root: str,
        context: SandboxContext | None = None,
        *,
        memory: str | None = "2g",
        isolate_network: bool = True,
        docker_bin: str = "docker",
        python: str = "python",
    ) -> None:
        super().__init__(Path(), context)
        self.container = container
        self._root = root
        self.memory = memory
        self.isolate_network = isolate_network
        self.docker_bin = docker_bin
        self.python = python
        self.disconnected: list[str] = []

    @property
    def root(self) -> str:
        return self._root

    def toolbox_argv(self) -> list[str]:
        return [self.python, self.CONTAINER_TOOLBOX]

    def _docker(
        self, *args: str, timeout: float = 120, check: bool = True
    ) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            [self.docker_bin, *args], capture_output=True, timeout=timeout, check=False
        )
        if check and proc.returncode != 0:
            raise SandboxError(
                f"docker {args[0]} failed: {proc.stderr.decode('utf-8', 'replace')[-600:]}"
            )
        return proc

    def start(self) -> None:
        if self.started:
            return
        info = json.loads(self._docker("inspect", self.container).stdout.decode())[0]
        if self.isolate_network:
            for network in (info.get("NetworkSettings") or {}).get("Networks") or {}:
                self._docker("network", "disconnect", "--force", network, self.container)
                self.disconnected.append(network)
        if self.memory:
            self._docker(
                "update",
                "--memory",
                self.memory,
                "--memory-swap",
                self.memory,
                self.container,
                check=False,
            )
        self._docker(
            "cp", str(TOOLBOX_PATH.resolve()), f"{self.container}:{self.CONTAINER_TOOLBOX}"
        )
        self.started = True

    def stop(self) -> None:
        self.started = False  # the owning harness removes the container

    def restore_network(self) -> list[str]:
        """Reconnect what ``start`` disconnected, once the agent phase is over, so the owning
        harness grades in the environment it built (ADE's test setup may run ``dbt deps``)."""
        restored = []
        for network in list(self.disconnected):
            if (
                self._docker("network", "connect", network, self.container, check=False).returncode
                == 0
            ):
                restored.append(network)
                self.disconnected.remove(network)
        return restored

    def exec(self, argv: list[str], *, timeout: float, stdin: str | None = None) -> ExecResult:
        started = time.monotonic()
        try:
            proc = subprocess.run(
                [self.docker_bin, "exec", "-i", "-w", self._root, self.container, *argv],
                input=stdin.encode("utf-8") if stdin is not None else None,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return ExecResult(
                -9,
                (exc.stdout or b"").decode("utf-8", "replace"),
                (exc.stderr or b"").decode("utf-8", "replace"),
                True,
                time.monotonic() - started,
            )
        return ExecResult(
            proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"),
            False,
            time.monotonic() - started,
        )

    def on_hang(self) -> None:
        """We do not own the container: kill only our toolbox processes inside it."""
        self._docker("exec", self.container, "pkill", "-f", self.CONTAINER_TOOLBOX, check=False)

    def network_mode(self) -> dict[str, Any]:
        info = json.loads(self._docker("inspect", self.container).stdout.decode())[0]
        return {
            "networks": list((info.get("NetworkSettings") or {}).get("Networks") or {}),
            "memory_bytes": info["HostConfig"].get("Memory"),
        }

    def describe(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "root": self.root,
            "container": self.container,
            "disconnected_networks": self.disconnected,
        }


def docker_available(docker_bin: str = "docker") -> bool:
    if shutil.which(docker_bin) is None:
        return False
    try:
        proc = subprocess.run(
            [docker_bin, "info", "--format", "{{.OSType}}"],
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and proc.stdout.decode().strip() == "linux"


def make_sandbox(
    kind: str, host_workdir: Path, context: SandboxContext | None = None, **docker_kwargs: Any
) -> Sandbox:
    if kind == "docker":
        return DockerSandbox(host_workdir, context, **docker_kwargs)
    if kind == "local":
        from dbt_agent.sandbox.local import LocalSandbox

        return LocalSandbox(host_workdir, context)
    raise ValueError(f"unknown sandbox kind {kind!r}")
