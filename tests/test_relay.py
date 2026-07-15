"""relay 우체통 — 코어 + 스레딩 + watch(무데몬 저지연 폴러) 테스트."""

import tempfile
import unittest
from pathlib import Path

from organum import relay


def _by_file(msgs, fname):
    return next(m for m in msgs if m["file"] == fname)


class TestRelayCore(unittest.TestCase):
    def test_send_and_list(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            fn = relay.send(cwd, "hi", frm="a1", to="b2", topic="t")
            self.assertTrue(fn.endswith(".md"))
            msgs = relay.list_all(cwd)
            self.assertEqual(len(msgs), 1)
            self.assertEqual(msgs[0]["from"], "a1")
            self.assertEqual(msgs[0]["thread"], "")  # 기본 = 스레드 없음

    def test_empty_body_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(relay.send(Path(td), "   "))

    def test_inbox_addressing_and_own_excluded(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            relay.send(cwd, "to me", frm="other", to="me000000")
            relay.send(cwd, "to all", frm="other", to="all")
            relay.send(cwd, "mine", frm="me000000", to="all")   # 내 편지 = 제외
            relay.send(cwd, "to them", frm="other", to="zz999999")  # 남에게 = 제외
            got = relay.inbox(cwd, "me000000")
            bodies = {m["body"] for m in got}
            self.assertEqual(bodies, {"to me", "to all"})

    def test_mark_read_hides(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            fn = relay.send(cwd, "x", frm="o", to="me000000")
            relay.mark_read(cwd, "me000000", fn)
            self.assertEqual(relay.inbox(cwd, "me000000"), [])


class TestRelayThreading(unittest.TestCase):
    def test_explicit_thread(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            relay.send(cwd, "x", frm="a", thread="thr-42")
            self.assertEqual(relay.list_all(cwd)[0]["thread"], "thr-42")

    def test_reply_inherits_parent_filename_as_root(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            parent = relay.send(cwd, "q", frm="a", to="b")           # thread 없음
            child = relay.send(cwd, "re", frm="b", to="a", reply_to=parent)
            cm = _by_file(relay.list_all(cwd), child)
            self.assertEqual(cm["thread"], parent)                   # 부모 파일명이 스레드 루트
            self.assertEqual(cm["in_reply_to"], parent)

    def test_reply_inherits_existing_thread(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            p = relay.send(cwd, "root", frm="a", to="b", thread="T1")
            c = relay.send(cwd, "re", frm="b", to="a", reply_to=p)
            self.assertEqual(_by_file(relay.list_all(cwd), c)["thread"], "T1")


class TestRelayWatch(unittest.TestCase):
    def test_delivers_marks_and_no_replay(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            relay.send(cwd, "hello", frm="other", to="me000000")
            got = []
            n = relay.watch(cwd, "me000000", got.append, mark=True, max_polls=2,
                            _sleep=lambda s: None)
            self.assertEqual(n, 1)
            self.assertEqual([m["body"] for m in got], ["hello"])
            # 읽음 표시됨 → 다음 watch는 재전달 안 함
            n2 = relay.watch(cwd, "me000000", got.append, max_polls=1, _sleep=lambda s: None)
            self.assertEqual(n2, 0)

    def test_only_addressed(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            relay.send(cwd, "not for me", frm="x", to="other000")
            got = []
            relay.watch(cwd, "me000000", got.append, max_polls=1, _sleep=lambda s: None)
            self.assertEqual(got, [])

    def test_idle_self_exit(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            clock = [1000.0]
            n = relay.watch(
                cwd, "me000000", lambda m: None, idle=50,
                _now=lambda: clock[0],
                _sleep=lambda s: clock.__setitem__(0, clock[0] + 100),  # 시간 진행
            )
            self.assertEqual(n, 0)  # 빈 폴 → idle 초과 → 자멸


if __name__ == "__main__":
    unittest.main()
