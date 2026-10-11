"""서명하는 쪽의 차례(0.10.0) — 승인된 초안 하나가 편지 하나가 된다.

계약:
1. 승인은 사람이 본 바이트에 묶인다. 승인 화면이 보여야 하는 것은 받은 그대로의 글이다.
2. 서명 직전에 함의 바이트를 다시 잰다. 다르면 서명하지 않고, 번호를 쓰지 않는다.
3. 같은 초안은 같은 편지로 모인다. 어느 걸음에서 죽어도 다시 하면 봉투와 번호가 하나다.
4. 번호를 쓴 편지가 문에 가기 전에는 다음 편지에 번호를 주지 않는다.
5. `checkpoint`가 올리기보다 앞이다. `sent`는 문에서 되읽은 때다.
6. 문과 발신함이 어긋나면 멈춘다. 다시 서명하지도 다시 번호를 주지도 않는다.
7. 서명하는 것은 승인된 `message.posted`뿐이다. 서명 키는 원장에 결속된 것이어야 한다.
11. 원장에 선 편지가 발신함보다 먼저다. 어느 초안을 부르든 그 편지부터 발신함에 놓는다.
8. 함에서 읽은 초안을 믿지 않는다. 창구가 본 것을 서명하는 쪽이 다시 본다.
9. 원장을 열기 전에는 서명하지 않는다. 빈 자리면 상태 칸에서 되살리고 문과 맞댄다.
10. 승인됐고 결과가 없는 초안은 찾아 잇는다.

실제 키와 실제 드롭은 쓰지 않는다 — 씨앗은 BIP340 시험 벡터의 공개된 값이고 드롭은 이 기기의 임시 서버다.
"""

import hashlib
import json
import sys
import threading
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
from organum import hub_draft as hdr        # noqa: E402
from organum import hub_drop as hd          # noqa: E402
from organum import hub_envelope as he      # noqa: E402
from organum import hub_ops as ho           # noqa: E402
from organum import hub_send as hsd         # noqa: E402
from organum import hub_state as hs         # noqa: E402
from organum import schnorr_pure as sp      # noqa: E402

SEED1 = (3).to_bytes(32, "big")             # TESTONLY — 복구 키 노릇(k1)
PUB1 = sp.public_key(SEED1).hex()
SEED3 = (5).to_bytes(32, "big")             # TESTONLY — 서명 서비스의 전용 키 노릇(k3)
PUB3 = sp.public_key(SEED3).hex()
SIGNER = "lab:testonly"
TO = "lab:receiver"
DOOR = "hub-ops/from-testonly"
TOKEN = "tok-test"
RECIPIENTS = {TO: 1}
T0 = 1791504000.0                           # 2026-10-09T00:00:00Z


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def _hub(root: Path):
    h, out = root / "hub", root / "out"
    h.mkdir(parents=True)
    out.mkdir()
    cfg = {"source_domain": "lab:testonly/hub", "machine_id": "TESTONLY-SIGNER",
           "claims": {"claims": {}}, "claims_sha256": he.canonical_sha({"claims": {}}),
           "keys": [{"pubkey": PUB1, "signer_id": SIGNER, "key_id": "k1", "key_epoch": 1},
                    {"pubkey": PUB3, "signer_id": SIGNER, "key_id": "k3", "key_epoch": 1}]}
    (h / "hub.json").write_text(json.dumps(cfg), encoding="utf-8")
    (h / "events.jsonl").write_bytes(b"")
    return h, out


def _serve(tmp_path, *, marks=False):
    tmp_path.mkdir(parents=True, exist_ok=True)
    tok = tmp_path / "tokens.txt"
    tok.write_text(f"{TOKEN}  id=testonly\n", encoding="utf-8")
    mdir = tmp_path / "srv-marks" if marks else None
    if mdir:
        mdir.mkdir()
    srv = hd.make_server(tmp_path / "srv-drops", tok, bind="127.0.0.1", port=0,
                         rate_limit_per_minute=0, state_dir=tmp_path / "srv-state", marks_dir=mdir)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", mdir


class _World:
    """초안 창구, 함, 서명하는 쪽의 자리를 한데 둔다."""

    def __init__(self, tmp_path, *, marks=False):
        self.tmp = tmp_path
        self.srv, self.url, self.mdir = _serve(tmp_path, marks=marks)
        self.hub, self.out = _hub(tmp_path / "signer")
        self.store = hdr.MemoryStore()
        self.t = T0
        self.front = hdr.DraftFront(self.store, {"jdot": {"sender": SIGNER, "to": {TO: 1}}},
                                    now=lambda: self.t)
        self.marks = marks

    def cfg(self, **kw):
        base = dict(hub_dir=self.hub, outbox_dir=self.out, door=DOOR, seed=SEED3, signer=SIGNER,
                    key_id="k3", key_epoch=1, drop_url=self.url, token=TOKEN, now=lambda: self.t,
                    recipients=RECIPIENTS,
                    wait=0, mirror_wait=0, sleep=lambda s: None, received_only=not self.marks)
        base.update(kw)
        return base

    def draft(self, body="hello", **kw):
        st, out = self.front.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                     "params": {"name": "mail_draft",
                                                "arguments": {"to_lab": TO, "to_id": "R", "body": body, **kw}}},
                                    "jdot")
        r = json.loads(out["result"]["content"][0]["text"])
        assert st == 200 and "draft_id" in r, r
        return r["draft_id"]

    def review(self, did, **kw):
        return hsd.review(self.store, did, signer=SIGNER, recipients=RECIPIENTS, now=lambda: self.t, **kw)

    def decide(self, did, decision, **kw):
        kw.setdefault("now", lambda: self.t)
        return hsd.decide(self.store, did, decision, approver="jj", signer=SIGNER,
                          recipients=RECIPIENTS, **kw)

    def approve(self, did):
        return self.decide(did, "approved", body_sha256=self.review(did)["body_sha256"])

    def approved(self, body="hello", **kw):
        did = self.draft(body, **kw)
        self.approve(did)
        return did

    def send(self, did, **kw):
        return hsd.send_approved(self.store, did, **self.cfg(**kw))

    def door(self):
        return {e["n"]: e for e in hs.fetch_index(f"{self.url}/v0/{DOOR}", TOKEN)}

    def events(self):
        return (self.hub / "events.jsonl").read_text(encoding="utf-8").splitlines()

    def quads(self):
        return sorted(f.name for f in self.out.iterdir())

    def outbox(self, did):
        st, out = self.front.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                     "params": {"name": "mail_outbox", "arguments": {"draft_id": did}}}, "jdot")
        return json.loads(out["result"]["content"][0]["text"])["drafts"][0]

    def supervise(self):
        """표지를 쓰는 배치의 감독기 노릇: 바깥에 옮긴 세대와 편지에 표지를 놓는다."""
        d = self.mdir / "state" / "testonly"
        d.mkdir(parents=True, exist_ok=True)
        slot = self.tmp / "srv-state" / "testonly"
        for f in (slot.iterdir() if slot.is_dir() else ()):
            (d / f.name.split(".")[0]).write_bytes(b"")
        q = self.mdir / "quads" / "hub-ops" / "from-testonly"
        q.mkdir(parents=True, exist_ok=True)
        for f in (self.tmp / "srv-drops" / "hub-ops" / "from-testonly").glob("*-envelope.json"):
            (q / f.name.split("-")[0]).write_bytes(b"")


# ── 한 통이 가는 길 ──────────────────────────────────────────────────────────

def test_an_approved_draft_becomes_one_letter_on_the_door(tmp_path):
    w = _World(tmp_path)
    text = "첫 줄\n\n**굵게** \"따옴표\""
    did = w.approved(text)
    assert w.outbox(did)["status"] == "approved" and w.events() == []     # 승인은 아직 편지가 아니다
    w.t = T0 + 60
    r = w.send(did)
    assert r["status"] == "sent" and r["draft_id"] == did and r["n"] == "001" and r["door"] == DOOR
    assert r["sent_at"] == "2026-10-09T00:01:00Z"
    raw = (w.out / "001-envelope.json").read_bytes()
    env = json.loads(raw)
    # 봉투가 대리 서명을 드러낸다: 전용 키의 이름, 서명한 기계, 초안으로 되짚는 손잡이
    assert env["signer"] == {"id": SIGNER, "key_id": "k3", "key_epoch": 1}
    assert env["provenance"]["machine"] == "TESTONLY-SIGNER"
    assert env["payload"]["body_locator"] == f"draft:{did}"
    assert env["event_kind"] == "message.posted"
    assert env["payload"]["target"] == {"lab_id": TO, "to_id": "R", "to_epoch": 1}
    assert env["payload"]["body_sha256"] == _sha(text.encode("utf-8"))
    assert (w.out / "001-body.md").read_bytes() == text.encode("utf-8")
    sig = bytes.fromhex((w.out / "001-sig.txt").read_text(encoding="utf-8").strip())
    assert sp.verify(sig, hashlib.sha256(raw).digest(), bytes.fromhex(PUB3))
    assert not sp.verify(sig, hashlib.sha256(raw).digest(), bytes.fromhex(PUB1))
    assert r["event_id"] == he.event_id_of(raw) and r["envelope_sha256"] == _sha(raw)
    # 문에 그 바이트가 있다. 원장에는 사건이 하나다
    assert w.door()["001"]["envelope_sha256"] == _sha(raw) and len(w.events()) == 1
    # 받는 쪽의 검증기가 그대로 받는다
    v = ho.verify_quad(env, sig.hex(), pubkey=PUB3)
    assert v["valid_signature"] is True
    # 모델이 보는 보낼 함: 이제야 「갔다」
    o = w.outbox(did)
    assert o["status"] == "sent" and (o["door"], o["n"], o["event_id"]) == (DOOR, "001", r["event_id"])
    w.srv.shutdown()


def test_sending_again_makes_no_second_letter(tmp_path):
    w = _World(tmp_path)
    did = w.approved("once")
    first = w.send(did)
    assert w.send(did) == first and len(w.events()) == 1
    # 결과의 기록을 잃었다(쓰지 못하고 죽었다). 다시 오면 같은 편지의 결과가 다시 적힌다
    w.store.drafts[did]["result"] = None
    w.t = T0 + 600
    again = w.send(did)
    assert (again["n"], again["event_id"]) == (first["n"], first["event_id"])
    assert len(w.events()) == 1 and w.quads() == ["001-body.md", "001-envelope.json", "001-sig.txt"]
    assert list(w.door()) == ["001"]
    # 같은 글을 같은 이에게 한 번 더 보내려면 다른 초안이어야 한다. 그것은 다른 편지다
    second = w.send(w.approved("once", client_ref="again"))
    assert second["n"] == "002" and second["event_id"] != first["event_id"] and len(w.events()) == 2
    w.srv.shutdown()


def test_each_failure_converges_to_one_envelope_and_one_number(tmp_path, monkeypatch):
    """Orin 061: 재시작과 응답 유실에서 최초의 사건과 번호로 모이는가."""
    w = _World(tmp_path)
    did = w.approved("survives")
    real_export, real_push = ho.export_quad, hd.push_quad

    # ① 원장에 넣은 뒤, 발신함에 놓기 전에 죽었다
    def dies(*a, **k):
        raise RuntimeError("crashed before export")
    monkeypatch.setattr(ho, "export_quad", dies)
    with pytest.raises(RuntimeError):
        w.send(did)
    assert len(w.events()) == 1 and w.quads() == []
    first_event = he.event_id_of(json.loads(w.events()[0])["raw"].encode("utf-8"))

    # ② 발신함에 쓰다가 죽었다. 봉투 없는 조각이 남는다
    def half(d, out, **k):
        (Path(out) / "001-sig.txt").write_text("deadbeef\n", encoding="utf-8")
        (Path(out) / "001-body.md").write_bytes(b"half")
        raise RuntimeError("crashed while exporting")
    monkeypatch.setattr(ho, "export_quad", half)
    w.t = T0 + 30                                                    # 다시 올 때는 시각이 다르다
    with pytest.raises(RuntimeError):
        w.send(did)
    assert len(w.events()) == 1 and w.quads() == ["001-body.md", "001-sig.txt"]
    monkeypatch.setattr(ho, "export_quad", real_export)

    # ③ 드롭이 편지를 받지 못했다
    def down(*a, **k):
        raise hd.DropError(503, "sleeping")
    monkeypatch.setattr(hd, "push_quad", down)
    r = w.send(did)
    assert r["status"] == "approved" and r["stage"] == "push" and w.door() == {}
    assert w.quads() == ["001-body.md", "001-envelope.json", "001-sig.txt"]      # 조각을 치우고 같은 번호에 놓았다
    assert (w.out / "001-body.md").read_bytes() == b"survives"
    assert w.outbox(did)["status"] == "approved"

    # ④ 드롭이 받았는데 답을 잃었다
    def lost(*a, **k):
        real_push(*a, **k)
        raise OSError("connection reset")
    monkeypatch.setattr(hd, "push_quad", lost)
    r = w.send(did)
    assert r["status"] == "approved" and r["stage"] == "push" and list(w.door()) == ["001"]
    monkeypatch.setattr(hd, "push_quad", real_push)

    # ⑤ 다시 오면 끝난다. 사건도 번호도 처음의 것 하나다
    r = w.send(did)
    assert r["status"] == "sent" and r["n"] == "001" and r["event_id"] == first_event
    assert len(w.events()) == 1 and list(w.door()) == ["001"]
    assert json.loads((w.out / "001-envelope.json").read_bytes())["created_at"] == "2026-10-09T00:00:00Z"
    w.srv.shutdown()


def test_a_signed_letter_reaches_the_door_before_the_next_gets_a_number(tmp_path, monkeypatch):
    w = _World(tmp_path)
    a, b = w.approved("first"), w.approved("second")
    real_push = hd.push_quad

    def down(*a_, **k):
        raise hd.DropError(502, "bad gateway")
    monkeypatch.setattr(hd, "push_quad", down)
    assert w.send(a)["stage"] == "push" and len(w.events()) == 1
    # 첫 편지가 문에 없다. 둘째에는 봉투도 번호도 주지 않는다
    r = w.send(b)
    assert r["status"] == "approved" and r["draft_id"] == b
    assert len(w.events()) == 1 and w.quads() == ["001-body.md", "001-envelope.json", "001-sig.txt"]
    # 드롭이 돌아오면 첫 편지부터 간다. 문에 빈 번호가 없다
    monkeypatch.setattr(hd, "push_quad", real_push)
    rb = w.send(b)
    assert rb["status"] == "sent" and rb["n"] == "002"
    assert w.store.get(a)["result"]["n"] == "001" and list(w.door()) == ["001", "002"]
    # 드롭이 답하지 못하면 아무것도 서명하지 않는다
    c = w.approved("third")
    monkeypatch.setattr(hs, "_http", lambda *a_, **k: (_ for _ in ()).throw(OSError("timed out")))
    r = w.send(c)
    assert r["status"] == "approved" and r["stage"] == "door" and len(w.events()) == 2
    # drain은 새로 서명하지 않고 남은 것만 민다
    monkeypatch.undo()
    assert hsd.drain(w.store, **w.cfg())["clear"] is True and len(w.events()) == 2
    # 드롭이 편지를 거절하면(잠든 것이 아니라) 멈춘다. 다시 해 볼 일이 아니다
    def refused(*a_, **k):
        raise hd.DropError(403, "this token may not write that door")
    monkeypatch.setattr(hd, "push_quad", refused)
    with pytest.raises(hsd.SendStopped, match="refused"):
        w.send(c)
    assert w.store.get(c)["result"] is None and len(w.events()) == 3   # 서명은 됐고 문에는 없다
    monkeypatch.undo()
    assert w.send(c)["n"] == "003"
    w.srv.shutdown()


# ── 승인 ─────────────────────────────────────────────────────────────────────

def test_review_shows_the_received_bytes_plainly():
    text = "보이는 글\n\t탭\n**md** <b>html</b>​숨은 글자‮뒤집기﻿\r\x00\U000e0041끝"
    s = hsd.shown(text.encode("utf-8"))
    assert s["text"] == ("보이는 글\n\t탭\n**md** <b>html</b>⟦U+200B⟧숨은 글자⟦U+202E⟧뒤집기⟦U+FEFF⟧"
                         "⟦U+000D⟧⟦U+0000⟧⟦U+E0041⟧끝")
    assert [r["codepoint"] for r in s["revealed"]] == ["U+200B", "U+202E", "U+FEFF", "U+000D", "U+0000", "U+E0041"]
    assert s["revealed"][1]["name"] == "RIGHT-TO-LEFT OVERRIDE" and s["revealed"][0]["at"] == text.index("​")
    assert hsd.shown("평범한 글\n둘째 줄".encode("utf-8")) == {"text": "평범한 글\n둘째 줄", "revealed": []}
    store = hdr.MemoryStore()
    rec = hdr.draft_record(caller="jdot", sender=SIGNER, to_lab=TO, to_id="R", to_epoch=1,
                           media_type="text/markdown", client_ref="", body=text.encode("utf-8"),
                           now=T0, ttl=hdr.DRAFT_TTL_SECONDS)
    store.put_draft(rec, text.encode("utf-8"))
    review = lambda: hsd.review(store, rec["draft_id"], signer=SIGNER, recipients=RECIPIENTS,   # noqa: E731
                                now=lambda: T0)
    rv = review()
    assert rv["status"] == "pending_approval" and rv["to"] == {"lab_id": TO, "to_id": "R", "to_epoch": 1}
    assert rv["body_sha256"] == _sha(text.encode("utf-8")) and rv["body_size"] == len(text.encode("utf-8"))
    assert rv["text"] == s["text"] and len(rv["revealed"]) == 6 and rv["sender"] == SIGNER
    # 함의 바이트가 기록과 다르면 보여 주지 않는다
    store.bodies[rec["draft_id"]] = b"swapped"
    with pytest.raises(hsd.Refused):
        review()
    store.bodies[rec["draft_id"]] = b"\xff\xfe"
    with pytest.raises(hsd.Refused):
        review()
    with pytest.raises(hsd.Refused):
        hsd.shown(b"\xff\xfe")


def test_approval_is_bound_to_the_bytes_that_were_reviewed(tmp_path):
    w = _World(tmp_path)
    did = w.draft("the reviewed text")
    sha = _sha(b"the reviewed text")
    # 본 글의 지문이 아니면 적지 않는다. 지문 없이 승인하지도 못한다
    for wrong in (_sha(b"something else"), None):
        with pytest.raises(hsd.Refused):
            w.decide(did, "approved", body_sha256=wrong)
    assert w.store.get(did)["decision"] is None
    # 승인하지 않은 초안은 보내지 못한다
    with pytest.raises(hsd.SendStopped):
        w.send(did)
    d = w.decide(did, "approved", body_sha256=sha)
    assert d["decision"] == "approved" and d["approver"] == "jj" and d["body_sha256"] == sha
    assert d["to"] == {"lab_id": TO, "to_id": "R", "to_epoch": 1} and d["expires_at"] == "2026-10-10T00:00:00Z"
    assert w.decide(did, "approved", body_sha256=sha) == d            # 같은 결정은 그대로
    with pytest.raises(hsd.Refused):                                  # 첫 결정을 덮어 적지 못한다
        w.decide(did, "rejected")
    with pytest.raises(ValueError):
        w.decide(did, "maybe", body_sha256=sha)
    with pytest.raises(ValueError):
        hsd.decide(w.store, did, "approved", approver="", signer=SIGNER, recipients=RECIPIENTS,
                   body_sha256=sha)

    # 두 사람이 동시에 다른 결정을 눌렀다. 함에 먼저 놓인 것이 남고, 진 쪽은 자기 결정이 적힌 줄 알지 않는다
    class Raced(hdr.MemoryStore):
        def put_decision(self, record):
            super().put_decision(dict(record, decision="rejected"))
            return super().put_decision(record)
    raced = Raced()
    rec = hdr.draft_record(caller="jdot", sender=SIGNER, to_lab=TO, to_id="R", to_epoch=1,
                           media_type="text/plain", client_ref="", body=b"raced", now=T0,
                           ttl=hdr.DRAFT_TTL_SECONDS)
    raced.put_draft(rec, b"raced")
    with pytest.raises(hsd.Refused):
        hsd.decide(raced, rec["draft_id"], "approved", approver="jj", signer=SIGNER,
                   recipients=RECIPIENTS, body_sha256=_sha(b"raced"), now=lambda: T0)
    assert raced.get(rec["draft_id"])["decision"]["decision"] == "rejected"
    # 승인한 뒤에 함의 글이 바뀌었다. 서명하지 않는다. 번호를 쓰지 않는다
    w.store.bodies[did] = b"the swapped text!"
    r = w.send(did)
    assert r["status"] == "failed" and r["number_used"] is False and "does not match" in r["reason"]
    assert w.events() == [] and w.quads() == [] and w.door() == {}
    assert w.outbox(did)["status"] == "failed"
    assert w.send(did) == r                                           # 실패는 한 번 적히고 그대로다
    # 승인의 다른 칸이 초안과 다르면 같다
    d2 = w.approved("second")
    w.store.drafts[d2]["decision"]["to"] = {"lab_id": "lab:elsewhere", "to_id": "R", "to_epoch": 1}
    assert w.send(d2)["status"] == "failed" and w.events() == []
    # 승인이 다른 글의 지문을 적고 있다. 함의 초안이 멀쩡해도 그 승인으로는 서명하지 않는다
    d2b = w.approved("approved for something else")
    w.store.drafts[d2b]["decision"]["body_sha256"] = _sha(b"another text entirely")
    r = w.send(d2b)
    assert r["status"] == "failed" and "not the text that was approved" in r["reason"] and w.events() == []
    # 승인이 서명 전에 끝났다
    d3 = w.approved("third")
    w.t = T0 + hsd.APPROVAL_TTL_SECONDS
    r = w.send(d3)
    assert r["status"] == "failed" and "expired" in r["reason"] and w.events() == []
    # 거절한 초안은 서명하지 않는다. 끝난 초안은 승인하지 못한다
    w.t = T0
    d4 = w.draft("fourth")
    assert w.decide(d4, "rejected")["decision"] == "rejected"         # 거절은 지문 없이 적는다
    assert w.decide(d4, "rejected")["decision"] == "rejected"
    assert w.send(d4) == {"status": "rejected", "draft_id": d4} and w.events() == []
    d5 = w.draft("fifth")
    with pytest.raises(hsd.Refused):
        w.decide(d5, "approved", body_sha256=_sha(b"fifth"), now=lambda: T0 + hdr.DRAFT_TTL_SECONDS)
    assert w.decide(d5, "rejected", now=lambda: T0 + hdr.DRAFT_TTL_SECONDS)["decision"] == "rejected"
    w.srv.shutdown()


def test_an_approval_that_ends_after_signing_does_not_strand_the_letter(tmp_path, monkeypatch):
    """번호를 쓴 편지는 문에 간다. 승인의 끝은 서명 전에만 막는다."""
    w = _World(tmp_path)
    did = w.approved("signed in time")
    real_push = hd.push_quad

    def down(*a, **k):
        raise hd.DropError(503, "sleeping")
    monkeypatch.setattr(hd, "push_quad", down)
    assert w.send(did)["stage"] == "push" and len(w.events()) == 1
    monkeypatch.setattr(hd, "push_quad", real_push)
    w.t = T0 + 3 * hsd.APPROVAL_TTL_SECONDS
    r = w.send(did)
    assert r["status"] == "sent" and r["n"] == "001" and len(w.events()) == 1
    # 원장에 넣고 발신함에 놓기 전에 죽은 편지도 같다. 서명은 이미 됐다 — 「번호를 쓰지 않았다」고
    # 적어 버리면 원장에 내보내지 않은 편지가 남아 그 뒤의 저장이 모두 막힌다
    w.t = T0
    d2 = w.approved("signed, then crashed")
    real_export = ho.export_quad
    monkeypatch.setattr(ho, "export_quad", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        w.send(d2)
    assert len(w.events()) == 2 and w.store.get(d2)["result"] is None
    monkeypatch.setattr(ho, "export_quad", real_export)
    w.t = T0 + 3 * hsd.APPROVAL_TTL_SECONDS
    r = w.send(d2)
    assert r["status"] == "sent" and r["n"] == "002" and len(w.events()) == 2
    w.srv.shutdown()


def test_signing_and_placing_is_done_once(tmp_path):
    """봉투를 짓고 발신함에 놓는 걸음을 다시 불러도 사건도 번호도 늘지 않는다."""
    w = _World(tmp_path)
    did = w.approved("placed once")
    ctx = hsd._Ctx(**w.cfg())
    first = hsd._sign(w.store, w.store.get(did), ctx)
    assert hsd._place_signed(w.store, ctx) == ["001"]
    files = {f.name: f.read_bytes() for f in w.out.iterdir()}
    w.t = T0 + 99
    assert hsd._sign(w.store, w.store.get(did), ctx) == first
    assert hsd._place_signed(w.store, ctx) == []
    assert {f.name: f.read_bytes() for f in w.out.iterdir()} == files and len(w.events()) == 1
    assert sorted(files) == ["001-body.md", "001-envelope.json", "001-sig.txt"]
    # 이미 서명한 편지에는 승인의 끝도, 그 뒤에 달라진 함도 닿지 않는다. 처음의 사건이 돌아온다
    w.t = T0 + 3 * hsd.APPROVAL_TTL_SECONDS
    assert hsd._sign(w.store, w.store.get(did), ctx) == first and len(w.events()) == 1
    w.srv.shutdown()


def test_a_letter_already_in_the_ledger_is_placed_before_any_other_draft(tmp_path, monkeypatch):
    """Orin 062 R1. A를 서명해 원장에 넣고 발신함에 놓기 전에 죽었다. 그 뒤에 A가 아니라 B를 부른다.
    앞 pin에서는 B가 001을 받았고, 체크포인트가 A의 빠짐에서 멈춰 A도 B도 `resume`도 풀리지 않았다."""
    def crashed_after_admit(w, did):
        real = ho.export_quad
        monkeypatch.setattr(ho, "export_quad", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash")))
        with pytest.raises(RuntimeError):
            w.send(did)
        monkeypatch.setattr(ho, "export_quad", real)
        assert len(w.events()) == 1 and w.quads() == []
        return he.event_id_of(json.loads(w.events()[0])["raw"].encode("utf-8"))

    # ① A가 죽은 뒤 B를 부른다. A가 먼저 001로 놓이고 문에 간다. B는 그 뒤의 002다
    w = _World(tmp_path / "one")
    a, b = w.approved("first"), w.approved("second")
    first_event = crashed_after_admit(w, a)
    w.t = T0 + 90
    rb = w.send(b)
    assert rb["status"] == "sent" and rb["n"] == "002" and len(w.events()) == 2
    ra = w.store.get(a)["result"]
    assert ra["status"] == "sent" and ra["n"] == "001" and ra["event_id"] == first_event   # 처음의 사건 그대로
    assert (w.out / "001-body.md").read_bytes() == b"first" and list(w.door()) == ["001", "002"]
    assert json.loads((w.out / "001-envelope.json").read_bytes())["created_at"] == "2026-10-09T00:00:00Z"
    assert w.send(a) == ra and hsd.resume(w.store, **w.cfg()) == [] and len(w.events()) == 2

    # ② 같은 자리에서 `resume`을 부른다. ③ `drain`만 불러도 A가 놓여 문에 간다
    w2 = _World(tmp_path / "two")
    a2, b2 = w2.approved("first"), w2.approved("second")
    crashed_after_admit(w2, a2)
    out = hsd.resume(w2.store, **w2.cfg())
    assert [(r["status"], r["n"]) for r in out] == [("sent", "001"), ("sent", "002")]
    assert [w2.store.get(d)["result"]["n"] for d in (a2, b2)] == ["001", "002"] and len(w2.events()) == 2
    w3 = _World(tmp_path / "three")
    a3 = w3.approved("first")
    crashed_after_admit(w3, a3)
    assert hsd.drain(w3.store, **w3.cfg())["clear"] is True
    assert w3.store.get(a3)["result"]["n"] == "001" and len(w3.events()) == 1

    # ④ 놓을 본문이 함에서 달라졌다. 멈춘다. 새 봉투를 짓지 않고, 그 편지를 건너뛰어 B에 번호를 주지도 않는다
    w4 = _World(tmp_path / "four")
    a4, b4 = w4.approved("first"), w4.approved("second")
    crashed_after_admit(w4, a4)
    w4.store.bodies[a4] = b"not the signed text"
    for call in (lambda: w4.send(b4), lambda: w4.send(a4), lambda: hsd.resume(w4.store, **w4.cfg()),
                 lambda: hsd.drain(w4.store, **w4.cfg())):
        with pytest.raises(hsd.SendStopped, match="no longer matches"):
            call()
    assert len(w4.events()) == 1 and w4.quads() == [] and w4.door() == {}
    del w4.store.bodies[a4]
    with pytest.raises(hsd.SendStopped, match="gone from the store"):
        w4.send(b4)
    w4.store.bodies[a4] = b"first"                                    # 본문이 돌아오면 잇는다
    assert w4.send(b4)["n"] == "002" and w4.store.get(a4)["result"]["n"] == "001"

    # ⑤ 초안에서 오지 않은 자기 편지가 원장에만 있다. 본문을 알 길이 없다 — 멈추고 사람에게 맡긴다
    w5 = _World(tmp_path / "five")
    with ho.hub_writer(w5.hub) as (d, cfg, hub):
        env = ho.build_message_envelope(cfg, signer=SIGNER, key_id="k3", epoch=1, to_lab=TO, to_id="R",
                                        to_epoch=1, body=b"by hand", body_locator="file://by-hand.md",
                                        media_type="text/markdown")
        assert ho.sign_and_admit(d, cfg, hub, env, SEED3)["admitted"]
    b5 = w5.approved("second")
    with pytest.raises(hsd.SendStopped, match="did not come from a draft"):
        w5.send(b5)
    assert len(w5.events()) == 1 and w5.quads() == []

    # ⑥ 편지가 아닌 사건(키의 기록)은 발신함에 없어도 된다. 그것을 놓으려 들지 않고, 편지는 그대로 간다
    w6 = _World(tmp_path / "six")
    with ho.hub_writer(w6.hub) as (d, cfg, hub):
        env = ho.build_envelope(cfg, "key.rotated",
                                {"new_key_id": "k4", "new_key_epoch": 1,
                                 "new_pubkey": sp.public_key((7).to_bytes(32, "big")).hex()},
                                signer=SIGNER, key_id="k1", epoch=1,
                                subject={"type": "machine", "id": "machine:" + cfg["machine_id"]})
        assert ho.sign_and_admit(d, cfg, hub, env, SEED1)["admitted"]
    r6 = w6.send(w6.approved("a letter after a key record"))
    assert r6["status"] == "sent" and r6["n"] == "001" and len(w6.events()) == 2
    assert w6.quads() == ["001-body.md", "001-envelope.json", "001-sig.txt"]
    for x in (w, w2, w3, w4, w5, w6):
        x.srv.shutdown()


def test_a_letter_the_door_does_not_show_is_not_called_sent(tmp_path, monkeypatch):
    """드롭이 받았다고 답했어도 문의 색인에 없으면 「갔다」가 아니다."""
    w = _World(tmp_path)
    did = w.approved("vanished")
    real_push = hd.push_quad
    monkeypatch.setattr(hd, "push_quad", lambda *a, **k: {"n": "001", "stored": True})   # 받았다고만 한다
    r = w.send(did)
    assert r["status"] == "approved" and r["stage"] == "door" and "not on the door" in r["detail"]
    assert w.store.get(did)["result"] is None and w.outbox(did)["status"] == "approved"
    monkeypatch.setattr(hd, "push_quad", real_push)
    assert w.send(did)["status"] == "sent" and len(w.events()) == 1
    w.srv.shutdown()


def test_two_approvals_at_once_make_one_letter(tmp_path):
    w = _World(tmp_path)
    did = w.approved("raced")
    results, errors = [], []

    def go():
        try:
            results.append(w.send(did))
        except Exception as e:                                        # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len(results) == 4 and all(r == results[0] for r in results)
    assert results[0]["status"] == "sent" and len(w.events()) == 1 and list(w.door()) == ["001"]
    w.srv.shutdown()


# ── 멈춤 ─────────────────────────────────────────────────────────────────────

def test_it_stops_when_the_door_and_the_outbox_disagree(tmp_path):
    w = _World(tmp_path)
    did = w.approved("mine")
    # 문에 이 발신함이 모르는 편지가 있다. 번호를 매기는 곳이 둘이라는 뜻이다
    other_hub, other_out = _hub(tmp_path / "other")
    with ho.hub_writer(other_hub) as (d, cfg, hub):
        env = ho.build_message_envelope(cfg, signer=SIGNER, key_id="k1", epoch=1, to_lab=TO, to_id="R",
                                        to_epoch=1, body=b"from the old place", body_locator="file://b",
                                        media_type="text/plain")
        r = ho.sign_and_admit(d, cfg, hub, env, SEED1)
        bp = tmp_path / "b.txt"
        bp.write_bytes(b"from the old place")
        ho.export_quad(d, other_out, event_id=r["event_id"], body_path=bp)
    hd.push_quad(f"{w.url}/v0/{DOOR}", TOKEN, other_out / "001", warmup=False)
    with pytest.raises(hsd.SendStopped, match="does not"):
        w.send(did)
    assert w.events() == [] and w.quads() == []                       # 서명하지 않았다
    # 같은 번호에 다른 편지가 있다
    w2 = _World(tmp_path / "w2")
    d2 = w2.approved("ours")
    (w2.out / "001-envelope.json").write_bytes((other_out / "001-envelope.json").read_bytes())
    (w2.out / "001-sig.txt").write_bytes((other_out / "001-sig.txt").read_bytes())
    w3_hub, w3_out = _hub(tmp_path / "third")
    with ho.hub_writer(w3_hub) as (d, cfg, hub):
        env = ho.build_message_envelope(cfg, signer=SIGNER, key_id="k1", epoch=1, to_lab=TO, to_id="R",
                                        to_epoch=1, body=b"a different letter", body_locator="file://c",
                                        media_type="text/plain")
        r = ho.sign_and_admit(d, cfg, hub, env, SEED1)
        ho.export_quad(d, w3_out, event_id=r["event_id"], body_path=bp)
    hd.push_quad(f"{w2.url}/v0/{DOOR}", TOKEN, w3_out / "001", warmup=False)
    with pytest.raises(hsd.SendStopped, match="differs"):
        w2.send(d2)
    assert w2.events() == []
    w.srv.shutdown()
    w2.srv.shutdown()


def test_the_signing_key_must_be_bound_in_the_ledger(tmp_path):
    w = _World(tmp_path)
    did = w.approved("unbound")
    stranger = (9).to_bytes(32, "big")
    with pytest.raises(hsd.SendStopped, match="did not admit"):
        w.send(did, seed=stranger)
    with pytest.raises(hsd.SendStopped, match="did not admit"):
        w.send(did, key_id="k9")
    assert w.events() == [] and w.quads() == [] and w.store.get(did)["result"] is None
    w.srv.shutdown()


def test_what_was_read_from_the_store_is_checked_again(tmp_path):
    """초안 창구가 뚫렸거나 그 계정이 샜다고 하자. 함에 무엇이 놓였든, 창구가 받을 때 보았어야 할 것을
    서명하는 쪽이 다시 본다(LxM 184 §2). 그런 초안은 보여 주지도 승인하지도 서명하지도 않는다."""
    w = _World(tmp_path)

    def planted(body=b"planted", **change):
        """창구를 거치지 않고 함에 바로 놓는다."""
        rec = hdr.draft_record(caller="leak", sender=change.pop("sender", SIGNER),
                               to_lab=change.pop("to_lab", TO), to_id=change.pop("to_id", "R"),
                               to_epoch=change.pop("to_epoch", 1),
                               media_type=change.pop("media_type", "text/markdown"),
                               client_ref=change.pop("client_ref", f"p{len(w.store.order)}"),
                               body=body, now=T0, ttl=hdr.DRAFT_TTL_SECONDS)
        rec.update(change)
        w.store.drafts[rec["draft_id"]] = {"draft": rec, "decision": None, "result": None}
        w.store.bodies[rec["draft_id"]] = body
        w.store.order.append(rec["draft_id"])
        return rec["draft_id"]

    bad = {
        "한도를 넘는 본문": planted(b"x" * (hdr.DRAFT_BODY_MAX_BYTES + 1)),
        "적어 두지 않은 받는 연구소": planted(to_lab="lab:not-on-the-list"),
        "다른 epoch": planted(to_epoch=7),
        "남의 이름": planted(sender="lab:someone-else"),
        "모르는 매체": planted(media_type="text/html"),
        "꼴이 틀린 받는 이": planted(to_id="a\nb"),
        "글이 아닌 본문": planted(b"\xff\xfe\x00"),
        "빈 본문": planted(b"  \n"),
        "꼴이 틀린 기록": planted(to="lab:receiver"),
    }
    renamed = planted(b"content A")
    w.store.drafts[renamed]["draft"]["client_ref"] = "changed-after-naming"      # 이름이 내용에서 나오지 않는다
    bad["내용과 다른 이름"] = renamed
    for why, did in bad.items():
        with pytest.raises(hsd.Refused):
            w.review(did)
        with pytest.raises(hsd.Refused):
            w.decide(did, "approved", body_sha256=w.store.drafts[did]["draft"]["body_sha256"])
        assert w.store.get(did)["decision"] is None, why
        # 승인의 기록까지 지어 넣어도 서명되지 않는다. 번호를 쓰지 않는다
        d = w.store.drafts[did]["draft"]
        w.store.drafts[did]["decision"] = {
            "decision": "approved", "to": d.get("to"), "media_type": d.get("media_type"),
            "body_sha256": d.get("body_sha256"), "body_size": d.get("body_size"),
            "decided_at": "2026-10-09T00:00:00Z", "expires_at": "2026-10-10T00:00:00Z"}
        r = w.send(did)
        assert r["status"] == "failed" and r["number_used"] is False, (why, r)
    assert w.events() == [] and w.quads() == [] and w.door() == {}
    # 꼴이 틀린 초안도 거절할 수 있다 — 승인 화면을 비울 수 있어야 한다
    extra = planted(b"y" * (hdr.DRAFT_BODY_MAX_BYTES + 1))
    assert w.decide(extra, "rejected")["decision"] == "rejected"
    # 바르게 놓인 초안은 그대로 간다. 한도를 좁히면 그것도 걸린다
    good = w.draft("fine")
    with pytest.raises(hsd.Refused):
        w.review(good, body_max_bytes=3)
    assert w.review(good)["status"] == "pending_approval"
    w.approve(good)
    assert w.send(good)["status"] == "sent"
    # 함이 준 놓인 때로 이레를 잰다
    late = w.draft("late")
    w.store.drafts[late]["placed_at"] = "2026-10-01T00:00:00Z"
    w.store.drafts[late]["draft"]["expires_at"] = "2036-01-01T00:00:00Z"
    assert w.review(late)["status"] == "expired" and w.review(late)["placed_at"] == "2026-10-01T00:00:00Z"
    with pytest.raises(hsd.Refused):
        w.decide(late, "approved", body_sha256=_sha(b"late"))
    w.srv.shutdown()


def test_approved_drafts_left_behind_are_picked_up_in_order(tmp_path, monkeypatch):
    """잠들었다 깬 서버에는 돌던 차례의 기억이 없다. 뜰 때와 승인 화면이 열릴 때 찾아 잇는다(LxM 184 §1)."""
    w = _World(tmp_path)
    ids = []
    for i, body in enumerate(("first", "second", "third")):
        w.t = T0 + i
        ids.append(w.approved(body))
    w.decide(w.draft("not this one"), "rejected")
    w.draft("still waiting")
    w.t = T0 + 60
    real_push = hd.push_quad

    def down(*a, **k):
        raise hd.DropError(503, "sleeping")
    monkeypatch.setattr(hd, "push_quad", down)
    out = hsd.resume(w.store, **w.cfg())
    assert [r["status"] for r in out] == ["approved"] and len(w.events()) == 1   # 첫 것이 못 가면 거기서 멈춘다
    monkeypatch.setattr(hd, "push_quad", real_push)
    out = hsd.resume(w.store, **w.cfg())
    assert [(r["status"], r["n"]) for r in out] == [("sent", "001"), ("sent", "002"), ("sent", "003")]
    assert [w.store.get(d)["result"]["n"] for d in ids] == ["001", "002", "003"]   # 승인된 차례대로
    assert hsd.resume(w.store, **w.cfg()) == [] and len(w.events()) == 3           # 이을 것이 없다
    # 초안이 아무리 쌓여도 승인해 둔 편지가 묻히지 않는다(LxM 186 §5). 창구의 계정이 새어 초안
    # 삼백 개가 그 뒤에 놓였다고 하자. 잇기는 결정의 기록에서 찾는다
    w.t = T0 + 120
    buried = w.approved("approved before the flood")
    for i in range(300):
        rec = hdr.draft_record(caller="leak", sender=SIGNER, to_lab=TO, to_id="R", to_epoch=1,
                               media_type="text/plain", client_ref=f"flood{i}", body=b"x", now=w.t,
                               ttl=hdr.DRAFT_TTL_SECONDS)
        w.store.put_draft(rec, b"x")
    assert buried not in [e["draft"]["draft_id"] for e in w.store.list(SIGNER, hsd.RESUME_SCAN, without="result")]
    out = hsd.resume(w.store, **w.cfg())
    assert [(r["status"], r["n"], r["draft_id"]) for r in out] == [("sent", "004", buried)]

    # 함이 준 것도 다시 본다. 함이 거절한 것, 기다리는 것, 끝난 것을 섞어 주어도 승인된 것만 잇는다
    class Sloppy(hdr.MemoryStore):
        def approved(self, sender, limit):
            return list(reversed(self.list(sender, limit)))
    w.t = T0 + 180
    sloppy = Sloppy()
    sloppy.__dict__.update(w.store.__dict__)
    rec = hdr.draft_record(caller="jdot", sender=SIGNER, to_lab=TO, to_id="R", to_epoch=1,
                           media_type="text/markdown", client_ref="sloppy", body=b"picked from a sloppy answer",
                           now=w.t, ttl=hdr.DRAFT_TTL_SECONDS)
    w.store.put_draft(rec, b"picked from a sloppy answer")          # 창구는 기다리는 초안이 차서 받지 않는다
    one = rec["draft_id"]
    w.approve(one)
    out = hsd.resume(sloppy, **w.cfg())
    assert [(r["status"], r["n"], r["draft_id"]) for r in out] == [("sent", "005", one)] and len(w.events()) == 5
    w.srv.shutdown()


def test_the_ledger_is_opened_before_anything_is_signed(tmp_path):
    """디스크가 없는 배치. 뜰 때마다 상태 칸에서 되살리고 문과 맞댄다(LxM 184 §4). 그 전에는 서명하지
    않고, 어긋나면 그 자리에서는 서명하지 않는다."""
    w = _World(tmp_path, marks=True)
    (w.mdir / "settled").write_bytes(b"")
    send = lambda did, **kw: w.send(did, wait=30, mirror_wait=30, sleep=lambda s: w.supervise(), **kw)   # noqa: E731
    assert send(w.approved("before the restart"))["n"] == "001"
    assert send(w.approved("also before"))["n"] == "002"

    # 서비스가 다시 떴다. 디스크에 아무것도 없다
    boot = tmp_path / "boot2"
    place = dict(hub_dir=boot / "hub", outbox_dir=boot / "out", door=DOOR, drop_url=w.url, token=TOKEN)
    did = w.approved("after the restart")
    with pytest.raises(hsd.SendStopped, match="open_ledger"):
        hsd.send_approved(w.store, did, **{**w.cfg(), **place})
    with pytest.raises(hsd.SendStopped, match="open_ledger"):
        hsd.resume(w.store, **{**w.cfg(), **place})
    # 믿는 키로 서명되지 않은 세대는 되살리지 않는다
    with pytest.raises(hsd.SendStopped, match="restore"):
        hsd.open_ledger(**place, trusted_pubkeys=[PUB1], wait=0, sleep=lambda s: None)
    assert not (boot / "hub").exists()
    report = hsd.open_ledger(**place, trusted_pubkeys=[PUB3], wait=0, sleep=lambda s: None)
    assert report["restored"] is True and report["letters"] == 2 and report["not_on_the_door"] == []
    assert set(report["signed_by"].values()) == {PUB3}
    assert sorted(f.name for f in (boot / "out").iterdir()) == w.quads()          # 본문까지 되살아났다
    assert (boot / "out" / "001-body.md").read_bytes() == b"before the restart"
    again = hsd.open_ledger(**place, trusted_pubkeys=[PUB3], wait=0, sleep=lambda s: None)
    assert again["restored"] is False and again["letters"] == 2                   # 열려 있으면 맞대기만 한다
    # 다른 문을 주면 열지 않는다. 되살린 것도, 이미 열린 것도 그렇다
    wrong = dict(place, door="hub-ops/from-elsewhere")
    with pytest.raises(hsd.SendStopped, match="another door"):
        hsd.open_ledger(**wrong, trusted_pubkeys=[PUB3], wait=0, sleep=lambda s: None)
    elsewhere = dict(wrong, hub_dir=tmp_path / "bootx" / "hub", outbox_dir=tmp_path / "bootx" / "out")
    with pytest.raises(hsd.SendStopped, match="another door"):
        hsd.open_ledger(**elsewhere, trusted_pubkeys=[PUB3], wait=0, sleep=lambda s: None)
    assert not (tmp_path / "bootx" / "hub").exists() and not (tmp_path / "bootx" / "out").exists()
    r = hsd.send_approved(w.store, did, **{**w.cfg(wait=30, mirror_wait=30, sleep=lambda s: w.supervise()),
                                           **place})
    assert r["status"] == "sent" and r["n"] == "003" and list(w.door()) == ["001", "002", "003"]

    # 또 다시 떴는데 그 사이에 누가 이 문에 편지를 올렸다. 되살린 발신함이 문과 어긋난다 — 멈춘다
    other_hub, other_out = _hub(tmp_path / "other")
    with ho.hub_writer(other_hub) as (d, cfg, hub):
        for i in range(4):
            env = ho.build_message_envelope(cfg, signer=SIGNER, key_id="k1", epoch=1, to_lab=TO, to_id="R",
                                            to_epoch=1, body=b"x%d" % i, body_locator=f"file://x{i}",
                                            media_type="text/plain")
            ho.export_quad(d, other_out, event_id=ho.sign_and_admit(d, cfg, hub, env, SEED1)["event_id"])
    hd.push_quad(f"{w.url}/v0/{DOOR}", TOKEN, other_out / "004", warmup=False)
    boot3 = tmp_path / "boot3"
    place3 = dict(place, hub_dir=boot3 / "hub", outbox_dir=boot3 / "out")
    with pytest.raises(hsd.SendStopped, match="does not"):
        hsd.open_ledger(**place3, trusted_pubkeys=[PUB3], wait=0, sleep=lambda s: None)
    w.srv.shutdown()


def test_only_message_posted_is_ever_built_here():
    src = (SRC / "organum" / "hub_send.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in src.split('"""')[2::2])            # 문서 글을 뺀 코드
    assert "build_message_envelope(" in code and ".build_envelope(" not in code
    for word in ("key.rotated", "key.revoked", "signer.introduced", "rotate", "revoke"):
        assert word not in code, word


# ── 되살린 발신함과, 표지를 쓰는 배치 ────────────────────────────────────────

def test_older_letters_and_key_records_in_the_outbox_are_carried_not_reported(tmp_path):
    """서명 서비스가 되살린 발신함에는 옛 편지와 키의 기록이 있다. 문에 없는 것은 먼저 올리고,
    초안에서 온 편지에만 결과를 적는다."""
    w = _World(tmp_path)
    with ho.hub_writer(w.hub) as (d, cfg, hub):
        env = ho.build_message_envelope(cfg, signer=SIGNER, key_id="k1", epoch=1, to_lab=TO, to_id="R",
                                        to_epoch=1, body=b"an old letter", body_locator="file://old.md",
                                        media_type="text/markdown")
        r = ho.sign_and_admit(d, cfg, hub, env, SEED1)
        bp = tmp_path / "old.md"
        bp.write_bytes(b"an old letter")
        ho.export_quad(d, w.out, event_id=r["event_id"], body_path=bp)
    hd.push_quad(f"{w.url}/v0/{DOOR}", TOKEN, w.out / "001", warmup=False)
    with ho.hub_writer(w.hub) as (d, cfg, hub):                      # 키의 기록. 본문이 없고 아직 문에 없다
        env = ho.build_envelope(cfg, "key.rotated",
                                {"new_key_id": "k4", "new_key_epoch": 1,
                                 "new_pubkey": sp.public_key((7).to_bytes(32, "big")).hex()},
                                signer=SIGNER, key_id="k1", epoch=1,
                                subject={"type": "machine", "id": "machine:" + cfg["machine_id"]})
        r = ho.sign_and_admit(d, cfg, hub, env, SEED1)
        ho.export_quad(d, w.out, event_id=r["event_id"])
    did = w.approved("the first letter from the service")
    r = w.send(did)
    assert r["status"] == "sent" and r["n"] == "003" and list(w.door()) == ["001", "002", "003"]
    assert [k for k, e in w.store.drafts.items() if e["result"] is not None] == [did]
    w.srv.shutdown()


def test_with_markers_a_letter_is_sent_only_after_the_outside_copy(tmp_path):
    w = _World(tmp_path, marks=True)
    did = w.approved("durable")
    # 묶음은 받았으나 표지가 없다. 올리지 않는다
    r = w.send(did)
    assert r["status"] == "approved" and r["stage"] == "checkpoint" and w.door() == {}
    assert len(w.events()) == 1
    # 세대의 표지가 선 뒤에 올린다. 그런데 편지의 표지가 아직 없다
    w.supervise()
    r = w.send(did)
    assert r["status"] == "approved" and r["stage"] == "door" and "outside" in r["detail"]
    assert w.door()["001"]["mirrored"] is False and w.store.get(did)["result"] is None
    # 편지가 바깥에 옮겨진 뒤에야 「갔다」
    w.supervise()
    r = w.send(did)
    assert r["status"] == "sent" and r["n"] == "001" and len(w.events()) == 1
    # 기다리는 동안 감독기가 돌면 한 번에 끝난다
    d2 = w.approved("second")
    r = w.send(d2, wait=30, mirror_wait=30, sleep=lambda s: w.supervise())
    assert r["status"] == "sent" and r["n"] == "002"
    # 표지를 쓰지 않는 서버에서는 200만 믿겠다고 밝혀야 나아간다
    w2 = _World(tmp_path / "plain")
    d3 = w2.approved("plain")
    r = w2.send(d3, received_only=False)
    assert r["status"] == "approved" and r["stage"] == "checkpoint" and w2.door() == {}
    assert w2.send(d3)["status"] == "sent"
    w.srv.shutdown()
    w2.srv.shutdown()
