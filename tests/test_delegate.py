"""위임 모듈 테스트 — 실측 JSON 형태(spike-recheck)로. 네트워크 없음."""

import json
import tempfile
import unittest
from pathlib import Path

from organum import delegate, guard

FIXTURES = Path(__file__).parent / "fixtures"

# spike-recheck §1: 성공 응답 (웜 캐시)
SUCCESS = json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "result": "4", "num_turns": 1, "total_cost_usd": 0.055,
})
# spike-recheck §2: budget abort — result 키 자체가 없다
BUDGET_ABORT = json.dumps({
    "subtype": "error_max_budget_usd", "is_error": True,
    "errors": ["Reached maximum budget ($0.001)"], "terminal_reason": None,
})
# is_error=true 이면서 result 키도 있는 병리 케이스 — is_error가 이겨야 한다
ERROR_WITH_RESULT = json.dumps({
    "subtype": "error_during_execution", "is_error": True,
    "result": "부분 출력", "errors": ["stream interrupted"],
})


class TestParseResult(unittest.TestCase):
    def test_success(self):
        r = delegate.parse_result(SUCCESS)
        self.assertTrue(r.ok)
        self.assertEqual(r.text, "4")
        self.assertEqual(r.cost_usd, 0.055)

    def test_budget_abort_no_result_key(self):
        # 핵심: .result 접근 없이 is_error로 분기 — result 키 부재로 크래시하지 않는다
        r = delegate.parse_result(BUDGET_ABORT)
        self.assertFalse(r.ok)
        self.assertEqual(r.subtype, "error_max_budget_usd")
        self.assertIn("budget", r.error.lower())
        self.assertEqual(r.text, "")

    def test_is_error_beats_result(self):
        r = delegate.parse_result(ERROR_WITH_RESULT)
        self.assertFalse(r.ok)  # result 키가 있어도 is_error가 이긴다
        self.assertEqual(r.text, "")

    def test_malformed_json(self):
        r = delegate.parse_result("not json at all", returncode=1)
        self.assertFalse(r.ok)
        self.assertIn("파싱", r.error)

    def test_non_object_json(self):
        r = delegate.parse_result("[1, 2, 3]")
        self.assertFalse(r.ok)


class TestStreakGate(unittest.TestCase):
    def _state(self, root):
        s = root / ".organum"
        (s / "memory").mkdir(parents=True)
        (s / "memory" / "events.jsonl").touch()
        (s / "guard.jsonl").touch()
        return s

    def test_active_streak_blocks_delegation(self):
        with tempfile.TemporaryDirectory() as td:
            state = self._state(Path(td))
            for _ in range(guard.STREAK_N):
                guard.record(state, guard.Verdict("blocked", "error-fallback"), "memories", "x")
            with self.assertRaises(delegate.StreakBlocked):
                # cli 미실행 — streak 게이트가 서브프로세스 전에 막는다
                delegate.delegate("무엇이든", state_dir=state, cli="/nonexistent-cli")

    def test_override_bypasses_streak(self):
        with tempfile.TemporaryDirectory() as td:
            state = self._state(Path(td))
            for _ in range(guard.STREAK_N):
                guard.record(state, guard.Verdict("blocked", "error-fallback"), "memories", "x")
            # override면 게이트 통과 → 없는 cli라 FileNotFoundError 경로로 (StreakBlocked 아님)
            r = delegate.delegate("x", state_dir=state, cli="/nonexistent-cli", override_streak=True)
            self.assertFalse(r.ok)
            self.assertIn("없음", r.error)


class TestBudgetFloor(unittest.TestCase):
    def test_cold_cache_floor(self):
        cmd = delegate.build_cmd("claude", max(0.05, delegate.MIN_BUDGET_USD))
        i = cmd.index("--max-budget-usd")
        self.assertEqual(float(cmd[i + 1]), delegate.MIN_BUDGET_USD)


class TestTurnFailDefenseInDepth(unittest.TestCase):
    """ludex turn-fail/* 케이스의 소비처 = 위임 층 (is_error 선분기).

    합의: turn-fail은 위임 모듈이 1차 방어. 추가로 — 만약 turn-fail 산출물이 저장 경계까지
    새더라도 guard가 잡는다는 심층방어를 검증 (ludex에 보고한 '셋 다 우리 초크포인트에서도
    잡힌다'는 주장의 테스트).
    """

    def test_turn_fail_payloads_caught_at_storage_boundary(self):
        cases_file = FIXTURES / "ludex" / "guard_cases.jsonl"
        if not cases_file.exists():
            self.skipTest("ludex 셋 미수령")
        turn_fails = [
            json.loads(l) for l in cases_file.read_text(encoding="utf-8").splitlines()
            if l.strip() and (json.loads(l).get("rule") or "").startswith("turn-fail/")
        ]
        self.assertTrue(turn_fails, "turn-fail 케이스가 있어야 한다")
        for case in turn_fails:
            with self.subTest(rule=case["rule"]):
                # 위임 층을 뚫고 저장까지 새더라도 guard가 차단해야 한다
                v = guard.evaluate(case["payload"])
                self.assertFalse(v.ok, f"심층방어 실패: {case['payload'][:50]!r} 통과됨")


if __name__ == "__main__":
    unittest.main()
