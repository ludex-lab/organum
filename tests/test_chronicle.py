"""Tests for organum.chronicle (Wave 2 Git-blob-fingerprinted ledger & diff)."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from organum import chronicle as ch, cli


YAML_V1 = """# Sample generic ledger
entries:
  - id: E-001
    landed: 2026-11-20
    what: "Initial rule" # inline comment allowed
    status: active
  - id: E-002
    landed: 2026-11-21
    what: 'Keep # literal inside quotes' # trailing comment
    status: draft
"""

YAML_V2 = """# Sample generic ledger v2
entries:
  - id: E-001
    landed: 2026-11-20
    what: "Initial rule"
    status: retired
  - id: E-003
    landed: 2026-11-25
    what: "Graduated Wave 2"
    status: active
"""


def test_yaml_ledger_inline_comments_and_snapshot_diff():
    s1 = ch.parse_yaml_ledger(YAML_V1, source_path="ledger.yaml", label="v1")
    s2 = ch.parse_yaml_ledger(YAML_V2, source_path="ledger.yaml", label="v2")
    assert len(s1.entries) == 2
    assert s1.entries[0].title == "Initial rule"
    assert s1.entries[1].title == "Keep # literal inside quotes"

    diff = ch.diff_snapshots(s1, s2)
    assert [e.entry_id for e in diff.added] == ["E-003"]
    assert [e.entry_id for e in diff.removed] == ["E-002"]
    assert [c.entry_id for c in diff.changed] == ["E-001"]
    assert diff.changed[0].field_changes["status"] == {"before": "active", "after": "retired"}


def test_markdown_chronicle_and_fail_fast_errors():
    md = "# Chronicle\n\n## 2026-11-24\n- [W1] Wave 1 landed\n- 2026-11-25 | W2 | Wave 2 ready\n"
    snap = ch.parse_markdown_chronicle(md, source_path="CHRONICLE.md")
    assert [e.entry_id for e in snap.entries] == ["W1", "W2"]

    with pytest.raises(ch.ChronicleParseError):
        ch.parse_yaml_ledger("entries:\n  - id: dup\n  - id: dup\n", source_path="bad.yaml")
    with pytest.raises(ch.ChronicleParseError):
        ch.parse_yaml_ledger('entries:\n  - id: bad\n    what: "unterminated\n', source_path="bad.yaml")


def test_cli_chronicle_check_diff_and_git_rev_provenance(tmp_path: Path, capsys):
    f1 = tmp_path / "v1.yaml"
    f2 = tmp_path / "v2.yaml"
    f1.write_text(YAML_V1, encoding="utf-8")
    f2.write_text(YAML_V2, encoding="utf-8")

    assert cli.main(["chronicle", "check", str(f1), "--json"]) == 0
    out_check = json.loads(capsys.readouterr().out)
    assert out_check["schema"] == ch.SCHEMA
    assert out_check["entry_count"] == 2

    assert cli.main(["chronicle", "diff", str(f1), str(f2), "--json"]) == 0
    out_diff = json.loads(capsys.readouterr().out)
    assert out_diff["counts"] == {"added": 1, "removed": 1, "changed": 1, "unchanged": 0}

    # Verify exact Git blob SHA-1 matches `git hash-object` in an isolated repo
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    ledger = repo / "ledger.yaml"
    ledger.write_text(YAML_V1, encoding="utf-8")
    subprocess.run(["git", "add", "ledger.yaml"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "v1"], cwd=repo, check=True)
    expected_blob = subprocess.check_output(["git", "hash-object", "ledger.yaml"], cwd=repo, text=True).strip()
    ledger.write_text(YAML_V2, encoding="utf-8")
    subprocess.run(["git", "commit", "-aq", "-m", "v2"], cwd=repo, check=True)

    rc_git = cli.main([
        "chronicle", "diff", "HEAD~1", "HEAD",
        "--git-repo", str(repo),
        "--rel-path", "ledger.yaml",
        "--json",
    ])
    assert rc_git == 0
    git_diff = json.loads(capsys.readouterr().out)
    assert git_diff["before"]["blob_fingerprint"] == expected_blob
    assert git_diff["counts"]["added"] == 1
