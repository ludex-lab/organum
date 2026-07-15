"""organum session — 세션 라이프사이클(state/discipline) 봉투 테스트."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from organum import cli
from organum import session
from organum import state as st


class TestSessionModule(unittest.TestCase):
    def _soma(self, td):
        state_dir, _ = st.init_state_dir(Path(td), "engine")
        return state_dir  # owner soma == state_dir(.organum) 루트

    def test_start_records_and_blocks_double_open(self):
        with tempfile.TemporaryDirectory() as td:
            soma = self._soma(td)
            rec = session.start(soma, "engine", "engine", "warren 씬버그 수정", "# engine\n")
            self.assertIsNone(rec["ended_at"])
            self.assertEqual(rec["role"], "engine")
            self.assertEqual(rec["intent"], "warren 씬버그 수정")
            self.assertTrue((soma / "sessions" / f"{rec['sid']}.json").exists())
            # 열린 세션 중복 거부 (한 세포 = 한 열린 세션)
            with self.assertRaises(session.SessionError):
                session.start(soma, "engine", "reviewer", "다른 일", "# reviewer\n")

    def test_empty_intent_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            soma = self._soma(td)
            with self.assertRaises(session.SessionError):
                session.start(soma, "engine", "engine", "   ", "# engine\n")

    def test_note_requires_open_and_appends(self):
        with tempfile.TemporaryDirectory() as td:
            soma = self._soma(td)
            with self.assertRaises(session.SessionError):
                session.note(soma, "비트")  # 열린 세션 없음
            session.start(soma, "engine", "engine", "의도", "# engine\n")
            session.note(soma, "테스트 통과")
            rec = session.note(soma, "커밋함")
            self.assertEqual(len(rec["notes"]), 2)
            self.assertEqual(rec["notes"][0]["text"], "테스트 통과")

    def test_status_pull(self):
        with tempfile.TemporaryDirectory() as td:
            soma = self._soma(td)
            self.assertIsNone(session.status(soma))  # 열린 세션 없음
            session.start(soma, "engine", "engine", "의도", "# engine\n")
            session.note(soma, "b1")
            s = session.status(soma)
            self.assertEqual(s["notes"], 1)
            self.assertEqual(s["role"], "engine")
            self.assertGreaterEqual(s["age_min"], 0)
            self.assertGreaterEqual(s["idle_min"], 0)

    def test_end_closes_with_ship_and_peers(self):
        with tempfile.TemporaryDirectory() as td:
            soma = self._soma(td)
            session.start(soma, "engine", "engine", "의도", "# engine\n")
            peers = [{
                "peer": "codex · reviewer",
                "strengths": ["seam 버그 먼저 짚음"],
                "frictions": "컨텍스트 재확인 잦음",  # 문자열 → 리스트 강제
                "would_pair_again": "yes",            # 문자열 → bool
                "role_fit": "리뷰어 적격",
            }]
            rec = session.end(soma, shipped=["session.py", " "], peers=peers)
            self.assertIsNotNone(rec["ended_at"])
            self.assertEqual(rec["shipped"], ["session.py"])  # 공백 항목 제거
            pn = rec["peers"][0]
            self.assertEqual(pn["frictions"], ["컨텍스트 재확인 잦음"])
            self.assertIs(pn["would_pair_again"], True)
            self.assertEqual(pn["role_fit"], "리뷰어 적격")
            # 닫힌 뒤엔 열린 세션 없음
            self.assertIsNone(session.status(soma))
            with self.assertRaises(session.SessionError):
                session.end(soma)

    def test_peer_requires_peer_field(self):
        with tempfile.TemporaryDirectory() as td:
            soma = self._soma(td)
            session.start(soma, "engine", "engine", "의도", "# engine\n")
            with self.assertRaises(session.SessionError):
                session.end(soma, peers=[{"strengths": ["x"]}])  # peer 없음

    def test_guest_soma_is_single_writer(self):
        # 게스트 세포 세션은 자기 soma(cells/<slug>/)에만 산다 — 루트/타 세포 오염 없음
        with tempfile.TemporaryDirectory() as td:
            root = self._soma(td)
            guest = st.ensure_soma(root, "solar-open2")
            self.assertNotEqual(guest, root)
            session.start(guest, "solar-open2", "scribe", "기록", "# scribe\n")
            self.assertTrue((guest / "sessions").exists())
            self.assertFalse((root / "sessions").exists())  # 루트(owner) 오염 없음

    def test_resolve_charter_default_and_commons_override(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._soma(td)
            rev = session.resolve_charter(root, "reviewer")
            self.assertIn("reviewer", rev)
            self.assertIn("조율 규율", rev)  # 공통 규율이 모든 charter에 append됨
            self.assertIn("먼저 사냥", rev)  # reviewer proactive 교정
            # commons override도 base + 공통 규율
            (root / "roles").mkdir()
            (root / "roles" / "engine.md").write_text("# custom engine\n", encoding="utf-8")
            got = session.resolve_charter(root, "engine")
            self.assertIn("# custom engine", got)
            self.assertIn("조율 규율", got)  # override에도 공통 규율 append
            # 미정 역할 → 스텁 (크래시 안 함)
            self.assertIn("mystery", session.resolve_charter(root, "mystery"))

    def test_chief_charter_is_dedicated_advisory(self):
        # chief = 전담 모니터 셀 (빌드 레인 없음) · 개입 사다리 · dispatch 금지 — 헌법 경계가 카드에 명시
        with tempfile.TemporaryDirectory() as td:
            root = self._soma(td)
            got = session.resolve_charter(root, "chief")
            self.assertIn("전담", got)
            self.assertIn("개입 사다리", got)
            self.assertIn("에스컬레이트", got)
            self.assertIn("dispatch", got)          # 금지 목록
            self.assertIn("alarm sound", got)       # PAUSE는 경보 필드로
            # 공통 규율에 경보 존중 + 에스컬레이션 습관 (모든 역할)
            self.assertIn("경보(alarm)를 존중해라", got)
            self.assertIn("--escalate", got)

    def test_peer_direction_field(self):
        # 상향(upward=셀→chief)·하향(downward=chief whole-view) 피어저널 — 기본은 peer, 오타는 거부
        with tempfile.TemporaryDirectory() as td:
            soma = self._soma(td)
            session.start(soma, "engine", "engine", "의도", "# engine\n")
            rec = session.end(soma, peers=[
                {"peer": "chief", "direction": "upward", "strengths": ["넛지가 구체적"]},
                {"peer": "codex · reviewer"},  # direction 생략 → peer
            ])
            self.assertEqual(rec["peers"][0]["direction"], "upward")
            self.assertEqual(rec["peers"][1]["direction"], "peer")
            session.start(soma, "engine", "engine", "다시", "# engine\n")
            with self.assertRaises(session.SessionError):
                session.end(soma, peers=[{"peer": "x", "direction": "sideways"}])


class TestSessionSiteScan(unittest.TestCase):
    def _root(self, td):
        state_dir, _ = st.init_state_dir(Path(td), "engine")
        return state_dir

    def test_open_sessions_scans_owner_and_guests(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root(td)
            session.start(root, "engine", "engine", "owner 세션", "# engine\n")
            guest = st.ensure_soma(root, "codex8")
            session.start(guest, "codex8", "reviewer", "guest 세션", "# reviewer\n")
            opens = session.open_sessions(root)
            self.assertEqual({o["role"] for o in opens}, {"engine", "reviewer"})
            self.assertEqual(session.recent_retros(root), [])  # 닫힌 것 없음
            # guest 닫으면 retro 1, open은 owner만
            session.end(guest, shipped=["x"], peers=[{"peer": "engine", "would_pair_again": "yes"}])
            rr = session.recent_retros(root)
            self.assertEqual(len(rr), 1)
            self.assertEqual(rr[0]["role"], "reviewer")
            self.assertEqual(rr[0]["shipped"], 1)
            self.assertEqual(rr[0]["peers"], 1)
            self.assertEqual([o["role"] for o in session.open_sessions(root)], ["engine"])


class TestSessionCli(unittest.TestCase):
    def _run(self, td, argv):
        cwd = os.getcwd()
        os.chdir(td)
        try:
            return cli.main(argv)
        finally:
            os.chdir(cwd)

    def test_cli_lifecycle(self):
        with tempfile.TemporaryDirectory() as td:
            st.init_state_dir(Path(td), "engine")
            self.assertEqual(self._run(td, ["session", "start", "--role", "engine",
                                            "--intent", "버그 수정", "--quiet-charter"]), 0)
            self.assertEqual(self._run(td, ["session", "note", "테스트 통과"]), 0)
            self.assertEqual(self._run(td, ["session", "status"]), 0)
            peer = json.dumps({"peer": "codex", "strengths": ["a"], "frictions": [],
                               "would_pair_again": True, "role_fit": "적격"})
            self.assertEqual(self._run(td, ["session", "end", "--ship", "fix.py",
                                            "--peer-json", peer, "--lesson", "seam은 리뷰어에게"]), 0)
            # carry-forward가 self.md에 실렸나 (reflect 재사용 경로)
            self_md = (Path(td) / ".organum" / "self.md").read_text(encoding="utf-8")
            self.assertIn("seam은 리뷰어에게", self_md)


class TestJoin(unittest.TestCase):
    def _run(self, td, argv, env=None):
        cwd = os.getcwd()
        os.chdir(td)
        saved = {}
        for k, v in (env or {}).items():
            saved[k] = os.environ.get(k)
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        try:
            return cli.main(argv)
        finally:
            os.chdir(cwd)
            for k, v in saved.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def test_join_onboards_cell(self):
        with tempfile.TemporaryDirectory() as td:
            st.init_state_dir(Path(td), "engine")
            self.assertEqual(self._run(td, ["join", "--role", "engine", "--for", "eng01",
                                            "--intent", "스폰 배선"], env={"ORGANUM_CELL": None}), 0)
            soma = st.soma_dir(Path(td) / ".organum", "eng01")
            s = session.status(soma)
            self.assertIsNotNone(s)  # 세션 열림
            self.assertEqual(s["role"], "engine")

    def test_join_generates_id_when_absent(self):
        with tempfile.TemporaryDirectory() as td:
            st.init_state_dir(Path(td), "engine")
            self.assertEqual(self._run(td, ["join", "--role", "scribe"],
                                       env={"ORGANUM_CELL": None}), 0)
            got = list((Path(td) / ".organum" / "cells").glob("*/sessions/*.json"))
            self.assertTrue(got)  # 자동 id로 게스트 soma에 세션 생성

    def test_env_fallback_drops_for(self):
        with tempfile.TemporaryDirectory() as td:
            st.init_state_dir(Path(td), "engine")
            self._run(td, ["join", "--role", "engine", "--for", "eng01"], env={"ORGANUM_CELL": None})
            # ORGANUM_CELL 설정 후 --for 없이 session note 동작
            self.assertEqual(self._run(td, ["session", "note", "bare"],
                                       env={"ORGANUM_CELL": "eng01"}), 0)
            soma = st.soma_dir(Path(td) / ".organum", "eng01")
            self.assertEqual(session.status(soma)["notes"], 1)

    def test_need_forid_errors_without_id(self):
        with tempfile.TemporaryDirectory() as td:
            st.init_state_dir(Path(td), "engine")
            with self.assertRaises(SystemExit):  # relay inbox는 id 필수 — 없으면 거부
                self._run(td, ["relay", "inbox"], env={"ORGANUM_CELL": None})

    def test_from_env_fallback_attribution(self):
        from organum import agora
        with tempfile.TemporaryDirectory() as td:
            st.init_state_dir(Path(td), "engine")
            # --from 없이 게시 → 익명 'cell' 아니라 ORGANUM_CELL로 귀속 (오늘 익명 'cell' 버그 fix)
            self.assertEqual(self._run(td, ["agora", "post", "hi"], env={"ORGANUM_CELL": "content7"}), 0)
            self.assertTrue(any(p.get("from") == "content7" for p in agora.list_all(Path(td))))
            self.assertFalse(any(p.get("from") == "cell" for p in agora.list_all(Path(td))))
            # 명시 --from은 env보다 우선
            self.assertEqual(self._run(td, ["agora", "post", "h2", "--from", "explicit"],
                                       env={"ORGANUM_CELL": "content7"}), 0)
            self.assertTrue(any(p.get("from") == "explicit" for p in agora.list_all(Path(td))))


if __name__ == "__main__":
    unittest.main()
