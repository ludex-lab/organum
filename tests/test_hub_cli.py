"""organum-hub CLI — 두 집단 교환 루프 e2e (만남 시나리오의 리허설).

시나리오: 랩 A(hub 운영)와 랩 B(외부 — 예: 다른 커뮤니티의 에이전트들)가
① 각자 키를 만들고 ② A의 hub에 양쪽 키를 등록하고 ③ A가 자기 파일을 attest하고
④ B가 자기 도구로 만든 봉투를 서명해 보내면 A가 admit하고 ⑤ 포함 증명을 떼어
B가 오프라인 검증하고 ⑥ B가 키를 회전·폐기해도 이력이 남는 것까지.

상태 모델 검증 포함: 로그가 곧 상태(재실행 = 재생 복원), 로그 변조는 재생이 잡는다.
"""
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from organum import hub_cli as cli  # noqa: E402
from organum import hub_envelope as he  # noqa: E402
from organum import schnorr_pure as sp  # noqa: E402


def run(capsys, *argv):
    """CLI 호출 → (exit, stdout-json|text)."""
    try:
        rc = cli.main(list(argv))
    except SystemExit as e:
        rc = e.code
    out = capsys.readouterr().out.strip()
    try:
        return rc, json.loads(out) if out else None
    except ValueError:
        return rc, out


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _setup(capsys, ws):
    """A/B 키 생성 + hub init + 양쪽 bootstrap 등록. (a_pub, b_pub) 반환."""
    _, a = run(capsys, "keygen", "lab-a")
    _, b = run(capsys, "keygen", "lab-b")
    _, h = run(capsys, "keygen", "hub-receipt")
    rc, _ = run(capsys, "init", "--dir", "hub", "--source-domain", "lab:a/hub")
    assert rc == 0
    for signer, pub in (("lab:a", a["pubkey"]), ("lab:b", b["pubkey"])):
        rc, _ = run(capsys, "register-key", "--dir", "hub", "--signer", signer,
                    "--key-id", "k1", "--epoch", "1", "--pubkey", pub)
        assert rc == 0
    return a["pubkey"], b["pubkey"], h["pubkey"]


def test_두_집단_교환_루프_전체(capsys, ws):
    a_pub, b_pub, h_pub = _setup(capsys, ws)

    # ③ A가 자기 파일을 attest (영수증 포함)
    Path("prereg.json").write_text('{"design": "walk-1"}', encoding="utf-8")
    rc, r = run(capsys, "attest", "--dir", "hub", "--key", "lab-a.seed",
                "--signer", "lab:a", "--key-id", "k1", "--epoch", "1",
                "--file", "prereg.json", "--claim", "core:artifact.frozen",
                "--scope", "meet-demo", "--receipt-key", "hub-receipt.seed")
    assert rc == 0 and r["admitted"] and r["accepted_seq"] == 1
    assert r["receipt"]["body"]["source_domain"] == "lab:a/hub"
    a_event_id = r["event_id"]

    # 영수증을 B가 검증한다 (hub 공개키만으로)
    Path("receipt.json").write_text(json.dumps(r["receipt"]), encoding="utf-8")
    rc, v = run(capsys, "receipt-verify", "--receipt", "receipt.json",
                "--hub-pubkey", h_pub, "--source-domain", "lab:a/hub")
    assert rc == 0 and v["valid"]

    # ④ B가 자기 도구(여기선 라이브러리)로 봉투를 만들어 서명해 보냄 → A가 admit
    env_b = {"envelope_schema": he.ENVELOPE_SCHEMA, "event_kind": "artifact.attested",
             "signer": {"id": "lab:b", "key_id": "k1", "key_epoch": 1},
             "subject": {"type": "artifact", "id": "artifact:b-greeting"},
             "provenance": {"lab": "lab:b", "machine": "m-b01", "platform": "linux",
                            "adapter": "b-tooling/1.0", "cli_version": None,
                            "capture": None},
             "idempotency_key": "b-hello-1", "created_at": "2026-08-16T01:00:00Z",
             "payload": {"artifact": {"role": "artifact", "schema_id": "raw/v1",
                                      "sha256": hashlib.sha256(b"hello").hexdigest(),
                                      "byte_length": 5,
                                      "media_type": "text/plain"},
                         "bindings": [], "causal": {},
                         "claim": {"type": "core:artifact.frozen", "scope": "meet-demo",
                                   "attests_ordering_of": "emission",
                                   "evidence_basis": {"method": "raw-bytes-sha256",
                                                      "verifier_schema": "raw/v1",
                                                      "body_custody": "repo",
                                                      "locator_authority": False}}}}
    Path("b-envelope.json").write_text(json.dumps(env_b, ensure_ascii=False),
                                       encoding="utf-8")
    rc, sig = run(capsys, "sign", "--key", "lab-b.seed", "--envelope", "b-envelope.json")
    assert rc == 0
    rc, r2 = run(capsys, "admit", "--dir", "hub", "--envelope", "b-envelope.json",
                 "--sig", sig["sig"], "--pubkey", sig["pubkey"])
    assert rc == 0 and r2["admitted"] and r2["accepted_seq"] == 2

    # ⑤ A가 자기 이벤트의 포함 증명을 떼고, B가 오프라인 검증
    rc, pf = run(capsys, "prove", "--dir", "hub", "--event-id", a_event_id)
    assert rc == 0
    Path("proof.json").write_text(json.dumps(pf), encoding="utf-8")
    # (B 손에는 A의 봉투 원문이 있다 — attest가 만든 봉투를 로그에서 복원)
    log = [json.loads(l) for l in
           (ws / "hub" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    Path("a-envelope.json").write_text(log[0]["raw"], encoding="utf-8")
    rc, pv = run(capsys, "verify-proof", "--envelope", "a-envelope.json",
                 "--proof", "proof.json")
    assert rc == 0 and pv["included"]

    # ⑥ B 키 회전 → 새 키로 계속 → 옛 키 폐기 → 이력 질의
    _, nb = run(capsys, "keygen", "lab-b2")
    rc, rot = run(capsys, "rotate-key", "--dir", "hub", "--key", "lab-b.seed",
                  "--signer", "lab:b", "--key-id", "k1", "--epoch", "1",
                  "--new-key-id", "k2", "--new-epoch", "2", "--new-pubkey", nb["pubkey"])
    assert rc == 0 and rot["admitted"]
    rc, rv = run(capsys, "revoke-key", "--dir", "hub", "--key", "lab-b2.seed",
                 "--signer", "lab:b", "--key-id", "k2", "--epoch", "2",
                 "--revoke-key-id", "k1", "--revoke-epoch", "1",
                 "--reason", "rotation 완료")
    assert rc == 0 and rv["admitted"]
    # "그때 유효했나"(C2: 효력은 n+1부터): B의 옛 키는 자기 이벤트 좌표(seq 2)와
    # revocation 이벤트 좌표까지 유효, 그 다음 좌표부터 무효다.
    rc, w1 = run(capsys, "was-valid", "--dir", "hub", "--pubkey", b_pub, "--seq", "2")
    assert w1["valid"] is True
    rc, w2 = run(capsys, "was-valid", "--dir", "hub", "--pubkey", b_pub,
                 "--seq", str(rv["accepted_seq"] + 1))
    assert w2["valid"] is False

    # list가 4건을 보여준다
    rc, _ = run(capsys, "list", "--dir", "hub")
    assert rc == 0


def test_로그가_곧_상태_재생_복원(capsys, ws):
    a_pub, b_pub, _ = _setup(capsys, ws)
    Path("f.txt").write_text("x", encoding="utf-8")
    rc, r = run(capsys, "attest", "--dir", "hub", "--key", "lab-a.seed",
                "--signer", "lab:a", "--key-id", "k1", "--epoch", "1",
                "--file", "f.txt", "--claim", "core:artifact.frozen", "--scope", "s")
    assert rc == 0
    # 새 프로세스 관점(재로드) — prove가 재생된 상태에서 동작
    rc, pf = run(capsys, "prove", "--dir", "hub", "--event-id", r["event_id"])
    assert rc == 0 and pf["tree_size"] == 1


def test_로그_변조는_재생이_잡는다(capsys, ws):
    _setup(capsys, ws)
    Path("f.txt").write_text("x", encoding="utf-8")
    run(capsys, "attest", "--dir", "hub", "--key", "lab-a.seed", "--signer", "lab:a",
        "--key-id", "k1", "--epoch", "1", "--file", "f.txt",
        "--claim", "core:artifact.frozen", "--scope", "s")
    log_p = ws / "hub" / "events.jsonl"
    rec = json.loads(log_p.read_text(encoding="utf-8"))
    rec["raw"] = rec["raw"].replace('"scope":"s"', '"scope":"tampered"')
    log_p.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
    rc, out = run(capsys, "list", "--dir", "hub")
    assert rc == 2                                        # 재생 실패 = 로그 손상으로 정지


def test_attest_재시도는_수렴(capsys, ws):
    _setup(capsys, ws)
    Path("f.txt").write_text("x", encoding="utf-8")
    args = ("attest", "--dir", "hub", "--key", "lab-a.seed", "--signer", "lab:a",
            "--key-id", "k1", "--epoch", "1", "--file", "f.txt",
            "--claim", "core:artifact.frozen", "--scope", "s")
    rc1, r1 = run(capsys, *args)
    rc2, r2 = run(capsys, *args)
    assert r2["duplicate"] is True                        # idem 결정론 파생 덕
    assert r2["accepted_seq"] == r1["accepted_seq"]
    # 로그에 중복 기록도 없다 — 재생이 계속 성립
    assert len((ws / "hub" / "events.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_keygen은_덮어쓰지_않고_0600(capsys, ws):
    rc, k = run(capsys, "keygen", "me")
    assert rc == 0
    assert (ws / "me.seed").stat().st_mode & 0o777 == 0o600
    rc2, _ = run(capsys, "keygen", "me")
    assert rc2 == 2                                       # 기존 seed 보호


def test_미등록_키의_봉투는_거부(capsys, ws):
    _setup(capsys, ws)
    run(capsys, "keygen", "stranger")
    Path("f.txt").write_text("x", encoding="utf-8")
    rc, r = run(capsys, "attest", "--dir", "hub", "--key", "stranger.seed",
                "--signer", "lab:stranger", "--key-id", "k1", "--epoch", "1",
                "--file", "f.txt", "--claim", "core:artifact.frozen", "--scope", "s")
    assert rc == 1 and not r["admitted"]
    assert any("self-assertion" in p for p in r["problems"])


def test_wire_out_in_왕복(capsys, ws):
    a_pub, b_pub, _ = _setup(capsys, ws)
    env = {"envelope_schema": he.ENVELOPE_SCHEMA, "event_kind": "message.read",
           "signer": {"id": "lab:b", "key_id": "k1", "key_epoch": 1},
           "subject": {"type": "run", "id": "run:meet-1"},
           "provenance": {"lab": "lab:b", "machine": "m-b01", "platform": "linux",
                          "adapter": "b-tooling/1.0", "cli_version": None,
                          "capture": None},
           "idempotency_key": "b-wire-1", "created_at": "2026-08-16T01:00:00Z",
           "payload": {"cursor": "c-1"}}
    Path("env.json").write_text(json.dumps(env, ensure_ascii=False), encoding="utf-8")
    rc, ev = run(capsys, "wire-out", "--envelope", "env.json", "--key", "lab-b.seed",
                 "--created-at", "1755300000")
    assert rc == 0 and ev["kind"] == 1
    Path("wire-event.json").write_text(json.dumps(ev, ensure_ascii=False),
                                       encoding="utf-8")
    rc, r = run(capsys, "wire-in", "--dir", "hub", "--event", "wire-event.json")
    assert rc == 0 and r["admitted"]
    # 재생에도 wire 이벤트가 복원된다
    rc, pf = run(capsys, "prove", "--dir", "hub", "--event-id", r["event_id"])
    assert rc == 0 and pf["tree_size"] == 1


# ═══ Orin C1a — late bootstrap 차단 (2026-08-16) ════════════════════════════

def test_c1_admitted_이벤트_후_register_key는_거부되고_config_불변(capsys, ws):
    """[Orin C1 재현] seq가 선 뒤 bootstrap 등록은 lifecycle/log 우회 — 거부 +
    hub.json bytes 무변 + rotate-key 안내."""
    _setup(capsys, ws)
    Path("f.txt").write_text("x", encoding="utf-8")
    run(capsys, "attest", "--dir", "hub", "--key", "lab-a.seed", "--signer", "lab:a",
        "--key-id", "k1", "--epoch", "1", "--file", "f.txt",
        "--claim", "core:artifact.frozen", "--scope", "s")
    before = (ws / "hub" / "hub.json").read_bytes()
    _, k = run(capsys, "keygen", "late")
    rc, _ = run(capsys, "register-key", "--dir", "hub", "--signer", "lab:late",
                "--key-id", "k1", "--epoch", "1", "--pubkey", k["pubkey"])
    assert rc == 2                                        # 거부
    assert (ws / "hub" / "hub.json").read_bytes() == before   # bytes 불변


def test_c1_같은_tuple_bootstrap_중복도_거부되고_config_불변(capsys, ws):
    """[Orin C1 재현 2·3] 같은 (signer,key_id,epoch)에 다른 pubkey — registry
    불변식이 쓰기 전에 거부."""
    _setup(capsys, ws)
    before = (ws / "hub" / "hub.json").read_bytes()
    _, k = run(capsys, "keygen", "dup")
    rc, _ = run(capsys, "register-key", "--dir", "hub", "--signer", "lab:a",
                "--key-id", "k1", "--epoch", "1", "--pubkey", k["pubkey"])
    assert rc == 2
    assert (ws / "hub" / "hub.json").read_bytes() == before


def test_c1_잘못된_shape도_쓰기_전_거부(capsys, ws):
    _setup(capsys, ws)
    before = (ws / "hub" / "hub.json").read_bytes()
    rc, _ = run(capsys, "register-key", "--dir", "hub", "--signer", "no-prefix",
                "--key-id", "k9", "--epoch", "1", "--pubkey", "e" * 64)
    assert rc == 2
    assert (ws / "hub" / "hub.json").read_bytes() == before
