"""roster presence 규율 테스트 — 선언 store + 파생 병합 (shared-cognition §8)."""

import tempfile
import time
import unittest
from pathlib import Path

from organum import roster


class TestRosterStore(unittest.TestCase):
    def test_write_and_read(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            e = roster.write_presence(cwd, "abc123de", name="physis-dev",
                                      focus="roster", open_to=["questions", "pairing"])
            self.assertEqual(e["id"], "abc123de")
            self.assertEqual(e["name"], "physis-dev")
            self.assertEqual(e["open_to"], ["questions", "pairing"])
            self.assertIn("joined_at", e)
            self.assertIn("last_beat", e)
            got = roster.read_presence(cwd)
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0]["focus"], "roster")

    def test_id_full_canonical_preserved_not_truncated(self):
        # canonical 9~40자 id는 값 그대로 보존 — 옛 8자 절단 폐지(critic A-blocker: prefix 충돌 방지)
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            e = roster.write_presence(cwd, "playtester-east")   # 15자 canonical
            self.assertEqual(e["id"], "playtester-east")        # exact, 절단 없음
            self.assertTrue((roster.roster_dir(cwd) / "playtester-east.json").is_file())
            # 앞 8자('playtest') 공유하는 두 canonical id → 두 presence(붕괴 없음)
            roster.write_presence(cwd, "playtester-west")
            self.assertEqual({x["id"] for x in roster.read_presence(cwd)},
                             {"playtester-east", "playtester-west"})
            # 비-canonical(slash)은 sanitize하되 8자 절단 안 함
            e2 = roster.write_presence(cwd, "AB/cd-3456789")
            self.assertNotIn("/", e2["id"])
            self.assertGreater(len(e2["id"]), 8)

    def test_case_insensitive_same_identity(self):
        # 대소문자 계약(2026-07-18): Agent ≡ agent = 같은 셀 — case-insensitive FS 붕괴를 계약으로 정본화
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            roster.write_presence(cwd, "Agent", focus="up")
            roster.write_presence(cwd, "agent", focus="lo")   # 같은 identity → 갱신(새 셀 아님)
            got = roster.read_presence(cwd)
            self.assertEqual(len(got), 1)               # 한 셀
            self.assertEqual(got[0]["id"], "agent")     # 소문자 정규화
            self.assertEqual(got[0]["focus"], "lo")

    def test_reset_presence_does_not_delete_prefix_neighbor(self):
        # critic 필수: reset_presence(B)가 앞 8자 공유하는 A presence를 지우면 안 됨
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            roster.write_presence(cwd, "playtester-east", focus="playtester")
            roster.write_presence(cwd, "playtester-west", focus="playtester")
            roster.reset_presence(cwd, "playtester-west")
            ids = {x["id"] for x in roster.read_presence(cwd)}
            self.assertEqual(ids, {"playtester-east"})  # east 생존, west만 삭제

    def test_partial_update_preserves_fields_and_joined_at(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            first = roster.write_presence(cwd, "cell0001", name="dev", focus="task-A")
            joined = first["joined_at"]
            # focus만 갱신 — name·joined_at 보존, last_beat 새로고침
            second = roster.write_presence(cwd, "cell0001", focus="task-B")
            self.assertEqual(second["name"], "dev")          # 보존
            self.assertEqual(second["focus"], "task-B")      # 갱신
            self.assertEqual(second["joined_at"], joined)    # 최초 가입 시각 불변

    def test_single_writer_one_file_per_cell(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            roster.write_presence(cwd, "aaaa1111", name="A")
            roster.write_presence(cwd, "bbbb2222", name="B")
            files = sorted(p.name for p in roster.roster_dir(cwd).glob("*.json"))
            self.assertEqual(files, ["aaaa1111.json", "bbbb2222.json"])


class TestRosterMerge(unittest.TestCase):
    def test_merge_combines_declared_intent_with_derived_liveness(self):
        now = 1_000_000.0
        declared = [{"id": "aaaa1111", "name": "dev", "focus": "F",
                     "last_beat": "2026-07-12T00:00:00Z"}]
        derived = [{"id": "aaaa1111", "brain": "claude-fable-5", "origin": "terminal",
                    "age": 3.0, "live": True}]
        merged = roster.merge(declared, derived, now=now)
        self.assertEqual(len(merged), 1)
        c = merged[0]
        self.assertEqual(c["name"], "dev")          # 선언 의도
        self.assertEqual(c["brain"], "claude-fable-5")  # 파생 관찰
        self.assertTrue(c["live"])                  # 파생 liveness 존중

    def test_declared_only_liveness_from_beat(self):
        now = time.time()
        recent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10))
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 5000))
        merged = roster.merge(
            [{"id": "live0000", "last_beat": recent},
             {"id": "away0000", "last_beat": old}],
            derived=[], live_secs=90.0, now=now)
        by = {c["id"]: c for c in merged}
        self.assertTrue(by["live0000"]["live"])     # 최근 beat = live
        self.assertFalse(by["away0000"]["live"])    # 오래된 beat = away

    def test_derived_only_cell_appears(self):
        merged = roster.merge([], [{"id": "sub00001", "origin": "subagent", "live": True}])
        self.assertEqual(merged[0]["id"], "sub00001")
        self.assertEqual(merged[0]["origin"], "subagent")

    def test_live_sorted_first(self):
        merged = roster.merge(
            [], [{"id": "z_away00", "live": False, "age": 500},
                 {"id": "a_live00", "live": True}])
        self.assertEqual([c["id"] for c in merged], ["a_live00", "z_away00"])


class TestTwoLensWholeView(unittest.TestCase):
    """필드-state × transcript-activity → coordination_state (two-lens-whole-view-v0)."""

    def _state(self, declared, derived, field_activity=None, **kw):
        m = roster.merge(declared, derived, field_activity=field_activity, **kw)
        return {c["id"]: c for c in m}

    def test_engaged_both_lenses_and_field(self):
        by = self._state([{"id": "c1"}], [{"id": "c1", "live": True, "age": 3}],
                         field_activity={"c1": 60})
        self.assertEqual(by["c1"]["coordination_state"], "engaged")
        self.assertTrue(by["c1"]["field_live"])
        self.assertTrue(by["c1"]["transcript_live"])

    def test_heads_down_working_but_field_silent(self):
        # 몸은 움직이는데 필드 조용 = R3 critic 실패 모드 (넛지 후보)
        by = self._state([{"id": "c2"}], [{"id": "c2", "live": True, "age": 3}],
                         field_activity={})
        self.assertEqual(by["c2"]["coordination_state"], "heads-down")

    def test_idle_neither_lens(self):
        by = self._state([{"id": "c3"}], [{"id": "c3", "live": False, "age": 500}],
                         field_activity={})
        self.assertEqual(by["c3"]["coordination_state"], "idle")

    def test_field_live_rescues_transcript_idle_to_engaged(self):
        # transcript 순간 idle이어도 최근 게시(field-live)면 engaged (transcript 90s는 노이즈)
        by = self._state([{"id": "c6"}], [{"id": "c6", "live": False, "age": 500}],
                         field_activity={"c6": 120})
        self.assertEqual(by["c6"]["coordination_state"], "engaged")
        self.assertFalse(by["c6"]["transcript_live"])   # paused 부가 대상

    def test_field_window_boundary(self):
        # 20분 전 게시(>15분 field_secs) = field-live 아님 → heads-down
        by = self._state([{"id": "c7"}], [{"id": "c7", "live": True, "age": 3}],
                         field_activity={"c7": 1200}, field_secs=900)
        self.assertEqual(by["c7"]["coordination_state"], "heads-down")

    def test_declared_unobserved_no_transcript(self):
        # 선언O·transcript 렌즈 없음 → heads-down/idle 판정 불가 (정직 버킷)
        now = time.time()
        recent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10))
        by = self._state([{"id": "c4", "last_beat": recent}], derived=[],
                         field_activity={}, now=now)
        self.assertEqual(by["c4"]["coordination_state"], "declared-unobserved")
        self.assertTrue(by["c4"]["live"])   # beat=present이지만 조율 상태는 미관측

    def test_unattributed_observed_without_join(self):
        # 활동 관측O·identity join X → role 미주장 (measured≠asserted)
        by = self._state([], [{"id": "c5", "live": True}], field_activity={})
        self.assertEqual(by["c5"]["coordination_state"], "unattributed")

    def test_render_flags_heads_down(self):
        out = roster.render([
            {"id": "c1", "coordination_state": "heads-down", "transcript_live": True, "live": True},
            {"id": "c2", "coordination_state": "engaged", "transcript_live": True, "live": True},
        ], "proj")
        self.assertIn("1 heads-down", out)
        self.assertIn("◐", out)              # heads-down 점
        self.assertIn("heads-down", out)


class TestRosterRender(unittest.TestCase):
    def test_render_empty_and_populated(self):
        self.assertIn("0 cells", roster.render([], "proj"))
        out = roster.render(
            [{"id": "aaaa1111", "origin": "terminal", "brain": "claude-fable-5",
              "live": True, "name": "dev", "focus": "F", "open_to": ["questions"]}], "proj")
        self.assertIn("1 cells (1 live)", out)
        self.assertIn("dev", out)
        self.assertIn("focus: F", out)
        self.assertIn("open: questions", out)


if __name__ == "__main__":
    unittest.main()
