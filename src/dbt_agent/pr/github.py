"""Optional: open the PR on GitHub. Dry run by default.

Real mode needs a token in the environment (``GITHUB_TOKEN`` by default; never a flag or a
file). It pushes the branch over HTTPS and opens a **draft** PR with a risk label. The token
is passed to git through an environment variable read by a credential helper, so it never
appears in a command line, a log or the event file.

The branch is pushed from a *publish repository* built from the trusted starting snapshot
plus the patch text (``prepare_publish_repo``) - never from the agent's work directory:
sandboxed code could have written ``.git/config`` there (``core.fsmonitor``, hooks), and
running host-side git in that directory would execute it.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from dbt_agent.util import force_rmtree

API = "https://api.github.com"
GIT = [
    "git",
    "-c",
    "core.autocrlf=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "user.name=dbt-background-agent",
    "-c",
    "user.email=agent@dbt-background-agent.invalid",
]


def prepare_publish_repo(
    template_dir: Path, patch: str, dest: Path, branch: str, message: str
) -> str:
    """Trusted copy of the starting snapshot + the patch, committed on ``branch``."""
    import re
    import shutil

    if re.search(r"^(?:new|old) mode 120000|^new file mode 120000", patch, re.MULTILINE):
        raise ValueError("refusing a patch that creates symlinks")
    force_rmtree(dest)
    shutil.copytree(
        template_dir,
        dest,
        symlinks=True,
        ignore=shutil.ignore_patterns(
            "*.duckdb", "*.duckdb.wal", "target", "logs", "dbt_packages", ".agent"
        ),
    )

    def git(*args: str, stdin: str | None = None) -> str:
        # bytes, not text mode: text mode would turn the patch's LF into CRLF on Windows
        proc = subprocess.run(
            [*GIT, *args],
            cwd=dest,
            input=stdin.encode("utf-8") if stdin is not None else None,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"git {args[0]} failed: {proc.stderr.decode('utf-8', 'replace')[-400:]}"
            )
        return proc.stdout.decode("utf-8", "replace").strip()

    git("checkout", "-q", "-B", branch)
    git("apply", "--whitespace=nowarn", "-", stdin=patch)
    git("add", "-A")
    git("commit", "-q", "-m", message)
    return git("rev-parse", "HEAD")


@dataclass
class GitHubResult:
    dry_run: bool
    url: str | None = None
    number: int | None = None
    plan: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


def plan_requests(
    repo: str, branch: str, base: str, title: str, body: str, risk: str
) -> list[dict[str, Any]]:
    return [
        {
            "action": "git push",
            "remote": f"https://github.com/{repo}.git",
            "refspec": f"{branch}:{branch}",
        },
        {
            "method": "POST",
            "url": f"{API}/repos/{repo}/pulls",
            "json": {"title": title, "head": branch, "base": base, "body": body, "draft": True},
        },
        {
            "method": "POST",
            "url": f"{API}/repos/{repo}/issues/{{number}}/labels",
            "json": {"labels": ["agent", f"risk:{risk}"]},
        },
    ]


def create_pull_request(
    *,
    repo: str,
    publish_dir: Path,
    branch: str,
    base: str,
    title: str,
    body: str,
    risk: str,
    dry_run: bool = True,
    token_env: str = "GITHUB_TOKEN",
    transport: httpx.BaseTransport | None = None,
    push: bool = True,
) -> GitHubResult:
    plan = plan_requests(repo, branch, base, title, body, risk)
    if dry_run:
        return GitHubResult(dry_run=True, plan=plan)
    token = os.environ.get(token_env)
    if not token:
        return GitHubResult(dry_run=False, plan=plan, error=f"${token_env} is not set")
    if push:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "AGENT_GH_TOKEN": token}
        helper = "!f() { echo username=x-access-token; echo password=$AGENT_GH_TOKEN; }; f"
        proc = subprocess.run(
            [
                *GIT,
                "-c",
                f"credential.helper={helper}",
                "push",
                f"https://github.com/{repo}.git",
                f"{branch}:{branch}",
            ],
            cwd=publish_dir,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return GitHubResult(
                dry_run=False,
                plan=plan,
                error="git push failed: " + proc.stderr.replace(token, "***")[-500:],
            )
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    with httpx.Client(headers=headers, timeout=30, transport=transport) as client:
        created = client.post(plan[1]["url"], json=plan[1]["json"])
        if created.status_code >= 300:
            return GitHubResult(
                dry_run=False,
                plan=plan,
                error=f"create PR: HTTP {created.status_code} {created.text[:300]}",
            )
        data = created.json()
        number = data.get("number")
        client.post(f"{API}/repos/{repo}/issues/{number}/labels", json=plan[2]["json"])
    return GitHubResult(dry_run=False, url=data.get("html_url"), number=number, plan=plan)
