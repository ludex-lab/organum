"""Gate 2 백로그 회귀 시험 — ㉒(hub_front LookupError)·㉕(open_ledger unexported)·㉗(hub_ops 키 수명 빌더)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from organum import hub_cli as hc
from organum import hub_envelope as he
from organum import hub_front as hf
from organum import hub_ops as ho
from organum import hub_send as hsd
from organum import hub_state as hs
from organum import schnorr_pure as sp


def _msg_bytes(to_lab: str, body: bytes, created_at: str) -> bytes:
    env = {
        "envelope_schema": he.ENVELOPE_SCHEMA,
        "event_kind": "message.posted",
        "signer": {"id": "lab:alpha", "key_id": "k1", "key_epoch": 1},
        "subject": {"type": "message", "id": "message:" + hashlib.sha256(body).hexdigest()[:24]},
        "provenance": {
            "lab": "lab:alpha", "machine": "m-1", "platform": "linux",
            "adapter": "test", "cli_version": None, "capture": None,
        },
        "idempotency_key": hashlib.sha256(body).hexdigest()[:32],
        "created_at": created_at,
        "payload": {
            "target": {"lab_id": to_lab, "to_id": "Jdot", "to_epoch": 1},
            "body_locator": "file://body.md",
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "body_media_type": "text/markdown",
        },
    }
    return he.canonical_bytes(env)


def test_22_mail_list_turns_missing_envelope_lookup_error_into_problem():
    """㉒: 색인에는 있는 번호의 봉투가 빠져 LookupError가 나도 목록이 500으로 죽지 않고 problems에 남긴다."""
    raw1 = _msg_bytes("lab:hq", b"first letter", "2026-11-25T00:00:01Z")
    raw3 = _msg_bytes("lab:hq", b"third letter", "2026-11-25T00:00:03Z")
    door = "hub-ops/from-alpha"

    class _FlakyReader:
        def index(self, d, since=None, timeout=10):
            assert d == door
            items = [
                {"n": "001", "envelope_sha256": hashlib.sha256(raw1).hexdigest(),
                 "body_name": "body.md", "body_size": 12},
                {"n": "002", "envelope_sha256": "0" * 64,
                 "body_name": "body.md", "body_size": 10},
                {"n": "003", "envelope_sha256": hashlib.sha256(raw3).hexdigest(),
                 "body_name": "body.md", "body_size": 12},
            ]
            if since:
                items = [e for e in items if int(e["n"]) > int(since)]
            return {"index": items, "more": False}

        def envelope(self, d, n, timeout=10):
            if n == "001":
                return raw1
            if n == "003":
                return raw3
            raise LookupError(f"{d}/{n}")

    grant = hf.Grant(recipient="lab:hq", doors=(door,))
    front = hf.MailFront(
        reader=_FlakyReader(),
        grants={"tok": grant},
        now=lambda: 1795478400.0,
    )
    status, resp = front.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "mail_list", "arguments": {}}},
        "tok",
    )
    assert status == 200
    body = json.loads(resp["result"]["content"][0]["text"])
    assert body["status"] == "ok"
    assert [x["n"] for x in body["letters"]] == ["001", "003"]
    assert body["problems"] == [{"door": door, "n": "002", "problem": "envelope missing from the door"}]
    assert body["examined"] == 3


def test_22_mail_list_missing_last_envelope_advances_cursor_and_audits_examined():
    """㉒ 보강(LxM 222 §3 ④): 문의 마지막 번호가 빠져도 커서가 그 번호를 지나가고 감사 줄 examined에 남는다."""
    raw1 = _msg_bytes("lab:hq", b"first letter", "2026-11-25T00:00:01Z")
    raw2 = _msg_bytes("lab:hq", b"second letter", "2026-11-25T00:00:02Z")
    door = "hub-ops/from-alpha"

    class _TailMissingReader:
        def index(self, d, since=None, timeout=10):
            assert d == door
            items = [
                {"n": "001", "envelope_sha256": hashlib.sha256(raw1).hexdigest(),
                 "body_name": "body.md", "body_size": 12},
                {"n": "002", "envelope_sha256": hashlib.sha256(raw2).hexdigest(),
                 "body_name": "body.md", "body_size": 13},
                {"n": "003", "envelope_sha256": "0" * 64,
                 "body_name": "body.md", "body_size": 10},
            ]
            if since:
                items = [e for e in items if int(e["n"]) > int(since)]
            return {"index": items, "more": False}

        def envelope(self, d, n, timeout=10):
            if n == "001":
                return raw1
            if n == "002":
                return raw2
            raise LookupError(f"{d}/{n}")

    records: list[dict] = []
    grant = hf.Grant(recipient="lab:hq", doors=(door,))
    front = hf.MailFront(
        reader=_TailMissingReader(),
        grants={"tok": grant},
        now=lambda: 1795478400.0,
        audit=records.append,
    )
    status1, resp1 = front.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "mail_list", "arguments": {}}},
        "tok",
    )
    assert status1 == 200
    body1 = json.loads(resp1["result"]["content"][0]["text"])
    assert body1["status"] == "ok"
    assert [x["n"] for x in body1["letters"]] == ["001", "002"]
    assert body1["problems"] == [{"door": door, "n": "003", "problem": "envelope missing from the door"}]
    assert body1["examined"] == 3
    assert len(records) == 1
    assert records[0]["examined"] == [[door, "001"], [door, "002"], [door, "003"]]
    assert records[0]["delivered"] == [[door, "001"], [door, "002"]]

    # 반환된 커서로 재호출하면 빠진 마지막 번호(003)를 지나간 상태라 새 문제도 검사 건수도 0이다
    status2, resp2 = front.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "mail_list", "arguments": {"cursor": body1["cursor"]}}},
        "tok",
    )
    assert status2 == 200
    body2 = json.loads(resp2["result"]["content"][0]["text"])
    assert body2["status"] == "ok"
    assert body2["letters"] == []
    assert "problems" not in body2
    assert body2["examined"] == 0


def test_25_open_ledger_reports_unexported_on_restore(tmp_path, monkeypatch):
    """㉕: open_ledger가 되살린 묶음의 unexported 목록을 보고에 그대로 싣는다."""
    hub = tmp_path / "hub"
    out = tmp_path / "out"
    out.mkdir()

    def _fake_restore(h, outboxes, **kw):
        h.mkdir(parents=True, exist_ok=True)
        (h / "hub.json").write_text("{}", encoding="utf-8")
        return {
            "generation": 4,
            "signed_by": {"k1": "aa" * 32},
            "events": 7,
            "unexported": ["evt-unexported-1"],
            "outboxes": {"out": {"door": "hub-ops/from-hq", "bodies_to_fetch": []}},
        }

    monkeypatch.setattr(hs, "restore_from_slot", _fake_restore)
    monkeypatch.setattr(hs, "load_record", lambda h: {"outboxes": {"out": {"door": "hub-ops/from-hq"}}})
    monkeypatch.setattr(hsd, "_complete", lambda p: {})
    monkeypatch.setattr(hsd, "_door", lambda c: {})

    rep = hsd.open_ledger(
        hub_dir=hub, outbox_dir=out, door="hub-ops/from-hq",
        drop_url="https://drop.example", token="tok", trusted_pubkeys=["aa" * 32],
    )
    assert rep["restored"] is True
    assert rep["unexported"] == ["evt-unexported-1"]
    assert rep["generation"] == 4


def test_27_hub_ops_rotate_and_revoke_key_builders_admit_and_match_cli(tmp_path):
    """㉗: hub_ops의 build_rotate_key_envelope · build_revoke_key_envelope가 유효한 봉투를 짓고 CLI와 바이트 동일하다."""
    seed1 = bytes.fromhex("11" * 32)
    pub1 = sp.public_key(seed1).hex()
    seed2 = bytes.fromhex("22" * 32)
    pub2 = sp.public_key(seed2).hex()

    hub_dir = tmp_path / "hub"
    assert hc.main(["init", "--dir", str(hub_dir), "--source-domain", "lab:test/hub"]) == 0
    assert hc.main(["register-key", "--dir", str(hub_dir), "--signer", "lab:test",
                    "--key-id", "k1", "--epoch", "1", "--pubkey", pub1]) == 0

    with ho.hub_writer(hub_dir) as (d, cfg, hub):
        rot = ho.build_rotate_key_envelope(
            cfg, signer="lab:test", key_id="k1", epoch=1,
            new_key_id="k2", new_epoch=2, new_pubkey=pub2,
            created_at="2026-11-25T00:00:00Z",
        )
        # CLI의 기존 _build_envelope 직접 호출 모양과 바이트 일치 확인
        rot_direct = ho.build_envelope(
            cfg, "key.rotated",
            {"new_key_id": "k2", "new_key_epoch": 2, "new_pubkey": pub2},
            signer="lab:test", key_id="k1", epoch=1,
            subject={"type": "machine", "id": "machine:" + cfg["machine_id"]},
            created_at="2026-11-25T00:00:00Z",
        )
        assert he.canonical_bytes(rot) == he.canonical_bytes(rot_direct)
        r1 = ho.sign_and_admit(d, cfg, hub, rot, seed1)
        assert r1["admitted"] is True and r1["accepted_seq"] == 1

        rev = ho.build_revoke_key_envelope(
            cfg, signer="lab:test", key_id="k1", epoch=1,
            revoke_key_id="k2", revoke_epoch=2, reason="retired in test",
            created_at="2026-11-25T00:01:00Z",
        )
        rev_direct = ho.build_envelope(
            cfg, "key.revoked",
            {"key_id": "k2", "key_epoch": 2, "reason": "retired in test"},
            signer="lab:test", key_id="k1", epoch=1,
            subject={"type": "machine", "id": "machine:" + cfg["machine_id"]},
            created_at="2026-11-25T00:01:00Z",
        )
        assert he.canonical_bytes(rev) == he.canonical_bytes(rev_direct)
        r2 = ho.sign_and_admit(d, cfg, hub, rev, seed1)
        assert r2["admitted"] is True and r2["accepted_seq"] == 2
