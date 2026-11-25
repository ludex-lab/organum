"""Tests for organum.zero_ref (Zero-Reference Equivalence Check, arXiv 2611.27186 §2.2)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from organum import cli, zero_ref as zr


def test_zero_ref_three_canonical_examples_and_strict_vs_inclusive_boundary():
    # Example 1: [0.25, 0.45], c=0.31, m=0.20 -> informative match (T=0.45 > 0.20)
    r1 = zr.evaluate_zero_ref("0.25", "0.45", "0.31", "0.20")
    assert r1.t_zero == pytest.approx(0.45)
    assert r1.candidate_pass_strict is True
    assert r1.zero_pass_strict is False
    assert r1.classification == "informative_match"
    assert r1.informative is True

    # Example 2: [-0.05, 0.12], c=0.03, m=0.20 -> uninformative pass (T=0.12 < 0.20)
    r2 = zr.evaluate_zero_ref("-0.05", "0.12", "0.03", "0.20")
    assert r2.t_zero == pytest.approx(0.12)
    assert r2.candidate_pass_strict is True
    assert r2.zero_pass_strict is True
    assert r2.classification == "uninformative_pass"
    assert r2.informative is False

    # Example 3: [-0.20, 0.20], c=0.0, m=0.20 -> boundary T == m
    # Inclusive (<=): zero PASS; Strict (<, paper §2.2): zero FAIL
    r3_strict = zr.evaluate_zero_ref("-0.20", "0.20", "0.0", "0.20", strict=True)
    assert r3_strict.zero_at_boundary is True
    assert r3_strict.zero_pass_inclusive is True
    assert r3_strict.zero_pass_strict is False
    assert r3_strict.classification_inclusive == "uninformative_pass"
    assert r3_strict.classification_strict == "mismatch"

    r3_inc = zr.evaluate_zero_ref("-0.20", "0.20", "0.0", "0.20", strict=False)
    assert r3_inc.classification == "uninformative_pass"


def test_zero_ref_decimal_exact_boundary_and_invalid_inputs():
    # Decimal arithmetic avoids 0.3 - 0.2 == 0.09999999999999998 IEEE-754 drift
    r = zr.evaluate_zero_ref("0.10", "0.30", "0.20", "0.10", strict=True)
    assert r.candidate_at_boundary is True
    assert r.candidate_pass_inclusive is True
    assert r.candidate_pass_strict is False

    with pytest.raises(zr.ZeroRefError):
        zr.evaluate_zero_ref(0.5, 0.1, 0.2, 0.2)
    with pytest.raises(zr.ZeroRefError):
        zr.evaluate_zero_ref(0.1, 0.2, 0.15, 0.0)
    with pytest.raises(zr.ZeroRefError):
        zr.evaluate_zero_ref(float("nan"), 0.2, 0.1, 0.2)


def test_cli_zero_ref_check_and_batch(tmp_path: Path, capsys):
    rc_ok = cli.main([
        "zero-ref", "check",
        "--lo", "0.25", "--hi", "0.45", "--candidate", "0.31", "--margin", "0.20",
        "--json",
    ])
    assert rc_ok == 0
    out_ok = json.loads(capsys.readouterr().out)
    assert out_ok["schema"] == zr.SCHEMA
    assert out_ok["classification"] == "informative_match"

    rc_uninf = cli.main([
        "zero-ref", "check",
        "--lo", "-0.05", "--hi", "0.12", "--candidate", "0.03", "--margin", "0.20",
    ])
    assert rc_uninf == 1
    assert "uninformative_pass" in capsys.readouterr().out

    batch_file = tmp_path / "items.jsonl"
    batch_file.write_text(
        json.dumps({"id": "ex1", "lo": 0.25, "hi": 0.45, "candidate": 0.31, "margin": 0.20}) + "\n"
        + json.dumps({"id": "ex2", "lo": -0.05, "hi": 0.12, "candidate": 0.03, "margin": 0.20}) + "\n",
        encoding="utf-8",
    )
    rc_batch = cli.main(["zero-ref", "batch", str(batch_file), "--json"])
    assert rc_batch == 1
    rep = json.loads(capsys.readouterr().out)
    assert rep["summary"]["informative_match"] == 1
    assert rep["summary"]["uninformative_pass"] == 1
