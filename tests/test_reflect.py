"""reflect carry-forward 규율 테스트 (§3.2)."""

import json
import tempfile
import unittest
from pathlib import Path

from organum import reflect, state as st


def _make_state(root: Path) -> Path:
    state = root / ".organum"
    (state / "memory").mkdir(parents=True)
    (state / "memory" / "events.jsonl").touch()
    (state / "guard.jsonl").touch()
    (state / "self.md").write_text(st.SELF_MD_TEMPLATE.format(agent="tester"), encoding="utf-8")
    return state


def _sections(state):
    _, _, secs, _ = reflect._parse((state / "self.md").read_text(encoding="utf-8"))
    return secs


class TestReflect(unittest.TestCase):
    def test_add_to_each_section(self):
        with tempfile.TemporaryDirectory() as td:
            state = _make_state(Path(td))
            reflect.apply(
                state,
                patterns=["보수적 차단 경향 (evidence: rules v2)"],
                lessons=["dogfood가 결함을 빨리 죽인다 (evidence: journal)"],
                questions=["전이 가설은 P3가 답한다"],
                trigger="substrate change",
            )
            secs = _sections(state)
            self.assertEqual(len(secs["Patterns"]["items"]), 1)
            self.assertEqual(len(secs["Lessons"]["items"]), 1)
            self.assertEqual(len(secs["Open questions"]["items"]), 1)
            text = (state / "self.md").read_text(encoding="utf-8")
            self.assertIn("Last reflection: 2", text)  # never → ISO
            self.assertIn("trigger: substrate change", text)
            self.assertIn("<!--", text)  # 주석 placeholder 보존

    def test_carry_forward_not_rewrite(self):
        with tempfile.TemporaryDirectory() as td:
            state = _make_state(Path(td))
            reflect.apply(state, lessons=["첫 교훈"])
            reflect.apply(state, lessons=["둘째 교훈"])
            items = _sections(state)["Lessons"]["items"]
            self.assertEqual(len(items), 2)  # 첫 항목 보존 — 전면 재작성 아님
            self.assertTrue(any("첫 교훈" in i for i in items))

    def test_section_cap_enforced(self):
        with tempfile.TemporaryDirectory() as td:
            state = _make_state(Path(td))
            reflect.apply(state, lessons=[f"교훈 {i}" for i in range(12)])
            with self.assertRaises(reflect.ReflectError):
                reflect.apply(state, lessons=["13번째 — 상한 초과"])

    def test_resolve_open_question(self):
        with tempfile.TemporaryDirectory() as td:
            state = _make_state(Path(td))
            reflect.apply(state, questions=["distill이 prose 회귀를 막는가", "다른 질문"])
            reflect.apply(state, resolve=["prose 회귀"])
            items = _sections(state)["Open questions"]["items"]
            self.assertEqual(len(items), 1)
            self.assertFalse(any("prose 회귀" in i for i in items))

    def test_resolve_unmatched_errors(self):
        with tempfile.TemporaryDirectory() as td:
            state = _make_state(Path(td))
            with self.assertRaises(reflect.ReflectError):
                reflect.apply(state, resolve=["존재하지 않는 질문"])

    def test_guard_blocks_bad_item(self):
        with tempfile.TemporaryDirectory() as td:
            state = _make_state(Path(td))
            with self.assertRaises(reflect.ReflectError):
                reflect.apply(state, lessons=["[Error: CLI timed out]"])
            # 차단 시 self.md 미변경 (all-or-nothing)
            self.assertEqual(len(_sections(state)["Lessons"]["items"]), 0)

    def test_reflect_event_appended(self):
        with tempfile.TemporaryDirectory() as td:
            state = _make_state(Path(td))
            reflect.apply(state, patterns=["x (evidence: y)"], trigger="test")
            events = [
                json.loads(l)
                for l in (state / "memory" / "events.jsonl").read_text().splitlines()
                if l.strip()
            ]
            self.assertTrue(any(e["kind"] == "reflect" for e in events))


if __name__ == "__main__":
    unittest.main()
