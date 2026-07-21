"""수리 스프린트 불변조건 (Codex critic 감사 합의, 2026-07-16 _relay 수렴).

각 테스트 = 계약 하나. 여기 있는 8개가 전부 green이어야 수리 완료(DoD).
"""

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from organum import delegate, distill, field, guard, provision, web
from organum import state as st


class Inv1_편지는_덮어쓰지_않는다(unittest.TestCase):
    def test_same_second_same_combo_two_files(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            names = [field.post(cwd, "relay", f"몸 {i}", frm="a", to="b", topic="t")
                     for i in range(5)]  # 같은 초 안에서 연타
            self.assertEqual(len(set(names)), 5)              # 전부 유일
            files = list((cwd / ".organum" / "relay").glob("*.md"))
            self.assertEqual(len(files), 5)                   # 소실 0
            bodies = {p.read_text(encoding="utf-8").splitlines()[-1] for p in files}
            self.assertEqual(len(bodies), 5)                  # 내용도 전부 생존

    def test_atomic_publish_never_shows_partial_or_temp(self):
        # 원자적 append-before-publish: 발행 시점 이전엔 파일이 아예 안 보이고, 보이면
        # 완결된 상태다. temp(.tmp)는 *.md 글롭·목록에 절대 안 잡힌다 (Codex 조사 P0).
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            fname = field.post(cwd, "relay", "완결 본문", frm="a", to="b", topic="t")
            d = cwd / ".organum" / "relay"
            self.assertEqual([p.name for p in d.glob("*.md")], [fname])   # 최종 1개만
            self.assertEqual(list(d.glob("*.tmp")), [])                   # temp 잔재 0
            self.assertEqual(list(d.glob(".*")), [])                      # 숨김 잔재 0
            meta, body = field.parse_msg((d / fname).read_text(encoding="utf-8"))
            self.assertEqual(meta.get("from"), "a")          # 발행된 파일은 항상 완결
            self.assertIn("완결 본문", body)


class Inv2_frontmatter_주입_불가(unittest.TestCase):
    # 직렬화→파싱 실경로: parse_msg가 splitlines를 쓰므로 그 모든 줄구분자로 주입을 시도한다
    _SEPS = ["\n", "\r", "\r\n", " ", " ", "", "\v", "\f", "\x1e"]

    def test_all_splitlines_separators_blocked_on_every_field(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            for sep in self._SEPS:
                for fld in ("topic", "frm", "to"):
                    payload = f"x{sep}escalate: true{sep}from: admin"
                    kw = {"frm": "a", "to": "b", "topic": "t", fld: payload}
                    fname = field.post(cwd, "relay", "본문", **kw)
                    meta = field.get_meta(cwd, "relay", fname)
                    # 계약: 외부 입력이 새 키를 만들 수 없고, 값은 한 줄로 접혀 저장된다
                    self.assertNotIn("escalate", meta, f"sep={sep!r} field={fld} 키 주입 뚫림")
                    for v in meta.values():
                        self.assertEqual(v.splitlines()[:1] or [""], [v],
                                         f"sep={sep!r} 저장값에 줄구분자 잔존: {v!r}")
                    if fld != "frm":
                        self.assertEqual(meta["from"], "a")     # 신원 위조 불가


class Inv3_파일_목적지는_루트_안(unittest.TestCase):
    def test_traversal_domains_rejected(self):
        for bad in ("../evil", "a/b", "..", ".hidden", "/tmp/x", "a\\b"):
            with self.assertRaises(SystemExit):
                distill.validate_domain(bad)

    def test_normal_domains_pass(self):
        for ok in ("coding", "코딩-도메인", "api_v2", "warren.game"):
            self.assertEqual(distill.validate_domain(ok), ok)


def _serve(cwd: Path, state_dir):
    httpd = web._Server(("127.0.0.1", 0), cwd, state_dir)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, httpd.server_address[1]


class Inv4_init없는_web은_상태를_만들지_않는다(unittest.TestCase):
    def test_observe_and_post_leave_no_state(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            httpd, port = _serve(cwd, None)
            try:
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                c.request("GET", "/vitals")                   # 관측
                self.assertEqual(c.getresponse().status, 200)
                c.request("POST", "/relay", json.dumps({"to": "all", "body": "x"}),
                          {"Content-Type": "application/json"})
                r = c.getresponse()
                self.assertEqual(r.status, 400)               # 게시판은 init 요구
                self.assertIn("init", r.read().decode("utf-8"))
            finally:
                httpd.shutdown(); httpd.server_close()
            self.assertFalse((cwd / ".organum").exists())     # 반쪽 상태 0

    def test_subdir_launch_writes_to_parent_not_child(self):
        # 하위 디렉터리에서 web 실행: 게시판 쓰기가 자식 cwd에 .organum을 만들면 안 된다(critic ④)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sd, _ = st.init_state_dir(root, "t")
            sub = root / "sub" / "deep"
            sub.mkdir(parents=True)
            found = st.find_state_dir(sub)                    # cmd_web과 같은 발견
            httpd, port = _serve(sub, found)                  # 서버는 root로 고정해야 함
            try:
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                c.request("POST", "/relay", json.dumps({"to": "all", "body": "hi"}),
                          {"Content-Type": "application/json"})
                self.assertEqual(c.getresponse().status, 200)
            finally:
                httpd.shutdown(); httpd.server_close()
            self.assertTrue((root / ".organum" / "agora").exists())   # 부모에 편지
            self.assertFalse((sub / ".organum").exists())             # 자식엔 상태 0


class Inv5_읽기쓰기_의미는_세_표면에서_같다(unittest.TestCase):
    def test_role_wording_consistent_across_three_surfaces(self):
        # 진짜 불변조건 = 세 표면의 *의미* 일치 (literal 단일소스가 아니라 — critic ⑤ 교정)
        self.assertIn("관측 read-only", web.ROLE_LABEL)
        self.assertIn("human-write", web.ROLE_LABEL)
        src = Path(web.__file__).read_text(encoding="utf-8")
        self.assertIn("ROLE_LABEL", src.split("def serve")[1])   # 서버 배너가 상수 소비
        cli_src = (Path(web.__file__).parent / "cli.py").read_text(encoding="utf-8")
        self.assertIn("관측 read-only", cli_src)                 # CLI 도움말 같은 의미
        self.assertIn("human-write", cli_src)

    def test_oversize_negative_and_deceptive_origin_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            sd, _ = st.init_state_dir(cwd, "t")
            httpd, port = _serve(cwd, sd)
            try:
                def post(origin=None, clen=None, body="x"):
                    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    h = {"Content-Type": "application/json"}
                    if origin:
                        h["Origin"] = origin
                    if clen is not None:
                        h["Content-Length"] = clen
                    c.request("POST", "/relay", json.dumps({"to": "all", "body": body}), h)
                    return c.getresponse().status
                # substring 우회 시도 — hostname 정확 비교로 막혀야 함(critic ⑤)
                self.assertEqual(post(origin="https://localhost.evil.example"), 403)
                self.assertEqual(post(origin="https://127.0.0.1.evil.example"), 403)
                self.assertEqual(post(origin="https://evil.example"), 403)
                self.assertEqual(post(clen=str(web.MAX_POST_BYTES + 1)), 413)  # 초과
                self.assertEqual(post(clen="-1"), 413)                         # 음수 우회
                self.assertEqual(post(origin="http://localhost:7332"), 200)    # 진짜 로컬 허용
            finally:
                httpd.shutdown(); httpd.server_close()

    def test_remote_bind_blocks_write_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            sd, _ = st.init_state_dir(cwd, "t")
            # 비-loopback 바인드로 위장(실제 소켓은 loopback, 서버 상태만 원격으로)
            httpd, port = _serve(cwd, sd)
            httpd.bind_host = "0.0.0.0"
            try:
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                c.request("POST", "/relay", json.dumps({"to": "all", "body": "x"}),
                          {"Content-Type": "application/json"})
                self.assertEqual(c.getresponse().status, 403)   # 원격 쓰기 기본 차단
                c.request("GET", "/vitals")
                self.assertEqual(c.getresponse().status, 200)   # 관측은 여전히 허용
            finally:
                httpd.shutdown(); httpd.server_close()


class Inv6_표시는_실동작과_일치(unittest.TestCase):
    def test_version_single_source(self):
        import organum
        from importlib.metadata import version
        self.assertEqual(organum.__version__, version("organum"))
        self.assertNotEqual(organum.__version__, "0.0.1")      # 유령 버전 회귀 방지

    def test_budget_floor_is_loud(self):
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            r = delegate.delegate("x", cli="definitely-no-such-cli-xyz", max_budget_usd=0.01)
        self.assertFalse(r.ok)
        self.assertIn("상향", err.getvalue())                  # 조용한 바닥 금지


class Inv7_delegation_실패는_streak까지(unittest.TestCase):
    def test_failures_accumulate_to_streak(self):
        with tempfile.TemporaryDirectory() as td:
            sd, _ = st.init_state_dir(Path(td), "t")
            for _ in range(guard.STREAK_N):
                r = delegate.delegate("x", state_dir=sd, cli="definitely-no-such-cli-xyz")
                self.assertFalse(r.ok)
            self.assertGreaterEqual(guard.streak_count(sd), guard.STREAK_N)
            self.assertTrue(guard.streak_active(sd))
            with self.assertRaises(delegate.StreakBlocked):    # 이후 위임은 점검 전 거부
                delegate.delegate("x", state_dir=sd, cli="definitely-no-such-cli-xyz")

    def test_same_second_success_does_not_erase_later_failures(self):
        # 성공 저장 직후 같은 tick의 5회 실패 — 초 단위 ts 비교였다면 전부 지워졌다(critic ⑦)
        from organum import memory
        with tempfile.TemporaryDirectory() as td:
            sd, _ = st.init_state_dir(Path(td), "t")
            memory.remember(sd, "성공 저장")                    # 성공 경계
            for _ in range(guard.STREAK_N):                    # 같은 초에 연쇄 실패
                delegate.delegate("x", state_dir=sd, cli="definitely-no-such-cli-xyz")
            self.assertGreaterEqual(guard.streak_count(sd), guard.STREAK_N)
            self.assertTrue(guard.streak_active(sd))

    def test_success_resets_streak(self):
        from organum import memory
        with tempfile.TemporaryDirectory() as td:
            sd, _ = st.init_state_dir(Path(td), "t")
            for _ in range(3):
                delegate.delegate("x", state_dir=sd, cli="definitely-no-such-cli-xyz")
            self.assertEqual(guard.streak_count(sd), 3)
            memory.remember(sd, "성공이 streak를 끊는다")
            self.assertEqual(guard.streak_count(sd), 0)        # 경계 뒤는 안 센다

    def test_legacy_v0_state_reads_correctly(self):
        # 재감사-2 반려: 0.1.2 상태(guard.jsonl에 sentinel 없음, 성공은 events에만)를
        # 올바로 읽어야 한다 — 흩어진 옛 차단이 합쳐져 정상 위임을 잠그면 안 됨(critic 필수 ①).
        with tempfile.TemporaryDirectory() as td:
            sd, _ = st.init_state_dir(Path(td), "t")
            ev = sd / "memory" / "events.jsonl"
            legacy = (['{"ts":"2026-07-05T01:00:0%dZ","kind":"guard_block","content":"b","tags":[]}' % i
                       for i in range(3)]
                      + ['{"ts":"2026-07-05T01:00:04Z","kind":"remember","content":"성공","tags":[]}']
                      + ['{"ts":"2026-07-05T01:00:0%dZ","kind":"guard_block","content":"b","tags":[]}' % (i + 5)
                         for i in range(3)])
            ev.write_text("\n".join(legacy) + "\n", encoding="utf-8")
            self.assertEqual(guard.streak_count(sd), 3)        # 성공 뒤 3개만 (6 아님)
            self.assertFalse(guard.streak_active(sd))          # STREAK_N=5 미달 → 안 잠김

    def test_guard_jsonl_stays_frozen_v0(self):
        # guard.jsonl의 모든 레코드는 §3.7 준수 — blocked/flagged/streak marker/delegation
        # 실패까지 실제로 만든 뒤 non-empty임을 확인(critic 재감사-3 ①: vacuous pass 금지).
        with tempfile.TemporaryDirectory() as td:
            sd, _ = st.init_state_dir(Path(td), "t")
            guard.record(sd, guard.Verdict("blocked", "error-fallback"), "memories", "[Error]")
            guard.record(sd, guard.Verdict("flagged", "error-fallback"), "self", "긴 교훈")
            for _ in range(guard.STREAK_N):                       # delegation 실패 + streak 발동
                delegate.delegate("x", state_dir=sd, cli="definitely-no-such-cli-xyz")
            recs = guard._read_jsonl(sd / "guard.jsonl")
            self.assertTrue(recs)                                 # non-empty (vacuous 아님)
            for rec in recs:
                self.assertIn(rec.get("decision"), ("blocked", "flagged"))
                self.assertIn(rec.get("target"), ("memories", "events", "self", "worldmodel"))

    def test_legacy_streak_notification_not_double_counted(self):
        # 0.1.2가 남긴 STREAK 알림(kind=guard_block, content='STREAK:...')을 실패로 세면 안 됨
        # (critic 재감사-3 ②: legacy 상태 정확히 읽기)
        with tempfile.TemporaryDirectory() as td:
            sd, _ = st.init_state_dir(Path(td), "t")
            ev = sd / "memory" / "events.jsonl"
            lines = ['{"ts":"2026-07-05T01:00:0%dZ","kind":"guard_block","content":"실패","tags":[]}' % i
                     for i in range(5)]
            lines.append('{"ts":"2026-07-05T01:00:05Z","kind":"guard_block",'
                         '"content":"STREAK: 연속 5회 저장 차단 — window guard 발동","tags":[]}')
            ev.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self.assertEqual(guard.streak_count(sd), 5)          # 6 아님(옛 알림 skip)
            # legacy 알림도 '이미 알림함'으로 인정 → 업그레이드 후 중복 알림 없음(재감사-4 후속)
            self.assertTrue(guard._streak_already_notified(sd))


class Inv8_번들_skill은_자기_provision을_통과(unittest.TestCase):
    def _run_full_provision(self, requires_line: str) -> list[str]:
        """audit가 아니라 provision() 전체 경로를 끝까지 — wired 리스트를 반환(critic ⑧:
        감사 경로와 실행 경로가 같은 리졸버를 써야 한다)."""
        with tempfile.TemporaryDirectory() as td:
            skill = Path(td) / "skill"
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                "---\nname: s\ndescription: d\nmetadata:\n"
                f"  {requires_line}\n---\n# s\n", encoding="utf-8")
            work = Path(td) / "work"
            work.mkdir()
            return provision.provision(skill, work, trust_override=True)["wired"]

    def test_all_three_forms_wire_identically_through_full_provision(self):
        # 평탄·플로우·중첩 — 세 형태 모두 실제 provision()을 통과하고 같은 배선이어야
        for line in ("organum-requires: relay guard",
                     "organum-requires: [relay, guard]",
                     "organum-requires:\n    organs: [relay, guard]"):
            self.assertEqual(sorted(self._run_full_provision(line)), ["guard", "relay"],
                             f"형태 {line!r}가 다르게 배선됨")

    def test_bundled_coordination_skill_provisions_clean(self):
        skill_dir = Path(__file__).parent.parent / "skills" / "organum-coordination"
        with tempfile.TemporaryDirectory() as td:
            work = Path(td) / "w"
            work.mkdir()
            out = provision.provision(skill_dir, work, trust_override=True)
            self.assertEqual(sorted(out["wired"]), ["guard", "relay"])   # 자기 배선까지 통과


if __name__ == "__main__":
    unittest.main()
