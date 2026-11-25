"""Tests for organum.semver_gate (Wave 2 SemVer 2.0.0 & CalVer upgrade gate)."""
from __future__ import annotations

import json

import pytest

from organum import cli, semver_gate as sg


def test_strict_semver_precedence_and_stable_range_policy():
    # SemVer §11 precedence: numeric < alphanumeric, prerelease < release, build ignored
    assert sg.compare_semver(sg.parse_semver("1.0.0-1"), sg.parse_semver("1.0.0-alpha")) == -1
    assert sg.compare_semver(sg.parse_semver("1.0.0-rc.1"), sg.parse_semver("1.0.0")) == -1
    assert sg.compare_semver(sg.parse_semver("1.0.0+build.1"), sg.parse_semver("1.0.0+build.2")) == 0

    # Policy layer excludes prerelease from stable range unless opted in
    dec = sg.evaluate_constraint("1.0.1-rc.1", ">= 1.0.0")
    assert dec.ok is False
    assert dec.reason_code == "prerelease_disallowed_in_stable_range"

    dec_opt = sg.evaluate_constraint("1.0.1-rc.1", ">= 1.0.1-alpha.1")
    assert dec_opt.ok is True

    # Malformed inputs rejected strictly
    for bad in ("v1.0.0", "1.0", "01.0.0", "1.0.0-01"):
        with pytest.raises(sg.SemVerError):
            sg.parse_semver(bad)


def test_calver_cursor_hex_build_vs_prerelease_and_invalid_date():
    tr_ok = sg.classify_upgrade_transition(
        "2026.10.08-1a2b3c4",
        "2026.11.22-e9042b2",
        tool="cursor-agent",
    )
    assert tr_ok.status == "upgraded_stable"
    assert tr_ok.alert is False

    tr_pre = sg.classify_upgrade_transition(
        "2026.10.08-1a2b3c4",
        "2026.11.25-rc.1",
        tool="cursor",
    )
    assert tr_pre.status == "prerelease_jump"
    assert tr_pre.alert is True

    with pytest.raises(sg.SemVerError):
        sg.parse_calver("2026.02.30")


def test_cli_semver_gate_check_and_transition(capsys):
    assert cli.main(["semver-gate", "check", "1.2.3", ">= 1.0.0, < 2.0.0", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["schema"] == sg.SCHEMA
    assert out["ok"] is True

    assert cli.main(["semver-gate", "check", "1.2.4-rc.1", ">= 1.0.0"]) == 1
    assert "prerelease_disallowed_in_stable_range" in capsys.readouterr().out

    rc_up = cli.main(["semver-gate", "transition", "tool 1.0.0", "tool 1.1.0", "--floor", ">= 1.0.0", "--json"])
    assert rc_up == 0
    tr_up = json.loads(capsys.readouterr().out)
    assert tr_up["status"] == "upgraded_stable"

    rc_down = cli.main(["semver-gate", "transition", "2.0.0", "1.9.0", "--json"])
    assert rc_down == 1
    tr_down = json.loads(capsys.readouterr().out)
    assert tr_down["status"] == "downgraded"
    assert tr_down["alert"] is True
