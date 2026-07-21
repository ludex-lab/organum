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

    def test_loadout_none_when_unjoined(self):
        # 미조인 셀(브리지 없음)은 loadout None — role None과 같은 정직성(오귀속 금지)
        with tempfile.TemporaryDirectory() as td:
            sd, _ = st.init_state_dir(Path(td), "owner")
            observatory.record(sd, [adapters._cell("codex", "cU", last_ts="2026-07-15T10:00:00Z")], "sync")
            r = observatory.load(sd)[0]
            self.assertIsNone(r["role"])
            self.assertIsNone(r["loadout"])

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
            observatory.record(sd, [_c(sid="old-mara", last_ts="2026-07-10T10:00:00Z",
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
