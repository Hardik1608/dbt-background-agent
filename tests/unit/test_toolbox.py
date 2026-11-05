"""The in-sandbox toolbox, exercised through LocalSandbox (no dbt, no Docker)."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from dbt_agent.sandbox import LocalSandbox, SandboxContext
from tests.helpers import make_repo

MODEL = "select 1 as id,\n    'a' as name\n"


@pytest.fixture
def box(tmp_path: Path) -> LocalSandbox:
    root = make_repo(
        tmp_path / "work",
        {
            "dbt_project.yml": "name: demo\n",
            "models/a.sql": MODEL,
            "models/schema.yml": "version: 2\n",
            "profiles.yml": "demo: {}\n",
            ".gitignore": "*.duckdb\ntarget/\n",
        },
    )
    con = duckdb.connect(str(root / "warehouse.duckdb"))
    con.execute("create schema gold; create table gold.t as select range as id from range(5)")
    con.close()
    sandbox = LocalSandbox(root, SandboxContext(db_path="warehouse.duckdb"))
    sandbox.start()
    return sandbox


def test_list_and_read(box):
    listing = box.call("list_files")
    assert listing.ok and "models/a.sql" in listing.output
    read = box.call("read_file", {"path": "models/a.sql"})
    assert read.ok
    assert "1\tselect 1 as id," in read.output
    assert "(lines 1-2 of 2)" in read.output


def test_read_missing_file_gives_hint(box):
    response = box.call("read_file", {"path": "models/missing.sql"})
    assert not response.ok
    assert "File not found" in response.error
    assert "a.sql" in response.hint  # lists the directory's files


def test_read_pages_long_files(box):
    box.call(
        "write_file",
        {"path": "models/long.sql", "content": "\n".join(f"-- {i}" for i in range(700))},
    )
    first = box.call("read_file", {"path": "models/long.sql"})
    assert "start_line=401" in first.output
    nxt = box.call("read_file", {"path": "models/long.sql", "start_line": 401})
    assert "401\t-- 400" in nxt.output


def test_edit_file_forgives_copied_line_numbers_and_shifted_indent(box):
    box.call(
        "write_file",
        {
            "path": "models/c.sql",
            "content": "with x as (\n    select 1 as id\n)\nselect * from x\n",
        },
    )
    # every line copied with one extra leading space (from a numbered view)
    shifted = box.call(
        "edit_file",
        {
            "path": "models/c.sql",
            "old_string": "     select 1 as id\n )",
            "new_string": "     select 2 as id\n )",
        },
    )
    assert shifted.ok and "Matched after removing one leading space" in shifted.output
    numbered = box.call(
        "edit_file",
        {
            "path": "models/c.sql",
            "old_string": "2\t    select 2 as id\n3\t)",
            "new_string": "    select 3 as id\n)",
        },
    )
    assert numbered.ok and "removing line numbers" in numbered.output
    text = box.call("read_file", {"path": "models/c.sql"}).output
    assert "2\t    select 3 as id" in text and "3\t)" in text
    # never applied when the shifted text would match more than once
    box.call("write_file", {"path": "models/d.sql", "content": "a\nb\na\nb\n"})
    ambiguous = box.call(
        "edit_file", {"path": "models/d.sql", "old_string": " a\n b", "new_string": " c\n d"}
    )
    assert not ambiguous.ok and "without the line number" in ambiguous.hint


def test_write_and_edit(box):
    created = box.call("write_file", {"path": "models/b.sql", "content": "select 2 as x"})
    assert created.ok and created.data["mutated"]
    edited = box.call(
        "edit_file", {"path": "models/b.sql", "old_string": "2 as x", "new_string": "3 as x"}
    )
    assert edited.ok
    assert (Path(box.root) / "models/b.sql").read_text() == "select 3 as x\n"


def test_edit_errors_tell_the_model_what_to_do(box):
    missing = box.call(
        "edit_file", {"path": "models/a.sql", "old_string": "select 9 as id", "new_string": "x"}
    )
    assert not missing.ok and "not found" in missing.error
    assert "closest match" in missing.hint and "line 1" in missing.hint
    box.call("write_file", {"path": "models/dup.sql", "content": "x\nx\n"})
    dup = box.call("edit_file", {"path": "models/dup.sql", "old_string": "x", "new_string": "y"})
    assert not dup.ok and "occurs 2 times" in dup.error
    allowed = box.call(
        "edit_file",
        {"path": "models/dup.sql", "old_string": "x", "new_string": "y", "replace_all": True},
    )
    assert allowed.ok


@pytest.mark.parametrize("path", ["../outside.sql", "/etc/passwd", "models/../../x.sql"])
def test_path_escape_is_blocked_and_reported(box, path):
    response = box.call("write_file", {"path": path, "content": "x"})
    assert not response.ok
    assert "outside the project" in response.error
    assert any(e["kind"] == "path_escape_blocked" for e in response.security)


def test_symlink_escape_is_blocked(box, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("host secret", encoding="utf-8")
    link = Path(box.root) / "models" / "link.sql"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need extra privileges on this platform")
    response = box.call("read_file", {"path": "models/link.sql"})
    assert not response.ok and "outside the project" in response.error


@pytest.mark.parametrize(
    "path",
    [".git/config", "target/x.sql", "warehouse.duckdb", "dbt_packages/x.sql", "profiles.yml"],
)
def test_protected_paths_are_read_only(box, path):
    response = box.call("write_file", {"path": path, "content": "x"})
    assert not response.ok
    assert any(e["kind"] == "protected_path_blocked" for e in response.security)


def test_query_is_read_only(box):
    ok = box.call("query_duckdb", {"sql": "select count(*) as n from gold.t"})
    assert ok.ok and "5" in ok.output
    for sql, kind in [
        ("create table gold.x as select 1", "write_attempt_blocked"),
        ("delete from gold.t", "write_attempt_blocked"),
        ("attach 'other.db'", "write_attempt_blocked"),
    ]:
        response = box.call("query_duckdb", {"sql": sql})
        assert not response.ok, sql
        assert "read-only" in response.error
        assert any(e["kind"] == kind for e in response.security), sql


def test_query_blocks_files_and_network(box):
    net = box.call("query_duckdb", {"sql": "select * from read_csv('http://example.com/a.csv')"})
    assert not net.ok
    assert any(e["kind"] == "egress_attempt_blocked" for e in net.security)
    local = box.call("query_duckdb", {"sql": "select * from read_csv('models/a.sql')"})
    assert not local.ok
    assert any(e["kind"] == "file_access_blocked" for e in local.security)
    two = box.call("query_duckdb", {"sql": "select 1; select 2"})
    assert not two.ok and "exactly one statement" in two.error


def test_query_limits_rows(box):
    response = box.call("query_duckdb", {"sql": "select * from range(500)", "max_rows": 10})
    assert response.ok and response.data["truncated"]
    assert "more exist" in response.output


def test_unknown_dbt_command_is_explained(box):
    response = box.call("run_dbt", {"command": "deps"})
    assert not response.ok and "no network" in response.hint
    assert any(e["kind"] == "blocked_command" for e in response.security)
    bad = box.call("run_dbt", {"command": "build", "select": "a; rm -rf /"})
    assert not bad.ok and "Invalid characters" in bad.error


def test_git_diff_snapshot_restore(box):
    assert "No changes" in box.call("git_diff").output
    start = box.call("snapshot").data["tree"]
    box.call("write_file", {"path": "models/new.sql", "content": "select 42"})
    box.call("edit_file", {"path": "models/a.sql", "old_string": "'a'", "new_string": "'b'"})
    diff = box.call("git_diff")
    assert "models/new.sql" in diff.output and "+    'b' as name" in diff.output
    after = box.call("snapshot").data["tree"]
    assert after != start
    patch = box.call("git_patch")
    assert {f["path"] for f in patch.data["files"]} == {"models/new.sql", "models/a.sql"}
    box.call("restore", {"tree": start})
    assert not (Path(box.root) / "models/new.sql").exists()
    assert (Path(box.root) / "models/a.sql").read_text() == MODEL
    box.call("restore", {"tree": after})
    assert (Path(box.root) / "models/new.sql").exists()


def test_apply_patch(box):
    patch = (
        "--- a/models/a.sql\n+++ b/models/a.sql\n@@ -1,2 +1,2 @@\n select 1 as id,\n"
        "-    'a' as name\n+    'z' as name\n"
    )
    response = box.call("apply_patch", {"patch": patch})
    assert response.ok, response.error
    assert "'z'" in (Path(box.root) / "models/a.sql").read_text()
    again = box.call("apply_patch", {"patch": patch})
    assert not again.ok and "does not apply" in again.error


def test_url_in_arguments_is_recorded_as_info(box):
    response = box.call(
        "write_file", {"path": "models/doc.yml", "content": "# see https://docs.getdbt.com\n"}
    )
    assert response.ok
    assert any(e["kind"] == "egress_reference" for e in response.security)


def test_output_truncation(box):
    from dbt_agent.sandbox.toolbox import truncate

    text = "\n".join(f"line {i}" for i in range(2000))
    out = truncate(text, 500)
    assert len(out) < 700 and "truncated" in out
    assert out.endswith("line 1999")


def test_delete_file(box):
    box.call("write_file", {"path": "models/old.sql", "content": "select 1"})
    deleted = box.call("delete_file", {"path": "models/old.sql"})
    assert deleted.ok and deleted.data["mutated"]
    assert not (Path(box.root) / "models/old.sql").exists()
    again = box.call("delete_file", {"path": "models/old.sql"})
    assert not again.ok and "not found" in again.error
    protected = box.call("delete_file", {"path": "warehouse.duckdb"})
    assert not protected.ok
    assert any(e["kind"] == "protected_path_blocked" for e in protected.security)


def test_search_files(box):
    found = box.call("search_files", {"pattern": "as name"})
    assert found.ok and "models/a.sql:2:" in found.output
    regex = box.call("search_files", {"pattern": r"select \d", "regex": True, "glob": "*.sql"})
    assert regex.ok and "1 match" in regex.output
    none = box.call("search_files", {"pattern": "does-not-occur"})
    assert none.ok and "No matches" in none.output
    bad = box.call("search_files", {"pattern": "(", "regex": True})
    assert not bad.ok and "regex=false" in bad.hint


def test_git_init_keeps_databases_and_packages_out_of_the_baseline(tmp_path):
    import subprocess

    root = tmp_path / "app"  # a project someone else prepared, no git and no .gitignore
    (root / "models").mkdir(parents=True)
    (root / "models" / "a.sql").write_text(MODEL, encoding="utf-8")
    (root / "dbt_packages" / "dbt_utils").mkdir(parents=True)
    (root / "dbt_packages" / "dbt_utils" / "x.sql").write_text("select 1", encoding="utf-8")
    con = duckdb.connect(str(root / "app.duckdb"))
    con.execute("create table t as select 1 as id")
    con.close()
    box = LocalSandbox(root, SandboxContext(db_path="app.duckdb"))
    box.start()
    response = box.call("git_init")
    assert response.ok and response.data["head"]
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.split()
    assert tracked == ["models/a.sql"]
    assert box.call("git_init").data["head"] == response.data["head"]  # idempotent


def test_patch_with_crlf_lines_survives_the_file_round_trip(tmp_path):
    """A project file with CRLF endings rewritten with LF: the patch must still apply."""
    import shutil

    from dbt_agent.util import atomic_write_text, read_text_exact

    start = make_repo(
        tmp_path / "start",
        {"seeds/_seeds.yml": "version: 2\r\nseeds:\r\n  - x\r\n", "models/a.sql": MODEL},
    )
    work = tmp_path / "work"
    shutil.copytree(start, work)
    box = LocalSandbox(work, SandboxContext())
    box.start()
    assert box.call(
        "write_file", {"path": "seeds/_seeds.yml", "content": "version: 2\nseeds:\n  - y\n"}
    ).ok
    patch = box.call("git_patch").output
    assert "\r" in patch
    atomic_write_text(tmp_path / "patch.diff", patch)
    assert read_text_exact(tmp_path / "patch.diff") == patch
    assert (tmp_path / "patch.diff").read_text(encoding="utf-8") != patch  # the old bug
    fresh = tmp_path / "fresh"
    shutil.copytree(start, fresh)
    other = LocalSandbox(fresh, SandboxContext())
    other.start()
    assert other.call("git_apply", {"patch": read_text_exact(tmp_path / "patch.diff")}).ok
    # the file keeps its CRLF endings: only the changed line differs
    assert (fresh / "seeds" / "_seeds.yml").read_bytes() == b"version: 2\r\nseeds:\r\n  - y\r\n"
    assert box.call("git_patch").data["files"][0]["added"] == "1"


def test_edit_file_keeps_crlf_line_endings(tmp_path):
    root = make_repo(tmp_path / "w", {"models/a.sql": "select 1 as id,\r\n    2 as two\r\n"})
    box = LocalSandbox(root, SandboxContext())
    box.start()
    assert box.call(
        "edit_file",
        {"path": "models/a.sql", "old_string": "    2 as two\n", "new_string": "    3 as three\n"},
    ).ok
    assert (root / "models" / "a.sql").read_bytes() == b"select 1 as id,\r\n    3 as three\r\n"
    files = box.call("git_patch").data["files"]
    assert files == [{"path": "models/a.sql", "added": "1", "deleted": "1"}]


def test_harness_git_never_runs_hooks_planted_in_the_work_directory(box):
    """dbt code can write into .git; the harness's own git calls must not execute it."""
    hooks = Path(box.root) / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    marker = Path(box.root).parent / "hook_ran"
    hook = hooks / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch '{marker.as_posix()}'\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    box.call("write_file", {"path": "models/b.sql", "content": "select 2 as x"})
    committed = box.call("git_commit", {"branch": "agent/test", "message": "change"})
    assert committed.ok, committed.error  # a failing pre-commit hook would block the commit
    assert not marker.exists()
