"""Task specification for the sales-warehouse suite (one YAML file per task).

A task describes a reproducible *starting state* (the base warehouse plus scripted
mutations of models, sources or data), what the agent is told (a CI failure log captured
by running the broken project, or a ticket), a reference fix, and hidden acceptance checks
that the agent never sees.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class Op(BaseModel):
    """One scripted change. File ops apply to the project; ``sql`` applies to the warehouse."""

    op: Literal["replace", "write", "append", "delete", "sql"]
    path: str | None = None
    old: str | None = None
    new: str | None = None
    content: str | None = None
    sql: str | None = None
    count: int = 1  # replace: expected number of occurrences (all are replaced)

    @model_validator(mode="after")
    def _check(self) -> Op:
        need = {
            "replace": ("path", "old", "new"),
            "write": ("path", "content"),
            "append": ("path", "content"),
            "delete": ("path",),
            "sql": ("sql",),
        }[self.op]
        missing = [f for f in need if getattr(self, f) is None]
        if missing:
            raise ValueError(f"op {self.op} needs {missing}")
        return self


class Expect(BaseModel):
    """Expected result of an assertion query.

    * ``value``: a literal scalar;
    * ``from: base``: the same query on the healthy base warehouse (before the mutation);
    * ``from: reference``: the same query after the reference fix (recorded at validation);
    * ``rows: 0``: the query must return no rows.
    """

    value: Any = None
    from_: Literal["base", "reference"] | None = Field(default=None, alias="from")
    rows: int | None = None
    tolerance: float = 0.0

    model_config = {"populate_by_name": True}


class Assertion(BaseModel):
    name: str
    sql: str
    expect: Expect


class HiddenTest(BaseModel):
    name: str
    sql: str  # singular dbt test: returns failing rows


class ManifestCheck(BaseModel):
    model: str
    exists: bool = True
    has_description: bool = False
    min_tests: int = 0
    columns: list[str] = Field(default_factory=list)
    documented_columns: list[str] = Field(default_factory=list)
    materialized: str | None = None
    min_unit_tests: int = 0


class TestPresenceCheck(BaseModel):
    """A generic test that must exist after the change (e.g. kept but downgraded to warn)."""

    test_name: str
    column: str | None = None
    target_contains: str
    severity: str | None = None


class ExposureCheck(BaseModel):
    name: str
    depends_on: list[str] = Field(default_factory=list)  # model names


class MutationTest(BaseModel):
    """Mutation testing for "add a test" tickets: after grading, break the data or the code
    on purpose and check that the project's tests now FAIL (i.e. the new test bites)."""

    name: str
    sql: list[str] = Field(default_factory=list)  # data changes applied after the build
    code: list[Op] = Field(default_factory=list)  # code changes applied after the build
    command: list[str] = Field(default_factory=lambda: ["test"])
    revert_sql: list[str] = Field(default_factory=list)  # undo, so mutations stay independent


class Hidden(BaseModel):
    dbt_tests: list[HiddenTest] = Field(default_factory=list)
    assertions: list[Assertion] = Field(default_factory=list)
    manifest: list[ManifestCheck] = Field(default_factory=list)
    exposures: list[ExposureCheck] = Field(default_factory=list)
    tests_present: list[TestPresenceCheck] = Field(default_factory=list)
    mutation_tests: list[MutationTest] = Field(default_factory=list)


class GradingStep(BaseModel):
    dbt: list[str] | None = None
    sql: str | None = None


class Grading(BaseModel):
    commands: list[list[str]] = Field(default_factory=lambda: [["build"]])
    # alternative to commands: dbt commands interleaved with data changes
    steps: list[GradingStep] = Field(default_factory=list)
    full_build_must_pass: bool = True
    collateral_check: bool = True


class TaskSpec(BaseModel):
    id: str
    kind: Literal["breakage", "feature"]
    category: str
    title: str
    trigger: Literal["ci_failure", "ticket"]
    ticket: str = ""
    difficulty: Literal["easy", "medium", "hard"] = "medium"
    mutation: list[Op] = Field(default_factory=list)
    trigger_command: list[str] = Field(default_factory=lambda: ["build"])
    reference_fix: list[Op] = Field(default_factory=list)
    hidden: Hidden = Field(default_factory=Hidden)
    grading: Grading = Field(default_factory=Grading)
    # tables the correct change may modify (excluded from the collateral-change check)
    affected_tables: list[str] = Field(default_factory=list)
    # tests the correct change may remove or rename (excluded from regression counting)
    allowed_removed: list[str] = Field(default_factory=list)
    # prompt-injection canary planted in the repository (measured, never followed)
    canary: str | None = None
    notes: str = ""

    @model_validator(mode="after")
    def _check(self) -> TaskSpec:
        if any(op.op == "sql" for op in self.reference_fix):
            raise ValueError("a reference fix may only change code, not data")
        if self.trigger == "ticket" and not self.ticket.strip():
            raise ValueError("ticket tasks need ticket text")
        return self

    @classmethod
    def load(cls, path: Path) -> TaskSpec:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(data)


def load_suite(directory: Path) -> list[TaskSpec]:
    tasks = [TaskSpec.load(p) for p in sorted(Path(directory).glob("*.yaml"))]
    ids = [t.id for t in tasks]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ValueError(f"duplicate task ids: {sorted(duplicates)}")
    return tasks
