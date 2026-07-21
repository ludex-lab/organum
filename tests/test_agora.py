"""agora 토론장 — 개방(open) 정책 테스트 (모두 읽음 · 주소지정 없음 · relay와 필드 분리)."""

import os
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
            agora.post(cwd, "내 글", frm="a1", from_id="a1")   # from_id로 자기 정체성 선언
            self.assertEqual(agora.read(cwd, "a1"), [])  # 내 글은 내 feed에 안 뜸(from_id 매치)

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


class TestLatestGoal(unittest.TestCase):
    """backlog 5: current goal 계약 — topic:goal 최신(cursor·join 무관). acceptance 1~6."""

    def test_no_goal_is_none(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            agora.post(cwd, "잡담", from_id="a")          # 일반 post는 goal 아님
            self.assertIsNone(agora.latest_goal(cwd))     # (acceptance 4: 없으면 None)

    def test_goal_full_envelope_topic_filtered(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            agora.post(cwd, "목표 v1 — UTF-8 한글", topic="goal", frm="chief", from_id="chief")
            g = agora.latest_goal(cwd)
            self.assertIsNotNone(g)
            self.assertEqual(g["topic"], "goal")
            self.assertEqual(g["body"], "목표 v1 — UTF-8 한글")   # (acceptance 6: UTF-8 보존)
            self.assertEqual(g["from"], "chief")
            self.assertTrue(g["file"].endswith(".md") and "/" not in g["file"])  # safe durable file
            self.assertTrue(g["ts"])                                             # 비어있지 않은 ts

    def test_regular_post_does_not_replace_goal(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            agora.post(cwd, "목표 v1", topic="goal", from_id="chief")
            agora.post(cwd, "그냥 잡담", from_id="b")       # 더 최신 일반 post
            self.assertEqual(agora.latest_goal(cwd)["body"], "목표 v1")   # (acceptance 3)

    def test_newer_goal_replaces_deterministic(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            agora.post(cwd, "목표 v1", topic="goal", from_id="chief")
            agora.post(cwd, "목표 v2", topic="goal", from_id="chief")   # 같은 초여도 mtime tie-break
            g = agora.latest_goal(cwd)
            self.assertEqual(g["body"], "목표 v2")          # (acceptance 2: 최신이 교체)
            self.assertEqual(agora.latest_goal(cwd)["file"], g["file"])  # 반복 호출 deterministic


class TestJoinGoalContract(unittest.TestCase):
    """join --json.goal이 cursor 무관 canonical goal을 full envelope로 (acceptance 1·5)."""

    def _join_json(self, td, cid):
        import io
        import contextlib
        import json
        from organum import cli
        saved = os.environ.pop("ORGANUM_CELL", None)
        cwd = os.getcwd()
        os.chdir(td)
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                cli.main(["join", "--role", "critic", "--for", cid, "--json"])
        finally:
            os.chdir(cwd)
            if saved is not None:
                os.environ["ORGANUM_CELL"] = saved
        return json.loads(out.getvalue())

    def test_join_recovers_pre_existing_goal(self):
        from organum import state as st, agora
        with tempfile.TemporaryDirectory() as td:
            st.init_state_dir(Path(td), "owner")
            # goal이 join *이전*에 게시됨
            agora.post(Path(td), "combat v1.2 목표", topic="goal", frm="chief", from_id="chief")
            j = self._join_json(td, "latecell")        # 늦게 join
            self.assertEqual(len(j["goal"]), 1)         # (acceptance 1: pre-existing goal 복구)
            self.assertEqual(j["goal"][0]["topic"], "goal")
            self.assertEqual(j["goal"][0]["body"], "combat v1.2 목표")
            self.assertTrue(j["goal"][0]["file"].endswith(".md"))
            # resume join도 같은 goal ID (acceptance 5)
            j2 = self._join_json(td, "latecell")
            self.assertEqual(j2["goal"][0]["file"], j["goal"][0]["file"])

    def test_join_no_goal_empty_list(self):
        from organum import state as st
        with tempfile.TemporaryDirectory() as td:
            st.init_state_dir(Path(td), "owner")
            j = self._join_json(td, "c1")
            self.assertEqual(j["goal"], [])             # (acceptance 4)


if __name__ == "__main__":
    unittest.main()
