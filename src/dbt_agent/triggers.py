"""Triggers: a labelled GitHub issue or a failed CI run becomes a job request, and a small
file-based queue hands jobs to a worker one at a time.

* Webhook payloads are authenticated with GitHub's HMAC (``X-Hub-Signature-256``) before
  anything is parsed.
* Issue bodies and CI logs are untrusted text. They become the ticket the agent reads, so
  they are a prompt-injection channel; containment (sandbox, no network, read-only DB,
  checklist, fresh-sandbox verification, human merge) is what makes that acceptable.
* The queue is idempotent: a job id is accepted once (``pending/`` -> ``running/`` ->
  ``done/`` or ``failed/`` by atomic renames), so a redelivered webhook does not start a
  second run.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from dbt_agent.util import atomic_write_json, utc_now


@dataclass
class JobRequest:
    job_id: str
    kind: Literal["ticket", "ci_failure"]
    title: str
    body: str
    repo: str | None = None
    ref: str | None = None
    source: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """GitHub webhook HMAC-SHA256 check (constant-time)."""
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def _safe_id(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")[:120]


def job_from_github_event(
    event: str, payload: dict[str, Any], *, label: str = "dbt-agent"
) -> JobRequest | None:
    """Map a webhook to a job, or None if the event is not for the agent."""
    repo = (payload.get("repository") or {}).get("full_name")
    if event == "issues" and payload.get("action") == "labeled":
        if (payload.get("label") or {}).get("name") != label:
            return None
        issue = payload.get("issue") or {}
        return JobRequest(
            job_id=_safe_id(f"gh-{repo}-issue-{issue.get('number')}"),
            kind="ticket",
            title=str(issue.get("title") or "untitled issue")[:200],
            body=str(issue.get("body") or "")[:20000],
            repo=repo,
            source={"issue": issue.get("number"), "url": issue.get("html_url")},
        )
    if event == "workflow_run" and payload.get("action") == "completed":
        run = payload.get("workflow_run") or {}
        if run.get("conclusion") != "failure":
            return None
        body = (
            f"The CI workflow `{run.get('name')}` failed on branch `{run.get('head_branch')}` "
            f"at commit {run.get('head_sha')}. Reproduce with `dbt build` and fix the "
            f"project. Run: {run.get('html_url')}"
        )
        return JobRequest(
            job_id=_safe_id(f"gh-{repo}-run-{run.get('id')}"),
            kind="ci_failure",
            title=f"CI failed: {run.get('name')} on {run.get('head_branch')}"[:200],
            body=body,
            repo=repo,
            ref=run.get("head_sha"),
            source={"run_id": run.get("id"), "url": run.get("html_url")},
        )
    return None


class FileQueue:
    STATES = ("pending", "running", "done", "failed")

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        for state in self.STATES:
            (self.root / state).mkdir(parents=True, exist_ok=True)

    def _path(self, state: str, job_id: str) -> Path:
        return self.root / state / f"{_safe_id(job_id)}.json"

    def state_of(self, job_id: str) -> str | None:
        return next((s for s in self.STATES if self._path(s, job_id).exists()), None)

    def submit(self, request: JobRequest) -> bool:
        """False when the job id was seen before (redelivered webhook)."""
        if self.state_of(request.job_id) is not None:
            return False
        atomic_write_json(self._path("pending", request.job_id), asdict(request))
        return True

    def claim(self) -> JobRequest | None:
        for path in sorted((self.root / "pending").glob("*.json")):
            target = self.root / "running" / path.name
            try:
                path.rename(target)  # atomic: only one worker wins
            except OSError:
                continue
            return JobRequest(**json.loads(target.read_text(encoding="utf-8")))
        return None

    def finish(self, job_id: str, *, ok: bool, result: dict[str, Any] | None = None) -> None:
        source = self._path("running", job_id)
        target = self._path("done" if ok else "failed", job_id)
        data = json.loads(source.read_text(encoding="utf-8"))
        data["result"] = result or {}
        data["finished_at"] = utc_now()
        atomic_write_json(target, data)
        source.unlink()

    def recover(self) -> int:
        """After a crash, put jobs that were running back in the queue (they resume)."""
        moved = 0
        for path in (self.root / "running").glob("*.json"):
            path.rename(self.root / "pending" / path.name)
            moved += 1
        return moved
