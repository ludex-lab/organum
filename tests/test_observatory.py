"""observatory — 관측 영속화 (멱등 append·라스트-라이트-윈·C2 정직성·guard)."""

import json
import tempfile
import unittest
from pathlib import Path

from organum import adapters, observatory


def _c(sid="aaaa1111-full", last_ts="2026-07-15T10:00:00Z", **kw):
    return adapters._cell("claude", sid, last_ts=last_ts, **kw)


def _state(td):
    d = Path(td) / ".organum"
    d.mkdir()
    return d


class TestRecord(unittest.TestCase):
    def setUp(self):
        observatory._recorded.clear()

    def test_writes_month_shard_with_fields(self):
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            n = observatory.record(sd, [_c(model="claude-fable-5", out_tok=100,
                                            origin="subagent", parent="bbbb2222")], "sync")
            self.assertEqual(n, 1)
            shard = sd / "observatory" / "2026-07.jsonl"
            self.assertTrue(shard.is_file())
            rec = json.loads(shard.read_text(encoding="utf-8"))
            self.assertEqual(rec["v"], 1)
            self.assertEqual(rec["out_tok"], 100)
            self.assertIsNone(rec["in_tok"])            # 미측정=None 그대로 (C2)
            self.assertEqual(rec["parent"], "bbbb2222")
            self.assertEqual(rec["capture_reason"], "sync")

    def test_idempotent_same_last_ts(self):
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            self.assertEqual(observatory.record(sd, [_c()], "sync"), 1)
            observatory._recorded.clear()  # 프로세스 캐시 무력화 → 샤드 진실로 멱등 검증
            self.assertEqual(observatory.record(sd, [_c()], "sync"), 0)
            shard = sd / "observatory" / "2026-07.jsonl"
            self.assertEqual(len(shard.read_text(encoding="utf-8").splitlines()), 1)

    def test_advanced_last_ts_appends_and_load_dedupes(self):
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            observatory.record(sd, [_c(out_tok=10)], "web")
            observatory.record(sd, [_c(last_ts="2026-07-15T11:00:00Z", out_tok=99)], "checkup")
            shard = sd / "observatory" / "2026-07.jsonl"
            self.assertEqual(len(shard.read_text(encoding="utf-8").splitlines()), 2)
            recs = observatory.load(sd)
            self.assertEqual(len(recs), 1)               # 라스트-라이트-윈
            self.assertEqual(recs[0]["out_tok"], 99)
            self.assertEqual(recs[0]["capture_reason"], "checkup")

    def test_no_last_ts_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            self.assertEqual(observatory.record(sd, [_c(last_ts=None)], "sync"), 0)
            self.assertFalse((sd / "observatory").exists())

    def test_only_idle_skips_active_cells(self):
        import time
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            fresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            n = observatory.record(sd, [_c(sid="active-1", last_ts=fresh),
                                        _c(sid="settled-1")], "web", only_idle_sec=90.0)
            self.assertEqual(n, 1)                       # settle된 것만
            recs = observatory.load(sd)
            self.assertEqual(recs[0]["session_id"], "settled-1")

    def test_missing_state_dir_noop(self):
        self.assertEqual(observatory.record(Path("/nonexistent-xyz"), [_c()], "web"), 0)


class TestStats(unittest.TestCase):
    def _recs(self):
        return [
            {"vendor": "claude", "origin": "terminal", "model": "m1",
             "in_tok": 100, "out_tok": 50, "cache": 1000, "role": "dev"},
            {"vendor": "claude", "origin": "subagent", "model": "m2",
             "in_tok": 10, "out_tok": 5, "cache": None, "role": None},
            {"vendor": "agy", "origin": "terminal", "model": "m1",
             "in_tok": None, "out_tok": None, "cache": None, "role": "dev"},
        ]

    def test_measured_only_sums_and_unmeasured_counts(self):
        s = observatory.stats(self._recs())
        self.assertEqual(s["sessions"], 3)
        self.assertEqual((s["terminal"], s["subagent"]), (2, 1))
        self.assertEqual(s["in_tok"], 110)               # None 제외 합산
        self.assertEqual(s["in_tok_unmeasured"], 1)
        self.assertEqual(s["cache_unmeasured"], 2)
        self.assertIsNone(s["cost_usd"])                 # 단가표 밖 모델뿐 → None

    def test_group_by_model(self):
        s = observatory.stats(self._recs(), by="model")
        self.assertEqual(set(s["by"]), {"m1", "m2"})
        self.assertEqual(s["by"]["m1"]["sessions"], 2)
        self.assertEqual(s["by"]["m1"]["in_tok"], 100)   # agy 미측정은 합산 제외

    def test_render_smoke(self):
        out = observatory.render_stats(observatory.stats(self._recs(), by="role"), 30, by="role")
        self.assertIn("3 세션", out)
        self.assertIn("—", out)                          # 미측정 표기


class TestReport(unittest.TestCase):
    """리포트 = 지금(live 직독)/오늘/역사(축적) 분리된 밴드 — 합산하지 않는다."""

    def setUp(self):
        observatory._recorded.clear()

    def test_bands_separated(self):
        import time
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            fresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            observatory.record(sd, [_c(sid="old-mara", last_ts="2026-07-10T10:00:00Z",
                                       out_tok=9_000_000, model="m-big")], "sync")
            observatory.record(sd, [_c(sid="today-x", last_ts=fresh, out_tok=50)], "sync")
            orig = adapters.snapshot
            adapters.snapshot = lambda cwd, window_min=30.0: [
                _c(sid="live-now", last_ts=fresh, out_tok=7, tools={"Bash": 3})]
            try:
                out = observatory.report(sd, Path(td), days=30)
            finally:
                adapters.snapshot = orig
            self.assertIn("■ 지금 — 살아있는 세션 1", out)
            self.assertIn("live-now", out)
            self.assertIn("■ 오늘", out)
            self.assertIn("■ 역사 — 2 세션", out)        # live는 역사에 합산 안 됨
            self.assertIn("old-mara", out)               # 대형 세션 top
            self.assertIn("일별 out:", out)

    def test_no_history_hint(self):
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            orig = adapters.snapshot
            adapters.snapshot = lambda cwd, window_min=30.0: []
            try:
                out = observatory.report(sd, Path(td))
            finally:
                adapters.snapshot = orig
            self.assertIn("스냅샷 없음", out)
            self.assertIn("observatory sync", out)       # 시작 힌트


if __name__ == "__main__":
    unittest.main()
