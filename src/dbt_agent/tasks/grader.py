"""Hidden acceptance grading of a patch against a task (in a fresh sandbox).

    copy the template -> add hidden tests -> apply the patch -> dbt build ->
    hidden dbt tests, SQL assertions, manifest checks, test integrity,
    collateral data changes (fingerprints vs the reference fix), regressions

``resolved`` needs every check to pass. ``regressions`` counts nodes that passed in the
starting state and fail (or vanished) after the patch, plus tables whose data changed
although the task did not ask for it.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dbt_agent.harness.integrity import compare_tests
from dbt_agent.tasks.fingerprint import table_fingerprints
from dbt_agent.tasks.model import Assertion, GradingStep, TaskSpec
from dbt_agent.tasks.templates import SuiteEnv, task_context
from dbt_agent.util import force_rmtree
from dbt_agent.workspace import remove_databases

HIDDEN_DIR = "tests/_hidden"
FINGERPRINT_SCHEMAS = ["silver", "gold", "snapshots"]


@dataclass
class GradeResult:
    task_id: str
    resolved: bool = False
    patch_applied: bool = False
    checks: dict[str, dict[str, Any]] = field(default_factory=dict)
    build: dict[str, Any] = field(default_factory=dict)
    regressions: list[str] = field(default_factory=list)
    collateral: list[str] = field(default_factory=list)
    test_flags: list[dict[str, Any]] = field(default_factory=list)
    recorded: dict[str, Any] = field(default_factory=dict)
    seconds: float = 0.0
    error: str | None = None

    def failed_checks(self) -> list[str]:
        return [name for name, c in self.checks.items() if not c.get("passed")]

    def as_dict(self) -> dict[str, Any]:
        out = dict(self.__dict__)
        out["failed_checks"] = self.failed_checks()
        return out


def _rows(response) -> list[list[Any]]:
    return response.data["results"][0]["rows"]


def _matches(actual: list[list[Any]], expected: Any, tolerance: float) -> bool:
    if isinstance(expected, list):
        if len(actual) != len(expected):
            return False
        return all(
            _matches([a], [e], tolerance)
            if not isinstance(e, list)
            else _row_equal(a, e, tolerance)
            for a, e in zip(actual, expected, strict=True)
        )
    value = actual[0][0] if actual and actual[0] else None
    return _scalar_equal(value, expected, tolerance)


def _scalar_equal(a: Any, b: Any, tolerance: float) -> bool:
    if a is None or b is None:
        return a is b
    try:
        return abs(float(a) - float(b)) <= tolerance
    except (TypeError, ValueError):
        return str(a) == str(b)


def _row_equal(a: list[Any], b: list[Any], tolerance: float) -> bool:
    return len(a) == len(b) and all(
        _scalar_equal(x, y, tolerance) for x, y in zip(a, b, strict=True)
    )


def _expected(assertion: Assertion, meta: dict[str, Any]) -> tuple[bool, Any]:
    exp = assertion.expect
    if exp.rows is not None:
        return True, {"rows": exp.rows}
    if exp.from_ == "base":
        return assertion.name in meta.get("base_values", {}), meta.get("base_values", {}).get(
            assertion.name
        )
    if exp.from_ == "reference":
        ref = (meta.get("reference") or {}).get("values", {})
        return assertion.name in ref, ref.get(assertion.name)
    return True, exp.value


def write_hidden_tests(task: TaskSpec, root: Path) -> None:
    hidden = root / HIDDEN_DIR
    hidden.mkdir(parents=True, exist_ok=True)
    for test in task.hidden.dbt_tests:
        (hidden / f"hidden__{test.name}.sql").write_text(
            test.sql.strip() + "\n", encoding="utf-8", newline="\n"
        )


def grade(
    task: TaskSpec,
    env: SuiteEnv,
    patch: str,
    workdir: Path,
    *,
    record_reference: bool = False,
    keep_workdir: bool = False,
) -> GradeResult:
    started = time.monotonic()
    meta = env.meta(task)
    result = GradeResult(task_id=task.id)
    force_rmtree(workdir)
    shutil.copytree(env.start_dir(task), workdir, symlinks=True)
    write_hidden_tests(task, workdir)
    box = env.make_sandbox(workdir, task_context())
    try:
        if HIDDEN_DIR in patch:
            result.error = "patch touches the hidden test directory"
            return result
        applied = box.call("git_apply", {"patch": patch})
        result.patch_applied = applied.ok
        if not applied.ok:
            result.error = f"patch does not apply: {applied.error}"
            return result

        statuses: dict[str, str] = {}
        exit_codes = []
        all_failures: list[str] = []
        steps = task.grading.steps or [GradingStep(dbt=c) for c in task.grading.commands]
        for step in steps:
            if step.sql:
                box.require("sql_exec", {"statements": [step.sql]})
                continue
            run = box.call("dbt_raw", {"argv": step.dbt, "timeout_s": 900}, timeout=900)
            data = run.data
            exit_codes.append(data.get("exit_code"))
            summary = data.get("results") or {}
            for uid in summary.get("passed", []) + summary.get("warned", []):
                statuses[uid] = "pass"
            for item in summary.get("failures", []):
                statuses[item["unique_id"]] = item["status"]
            for uid in summary.get("skipped", []):
                statuses[uid] = "skipped"
            step_failures = [f["unique_id"] for f in summary.get("failures", [])]
            all_failures.extend(f"step {len(exit_codes)}: {u}" for u in step_failures)
            if data.get("exit_code") != 0 and not step_failures:
                all_failures.append(
                    f"step {len(exit_codes)}: " + str(data.get("error_excerpt", ""))[-400:]
                )
            result.build = {
                "exit_codes": exit_codes,
                "counts": summary.get("counts"),
                "failures": all_failures,
                "error_excerpt": data.get("error_excerpt", "")[-1500:],
            }
        build_ok = all(code == 0 for code in exit_codes)
        if task.grading.full_build_must_pass:
            result.checks["build_passes"] = {
                "passed": build_ok,
                "detail": result.build.get("failures"),
            }

        for test in task.hidden.dbt_tests:
            uid = next((u for u in statuses if u.split(".")[-1] == f"hidden__{test.name}"), None)
            status = statuses.get(uid) if uid else "not_run"
            result.checks[f"hidden_test:{test.name}"] = {
                "passed": status == "pass",
                "detail": status,
            }

        recorded_values: dict[str, Any] = {}
        for assertion in task.hidden.assertions:
            name = f"assert:{assertion.name}"
            response = box.call("sql_exec", {"statements": [assertion.sql], "read_only": True})
            if not response.ok:
                result.checks[name] = {
                    "passed": False,
                    "detail": f"query failed: {response.error}"[:500],
                }
                continue
            rows = _rows(response)
            if record_reference and assertion.expect.from_ == "reference":
                recorded_values[assertion.name] = rows
                result.checks[name] = {"passed": True, "detail": "recorded"}
                continue
            known, expected = _expected(assertion, meta)
            if not known:
                result.checks[name] = {
                    "passed": False,
                    "detail": "expected value unknown (run suite validation)",
                }
                continue
            if isinstance(expected, dict) and "rows" in expected:
                passed = len(rows) == expected["rows"]
            else:
                passed = _matches(rows, expected, assertion.expect.tolerance)
            result.checks[name] = {
                "passed": passed,
                "detail": {"actual": rows[:5], "expected": expected},
            }

        manifest = box.call("manifest_summary", {"path": "target/manifest.json"})
        nodes = manifest.data.get("nodes", {}) if manifest.ok else {}
        unit_tests = manifest.data.get("unit_tests", []) if manifest.ok else []
        for check in task.hidden.manifest:
            outcome = _manifest_check(check, nodes)
            if check.min_unit_tests:
                found = [u for u in unit_tests if f".{check.model}." in u]
                if len(found) < check.min_unit_tests:
                    outcome["passed"] = False
                    detail = outcome["detail"] if isinstance(outcome["detail"], list) else []
                    outcome["detail"] = [
                        *detail,
                        f"{len(found)} unit tests < {check.min_unit_tests}",
                    ]
            if check.columns and outcome.get("node"):
                node = outcome["node"]
                sql = (
                    "select column_name from information_schema.columns "
                    f"where table_schema = '{node.get('schema')}' "
                    f"and table_name = '{node.get('alias') or node.get('name')}'"
                )
                cols = box.call("sql_exec", {"statements": [sql], "read_only": True})
                present = {r[0] for r in _rows(cols)} if cols.ok else set()
                missing = [c for c in check.columns if c not in present]
                if missing:
                    outcome["passed"] = False
                    outcome["detail"] = [
                        *(outcome["detail"] if isinstance(outcome["detail"], list) else []),
                        f"missing columns {missing}",
                    ]
            outcome.pop("node", None)
            result.checks[f"manifest:{check.model}"] = outcome

        baseline = box.call("manifest_summary", {"path": ".agent/baseline_state/manifest.json"})
        if baseline.ok and manifest.ok:
            flags = compare_tests(baseline.data, manifest.data)
            result.test_flags = [
                f for f in flags if not any(a in str(f.get("test")) for a in task.allowed_removed)
            ]
            result.checks["tests_intact"] = {
                "passed": not result.test_flags,
                "detail": result.test_flags,
            }

        start_statuses = meta.get("start_statuses", {})
        for uid, before in start_statuses.items():
            if before != "pass" or "hidden__" in uid:
                continue
            if any(a in uid for a in task.allowed_removed):
                continue
            now = statuses.get(uid)
            if now is None and uid not in nodes and not uid.startswith("unit_test"):
                result.regressions.append(f"{uid}: removed")
            elif now in {"error", "fail", "runtime error"}:
                result.regressions.append(f"{uid}: {now}")

        reference = meta.get("reference") or {}
        if record_reference:
            result.recorded = {
                "values": recorded_values,
                "fingerprints": table_fingerprints(box, FINGERPRINT_SCHEMAS),
                "statuses": statuses,
            }
        elif task.grading.collateral_check and reference.get("fingerprints") and build_ok:
            ref_fp = reference["fingerprints"]
            affected = set(task.affected_tables)
            wanted = {
                k: v["columns"] for k, v in ref_fp.items() if k not in affected and v.get("columns")
            }
            now_fp = table_fingerprints(box, FINGERPRINT_SCHEMAS, only_columns=wanted)
            for key, ref in ref_fp.items():
                if key in affected:
                    continue
                got = now_fp.get(key)
                if (
                    got is None
                    or got.get("rows") != ref.get("rows")
                    or got.get("hash") != ref.get("hash")
                ):
                    result.collateral.append(key)
            result.checks["no_collateral_changes"] = {
                "passed": not result.collateral,
                "detail": result.collateral,
            }

        exposures = manifest.data.get("exposures", {}) if manifest.ok else {}
        for check in task.hidden.exposures:
            found = next((e for e in exposures.values() if e.get("name") == check.name), None)
            if found is None:
                result.checks[f"exposure:{check.name}"] = {
                    "passed": False,
                    "detail": "exposure not found",
                }
                continue
            deps = {d.split(".")[-1] for d in found.get("depends_on", [])}
            missing = [m for m in check.depends_on if m not in deps]
            result.checks[f"exposure:{check.name}"] = {
                "passed": not missing,
                "detail": missing or "ok",
            }

        for check in task.hidden.tests_present:
            matches = [
                n
                for n in nodes.values()
                if n.get("resource_type") == "test"
                and n.get("test_name") == check.test_name
                and (check.column is None or n.get("column_name") == check.column)
                and (
                    check.target_contains in str(n.get("attached_node"))
                    or any(check.target_contains in d for d in n.get("depends_on", []))
                )
                and (
                    check.severity is None
                    or str(n.get("severity") or "ERROR").lower() == check.severity.lower()
                )
            ]
            label = f"test_present:{check.test_name}({check.column})"
            result.checks[label] = {
                "passed": bool(matches),
                "detail": "found" if matches else "missing",
            }

        # mutation tests last: they break data/code on purpose
        for mutation in task.hidden.mutation_tests:
            name = f"mutation:{mutation.name}"
            if not build_ok:
                result.checks[name] = {"passed": False, "detail": "build failed"}
                continue
            before = box.call("dbt_raw", {"argv": mutation.command, "timeout_s": 600}, timeout=600)
            if before.data.get("exit_code") != 0:
                result.checks[name] = {
                    "passed": False,
                    "detail": "tests already fail before the mutation",
                }
                continue
            problems = []
            for op in mutation.code:
                edit = box.call(
                    "edit_file",
                    {
                        "path": op.path,
                        "old_string": op.old,
                        "new_string": op.new,
                        "replace_all": True,
                    },
                )
                if not edit.ok:
                    problems.append(f"code mutation not applicable: {edit.error}")
            if mutation.sql:
                applied_sql = box.call("sql_exec", {"statements": mutation.sql})
                if not applied_sql.ok:
                    problems.append(f"data mutation failed: {applied_sql.error}")
            if problems:
                result.checks[name] = {"passed": False, "detail": problems}
                continue
            probe = box.call("dbt_raw", {"argv": mutation.command, "timeout_s": 600}, timeout=600)
            caught = probe.data.get("exit_code") not in (0, None)
            if mutation.revert_sql:
                box.call("sql_exec", {"statements": mutation.revert_sql})
            for op in reversed(mutation.code):  # undo code mutations: tests stay independent
                box.call(
                    "edit_file",
                    {
                        "path": op.path,
                        "old_string": op.new,
                        "new_string": op.old,
                        "replace_all": True,
                    },
                )
            failing = [
                f["unique_id"] for f in (probe.data.get("results") or {}).get("failures", [])
            ]
            result.checks[name] = {
                "passed": caught,
                "detail": failing[:5] or "no test failed: the change is not caught",
            }

        result.resolved = result.patch_applied and all(c["passed"] for c in result.checks.values())
        return result
    except Exception as exc:  # grading infrastructure failure: never counts as resolved
        result.error = f"{type(exc).__name__}: {exc}"
        result.resolved = False
        return result
    finally:
        box.stop()
        result.seconds = round(time.monotonic() - started, 2)
        if not keep_workdir:
            remove_databases(workdir)
            force_rmtree(workdir)


def _manifest_check(check, nodes: dict[str, Any]) -> dict[str, Any]:
    model = next(
        (
            n
            for n in nodes.values()
            if n.get("resource_type") == "model" and n.get("name") == check.model
        ),
        None,
    )
    if model is None:
        return {"passed": not check.exists, "detail": "model not found"}
    problems = []
    if check.has_description and not model.get("description", "").strip():
        problems.append("no model description")
    tests = [
        n
        for n in nodes.values()
        if n.get("resource_type") == "test"
        and (
            n.get("attached_node") == f"model.{model_project(model, nodes)}.{check.model}"
            or any(dep.endswith(f".{check.model}") for dep in n.get("depends_on", []))
        )
        and not str(n.get("name", "")).startswith("hidden__")
    ]
    if len(tests) < check.min_tests:
        problems.append(f"{len(tests)} tests < {check.min_tests}")
    columns = model.get("columns", {})
    for col in check.documented_columns:
        if not (columns.get(col) or "").strip():
            problems.append(f"column {col} not documented")
    if check.materialized and model.get("materialized") != check.materialized:
        problems.append(f"materialized={model.get('materialized')}")
    return {"passed": not problems, "detail": problems or "ok", "node": model}


def model_project(model: dict[str, Any], nodes: dict[str, Any]) -> str:
    for uid, node in nodes.items():
        if node is model:
            return uid.split(".")[1]
    return ""
