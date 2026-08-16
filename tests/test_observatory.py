"""observatory — 관측 영속화 (멱등 append·라스트-라이트-윈·C2 정직성·guard)."""

import json
import tempfile
import unittest
from pathlib import Path

from organum import adapters, observatory
from organum import state as st


def _c(sid="aaaa1111-full", last_ts="2026-07-15T10:00:00Z", **kw):
    return adapters._cell("claude", sid, last_ts=last_ts, **kw)


def _state(td):
    d = Path(td) / ".organum"
    d.mkdir()
    return d


class TestRecord(unittest.TestCase):
    def setUp(self):
        observatory._recorded.clear()

    def test_writes_month_shard_with_fields(self):
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            n = observatory.record(sd, [_c(model="claude-fable-5", out_tok=100,
                                            origin="subagent", parent="bbbb2222")], "sync")
            self.assertEqual(n, 1)
            shard = sd / "observatory" / "2026-07.jsonl"
            self.assertTrue(shard.is_file())
            rec = json.loads(shard.read_text(encoding="utf-8"))
            self.assertEqual(rec["v"], 1)
            self.assertEqual(rec["out_tok"], 100)
            self.assertIsNone(rec["in_tok"])            # 미측정=None 그대로 (C2)
            self.assertEqual(rec["parent"], "bbbb2222")
            self.assertEqual(rec["capture_reason"], "sync")

    def test_idempotent_same_last_ts(self):
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            self.assertEqual(observatory.record(sd, [_c()], "sync"), 1)
            observatory._recorded.clear()  # 프로세스 캐시 무력화 → 샤드 진실로 멱등 검증
            self.assertEqual(observatory.record(sd, [_c()], "sync"), 0)
            shard = sd / "observatory" / "2026-07.jsonl"
            self.assertEqual(len(shard.read_text(encoding="utf-8").splitlines()), 1)

    def test_advanced_last_ts_appends_and_load_dedupes(self):
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            observatory.record(sd, [_c(out_tok=10)], "web")
            observatory.record(sd, [_c(last_ts="2026-07-15T11:00:00Z", out_tok=99)], "checkup")
            shard = sd / "observatory" / "2026-07.jsonl"
            self.assertEqual(len(shard.read_text(encoding="utf-8").splitlines()), 2)
            recs = observatory.load(sd)
            self.assertEqual(len(recs), 1)               # 라스트-라이트-윈
            self.assertEqual(recs[0]["out_tok"], 99)
            self.assertEqual(recs[0]["capture_reason"], "checkup")

    def test_no_last_ts_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            self.assertEqual(observatory.record(sd, [_c(last_ts=None)], "sync"), 0)
            self.assertFalse((sd / "observatory").exists())

    def test_only_idle_skips_active_cells(self):
        import time
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            fresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            n = observatory.record(sd, [_c(sid="active-1", last_ts=fresh),
                                        _c(sid="settled-1")], "web", only_idle_sec=90.0)
            self.assertEqual(n, 1)                       # settle된 것만
            recs = observatory.load(sd)
            self.assertEqual(recs[0]["session_id"], "settled-1")

    def test_missing_state_dir_noop(self):
        self.assertEqual(observatory.record(Path("/nonexistent-xyz"), [_c()], "web"), 0)

    # ── refresh: 이미 기록된 세션의 attribution 자가교정 (identity fix가 노출한 gap) ──

    def test_refresh_reattributes_changed_same_last_ts(self):
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            self.assertEqual(observatory.record(sd, [_c(id="worker")], "sync"), 1)  # declared None
            joined = {"declared": "worker", "role": "engine", "intent": "i",
                      "sid_declared": "s", "loadout": None, "join_method": "direct",
                      "join_status": "joined", "n_sessions": 1}
            orig = observatory._declared_join
            observatory._declared_join = lambda sd_, cells: {c["id"]: joined for c in cells}
            try:
                observatory._recorded.clear()
                # refresh 아니면 같은 last_ts라 attribution 바뀌어도 무기록
                self.assertEqual(observatory.record(sd, [_c(id="worker")], "sync"), 0)
                observatory._recorded.clear()
                # refresh면 교정 레코드 append (append-only, 로더가 tie 최신 선호)
                self.assertEqual(observatory.record(sd, [_c(id="worker")], "refresh", refresh=True), 1)
            finally:
                observatory._declared_join = orig
            recs = observatory.load(sd)
            self.assertEqual(len(recs), 1)                     # load dedup → 교정본
            self.assertEqual(recs[0]["declared"], "worker")
            self.assertEqual(recs[0]["role"], "engine")
            self.assertEqual(recs[0]["join_status"], "joined")
            self.assertEqual(recs[0]["capture_reason"], "refresh")
            shard = sd / "observatory" / "2026-07.jsonl"
            self.assertEqual(len(shard.read_text(encoding="utf-8").splitlines()), 2)  # 원본+교정 보존

    def test_refresh_idempotent_when_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            joined = {"declared": "worker", "role": "engine", "intent": None,
                      "sid_declared": None, "loadout": None, "join_method": "direct",
                      "join_status": "joined", "n_sessions": 1}
            orig = observatory._declared_join
            observatory._declared_join = lambda sd_, cells: {c["id"]: joined for c in cells}
            try:
                self.assertEqual(observatory.record(sd, [_c(id="worker")], "sync"), 1)
                observatory._recorded.clear()
                # attribution 동일 → refresh여도 no-op (멱등, bloat 없음)
                self.assertEqual(observatory.record(sd, [_c(id="worker")], "refresh", refresh=True), 0)
            finally:
                observatory._declared_join = orig
            shard = sd / "observatory" / "2026-07.jsonl"
            self.assertEqual(len(shard.read_text(encoding="utf-8").splitlines()), 1)

    def test_load_ignores_integrity_log(self):
        # observatory/integrity.jsonl(core-integrity 로그)을 세션 로더가 유령 세션(vendor/
        # session_id=None)으로 읽지 않아야 — 같은 디렉터리 공유라 *.jsonl glob이 섞던 버그.
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            observatory.record(sd, [_c(sid="real-1")], "sync")
            (sd / "observatory" / "integrity.jsonl").write_text(
                json.dumps({"ts": "2026-07-21T03:00:00Z", "path": ".organum/roles",
                            "status": "blessed", "rev": "abc"}) + "\n", encoding="utf-8")
            recs = observatory.load(sd)
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0]["session_id"], "real-1")
            self.assertNotIn((None, None), observatory._shard_index(sd))

    def test_refresh_still_skips_backward_last_ts(self):
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            observatory.record(sd, [_c(last_ts="2026-07-15T11:00:00Z")], "sync")
            observatory._recorded.clear()
            # 더 이른 관측은 refresh여도 무기록 (전진분만 — stale 되감기 방지)
            n = observatory.record(sd, [_c(last_ts="2026-07-15T10:00:00Z")], "refresh", refresh=True)
            self.assertEqual(n, 0)
            shard = sd / "observatory" / "2026-07.jsonl"
            self.assertEqual(len(shard.read_text(encoding="utf-8").splitlines()), 1)


class TestBrainRoleJoin(unittest.TestCase):
    """brain↔role 조인 — fail-closed(critic 재감사). 오조인 금지가 미조인보다 우선.
    조인 키 = 선언 셀당 role 유일성(세션 창은 선언 시각이지 작업 창이 아니라 시간매칭 불가)."""

    def _sess(self, cell, role, sid="s"):
        return {"cell": cell, "role": role, "sid": sid,
                "started_at": "2026-07-14T10:00:00Z", "ended_at": "2026-07-14T10:00:05Z"}

    # ── role 유일성(critic ①③: 브레인이 role을 가로지르면 None) ──
    def test_unique_role_joins(self):
        from organum import observatory as obs
        single = obs._role_of_cell([self._sess("w", "engine")])
        self.assertEqual(single["role"], "engine")
        self.assertEqual(single["sid"], "s")               # 단일 후보 → intent/sid 확정
        # 같은 role 여러 세션도 유일 → role은 조인, 단 intent/sid는 None(임의 대표 금지, critic 2)
        multi = obs._role_of_cell([self._sess("w", "tests", "a"), self._sess("w", "tests", "b")])
        self.assertEqual(multi["role"], "tests")
        self.assertIsNone(multi["sid"])

    def test_multiple_distinct_roles_ambiguous_none(self):
        from organum import observatory as obs
        # 한 셀이 engine·critic 두 role → 가로지름 → role None
        self.assertIsNone(obs._role_of_cell(
            [self._sess("w", "engine", "a"), self._sess("w", "critic", "b")])["role"])

    def test_missing_role_session_blocks_join(self):
        from organum import observatory as obs
        self.assertIsNone(obs._role_of_cell([])["role"])
        self.assertIsNone(obs._role_of_cell([self._sess("w", None)])["role"])
        # role 있는 세션 + role 없는 세션 → 결손 무시 안 함 → None (critic 2)
        self.assertIsNone(obs._role_of_cell(
            [self._sess("w", "engine", "a"), self._sess("w", None, "b")])["role"])

    # ── 마커(critic): exact token, complete scan, unique, 구조화 원인 ──
    def _fd(self, path, cids):
        from organum import web
        return web._find_declared(str(path), cids)

    def test_marker_prefix_no_match(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.jsonl"
            p.write_text("ORGANUM_CELL=w9\n", encoding="utf-8")
            self.assertEqual(self._fd(p, ["w9"]), ("w9", "found"))       # exact
            self.assertEqual(self._fd(p, ["w"]), (None, "marker-unknown"))  # 'w'는 'w9' 못 먹음

    def test_marker_multiple_ambiguous_none(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.jsonl"
            p.write_text("ORGANUM_CELL=alpha ... ORGANUM_CELL=beta\n", encoding="utf-8")
            self.assertEqual(self._fd(p, ["alpha", "beta"]), (None, "marker-ambiguous"))

    def test_no_marker(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.jsonl"
            p.write_text("아무 마커 없음\n", encoding="utf-8")
            self.assertEqual(self._fd(p, ["alpha"]), (None, "no-marker"))

    def test_marker_dir_scan(self):
        with tempfile.TemporaryDirectory() as td:
            gdir = Path(td) / "grok"
            (gdir / "terminal").mkdir(parents=True)
            (gdir / "terminal" / "t.log").write_text("export ORGANUM_CELL=w9\n", encoding="utf-8")
            self.assertEqual(self._fd(gdir, ["w9", "other"]), ("w9", "found"))

    def test_ghost_marker_is_ambiguous(self):
        # 전체 마커 2개면 하나만 cids에 있어도 ambiguous(critic 1: 교집합 1 ≠ unique)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.jsonl"
            p.write_text("ORGANUM_CELL=alpha ORGANUM_CELL=ghost\n", encoding="utf-8")
            self.assertEqual(self._fd(p, ["alpha"]), (None, "marker-ambiguous"))

    def test_cache_invalidates_on_append(self):
        import time as _t
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.jsonl"
            p.write_text("ORGANUM_CELL=alpha\n", encoding="utf-8")
            self.assertEqual(self._fd(p, ["alpha", "beta"]), ("alpha", "found"))
            _t.sleep(0.01)
            with open(p, "a", encoding="utf-8") as f:
                f.write("ORGANUM_CELL=beta\n")
            self.assertEqual(self._fd(p, ["alpha", "beta"]), (None, "marker-ambiguous"))

    def test_same_id_repeated_is_one_identity(self):
        # 같은 id가 여러 번 나와도 distinct identity 1개 → found (occurrence 아님, critic 재감사-4)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.jsonl"
            p.write_text("ORGANUM_CELL=alpha\n" * 7, encoding="utf-8")
            self.assertEqual(self._fd(p, ["alpha"]), ("alpha", "found"))

    def test_nonconformant_marker_forces_ambiguous(self):
        # 계약 위반 마커(한글·>40)를 조용히 버리지 않는다 — 별개 identity로 ambiguity (critic Blocker 1)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.jsonl"
            p.write_text("ORGANUM_CELL=alpha\nORGANUM_CELL=가\n", encoding="utf-8")
            self.assertEqual(self._fd(p, ["alpha"]), (None, "marker-ambiguous"))
            p.write_text("ORGANUM_CELL=alpha\nORGANUM_CELL=" + "z" * 60 + "\n", encoding="utf-8")
            self.assertEqual(self._fd(p, ["alpha"]), (None, "marker-ambiguous"))

    def test_raw_token_not_truncated_to_valid_prefix(self):
        # 41자 invalid 마커를 앞 40자 valid로 잘라 found 하면 안 된다 (critic 재감사-5 blocker)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.jsonl"
            z40 = "z" * 40
            p.write_text(f"ORGANUM_CELL={z40}z\n", encoding="utf-8")   # 41자 = 계약 위반
            self.assertEqual(self._fd(p, [z40]), (None, "marker-unknown"))  # z40으로 절대 안 잘림
            p.write_text("ORGANUM_CELL=alpha가\n", encoding="utf-8")   # alpha가 = 계약 위반
            self.assertEqual(self._fd(p, ["alpha"]), (None, "marker-unknown"))  # alpha로 안 잘림
            p.write_text(f"ORGANUM_CELL={z40}   # ok\n", encoding="utf-8")  # 정확 40자 = 대조군
            self.assertEqual(self._fd(p, [z40]), (z40, "found"))

    def test_marker_case_insensitive(self):
        # 재감사4 Blocker2: case-varied 마커 반복은 same identity(found), 진짜 다른 건 ambiguous
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.jsonl"
            p.write_text("ORGANUM_CELL=Agent\nORGANUM_CELL=agent\n", encoding="utf-8")
            self.assertEqual(self._fd(p, ["agent"]), ("agent", "found"))          # 같은 셀
            p.write_text("ORGANUM_CELL=Agent\nORGANUM_CELL=other\n", encoding="utf-8")
            self.assertEqual(self._fd(p, ["agent", "other"]), (None, "marker-ambiguous"))  # 진짜 애매

    def test_marker_left_boundary(self):
        # 긴 identifier의 suffix를 마커로 승격하면 안 된다 — 시작 경계 계약 (critic 재감사-6)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.jsonl"
            for bad in ("NOT_ORGANUM_CELL=alpha\n", "XORGANUM_CELL=alpha\n", "_ORGANUM_CELL=alpha\n"):
                p.write_text(bad, encoding="utf-8")
                self.assertEqual(self._fd(p, ["alpha"]), (None, "no-marker"))  # 마커 아님
            # 진짜 마커는 시작 경계(줄 시작·공백·JSON 따옴표)에서 계속 found (대조군)
            for good in ("ORGANUM_CELL=alpha\n", "export ORGANUM_CELL=alpha   # ok\n",
                         '{"c":"...ORGANUM_CELL=alpha\\n..."}\n'):
                p.write_text(good, encoding="utf-8")
                self.assertEqual(self._fd(p, ["alpha"]), ("alpha", "found"))

    def test_loadout_flows_to_observation_row(self):
        # 조인된 세션의 loadout이 observation row로 흐른다 (v0.1.1 §1 — Ludex 합의 첫 체크포인트)
        from organum import session, web
        with tempfile.TemporaryDirectory() as td:
            sd, _ = st.init_state_dir(Path(td), "owner")
            soma = st.ensure_soma(sd, "alpha")
            session.start(soma, "alpha", "engine", "v1", "# c\n", loadout="relay, guard")
            session.end(soma)
            tp = Path(td) / "t.jsonl"
            tp.write_text("ORGANUM_CELL=alpha\n", encoding="utf-8")
            web._declared_cache.clear()
            observatory.record(sd, [adapters._cell("codex", "cL", last_ts="2026-07-15T10:00:00Z",
                                                   path=str(tp))], "sync")
            r = observatory.load(sd)[0]
            self.assertEqual(r["join_status"], "joined")
            self.assertEqual(r["loadout"], ["relay", "guard"])

    def test_problem_type_and_joint_observed_flow(self):
        # §5 problem_type이 조인된 row로 흐르고 §6 joint_observed가 필드로 강제된다
        from organum import session, web
        with tempfile.TemporaryDirectory() as td:
            sd, _ = st.init_state_dir(Path(td), "owner")
            soma = st.ensure_soma(sd, "beta")
            session.start(soma, "beta", "critic", "v1", "# c\n",
                          problem_type="seam/integration")
            session.end(soma)
            tp = Path(td) / "t2.jsonl"
            tp.write_text("ORGANUM_CELL=beta\n", encoding="utf-8")
            web._declared_cache.clear()
            observatory.record(sd, [adapters._cell("codex", "cP", last_ts="2026-07-15T10:00:00Z",
                                                   path=str(tp))], "sync")
            r = observatory.load(sd)[0]
            self.assertEqual(r["problem_type"], "seam/integration")
            self.assertTrue(r["joint_observed"])           # worker row = 교란 필드 명시

    def test_loadout_none_when_unjoined(self):
        # 미조인 셀(브리지 없음)은 loadout None — role None과 같은 정직성(오귀속 금지)
        with tempfile.TemporaryDirectory() as td:
            sd, _ = st.init_state_dir(Path(td), "owner")
            observatory.record(sd, [adapters._cell("codex", "cU", last_ts="2026-07-15T10:00:00Z")], "sync")
            r = observatory.load(sd)[0]
            self.assertIsNone(r["role"])
            self.assertIsNone(r["loadout"])
            self.assertIsNone(r["problem_type"])
            self.assertIsNone(r["joint_observed"])   # 미조인 = 주장 자체 불가(None, false 아님)

    def test_marker_left_boundary_record_level(self):
        # NOT_ 접두 identifier만 있는 transcript → record() 최신 row까지 role None (critic 재감사-6)
        from organum import session
        with tempfile.TemporaryDirectory() as td:
            sd, _ = st.init_state_dir(Path(td), "owner")
            soma = st.ensure_soma(sd, "alpha")
            session.start(soma, "alpha", "alpha-role", "v1", "# c\n"); session.end(soma)
            tp = Path(td) / "t.jsonl"
            tp.write_text("NOT_ORGANUM_CELL=alpha\n", encoding="utf-8")
            from organum import web
            web._declared_cache.clear()
            observatory.record(sd, [adapters._cell("codex", "cY", last_ts="2026-07-15T10:00:00Z",
                                                   path=str(tp))], "sync")
            r = observatory.load(sd)[0]
            self.assertIsNone(r["role"])
            self.assertNotEqual(r["join_status"], "joined")

    def test_raw_token_truncation_record_level(self):
        # 41자 invalid 마커 하나 + 그 40자 prefix가 declared cell → record()에서 role None
        from organum import session
        with tempfile.TemporaryDirectory() as td:
            sd, _ = st.init_state_dir(Path(td), "owner")
            z40 = "z" * 40
            soma = st.ensure_soma(sd, z40)
            session.start(soma, z40, "prefix-role", "v1", "# c\n"); session.end(soma)
            tp = Path(td) / "t.jsonl"
            tp.write_text(f"ORGANUM_CELL={z40}z\n", encoding="utf-8")   # 41자
            from organum import web
            web._declared_cache.clear()
            observatory.record(sd, [adapters._cell("codex", "cX", last_ts="2026-07-15T10:00:00Z",
                                                   path=str(tp))], "sync")
            r = observatory.load(sd)[0]
            self.assertIsNone(r["role"])
            self.assertEqual(r["join_status"], "marker-unknown")

    def test_chunk_split_invariant_all_positions(self):
        # 마커를 1바이트 chunk로 읽어도(모든 split 위치) whole-input parse와 같은 결과(critic)
        from organum import web
        orig = web._CHUNK
        web._CHUNK = 1
        try:
            with tempfile.TemporaryDirectory() as td:
                p = Path(td) / "t.jsonl"
                # 패딩과 마커 사이 공백 = 유효 시작 경계(패딩은 split 위치 커버용, 인접 아님)
                p.write_text("x" * 40 + " ORGANUM_CELL=beta\n" + "y" * 40, encoding="utf-8")
                self.assertEqual(self._fd(p, ["beta"]), ("beta", "found"))
                p.write_text("ORGANUM_CELL=alpha\nORGANUM_CELL=beta\n", encoding="utf-8")
                self.assertEqual(self._fd(p, ["alpha", "beta"]), (None, "marker-ambiguous"))
        finally:
            web._CHUNK = orig

    def test_incomplete_scan_is_scan_incomplete(self):
        from organum import web
        orig = web._SCAN_CAP
        web._SCAN_CAP = 500
        try:
            with tempfile.TemporaryDirectory() as td:
                p = Path(td) / "big.jsonl"
                p.write_text("ORGANUM_CELL=alpha\n" + ("x" * 2000), encoding="utf-8")
                self.assertEqual(self._fd(p, ["alpha"]), (None, "scan-incomplete"))
        finally:
            web._SCAN_CAP = orig

    # ── canonical cell ID 계약 (critic Blocker 1: 자유 id 차단) ──
    def test_cell_id_contract_validator(self):
        self.assertTrue(st.valid_cell_id("worker7"))
        self.assertTrue(st.valid_cell_id("a.b-c_9"))
        self.assertTrue(st.valid_cell_id("a" * 40))
        self.assertFalse(st.valid_cell_id("가"))              # 비ASCII
        self.assertFalse(st.valid_cell_id("a" * 41))          # >40
        self.assertFalse(st.valid_cell_id(".hidden"))         # 선행 점(traversal)
        self.assertFalse(st.valid_cell_id("trail."))          # 후행 점
        self.assertFalse(st.valid_cell_id(""))

    def test_session_start_rejects_noncanonical_id(self):
        from organum import session
        with tempfile.TemporaryDirectory() as td:
            soma = st.ensure_soma(st.init_state_dir(Path(td), "o")[0], "x")
            with self.assertRaises(session.SessionError):
                session.start(soma, "가", "engine", "v1", "# c\n")     # 한글 거부
            with self.assertRaises(session.SessionError):
                session.start(soma, "z" * 50, "engine", "v1", "# c\n")  # >40 거부

    def test_init_rejects_noncanonical_agent(self):
        # --agent는 주소 가능한 cell id(owner alias) — init ingress에서 계약 거부(critic).
        import os
        from argparse import Namespace
        from organum import cli
        with tempfile.TemporaryDirectory() as td:
            cwd = os.getcwd()
            os.chdir(td)
            try:
                with self.assertRaises(SystemExit):
                    cli.cmd_init(Namespace(agent="가"))          # 한글 거부
                with self.assertRaises(SystemExit):
                    cli.cmd_init(Namespace(agent="z" * 41))      # >40 거부
                self.assertEqual(cli.cmd_init(Namespace(agent="cody")), 0)  # canonical 통과
            finally:
                os.chdir(cwd)

    # ── record() 레벨 fail-closed (critic: 최신 row까지) ──
    def test_record_level_scan_during_append(self):
        # scan 도중 append(pre/post 서명 불일치) → 조인 안 함(role None), 오귀속 방지
        from organum import web
        with tempfile.TemporaryDirectory() as td:
            sd, _ = st.init_state_dir(Path(td), "owner")
            soma = st.ensure_soma(sd, "alpha")
            from organum import session
            session.start(soma, "alpha", "content", "v1", "# c\n"); session.end(soma)
            tp = Path(td) / "t.jsonl"
            tp.write_text("ORGANUM_CELL=alpha\n", encoding="utf-8")
            orig = web._scan_markers
            def racing(path):  # scan 직후 파일이 커진 것처럼(서명 변경)
                r = orig(path)
                with open(tp, "a", encoding="utf-8") as f:
                    f.write("ORGANUM_CELL=beta\n")
                return r
            web._scan_markers = racing
            web._declared_cache.clear()
            try:
                observatory.record(sd, [adapters._cell("codex", "cX", last_ts="2026-07-15T10:00:00Z",
                                                       path=str(tp))], "sync")
            finally:
                web._scan_markers = orig
            r = observatory.load(sd)[0]
            self.assertIsNone(r["role"])
            self.assertEqual(r["join_status"], "scan-incomplete")

    def test_record_level_unreadable_dir(self):
        from organum import web
        with tempfile.TemporaryDirectory() as td:
            sd, _ = st.init_state_dir(Path(td), "owner")
            gdir = Path(td) / "grok"
            gdir.mkdir()
            f = gdir / "t.log"
            f.write_text("ORGANUM_CELL=alpha\n", encoding="utf-8")
            import os as _os
            _os.chmod(f, 0)  # 읽기 실패 → complete=False
            try:
                web._declared_cache.clear()
                observatory.record(sd, [adapters._cell("grok", "gX", last_ts="2026-07-15T10:00:00Z",
                                                       path=str(gdir))], "sync")
                r = observatory.load(sd)[0]
                self.assertIsNone(r["role"])
                self.assertEqual(r["join_status"], "scan-incomplete")
            finally:
                _os.chmod(f, 0o644)

    # ── opencode 전역 DB 스캔 끔 + 통합 경로 ──
    def test_opencode_global_store_join_off(self):
        with tempfile.TemporaryDirectory() as td:
            sd, _ = st.init_state_dir(Path(td), "owner")
            db = Path(td) / "opencode.db"
            db.write_text("part row: ORGANUM_CELL=alpha ...", encoding="utf-8")
            soma = st.ensure_soma(sd, "alpha")
            from organum import session
            session.start(soma, "alpha", "content", "v1", "# c\n"); session.end(soma)
            observatory.record(sd, [adapters._cell("opencode", "ocX", last_ts="2026-07-15T10:00:00Z",
                                                   path=str(db))], "sync")
            self.assertIsNone(observatory.load(sd)[0]["role"])  # 전역 DB 마커로 조인 안 함

    def test_direct_id_full_flow_with_join_method(self):
        from organum import session
        with tempfile.TemporaryDirectory() as td:
            sd, _ = st.init_state_dir(Path(td), "owner")
            soma = st.ensure_soma(sd, "worker7")
            session.start(soma, "worker7", "engine", "v1", "# c\n")
            session.end(soma, shipped=["x"])
            observatory.record(sd, [adapters._cell("codex", "w7full", last_ts="2026-07-15T10:00:00Z",
                                                   path="/x.jsonl", **{"id": "worker7"})], "sync")
            rec = observatory.load(sd)[0]
            self.assertEqual(rec["role"], "engine")
            self.assertEqual(rec["join_method"], "direct")
            self.assertEqual(rec["join_status"], "joined")

    def test_unjoined_stays_none_honest(self):
        with tempfile.TemporaryDirectory() as td:
            sd, _ = st.init_state_dir(Path(td), "owner")
            observatory.record(sd, [_c(sid="lone", last_ts="2026-07-15T10:00:00Z")], "sync")
            r = observatory.load(sd)[0]
            self.assertIsNone(r["role"])
            self.assertIsNone(r["join_method"])
            self.assertEqual(r["join_status"], "no-bridge")   # 실패 provenance (critic 3)
            self.assertEqual(r["role_basis"], "cell-role-unique")

    def test_join_status_role_ambiguous(self):
        # identity는 확인(직접 id)되나 셀에 두 role → join_status=role-ambiguous
        from organum import session
        with tempfile.TemporaryDirectory() as td:
            sd, _ = st.init_state_dir(Path(td), "owner")
            soma = st.ensure_soma(sd, "w2")
            session.start(soma, "w2", "engine", "v1", "# c\n"); session.end(soma)
            session.start(soma, "w2", "critic", "v2", "# c\n"); session.end(soma)
            observatory.record(sd, [_c(sid="w2full", last_ts="2026-07-15T10:00:00Z",
                                       **{"id": "w2"})], "sync")
            r = observatory.load(sd)[0]
            self.assertIsNone(r["role"])
            self.assertEqual(r["join_status"], "role-ambiguous")
            self.assertEqual(r["declared"], "w2")          # identity 보존(critic 3)
            self.assertEqual(r["join_method"], "direct")   # 어느 브릿지였는지 보존
            self.assertEqual(r["declared_sessions"], 2)


class TestStats(unittest.TestCase):
    def _recs(self):
        return [
            {"vendor": "claude", "origin": "terminal", "model": "m1",
             "in_tok": 100, "out_tok": 50, "cache": 1000, "role": "dev"},
            {"vendor": "claude", "origin": "subagent", "model": "m2",
             "in_tok": 10, "out_tok": 5, "cache": None, "role": None},
            {"vendor": "agy", "origin": "terminal", "model": "m1",
             "in_tok": None, "out_tok": None, "cache": None, "role": "dev"},
        ]

    def test_measured_only_sums_and_unmeasured_counts(self):
        s = observatory.stats(self._recs())
        self.assertEqual(s["sessions"], 3)
        self.assertEqual((s["terminal"], s["subagent"]), (2, 1))
        self.assertEqual(s["in_tok"], 110)               # None 제외 합산
        self.assertEqual(s["in_tok_unmeasured"], 1)
        self.assertEqual(s["cache_unmeasured"], 2)
        self.assertIsNone(s["cost_usd"])                 # 단가표 밖 모델뿐 → None

    def test_group_by_model(self):
        s = observatory.stats(self._recs(), by="model")
        self.assertEqual(set(s["by"]), {"m1", "m2"})
        self.assertEqual(s["by"]["m1"]["sessions"], 2)
        self.assertEqual(s["by"]["m1"]["in_tok"], 100)   # agy 미측정은 합산 제외

    def test_render_smoke(self):
        out = observatory.render_stats(observatory.stats(self._recs(), by="role"), 30, by="role")
        self.assertIn("3 세션", out)
        self.assertIn("—", out)                          # 미측정 표기


class TestReport(unittest.TestCase):
    """리포트 = 지금(live 직독)/오늘/역사(축적) 분리된 밴드 — 합산하지 않는다."""

    def setUp(self):
        observatory._recorded.clear()

    def test_bands_separated(self):
        import time
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            fresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            # 과거 세션은 상대 시각으로 — 달력 고정 날짜는 30일 창이 굴러가면
            # 시한폭탄이 된다(2026-08-16 실측: 7/10 고정값이 창 밖으로 빠져 실패).
            old_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                   time.gmtime(time.time() - 10 * 86400))
            observatory.record(sd, [_c(sid="old-mara", last_ts=old_ts,
                                       out_tok=9_000_000, model="m-big")], "sync")
            observatory.record(sd, [_c(sid="today-x", last_ts=fresh, out_tok=50)], "sync")
            orig = adapters.snapshot
            adapters.snapshot = lambda cwd, window_min=30.0: [
                _c(sid="live-now", last_ts=fresh, out_tok=7, tools={"Bash": 3})]
            try:
                out = observatory.report(sd, Path(td), days=30)
            finally:
                adapters.snapshot = orig
            self.assertIn("■ 지금 — 살아있는 세션 1", out)
            self.assertIn("live-now", out)
            self.assertIn("■ 오늘", out)
            self.assertIn("■ 역사 — 2 세션", out)        # live는 역사에 합산 안 됨
            self.assertIn("old-mara", out)               # 대형 세션 top
            self.assertIn("일별 out:", out)

    def test_no_history_hint(self):
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            orig = adapters.snapshot
            adapters.snapshot = lambda cwd, window_min=30.0: []
            try:
                out = observatory.report(sd, Path(td))
            finally:
                adapters.snapshot = orig
            self.assertIn("스냅샷 없음", out)
            self.assertIn("observatory sync", out)       # 시작 힌트


if __name__ == "__main__":
    unittest.main()


def _obs_v1(**over):
    """observation/v1 최소 유효 레코드 (organum-code golden 형상)."""
    rec = {
        "schema": "organum-code/observation/v1",
        "run": {"id": "ocobs-" + "a" * 64, "attempt": 1, "status": "passed",
                "startedAt": None, "finishedAt": None,
                "recordedAt": "2026-07-24T15:47:23.000Z",
                "timingCompleteness": "partial", "comparisonKey": None,
                "preregistrationId": None},
        "identity": {"canonicalCell": "grok-0006267341951b9cecf0c78301310935d1d",
                     "joinStatus": "joined", "role": "critic",
                     "persona": None, "workspace": None},
        "backend": {"id": "grok-build", "version": "0.2.111", "protocol": "acp",
                    "nativeSessionId": None},
        "brain": {"provider": "upstage", "model": "solar-open2",
                  "protocol": "openai-chat-completions",
                  "reasoning": {"enabled": False, "effort": None}},
        "usage": {"semantics": "organum-code/provider-usage/v1", "source": "inference-broker",
                  "completeness": "lower-bound", "requests": None, "responses": 14,
                  "inputTokens": 259215, "outputTokens": 2032, "cachedInputTokens": 218688,
                  "totalTokens": 261247, "reasoningTokens": 0, "costUsd": None},
        "coordination": {"contributions": 1, "topic": "critic-review",
                         "publicationPhase": "shipped", "sessionClosed": True,
                         "receipt": {"file": "20260724-x-to-field.md", "bodyBytes": 1,
                                      "bodySha256": "0" * 64}},
        "discipline": {"commands": [], "declaredExecutions": 1,
                       "additionalReadOnlyCommands": 0, "strictSingleExecute": True,
                       "executionBudgetPhase": "conservation", "checkpointActuations": 0,
                       "conservationActuations": 0, "worktreeClean": True},
        "outcome": {"gate": "pass", "classification": "x", "causalClaim": "observational",
                    "checks": {"ok": True}},
        "provenance": {"observationSource": "reported",
                       "producer": {"name": "organum-code", "version": "0", "commit": None},
                       "source": {"schema": "s/v1", "digest": "0" * 64,
                                   "repositoryCommit": None, "priorFailure": None}},
        "evaluation": {"name": "e", "scenario": None},
    }
    for k, v in over.items():
        parts = k.split("__")
        cur = rec
        for p in parts[:-1]:
            cur = cur[p]
        cur[parts[-1]] = v
    return rec


class TestReportedIngestion(unittest.TestCase):
    """organum-code observation/v1 ingest — 계약 판정 6건 + invariant fail-closed."""

    def test_ingest_and_load_flat_row(self):
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            self.assertEqual(observatory.ingest_report(sd, _obs_v1()), "ingested")
            rows = observatory.load_reported(sd)
            self.assertEqual(len(rows), 1)
            r = rows[0]
            self.assertEqual(r["backend"], "grok-build")     # 판정 2: backend 별도 축
            self.assertEqual(r["model"], "solar-open2")
            self.assertEqual(r["provider"], "upstage")       # provider ≠ backend
            self.assertEqual(r["declared"], r["id"])         # joined → declared
            self.assertIsNone(r["requests"])                 # 판정 4: null 보존
            self.assertEqual(r["usage_completeness"], "lower-bound")

    def test_unknown_valid_backend_accepted(self):
        # 판정 1: 미지 백엔드도 문법 유효하면 수용 (closed enum 아님)
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            rec = _obs_v1(backend__id="future-tui-2027")
            self.assertEqual(observatory.ingest_report(sd, rec), "ingested")

    def test_invalid_backend_grammar_rejected(self):
        for bad in ("", "UPPER", "has space", "a" * 65, None):
            rec = _obs_v1(backend__id=bad)
            self.assertTrue(observatory.validate_observation(rec), f"통과되면 안 됨: {bad!r}")

    def test_failed_and_notjoined_run_ingested(self):
        # 판정 3: 실패 run·nullable identity도 정상 observation
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            rec = _obs_v1(run__id="ocobs-" + "b" * 64, run__status="failed",
                          identity__canonicalCell=None, identity__joinStatus="not-joined",
                          identity__role=None)
            rec["coordination"]["contributions"] = 0
            rec["coordination"]["receipt"] = None      # contributions=0 → receipt null (A2.1)
            rec["outcome"]["gate"] = "fail"
            self.assertEqual(observatory.ingest_report(sd, rec), "ingested")
            r = observatory.load_reported(sd)[0]
            self.assertIsNone(r["id"])
            self.assertIsNone(r["declared"])
            self.assertEqual(r["run_status"], "failed")

    def test_idempotent_replay_and_conflict(self):
        # 판정 5: 같은 payload 재전송=duplicate, 다른 payload=conflict fail-closed
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            self.assertEqual(observatory.ingest_report(sd, _obs_v1()), "ingested")
            self.assertEqual(observatory.ingest_report(sd, _obs_v1()), "duplicate")
            mutated = _obs_v1(usage__outputTokens=9999, usage__totalTokens=259215 + 9999)
            with self.assertRaises(observatory.IngestConflict):
                observatory.ingest_report(sd, mutated)
            self.assertEqual(len(observatory.load_reported(sd)), 1)

    def test_passive_and_reported_separate(self):
        # 판정 6: passive 로더가 reported 샤드 안 읽고, reported 로더가 passive 안 읽음
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            observatory.record(sd, [_c(sid="passive-1")], "sync")
            observatory.ingest_report(sd, _obs_v1())
            self.assertEqual(len(observatory.load(sd)), 1)
            self.assertEqual(observatory.load(sd)[0]["session_id"], "passive-1")
            self.assertEqual(len(observatory.load_reported(sd)), 1)
            self.assertEqual(observatory.load_reported(sd)[0]["source"], "reported")

    def test_invariants_fail_closed(self):
        cases = [
            _obs_v1(schema="organum-code/observation/v2"),               # 미지 스키마
            _obs_v1(identity__canonicalCell=None),                        # joined인데 cell 없음
            _obs_v1(identity__persona="p"),                               # persona만(workspace 없이)
            _obs_v1(run__status="failed"),                                # gate=pass인데 status!=passed
            _obs_v1(usage__totalTokens=1),                                # total != in+out
            _obs_v1(usage__cachedInputTokens=999999999),                  # cached > input
            _obs_v1(run__timingCompleteness="complete"),                  # complete인데 ts 없음
        ]
        for rec in cases:
            self.assertTrue(observatory.validate_observation(rec))
        # receipt 경로 탈출
        bad = _obs_v1()
        bad["coordination"]["receipt"]["file"] = "../escape.md"
        self.assertTrue(observatory.validate_observation(bad))

    def test_stats_by_backend(self):
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            observatory.ingest_report(sd, _obs_v1())
            rec2 = _obs_v1(run__id="ocobs-" + "c" * 64, backend__id="claude-code")
            observatory.ingest_report(sd, rec2)
            s = observatory.stats(observatory.load_reported(sd), by="backend")
            self.assertEqual(set(s["by"]), {"grok-build", "claude-code"})


def _mp_ingest(sd, rec_json, barrier, q):
    """2-process race 헬퍼 (spawn-picklable 모듈 레벨) — critic A3 회귀."""
    from pathlib import Path as _P

    from organum import observatory as _obs
    rec = json.loads(rec_json)
    barrier.wait(timeout=10)
    try:
        q.put(_obs.ingest_report(_P(sd), rec))
    except _obs.IngestConflict:
        q.put("conflict")
    except ValueError:
        q.put("invalid")


class TestIngestCriticA1A3(unittest.TestCase):
    """공동 schema critic 1차 blocker 회귀 — A1 timestamp→path · A2 strict 문법 · A3 동시성."""

    # ── A1: raw timestamp가 shard 경로에 못 들어감 ──

    def test_path_escape_timestamp_rejected_no_write(self):
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            rec = _obs_v1(run__recordedAt="/../../")
            with self.assertRaises(ValueError):
                observatory.ingest_report(sd, rec)
            self.assertEqual(list((sd / "observatory").glob("*")) if (sd / "observatory").exists() else [], [])
            self.assertEqual(list(Path(td).glob("*.jsonl")), [])   # 경계 이탈 파일 없음

    def test_type_confusion_timestamp_rejected_not_typeerror(self):
        rec = _obs_v1(run__recordedAt=1)
        errs = observatory.validate_observation(rec)   # TypeError가 아니라 controlled reject
        self.assertTrue(errs)

    def test_offset_timestamp_rejected(self):
        rec = _obs_v1(run__recordedAt="2026-07-25T00:47:23+09:00")
        self.assertTrue(observatory.validate_observation(rec))

    def test_ordering_violation_rejected(self):
        rec = _obs_v1(run__startedAt="2026-07-24T16:00:00Z",
                      run__finishedAt="2026-07-24T15:00:00Z",
                      run__recordedAt="2026-07-24T17:00:00Z")
        errs = observatory.validate_observation(rec)
        self.assertTrue(any("ordering" in e for e in errs))

    def test_shard_name_from_parsed_datetime(self):
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            observatory.ingest_report(sd, _obs_v1())
            self.assertTrue((sd / "observatory" / "reported-2026-07.jsonl").is_file())

    # ── A2: portable schema와 동일 언어 (strict) ──

    def test_backend_grammar_exact_producer_regex(self):
        for bad in ("under_score", "dot.ted", "trailing-", "a" * 65, "UPPER"):
            self.assertTrue(observatory.validate_observation(_obs_v1(backend__id=bad)),
                            f"producer-invalid인데 통과: {bad!r}")
        for ok in ("grok-build", "claude-code", "deepcode", "opencode", "future-tui-2027", "a" * 64):
            rec = _obs_v1(backend__id=ok)
            self.assertFalse(observatory.validate_observation(rec), f"producer-valid인데 거부: {ok!r}")

    def test_strict_shape_counterexamples(self):
        base = _obs_v1()
        extra_root = dict(base); extra_root["surprise"] = 1
        extra_nested = _obs_v1(); extra_nested["usage"]["surprise"] = 1
        missing_brain = _obs_v1(); del missing_brain["brain"]
        cases = {
            "extra root key": extra_root,
            "extra nested key": extra_nested,
            "missing brain": missing_brain,
            "negative token": _obs_v1(usage__inputTokens=-1, usage__totalTokens=2031),
            "fractional count": _obs_v1(usage__responses=14.5),
            "bool as count": _obs_v1(usage__responses=True),
            "enum drift": _obs_v1(run__status="ok"),
        }
        for name, rec in cases.items():
            self.assertTrue(observatory.validate_observation(rec), f"통과되면 안 됨: {name}")

    def test_receipt_dot_dotdot_rejected(self):
        for bad in (".", "..", "a/b.md", "a\\b.md"):
            rec = _obs_v1()
            rec["coordination"]["receipt"]["file"] = bad
            self.assertTrue(observatory.validate_observation(rec), f"통과되면 안 됨: {bad!r}")

    def test_golden_and_failed_fixture_still_accepted(self):
        self.assertFalse(observatory.validate_observation(_obs_v1()))
        failed = _obs_v1(run__status="failed", identity__canonicalCell=None,
                         identity__joinStatus="not-joined", identity__role=None)
        failed["coordination"]["contributions"] = 0
        failed["coordination"]["receipt"] = None       # contributions=0 → receipt null (A2.1)
        failed["outcome"]["gate"] = "fail"
        self.assertFalse(observatory.validate_observation(failed))

    # ── A3: 동시성 conflict fail-closed ──

    def test_two_process_same_payload_one_ingest(self):
        import multiprocessing as mp
        ctx = mp.get_context("spawn")
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            rec_json = json.dumps(_obs_v1())
            barrier = ctx.Barrier(2); q = ctx.Queue()
            ps = [ctx.Process(target=_mp_ingest, args=(str(sd), rec_json, barrier, q))
                  for _ in range(2)]
            for p in ps: p.start()
            for p in ps: p.join(30)
            results = sorted(q.get(timeout=5) for _ in range(2))
            self.assertEqual(results, ["duplicate", "ingested"])
            shard = sd / "observatory" / "reported-2026-07.jsonl"
            self.assertEqual(len(shard.read_text(encoding="utf-8").splitlines()), 1)

    def test_two_process_changed_payload_conflict(self):
        import multiprocessing as mp
        ctx = mp.get_context("spawn")
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            a = json.dumps(_obs_v1())
            b = json.dumps(_obs_v1(usage__outputTokens=9999, usage__totalTokens=259215 + 9999))
            barrier = ctx.Barrier(2); q = ctx.Queue()
            ps = [ctx.Process(target=_mp_ingest, args=(str(sd), r, barrier, q)) for r in (a, b)]
            for p in ps: p.start()
            for p in ps: p.join(30)
            results = sorted(q.get(timeout=5) for _ in range(2))
            self.assertEqual(results, ["conflict", "ingested"])
            shard = sd / "observatory" / "reported-2026-07.jsonl"
            self.assertEqual(len(shard.read_text(encoding="utf-8").splitlines()), 1)

    def test_loader_quarantines_conflicting_shard(self):
        # pre-existing 샤드에 같은 run·다른 fp 두 줄 → LWW 금지, 격리 + 표면화
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            observatory.ingest_report(sd, _obs_v1())
            shard = sd / "observatory" / "reported-2026-07.jsonl"
            row = json.loads(shard.read_text(encoding="utf-8"))
            evil = dict(row); evil["payload_fp"] = "f" * 64
            evil["ingested_at"] = "2099-01-01T00:00:00Z"   # LWW였다면 이게 이겼을 것
            with open(shard, "a", encoding="utf-8") as f:
                f.write(json.dumps(evil, ensure_ascii=False) + "\n")
            self.assertEqual(observatory.load_reported(sd), [])          # 격리 → 0행
            self.assertEqual(observatory.reported_conflicts(sd), [row["run_id"]])


class TestIngestCriticA21(unittest.TestCase):
    """A2.1 잔여 — usage completeness superRefine 등가 + finite number (6 differential 회귀)."""

    def test_unavailable_with_measured_counters_rejected(self):
        rec = _obs_v1(usage__completeness="unavailable")   # counter들 measured 유지
        self.assertTrue(observatory.validate_observation(rec))

    def test_unavailable_all_null_accepted(self):
        rec = _obs_v1(usage__completeness="unavailable", usage__source="unavailable",
                      usage__requests=None, usage__responses=None, usage__inputTokens=None,
                      usage__outputTokens=None, usage__cachedInputTokens=None,
                      usage__totalTokens=None, usage__reasoningTokens=None, usage__costUsd=None)
        self.assertFalse(observatory.validate_observation(rec))

    def test_complete_requires_requests(self):
        rec = _obs_v1(usage__completeness="complete")      # requests=null 유지
        self.assertTrue(observatory.validate_observation(rec))
        ok = _obs_v1(usage__completeness="complete", usage__requests=14)
        self.assertFalse(observatory.validate_observation(ok))

    def test_responses_le_requests(self):
        rec = _obs_v1(usage__requests=1)                   # responses=14 > requests=1
        self.assertTrue(observatory.validate_observation(rec))

    def test_zero_contributions_requires_null_receipt(self):
        rec = _obs_v1()
        rec["coordination"]["contributions"] = 0           # receipt 유지 → 거부
        self.assertTrue(observatory.validate_observation(rec))
        rec["coordination"]["receipt"] = None
        self.assertFalse(observatory.validate_observation(rec))

    def test_native_tool_approval_confound_additive(self):
        # S16 additive: null/생략(구 레코드)=valid · 유효 집계=valid · 보존 위반·금지 필드·
        # latency 비정합=invalid (count-conservation은 superRefine 등가 — A2.1 패턴)
        ok_agg = {"schema": "organum-code/native-tool-approval-confound/v1",
                  "productSurface": "cli-print-hook-projection", "presentations": 1,
                  "allowOnce": 1, "rejectOnce": 0, "cancelled": 0,
                  "latencyMs": {"count": 1, "sum": 42, "max": 42},
                  "decider": {"kind": "human", "presenter": "organum-code-terminal"}}
        self.assertFalse(observatory.validate_observation(
            _obs_v1(discipline__nativeToolApproval=None)))
        self.assertFalse(observatory.validate_observation(
            _obs_v1(discipline__nativeToolApproval=dict(ok_agg))))
        bad_cons = dict(ok_agg, presentations=0)
        self.assertTrue(observatory.validate_observation(
            _obs_v1(discipline__nativeToolApproval=bad_cons)))
        bad_extra = dict(ok_agg)
        bad_extra["command"] = "rm -rf /"                  # 금지 필드 — strict 거부
        self.assertTrue(observatory.validate_observation(
            _obs_v1(discipline__nativeToolApproval=bad_extra)))
        # producer Zod 4규칙 exact 등가 (reconciliation: 느슨한 부등식이 false-accept
        # 3종을 통과시켰다 — latency 측정 소실·provenance 오기/소실)
        self.assertTrue(observatory.validate_observation(   # ② count != presentations
            _obs_v1(discipline__nativeToolApproval=dict(
                ok_agg, latencyMs={"count": 0, "sum": 0, "max": 0}))))
        self.assertTrue(observatory.validate_observation(   # ③ 비활성인데 decider 존재
            _obs_v1(discipline__nativeToolApproval=dict(
                ok_agg, presentations=0, allowOnce=0,
                latencyMs={"count": 0, "sum": 0, "max": 0}))))
        self.assertTrue(observatory.validate_observation(   # ④ 활성인데 decider null
            _obs_v1(discipline__nativeToolApproval=dict(ok_agg, decider=None))))
        inactive_ok = dict(ok_agg, presentations=0, allowOnce=0, decider=None,
                           latencyMs={"count": 0, "sum": 0, "max": 0})
        self.assertFalse(observatory.validate_observation(  # 정상 비활성 = valid
            _obs_v1(discipline__nativeToolApproval=inactive_ok)))

    def test_native_tool_approval_flat_row(self):
        # flat row에 additive 노출 — 승인 confound가 측정으로 흐른다 (B2/Q8)
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            agg = {"schema": "organum-code/native-tool-approval-confound/v1",
                   "productSurface": "cli-print-wrapper-projection", "presentations": 1,
                   "allowOnce": 0, "rejectOnce": 1, "cancelled": 0,
                   "latencyMs": {"count": 1, "sum": 7, "max": 7},
                   "decider": {"kind": "policy",
                               "policyId": "organum-native-noninteractive-deny",
                               "policyVersion": "1.0.0"}}
            observatory.ingest_report(sd, _obs_v1(discipline__nativeToolApproval=agg))
            r = observatory.load_reported(sd)[0]
            self.assertEqual(r["native_tool_approval"]["rejectOnce"], 1)
            self.assertEqual(r["native_tool_approval"]["decider"]["kind"], "policy")

    def test_nonfinite_costusd_rejected(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            rec = _obs_v1(usage__costUsd=bad)
            self.assertTrue(observatory.validate_observation(rec), f"통과되면 안 됨: {bad!r}")

    def test_cli_rejects_nonstandard_json_constants(self):
        # CLI parse 단계에서 NaN/Infinity controlled reject + write 0
        import io
        from contextlib import redirect_stderr

        from organum import cli as cli_mod
        with tempfile.TemporaryDirectory() as td:
            proj = Path(td)
            bad = proj / "bad.json"
            bad.write_text('{"schema": "organum-code/observation/v1", "usage": {"costUsd": NaN}}',
                           encoding="utf-8")
            import os
            from contextlib import redirect_stdout
            cwd = os.getcwd()
            os.chdir(proj)
            try:
                with redirect_stdout(io.StringIO()):
                    cli_mod.main(["init"])                  # 실제 init으로 유효 state 생성
                err = io.StringIO()
                with redirect_stderr(err):
                    rc = cli_mod.main(["observatory", "ingest", str(bad)])
            finally:
                os.chdir(cwd)
            self.assertNotEqual(rc, 0)                      # controlled nonzero
            self.assertIn("nonstandard", err.getvalue())
            self.assertEqual(list((proj / ".organum" / "observatory").glob("reported-*")), [])


class TestCorrelation(unittest.TestCase):
    """H1 correlation 링크 — exact (backend, nativeSessionId) pair만, 읽기 시점·무기록·무병합."""

    NSID = "6a670000-1111-2222-3333-444455556666"

    def _reported(self, sd, backend="claude-code", nsid=NSID, rid_suffix="a"):
        rec = _obs_v1(backend__id=backend, backend__nativeSessionId=nsid,
                      run__id="ocobs-" + rid_suffix * 64)
        self.assertEqual(observatory.ingest_report(sd, rec), "ingested")

    def test_linked_exact_pair(self):
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            observatory.record(sd, [_c(sid=self.NSID, out_tok=2100, model="solar-open2")],
                               reason="test")
            self._reported(sd)
            links = observatory.correlate(sd)
            self.assertEqual(len(links), 1)
            l = links[0]
            self.assertEqual(l["link_status"], "linked")
            self.assertEqual(l["passive"]["out_tok"], 2100)     # 양 소스 나란히
            self.assertEqual(l["reported"]["out_tok"], 2032)    # 병합 없음 — 각자 값 유지
            self.assertEqual(l["reported"]["usage_completeness"], "lower-bound")

    def test_no_passive_when_absent(self):
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            self._reported(sd)
            self.assertEqual(observatory.correlate(sd)[0]["link_status"], "no-passive")

    def test_no_native_id(self):
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            self._reported(sd, nsid=None)
            l = observatory.correlate(sd)[0]
            self.assertEqual(l["link_status"], "no-native-id")
            self.assertIsNone(l["passive"])

    def test_unknown_backend_no_mapping(self):
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            self._reported(sd, backend="future-tui-2027")
            # 미지 백엔드도 ingest는 유효(계약 1) — correlation만 no-mapping
            self.assertEqual(observatory.correlate(sd)[0]["link_status"], "no-mapping")

    def test_cross_vendor_same_sid_not_linked(self):
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            # 같은 session id가 다른 vendor 네임스페이스(grok)에 존재 — claude-code는 못 잇는다
            observatory.record(sd, [adapters._cell("grok", self.NSID,
                                                   last_ts="2026-07-15T10:00:00Z")],
                               reason="test")
            self._reported(sd, backend="claude-code")
            self.assertEqual(observatory.correlate(sd)[0]["link_status"], "no-passive")

    def test_prefix_id_not_linked(self):
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            observatory.record(sd, [_c(sid=self.NSID[:-1])], reason="test")  # 근접 id
            self._reported(sd)
            self.assertEqual(observatory.correlate(sd)[0]["link_status"], "no-passive")

    def test_correlate_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            observatory.record(sd, [_c(sid=self.NSID)], reason="test")
            self._reported(sd)
            before = sorted((p.name, p.stat().st_size)
                            for p in (sd / "observatory").iterdir())
            observatory.correlate(sd)
            after = sorted((p.name, p.stat().st_size)
                           for p in (sd / "observatory").iterdir())
            self.assertEqual(before, after)                 # 읽기 시점 계산 — 무기록

    def test_days_filters_reported_only_passive_window_free(self):
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            # passive는 오래전 settle — reported 창(since_days) 밖이어도 링크되어야
            observatory.record(sd, [_c(sid=self.NSID, last_ts="2026-01-01T00:00:00Z")],
                               reason="test")
            self._reported(sd)   # anchor=recordedAt 2026-07-24
            links = observatory.correlate(sd, since_days=36500)
            self.assertEqual(links[0]["link_status"], "linked")

    def test_render_smoke(self):
        with tempfile.TemporaryDirectory() as td:
            sd = _state(td)
            observatory.record(sd, [_c(sid=self.NSID)], reason="test")
            self._reported(sd)
            out = observatory.render_correlation(observatory.correlate(sd), 30)
            self.assertIn("linked 1", out)
            self.assertIn("reported:", out)
            self.assertIn("passive :", out)
            self.assertIn("병합·판결 없음", out)
