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

    def test_loadout_default_explicit_bare_invalid(self):
        # loadout(v0.1.1 §1): 기본=전-preset ["*"] · 명시=슬러그 · bare=[] · 자유이름=거부
        self.assertEqual(session.normalize_loadout(None), ["*"])
        self.assertEqual(session.normalize_loadout("relay, guard"), ["relay", "guard"])
        self.assertEqual(session.normalize_loadout("relay guard"), ["relay", "guard"])
        self.assertEqual(session.normalize_loadout("bare"), [])
        self.assertEqual(session.normalize_loadout([]), [])
        with self.assertRaises(session.SessionError):
            session.normalize_loadout("Relay")        # 대문자 슬러그 위반
        with self.assertRaises(session.SessionError):
            session.normalize_loadout("한글organ")     # 비ASCII
        with tempfile.TemporaryDirectory() as td:
            soma = self._soma(td)
            rec = session.start(soma, "engine", "engine", "의도", "# c\n", loadout="relay,guard")
            self.assertEqual(rec["loadout"], ["relay", "guard"])
            saved = json.loads((soma / "sessions" / f"{rec['sid']}.json").read_text())
            self.assertEqual(saved["loadout"], ["relay", "guard"])

    def test_loadout_default_and_legacy_join(self):
        # 미선언=["*"] 저장 · sessions_for_join이 레거시(loadout 키 없음)를 ["*"]로 보정
        with tempfile.TemporaryDirectory() as td:
            soma = self._soma(td)
            rec = session.start(soma, "engine", "engine", "의도", "# c\n")   # loadout 미지정
            self.assertEqual(rec["loadout"], ["*"])
            session.end(soma)
            # 레거시 세션 모사: loadout 키 제거
            sp = soma / "sessions" / f"{rec['sid']}.json"
            d = json.loads(sp.read_text()); d.pop("loadout")
            sp.write_text(json.dumps(d), encoding="utf-8")
            got = [s for s in session.sessions_for_join(soma) if s["sid"] == rec["sid"]][0]
            self.assertEqual(got["loadout"], ["*"])   # 레거시 = 전-preset 보정

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

    def test_join_registers_roster_presence(self):
        # ① join이 roster에도 presence 등록 — 필드-whole-view가 조인 셀 못 보던 blind spot fix
        from organum import roster
        with tempfile.TemporaryDirectory() as td:
            st.init_state_dir(Path(td), "engine")
            self._run(td, ["join", "--role", "dev", "--for", "w1"], env={"ORGANUM_CELL": None})
            pres = roster.read_presence(Path(td))
            self.assertTrue(any(e.get("focus") == "dev" for e in pres))

    def test_post_accepts_for_as_from_alias(self):
        # ④ send/post가 --for를 --from 별칭으로 수용 — 학습된 --for로 send 시 argparse silent 실패 fix
        from organum import agora
        with tempfile.TemporaryDirectory() as td:
            st.init_state_dir(Path(td), "engine")
            self.assertEqual(self._run(td, ["agora", "post", "hi", "--for", "chief"],
                                       env={"ORGANUM_CELL": None}), 0)
            self.assertTrue(any(p.get("from") == "chief" for p in agora.list_all(Path(td))))

    # ── critic A1 재감사: resume 판정 (역할 분열 blocker) ──
    def test_join_same_role_resume_preserves_cursor(self):
        import time as _t
        from organum import agora
        with tempfile.TemporaryDirectory() as td:
            st.init_state_dir(Path(td), "engine"); cwd = Path(td)
            self._run(td, ["join", "--role", "critic", "--for", "c1"], env={"ORGANUM_CELL": None})
            _t.sleep(1.1); agora.post(cwd, "mid", frm="x"); _t.sleep(1.1)
            # 같은 role 재join → 이어감(성공), 커서 보존 → mid 남음
            self.assertEqual(self._run(td, ["join", "--role", "critic", "--for", "c1"],
                                       env={"ORGANUM_CELL": None}), 0)
            self.assertGreaterEqual(len(agora.read(cwd, "c1", include_read=True)), 1)

    def test_join_different_role_rejected(self):
        from organum import roster, session
        with tempfile.TemporaryDirectory() as td:
            st.init_state_dir(Path(td), "engine"); cwd = Path(td)
            self._run(td, ["join", "--role", "critic", "--for", "c1"], env={"ORGANUM_CELL": None})
            with self.assertRaises(SystemExit):  # 다른 role 재join → 거부
                self._run(td, ["join", "--role", "builder", "--for", "c1"], env={"ORGANUM_CELL": None})
            soma = st.soma_dir(cwd / ".organum", "c1")
            self.assertEqual(session.status(soma)["role"], "critic")          # 세션 role 불변
            foci = [e.get("focus") for e in roster.read_presence(cwd)]
            self.assertIn("critic", foci); self.assertNotIn("builder", foci)  # roster 불변

    def test_join_new_session_after_end_resets_cursor(self):
        import time as _t
        from organum import agora, session
        with tempfile.TemporaryDirectory() as td:
            st.init_state_dir(Path(td), "engine"); cwd = Path(td)
            self._run(td, ["join", "--role", "critic", "--for", "c1"], env={"ORGANUM_CELL": None})
            _t.sleep(1.1); agora.post(cwd, "old", frm="x")
            session.end(st.soma_dir(cwd / ".organum", "c1")); _t.sleep(1.1)
            # 옛 세션 end 후 같은 id 새 세션 → 커서 reset → old 안 보임
            self.assertEqual(self._run(td, ["join", "--role", "critic", "--for", "c1"],
                                       env={"ORGANUM_CELL": None}), 0)
            self.assertEqual(len(agora.read(cwd, "c1", include_read=True)), 0)

    def test_join_whitespace_intent_errors_not_resumed(self):
        with tempfile.TemporaryDirectory() as td:
            st.init_state_dir(Path(td), "engine")
            with self.assertRaises(SystemExit):  # 빈 intent → non-zero, "이어감" 오인 금지
                self._run(td, ["join", "--role", "critic", "--for", "c1", "--intent", "   "],
                          env={"ORGANUM_CELL": None})

    # ── --for canonical 검증 + --from(display)/--for(identity) 분리 ──
    def test_for_canonical_validated_and_from_display_separated(self):
        from organum import agora
        with tempfile.TemporaryDirectory() as td:
            st.init_state_dir(Path(td), "engine")
            with self.assertRaises(SystemExit):  # 비-canonical --for
                self._run(td, ["agora", "post", "hi", "--for", "../bad"], env={"ORGANUM_CELL": None})
            # --from + --for 동시 = 유효: display=from, identity=from_id 분리(id≠display, 0.2.0 Q6)
            self.assertEqual(
                self._run(td, ["agora", "post", "hi", "--from", "critic (solar)", "--for", "b"],
                          env={"ORGANUM_CELL": None}), 0)
            posts = agora.list_all(Path(td), limit=5)
            self.assertEqual(len(posts), 1)
            self.assertEqual(posts[0]["from"], "critic (solar)")  # 사람이 읽는 display
            self.assertEqual(posts[0]["from_id"], "b")             # canonical identity(self-exclusion용)

    def test_soma_case_insensitive_owner_and_guest(self):
        # 재감사4 Blocker1: Agent≡agent가 owner root와 guest soma로 갈리지 않음(case로 세션 2개 차단)
        with tempfile.TemporaryDirectory() as td:
            state, _ = st.init_state_dir(Path(td), "Agent")     # owner = Agent
            self.assertEqual(st.soma_dir(state, "agent"), state)   # --for agent → root soma(=owner)
            self.assertEqual(st.soma_dir(state, "AGENT"), state)
            self.assertEqual(st.soma_dir(state, "Worker"), st.soma_dir(state, "worker"))  # guest 한 soma

    def test_join_case_variant_rejected_as_same_cell(self):
        # 재감사4: Worker/worker가 같은 셀 → 다른 role 동시 세션 거부(case로 role-state split 재발 차단)
        with tempfile.TemporaryDirectory() as td:
            st.init_state_dir(Path(td), "owner")
            self._run(td, ["join", "--role", "critic", "--for", "Worker"], env={"ORGANUM_CELL": None})
            with self.assertRaises(SystemExit):
                self._run(td, ["join", "--role", "builder", "--for", "worker"], env={"ORGANUM_CELL": None})

    def _legacy_worker_session(self, td):
        from organum import session
        state, _ = st.init_state_dir(Path(td), "owner")
        legacy = state / "cells" / "Worker"                          # 옛 case-preserving soma
        (legacy / "memory").mkdir(parents=True)
        session.start(legacy, "Worker", "critic", "v1", "# c\n")     # 옛 soma의 열린 세션
        return state

    def test_join_legacy_soma_reject_before_mutation(self):
        # 재감사6 A-blocker1: legacy soma 발견 시 거부가 **두 번째 세션을 만들지 않음**(빈 soma 선행생성 X).
        # (dir 존재 체크는 macOS case-insensitive FS에서 Worker==worker라 confound → 세션 수 불변으로)
        from organum import session
        with tempfile.TemporaryDirectory() as td:
            state = self._legacy_worker_session(td)
            with self.assertRaises(SystemExit):
                self._run(td, ["join", "--role", "builder", "--for", "worker"], env={"ORGANUM_CELL": None})
            # 같은 role도 legacy soma면 fail-closed(migration-required) — 거짓 '이어감' 아님
            with self.assertRaises(SystemExit):
                self._run(td, ["join", "--role", "critic", "--for", "worker"], env={"ORGANUM_CELL": None})
            opens = [s for s in session.open_sessions(state) if st.cell_key(s["cell"]) == "worker"]
            self.assertEqual(len(opens), 1)          # 여전히 legacy 세션 1개(중복 생성 없음)
            self.assertEqual(opens[0]["role"], "critic")

    def test_session_start_site_wide_gate(self):
        # 재감사6 A-blocker2: public `session start`도 site-wide 검사로 legacy/중복 canonical 세션 거부
        with tempfile.TemporaryDirectory() as td:
            self._legacy_worker_session(td)
            with self.assertRaises(SystemExit):
                self._run(td, ["session", "start", "--role", "builder", "--intent", "x", "--for", "worker"],
                          env={"ORGANUM_CELL": None})

    def test_from_env_fallback_validated(self):
        # A3 잔여(재감사): ORGANUM_CELL fallback도 canonical 검증 — 비-canonical이면 non-zero
        with tempfile.TemporaryDirectory() as td:
            st.init_state_dir(Path(td), "engine")
            with self.assertRaises(SystemExit):
                self._run(td, ["agora", "post", "hi"], env={"ORGANUM_CELL": "../bad"})

    def test_relay_send_cli_full_id_isolation(self):
        # relay send CLI integration(재감사: unit만이었음) + --for alias + full-id 배송(prefix 이웃 격리)
        from organum import relay
        with tempfile.TemporaryDirectory() as td:
            st.init_state_dir(Path(td), "engine"); cwd = Path(td)
            self.assertEqual(self._run(td, ["relay", "send", "hi east", "--for", "sender-x",
                                            "--to", "playtester-east"], env={"ORGANUM_CELL": None}), 0)
            self.assertEqual(len(relay.inbox(cwd, "playtester-east")), 1)
            self.assertEqual(len(relay.inbox(cwd, "playtester-west")), 0)

    # ── critic A2: roster 실패 무음 금지 ──
    def test_join_roster_failure_warns(self):
        import io
        import contextlib
        from unittest import mock
        from organum import roster as roster_mod
        with tempfile.TemporaryDirectory() as td:
            st.init_state_dir(Path(td), "engine")
            with mock.patch.object(roster_mod, "write_presence", side_effect=OSError("disk full")):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    rc = self._run(td, ["join", "--role", "critic", "--for", "c1"],
                                   env={"ORGANUM_CELL": None})
                self.assertEqual(rc, 0)                          # join 계속(degraded)
                self.assertIn("roster", err.getvalue().lower())  # 경고 관측(무음 아님)

    # ── 0.2.0 Q5: 멱등 게시(재전송 dedup) + payload conflict(fail-closed) ──
    def test_idem_key_dedups_and_conflicts(self):
        from organum import agora
        with tempfile.TemporaryDirectory() as td:
            st.init_state_dir(Path(td), "engine")
            # 같은 키·같은 본문 두 번 → 봉투 하나(진짜 멱등)
            self.assertEqual(self._run(td, ["agora", "post", "goal v1", "--for", "c1",
                                            "--idem-key", "k1"], env={"ORGANUM_CELL": None}), 0)
            self.assertEqual(self._run(td, ["agora", "post", "goal v1", "--for", "c1",
                                            "--idem-key", "k1"], env={"ORGANUM_CELL": None}), 0)
            posts = agora.list_all(Path(td), limit=10)
            self.assertEqual(len(posts), 1)
            self.assertEqual(posts[0]["idem"], "k1")
            # 같은 키·다른 본문 → conflict(fail-closed, exactly-once 위반 — 조용한 옛 receipt 반환 금지)
            with self.assertRaises(SystemExit):
                self._run(td, ["agora", "post", "goal v2 다름", "--for", "c1",
                               "--idem-key", "k1"], env={"ORGANUM_CELL": None})
            self.assertEqual(len(agora.list_all(Path(td), limit=10)), 1)   # 변화 없음
            # 다른 발신자의 같은 키 → 별개(교차 dedup 금지)
            self.assertEqual(self._run(td, ["agora", "post", "other", "--for", "c2",
                                            "--idem-key", "k1"], env={"ORGANUM_CELL": None}), 0)
            self.assertEqual(len(agora.list_all(Path(td), limit=10)), 2)

    # ── 0.2.0 Q1: --json 구조화 출력 (post/read/join) + 비소비 읽기 ──
    def test_json_output_and_nonconsuming_read(self):
        import io
        import contextlib
        with tempfile.TemporaryDirectory() as td:
            st.init_state_dir(Path(td), "engine")

            def _run_json(argv):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self._run(td, argv, env={"ORGANUM_CELL": None})
                return json.loads(out.getvalue())

            # agora post --json → {file, from_id}
            rec = _run_json(["agora", "post", "hi", "--for", "c1", "--json"])
            self.assertTrue(rec["file"].endswith(".md"))
            self.assertEqual(rec["from_id"], "c1")
            # topic:goal 게시 → join --json.goal이 canonical goal 전체 envelope로(backlog 5)
            self._run(td, ["agora", "post", "목표", "--for", "c1", "--topic", "goal"],
                      env={"ORGANUM_CELL": None})
            j = _run_json(["join", "--role", "critic", "--for", "c2", "--json"])
            self.assertEqual(j["cell"], "c2")
            self.assertEqual(j["role"], "critic")
            self.assertIn("charter", j)
            self.assertEqual(len(j["goal"]), 1)
            self.assertEqual(j["goal"][0]["topic"], "goal")
            self.assertEqual(j["goal"][0]["body"], "목표")
            # 가입 후 새 글 → c2의 read에 뜬다(c1 발신, 자기제외 아님)
            self._run(td, ["agora", "post", "after", "--for", "c1"], env={"ORGANUM_CELL": None})
            arr = _run_json(["agora", "read", "--for", "c2", "--json"])
            self.assertTrue(any(m["body"] == "after" and m["from_id"] == "c1" for m in arr))
            # 비소비: 다시 읽어도 같은 집합(커서 전진 안 함 — Q3)
            arr2 = _run_json(["agora", "read", "--for", "c2", "--json"])
            self.assertEqual(len(arr2), len(arr))


if __name__ == "__main__":
    unittest.main()
