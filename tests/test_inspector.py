"""organum-inspector — 사후 계측 CLI (duration·전량 파싱·정직 표기)."""

import json
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
