"""agora 토론장 — 개방(open) 정책 테스트 (모두 읽음 · 주소지정 없음 · relay와 필드 분리)."""

import tempfile
import unittest
from pathlib import Path

from organum import agora, field, relay


class TestAgoraOpen(unittest.TestCase):
    def test_post_and_everyone_reads(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            agora.post(cwd, "다들 이거 어때?", frm="a1")
            for who in ("b2", "c3"):  # 서로 다른 세포 모두 읽는다 (주소지정 없음)
                got = agora.read(cwd, who)
                self.assertEqual(len(got), 1)
                self.assertEqual(got[0]["body"], "다들 이거 어때?")

    def test_own_excluded(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            agora.post(cwd, "내 글", frm="a1")
            self.assertEqual(agora.read(cwd, "a1"), [])  # 내 글은 내 feed에 안 뜸

    def test_no_addressing_filter(self):
        # relay였다면 to=특정인이라 안 보이겠지만, agora는 개방이라 다 보인다
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            field.post(cwd, "agora", "특정인 대상처럼 보이나", frm="a1", to="zz999999")
            self.assertEqual(len(agora.read(cwd, "b2")), 1)

    def test_thread_inheritance(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            root = agora.post(cwd, "주제", frm="a1")
            agora.post(cwd, "동의", frm="b2", reply_to=root)
            reply = next(m for m in agora.list_all(cwd) if m["body"] == "동의")
            self.assertEqual(reply["thread"], root)
            self.assertEqual(reply["in_reply_to"], root)

    def test_watch_open(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            agora.post(cwd, "새 글", frm="a1")
            got = []
            n = agora.watch(cwd, "b2", got.append, max_polls=2, _sleep=lambda s: None)
            self.assertEqual(n, 1)
            self.assertEqual(got[0]["body"], "새 글")

    def test_field_separated_from_relay(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            agora.post(cwd, "광장 글", frm="a1")
            self.assertEqual(relay.list_all(cwd), [])       # relay 필드엔 안 섞임
            self.assertEqual(len(agora.list_all(cwd)), 1)
            self.assertTrue((cwd / ".organum" / "agora").is_dir())
            self.assertFalse((cwd / ".organum" / "relay").is_dir())


if __name__ == "__main__":
    unittest.main()
