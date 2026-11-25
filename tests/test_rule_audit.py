"""Tests for organum.rule_audit (Wave 2 independent invariant rule-audit core)."""
from __future__ import annotations

import json
from pathlib import Path

from organum import cli, rule_audit as ra


def test_queue_self_demo_catches_all_verifier_escapes_zero_false_alarms():
    res = ra.evaluate_cases(ra.build_queue_self_demo_cases(), ra.QUEUE_RULES, pack="queue")
    s = res["summary"]
    assert res["schema"] == ra.SCHEMA
    assert s["total"] == 7
    assert s["escapes_caught"] == 3
    assert s["true_passes"] == 2
    assert s["true_fails"] == 5
    assert s["false_alarms"] == 0
    assert s["missed_faults"] == 0


def test_hygiene_pack_catches_quota_privacy_and_pin_drift():
    clean_trace = {
        "runs": [{"actor": "a1", "exit_code": 0, "outcome": "ok", "summary": "done cleanly"}],
        "artifacts": [{"row": "R1", "note": "anonymized summary"}],
        "pin_checks": [{"path": "spec.md", "expected_sha256": "abc123", "actual_sha256": "abc123"}],
    }
    clean_rep = ra.audit_trace(clean_trace, ra.HYGIENE_RULES, pack="hygiene", legacy_verdict="PASS")
    assert clean_rep.passed is True
    assert clean_rep.escape_detected is False

    bad_trace = {
        "runs": [{"actor": "a1", "exit_code": 0, "outcome": "ok", "output_preview": "You hit your limit for today"}],
        "artifacts": [{"note": "saved under /Users/alice/secret.txt by alice@example.com"}],
        "pin_checks": [{"path": "spec.md", "expected_sha256": "aaa", "actual_sha256": "bbb"}],
    }
    bad_rep = ra.audit_trace(bad_trace, ra.HYGIENE_RULES, pack="hygiene", legacy_verdict="PASS")
    assert bad_rep.passed is False
    assert bad_rep.escape_detected is True
    ids = {v.rule_id for v in bad_rep.violations}
    assert ids == {"H1", "H2", "H3"}


def test_cli_rule_audit_demo_and_check(tmp_path: Path, capsys):
    assert cli.main(["rule-audit", "demo", "--json"]) == 0
    demo_out = json.loads(capsys.readouterr().out)
    assert demo_out["summary"]["escapes_caught"] == 3

    trace_path = tmp_path / "trace.json"
    trace_path.write_text(json.dumps({
        "runs": [{"actor": "c1", "exit_code": 0, "summary": "weekly limit reached"}],
    }), encoding="utf-8")

    rc = cli.main(["rule-audit", "check", str(trace_path), "--pack", "hygiene", "--legacy-verdict", "PASS", "--json"])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["passed"] is False
    assert out["escape_detected"] is True
    assert out["violations"][0]["rule_id"] == "H1"
