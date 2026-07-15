"""에스컬레이션 primitive — 세포/chief가 'human 필요'를 플래그, 관제탑이 표면화 (view/medium, dispatch 아님)."""

import tempfile
import unittest
from pathlib import Path

from organum import agora, field, relay, web


class TestEscalation(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_relay_send_escalate_roundtrip(self):
        fn = relay.send(self.cwd, "scribe spiral — 강제 중단 필요", frm="chief", to="human",
                        escalate=True)
        m = next(x for x in relay.list_all(self.cwd) if x["file"] == fn)
        self.assertTrue(m["escalate"])

    def test_default_not_escalated(self):
        fn = relay.send(self.cwd, "평범한 편지", frm="aaaa1111", to="bbbb2222")
        m = next(x for x in relay.list_all(self.cwd) if x["file"] == fn)
        self.assertFalse(m["escalate"])

    def test_agora_post_escalate(self):
        fn = agora.post(self.cwd, "레인 충돌 — human 판단 필요", frm="chief", escalate=True)
        m = next(x for x in agora.list_all(self.cwd) if x["file"] == fn)
        self.assertTrue(m["escalate"])

    def test_web_escalations_aggregate_and_resolve(self):
        relay.send(self.cwd, "일반 편지", frm="aaaa1111", to="bbbb2222")
        f1 = relay.send(self.cwd, "R 에스컬", frm="chief", to="human", escalate=True)
        f2 = agora.post(self.cwd, "A 에스컬", frm="cell1", escalate=True)
        es = web.escalations(self.cwd)
        self.assertEqual({(e["file"], e["field"]) for e in es},
                         {(f1, "relay"), (f2, "agora")})
        # 처리 = human의 보관(가역, 엔벨로프 불변) — 패널에서 사라진다
        self.assertTrue(field.archive(self.cwd, "agora", f2))
        self.assertEqual([e["file"] for e in web.escalations(self.cwd)], [f1])

    def test_payload_includes_escalations(self):
        from organum import adapters
        f1 = relay.send(self.cwd, "human 개입 필요", frm="chief", to="human", escalate=True)
        orig = adapters.snapshot
        adapters.snapshot = lambda cwd, window_min=30.0: []
        try:
            d = web.payload(self.cwd)
        finally:
            adapters.snapshot = orig
        self.assertEqual([e["file"] for e in d["escalations"]], [f1])


if __name__ == "__main__":
    unittest.main()
