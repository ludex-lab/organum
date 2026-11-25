"""Tests for organum.manifest (Wave 1 Record Manifest Presence Verifier)."""
from __future__ import annotations

import json
from pathlib import Path
import unicodedata

import pytest

from organum import cli, manifest as mf


COVER_SAMPLE = """# Experiment Bundle

## Files

| Group | Files |
|---|---|
| Core | `summary.md` `e1_{agy,codex}.json` |
| Receipts | `run_receipt.json` `raw_trace.jsonl` |
| Notes | `README.md` |
"""


def test_manifest_expands_braces_normalizes_nfd_and_explains_gitignore():
    nfd_name = unicodedata.normalize("NFD", "결과.md")
    nfc_cover = "# Cover\n\n## Files\n\n| Group | Files |\n|---|---|\n| Core | `결과.md` `e1_{a,b}.json` `trace.jsonl` |\n"
    rep = mf.check_manifest_presence(
        nfc_cover,
        [nfd_name, "e1_a.json", "e1_b.json"],
        gitignore_text="*.jsonl\n",
        folder_prefix="bundle",
    )
    assert rep["passed"] is False
    assert rep["missing_count"] == 1
    fail = next(f for f in rep["findings"] if f["level"] == "FAIL")
    assert fail["name"] == "trace.jsonl"
    assert fail["ignored_by"] == "*.jsonl"


def test_manifest_check_dir_and_cli_pass_and_fail(tmp_path: Path, capsys):
    (tmp_path / "README.md").write_text(COVER_SAMPLE, encoding="utf-8")
    for fn in ("summary.md", "e1_agy.json", "e1_codex.json", "run_receipt.json", "raw_trace.jsonl"):
        (tmp_path / fn).write_text("ok\n", encoding="utf-8")

    # Complete directory -> PASS (exit 0)
    rc_ok = cli.main(["manifest", "check", str(tmp_path), "--json"])
    assert rc_ok == 0
    out_ok = json.loads(capsys.readouterr().out)
    assert out_ok["schema"] == mf.SCHEMA
    assert out_ok["passed"] is True
    assert out_ok["missing_count"] == 0

    # Remove receipt -> FAIL (exit 1, missing_receipt)
    (tmp_path / "run_receipt.json").unlink()
    rc_fail = cli.main(["manifest", "check", str(tmp_path), "--json"])
    assert rc_fail == 1
    out_fail = json.loads(capsys.readouterr().out)
    assert out_fail["passed"] is False
    assert any(f["code"] == "missing_receipt" for f in out_fail["findings"])


def test_manifest_rejects_cover_path_escape(tmp_path: Path):
    outside = tmp_path / "outside.md"
    outside.write_text(COVER_SAMPLE, encoding="utf-8")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    with pytest.raises(mf.ManifestError) as exc:
        mf.check_record_dir(bundle, cover_name="../outside.md")
    assert exc.value.code == "path_outside_root"


def test_manifest_cli_human_gitignore_and_error_json(tmp_path: Path, capsys):
    gi = tmp_path / ".gitignore"
    gi.write_text("*.jsonl\n", encoding="utf-8")
    bundle = tmp_path / "exp1"
    bundle.mkdir()
    (bundle / "README.md").write_text(COVER_SAMPLE, encoding="utf-8")
    for fn in ("summary.md", "e1_agy.json", "e1_codex.json", "run_receipt.json", "extra_notes.txt"):
        (bundle / fn).write_text("ok\n", encoding="utf-8")

    # raw_trace.jsonl is missing and explained by .gitignore
    rc = cli.main(["manifest", "check", str(bundle), "--gitignore", str(gi)])
    assert rc == 1
    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "raw_trace.jsonl" in out
    assert "*.jsonl" in out
    assert "listed_not_named" in out

    # Missing ## Files section returns exit 1 + structured JSON error when --json is passed
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "README.md").write_text("# No files section here\n", encoding="utf-8")
    rc_err = cli.main(["manifest", "check", str(bad), "--json"])
    assert rc_err == 1
    err_obj = json.loads(capsys.readouterr().out)
    assert err_obj["verdict"] == "ERROR"
    assert err_obj["code"] == "missing_files_section"
