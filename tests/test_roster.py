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

    def test_id_is_sanitized_and_truncated(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            e = roster.write_presence(cwd, "AB/../cd-3456789")
            self.assertEqual(e["id"], "ABcd-345")  # slash 제거 + 8자
            self.assertTrue((roster.roster_dir(cwd) / "ABcd-345.json").is_file())

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
