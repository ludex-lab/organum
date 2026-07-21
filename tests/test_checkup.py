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


class TestCheckupMapAutoSync(unittest.TestCase):
    """map 자동 sync 편승 (도그푸드: map이 개발 속도를 못 따라감 → checkup이 병합).
    실 git repo로 E2E — read 마킹 보존까지 확인."""

    def _git_repo(self, td):
        import subprocess
        r = Path(td)
        env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "HOME": td}
        subprocess.run(["git", "init", "-q"], cwd=r, env=env, check=True)
        (r / "a.py").write_text("a\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=r, env=env, check=True)
        return r, env

    def _run(self, td, argv):
        import os
        from organum import cli
        cwd = os.getcwd()
        os.chdir(td)
        try:
            return cli.main(argv)
        finally:
            os.chdir(cwd)

    def _map_bytes(self, r):
        return (r / ".organum" / "map" / "repo.map.json").read_bytes()

    def test_default_checkup_is_diagnose_only_no_map_write(self):
        # 기본 checkup은 [shared] map을 건드리지 않는다 (critic A: 진단-only, CI diff 0)
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            r, env = self._git_repo(td)
            st.init_state_dir(r, "t")
            (r / "b.py").write_text("b\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=r, env=env, check=True)
            before = self._map_bytes(r)
            self.assertEqual(self._run(td, ["checkup"]), 0)
            self.assertEqual(self._map_bytes(r), before)           # 원바이트 불변
            files = {k for k, v in st.load_repo_map(r / ".organum")["nodes"].items()
                     if v.get("kind") == "file"}
            self.assertEqual(files, {"a.py"})                      # b.py 미반영(진단만)

    def test_sync_map_opt_in_merges_and_preserves_read(self):
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            r, env = self._git_repo(td)
            st.init_state_dir(r, "t")
            m = st.load_repo_map(r / ".organum")
            m["nodes"]["a.py"]["status"] = "read"
            m["nodes"]["a.py"]["sha"] = "deadbeef"
            st.write_json(r / ".organum" / "map" / "repo.map.json", m)
            (r / "b.py").write_text("b\n", encoding="utf-8")
            (r / "c.py").write_text("c\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=r, env=env, check=True)

            self.assertEqual(self._run(td, ["checkup", "--sync-map"]), 0)

            after = st.load_repo_map(r / ".organum")
            files = {k for k, v in after["nodes"].items() if v.get("kind") == "file"}
            self.assertEqual(files, {"a.py", "b.py", "c.py"})       # opt-in 시 병합
            self.assertEqual(after["nodes"]["a.py"]["status"], "read")  # read 보존
            self.assertEqual(after["nodes"]["b.py"]["status"], "unvisited")

    def test_future_format_blocks_all_state_writes(self):
        # 포맷 게이트(critic A + 잔여): 미래 포맷이면 map·events·observatory 어떤 것도 안 쓴다.
        # snapshot에 유효 cell을 주입해 observatory 스윕까지 막히는지 확인.
        import subprocess
        import time as _time
        from organum import adapters
        with tempfile.TemporaryDirectory() as td:
            r, env = self._git_repo(td)
            st.init_state_dir(r, "t")
            meta = json.loads((r / ".organum" / "meta.json").read_text(encoding="utf-8"))
            meta["format_version"] = 99
            (r / ".organum" / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
            (r / "b.py").write_text("b\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=r, env=env, check=True)
            now = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())
            # cell을 밖에서 미리 만들어 고정 반환 — 람다 안에서 만들면 shadowing으로 회귀가
            # AttributeError→broad except에 삼켜져 false-positive green이 된다(critic tripwire).
            cell = adapters._cell("claude", "cell1234", out_tok=5, last_ts=now, path="/x.jsonl")
            orig = adapters.snapshot
            adapters.snapshot = lambda *a, **k: [cell]
            map_before = self._map_bytes(r)
            ev_before = (r / ".organum" / "memory" / "events.jsonl").read_bytes()
            try:
                rc = self._run(td, ["checkup", "--sync-map"])
            finally:
                adapters.snapshot = orig
            self.assertEqual(rc, 1)                                 # 건강 ERROR → 비-0
            self.assertEqual(self._map_bytes(r), map_before)       # map 불변
            self.assertEqual((r / ".organum" / "memory" / "events.jsonl").read_bytes(), ev_before)
            self.assertFalse((r / ".organum" / "observatory").exists())  # observatory 샤드 0


class TestLegacyId8Ghost(unittest.TestCase):
    def test_legacy_id8_roster_ghost_detected(self):
        # 재감사3 A-P1: full-id 이전 8자-절단 roster presence를 whole-view ghost로 탐지(경고)
        from organum import roster, session
        with tempfile.TemporaryDirectory() as td:
            state = _state(Path(td)); cwd = Path(td)
            soma = st.ensure_soma(state, "playtester-east")
            session.start(soma, "playtester-east", "playtester", "v1", "# c\n")
            rdir = roster.roster_dir(cwd); rdir.mkdir(parents=True, exist_ok=True)
            (rdir / "playtest.json").write_text(  # 옛 8자 절단 형식 ghost
                json.dumps({"id": "playtest", "focus": "old"}), encoding="utf-8")
            findings = checkup.run(state)
            self.assertTrue(any("legacy roster ghost" in msg for _lvl, msg in findings))

    def test_dot_roster_legacy_detected(self):
        # 재감사5 A-P1: 옛 _id8은 점을 제거(a.b→ab) — prefix 매칭이 못 잡던 것을 옛 인코딩 계산으로 탐지
        from organum import roster, session
        with tempfile.TemporaryDirectory() as td:
            state = _state(Path(td)); cwd = Path(td)
            soma = st.ensure_soma(state, "a.b")
            session.start(soma, "a.b", "engine", "v1", "# c\n")
            rdir = roster.roster_dir(cwd); rdir.mkdir(parents=True, exist_ok=True)
            (rdir / "ab.json").write_text(  # 옛 _id8("a.b")="ab" (점 제거)
                json.dumps({"id": "ab", "focus": "old"}), encoding="utf-8")
            findings = checkup.run(state)
            self.assertTrue(any("legacy roster ghost" in msg for _lvl, msg in findings))

    def test_dot_legacy_ambiguity_warned(self):
        # 재감사6 A-P1: a.b·ab 둘 다 선언 + presence ab = 옛 인코딩이 여러 canonical로 수렴 → ambiguity
        from organum import roster, session
        with tempfile.TemporaryDirectory() as td:
            state = _state(Path(td)); cwd = Path(td)
            for c in ("a.b", "ab"):
                soma = st.ensure_soma(state, c)
                session.start(soma, c, "engine", "v1", "# c\n"); session.end(soma)
            rdir = roster.roster_dir(cwd); rdir.mkdir(parents=True, exist_ok=True)
            (rdir / "ab.json").write_text(json.dumps({"id": "ab", "focus": "x"}), encoding="utf-8")
            findings = checkup.run(state)
            self.assertTrue(any("ambiguity" in msg for _lvl, msg in findings))

    def test_noncanonical_soma_dir_detected_not_cleanup(self):
        # 재감사5 A-blocker2: personal soma(cells/) case legacy는 삭제 금지 경고(cleanup 아님)
        with tempfile.TemporaryDirectory() as td:
            state = _state(Path(td))
            (state / "cells" / "Worker").mkdir(parents=True)   # 옛 case-preserving soma
            findings = checkup.run(state)
            self.assertTrue(any("personal" in msg and "cleanup 금지" in msg for _lvl, msg in findings))

    def test_mixed_case_roster_legacy_detected(self):
        # 재감사4: 비정규화(mixed-case) roster presence 탐지
        from organum import roster
        with tempfile.TemporaryDirectory() as td:
            state = _state(Path(td)); cwd = Path(td)
            rdir = roster.roster_dir(cwd); rdir.mkdir(parents=True, exist_ok=True)
            (rdir / "Worker.json").write_text(  # 옛 case-preserving 형식(id != cell_key)
                json.dumps({"id": "Worker", "focus": "old"}), encoding="utf-8")
            findings = checkup.run(state)
            self.assertTrue(any("비정규화" in msg for _lvl, msg in findings))


if __name__ == "__main__":
    unittest.main()
