"""web payload — 크로스-벤더 셀 매핑 (adapters.snapshot 주입)."""

import time
import unittest
from pathlib import Path

from organum import adapters, web


class TestWebPayload(unittest.TestCase):
    def test_cross_vendor_cell_list(self):
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        fake = [
            adapters._cell("claude", "aaaa1111", model="fable-5", origin="terminal",
                           out_tok=100, cache=5, tools={"Edit": 2}, files=["a.py"], last_ts=now),
            adapters._cell("codex", "bbbb2222", model="gpt-5.6-sol", origin="terminal",
                           out_tok=200, tools={"shell": 1}, files=["b.py", "c.py"], last_ts=now),
        ]
        orig = adapters.snapshot
        adapters.snapshot = lambda cwd, window_min=30.0: fake
        try:
            d = web.payload(Path("/x"))
        finally:
            adapters.snapshot = orig
        self.assertEqual(d["cells"], 2)
        self.assertEqual({c["vendor"] for c in d["cell_list"]}, {"claude", "codex"})
        codex = next(c for c in d["cell_list"] if c["vendor"] == "codex")
        self.assertEqual(codex["model"], "gpt-5.6-sol")
        self.assertEqual(codex["touch"], 2)      # files 개수
        self.assertTrue(codex["live"])           # 방금 = live
        self.assertEqual(d["aggregate"]["out"], 300)

    def test_unmeasured_tokens_stay_none(self):
        # 미측정(None)은 0으로 뭉개지 않는다 — 셀엔 null(→프론트 '—'), 집계엔 측정된 것만
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        fake = [
            adapters._cell("claude", "aaaa1111", in_tok=50, out_tok=100, cache=5, last_ts=now),
            adapters._cell("agy", "cccc3333", last_ts=now),        # Tier-1: 토큰 전부 미측정
            adapters._cell("grok", "dddd4444", in_tok=700, last_ts=now),  # out/cache 원천 부재
        ]
        orig = adapters.snapshot
        adapters.snapshot = lambda cwd, window_min=30.0: fake
        try:
            d = web.payload(Path("/x"))
        finally:
            adapters.snapshot = orig
        agy = next(c for c in d["cell_list"] if c["vendor"] == "agy")
        grok = next(c for c in d["cell_list"] if c["vendor"] == "grok")
        self.assertIsNone(agy["out"])
        self.assertIsNone(grok["out"])
        self.assertEqual(grok["in"], 700)
        self.assertEqual(d["aggregate"]["in"], 750)    # 측정된 것만 합산
        self.assertEqual(d["aggregate"]["out"], 100)   # agy·grok None은 제외 (0 아님)

    def test_declared_id_crossref_links_session(self):
        # 관찰 id(세션 해시)와 선언 id(join)가 달라도 ORGANUM_CELL 마커 cross-ref로 세션이 붙는다 (C3)
        import tempfile
        from organum import session
        from organum import state as st
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            state_dir, _ = st.init_state_dir(cwd, "owner")
            soma = st.ensure_soma(state_dir, "worker01")
            session.start(soma, "worker01", "engine", "v1.1 구현", "# engine\n")
            tr = cwd / "fake-transcript.jsonl"
            tr.write_text('{"out":"→ export ORGANUM_CELL=worker01   # 이후 생략 가능"}\n',
                          encoding="utf-8")
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            fake = [adapters._cell("claude", "aaaa1111", last_ts=now, path=str(tr))]
            orig = adapters.snapshot
            adapters.snapshot = lambda c, window_min=30.0: fake
            try:
                d = web.payload(cwd)
            finally:
                adapters.snapshot = orig
            c = d["cell_list"][0]
            self.assertEqual(c["declared"], "worker01")           # 칩·relay 수신인이 이걸 쓴다
            self.assertEqual(c["session"]["role"], "engine")      # 선언 세션이 카드에 붙음
            self.assertEqual(c["session"]["intent"], "v1.1 구현")

    def test_adapter_parent_passthrough(self):
        # 어댑터가 계보(경로/DB)에서 확정한 parent가 카드에 그대로 — 텍스트 cross-ref 휴리스틱 불요
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        fake = [
            adapters._cell("claude", "aaaa1111", origin="terminal", last_ts=now),
            adapters._cell("claude", "deadbeef", origin="subagent", parent="aaaa1111",
                           model="claude-opus-4-8", out_tok=432, last_ts=now),
        ]
        orig = adapters.snapshot
        adapters.snapshot = lambda cwd, window_min=30.0: fake
        try:
            d = web.payload(Path("/x"))
        finally:
            adapters.snapshot = orig
        sub = next(c for c in d["cell_list"] if c["id"] == "deadbeef")
        self.assertEqual(sub["origin"], "subagent")
        self.assertEqual(sub["parent"], "aaaa1111")
        self.assertEqual(d["aggregate"]["out"], 432)  # 서브에이전트 토큰이 집계에 포함

    def test_stale_cell_excluded(self):
        old = "2000-01-01T00:00:00Z"
        fake = [adapters._cell("codex", "old00000", model="x", last_ts=old)]
        orig = adapters.snapshot
        adapters.snapshot = lambda cwd, window_min=30.0: fake
        try:
            d = web.payload(Path("/x"))
        finally:
            adapters.snapshot = orig
        self.assertEqual(d["cells"], 0)          # 30분 창 밖 = 유령 → 안 보임


if __name__ == "__main__":
    unittest.main()
