"""substrate-health — 면역 감지 tier (read-only 관측·세션 단위 판정·사람 게이트 assert)."""

import json
import tempfile
import unittest
from pathlib import Path

from organum import alarm, health, relay
from organum import state as st

MB = 1024 * 1024


def _mk(path: Path, mb: float):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * int(mb * MB))


class _Thresholds(unittest.TestCase):
    """작은 실파일로 검정 — 문턱을 테스트 스케일로 patch, teardown 복원."""

    _CONSTS = ("SESSION_WARN_MB", "SESSION_ALERT_MB", "GROWTH_ALERT_MB_MIN",
               "GROWTH_MIN_DELTA_MB", "DISK_WARN_GB", "DISK_ALERT_GB", "SIGNATURES")

    def setUp(self):
        self._saved = {k: getattr(health, k) for k in self._CONSTS}
        health.SESSION_WARN_MB = 1.5
        health.SESSION_ALERT_MB = 3.0
        health.GROWTH_ALERT_MB_MIN = 0.1
        health.GROWTH_MIN_DELTA_MB = 1.0
        health.SIGNATURES = [{"vendor": "grok", "dirname": "recap_requests",
                              "warn_mb": 0.5, "alert_mb": 2.5, "note": "recap 폭주 항체"}]
        self._tmp = tempfile.TemporaryDirectory()
        self.td = Path(self._tmp.name)
        self.state_dir, _ = st.init_state_dir(self.td / "proj", "cody")

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(health, k, v)
        self._tmp.cleanup()

    def _grok_root(self):
        return self.td / "grokstore"


class TestMeasure(_Thresholds):
    def test_session_unit_flag_and_identity(self):
        # grok: <enc-cwd>/<sid>/ = 세션 단위(depth 2) — 크기 초과 + sid·cwd 파생
        root = self._grok_root()
        _mk(root / "%2FUsers%2Fjj%2FMovies" / "sid-001" / "chat_history.jsonl", 2.0)
        rep = health.measure(roots=[("grok", root)], disk_free_gb=100)
        f = [x for x in rep["findings"] if x["kind"] == "store-size"]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["severity"], "warn")
        self.assertEqual(f[0]["session_id"], "sid-001")
        self.assertEqual(f[0]["cwd_hint"], "/Users/jj/Movies")

    def test_codex_month_dir_not_flagged_unit_file_is(self):
        # codex 세션 단위 = depth 4 파일 — 월 폴더(depth 2)의 정상 누적은 폭주로 오인 금지
        root = self.td / "codexstore"
        uuid = "019f6544-c36b-70e0-a36b-30c3796443e5"
        _mk(root / "2026" / "07" / "15" / f"rollout-2026-07-15T19-13-53-{uuid}.jsonl", 2.0)
        _mk(root / "2026" / "07" / "15" / "rollout-small.jsonl", 1.0)  # 합계로 월=3.0MB(alert급)
        rep = health.measure(roots=[("codex", root)], disk_free_gb=100)
        f = [x for x in rep["findings"] if x["kind"] == "store-size"]
        self.assertEqual(len(f), 1)                      # 파일 하나만 — 월/일 폴더 미플래그
        self.assertTrue(f[0]["path"].endswith(".jsonl"))
        self.assertEqual(f[0]["session_id"], uuid)

    def test_signature_antibody_low_threshold(self):
        # 세션 단위 문턱(1.5MB) 아래여도 항체 시그니처(0.5MB)는 조기 경보
        root = self._grok_root()
        _mk(root / "%2Fp" / "sid-002" / "recap_requests" / "r1.json", 1.0)
        rep = health.measure(roots=[("grok", root)], disk_free_gb=100)
        sig = [x for x in rep["findings"] if x["kind"] == "signature"]
        self.assertEqual(len(sig), 1)
        self.assertEqual(sig[0]["severity"], "warn")
        self.assertIn("항체", sig[0]["note"])
        self.assertEqual(sig[0]["session_id"], "sid-002")

    def test_disk_low(self):
        rep = health.measure(roots=[], disk_free_gb=5.0)
        self.assertEqual(rep["findings"][0]["kind"], "disk-low")
        self.assertEqual(rep["findings"][0]["severity"], "alert")

    def test_growth_rate_and_ancestor_suppression(self):
        root = self._grok_root()
        target = root / "%2Fp" / "sid-003"
        _mk(target / "chat_history.jsonl", 1.2)          # 문턱(1.5) 아래 — 크기론 안 걸림
        roots = [("grok", root)]
        rep = health.measure(self.state_dir, roots=roots, persist=True, disk_free_gb=100)
        self.assertFalse(rep["findings"])                 # baseline
        # 직전 측정을 10분 전·모두 0.0MB로 조작 → 재측정 시 +1.2MB/10분 = 성장 alert
        cache = health._health_dir(self.state_dir) / health.LAST_FILE
        d = json.loads(cache.read_text(encoding="utf-8"))
        old_ts = "2026-07-25T00:00:00Z"
        import datetime
        now = datetime.datetime(2026, 7, 25, 0, 10, tzinfo=datetime.timezone.utc)
        cache.write_text(json.dumps({"ts": old_ts, "mb": {k: 0.0 for k in d["mb"]}}),
                         encoding="utf-8")
        rep2 = health.measure(self.state_dir, roots=roots, persist=False,
                              disk_free_gb=100, now=now)
        g = [x for x in rep2["findings"] if x["kind"] == "store-growth"]
        self.assertEqual(len(g), 1)                       # 조상(루트·enc-cwd) 성장은 억제
        self.assertEqual(g[0]["path"], str(target))
        self.assertAlmostEqual(g[0]["rate_mb_min"], 0.1, places=1)

    def test_transition_log_appends_on_change_only(self):
        root = self._grok_root()
        _mk(root / "%2Fp" / "sid-004" / "big.jsonl", 2.0)
        roots = [("grok", root)]
        health.measure(self.state_dir, roots=roots, persist=True, disk_free_gb=100)
        health.measure(self.state_dir, roots=roots, persist=True, disk_free_gb=100)
        log = health._health_dir(self.state_dir) / health.LOG_FILE
        lines = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(lines), 1)                   # 무변화 재측정 = append 없음
        # 해소되면 ok 전이가 기록된다 (사건의 끝도 이력)
        (root / "%2Fp" / "sid-004" / "big.jsonl").write_bytes(b"")
        health.measure(self.state_dir, roots=roots, persist=True, disk_free_gb=100)
        lines = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[-1]["severity"], "ok")

    def test_measure_does_not_touch_vendor_files(self):
        root = self._grok_root()
        p = root / "%2Fp" / "sid-005" / "chat_history.jsonl"
        _mk(p, 2.0)
        before = p.lstat().st_mtime_ns
        health.measure(roots=[("grok", root)], disk_free_gb=100)
        self.assertEqual(p.lstat().st_mtime_ns, before)   # read-only — lstat만


class TestResolveAndAssert(_Thresholds):
    def _shard_row(self, vendor="grok", sid="sid-010", declared="lens"):
        obs = self.state_dir / "observatory"
        obs.mkdir(parents=True, exist_ok=True)
        row = {"v": 1, "vendor": vendor, "session_id": sid, "last_ts": "2026-07-25T09:00:00Z",
               "declared": declared, "id": sid[:8]}
        with open(obs / "2026-07.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")

    def test_resolve_cell_via_passive_declared(self):
        self._shard_row()
        self.assertEqual(health.resolve_cell(self.state_dir, "grok", "sid-010"), "lens")
        self.assertIsNone(health.resolve_cell(self.state_dir, "grok", "sid-x"))
        self.assertIsNone(health.resolve_cell(self.state_dir, None, None))

    def test_assert_human_alarm_and_auto_letter(self):
        root = self._grok_root()
        _mk(root / "%2Fp" / "sid-010" / "big.jsonl", 2.0)
        self._shard_row(sid="sid-010", declared="lens")
        cwd = self.state_dir.parent
        orig = health.store_roots
        health.store_roots = lambda: [("grok", root)]     # assert 내부 재측정 경로 주입
        try:
            path = str(root / "%2Fp" / "sid-010")
            res = health.assert_finding(cwd, self.state_dir, path, frm="human", level="pause")
        finally:
            health.store_roots = orig
        a = alarm.active(cwd)[0]
        self.assertEqual(a["level"], "pause")
        self.assertIn("substrate-health", a["body"])
        self.assertEqual(res["target"], "lens")           # passive declared 자동 식별
        letters = relay.list_all(cwd)
        mine = [m for m in letters if m.get("topic") == "substrate-health"]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["to"], "lens")
        self.assertTrue(mine[0]["escalate"])              # 관제탑 우선순위 표면

    def test_assert_worker_refused_and_unknown_path(self):
        root = self._grok_root()
        _mk(root / "%2Fp" / "sid-011" / "big.jsonl", 2.0)
        cwd = self.state_dir.parent
        orig = health.store_roots
        health.store_roots = lambda: [("grok", root)]
        try:
            path = str(root / "%2Fp" / "sid-011")
            with self.assertRaises(alarm.AlarmError):     # 사람 게이트 — worker 발동 불가
                health.assert_finding(cwd, self.state_dir, path, frm="worker01")
            with self.assertRaises(health.HealthError):   # finding 없는 경로 = 추측 경보 방지
                health.assert_finding(cwd, self.state_dir, "/no/such/path", frm="human")
        finally:
            health.store_roots = orig


if __name__ == "__main__":
    unittest.main()


class TestSymlinkEscape(_Thresholds):
    def test_symlink_not_followed_any_depth(self):
        # critic blocker 재현: store 내부 symlink → 외부 디렉터리의 문턱-초과 파일이
        # 세션 단위로 오인·오경보(escape/not-a-vendor-session.jsonl). symlink 미추적 계약.
        root = self._grok_root()
        _mk(root / "%2Fp" / "real-sid" / "small.jsonl", 0.1)
        external = self.td / "external"
        _mk(external / "not-a-vendor-session.jsonl", 2.0)      # alert급 크기
        (root / "escape").symlink_to(external)                  # depth1 symlink dir
        (root / "%2Fp" / "escape2").symlink_to(external)        # depth2(세션 단위) symlink dir
        (root / "%2Fp" / "real-sid" / "link.jsonl").symlink_to(
            external / "not-a-vendor-session.jsonl")            # 파일 symlink (lstat=링크 크기)
        rep = health.measure(self.state_dir, roots=[("grok", root)], persist=True,
                             disk_free_gb=100)
        self.assertEqual(rep["findings"], [])                   # 탈출 경로 오경보 없음
        self.assertNotIn("external", json.dumps(rep))
        cache = (health._health_dir(self.state_dir) / health.LAST_FILE).read_text(encoding="utf-8")
        self.assertNotIn("external", cache)                     # 성장 추적 노드에도 외부 경로 없음
