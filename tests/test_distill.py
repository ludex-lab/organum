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

    def test_prose_body_rejected_under_map_profile(self):
        with tempfile.TemporaryDirectory() as td:
            state = _state(Path(td))
            with self.assertRaises(distill.DistillError):
                distill.distill(state, "build", "자료", generate=_fake(PROSE_BODY))  # 기본=map
            self.assertFalse((state / "worldmodel" / "build.md").exists())  # map 프로파일은 거부


class TestDistillProfiles(unittest.TestCase):
    """form>content는 도메인-특정(P3 null) → 프로파일. prose 프로파일은 서술을 저장한다."""

    NARRATIVE = ("이 코딩 프로젝트는 CLI와 어댑터로 구성된다. 어제 관측층을 손봤고, "
                 "guard 계약이 아직 유동적이다. 다음엔 프로파일 반영을 검증한다.")

    def test_prose_profile_accepts_narrative(self):
        with tempfile.TemporaryDirectory() as td:
            state = _state(Path(td))
            r = distill.distill(state, "coding", self.NARRATIVE,
                                profile="prose", generate=_fake(self.NARRATIVE))
            self.assertEqual(r["profile"], "prose")
            wm = (state / "worldmodel" / "coding.md").read_text(encoding="utf-8")
            self.assertIn("# WM: coding", wm)                 # 저장됨(형태 거부 없음)
            self.assertIn("어댑터", wm)
            self.assertNotEqual(guard.check_wm_shape(wm), [])  # form 계약은 안 통과하지만 저장됨

    def test_prose_profile_still_guards_error_fallback(self):
        # 프로파일과 무관하게 저장 경계(error-fallback)는 항상 막는다
        with tempfile.TemporaryDirectory() as td:
            state = _state(Path(td))
            with self.assertRaises(distill.DistillError):
                distill.distill(state, "coding", "자료", profile="prose",
                                generate=_fake("[Error: CLI timed out]"))
            self.assertFalse((state / "worldmodel" / "coding.md").exists())

    def test_unknown_profile_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            state = _state(Path(td))
            with self.assertRaises(distill.DistillError):
                distill.distill(state, "x", "자료", profile="freeform", generate=_fake("무엇이든"))

    def test_map_profile_is_default(self):
        with tempfile.TemporaryDirectory() as td:
            state = _state(Path(td))
            r = distill.distill(state, "build", "자료", generate=_fake(FORM_BODY))
            self.assertEqual(r["profile"], "map")             # 기본=map(하위호환)

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
