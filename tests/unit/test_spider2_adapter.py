"""Spider2-DBT adapter: archive extraction layout, task loading and the safe result copy."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from dbt_agent.bench import spider2


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo" / "spider2-dbt"
    (root / "examples").mkdir(parents=True)
    (root / "evaluation_suite" / "gold").mkdir(parents=True)
    (root / "examples" / "spider2-dbt.jsonl").write_text(
        json.dumps({"instance_id": "playbook001", "instruction": "Do it"}) + "\n", encoding="utf-8"
    )
    (root / "evaluation_suite" / "gold" / "spider2_eval.jsonl").write_text(
        json.dumps(
            {
                "instance_id": "playbook001",
                "evaluation": {
                    "func": "duckdb_match",
                    "parameters": {
                        "gold": "playbook.duckdb",
                        "condition_tabs": ["attribution_touches"],
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return root


def test_extract_matches_official_layout(tmp_path):
    archive = tmp_path / "start.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("playbook001/playbook.duckdb", b"db")
        z.writestr("playbook001/nested/other.duckdb", b"db2")
        z.writestr("bq001/x.duckdb", b"kept")  # extracted like every top-level folder
        z.writestr("__MACOSX/playbook001/._playbook.duckdb", b"junk")
        z.writestr("../escape.duckdb", b"evil")
        z.writestr("playbook001/readme.txt", b"not a db")
    target = tmp_path / "examples"
    spider2._extract(archive, target)
    assert (target / "playbook001" / "playbook.duckdb").read_bytes() == b"db"
    assert (target / "playbook001" / "other.duckdb").exists()
    assert (target / "bq001" / "x.duckdb").read_bytes() == b"kept"
    assert not (target / "__MACOSX").exists()
    assert not (tmp_path / "escape.duckdb").exists()
    assert not (target / "playbook001" / "readme.txt").exists()


def test_load_tasks_joins_instructions_and_gold_spec(tmp_path):
    _repo(tmp_path)
    tasks = spider2.load_tasks(tmp_path)
    assert len(tasks) == 1
    assert tasks[0].db_file == "playbook.duckdb"
    assert tasks[0].condition_tabs == ["attribution_touches"]


def test_instance_databases_from_archive_or_official_layout(tmp_path):
    _repo(tmp_path)
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    with zipfile.ZipFile(downloads / spider2.START_ARCHIVE, "w") as z:
        z.writestr("playbook001/playbook.duckdb", b"start")
        z.writestr("playbook001/sub/extra.duckdb", b"extra")
        z.writestr("other001/other.duckdb", b"not this one")
    dest = tmp_path / "template"
    names = spider2.instance_databases(tmp_path, "playbook001", "start", dest)
    assert sorted(names) == ["extra.duckdb", "playbook.duckdb"]
    assert sorted(p.name for p in dest.iterdir()) == ["extra.duckdb", "playbook.duckdb"]
    gold = tmp_path / "repo" / "spider2-dbt" / "evaluation_suite" / "gold" / "playbook001"
    gold.mkdir()
    (gold / "playbook.duckdb").write_bytes(b"gold")  # no gold archive: official layout
    names = spider2.instance_databases(tmp_path, "playbook001", "gold", tmp_path / "g")
    assert names == ["playbook.duckdb"]
    assert (tmp_path / "g" / "playbook.duckdb").read_bytes() == b"gold"


FAKE_EVALUATE = """
import argparse, json, os
p = argparse.ArgumentParser()
p.add_argument("--result_dir"); p.add_argument("--gold_dir")
a = p.parse_args()
assert [f for f in os.listdir(a.result_dir) if f.endswith(".jsonl")] == ["results_metadata.jsonl"]
gold = {}
for line in open(os.path.join(a.gold_dir, "spider2_eval.jsonl")):
    entry = json.loads(line)
    gold[entry["instance_id"]] = entry["evaluation"]["parameters"]["gold"]
ok = n = 0
for line in open(os.path.join(a.result_dir, "results_metadata.jsonl")):
    entry = json.loads(line)
    iid = entry["instance_id"]
    n += 1
    res = os.path.join(a.result_dir, iid, entry["answer_or_path"])
    ref = os.path.join(a.gold_dir, iid, gold[iid])
    if os.path.exists(res) and open(res, "rb").read() == open(ref, "rb").read():
        ok += 1
        print(iid)
print(ok / n, ok, n)
"""


def test_evaluate_runs_the_script_per_instance_and_cleans_up(tmp_path, monkeypatch):
    import sys

    monkeypatch.chdir(tmp_path)  # relative data dir, as the CLI passes it
    root = _repo(tmp_path)
    suite = root / "evaluation_suite"
    (suite / "evaluate.py").write_text(FAKE_EVALUATE, encoding="utf-8")
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    with zipfile.ZipFile(downloads / spider2.GOLD_ARCHIVE, "w") as z:
        z.writestr("playbook001/playbook.duckdb", b"right answer")
    submission = tmp_path / "submission"
    (submission / "playbook001").mkdir(parents=True)
    (submission / "playbook001" / "playbook.duckdb").write_bytes(b"right answer")
    results = [{"instance_id": "playbook001"}]
    out = spider2.evaluate(Path(), submission, results, sys.executable, work=Path("work"))
    assert out["official_passed"] == 1 and out["official_evaluated"] == 1
    assert out["passed_instances"] == ["playbook001"]
    assert not any((tmp_path / "work").iterdir())  # gold copies deleted
    assert (submission / "results_metadata.jsonl").exists()
    (submission / "playbook001" / "playbook.duckdb").write_bytes(b"wrong")
    out = spider2.evaluate(tmp_path, submission, results, sys.executable, work=tmp_path / "work")
    assert out["official_passed"] == 0 and out["official_evaluated"] == 1


def test_safe_copy_refuses_symlinks_and_outside_paths(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    (root / "result.duckdb").write_bytes(b"ok")
    assert spider2._safe_copy(root / "result.duckdb", tmp_path / "out" / "a.duckdb", root)
    assert (tmp_path / "out" / "a.duckdb").read_bytes() == b"ok"
    secret = tmp_path / "secret.txt"
    secret.write_text("host secret", encoding="utf-8")
    try:
        (root / "link.duckdb").symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need extra privileges on this platform")
    assert not spider2._safe_copy(root / "link.duckdb", tmp_path / "out" / "b.duckdb", root)
    assert not (tmp_path / "out" / "b.duckdb").exists()
