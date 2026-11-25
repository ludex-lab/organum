"""Tests for organum.occupancy (Wave 1 Collective Memory Hygiene utility)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from organum import cli, occupancy as occ


def test_bucket_cap_prevents_burst_monoculture_while_baselines_collapse():
    # 3 earlier quiet memories (days 1..3) + 12 high-score burst memories on day 4
    items = [
        {"id": "early-1", "t": 1 * 86400.0, "score": 0.60},
        {"id": "early-2", "t": 2 * 86400.0, "score": 0.61},
        {"id": "early-3", "t": 3 * 86400.0, "score": 0.62},
    ] + [
        {"id": f"burst-{i}", "t": 4 * 86400.0 + i * 10.0, "score": 0.90 + i * 0.005}
        for i in range(12)
    ]

    score_ids = {m.item_id for m in occ.select_score_only(items, capacity=6)}
    fifo_ids = {m.item_id for m in occ.select_recency_fifo(items, capacity=6)}
    capped = occ.select_with_bucket_cap(items, capacity=6, bucket_seconds=86400.0, bucket_cap=3)
    cap_ids = {m.item_id for m in capped["survivors"]}

    # Both weak baselines lose ALL 3 early memories to the day-4 burst
    assert not (score_ids & {"early-1", "early-2", "early-3"})
    assert not (fifo_ids & {"early-1", "early-2", "early-3"})
    # Bucket-capped selection preserves all 3 early windows + top 3 burst items
    assert {"early-1", "early-2", "early-3"} <= cap_ids
    assert capped["kept_count"] == 6
    assert capped["max_bucket_occupancy"] == 3
    assert capped["distinct_buckets"] == 4


def test_inspect_jsonl_and_cli_occupancy_read_only(tmp_path: Path, capsys):
    ledger = tmp_path / "memories.jsonl"
    rows = [
        {"id": "m1", "ts": "2026-11-20T10:00:00Z", "confidence": "confirmed", "content": "day 1"},
        {"id": "m2", "ts": "2026-11-21T10:00:00Z", "confidence": "confirmed", "content": "day 2"},
    ] + [
        {"id": f"b{i}", "ts": f"2026-11-24T12:{i:02d}:00Z", "confidence": "well-supported", "content": f"burst {i}"}
        for i in range(8)
    ]
    raw = "\n".join(json.dumps(r) for r in rows) + "\n"
    ledger.write_text(raw, encoding="utf-8")

    rc = cli.main(["occupancy", "--file", str(ledger), "--capacity", "5", "--bucket-hours", "24", "--bucket-cap", "3", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["schema"] == occ.SCHEMA
    assert out["total_items"] == 10
    assert out["raw_max_bucket_count"] == 8
    assert out["max_bucket_occupancy"] == 3
    assert {"m1", "m2"} <= set(out["survivor_ids"])
    # Strictly read-only
    assert ledger.read_text(encoding="utf-8") == raw


def test_invalid_params_fail_fast():
    with pytest.raises(occ.OccupancyError):
        occ.select_with_bucket_cap([], capacity=0, bucket_seconds=60.0, bucket_cap=2)
    with pytest.raises(occ.OccupancyError):
        occ.select_with_bucket_cap([], capacity=4, bucket_seconds=-1.0, bucket_cap=2)


def test_cli_occupancy_human_output_and_missing_file(tmp_path: Path, capsys):
    missing = tmp_path / "nope.jsonl"
    assert cli.main(["occupancy", "--file", str(missing)]) == 1
    assert "찾을 수 없습니다" in capsys.readouterr().err

    ledger = tmp_path / "memories.jsonl"
    ledger.write_text(
        json.dumps({"id": "a", "ts": "2026-11-24T00:00:00Z", "confidence": "confirmed"}) + "\n",
        encoding="utf-8",
    )
    assert cli.main(["occupancy", "--file", str(ledger), "--capacity", "4", "--bucket-cap", "2"]) == 0
    out = capsys.readouterr().out
    assert "temporal occupancy (read-only)" in out
    assert "kept 1/4" in out


def test_cli_occupancy_defaults_to_organum_state_ledger(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init", "--agent", "tester"]) == 0
    capsys.readouterr()
    assert cli.main(["remember", "first episodic record"]) == 0
    capsys.readouterr()

    assert cli.main(["occupancy", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["schema"] == occ.SCHEMA
    assert out["total_items"] >= 1
