"""guard 테스트 — fixture는 랩 간 교환 포맷(JSONL) 그대로 사용한다."""

import json
import tempfile
import unittest
from pathlib import Path

from organum import guard, state as st

FIXTURES = Path(__file__).parent / "fixtures"


def _load_jsonl(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _make_state_dir(root: Path) -> Path:
    state = root / ".organum"
    (state / "memory").mkdir(parents=True)
    (state / "memory" / "events.jsonl").touch()
    (state / "guard.jsonl").touch()
    return state


class TestEvaluateFixtures(unittest.TestCase):
    def test_exchange_fixture_cases(self):
        for case in _load_jsonl(FIXTURES / "guard_cases.jsonl"):
            with self.subTest(note=case["note"] or case["payload"][:40]):
                v = guard.evaluate(case["payload"])
                self.assertEqual(v.decision, case["expected"], f"payload={case['payload']!r}")
                if case["expected"] != "pass":
                    self.assertEqual(v.rule, case["rule"])

    def test_delegation_is_error_branch(self):
        # budget abort: result 키 부재 → 호출자가 is_error를 넘긴다 (spike-recheck §2)
        v = guard.evaluate("아무 내용", is_error=True)
        self.assertEqual((v.decision, v.rule), ("blocked", "error-fallback"))


class TestWmShape(unittest.TestCase):
    VALID = (
        "---\norganum-format: 0\ndomain: build\nupdated: 2026-07-04T10:00:00Z\n---\n"
        "# WM: build\n\n## Map\n- src: cli→stdout · config→?\n\n"
        "## Frontier\n- docs/notes.md 읽기\n\n"
        "## Claims\n- [tentative] 빌드 단계 없음 (evidence: src/app/main.py)\n"
    )

    def test_valid_passes(self):
        self.assertEqual(guard.check_wm_shape(self.VALID), [])

    def test_missing_section(self):
        text = self.VALID.replace("## Frontier\n- docs/notes.md 읽기\n\n", "")
        self.assertTrue(any("Frontier" in v for v in guard.check_wm_shape(text)))

    def test_prose_paragraph_rejected(self):
        text = self.VALID + "\nX를 한 뒤 Y를 하면 Z가 열린다는 것을 배웠다.\n"
        self.assertTrue(any("산문" in v for v in guard.check_wm_shape(text)))

    def test_claim_without_evidence_rejected(self):
        text = self.VALID.replace(
            "- [tentative] 빌드 단계 없음 (evidence: src/app/main.py)", "- [tentative] 빌드 단계 없음"
        )
        self.assertTrue(any("Claims" in v for v in guard.check_wm_shape(text)))

    def test_missing_front_matter(self):
        self.assertTrue(any("front matter" in v for v in guard.check_wm_shape("# WM: x\n## Map\n")))


class TestStreak(unittest.TestCase):
    def test_streak_sequence_fixture(self):
        seq = _load_jsonl(FIXTURES / "guard_streak_seq.jsonl")[0]
        with tempfile.TemporaryDirectory() as td:
            state = _make_state_dir(Path(td))
            fired_at = None
            for i, payload in enumerate(seq["sequence"], start=1):
                v = guard.evaluate(payload)
                self.assertEqual(v.decision, "blocked")
                guard.record(state, v, "memories", payload)
                if guard.mark_streak_if_reached(state) and fired_at is None:
                    fired_at = i
            self.assertEqual(fired_at, seq["expected_streak_at"])
            self.assertTrue(guard.streak_active(state))

    def test_success_resets_streak(self):
        with tempfile.TemporaryDirectory() as td:
            state = _make_state_dir(Path(td))
            for _ in range(4):
                v = guard.evaluate("[Error: CLI timed out]")
                guard.record(state, v, "memories", "[Error: CLI timed out]")
            self.assertEqual(guard.streak_count(state), 4)
            st.append_event(state, "remember", "정상 저장")  # 성공 이벤트 = 경계 → 리셋 (§7.2)
            self.assertEqual(guard.streak_count(state), 0)

    def test_flagged_resets_streak(self):
        with tempfile.TemporaryDirectory() as td:
            state = _make_state_dir(Path(td))
            for _ in range(3):
                guard.record(state, guard.Verdict("blocked", "error-fallback"), "memories", "x")
            guard.record(state, guard.Verdict("flagged", "error-fallback"), "memories", "긴 교훈")
            self.assertEqual(guard.streak_count(state), 0)  # flagged = 저장됨

    def test_streak_marker_not_counted_and_not_repeated(self):
        with tempfile.TemporaryDirectory() as td:
            state = _make_state_dir(Path(td))

            def streak_events():
                return [r for r in guard._read_jsonl(state / "memory" / "events.jsonl")
                        if r.get("kind") == "guard_streak"]
            for _ in range(guard.STREAK_N):
                guard.record(state, guard.Verdict("blocked", "error-fallback"), "memories", "x")
            self.assertTrue(guard.mark_streak_if_reached(state))
            self.assertEqual(len(streak_events()), 1)              # 알림 1회
            # guard.jsonl에는 v0 위반 마커(target=delegation)가 없어야 한다
            self.assertFalse(any(r.get("rule") == "streak"
                                 for r in guard._read_jsonl(state / "guard.jsonl")))
            # 6번째 blocked — 알림은 반복되지 않되 streak는 유지
            guard.record(state, guard.Verdict("blocked", "error-fallback"), "memories", "x")
            self.assertTrue(guard.mark_streak_if_reached(state))
            self.assertEqual(len(streak_events()), 1)              # 여전히 1회


class TestLudexExchange(unittest.TestCase):
    """ludex regression 셋 교차 검증 — 파일 우체국 수령분 (수신됨 ≠ 검증됨).

    합의(2026-07-04): rule prefix 2층 — storage-boundary/*는 §7.1 규칙으로 매핑,
    turn-fail/*는 위임 층 케이스(여기선 스킵, 위임 모듈 테스트에서 소비),
    tool-arg 클래스는 스코프 밖 명시 보관.
    판정 규약: 그들 blocked ↔ 우리 pass = 하드 충돌 (독성 저장) · 그들 pass ↔ 우리
    blocked = 하드 충돌 (정당 기억 파괴) · 그들 pass ↔ 우리 flagged = 소프트 발산
    (flagged=저장됨이라 데이터 손실 없음 — 리포트만).
    """

    DIR = FIXTURES / "ludex"

    def test_ludex_cases_if_present(self):
        # 모든 라운드 파일 (guard_cases.jsonl, guard_cases_r2.jsonl, ...)
        case_files = sorted(self.DIR.glob("guard_cases*.jsonl"))
        if not case_files:
            self.skipTest("ludex 셋 미수령 — _relay/ 착지 대기 (수신됨 ≠ 검증됨)")
        soft = []
        cases = [c for f in case_files for c in _load_jsonl(f)]
        for case in cases:
            rule = case.get("rule") or ""
            if rule.startswith("turn-fail/"):
                continue  # 위임 층 — guard 스코프 아님
            if "tool-arg" in rule or "scope-out" in (case.get("note") or ""):
                continue  # 합의: 스코프 밖 명시 보관
            with self.subTest(note=case.get("note") or case["payload"][:40]):
                v = guard.evaluate(case["payload"])
                ours = v.decision
                theirs = case["expected"]
                caught_ours = ours in ("blocked", "flagged")
                caught_theirs = theirs in ("blocked", "flagged")
                if theirs == "pass" and ours == "flagged":
                    soft.append((case.get("note"), theirs, ours))
                    continue  # 소프트 발산 — 저장은 되므로 손실 없음
                self.assertEqual(
                    caught_theirs, caught_ours,
                    f"하드 충돌: ludex={theirs} vs ours={ours} ({v.rule}) — {case['payload'][:60]!r}",
                )
        if soft:
            print(f"\n[ludex 교차] 소프트 발산 {len(soft)}건: {soft}")

    def test_ludex_streak_if_present(self):
        seq_file = self.DIR / "streak_seq.jsonl"
        if not seq_file.exists():
            self.skipTest("ludex streak 시퀀스 미수령")
        seq = _load_jsonl(seq_file)[0]
        with tempfile.TemporaryDirectory() as td:
            state = _make_state_dir(Path(td))
            fired_at = None
            for i, payload in enumerate(seq["sequence"], start=1):
                v = guard.evaluate(payload)
                if v.decision == "blocked":
                    guard.record(state, v, "memories", payload)
                    if guard.mark_streak_if_reached(state) and fired_at is None:
                        fired_at = i
            self.assertEqual(fired_at, seq["expected_streak_at"])


class TestInjectionGap(unittest.TestCase):
    """injection 갭의 정직한 문서화 + tripwire.

    ludex 실측(2026-07-05): content-scan(immune)은 deception 탐지지 injection 아님 —
    guard는 "Ignore all previous instructions…"류를 통과시킨다. injection 규칙 클래스는
    실측-후-규칙 + 크로스랩 fixture 교환 규율로 **대기 중**(ludex 다음 라운드).

    이 테스트는 fixture의 expected가 **현재 guard 판정과 일치**하는지 확인한다:
    - 지금: injection 샘플 전부 pass (갭이 문서화됨, 숨기지 않음)
    - injection 규칙이 랜딩하면: guard가 blocked → fixture의 expected(pass)와 불일치 →
      이 테스트가 시끄럽게 깨져 fixture 갱신(expected→blocked, gap→false)을 강제 (tripwire)
    - 대조군(injection을 논하는 정당 기억)은 규칙 후에도 pass 유지해야 함 = 미래 규칙의 경계
    """

    FIXTURE = FIXTURES / "injection_samples.jsonl"

    def test_fixture_expected_matches_current_guard(self):
        cases = _load_jsonl(self.FIXTURE)
        self.assertTrue(cases, "injection 샘플 fixture가 있어야 한다")
        for c in cases:
            with self.subTest(note=c["note"][:40]):
                v = guard.evaluate(c["payload"])
                self.assertEqual(
                    v.decision, c["expected"],
                    f"injection fixture 표류: expected={c['expected']} ours={v.decision} "
                    f"({c['class']}) — injection 규칙이 랜딩했으면 fixture를 갱신하라",
                )

    def test_control_must_pass(self):
        # injection을 '논하는' 정당 기억은 지금도 미래에도 통과 (오차단 금지 경계)
        for c in _load_jsonl(self.FIXTURE):
            if c["class"] == "control":
                self.assertEqual(guard.evaluate(c["payload"]).decision, "pass")

    def test_gap_flag_honest(self):
        # gap=True인 것은 실제로 통과해야 (갭의 정직한 표기) — 아니면 이미 잡히는 것
        for c in _load_jsonl(self.FIXTURE):
            if c.get("gap"):
                self.assertEqual(guard.evaluate(c["payload"]).decision, "pass",
                                 "gap 표기가 부정직: 이미 차단되는데 gap=True")


if __name__ == "__main__":
    unittest.main()
