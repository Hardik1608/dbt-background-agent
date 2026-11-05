"""Classify security events reported by the toolbox.

* escape - tried to reach files outside the work directory (blocked)
* egress - tried to reach the network: SQL/HTTP reads, ``dbt deps``, network errors seen in
  dbt output (blocked by policy or by ``--network none``)
* policy - tried something the sandbox forbids but that is not an escape (writing build
  directories or the database file, write SQL through the read-only query tool)
* info   - URLs or network imports written into files; not attempts by themselves
"""

from __future__ import annotations

from collections import Counter
from typing import Any

CATEGORY = {
    "path_escape_blocked": "escape",
    "symlink_blocked": "escape",
    "egress_attempt_blocked": "egress",
    "egress_attempt_failed": "egress",
    "protected_path_blocked": "policy",
    "write_attempt_blocked": "policy",
    "file_access_blocked": "policy",
    "egress_reference": "info",
    "network_code_reference": "info",
}


def categorise(event: dict[str, Any]) -> str:
    kind = event.get("kind", "")
    if kind == "blocked_command":
        return "egress" if "deps" in str(event.get("detail", "")) else "policy"
    return CATEGORY.get(kind, "info")


def summarise(events: list[dict[str, Any]]) -> dict[str, Any]:
    by_category: Counter[str] = Counter()
    by_kind: Counter[str] = Counter()
    for event in events:
        by_category[categorise(event)] += 1
        by_kind[event.get("kind", "?")] += 1
    return {
        "escape_attempts": by_category.get("escape", 0),
        "egress_attempts": by_category.get("egress", 0),
        "policy_blocks": by_category.get("policy", 0),
        "info": by_category.get("info", 0),
        "by_kind": dict(sorted(by_kind.items())),
    }
