"""htmlreport — 자립형 HTML 리포트 (정직 표기·이스케이프·타임라인·벤더 롤업)."""

import tempfile
import unittest
from pathlib import Path

from organum import adapters, htmlreport, inspector


def _cells():
    return [
        adapters._cell("grok", "g1", model="grok-4.5", in_tok=116_068,
                       tools={"image_gen": 48}, files=["/d/a.png"],
                       first_ts="2026-07-15T12:10:34Z", last_ts="2026-07-15T12:28:23Z"),
        adapters._cell("codex", "c1", model="gpt-5.6-sol", in_tok=34_168_929, out_tok=64_318,
                       cache=32_322_560, tools={"shell": 279}, files=["/d/x.md"],
                       first_ts="2026-07-15T10:14:33Z", last_ts="2026-07-15T13:12:45Z"),
    ]


def _with_duration(cells):
    for c in cells:
        c["duration_s"] = inspector._dur_s(c.get("first_ts"), c.get("last_ts"))
        c["tool_calls"] = sum((c.get("tools") or {}).values())
    return cells


class TestInspectorPage(unittest.TestCase):
    def test_table_timeline_rollup_honesty(self):
        html = htmlreport.inspector_page(_with_duration(_cells()), "duel", 45,
                                         generated_at="2026-07-16 09:00")
        self.assertIn("<!doctype html>", html)
        self.assertIn("grok-4.5", html)
        self.assertIn("3.0h", html)                       # codex duration
        self.assertIn("17.8m", html)                      # grok duration
        self.assertGreaterEqual(html.count("class='tl'"), 2)   # 타임라인 바 2개
        self.assertIn("By vendor", html)                  # 2벤더 → 롤업
        self.assertIn(">—<", html)                        # grok out 미측정 '—'
        self.assertIn("never a silent zero", html)        # 정직 범례

    def test_escapes_hostile_model_name(self):
        c = adapters._cell("claude", "x1", model="<script>alert(1)</script>",
                           last_ts="2026-07-15T10:00:00Z")
        html = htmlreport.inspector_page([c], "p", 45, generated_at="t")
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;", html)

    def test_cli_writes_file(self):
        orig = adapters.snapshot
        adapters.snapshot = lambda cwd, window_min=30.0, adapters=None, deep=False: _cells()
        try:
            with tempfile.TemporaryDirectory() as td:
                out = Path(td) / "r.html"
                rc = inspector.main([td, "--html", str(out)])
                self.assertEqual(rc, 0)
                self.assertIn("organum inspector", out.read_text(encoding="utf-8"))
        finally:
            adapters.snapshot = orig


class TestObservatoryPage(unittest.TestCase):
    def test_bands_and_record_adaptation(self):
        recs = [{"vendor": "claude", "model": "m", "origin": "terminal",
                 "in_tok": 10, "out_tok": 5, "cache": None, "files_touched": 3,
                 "tools": {"Bash": 2}, "first_ts": "2026-07-14T01:00:00Z",
                 "last_ts": "2026-07-14T02:00:00Z"}]
        html = htmlreport.observatory_page(_with_duration(_cells()), recs, "proj", 30,
                                           generated_at="t")
        self.assertIn("Now — live sessions", html)
        self.assertIn("History — accumulated", html)
        self.assertIn("1.0h", html)                       # 레코드 duration 계산됨
        self.assertIn(">3<", html)                        # files_touched → files 열

    def test_empty_history_hint(self):
        html = htmlreport.observatory_page([], [], "proj", 30, generated_at="t")
        self.assertIn("observatory sync", html)


if __name__ == "__main__":
    unittest.main()
