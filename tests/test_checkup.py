"""checkup 테스트 — 특히 기억 decay(구조적 망각) 신호. 판정만, 자동 삭제 없음."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from organum import checkup, state as st


def _state(root: Path) -> Path:
    s, _ = st.init_state_dir(root, "t")
    return s


def _mem(state, *, ts, confidence="tentative", mid="m1", supersedes=None):
    rec = {"id": mid, "ts": ts, "content": "x", "type": "episodic",
           "tags": [], "confidence": confidence, "supersedes": supersedes}
    with (state / "memory" / "memories.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _decay_findings(state):
    return [m for lvl, m in checkup.run(state) if "tentative" in m]


class TestMemoryDecay(unittest.TestCase):
    def test_old_tentative_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            s = _state(Path(td))
            _mem(s, ts=_iso(40))  # 40일 > 30 → stale
            self.assertTrue(any("1개" in f for f in _decay_findings(s)))

    def test_recent_tentative_not_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            s = _state(Path(td))
            _mem(s, ts=_iso(5))  # 최근 → OK
            self.assertTrue(any("없음" in f for f in _decay_findings(s)))

    def test_confirmed_not_decayed(self):
        with tempfile.TemporaryDirectory() as td:
            s = _state(Path(td))
            _mem(s, ts=_iso(100), confidence="confirmed")  # 오래됐어도 tentative 아님
            self.assertTrue(any("없음" in f for f in _decay_findings(s)))

    def test_superseded_tentative_not_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            s = _state(Path(td))
            _mem(s, ts=_iso(40), mid="old")
            _mem(s, ts=_iso(1), mid="new", supersedes="old")  # old는 대체됨 → decay 대상 아님
            # new(tentative, 최근)만 남아 stale 아님
            self.assertTrue(any("없음" in f for f in _decay_findings(s)))

    def test_advisory_only_no_mutation(self):
        # 핵심: checkup은 판정만 — memories.jsonl을 건드리지 않는다
        with tempfile.TemporaryDirectory() as td:
            s = _state(Path(td))
            _mem(s, ts=_iso(40))
            before = (s / "memory" / "memories.jsonl").read_text()
            checkup.run(s)
            after = (s / "memory" / "memories.jsonl").read_text()
            self.assertEqual(before, after)  # 자동 삭제/변경 없음


class TestStaleOpenSession(unittest.TestCase):
    def _session_findings(self, state):
        return [(lvl, m) for lvl, m in checkup.run(state) if "세션" in m]

    def _backdate(self, state, cell_id, minutes):
        from organum import session
        soma = st.ensure_soma(state, cell_id)
        rec = session.start(soma, cell_id, "engine", "작업", "# engine\n")
        p = soma / "sessions" / f"{rec['sid']}.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        old = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        data["started_at"] = old
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_stale_open_session_warned(self):
        # session end 없이 죽은 셀(idle ≥ 문턱) → WARN (advisory — checkup이 세션을 닫지 않는다)
        with tempfile.TemporaryDirectory() as td:
            s = _state(Path(td))
            self._backdate(s, "ghost01", checkup.STALE_SESSION_MIN + 30)
            fs = self._session_findings(s)
            self.assertTrue(any(lvl == checkup.WARN and "stale 열린 세션" in m for lvl, m in fs))
            # advisory: 세션 레코드 무변경 (여전히 열려 있음)
            from organum import session
            self.assertEqual(len(session.open_sessions(s)), 1)

    def test_fresh_open_session_ok(self):
        with tempfile.TemporaryDirectory() as td:
            s = _state(Path(td))
            self._backdate(s, "live01", 5)  # 방금 활동
            fs = self._session_findings(s)
            self.assertTrue(any(lvl == checkup.OK and "모두 활성" in m for lvl, m in fs))

    def test_no_sessions_no_noise(self):
        # 세션 기능을 안 쓰는 현장엔 항목 자체가 안 뜬다
        with tempfile.TemporaryDirectory() as td:
            s = _state(Path(td))
            self.assertEqual(self._session_findings(s), [])


if __name__ == "__main__":
    unittest.main()
