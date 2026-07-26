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
