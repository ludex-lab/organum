"""core-integrity 감시 — git-추적 core의 blessed/unblessed/unprotected (memory-surveillance v0).

정직 경계: 탐지지 예방·판결 아님. 여기선 git 관점 분류(commit=bless)만 검증한다."""

import datetime
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from organum import integrity
from organum import observatory
from organum import state as st


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


def _init_repo(td):
    p = Path(td)
    _git(p, "init", "-q")
    _git(p, "config", "user.email", "t@t")
    _git(p, "config", "user.name", "tester")
    st.init_state_dir(p, "owner")
    return p


class TestCoreIntegrity(unittest.TestCase):
    def _repo(self, td):
        return _init_repo(td)

    def test_no_git_repo(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            st.init_state_dir(p, "owner")
            self.assertFalse(integrity.is_git_repo(p))
            self.assertEqual(integrity.classify(p, "CONTRACT.md")["status"], "no-git")

    def test_blessed_after_commit(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._repo(td)
            (p / "CONTRACT.md").write_text("헌법 v1\n", encoding="utf-8")
            _git(p, "add", "CONTRACT.md")
            _git(p, "commit", "-q", "-m", "add contract")
            c = integrity.classify(p, "CONTRACT.md")
            self.assertEqual(c["status"], "blessed")          # committed = bless
            self.assertIsNotNone(c["last_commit"])
            self.assertEqual(c["last_commit"]["author"], "tester")   # 귀속=git author

    def test_unblessed_on_uncommitted_change(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._repo(td)
            (p / "CONTRACT.md").write_text("헌법 v1\n", encoding="utf-8")
            _git(p, "add", "CONTRACT.md")
            _git(p, "commit", "-q", "-m", "v1")
            (p / "CONTRACT.md").write_text("헌법 v2 — 미commit 변경\n", encoding="utf-8")
            self.assertEqual(integrity.classify(p, "CONTRACT.md")["status"], "unblessed")
            # 다시 commit(=bless) → blessed 복귀
            _git(p, "commit", "-q", "-am", "v2")
            self.assertEqual(integrity.classify(p, "CONTRACT.md")["status"], "blessed")

    def test_unprotected_when_not_tracked(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._repo(td)
            (p / "CONTRACT.md").write_text("추적 안 됨\n", encoding="utf-8")  # add 안 함
            self.assertEqual(integrity.classify(p, "CONTRACT.md")["status"], "unprotected")

    def test_report_auto_core_and_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._repo(td)
            state_dir = p / ".organum"
            # AUTO_CORE 루트 파일
            (p / "CONTRACT.md").write_text("헌법\n", encoding="utf-8")
            # manifest 선언 core
            (p / "core-decision.md").write_text("핵심 결정\n", encoding="utf-8")
            (state_dir / "core-manifest.json").write_text(
                '{"core":[{"path":"core-decision.md","authority":"canonical"}]}', encoding="utf-8")
            _git(p, "add", "CONTRACT.md", "core-decision.md")
            _git(p, "commit", "-q", "-m", "core")
            rep = integrity.report(state_dir)
            paths = {r["path"]: r for r in rep}
            self.assertIn("CONTRACT.md", paths)                # auto
            self.assertIn("core-decision.md", paths)           # manifest
            self.assertEqual(paths["core-decision.md"]["authority"], "canonical")
            self.assertEqual(paths["CONTRACT.md"]["status"], "blessed")

    def test_manifest_path_injection_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._repo(td)
            state_dir = p / ".organum"
            (state_dir / "core-manifest.json").write_text(
                '{"core":[{"path":"../../etc/passwd"},{"path":"/etc/hosts"}]}', encoding="utf-8")
            # 경로 주입(상대 탈출·절대)은 core_paths에서 배제
            self.assertNotIn("../../etc/passwd", integrity.core_paths(state_dir))
            self.assertNotIn("/etc/hosts", integrity.core_paths(state_dir))


class TestObservatoryIntegritySurveillance(unittest.TestCase):
    """observatory 시간축 감시 — transition 로그·fossil 탐지 (memory-surveillance observatory tier)."""

    def test_transition_log_only_on_change(self):
        with tempfile.TemporaryDirectory() as td:
            p = _init_repo(td)
            sd = p / ".organum"
            (p / "CONTRACT.md").write_text("v1\n", encoding="utf-8")
            _git(p, "add", "CONTRACT.md")
            _git(p, "commit", "-q", "-m", "v1")
            n1 = observatory.record_integrity(sd)
            self.assertGreaterEqual(n1, 1)                 # 최초 = transition 기록
            self.assertEqual(observatory.record_integrity(sd), 0)  # 변화 없음 = 0 (로그 안 커짐)
            (p / "CONTRACT.md").write_text("v2 미commit\n", encoding="utf-8")
            self.assertGreaterEqual(observatory.record_integrity(sd), 1)  # blessed→unblessed transition

    def test_recent_unblessed_not_fossil(self):
        with tempfile.TemporaryDirectory() as td:
            p = _init_repo(td)
            sd = p / ".organum"
            (p / "CONTRACT.md").write_text("v1\n", encoding="utf-8")
            _git(p, "add", "CONTRACT.md")
            _git(p, "commit", "-q", "-m", "v1")
            (p / "CONTRACT.md").write_text("v2 미commit\n", encoding="utf-8")
            observatory.record_integrity(sd)               # 방금 unblessed
            d = {x["path"]: x for x in observatory.integrity_drift(sd)}["CONTRACT.md"]
            self.assertEqual(d["status"], "unblessed")
            self.assertFalse(d["fossil"])                  # 방금 = fossil 아님

    def test_old_unblessed_is_fossil(self):
        with tempfile.TemporaryDirectory() as td:
            p = _init_repo(td)
            sd = p / ".organum"
            obs_dir = sd / "observatory"
            obs_dir.mkdir(parents=True, exist_ok=True)
            old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=10)) \
                .strftime("%Y-%m-%dT%H:%M:%SZ")
            (obs_dir / "integrity.jsonl").write_text(
                json.dumps({"ts": old, "path": "CONTRACT.md", "status": "unblessed", "rev": ""}) + "\n",
                encoding="utf-8")
            d = {x["path"]: x for x in observatory.integrity_drift(sd, fossil_days=5.0)}["CONTRACT.md"]
            self.assertEqual(d["status"], "unblessed")
            self.assertTrue(d["fossil"])                   # 10일째 unblessed = fossil(방치된 sediment)
            self.assertGreater(d["drift_days"], 5)

    def test_untracked_when_no_declared_session_at_change(self):
        # 두-렌즈-for-memory: core 변이인데 활성 세션 0 → untracked(검토, verdict 아님)
        with tempfile.TemporaryDirectory() as td:
            p = _init_repo(td)
            sd = p / ".organum"
            (p / "CONTRACT.md").write_text("v1\n", encoding="utf-8")
            _git(p, "add", "CONTRACT.md")
            _git(p, "commit", "-q", "-m", "v1")
            (p / "CONTRACT.md").write_text("v2 미commit\n", encoding="utf-8")  # 세션 없이 변이
            observatory.record_integrity(sd)
            d = {x["path"]: x for x in observatory.integrity_drift(sd)}["CONTRACT.md"]
            self.assertTrue(d["no_context_at_observation"])       # B3: 관측 시 조율 0
            self.assertEqual(d["context_at_observation"], [])

    def test_attributed_when_session_active_at_change(self):
        from organum import session as _sess
        with tempfile.TemporaryDirectory() as td:
            p = _init_repo(td)
            sd = p / ".organum"
            soma = st.ensure_soma(sd, "critic1")
            _sess.start(soma, "critic1", "critic", "리뷰", "charter")   # declared 세션 활성
            (p / "CONTRACT.md").write_text("v1\n", encoding="utf-8")
            _git(p, "add", "CONTRACT.md")
            _git(p, "commit", "-q", "-m", "v1")
            (p / "CONTRACT.md").write_text("v2 미commit\n", encoding="utf-8")
            observatory.record_integrity(sd)
            d = {x["path"]: x for x in observatory.integrity_drift(sd)}["CONTRACT.md"]
            self.assertFalse(d["no_context_at_observation"])       # 관측 시 세션 활성
            self.assertTrue(any(c["role"] == "critic" for c in d["context_at_observation"]))

    def test_non_git_records_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            st.init_state_dir(p, "owner")                  # git 아님
            self.assertEqual(observatory.record_integrity(p / ".organum"), 0)


class TestFalseCleanB1(unittest.TestCase):
    """critic B1: 실제 변경이 blessed로 새면 안 됨 — 의심스러우면 fail-closed."""

    def _contract(self, td):
        p = _init_repo(td)
        (p / "CONTRACT.md").write_text("v1\n", encoding="utf-8")
        _git(p, "add", "CONTRACT.md")
        _git(p, "commit", "-q", "-m", "v1")
        return p

    def test_deleted_tracked_core_surfaced_not_omitted(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._contract(td)
            (p / "CONTRACT.md").unlink()                         # 삭제 = 변경
            rep = {r["path"]: r for r in integrity.report(p / ".organum")}
            self.assertIn("CONTRACT.md", rep)                    # inventory에서 안 사라짐
            self.assertEqual(rep["CONTRACT.md"]["status"], "unblessed")  # tracked 삭제 = uncommitted 변경

    def test_deleted_declared_core_is_missing(self):
        # manifest 선언 core가 미추적·부재 → missing(선언은 존재 여부로 안 사라짐)
        with tempfile.TemporaryDirectory() as td:
            p = _init_repo(td)
            sd = p / ".organum"
            (sd / "core-manifest.json").write_text(
                json.dumps({"core": [{"path": "decision.md"}]}), encoding="utf-8")  # decision.md 없음
            rep = {r["path"]: r for r in integrity.report(sd)}
            self.assertIn("decision.md", rep)
            self.assertEqual(rep["decision.md"]["status"], "missing")

    def test_assume_unchanged_not_blessed(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._contract(td)
            _git(p, "update-index", "--assume-unchanged", "CONTRACT.md")
            (p / "CONTRACT.md").write_text("숨은 변경\n", encoding="utf-8")
            self.assertEqual(integrity.classify(p, "CONTRACT.md")["status"], "unprotected")

    def test_symlink_core_unsupported(self):
        with tempfile.TemporaryDirectory() as td:
            p = _init_repo(td)
            (p / "real.md").write_text("real\n", encoding="utf-8")
            (p / "CONTRACT.md").symlink_to("real.md")
            _git(p, "add", "CONTRACT.md", "real.md")
            _git(p, "commit", "-q", "-m", "link")
            self.assertEqual(integrity.classify(p, "CONTRACT.md")["status"], "unsupported")

    def test_ignored_child_in_core_dir_unprotected(self):
        with tempfile.TemporaryDirectory() as td:
            p = _init_repo(td)
            (p / "coredir").mkdir()
            (p / "coredir" / "base.md").write_text("base\n", encoding="utf-8")
            (p / ".gitignore").write_text("coredir/injected.md\n", encoding="utf-8")
            _git(p, "add", "coredir/base.md", ".gitignore")
            _git(p, "commit", "-q", "-m", "c")
            (p / "coredir" / "injected.md").write_text("injected\n", encoding="utf-8")  # git 밖 내용
            self.assertEqual(integrity.classify(p, "coredir")["status"], "unprotected")

    def test_dir_core_symlink_descendant_unsupported(self):
        # critic 재감사 B1: 디렉터리 core의 tracked symlink 자식(mode 120000) → fail-closed
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outside:
            p = _init_repo(td)
            (p / "coredir").mkdir()
            (p / "coredir" / "base.md").write_text("base\n", encoding="utf-8")
            secret = Path(outside) / "secret.md"
            secret.write_text("secret\n", encoding="utf-8")
            (p / "coredir" / "linked.md").symlink_to(secret)
            _git(p, "add", "coredir/base.md", "coredir/linked.md")
            _git(p, "commit", "-q", "-m", "c")
            self.assertEqual(integrity.classify(p, "coredir")["status"], "unsupported")

    def test_gitlink_submodule_unsupported(self):
        # critic 재감사 B1: gitlink/submodule(mode 160000) → fail-closed
        with tempfile.TemporaryDirectory() as td:
            p = _init_repo(td)
            (p / "base.md").write_text("x\n", encoding="utf-8")
            _git(p, "add", "base.md")
            _git(p, "commit", "-q", "-m", "c")
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=p,
                                  capture_output=True).stdout.decode().strip()
            _git(p, "update-index", "--add", "--cacheinfo", f"160000,{head},submod")
            self.assertEqual(integrity.classify(p, "submod")["status"], "unsupported")


class TestFossilContinuityB2(unittest.TestCase):
    """critic B2: rev flapping·clock rollback이 fossil을 숨기면 안 됨."""

    def _log(self, td, *recs):
        p = _init_repo(td)
        obs = p / ".organum" / "observatory"
        obs.mkdir(parents=True, exist_ok=True)
        (obs / "integrity.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
        return p / ".organum"

    def _iso(self, days_ago):
        return (datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_rev_flap_preserves_fossil(self):
        with tempfile.TemporaryDirectory() as td:
            sd = self._log(td,
                {"ts": self._iso(10), "path": "C.md", "status": "unblessed", "rev": "r1"},
                {"ts": self._iso(0), "path": "C.md", "status": "unblessed", "rev": "r2"})  # rev flap
            d = {x["path"]: x for x in observatory.integrity_drift(sd)}["C.md"]
            self.assertTrue(d["fossil"])                     # episode 시작=10일 전, rev flap이 리셋 못 함
            self.assertGreater(d["drift_days"], 5)

    def test_clock_rollback_uses_append_order(self):
        with tempfile.TemporaryDirectory() as td:
            sd = self._log(td,
                {"ts": self._iso(0), "path": "C.md", "status": "blessed", "rev": "r1"},
                {"ts": self._iso(1), "path": "C.md", "status": "unblessed", "rev": "r2"})  # later append, older ts
            d = {x["path"]: x for x in observatory.integrity_drift(sd)}["C.md"]
            self.assertEqual(d["status"], "unblessed")       # append 마지막 = current (ts max 아님)

    def test_future_ts_age_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            sd = self._log(td,
                {"ts": self._iso(-5), "path": "C.md", "status": "unblessed", "rev": ""})  # 미래 ts
            d = {x["path"]: x for x in observatory.integrity_drift(sd)}["C.md"]
            self.assertTrue(d["age_unknown"])
            self.assertFalse(d["fossil"])                    # 조용히 false 아니라 unknown


class TestWriteBoundaryB4(unittest.TestCase):
    """critic B4: future-format·반쪽 init에 감시 로그를 쓰면 안 됨."""

    def test_future_format_no_write(self):
        with tempfile.TemporaryDirectory() as td:
            p = _init_repo(td)
            sd = p / ".organum"
            meta = json.loads((sd / "meta.json").read_text())
            meta["format_version"] = 99
            (sd / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
            self.assertEqual(observatory.record_integrity(sd), 0)
            self.assertFalse((sd / "observatory" / "integrity.jsonl").exists())

    def test_half_init_no_write(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            _git(p, "init", "-q")
            sd = p / ".organum"
            sd.mkdir()                                       # meta 없음(반쪽)
            self.assertEqual(observatory.record_integrity(sd), 0)
            self.assertFalse((sd / "observatory").exists())


class TestSchemaPathB5(unittest.TestCase):
    """critic B5: 손상 manifest/log가 scan을 죽이거나 경로가 project 밖으로 탈출하면 안 됨."""

    def test_manifest_wrong_shape_no_crash(self):
        with tempfile.TemporaryDirectory() as td:
            p = _init_repo(td)
            sd = p / ".organum"
            for bad in ("[]", '"str"', '{"core": 42}', '{"core": ["notdict"]}'):
                (sd / "core-manifest.json").write_text(bad, encoding="utf-8")
                integrity.core_paths(sd)                     # crash 안 함
                integrity.report(sd)

    def test_log_bad_lines_skipped_incomplete_surfaced(self):
        with tempfile.TemporaryDirectory() as td:
            p = _init_repo(td)
            sd = p / ".organum"
            obs = sd / "observatory"
            obs.mkdir(parents=True, exist_ok=True)
            (obs / "integrity.jsonl").write_text(
                '42\n"str"\n[1,2]\n' + json.dumps(
                    {"ts": "2026-07-20T00:00:00Z", "path": "C.md", "status": "unblessed"}) + "\n",
                encoding="utf-8")
            d = observatory.integrity_drift(sd)              # crash 안 함, 좋은 줄만
            self.assertTrue(any(x["path"] == "C.md" for x in d))
            self.assertTrue(observatory.integrity_incomplete(sd))   # 손상 표면화(critic B5)

    def test_log_wrong_type_status_no_cli_crash(self):
        # critic 재감사 B5: status가 truthy non-string(list)이면 드롭 + incomplete (CLI unhashable crash 방지)
        with tempfile.TemporaryDirectory() as td:
            p = _init_repo(td)
            sd = p / ".organum"
            obs = sd / "observatory"
            obs.mkdir(parents=True, exist_ok=True)
            (obs / "integrity.jsonl").write_text(json.dumps(
                {"ts": "2026-07-20T00:00:00Z", "path": "C.md", "status": ["unblessed"]}) + "\n",
                encoding="utf-8")
            self.assertEqual(observatory.integrity_drift(sd), [])   # list status 드롭
            self.assertTrue(observatory.integrity_incomplete(sd))

    def test_log_invalid_utf8_incomplete_no_crash(self):
        with tempfile.TemporaryDirectory() as td:
            p = _init_repo(td)
            sd = p / ".organum"
            obs = sd / "observatory"
            obs.mkdir(parents=True, exist_ok=True)
            (obs / "integrity.jsonl").write_bytes(b"\xff\xfe invalid utf8\n")
            self.assertEqual(observatory.integrity_drift(sd), [])   # decode 실패 crash 안 함
            self.assertTrue(observatory.integrity_incomplete(sd))

    def test_corrupt_manifest_surfaced(self):
        with tempfile.TemporaryDirectory() as td:
            p = _init_repo(td)
            sd = p / ".organum"
            for bad in ("[]", '"str"', "{", '{"core": 42}', '{"core":[{"path":9}]}'):
                (sd / "core-manifest.json").write_text(bad, encoding="utf-8")
                self.assertFalse(integrity.manifest_ok(sd))   # 조용히 True(정상) 아님

    def test_manifest_symlink_escape_excluded(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outside:
            p = _init_repo(td)
            sd = p / ".organum"
            secret = Path(outside) / "secret.md"
            secret.write_text("secret\n", encoding="utf-8")
            (p / "core-link").symlink_to(secret)             # project 밖으로 escape
            (sd / "core-manifest.json").write_text(
                json.dumps({"core": [{"path": "core-link"}]}), encoding="utf-8")
            self.assertNotIn("core-link", integrity.core_paths(sd))   # containment 차단
            self.assertFalse(integrity.manifest_ok(sd))              # 탈락을 손상으로 표면화(B5-a)

    def test_manifest_ok_shares_path_contract_with_core_paths(self):
        # critic 재감사3 B5-a: manifest_ok가 core_paths와 같은 path 계약(빈/절대/../탈출) — divergence 금지
        with tempfile.TemporaryDirectory() as td:
            p = _init_repo(td)
            sd = p / ".organum"
            for bad in ("", "   ", "/etc/hosts", "../../outside"):
                (sd / "core-manifest.json").write_text(
                    json.dumps({"core": [{"path": bad}]}), encoding="utf-8")
                self.assertFalse(integrity.manifest_ok(sd), bad)     # 선언 탈락 → 손상
                self.assertNotIn(bad.strip(), integrity.core_paths(sd))

    def test_unknown_status_dropped_and_incomplete(self):
        # critic 재감사3 B5-b: string이지만 미지 status("blesssed" 오타)도 drop + incomplete
        with tempfile.TemporaryDirectory() as td:
            p = _init_repo(td)
            sd = p / ".organum"
            obs = sd / "observatory"
            obs.mkdir(parents=True, exist_ok=True)
            (obs / "integrity.jsonl").write_text(json.dumps(
                {"ts": "2026-07-20T00:00:00Z", "path": "C.md", "status": "blesssed"}) + "\n",
                encoding="utf-8")
            self.assertEqual(observatory.integrity_drift(sd), [])     # 미지 status 드롭
            self.assertTrue(observatory.integrity_incomplete(sd))

    def test_inspector_surfaces_corrupt_manifest(self):
        # critic 재감사3 B5-c: inspector도 손상 manifest를 부분 결과로 명시(complete 주장 안 함)
        from organum import inspector
        with tempfile.TemporaryDirectory() as td:
            p = _init_repo(td)
            (p / "CONTRACT.md").write_text("v\n", encoding="utf-8")
            _git(p, "add", "CONTRACT.md")
            _git(p, "commit", "-q", "-m", "v")
            (p / ".organum" / "core-manifest.json").write_text("[]", encoding="utf-8")  # 손상
            incomplete = not integrity.manifest_ok(p / ".organum")
            self.assertTrue(incomplete)
            out = inspector.render([], p, 45, inspector.core_integrity(p, []), incomplete)
            self.assertIn("incomplete", out.lower())                  # 부분 결과 명시


class TestInspectorAudit(unittest.TestCase):
    """inspector 한 방 포렌식 audit — state 불요·아무 폴더나 + 재구성 세션 교차(reconstructive two-lens)."""

    def _bare_git(self, td):
        p = Path(td)
        _git(p, "init", "-q")
        _git(p, "config", "user.email", "t@t")
        _git(p, "config", "user.name", "tester")
        return p

    def test_core_integrity_no_organum_state(self):
        from organum import inspector
        with tempfile.TemporaryDirectory() as td:
            p = self._bare_git(td)   # .organum 없음 — state 불요
            (p / "CONTRACT.md").write_text("헌법\n", encoding="utf-8")
            _git(p, "add", "CONTRACT.md")
            _git(p, "commit", "-q", "-m", "v1")
            integ = inspector.core_integrity(p, [])
            paths = {r["path"]: r for r in integ}
            self.assertIn("CONTRACT.md", paths)                # AUTO_CORE로 감사
            self.assertEqual(paths["CONTRACT.md"]["status"], "blessed")
            self.assertIn("active_sessions", paths["CONTRACT.md"])
            out = inspector.render([], p, 45, integ)
            self.assertIn("core-integrity", out)
            self.assertIn("CONTRACT.md", out)

    def test_reconstructive_session_cross_ref(self):
        from organum import inspector
        with tempfile.TemporaryDirectory() as td:
            p = self._bare_git(td)
            (p / "CONTRACT.md").write_text("v\n", encoding="utf-8")
            _git(p, "add", "CONTRACT.md")
            _git(p, "commit", "-q", "-m", "v")
            date = inspector.core_integrity(p, [])[0]["last_commit"]["date"]  # bless 시각
            t = datetime.datetime.fromisoformat(date.replace("Z", "+00:00"))
            cells = [{"vendor": "claude", "model": "opus", "id": "x",
                      "first_ts": (t - datetime.timedelta(minutes=5)).isoformat(),
                      "last_ts": (t + datetime.timedelta(minutes=5)).isoformat()}]
            c = {r["path"]: r for r in inspector.core_integrity(p, cells)}["CONTRACT.md"]
            self.assertTrue(any(s["vendor"] == "claude" for s in c["active_sessions"]))


if __name__ == "__main__":
    unittest.main()
