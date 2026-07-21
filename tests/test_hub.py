"""크로스-워크스페이스 허브 — 격리 불변식·send-시점 확정·epoch rebind·cursor·conflict.

핵심 불변식(docs/cross-workspace-hub-v0.md §6, organum-code 정렬 계약):
① frozen cell_key 불변(case-insensitive 유지),
② 크로스-워크스페이스 누수 없음(broadcast 부재 + send 시점 to_id 확정),
③ rebind 후 과거 epoch 편지가 새 cell로 안 샘,
④ inbox bounded·무손실·비소비(opaque cursor)."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from organum import cli
from organum import hub
from organum import state as st


class TestHub(unittest.TestCase):
    def setUp(self):
        self._hub = tempfile.mkdtemp()
        self._saved_hub = os.environ.get("ORGANUM_HUB")
        os.environ["ORGANUM_HUB"] = self._hub

    def tearDown(self):
        if self._saved_hub is None:
            os.environ.pop("ORGANUM_HUB", None)
        else:
            os.environ["ORGANUM_HUB"] = self._saved_hub
        shutil.rmtree(self._hub, ignore_errors=True)

    def _cell(self, cid, persona, workspace, role="critic"):
        rec = hub.register(cid, persona, workspace, f"/p/{workspace}", role)
        hub.mark_join(cid, reset=True)
        return rec

    def _items(self, cid, **kw):
        return hub.inbox(cid, **kw)["items"]

    # ── 불변식 ②: 핀포인트 + 크로스-워크스페이스 격리 ──
    def test_pinpoint_and_cross_workspace_isolation(self):
        self._cell("cellA", "critic", "warren")   # 같은 persona critic이
        self._cell("cellB", "critic", "ludex")    # 두 워크스페이스에
        self._cell("cellR", "reviewer", "warren")
        rec = hub.send("warren 리뷰 요청", frm="reviewer", from_id="cellR", to="critic@warren")
        self.assertEqual(rec["to"]["cell"], "cella")       # send 시점에 to_id 확정
        self.assertTrue(rec["to"]["epoch"])                # epoch 고정
        inA = self._items("cellA")
        inB = self._items("cellB")
        self.assertEqual(len(inA), 1)
        self.assertEqual(inA[0]["body"], "warren 리뷰 요청")
        self.assertEqual(inA[0]["to_id"], "cella")
        self.assertEqual(len(inB), 0)   # ★ critic@ludex는 critic@warren 편지를 안 받음(누수 없음)

    def test_cell_key_addressing(self):
        self._cell("cellA", "critic", "warren")
        hub.send("직접 지정", frm="x", from_id="cellX", to="cellA")
        got = self._items("cellA")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["body"], "직접 지정")

    def test_single_target_only(self):
        self._cell("cellA", "critic", "warren")
        self._cell("cellB", "critic", "ludex")
        with self.assertRaises(hub.HubError):   # 다중 대상은 각각 send (핀포인트)
            hub.send("둘 다", frm="x", from_id="cellX", to="critic@warren,critic@ludex")

    # ── broadcast 거부(핀포인트 강제) ──
    def test_broadcast_rejected(self):
        for bad in ("all", "*", "", "  "):
            with self.assertRaises(hub.HubError):
                hub.send("hi", frm="x", from_id="cellX", to=bad)

    # ── fail-closed 해소 ──
    def test_zero_resolution_fails(self):
        with self.assertRaises(hub.HubError):
            hub.send("아무도", frm="x", from_id="cellX", to="ghost@nowhere")

    def test_ambiguous_resolution_fails(self):
        self._cell("cellA", "critic", "warren")
        self._cell("cellB", "critic", "warren")   # 같은 alias 두 live cell
        with self.assertRaises(hub.HubError):
            hub.send("모호", frm="x", from_id="cellX", to="critic@warren")

    # ── 자기 글 제외(from_id) ──
    def test_self_exclusion(self):
        self._cell("cellA", "critic", "warren")
        hub.send("내 글", frm="critic", from_id="cellA", to="cellA")
        self.assertEqual(len(self._items("cellA")), 0)

    # ── 불변식 ④: 비소비 read + 명시 ACK ──
    def test_nonconsuming_read_and_ack(self):
        self._cell("cellA", "critic", "warren")
        rec = hub.send("hi", frm="x", from_id="cellX", to="critic@warren")
        self.assertEqual(len(self._items("cellA")), 1)
        self.assertEqual(len(self._items("cellA")), 1)   # 재독 동일(커서 전진 안 함)
        hub.mark_read("cellA", rec["file"])
        self.assertEqual(len(self._items("cellA")), 0)   # 명시 ACK 후 사라짐

    # ── 불변식 ④: bounded cursor 페이지네이션(무손실) ──
    def test_cursor_pagination(self):
        self._cell("cellA", "critic", "warren")
        for i in range(3):
            hub.send(f"m{i}", frm="x", from_id="cellX", to="critic@warren")
        p1 = hub.inbox("cellA", limit=2)
        self.assertEqual(len(p1["items"]), 2)
        self.assertTrue(p1["has_more"])
        self.assertTrue(p1["next_cursor"])
        p2 = hub.inbox("cellA", limit=2, cursor=p1["next_cursor"])
        self.assertEqual(len(p2["items"]), 1)
        self.assertFalse(p2["has_more"])
        bodies = [m["body"] for m in p1["items"] + p2["items"]]
        self.assertEqual(bodies, ["m0", "m1", "m2"])   # oldest-first, 무손실

    # ── idem: 같은 본문 dedup(안정 event_id) + 다른 본문 conflict(fail-closed) ──
    def test_idem_dedup_same_payload_stable_event_id(self):
        self._cell("cellA", "critic", "warren")
        r1 = hub.send("v1", frm="x", from_id="cellX", to="critic@warren", idem_key="k1")
        r2 = hub.send("v1", frm="x", from_id="cellX", to="critic@warren", idem_key="k1")
        self.assertEqual(r1["file"], r2["file"])
        self.assertEqual(r1["event_id"], r2["event_id"])   # 안정 event_id
        self.assertEqual(len(self._items("cellA")), 1)

    def test_idem_conflict_on_changed_payload(self):
        self._cell("cellA", "critic", "warren")
        hub.send("v1", frm="x", from_id="cellX", to="critic@warren", idem_key="k1")
        with self.assertRaises(hub.HubError):   # 같은 (from_id, idem)·다른 body = conflict
            hub.send("v2 다름", frm="x", from_id="cellX", to="critic@warren", idem_key="k1")
        self.assertEqual(len(self._items("cellA")), 1)   # 원본 유지, 새 봉투 없음

    def test_idem_conflict_on_changed_target(self):
        # 같은 key·같은 body라도 목적지(to_id)가 다르면 conflict — payload 지문에 목적지 포함
        self._cell("cellA", "critic", "warren")
        self._cell("cellB", "critic", "ludex")
        hub.send("same", frm="x", from_id="cellX", to="critic@warren", idem_key="k1")
        with self.assertRaises(hub.HubError):
            hub.send("same", frm="x", from_id="cellX", to="critic@ludex", idem_key="k1")

    # ── semantic ACK authorization (blocker 2) ──
    def test_ack_authorization(self):
        self._cell("cellA", "critic", "warren")
        self._cell("cellB", "reviewer", "warren")
        rec = hub.send("hi", frm="x", from_id="cellX", to="critic@warren")
        fn = rec["file"]
        # 다른 cell이 남의 편지 ACK 시도 → 거부(권한 없음)
        with self.assertRaises(hub.HubError):
            hub.mark_read("cellB", fn)
        # 존재하지 않는 파일 ACK → 거부
        with self.assertRaises(hub.HubError):
            hub.mark_read("cellA", "does-not-exist.md")
        # 경로 주입 시도 → 거부
        with self.assertRaises(hub.HubError):
            hub.mark_read("cellA", "../evil.md")
        # 정당한 수신자 ACK → receipt, 재시도는 idempotent(already_read)
        r1 = hub.mark_read("cellA", fn)
        self.assertTrue(r1["read"])
        self.assertFalse(r1["already_read"])
        self.assertEqual(r1["for_id"], "cella")
        r2 = hub.mark_read("cellA", fn)
        self.assertTrue(r2["already_read"])   # 유효 ACK 재시도만 idempotent
        self.assertEqual(len(self._items("cellA")), 0)

    def test_ack_rejects_old_epoch(self):
        # rebind 후 구 epoch 편지는 새 generation이 ACK 못 함
        self._cell("cellA", "critic", "warren")
        rec = hub.send("old", frm="x", from_id="cellX", to="critic@warren")
        hub.deregister("cellA")
        self._cell("cellA", "critic", "warren")   # 같은 cell 새 epoch
        with self.assertRaises(hub.HubError):
            hub.mark_read("cellA", rec["file"])   # 구 epoch item → ACK 거부

    # ── critic A1: epochless direct send → 다음 registration 누수 차단 ──
    def test_unregistered_direct_send_rejected(self):
        self._cell("cellA", "critic", "warren")
        hub.deregister("cellA")
        with self.assertRaises(hub.HubError):     # 미등록 cell direct send = fail-closed(빈 epoch 봉투 금지)
            hub.send("gap", frm="x", from_id="cellX", to="cellA")

    def test_a1_gap_send_no_leak_to_next_registration(self):
        # critic A1 반례: register(e1)→leave→(gap direct send)→same-cell register(e2, 다른 persona) → 0건
        self._cell("cellA", "critic", "warren")           # e1
        hub.deregister("cellA")
        with self.assertRaises(hub.HubError):             # gap send 자체가 실패 → 누수 봉투 없음
            hub.send("gap", frm="x", from_id="cellX", to="cellA")
        self._cell("cellA", "reviewer", "ludex")          # e2, 다른 persona
        self.assertEqual(len(self._items("cellA")), 0)    # 새 registration inbox 0건

    def test_empty_epoch_envelope_rejected_by_inbox_and_ack(self):
        # 방어 심층: 빈 to_epoch 봉투가 (레거시로라도) 있으면 inbox·ACK 모두 거부(wildcard 아님)
        from organum import field as _f
        self._cell("cellA", "critic", "warren")
        fn = _f.post(hub._base(), hub.RELAY_FIELD, "sneaky", frm="x", from_id="cellX",
                     to="cella", extra={"to_id": "cella", "to_epoch": ""})
        self.assertEqual(len(self._items("cellA")), 0)    # 등록 수신도 빈 epoch 거부
        with self.assertRaises(hub.HubError):
            hub.mark_read("cellA", fn)

    # ── critic 재감사 A1: None(미등록 수신)과 ""(빈 epoch)를 합치지 않음 ──
    def test_unregistered_receiver_no_empty_epoch_delivery(self):
        from organum import field as _f
        # 미등록 수신 cellZ + 빈 epoch 봉투 → inbox 0, ACK 거부 (None≠"" 못박음)
        fn = _f.post(hub._base(), hub.RELAY_FIELD, "sneaky", frm="x", from_id="cellX",
                     to="cellz", extra={"to_id": "cellz", "to_epoch": ""})
        self.assertEqual(len(self._items("cellZ")), 0)    # 미등록 수신 — 0
        with self.assertRaises(hub.HubError):
            hub.mark_read("cellZ", fn)                      # 미등록 수신 ACK 거부

    def test_alias_send_rejects_empty_epoch_registry(self):
        import json as _json
        # 손상된 registry(빈 epoch)를 alias로 해소 시도 → fail-closed(빈-epoch 봉투 생성 금지)
        d = hub._registry_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / "cellq.json").write_text(_json.dumps(
            {"cell_key": "cellq", "persona": "critic", "workspace": "broken",
             "epoch": "", "role": "critic"}), encoding="utf-8")
        with self.assertRaises(hub.HubError):
            hub.send("x", frm="s", from_id="cellS", to="critic@broken")

    # ── critic A2: idem scope가 익명·식별 발신자를 구분 ──
    def test_idem_scope_distinguishes_empty_and_identified_sender(self):
        self._cell("cellA", "critic", "warren")
        # 익명(from_id="")과 식별(from_id 있음)은 다른 (from_id, idem) scope → 별개 봉투(양방향)
        r1 = hub.send("same", frm="anon", from_id="", to="critic@warren", idem_key="k1")
        r2 = hub.send("same", frm="named", from_id="cellS", to="critic@warren", idem_key="k1")
        self.assertNotEqual(r1["file"], r2["file"])
        r3 = hub.send("same2", frm="named", from_id="cellS", to="critic@warren", idem_key="k2")
        r4 = hub.send("same2", frm="anon", from_id="", to="critic@warren", idem_key="k2")
        self.assertNotEqual(r3["file"], r4["file"])
        # 같은 빈 scope·같은 payload = dedup(진짜 멱등)
        r5 = hub.send("same3", frm="a", from_id="", to="critic@warren", idem_key="k3")
        r6 = hub.send("same3", frm="a", from_id="", to="critic@warren", idem_key="k3")
        self.assertEqual(r5["file"], r6["file"])

    # ── 불변식 ①: frozen cell_key(case-insensitive) 유지 ──
    def test_case_insensitive_identity(self):
        self._cell("CellA", "Critic", "Warren")          # 대문자 등록
        hub.send("hi", frm="x", from_id="cellX", to="critic@warren")   # 소문자 주소
        self.assertEqual(len(self._items("cella")), 1)   # 소문자 셀 id로 조회 — 같은 셀

    # ── 불변식 ③: rebind 후 과거 epoch 편지가 새 cell로 안 샘 ──
    def test_rebind_no_leak(self):
        self._cell("cellA", "critic", "warren")          # C1 (epoch e1)
        hub.send("old", frm="x", from_id="cellX", to="critic@warren")   # to_id=cella, epoch e1
        hub.deregister("cellA")                          # C1 leave → 슬롯 비움
        self._cell("cellC", "critic", "warren")          # C2 (다른 cell, epoch e2)
        rec = hub.send("new", frm="x", from_id="cellX", to="critic@warren")   # to_id=cellc
        self.assertEqual(rec["to"]["cell"], "cellc")
        got = self._items("cellC")
        self.assertEqual(len(got), 1)                    # ★ new만, 과거 old는 안 샘
        self.assertEqual(got[0]["body"], "new")

    # ── conflict: 같은 cell 다른 persona 조용한 재등록 금지 ──
    def test_rebind_conflict_same_cell(self):
        self._cell("cellA", "critic", "warren")
        with self.assertRaises(hub.HubError):
            hub.register("cellA", "reviewer", "warren", "/p/warren", "reviewer")
        # 명시 leave 후엔 rebind 허용
        hub.deregister("cellA")
        rec = hub.register("cellA", "reviewer", "warren", "/p/warren", "reviewer")
        self.assertEqual(rec["persona"], "reviewer")

    def test_same_cell_resume_keeps_epoch(self):
        r1 = self._cell("cellA", "critic", "warren")
        r2 = hub.register("cellA", "critic", "warren", "/p/warren", "critic")
        self.assertEqual(r1["epoch"], r2["epoch"])   # resume 수렴(같은 epoch)

    # ── registry 발견·해소 ──
    def test_registry_resolve_and_deregister(self):
        self._cell("cellA", "critic", "warren")
        self._cell("cellB", "critic", "ludex")
        self.assertEqual(len(hub.registry_all()), 2)
        self.assertEqual(len(hub.resolve("critic", "warren")), 1)
        self.assertEqual(hub.resolve("critic", "warren")[0]["cell_key"], "cella")
        self.assertTrue(hub.deregister("cellA"))
        self.assertEqual(len(hub.resolve("critic", "warren")), 0)
        self.assertIsNone(hub.registry_of("cellA"))

    # ── CLI join --persona 등록 + echo ──
    def test_join_persona_registers(self):
        with tempfile.TemporaryDirectory() as proj:
            st.init_state_dir(Path(proj), "owner")
            saved_cell = os.environ.pop("ORGANUM_CELL", None)
            cwd = os.getcwd()
            os.chdir(proj)
            try:
                rc = cli.main(["join", "--role", "critic", "--for", "warrencell",
                               "--persona", "critic", "--workspace", "warren"])
            finally:
                os.chdir(cwd)
                if saved_cell is not None:
                    os.environ["ORGANUM_CELL"] = saved_cell
            self.assertEqual(rc, 0)
            reg = hub.registry_of("warrencell")
            self.assertIsNotNone(reg)
            self.assertEqual(reg["persona"], "critic")
            self.assertEqual(reg["workspace"], "warren")
            self.assertTrue(reg["epoch"])

    def test_join_without_persona_no_registration(self):
        with tempfile.TemporaryDirectory() as proj:
            st.init_state_dir(Path(proj), "owner")
            saved_cell = os.environ.pop("ORGANUM_CELL", None)
            cwd = os.getcwd()
            os.chdir(proj)
            try:
                cli.main(["join", "--role", "critic", "--for", "plaincell"])
            finally:
                os.chdir(cwd)
                if saved_cell is not None:
                    os.environ["ORGANUM_CELL"] = saved_cell
            self.assertIsNone(hub.registry_of("plaincell"))   # opt-in — persona 없으면 미등록

    def test_join_invalid_persona_rejected(self):
        with tempfile.TemporaryDirectory() as proj:
            st.init_state_dir(Path(proj), "owner")
            saved_cell = os.environ.pop("ORGANUM_CELL", None)
            cwd = os.getcwd()
            os.chdir(proj)
            try:
                with self.assertRaises(SystemExit):
                    cli.main(["join", "--role", "critic", "--for", "c1", "--persona", "../bad"])
            finally:
                os.chdir(cwd)
                if saved_cell is not None:
                    os.environ["ORGANUM_CELL"] = saved_cell


if __name__ == "__main__":
    unittest.main()
