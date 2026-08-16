"""bench — 피어저널 수확·집계 (label≠identity·포화 강등·verbatim+provenance·read-only)."""

import json
import tempfile
import unittest
from pathlib import Path

from organum import bench, session
from organum import state as st


class TestBench(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, _ = st.init_state_dir(Path(self._tmp.name), "owner")

    def tearDown(self):
        self._tmp.cleanup()

    def _journal(self, rater, peers, role="critic"):
        soma = st.ensure_soma(self.state_dir, rater)
        session.start(soma, rater, role, f"{rater} 세션", "# charter\n")
        session.end(soma, shipped=["x"], peers=peers)

    def _declare(self, cell, role="engine"):
        soma = st.ensure_soma(self.state_dir, cell)
        session.start(soma, cell, role, f"{cell} 세션", "# charter\n")
        session.end(soma, shipped=[], peers=[])

    def test_resolved_vs_label_only(self):
        self._declare("engine")                        # 선언 셀 실존 → resolve 가능
        self._journal("critic1", [
            {"peer": "engine", "strengths": ["계약 구체화"], "frictions": [],
             "would_pair_again": True, "role_fit": "리드 적합"},
            {"peer": "engine·codex", "strengths": ["신속"], "frictions": ["재촉"],
             "would_pair_again": True, "role_fit": "실행 강"},   # R2 실물 라벨 — 병합 금지
        ])
        rep = bench.report(self.state_dir)
        by = {p["peer"]: p for p in rep["peers"]}
        self.assertIn("engine", by)
        self.assertTrue(by["engine"]["resolved"])
        self.assertIn("engine·codex", by)               # fuzzy 병합 없이 label-only 유지
        self.assertFalse(by["engine·codex"]["resolved"])

    def test_case_insensitive_resolve_same_cell(self):
        self._declare("engine")
        self._journal("a1", [{"peer": "Engine", "strengths": ["s"], "frictions": [],
                              "would_pair_again": True, "role_fit": ""}])
        self._journal("a2", [{"peer": "engine", "strengths": ["t"], "frictions": [],
                              "would_pair_again": True, "role_fit": ""}], role="tests")
        rep = bench.report(self.state_dir)
        eng = [p for p in rep["peers"] if p["peer"] == "engine"]
        self.assertEqual(len(eng), 1)                   # cell_key 계약으로 한 identity
        self.assertEqual(eng[0]["journals_n"], 2)
        self.assertEqual(eng[0]["raters_n"], 2)
        self.assertEqual(sorted(eng[0]["labels"]), ["Engine", "engine"])

    def test_saturation_demotion(self):
        self._journal("r1", [
            {"peer": f"p{i}", "strengths": [], "frictions": [],
             "would_pair_again": True, "role_fit": ""} for i in range(10)
        ])
        rep = bench.report(self.state_dir)
        self.assertTrue(rep["wpa_saturated"])           # 10/10 True > 90% → 보조 플래그
        self.assertEqual(rep["wpa"], {"true": 10, "false": 0, "null": 0})

    def test_no_saturation_when_discriminating(self):
        self._journal("r1", [
            {"peer": "a", "strengths": [], "frictions": [], "would_pair_again": True,
             "role_fit": ""},
            {"peer": "b", "strengths": [], "frictions": [], "would_pair_again": False,
             "role_fit": ""},
            {"peer": "c", "strengths": [], "frictions": [], "would_pair_again": None,
             "role_fit": ""},
        ])
        rep = bench.report(self.state_dir)
        self.assertFalse(rep["wpa_saturated"])
        self.assertEqual(rep["wpa"], {"true": 1, "false": 1, "null": 1})

    def test_provenance_on_verbatim(self):
        self._journal("chief", [{"peer": "builder", "strengths": ["레인 일관 착륙"],
                                 "frictions": ["수신자 미확인 대기"],
                                 "would_pair_again": True, "role_fit": "내용 소유 적합",
                                 "direction": "downward"}], role="chief")
        rep = bench.report(self.state_dir)
        p = rep["peers"][0]
        self.assertEqual(p["strengths"][0]["rater"], "chief")
        self.assertEqual(p["strengths"][0]["direction"], "downward")
        self.assertTrue(p["strengths"][0]["sid"])
        self.assertEqual(p["role_fit"][0]["text"], "내용 소유 적합")
        self.assertEqual(p["directions"], {"downward": 1})

    def test_read_only_no_writes(self):
        self._journal("r1", [{"peer": "x", "strengths": ["s"], "frictions": [],
                              "would_pair_again": True, "role_fit": ""}])
        snap = sorted(str(p.relative_to(self.state_dir)) + f":{p.stat().st_size}"
                      for p in self.state_dir.rglob("*") if p.is_file())
        bench.report(self.state_dir)
        after = sorted(str(p.relative_to(self.state_dir)) + f":{p.stat().st_size}"
                       for p in self.state_dir.rglob("*") if p.is_file())
        self.assertEqual(snap, after)

    def test_days_filter(self):
        self._journal("r1", [{"peer": "x", "strengths": [], "frictions": [],
                              "would_pair_again": True, "role_fit": ""}])
        self.assertEqual(bench.report(self.state_dir, since_days=1)["entries_n"], 1)
        self.assertEqual(bench.report(self.state_dir, since_days=-1)["entries_n"], 0)

    def test_invalid_declared_cell_stays_label_only(self):
        # critic blocker 재현: 손상/legacy 세션 파일의 cell="ENGINE!"(비-canonical)이
        # cell_key sanitize("engine")로 정규화돼 정상 라벨 peer="engine"을 resolved=true로
        # 승격하던 것 — 선언측 valid_cell_id 게이트(디스크 파일은 신뢰 경계 밖, read 재검증)
        self._declare("owner2")
        p = next((self.state_dir / "cells" / "owner2" / "sessions").glob("*.json"))
        rec = json.loads(p.read_text(encoding="utf-8"))
        rec["cell"] = "ENGINE!"
        p.write_text(json.dumps(rec), encoding="utf-8")
        self._journal("critic1", [{"peer": "engine", "strengths": [], "frictions": [],
                                   "would_pair_again": True, "role_fit": ""}])
        rep = bench.report(self.state_dir)
        eng = next(pp for pp in rep["peers"] if pp["peer"] == "engine")
        self.assertFalse(eng["resolved"])                 # label-only 유지

    def test_nonstring_cell_does_not_crash_read_view(self):
        # critic 비차단 hardening: 손상 JSON cell=123(비-문자열)이 valid_cell_id TypeError로
        # 읽기 뷰를 죽이던 것 — isinstance 게이트, false resolve 없음 유지
        self._declare("owner3")
        p = next((self.state_dir / "cells" / "owner3" / "sessions").glob("*.json"))
        rec = json.loads(p.read_text(encoding="utf-8"))
        rec["cell"] = 123
        p.write_text(json.dumps(rec), encoding="utf-8")
        self._journal("critic1", [{"peer": "engine", "strengths": [], "frictions": [],
                                   "would_pair_again": True, "role_fit": ""}])
        rep = bench.report(self.state_dir)                # crash 없음
        eng = next(pp for pp in rep["peers"] if pp["peer"] == "engine")
        self.assertFalse(eng["resolved"])

    def test_render_summary_and_detail(self):
        self._declare("engine")
        self._journal("critic1", [{"peer": "engine", "strengths": ["계약 구체화"],
                                   "frictions": ["자기계약 blocking"],
                                   "would_pair_again": True, "role_fit": "리드 적합"}])
        rep = bench.report(self.state_dir)
        out = bench.render(rep)
        self.assertIn("bench peers", out)
        self.assertIn("engine", out)
        self.assertIn("축 코딩 없음", out)              # 정직 경계 명시
        detail = bench.render(rep, peer="engine")
        self.assertIn("계약 구체화", detail)
        self.assertIn("critic1", detail)                # provenance 표기
        self.assertIn("리드 적합", detail)


if __name__ == "__main__":
    unittest.main()


class TestCells(unittest.TestCase):
    """claim cell 집계 — §1 키·§6 joint_observed·contrast 정직성·C2."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, _ = st.init_state_dir(Path(self._tmp.name), "owner")

    def tearDown(self):
        self._tmp.cleanup()

    def _row(self, sid, model="solar-open2", role="critic", ptype="seam/integration",
             loadout=("*",), in_tok=100, joint=True, declared="lens"):
        obs = self.state_dir / "observatory"
        obs.mkdir(parents=True, exist_ok=True)
        row = {"v": 1, "vendor": "grok", "session_id": sid, "last_ts": "2026-07-25T09:00:00Z",
               "model": model, "role": role, "problem_type": ptype,
               "loadout": list(loadout) if loadout is not None else None,
               "joint_observed": joint, "declared": declared, "id": sid[:8],
               "in_tok": in_tok, "out_tok": None, "cache": 50,
               "tools": {"read": 2}, "files_touched": 1, "origin": "terminal"}
        with open(obs / "2026-07.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")

    def test_grouping_and_c2(self):
        self._row("s1", in_tok=100)
        self._row("s2", in_tok=200)
        self._row("s3", role="engine", ptype=None, declared=None)   # 다른 셀
        rep = bench.cells(self.state_dir)
        self.assertEqual(rep["rows_n"], 3)
        self.assertEqual(len(rep["cells"]), 2)
        c = rep["cells"][0]                                # n 내림차순 — critic 셀
        self.assertEqual((c["brain"], c["role"], c["problem_type"]),
                         ("solar-open2", "critic", "seam/integration"))
        self.assertEqual(c["n"], 2)
        self.assertEqual(c["cost"]["in_tok"], 300)         # 측정분 합
        self.assertIsNone(c["cost"]["out_tok"])            # C2: 전부 미측정=None(0 아님)
        self.assertEqual(c["cost"]["out_tok_unmeasured"], 2)
        self.assertTrue(c["joint_observed"])               # §6 필드 강제
        self.assertEqual(c["declared_cells"], ["lens"])
        self.assertEqual(c["evidence_grade"], "rwe-observational")

    def test_contrast_status_honesty(self):
        # loadout 상수 → no-loadout-variation · 변주 존재 → varied-no-score (delta 주장 없음)
        self._row("s1", loadout=("*",))
        rep = bench.cells(self.state_dir)
        self.assertEqual(rep["cells"][0]["contrast_status"], "no-loadout-variation")
        self.assertIsNone(rep["cells"][0]["contrast"])
        self._row("s2", loadout=())                        # 같은 brp에 bare 변주
        rep2 = bench.cells(self.state_dir)
        self.assertEqual(len(rep2["cells"]), 2)
        for c in rep2["cells"]:
            self.assertEqual(c["contrast_status"], "varied-no-score")
            self.assertIsNone(c["contrast"])               # score 원천 없음 — delta 주장 안 함

    def test_legacy_row_joint_derived_from_role(self):
        # joint_observed 키 이전 레거시 row — role 있으면 worker 세팅 상수로 joint 처리
        obs = self.state_dir / "observatory"
        obs.mkdir(parents=True, exist_ok=True)
        row = {"v": 1, "vendor": "grok", "session_id": "sL", "last_ts": "2026-07-25T09:00:00Z",
               "model": "m", "role": "engine", "loadout": ["*"], "in_tok": 1,
               "tools": {}, "origin": "terminal"}
        with open(obs / "2026-07.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        rep = bench.cells(self.state_dir)
        self.assertTrue(rep["cells"][0]["joint_observed"])

    def _reported(self, rid_suffix="a", cell="lens", role="critic"):
        import test_observatory as tob
        from organum import observatory as _o
        rec = tob._obs_v1(run__id="ocobs-" + rid_suffix * 64,
                          identity__canonicalCell=cell, identity__role=role)
        _o.ingest_report(self.state_dir, rec)

    def _declare_session(self, cell, role="critic", loadout="graphify",
                         ptype="discovery/navigation"):
        soma = st.ensure_soma(self.state_dir, cell)
        session.start(soma, cell, role, "s", "# c\n", loadout=loadout, problem_type=ptype)
        session.end(soma)

    def test_reported_rows_join_axes_and_stay_separate(self):
        # reported row가 선언 세션에서 loadout/problem_type을 조인하되, source가 셀 키 축이라
        # 같은 축의 passive 셀과 절대 안 섞인다(증거 분리)
        self._declare_session("lens")
        self._reported(cell="lens", role="critic")
        self._row("p1", model="solar-open2", role="critic", ptype="discovery/navigation",
                  loadout=("graphify",), declared="lens")
        rep = bench.cells(self.state_dir)
        srcs = {(c["source"], tuple(c["loadout"] or [])) for c in rep["cells"]}
        self.assertIn(("reported", ("graphify",)), srcs)   # 조인 성공
        self.assertIn(("passive", ("graphify",)), srcs)    # 분리 유지(셀 2개)
        rc = next(c for c in rep["cells"] if c["source"] == "reported")
        self.assertEqual(rc["problem_type"], "discovery/navigation")
        self.assertTrue(rc["joint_observed"])

    def test_reported_role_mismatch_fail_closed(self):
        # 선언 세션 role(engine) ≠ reported role(critic) → 축 None (오귀속 금지)
        self._declare_session("lens", role="engine")
        self._reported(cell="lens", role="critic")
        rep = bench.cells(self.state_dir)
        rc = next(c for c in rep["cells"] if c["source"] == "reported")
        self.assertIsNone(rc["loadout"])
        self.assertIsNone(rc["problem_type"])

    def test_render_cells_smoke(self):
        self._row("s1")
        out = bench.render_cells(bench.cells(self.state_dir))
        self.assertIn("bench cells", out)
        self.assertIn("solar-open2 × critic × seam/integration", out)
        self.assertIn("no-loadout-variation", out)
        self.assertIn("분리 효과 주장 불가", out)          # §6 정직 경계 명시
