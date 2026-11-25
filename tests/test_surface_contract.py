"""1.x 표면 계약(Surface Contract) 회귀 가드 — Gate 1 & Gate 5.

docs/surface-contract.md(manual/surface-contract.md)의 모든 기계 계약을 코드로 고정한다:
1. `organum` 최상위 하위 명령은 STABLE_COMMANDS 또는 EXPERIMENTAL_COMMANDS 중 정확히 하나에 속한다.
2. `organum --help` 출력에서 모든 Stable 명령은 `[stable]`, Experimental 명령은 `[experimental]` 접두를 가진다.
3. Stable 검증·위생 기관 6종의 `--json` 출력은 고정된 `schema` 문자열, 필수 최상위 키, 0/1 종료코드를 지킨다.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
from pathlib import Path

import pytest

from organum import cli


def _subcommand_helps() -> dict[str, str]:
    parser = cli.build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return {ca.dest: (ca.help or "") for ca in action._choices_actions}
    raise AssertionError("subparsers action not found")


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


def test_every_top_level_command_is_classified_disjointly():
    helps = _subcommand_helps()
    registered = set(helps.keys())
    assert cli.STABLE_COMMANDS.isdisjoint(cli.EXPERIMENTAL_COMMANDS)
    assert registered == (cli.STABLE_COMMANDS | cli.EXPERIMENTAL_COMMANDS), (
        f"unclassified={registered - (cli.STABLE_COMMANDS | cli.EXPERIMENTAL_COMMANDS)} "
        f"extra={(cli.STABLE_COMMANDS | cli.EXPERIMENTAL_COMMANDS) - registered}"
    )


def test_help_labels_match_surface_classification():
    helps = _subcommand_helps()
    for name in sorted(cli.STABLE_COMMANDS):
        assert helps[name].startswith("[stable] "), f"{name}: {helps[name]!r}"
    for name in sorted(cli.EXPERIMENTAL_COMMANDS):
        assert helps[name].startswith("[experimental] "), f"{name}: {helps[name]!r}"
    full_help = cli.build_parser().format_help()
    assert "[stable]" in full_help and "[experimental]" in full_help
    assert "organum-inspector" in full_help and "organum-hub" in full_help and "organum-bbs" in full_help


def test_pyproject_entrypoints_match_stable_entrypoints():
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    sec = text.split("[project.scripts]", 1)[1].split("\n[", 1)[0]
    declared = set(re.findall(r"^([a-z0-9-]+)\s*=", sec, re.M))
    assert declared == cli.STABLE_ENTRYPOINTS


def test_occupancy_json_contract_and_exit_codes(tmp_path):
    f = tmp_path / "mem.jsonl"
    f.write_text('{"ts":"2026-11-25T00:00:00Z","text":"a"}\n{"ts":"2026-11-25T01:00:00Z","text":"b"}\n',
                 encoding="utf-8")
    rc, out, _ = _run_cli(["occupancy", "--file", str(f), "--json"])
    assert rc == 0
    d = json.loads(out)
    assert d["schema"] == "organum.temporal-occupancy/v1"
    for k in ("schema", "source", "total_items", "skipped_rows", "capacity", "bucket_cap",
              "kept_count", "distinct_buckets", "max_bucket_occupancy",
              "evicted_bucket_cap", "evicted_global_cap"):
        assert k in d, k

    rc_missing, _, _ = _run_cli(["occupancy", "--file", str(tmp_path / "missing.jsonl"), "--json"])
    assert rc_missing == 1


def test_manifest_json_contract_and_exit_codes(tmp_path):
    rec = tmp_path / "rec"
    rec.mkdir()
    (rec / "README.md").write_text("# R\n\n## Files\n\n| Group | File |\n|---|---|\n| g | `a.txt` |\n",
                                   encoding="utf-8")
    (rec / "a.txt").write_text("ok\n", encoding="utf-8")
    rc, out, _ = _run_cli(["manifest", "check", str(rec), "--json"])
    assert rc == 0
    d = json.loads(out)
    assert d["schema"] == "organum.record-manifest/v1" and d["passed"] is True
    for k in ("schema", "verdict", "passed", "named_count", "listed_count", "missing_count",
              "findings", "directory", "cover"):
        assert k in d, k

    (rec / "a.txt").unlink()
    rc_fail, out_fail, _ = _run_cli(["manifest", "check", str(rec), "--json"])
    assert rc_fail == 1
    assert json.loads(out_fail)["passed"] is False


def test_semver_gate_json_contract_and_exit_codes():
    rc, out, _ = _run_cli(["semver-gate", "check", "1.4.2", ">= 1.0.0, < 2.0.0", "--json"])
    assert rc == 0
    d = json.loads(out)
    assert d["schema"] == "organum.semver-gate/v1" and d["ok"] is True
    for k in ("schema", "ok", "reason_code", "detail", "version_raw", "spec_raw"):
        assert k in d, k

    rc_pre, out_pre, _ = _run_cli(["semver-gate", "check", "2.0.0-rc.1", ">= 1.0.0", "--json"])
    assert rc_pre == 1 and json.loads(out_pre)["ok"] is False

    rc_tr, out_tr, _ = _run_cli(["semver-gate", "transition", "tool 1.0.0", "tool 1.1.0", "--json"])
    assert rc_tr == 0
    dt = json.loads(out_tr)
    assert dt["schema"] == "organum.semver-gate/v1" and dt["alert"] is False
    for k in ("schema", "status", "alert", "detail", "before_raw", "after_raw"):
        assert k in dt, k

    rc_down, out_down, _ = _run_cli(["semver-gate", "transition", "tool 1.2.0", "tool 1.1.0", "--json"])
    assert rc_down == 1 and json.loads(out_down)["alert"] is True


def test_chronicle_json_contract_and_exit_codes(tmp_path):
    a = tmp_path / "a.yaml"
    b = tmp_path / "b.yaml"
    a.write_text("entries:\n  - id: E-01\n    state: active\n    date: 2026-11-25\n", encoding="utf-8")
    b.write_text("entries:\n  - id: E-01\n    state: retired\n    date: 2026-11-25\n", encoding="utf-8")
    rc, out, _ = _run_cli(["chronicle", "check", str(a), "--json"])
    assert rc == 0
    d = json.loads(out)
    assert d["schema"] == "organum.chronicle/v1"
    for k in ("schema", "source_path", "blob_fingerprint", "entry_count", "entries"):
        assert k in d, k

    rc_d, out_d, _ = _run_cli(["chronicle", "diff", str(a), str(b), "--json"])
    assert rc_d == 0
    dd = json.loads(out_d)
    assert dd["schema"] == "organum.chronicle/v1"
    for k in ("schema", "before", "after", "counts", "added", "removed", "changed"):
        assert k in dd, k

    bad = tmp_path / "dup.yaml"
    bad.write_text("entries:\n  - id: E-01\n    state: a\n  - id: E-01\n    state: b\n", encoding="utf-8")
    rc_bad, _, _ = _run_cli(["chronicle", "check", str(bad), "--json"])
    assert rc_bad == 1


def test_rule_audit_json_contract_and_exit_codes(tmp_path):
    rc, out, _ = _run_cli(["rule-audit", "demo", "--json"])
    assert rc == 0
    d = json.loads(out)
    assert d["schema"] == "organum.rule-audit/v1"
    for k in ("schema", "pack", "summary", "cases"):
        assert k in d, k

    bad_trace = tmp_path / "trace.json"
    bad_trace.write_text(json.dumps({
        "events": [
            {"op": "enqueue", "id": "x"},
            {"op": "dequeue", "id": "y"},
        ]
    }), encoding="utf-8")
    rc_chk, out_chk, _ = _run_cli(["rule-audit", "check", str(bad_trace),
                                   "--pack", "queue", "--legacy-verdict", "PASS", "--json"])
    assert rc_chk == 1
    dc = json.loads(out_chk)
    assert dc["schema"] == "organum.rule-audit/v1"
    assert dc["passed"] is False and dc["escape_detected"] is True
    for k in ("schema", "pack", "passed", "escape_detected", "rules_checked", "violations"):
        assert k in dc, k


def test_zero_ref_json_contract_and_exit_codes(tmp_path):
    rc, out, _ = _run_cli(["zero-ref", "check", "--lo", "0.50", "--hi", "0.80",
                           "--candidate", "0.65", "--margin", "0.25", "--json"])
    assert rc == 0
    d = json.loads(out)
    assert d["schema"] == "organum.zero-ref/v1" and d["classification"] == "informative_match"
    for k in ("schema", "mode", "classification", "informative", "candidate_pass",
              "zero_pass", "t_zero", "margin", "zero_at_boundary"):
        assert k in d, k

    rc_vac, out_vac, _ = _run_cli(["zero-ref", "check", "--lo", "-0.10", "--hi", "0.20",
                                   "--candidate", "0.05", "--margin", "0.25", "--json"])
    assert rc_vac == 1 and json.loads(out_vac)["classification"] == "uninformative_pass"

    batch = tmp_path / "batch.jsonl"
    batch.write_text(
        json.dumps({"id": "a", "lo": 0.50, "hi": 0.80, "candidate": 0.65, "margin": 0.25}) + "\n",
        encoding="utf-8",
    )
    rc_b, out_b, _ = _run_cli(["zero-ref", "batch", str(batch), "--json"])
    assert rc_b == 0
    db = json.loads(out_b)
    assert db["schema"] == "organum.zero-ref/v1"
    for k in ("schema", "mode", "summary", "results"):
        assert k in db, k


def test_inspector_json_contract_on_empty_dir(tmp_path):
    rc, out, _ = _run_cli(["inspector", str(tmp_path), "--json"])
    assert rc == 0
    d = json.loads(out)
    assert isinstance(d, list) and d == []
