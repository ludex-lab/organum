"""organum-inspector — 사후 계측 CLI (duration·전량 파싱·정직 표기)."""

import json
import tempfile
import unittest
from pathlib import Path

from organum import adapters, inspector


class TestDuration(unittest.TestCase):
    def test_dur_and_fmt(self):
        self.assertEqual(inspector._dur_s("2026-07-15T10:00:00Z", "2026-07-15T13:00:00Z"), 10800.0)
        self.assertIsNone(inspector._dur_s(None, "2026-07-15T13:00:00Z"))
        self.assertEqual(inspector._fmt_dur(10800.0), "3.0h")
        self.assertEqual(inspector._fmt_dur(1068), "17.8m")
        self.assertEqual(inspector._fmt_dur(None), "—")


def _fake_cells():
    return [
        adapters._cell("grok", "g1", model="grok-4.5", in_tok=116_068,
                       tools={"image_gen": 48, "run": 52}, files=["/d/a.png"],
                       first_ts="2026-07-15T12:10:34Z", last_ts="2026-07-15T12:28:23Z"),
        adapters._cell("codex", "c1", model="gpt-5.6-sol", in_tok=34_168_929, out_tok=64_318,
                       cache=32_322_560, tools={"shell": 279}, files=["/d/x.md"],
                       first_ts="2026-07-15T10:14:33Z", last_ts="2026-07-15T13:12:45Z"),
    ]


class TestCollectRender(unittest.TestCase):
    def setUp(self):
        import os
        self._lang = os.environ.get("ORGANUM_LANG")
        os.environ["ORGANUM_LANG"] = "ko"   # 표시 단언은 KO 기준으로 고정

    def tearDown(self):
        import os
        if self._lang is None:
            os.environ.pop("ORGANUM_LANG", None)
        else:
            os.environ["ORGANUM_LANG"] = self._lang

    def _collect(self, fake):
        orig = adapters.snapshot
        adapters.snapshot = lambda cwd, window_min=30.0, adapters=None, deep=False: fake
        try:
            return inspector.collect(Path("/x"), 45)
        finally:
            adapters.snapshot = orig

    def test_duration_attached_and_sorted(self):
        cells = self._collect(_fake_cells())
        self.assertEqual(cells[0]["vendor"], "codex")            # first_ts 오름차순
        self.assertAlmostEqual(cells[0]["duration_s"], 10692.0)  # 178.2분
        self.assertAlmostEqual(cells[1]["duration_s"], 1069.0)   # 17.8분

    def test_render_table_totals_and_honesty(self):
        cells = self._collect(_fake_cells())
        out = inspector.render(cells, Path("/x/ludex-design"), 45)
        self.assertIn("2 세션", out)
        self.assertIn("grok-4.5", out)
        self.assertIn("3.0h", out)                               # codex 소요
        self.assertIn("17.8m", out)                              # grok 소요
        self.assertIn("Σ grok", out)                             # 2벤더 → 벤더 합계
        self.assertIn("Σ codex", out)
        self.assertIn("'—' = 미측정", out)                       # 정직 범례
        self.assertIn("—", out)                                  # grok out 미측정 표기

    def test_empty_hint(self):
        out = inspector.render([], Path("/x/empty"), 45)
        self.assertIn("세션 없음", out)
        self.assertIn("--window", out)

    def test_collect_json_roundtrip(self):
        cells = self._collect(_fake_cells())
        self.assertEqual(json.loads(json.dumps(cells))[0]["vendor"], "codex")

    def test_locale_switches_output_language(self):
        import os
        cells = self._collect(_fake_cells())
        orig = os.environ.get("ORGANUM_LANG")
        try:
            os.environ["ORGANUM_LANG"] = "en"
            en = inspector.render(cells, Path("/x/p"), 45)
            self.assertIn("sessions", en)
            self.assertIn("never a silent zero", en)
            os.environ["ORGANUM_LANG"] = "ko"
            ko = inspector.render(cells, Path("/x/p"), 45)
            self.assertIn("세션", ko)
            self.assertIn("미측정", ko)
        finally:
            if orig is None:
                os.environ.pop("ORGANUM_LANG", None)
            else:
                os.environ["ORGANUM_LANG"] = orig


if __name__ == "__main__":
    unittest.main()


class TestCachePctAndPrices(unittest.TestCase):
    def test_cache_pct_math_and_honesty(self):
        self.assertAlmostEqual(inspector._cache_pct(100, 400), 80.0)
        self.assertIsNone(inspector._cache_pct(None, 400))     # 미측정=None(0 아님)
        self.assertIsNone(inspector._cache_pct(100, None))
        self.assertIsNone(inspector._cache_pct(0, 0))
        self.assertEqual(inspector._fmt_pct(None), "—")
        self.assertEqual(inspector._fmt_pct(84.4), "84%")

    def test_user_prices_loader_fail_closed(self):
        from organum import inspect as _ins
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "prices.json"
            p.write_text(json.dumps({
                "solar-open2": {"in": 0.5, "out": 1.5, "cache_read": 0.05},
                "bad-entry": {"in": "free"},                   # 필수키 형 위반 → skip
                "neg": {"in": -1, "out": 1, "cache_read": 0},  # 음수 → skip
            }), encoding="utf-8")
            got = _ins.user_prices(str(p))
            self.assertEqual(sorted(got), ["solar-open2"])
            self.assertEqual(got["solar-open2"]["cache_write"], 0.0)
            self.assertEqual(_ins.user_prices(str(Path(td) / "none.json")), {})  # 부재=빈 표
            bad = Path(td) / "bad.json"
            bad.write_text("{broken", encoding="utf-8")
            self.assertEqual(_ins.user_prices(str(bad)), {})   # 파싱 오류=빈 표

    def test_render_cachepct_role_cost_and_reported(self):
        cells = [{"vendor": "claude", "id": "aa", "model": "solar-open2", "origin": "terminal",
                  "first_ts": "2026-07-26T10:00:00Z", "last_ts": "2026-07-26T10:10:00Z",
                  "duration_s": 600.0, "in_tok": 100_000, "out_tok": 2_000, "cache": 400_000,
                  "cache_pct": 80.0, "tools": {"Read": 3}, "tool_calls": 3, "files": ["a"],
                  "role": "critic"},
                 {"vendor": "grok", "id": "bb", "model": "grok-4.5", "origin": "terminal",
                  "first_ts": "2026-07-26T11:00:00Z", "last_ts": "2026-07-26T11:05:00Z",
                  "duration_s": 300.0, "in_tok": None, "out_tok": None, "cache": None,
                  "cache_pct": None, "tools": {}, "tool_calls": 0, "files": []}]
        reported = [{"backend": "grok-build", "model": "solar-open2", "run_status": "passed",
                     "in_tok": 259_215, "out_tok": 2_032, "cache": 218_688, "gate": "pass"}]
        prices = {"solar-open2": {"in": 0.5, "out": 1.5, "cache_read": 0.05, "cache_write": 0}}
        out = inspector.render(cells, Path("/tmp/x"), 45, reported=reported, prices=prices)
        self.assertIn("80%", out)                              # 세션 c%
        self.assertIn("critic", out)                           # 귀속 role 컬럼
        self.assertIn("$", out)                                # 벤더 롤업 비용(단가 제공 시)
        self.assertIn("grok-build", out)                       # reported 밴드
        self.assertIn("84%", out)                              # reported c% = cached/input
        self.assertNotIn("—%", out)

    def test_render_blind_note_without_reported(self):
        cells = [{"vendor": "claude", "id": "aa", "model": "m", "origin": "terminal",
                  "first_ts": None, "last_ts": "2026-07-26T10:10:00Z", "duration_s": None,
                  "in_tok": None, "out_tok": None, "cache": None, "cache_pct": None,
                  "tools": {}, "tool_calls": 0, "files": []}]
        out = inspector.render(cells, Path("/tmp/x"), 45, reported=None, prices={})
        self.assertIn(inspector._t("legend.blind").strip()[:20], out)  # 블라인드스팟 정직 주석

    def test_reported_runs_reads_shard(self):
        with tempfile.TemporaryDirectory() as td:
            proj = Path(td)
            obs = proj / ".organum" / "observatory"
            obs.mkdir(parents=True)
            # load_reported가 읽는 최소 유효 record 형태로 관측 1건
            from test_observatory import _obs_v1
            import organum.observatory as _o
            (proj / ".organum" / "meta.json").write_text("{}", encoding="utf-8")
            _o.ingest_report(proj / ".organum", _obs_v1())
            rows = inspector.reported_runs(proj)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["backend"], "grok-build")
        self.assertEqual(inspector.reported_runs(Path("/no/such")), [])
