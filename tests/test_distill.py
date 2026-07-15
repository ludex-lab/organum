"""distill 테스트 — form 집행이 핵심. fake generate로 네트워크 없이."""

import json
import tempfile
import unittest
from pathlib import Path

from organum import distill, guard
from organum.delegate import DelegationResult


def _state(root: Path) -> Path:
    s = root / ".organum"
    (s / "memory").mkdir(parents=True)
    (s / "worldmodel").mkdir()
    (s / "memory" / "events.jsonl").touch()
    (s / "guard.jsonl").touch()
    return s


def _fake(text, ok=True, subtype="success", error=None):
    def gen(system, user):
        return DelegationResult(ok=ok, text=text, subtype=subtype, error=error, cost_usd=0.1)
    return gen


FORM_BODY = """\
## Map
- src/organum: cli→stdout · delegate→claude-cli

## Frontier
- distill의 dogfood를 이 세션 자료로 돌려본다

## Claims
- [confirmed] distill은 형태를 강제한다 (evidence: test_distill)
"""

PROSE_BODY = """\
## Map
distill은 세션 자료를 받아서 세계모델을 만드는 명령이다. 이것은 매우 유용하다.

## Frontier
- 뭔가 시도

## Claims
- [tentative] 주장 (evidence: x)
"""


class TestDistillForm(unittest.TestCase):
    def test_form_body_written(self):
        with tempfile.TemporaryDirectory() as td:
            state = _state(Path(td))
            r = distill.distill(state, "build", "자료...", generate=_fake(FORM_BODY))
            wm = (state / "worldmodel" / "build.md").read_text(encoding="utf-8")
            self.assertIn("organum-format: 0", wm)
            self.assertIn("domain: build", wm)
            self.assertIn("# WM: build", wm)
            self.assertIn("## Frontier", wm)
            self.assertEqual(guard.check_wm_shape(wm), [])  # 저장된 것은 형태 계약 통과

    def test_prose_body_rejected_not_written(self):
        with tempfile.TemporaryDirectory() as td:
            state = _state(Path(td))
            with self.assertRaises(distill.DistillError):
                distill.distill(state, "build", "자료", generate=_fake(PROSE_BODY))
            self.assertFalse((state / "worldmodel" / "build.md").exists())  # prose는 저장 안 됨

    def test_delegation_error_not_written(self):
        with tempfile.TemporaryDirectory() as td:
            state = _state(Path(td))
            with self.assertRaises(distill.DistillError):
                distill.distill(state, "build", "자료",
                                generate=_fake("", ok=False, subtype="error_max_budget_usd",
                                               error="Reached maximum budget"))
            self.assertFalse((state / "worldmodel" / "build.md").exists())

    def test_error_fallback_body_guard_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            state = _state(Path(td))
            with self.assertRaises(distill.DistillError):
                distill.distill(state, "build", "자료", generate=_fake("[Error: CLI timed out]"))
            # guard.jsonl에 차단 기록
            g = (state / "guard.jsonl").read_text(encoding="utf-8")
            self.assertIn("error-fallback", g)

    def test_assemble_strips_stray_frontmatter_and_title(self):
        stray = "---\norganum-format: 0\ndomain: x\n---\n# WM: x\n" + FORM_BODY
        out = distill.assemble("build", stray)
        # front matter 하나만, 제목 하나만
        self.assertEqual(out.count("organum-format:"), 1)
        self.assertEqual(out.count("# WM:"), 1)
        self.assertIn("# WM: build", out)  # domain은 인자 기준

    def test_prior_wm_included_in_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            state = _state(Path(td))
            (state / "worldmodel" / "build.md").write_text("PRIOR-WM-MARKER", encoding="utf-8")
            captured = {}

            def gen(system, user):
                captured["user"] = user
                return DelegationResult(ok=True, text=FORM_BODY, cost_usd=0.1)

            distill.distill(state, "build", "새 자료", generate=gen)
            self.assertIn("PRIOR-WM-MARKER", captured["user"])  # 기존 WM이 프롬프트에 실림

    def test_distill_event_appended(self):
        with tempfile.TemporaryDirectory() as td:
            state = _state(Path(td))
            distill.distill(state, "build", "자료", generate=_fake(FORM_BODY))
            events = [
                json.loads(l)
                for l in (state / "memory" / "events.jsonl").read_text().splitlines()
                if l.strip()
            ]
            self.assertTrue(any(e["kind"] == "distill" for e in events))


if __name__ == "__main__":
    unittest.main()
