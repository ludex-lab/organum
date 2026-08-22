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


# ═══ message — 만남의 기본 동사 (2026-08-16) ════════════════════════════════

def test_message_봉투_본문_digest_결속(capsys, ws):
    """원격 지인 시나리오: A가 인사말 파일을 쓰고 message로 B(수신 셀)에게 —
    봉투가 본문 digest·수신자·발신 서명을 결속하고, 수신 쪽은 본문 sha256 대조."""
    import hashlib as _h
    _setup(capsys, ws)
    Path("greeting.md").write_text("만나서 반갑습니다 — from lab:a", encoding="utf-8")
    rc, r = run(capsys, "message", "--dir", "hub", "--key", "lab-a.seed",
                "--signer", "lab:a", "--key-id", "k1", "--epoch", "1",
                "--to-lab", "lab:b", "--to-id", "creature-alpha", "--to-epoch", "1",
                "--body-file", "greeting.md")
    assert rc == 0 and r["admitted"], r
    # 봉투에서 본문 digest 대조 (수신 쪽이 할 일)
    log = [json.loads(l) for l in
           (ws / "hub" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    env = json.loads(log[-1]["raw"])
    assert env["event_kind"] == "message.posted"
    assert env["payload"]["target"]["to_id"] == "creature-alpha"
    assert env["payload"]["body_sha256"] == _h.sha256(
        Path("greeting.md").read_bytes()).hexdigest()
    # role 격납: 이 메시지는 exact target의 lab_operator에게만 전달 가능
    ok = he.admit_to_role("lab_operator", env, target_cell="creature-alpha",
                          target_epoch=1)
    assert ok["admitted"]
    other = he.admit_to_role("lab_operator", env, target_cell="creature-beta",
                             target_epoch=1)
    assert not other["admitted"]


def test_message_재전송도_수렴(capsys, ws):
    _setup(capsys, ws)
    Path("g.md").write_text("hi", encoding="utf-8")
    args = ("message", "--dir", "hub", "--key", "lab-a.seed", "--signer", "lab:a",
            "--key-id", "k1", "--epoch", "1", "--to-lab", "lab:b",
            "--to-id", "c", "--to-epoch", "1", "--body-file", "g.md")
    rc1, r1 = run(capsys, *args)
    rc2, r2 = run(capsys, *args)
    assert r2["duplicate"] is True and r2["accepted_seq"] == r1["accepted_seq"]


# ═══ export — 우체통 quad (일반 채널 절차의 발신 절반, 2026-08-16) ═══════════

def test_export_quad_와_수신측_admit_왕복(capsys, ws):
    """발신: message → export가 NNN quad를 깐다. 수신: 별도 hub가 quad 파일만으로
    admit(--sig-file) — events.jsonl 손 추출 없는 완결 루프."""
    _setup(capsys, ws)
    Path("g.md").write_text("인사", encoding="utf-8")
    rc, r = run(capsys, "message", "--dir", "hub", "--key", "lab-a.seed",
                "--signer", "lab:a", "--key-id", "k1", "--epoch", "1",
                "--to-lab", "lab:b", "--to-id", "Aria", "--to-epoch", "1",
                "--body-file", "g.md")
    assert rc == 0
    rc, ex = run(capsys, "export", "--dir", "hub", "--out", "from-a", "--body", "g.md")
    assert rc == 0 and ex["nnn"] == "001"
    assert Path("from-a/001-envelope.json").is_file()
    assert Path("from-a/001-sig.txt").is_file()
    assert Path("from-a/001-body.md").read_text(encoding="utf-8") == "인사"
    # 번호 증가
    Path("g2.md").write_text("둘", encoding="utf-8")
    run(capsys, "message", "--dir", "hub", "--key", "lab-a.seed", "--signer", "lab:a",
        "--key-id", "k1", "--epoch", "1", "--to-lab", "lab:b", "--to-id", "Aria",
        "--to-epoch", "1", "--body-file", "g2.md")
    rc, ex2 = run(capsys, "export", "--dir", "hub", "--out", "from-a")
    assert ex2["nnn"] == "002"
    # ── 수신측: 별도 hub가 quad만으로 수용 ──
    _, a = run(capsys, "keygen", "recv-hub")
    rc, _ = run(capsys, "init", "--dir", "hub-b", "--source-domain", "lab:b/hub")
    rc, _ = run(capsys, "register-key", "--dir", "hub-b", "--signer", "lab:a",
                "--key-id", "k1", "--epoch", "1",
                "--pubkey", ex["pubkey"])
    rc, r2 = run(capsys, "admit", "--dir", "hub-b",
                 "--envelope", "from-a/001-envelope.json",
                 "--sig-file", "from-a/001-sig.txt", "--pubkey", ex["pubkey"])
    assert rc == 0 and r2["admitted"], r2
    assert r2["event_id"] == ex["event_id"]


def test_export_빈_로그와_sig_이중지정_거부(capsys, ws):
    _setup(capsys, ws)
    rc, _ = run(capsys, "export", "--dir", "hub", "--out", "o")
    assert rc == 2                                    # 빈 로그
    Path("g.md").write_text("x", encoding="utf-8")
    run(capsys, "message", "--dir", "hub", "--key", "lab-a.seed", "--signer", "lab:a",
        "--key-id", "k1", "--epoch", "1", "--to-lab", "lab:b", "--to-id", "c",
        "--to-epoch", "1", "--body-file", "g.md")
    run(capsys, "export", "--dir", "hub", "--out", "o")
    rc, _ = run(capsys, "admit", "--dir", "hub", "--envelope", "o/001-envelope.json",
                "--sig", "aa", "--sig-file", "o/001-sig.txt", "--pubkey", "b" * 64)
    assert rc == 2                                    # --sig/--sig-file 동시 지정


# ═══ Windows 첫 수확 — cp949 콘솔 크래시 (2026-08-16, Ray 랩 보고) ═══════════

def test_cp949_콘솔에서_출력이_크래시하지_않는다():
    """cp949 스트림에 em-dash·특수문자 출력 시 UnicodeEncodeError로 죽던 것 —
    _crashproof_console 후에는 못 담는 문자만 ?로 대체되고 한글은 보존된다."""
    import io
    buf = io.BytesIO()
    stream = io.TextIOWrapper(buf, encoding="cp949")
    with pytest.raises(UnicodeEncodeError):
        stream.write("서명 봉투 — 수용·영수증")           # 재현: em-dash가 죽인다
    stream2 = io.TextIOWrapper(io.BytesIO(), encoding="cp949")
    stream2.reconfigure(errors="replace")                  # 처방 그대로
    stream2.write("서명 봉투 — 수용·영수증 ✓")
    stream2.flush()
    out = stream2.buffer.getvalue().decode("cp949")
    assert "서명 봉투" in out and "수용" in out            # 한글 보존
    assert "?" in out                                      # 못 담는 문자만 대체


def test_crashproof가_main_경로에_걸려_있다(capsys, ws):
    cli._crashproof_console()                              # 예외 없이(비콘솔 포함)
    rc, _ = run(capsys, "keygen", "cp949-check")
    assert rc == 0


# ═══ signer.introduced — 신규 서명자 사후 도입 (0.4.2) ═══════════════════════

def test_introduce_signer_사후_도입_소급_정식화_무연쇄(capsys, ws):
    """log가 선 hub에 제3랩 signer를 admitted 이벤트로 도입(C1 우회 아님 —
    valid_from_seq=도입 좌표+1). ① 도입 전 admit은 거부(미등록) ② 도입 전에
    서명해 둔 봉투가 도입 뒤 admit으로 정식화(소급 정식화 명문) ③ 도입자 키
    revoke는 피도입자에 연쇄하지 않는다(도입=사실 기록) ④ was_valid 좌표 의미."""
    a_pub, b_pub, _ = _setup(capsys, ws)
    Path("f.txt").write_text("x", encoding="utf-8")
    rc, r = run(capsys, "attest", "--dir", "hub", "--key", "lab-a.seed",
                "--signer", "lab:a", "--key-id", "k1", "--epoch", "1",
                "--file", "f.txt", "--claim", "core:artifact.frozen", "--scope", "s")
    assert rc == 0 and r["admitted"]                      # seq 1 — log가 섰다

    _, c = run(capsys, "keygen", "lab-c")
    env_c = {"envelope_schema": he.ENVELOPE_SCHEMA, "event_kind": "message.read",
             "signer": {"id": "lab:c", "key_id": "k1", "key_epoch": 1},
             "subject": {"type": "run", "id": "run:intro-1"},
             "provenance": {"lab": "lab:c", "machine": "m-c01", "platform": "linux",
                            "adapter": "c-tooling/1.0", "cli_version": None,
                            "capture": None},
             "idempotency_key": "c-hello-1", "created_at": "2026-08-17T01:00:00Z",
             "payload": {"cursor": "c-0"}}
    Path("c-envelope.json").write_text(json.dumps(env_c, ensure_ascii=False),
                                       encoding="utf-8")
    rc, sig = run(capsys, "sign", "--key", "lab-c.seed",
                  "--envelope", "c-envelope.json")
    assert rc == 0                              # 도입 **전** 서명(게이트 수준 시절)

    # ① 도입 전 admit은 미등록 키로 거부
    rc, r0 = run(capsys, "admit", "--dir", "hub", "--envelope", "c-envelope.json",
                 "--sig", sig["sig"], "--pubkey", sig["pubkey"])
    assert rc == 1 and not r0["admitted"]

    # 도입 — hub 운영 lab(lab:a = source_domain lab)이 서명한 admitted 이벤트(I1)
    rc, intro = run(capsys, "introduce-signer", "--dir", "hub", "--key", "lab-a.seed",
                    "--signer", "lab:a", "--key-id", "k1", "--epoch", "1",
                    "--new-signer", "lab:c", "--new-key-id", "k1",
                    "--new-epoch", "1", "--new-pubkey", c["pubkey"])
    assert rc == 0 and intro["admitted"]
    n = intro["accepted_seq"]

    # ④ 효력은 n+1부터(C2 관례) — 도입 이벤트 좌표에서 무효, 다음부터 유효
    rc, w = run(capsys, "was-valid", "--dir", "hub", "--pubkey", c["pubkey"],
                "--seq", str(n))
    assert w["valid"] is False
    rc, w = run(capsys, "was-valid", "--dir", "hub", "--pubkey", c["pubkey"],
                "--seq", str(n + 1))
    assert w["valid"] is True

    # ② 소급 정식화 — 같은 봉투(도입 전 서명)가 이제 admit된다(재생 경유 = 지속성)
    rc, r1 = run(capsys, "admit", "--dir", "hub", "--envelope", "c-envelope.json",
                 "--sig", sig["sig"], "--pubkey", sig["pubkey"])
    assert rc == 0 and r1["admitted"]

    # ③ 무연쇄 — 도입자(lab:a) 키를 revoke해도 lab:c는 산다
    rc, rv = run(capsys, "revoke-key", "--dir", "hub", "--key", "lab-a.seed",
                 "--signer", "lab:a", "--key-id", "k1", "--epoch", "1",
                 "--revoke-key-id", "k1", "--revoke-epoch", "1",
                 "--reason", "무연쇄 검증")
    assert rc == 0 and rv["admitted"]
    env_c2 = dict(env_c, idempotency_key="c-hello-2", payload={"cursor": "c-1"})
    Path("c2.json").write_text(json.dumps(env_c2, ensure_ascii=False),
                               encoding="utf-8")
    rc, sig2 = run(capsys, "sign", "--key", "lab-c.seed", "--envelope", "c2.json")
    rc, r2 = run(capsys, "admit", "--dir", "hub", "--envelope", "c2.json",
                 "--sig", sig2["sig"], "--pubkey", sig2["pubkey"])
    assert rc == 0 and r2["admitted"]


def test_introduce_signer_음성_4종_자기도입_기존signer_결속pubkey_grammar(capsys, ws):
    """음성: 자기 도입(rotate 전용)·이미 결속 있는 signer(남의 authority 확장 금지)·
    이미 결속된 pubkey(조용한 재사용 금지)·lab grammar 밖(payload shape)."""
    a_pub, b_pub, _ = _setup(capsys, ws)
    _, x = run(capsys, "keygen", "lab-x")
    base = ["introduce-signer", "--dir", "hub", "--key", "lab-a.seed",
            "--signer", "lab:a", "--key-id", "k1", "--epoch", "1"]
    for tail, why in [
        (["--new-signer", "lab:a", "--new-key-id", "k9", "--new-epoch", "1",
          "--new-pubkey", x["pubkey"]], "자기 도입"),
        (["--new-signer", "lab:b", "--new-key-id", "k9", "--new-epoch", "1",
          "--new-pubkey", x["pubkey"]], "기존 signer"),
        (["--new-signer", "lab:y", "--new-key-id", "k1", "--new-epoch", "1",
          "--new-pubkey", a_pub], "결속 pubkey"),
        (["--new-signer", "no-prefix", "--new-key-id", "k1", "--new-epoch", "1",
          "--new-pubkey", x["pubkey"]], "lab grammar"),
    ]:
        rc, r = run(capsys, *base, *tail)
        assert rc == 1 and not r["admitted"], why


def test_i1_도입_authority는_hub_운영_lab만_재위임과_선점_불가(capsys, ws):
    """[Orin I1] ① 등록 peer(lab:b)의 도입 → 거부 + 로그·registry 무전이(선점 실패)
    ② 거부가 seq를 안 먹었으므로 뒤의 local 도입이 정상 성립(선점 뒤 정상 도입)
    ③ 정식 도입된 lab:c의 재위임(lab:d 도입) → 거부(권한 자동 상속 없음)
    ④ replay(새 CLI 호출) 후 동일 상태."""
    a_pub, b_pub, _ = _setup(capsys, ws)
    Path("f.txt").write_text("x", encoding="utf-8")
    rc, r = run(capsys, "attest", "--dir", "hub", "--key", "lab-a.seed",
                "--signer", "lab:a", "--key-id", "k1", "--epoch", "1",
                "--file", "f.txt", "--claim", "core:artifact.frozen", "--scope", "s")
    assert rc == 0 and r["accepted_seq"] == 1

    _, c = run(capsys, "keygen", "lab-c")
    _, d = run(capsys, "keygen", "lab-d")
    _, x = run(capsys, "keygen", "lab-x")

    # ① peer lab:b가 lab:c를 제 선택 pubkey로 선점 시도 → 거부
    rc, r = run(capsys, "introduce-signer", "--dir", "hub", "--key", "lab-b.seed",
                "--signer", "lab:b", "--key-id", "k1", "--epoch", "1",
                "--new-signer", "lab:c", "--new-key-id", "k1", "--new-epoch", "1",
                "--new-pubkey", x["pubkey"])
    assert rc == 1 and not r["admitted"]
    assert any("authority" in p for p in r["problems"])

    # ② local(lab:a) 도입이 seq 2 — 거부가 로그를 안 전진시킨 증거 + 선점 무효
    rc, intro = run(capsys, "introduce-signer", "--dir", "hub", "--key", "lab-a.seed",
                    "--signer", "lab:a", "--key-id", "k1", "--epoch", "1",
                    "--new-signer", "lab:c", "--new-key-id", "k1", "--new-epoch", "1",
                    "--new-pubkey", c["pubkey"])
    assert rc == 0 and intro["admitted"] and intro["accepted_seq"] == 2

    # ③ 도입된 lab:c의 재위임 시도 → 거부(재귀 membership CA 금지)
    rc, r = run(capsys, "introduce-signer", "--dir", "hub", "--key", "lab-c.seed",
                "--signer", "lab:c", "--key-id", "k1", "--epoch", "1",
                "--new-signer", "lab:d", "--new-key-id", "k1", "--new-epoch", "1",
                "--new-pubkey", d["pubkey"])
    assert rc == 1 and not r["admitted"]
    assert any("authority" in p for p in r["problems"])

    # ④ replay: 새 호출이 재생 경유 — c 유효·d 미등록 유지
    rc, w = run(capsys, "was-valid", "--dir", "hub", "--pubkey", c["pubkey"],
                "--seq", str(intro["accepted_seq"] + 1))
    assert w["valid"] is True
    rc, w = run(capsys, "was-valid", "--dir", "hub", "--pubkey", d["pubkey"],
                "--seq", "9")
    assert w["valid"] is None


def test_i1_r3_bare_name_source_domain은_bootstrap_교차확인으로_파생(capsys, ws):
    """[Ludex r2 반증 교정] 공개 CLI가 허용하는 bare name source_domain("ludex")
    hub에서 ① 운영 lab(lab:ludex, bootstrap 결속)의 도입이 성립하고 replay가
    산다(r2에서는 authority=None → 정당 도입 거부 → hub 동결) ② 다른 bootstrap
    peer(lab:ray)는 여전히 도입 불가 ③ bare name과 일치하는 bootstrap 서명자가
    없으면 fail-closed 유지."""
    _, lx = run(capsys, "keygen", "ludex")
    _, ry = run(capsys, "keygen", "ray")
    rc, _ = run(capsys, "init", "--dir", "hub", "--source-domain", "ludex")  # bare
    assert rc == 0
    for signer, pub in (("lab:ludex", lx["pubkey"]), ("lab:ray", ry["pubkey"])):
        rc, _ = run(capsys, "register-key", "--dir", "hub", "--signer", signer,
                    "--key-id", "k1", "--epoch", "1", "--pubkey", pub)
        assert rc == 0
    Path("f.txt").write_text("x", encoding="utf-8")
    rc, r = run(capsys, "attest", "--dir", "hub", "--key", "ludex.seed",
                "--signer", "lab:ludex", "--key-id", "k1", "--epoch", "1",
                "--file", "f.txt", "--claim", "core:artifact.frozen", "--scope", "s")
    assert rc == 0 and r["accepted_seq"] == 1              # log가 섰다

    _, c = run(capsys, "keygen", "lab-c")
    # ② 다른 bootstrap peer는 authority 아님
    rc, r = run(capsys, "introduce-signer", "--dir", "hub", "--key", "ray.seed",
                "--signer", "lab:ray", "--key-id", "k1", "--epoch", "1",
                "--new-signer", "lab:c", "--new-key-id", "k1", "--new-epoch", "1",
                "--new-pubkey", c["pubkey"])
    assert rc == 1 and not r["admitted"]
    # ① 운영 lab 도입 성립 (Ludex 라이브 seq 6과 같은 모양)
    rc, intro = run(capsys, "introduce-signer", "--dir", "hub", "--key", "ludex.seed",
                    "--signer", "lab:ludex", "--key-id", "k1", "--epoch", "1",
                    "--new-signer", "lab:c", "--new-key-id", "k1", "--new-epoch", "1",
                    "--new-pubkey", c["pubkey"])
    assert rc == 0 and intro["admitted"] and intro["accepted_seq"] == 2
    # replay 생존 — r2에서는 이 호출이 "로그 손상"으로 죽었다
    rc, w = run(capsys, "was-valid", "--dir", "hub", "--pubkey", c["pubkey"],
                "--seq", str(intro["accepted_seq"] + 1))
    assert rc == 0 and w["valid"] is True

    # ③ bare name과 일치하는 bootstrap 서명자가 없으면 fail-closed
    _, o = run(capsys, "keygen", "lab-other")
    rc, _ = run(capsys, "init", "--dir", "hub2", "--source-domain", "solo")
    assert rc == 0
    rc, _ = run(capsys, "register-key", "--dir", "hub2", "--signer", "lab:other",
                "--key-id", "k1", "--epoch", "1", "--pubkey", o["pubkey"])
    assert rc == 0
    rc, r = run(capsys, "introduce-signer", "--dir", "hub2", "--key", "lab-other.seed",
                "--signer", "lab:other", "--key-id", "k1", "--epoch", "1",
                "--new-signer", "lab:c", "--new-key-id", "k1", "--new-epoch", "1",
                "--new-pubkey", c["pubkey"])
    assert rc == 1 and not r["admitted"]
    assert any("authority" in p for p in r["problems"])


# ═══ 0.4.5 — verify-envelope(장부 무접촉) + admit 비수신자 기본 거부 ═══════════

def _msg_env(frm, to_lab, body: bytes):
    return {"envelope_schema": he.ENVELOPE_SCHEMA, "event_kind": "message.posted",
            "signer": {"id": frm, "key_id": "k1", "key_epoch": 1},
            "subject": {"type": "message", "id": "message:t-1"},
            "provenance": {"lab": frm, "machine": "m-t01", "platform": "linux",
                           "adapter": "t/1.0", "cli_version": None, "capture": None},
            "idempotency_key": f"t-{to_lab}-1", "created_at": "2026-08-18T01:00:00Z",
            "payload": {"body_locator": None, "body_media_type": "text/markdown",
                        "body_sha256": hashlib.sha256(body).hexdigest(),
                        "target": {"lab_id": to_lab, "to_id": "Cody", "to_epoch": 1}}}


def test_verify_envelope는_장부_무접촉_검증(capsys, ws):
    """[0.4.5 LxM 제안] 서명·event_id·스키마·body digest 검증만 — hub 디렉터리를
    받지 않으며 어떤 장부도 만들거나 건드리지 않는다."""
    _, k = run(capsys, "keygen", "lab-v")
    body = "회람 본문".encode("utf-8")
    Path("v-body.md").write_bytes(body)
    env = _msg_env("lab:v", "lab:other", body)
    Path("v-env.json").write_text(json.dumps(env, ensure_ascii=False), encoding="utf-8")
    rc, sig = run(capsys, "sign", "--key", "lab-v.seed", "--envelope", "v-env.json")
    rc, r = run(capsys, "verify-envelope", "--envelope", "v-env.json",
                "--sig", sig["sig"], "--pubkey", sig["pubkey"],
                "--body", "v-body.md")
    assert rc == 0 and r["valid_signature"] is True
    assert r["schema_problems"] == [] and r["body_sha256_match"] is True
    assert r["target"]["lab_id"] == "lab:other" and r["ledger_touched"] is False
    assert not (ws / "hub").exists()                      # 장부 무접촉의 물증
    # 변조 서명은 실패로
    bad = ("0" * 128) if sig["sig"][0] != "0" else ("1" * 128)
    rc, r = run(capsys, "verify-envelope", "--envelope", "v-env.json",
                "--sig", bad, "--pubkey", sig["pubkey"])
    assert rc == 1 and r["valid_signature"] is False


def test_admit_비수신자_기본_거부_회람은_명시_플래그(capsys, ws):
    """[0.4.5 실사고 3건] addressed 봉투의 target lab ≠ 운영 lab이면 기본 거부 +
    로그 무전이; --accept-foreign-target 명시 시에만 회람 증인으로 수용."""
    a_pub, b_pub, _ = _setup(capsys, ws)                  # hub = lab:a/hub
    body = b"foreign"
    for to_lab, name in (("lab:zzz", "f"), ("lab:a", "m")):
        env = _msg_env("lab:b", to_lab, body)
        Path(f"{name}.json").write_text(json.dumps(env, ensure_ascii=False),
                                        encoding="utf-8")
    rc, s_f = run(capsys, "sign", "--key", "lab-b.seed", "--envelope", "f.json")
    rc, s_m = run(capsys, "sign", "--key", "lab-b.seed", "--envelope", "m.json")

    # ① 비수신자 → 기본 거부, 로그 무전이
    rc, _out = run(capsys, "admit", "--dir", "hub", "--envelope", "f.json",
                   "--sig", s_f["sig"], "--pubkey", s_f["pubkey"])
    assert rc == 2                       # HubCliError 거부(메시지는 stderr)
    # ② 자기 수신 → 평소대로 admit (거부가 seq를 안 먹은 증거 = seq 1)
    rc, r = run(capsys, "admit", "--dir", "hub", "--envelope", "m.json",
                "--sig", s_m["sig"], "--pubkey", s_m["pubkey"])
    assert rc == 0 and r["admitted"] and r["accepted_seq"] == 1
    # ③ 회람 증인은 명시 플래그로
    rc, r = run(capsys, "admit", "--dir", "hub", "--envelope", "f.json",
                "--sig", s_f["sig"], "--pubkey", s_f["pubkey"],
                "--accept-foreign-target")
    assert rc == 0 and r["admitted"] and r["accepted_seq"] == 2


def test_r2_own_파생불가_hub는_addressed_admit_fail_closed(capsys, ws):
    """[LxM R1·Orin 반례] 운영 lab 파생이 None인 hub(bare name·무결속)에서
    addressed 봉투가 무플래그로 통과하던 fail-open 폐쇄 — 파생 불가 = 기본 거부,
    로그 무전이; 수용은 명시 플래그로만."""
    _, o = run(capsys, "keygen", "lab-o")
    rc, _ = run(capsys, "init", "--dir", "hub", "--source-domain", "solo")  # 파생 불가
    assert rc == 0
    rc, _ = run(capsys, "register-key", "--dir", "hub", "--signer", "lab:other",
                "--key-id", "k1", "--epoch", "1", "--pubkey", o["pubkey"])
    assert rc == 0
    body = b"x"
    env = _msg_env("lab:other", "lab:zzz", body)
    Path("z.json").write_text(json.dumps(env, ensure_ascii=False), encoding="utf-8")
    rc, s = run(capsys, "sign", "--key", "lab-o.seed", "--envelope", "z.json")
    # 무플래그 → fail-closed 거부 (종전: admitted seq 1이 나오던 P4 반례)
    rc, _out = run(capsys, "admit", "--dir", "hub", "--envelope", "z.json",
                   "--sig", s["sig"], "--pubkey", s["pubkey"])
    assert rc == 2
    # 명시 플래그 → 수용, 그리고 거부가 seq를 안 먹었다는 증거(seq 1)
    rc, r = run(capsys, "admit", "--dir", "hub", "--envelope", "z.json",
                "--sig", s["sig"], "--pubkey", s["pubkey"],
                "--accept-foreign-target")
    assert rc == 0 and r["admitted"] and r["accepted_seq"] == 1


def test_list는_tofu와_소개받은_서명자를_구분해_보여준다(capsys, ws):
    """[Ludex 0.4.7 지적] 원장이 두 등급을 똑같이 인쇄하면 TOFU가 '결정'이 아니라
    기본값이 된 것을 세기 전엔 모른다. bootstrap(첫인상)과 introduced(hub 안의
    누군가가 보증)는 신뢰 근거가 다르므로 표면에서 갈라야 한다."""
    a_pub, b_pub, _ = _setup(capsys, ws)          # lab:a·lab:b = init 때 등록 = tofu
    Path("f.txt").write_text("x", encoding="utf-8")
    run(capsys, "attest", "--dir", "hub", "--key", "lab-a.seed", "--signer", "lab:a",
        "--key-id", "k1", "--epoch", "1", "--file", "f.txt",
        "--claim", "core:artifact.frozen", "--scope", "s")
    _, c = run(capsys, "keygen", "lab-c")
    rc, intro = run(capsys, "introduce-signer", "--dir", "hub", "--key", "lab-a.seed",
                    "--signer", "lab:a", "--key-id", "k1", "--epoch", "1",
                    "--new-signer", "lab:c", "--new-key-id", "k1", "--new-epoch", "1",
                    "--new-pubkey", c["pubkey"])
    assert rc == 0
    env_c = {"envelope_schema": he.ENVELOPE_SCHEMA, "event_kind": "message.read",
             "signer": {"id": "lab:c", "key_id": "k1", "key_epoch": 1},
             "subject": {"type": "run", "id": "run:g-1"},
             "provenance": {"lab": "lab:c", "machine": "m-c", "platform": "linux",
                            "adapter": "t/1.0", "cli_version": None, "capture": None},
             "idempotency_key": "c-g-1", "created_at": "2026-08-21T01:00:00Z",
             "payload": {"cursor": "c-0"}}
    Path("cg.json").write_text(json.dumps(env_c, ensure_ascii=False), encoding="utf-8")
    rc, sig = run(capsys, "sign", "--key", "lab-c.seed", "--envelope", "cg.json")
    rc, _ = run(capsys, "admit", "--dir", "hub", "--envelope", "cg.json",
                "--sig", sig["sig"], "--pubkey", sig["pubkey"])
    assert rc == 0
    rc, out = run(capsys, "list", "--dir", "hub")
    assert rc == 0
    lines = [l for l in str(out).splitlines() if l.strip()]
    a_rows = [l for l in lines if "lab:a" in l]   # hub = lab:a/hub → 운영 lab
    c_rows = [l for l in lines if "lab:c" in l]
    assert a_rows and all("self" in l for l in a_rows)          # 운영 lab = 자기 자신
    assert c_rows and all("introduced@" in l for l in c_rows)   # 보증받은 서명자


def test_admit는_등록_signer의_pubkey를_registry에서_파생한다(capsys, ws):
    """[0.4.8 실사고] 운영자가 축약 지문에서 키를 재구성해 넘겨 여섯 봉투가 서명 실패로
    떨어졌다 — 앞뒤 자리만 맞고 가운데가 허구였다. registry가 (signer,key_id,epoch)
    결속을 이미 쥐고 있으므로: ①등록 signer는 --pubkey 생략 가능 ②잘못된 키는 서명
    검증 전에 'registry 결속과 다르다'로 이름 붙어 떨어진다 ③미등록 signer는 여전히
    명시적 키를 요구한다(첫인상은 결정이어야 한다)."""
    _setup(capsys, ws)
    env = {"envelope_schema": he.ENVELOPE_SCHEMA, "event_kind": "message.read",
           "signer": {"id": "lab:b", "key_id": "k1", "key_epoch": 1},
           "subject": {"type": "run", "id": "run:d-1"},
           "provenance": {"lab": "lab:b", "machine": "m-b", "platform": "linux",
                          "adapter": "t/1.0", "cli_version": None, "capture": None},
           "idempotency_key": "b-derive-1", "created_at": "2026-08-22T01:00:00Z",
           "payload": {"cursor": "c-1"}}
    Path("d.json").write_text(json.dumps(env, ensure_ascii=False), encoding="utf-8")
    rc, sig = run(capsys, "sign", "--key", "lab-b.seed", "--envelope", "d.json")
    # ① pubkey 생략 → registry 파생으로 admit 성공
    rc, out = run(capsys, "admit", "--dir", "hub", "--envelope", "d.json",
                  "--sig", sig["sig"])
    assert rc == 0 and out["admitted"] is True
    # ② 가운데가 허구인 그럴듯한 키 → 서명 단계가 아니라 registry 대조에서 이름 붙어 실패
    fake = sig["pubkey"][:8] + "0" * 52 + sig["pubkey"][-4:]
    env["idempotency_key"] = "b-derive-2"
    Path("d.json").write_text(json.dumps(env, ensure_ascii=False), encoding="utf-8")
    rc, sig2 = run(capsys, "sign", "--key", "lab-b.seed", "--envelope", "d.json")
    try:
        rc2 = cli.main(["admit", "--dir", "hub", "--envelope", "d.json",
                        "--sig", sig2["sig"], "--pubkey", fake])
    except SystemExit as e:
        rc2 = e.code
    cap = capsys.readouterr()
    assert rc2 == 2 and "registry 결속과 다르다" in cap.err
    # ③ 미등록 signer + 생략 → 명시적 키 요구
    _, c = run(capsys, "keygen", "lab-x")
    env_x = dict(env, signer={"id": "lab:x", "key_id": "k1", "key_epoch": 1},
                 idempotency_key="x-derive-1",
                 provenance=dict(env["provenance"], lab="lab:x"))
    Path("x.json").write_text(json.dumps(env_x, ensure_ascii=False), encoding="utf-8")
    rc, sigx = run(capsys, "sign", "--key", "lab-x.seed", "--envelope", "x.json")
    try:
        rcx = cli.main(["admit", "--dir", "hub", "--envelope", "x.json",
                        "--sig", sigx["sig"]])
    except SystemExit as e:
        rcx = e.code
    cap = capsys.readouterr()
    assert rcx == 2 and "registry에 결속이 없다" in cap.err
