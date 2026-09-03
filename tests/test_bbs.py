"""organum bbs 제품 계약 시험 (0.5.0) — claimed core + CLI 결정성 + voice seam.

experiments/bbs-v0의 계약 시험이 검증한 성질을 제품 모듈 `organum.bbs`에 대해
다시 고정한다. 제품 core는 experiments·voice에 의존하지 않는다 — verified 등급은
주입된 verifier로만 열리는 seam이다(Orin 037)."""

import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
from organum import bbs  # noqa: E402
from organum import bbs_cli  # noqa: E402


def _m(lab, i):
    return {"lab": lab, "id": i}


def _w_round():
    care = _m("lab:ludex", "Cody")
    ev = [{"kind": "board.created", "board": "w", "caretaker": care,
           "at": "t01", "signer_lab": "lab:ludex"},
          {"kind": "board.metadata", "board": "w", "at": "t02",
           "signer_lab": "lab:ludex", "name": "주간 교류회"}]
    for lab, i in [("lab:ludex", "Cody"), ("lab:organum", "Cody"),
                   ("lab:ray", "Ray")]:
        ev.append({"kind": "board.member.admitted", "board": "w",
                   "member": _m(lab, i), "acting_agent": care,
                   "signer_lab": "lab:ludex", "at": "t03"})
    ev += [{"kind": "board.post", "board": "w", "post_id": "p1",
            "author": _m("lab:ludex", "Cody"), "signer_lab": "lab:ludex",
            "reply_to": None, "at": "t10"},
           {"kind": "board.post", "board": "w", "post_id": "p2",
            "author": _m("lab:organum", "Cody"), "signer_lab": "lab:organum",
            "reply_to": "p1", "at": "t11"}]
    return ev


def _profile(lab, i, at, **extra):
    p = {"kind": "creature", "display_name": i,
         "contact": {"lab_id": lab, "to_id": i, "to_epoch": 1}}
    p.update(extra)
    return {"kind": "profile.announced", "subject": _m(lab, i),
            "author": _m(lab, i), "signer_lab": lab, "profile": p, "at": at}


class TestBoard(unittest.TestCase):
    def test_투영과_스레드(self):
        st = bbs.project_board(_w_round())
        self.assertEqual(st["status"], "active")
        self.assertEqual(len(st["members"]), 3)
        self.assertEqual([p["post_id"] for p in st["posts"]], ["p1", "p2"])
        self.assertEqual(bbs.threads(st), {"p1": ["p2"]})

    def test_비멤버_게시_거부되고_원장에_남는다(self):
        ev = _w_round() + [{"kind": "board.post", "board": "w", "post_id": "px",
                            "author": _m("lab:x", "S"), "signer_lab": "lab:x",
                            "reply_to": None, "at": "t20"}]
        st = bbs.project_board(ev)
        self.assertNotIn("px", [p["post_id"] for p in st["posts"]])
        self.assertTrue(st["rejected"])

    def test_좌표_밖_게시는_재운반_거부(self):
        ev = _w_round() + [{"kind": "board.post", "board": "other",
                            "post_id": "py", "author": _m("lab:ray", "Ray"),
                            "signer_lab": "lab:ray", "reply_to": None,
                            "at": "t20"}]
        st = bbs.project_board(ev)
        self.assertTrue(any("재운반 금지" in r["why"][0] for r in st["rejected"]))

    def test_notice는_안_지우고_채택은_로컬(self):
        ev = _w_round() + [{"kind": "board.notice", "board": "w",
                            "notice_id": "n1", "target_post": "p2",
                            "by": _m("lab:ludex", "Cody"),
                            "signer_lab": "lab:ludex", "at": "t21"}]
        st = bbs.project_board(ev)
        self.assertEqual(len(st["posts"]), 2)
        self.assertEqual([p["post_id"] for p in bbs.adopted_view(st, {"n1"})],
                         ["p1"])

    def test_digest_반가설_같은_스트림_파생(self):
        ev = _w_round()
        st = bbs.project_board(ev)
        dg = bbs.project_digest(ev)
        self.assertEqual(dg["post_ids"], [p["post_id"] for p in st["posts"]])


class TestDirectory(unittest.TestCase):
    def test_컴파일과_행_스키마(self):
        rows = bbs.compile_people_directory(
            [_profile("lab:ludex", "Aria", "t1", village="나루")],
            compiled_at="t99")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(set(r), {"subject", "profile", "vouch",
                                  "subject_claimed", "announced_at",
                                  "profile_event_last_seen", "compiled_at"})
        self.assertNotIn("subject_authority_verified", r)   # verifier 없음
        self.assertNotIn("active", r)

    def test_화이트리스트_밖_필드_거부(self):
        ev = _profile("lab:x", "A", "t1", memory_dump="...")
        self.assertTrue(any("화이트리스트 밖" in p
                            for p in bbs.validate_profile_event(ev)))

    def test_public_projection_contact_제거(self):
        rows = bbs.compile_people_directory([_profile("lab:x", "A", "t1")],
                                            compiled_at="t9")
        self.assertIn("contact", rows[0]["profile"])
        pub = bbs.public_projection(rows)
        self.assertNotIn("contact", pub[0]["profile"])


class TestVoiceSeam(unittest.TestCase):
    """제품 core는 voice에 의존하지 않는다 — verifier 주입 seam만 있다."""

    def _voiced(self):
        ev = _profile("lab:organum", "Cody", "t1")
        ev["voice"] = {"sig": "aa", "attestation": {}}      # 모양만
        return ev

    def test_verifier_없으면_unverifiable_보존(self):
        rows = bbs.compile_people_directory([self._voiced()], compiled_at="t9")
        self.assertEqual(rows[0].get("voice_evidence"), "unverifiable")
        self.assertNotIn("subject_authority_verified", rows[0])

    def test_주입된_verifier가_verified를_연다(self):
        rows = bbs.compile_people_directory(
            [self._voiced()], compiled_at="t9",
            voice_verifier=lambda ev: "verified")
        self.assertTrue(rows[0]["subject_authority_verified"])
        self.assertEqual(rows[0]["voice_evidence"], "verified")

    def test_verifier_rejected는_행을_거부(self):
        st = bbs.project_profiles([self._voiced()],
                                  voice_verifier=lambda ev: "rejected")
        self.assertEqual(st["rows"], {})
        self.assertTrue(st["rejected"])

    def test_voice_없는_프로필은_evidence_필드_부재(self):
        rows = bbs.compile_people_directory([_profile("lab:x", "A", "t1")],
                                            compiled_at="t9")
        self.assertNotIn("voice_evidence", rows[0])


class TestCLI(unittest.TestCase):
    """CLI와 라이브러리는 같은 core를 호출한다 — 결정적 JSON·malformed nonzero."""

    def _run(self, argv, stream):
        out, err = io.StringIO(), io.StringIO()
        f = Path(self.tmp) / "ev.json"
        f.write_text(json.dumps(stream, ensure_ascii=False), encoding="utf-8")
        with redirect_stdout(out), redirect_stderr(err):
            rc = bbs_cli.main(argv + [str(f)])
        return rc, out.getvalue(), err.getvalue()

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()

    def test_board_결정적_출력(self):
        rc, out, _ = self._run(["board"], _w_round())
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["threads"], {"p1": ["p2"]})
        rc2, out2, _ = self._run(["board"], _w_round())
        self.assertEqual(out, out2)                          # 결정적

    def test_directory_claimed만_verified_미발행(self):
        v = _profile("lab:organum", "Cody", "t1")
        v["voice"] = {"sig": "aa", "attestation": {}}
        rc, out, _ = self._run(["directory"], [v])
        row = json.loads(out)[0]
        self.assertNotIn("subject_authority_verified", row)  # CLI 기본=claimed
        self.assertEqual(row.get("voice_evidence"), "unverifiable")

    def test_malformed_nonzero_exit(self):
        f = Path(self.tmp) / "bad.json"
        f.write_text("not json", encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = bbs_cli.main(["board", str(f)])
        self.assertEqual(rc, 1)
        self.assertIn("error", err.getvalue())

    def test_배열_아니면_거부(self):
        f = Path(self.tmp) / "obj.json"
        f.write_text('{"not":"array"}', encoding="utf-8")
        err = io.StringIO()
        with redirect_stderr(err):
            rc = bbs_cli.main(["board", str(f)])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
