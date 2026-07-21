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
            relay.send(cwd, "mine", frm="me000000", from_id="me000000", to="all")   # 내 편지 = 제외(from_id로)
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

    def test_mark_join_reset_updates_cursor(self):
        # cell id 재사용 시 옛 커서 상속 → history flood. reset=True가 커서를 now로(dogfood ②).
        import time as _t
        from organum import field as fld
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            relay.mark_join(cwd, "c1")                  # 커서 T0
            _t.sleep(1.1)
            relay.send(cwd, "old", frm="x", to="c1")    # T1 > T0
            _t.sleep(1.1)
            relay.mark_join(cwd, "c1")                  # 멱등 — 커서 T0 보존
            self.assertEqual(len(fld.feed(cwd, "relay", "c1", include_read=True)), 1)  # old 보임
            relay.mark_join(cwd, "c1", reset=True)      # 커서 now T2 > T1
            self.assertEqual(len(fld.feed(cwd, "relay", "c1", include_read=True)), 0)  # old 밀림=flood 차단

    def test_full_identity_no_prefix_collision(self):
        # critic A-blocker: 앞 8자 공유하는 두 canonical id가 relay에서 같은 셀로 취급되면 안 됨
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            relay.send(cwd, "for east", frm="s0", to="playtester-east")
            self.assertEqual(len(relay.inbox(cwd, "playtester-east")), 1)  # A 수신
            self.assertEqual(len(relay.inbox(cwd, "playtester-west")), 0)  # B(같은 prefix) 오배송 없음
            # A(east)의 to=all 글: B(west)엔 보이고 A 자신엔 자기 글로 제외
            relay.send(cwd, "hello all", frm="playtester-east", from_id="playtester-east", to="all")
            self.assertIn("playtester-east", [m["from"] for m in relay.inbox(cwd, "playtester-west")])
            self.assertNotIn("playtester-east", [m["from"] for m in relay.inbox(cwd, "playtester-east")])

    def test_from_id_raw_validated_not_truncated(self):
        # 재감사5 A-blocker1: raw from_id를 변형 전 검증 — 41자·개행은 40자 valid로 잘려 다른 셀로
        # 승격되면 안 됨(marker {1,40} 절단과 같은 identity-prefix 승격)
        from organum import field
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            relay.send(cwd, "ok40", frm="d", from_id="a" * 40)     # 40자 valid
            relay.send(cwd, "bad41", frm="d", from_id="a" * 41)    # 41자 → 드롭(변형 금지)
            relay.send(cwd, "badnl", frm="d", from_id="alice\n")   # 개행 → 드롭
            fids = {m["body"]: m.get("from_id", "") for m in field.list_all(cwd, "relay")}
            self.assertEqual(fids["ok40"], "a" * 40)
            self.assertEqual(fids["bad41"], "")   # 40자로 잘리지 않음
            self.assertEqual(fids["badnl"], "")

    def test_read_cursor_dot_hyphen_isolation(self):
        # 재감사3 A-blocker1: .read- cursor가 a.b/a-b를 같은 파일로 공유하면 A 읽기가 B 편지 숨김
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            relay.send(cwd, "broadcast", frm="s0", to="all")
            self.assertEqual(len(relay.inbox(cwd, "a.b")), 1)   # A 읽음(mark_read)
            self.assertEqual(len(relay.inbox(cwd, "a-b")), 1)   # B는 A read state 상속 안 함(격리)

    def test_free_from_not_false_self_excluded(self):
        # 재감사3~4 Blocker3: 자유 --from(canonical 문법이어도)이 from_id 없으면 자기 글로 오인 안 됨
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            relay.send(cwd, "free ab", frm="a b", to="all")               # noncanonical 자유
            self.assertEqual(len(relay.inbox(cwd, "a-b")), 1)
            relay.send(cwd, "free Alice", frm="Alice", to="all")          # canonical-looking 자유(재감사4)
            self.assertEqual(len(relay.inbox(cwd, "alice")), 2)           # alice가 둘 다 봄(오제외 아님)
            # from_id 있는 진짜 셀 발신만 자기제외
            relay.send(cwd, "mine", frm="a-b", from_id="a-b", to="all")
            self.assertNotIn("mine", [m["body"] for m in relay.inbox(cwd, "a-b")])


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
