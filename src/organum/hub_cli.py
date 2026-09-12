#!/usr/bin/env python3
"""organum-hub — 서명 증거 봉투 CLI (experimental).

두 집단(랩)이 봉투를 주고받고 서로 검증하는 루프의 표면이다:

    organum-hub keygen mylab                      # 키 생성 (seed 0600 + pub)
    organum-hub init --dir hub --source-domain lab:me/hub
    organum-hub register-key --dir hub --signer lab:me --key-id k1 --epoch 1 --pubkey …
    organum-hub attest --dir hub --key mylab.seed --signer lab:me --key-id k1 --epoch 1 \\
                --file prereg.json --claim core:artifact.frozen --scope demo
    organum-hub admit --dir hub --envelope their.json --sig … --pubkey …   # 상대 봉투
    organum-hub prove --dir hub --event-id …      # 포함 증명 → verify-proof로 오프라인 검증
    organum-hub rotate-key / revoke-key / was-valid                        # key lifecycle
    organum-hub serve / push / pull / channels    # git 없는 전달 — HTTP 우체통(drop v0)

## 상태 모델 — 로그가 곧 상태다

상태 디렉터리에는 `hub.json`(source_domain·claim registry·bootstrap 키)과
`events.jsonl`(admitted 이벤트의 exact bytes + 서명, append-only)만 있다. 매 실행은
로그를 **재생(replay)** 해 index를 결정론적으로 복원한다 — 파생 상태를 저장하지 않으므로
상태와 로그가 갈라질 수 없다(랩 규모 수백 이벤트에서 재생은 싸다).

secret 규율: seed 파일은 0600으로 만들고, 상태 디렉터리에는 **절대 저장하지 않는다**
(receipt 서명 키도 `--receipt-key`로 실행 시에만 받는다). 산출물에 seed가 실리지 않는다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import time
from pathlib import Path

try:
    from organum import hub_drop as hd
    from organum import hub_envelope as he
    from organum import hub_log as hl
    from organum import hub_ops as ho
    from organum import hub_wire as hw
    from organum import schnorr_pure as sp
except ImportError:                                    # 스크립트 직접 실행 경로
    import hub_drop as hd
    import hub_envelope as he
    import hub_log as hl
    import hub_ops as ho
    import hub_wire as hw
    import schnorr_pure as sp

DEFAULT_CLAIMS = {"claims": {
    "core:artifact.frozen": {
        "act_class": "self_attesting", "subject_types": ["artifact"],
        "ordering_levels": ["emission"], "capture_required": False,
        "revocation_authority": "same_signer"},
}}


class HubCliError(SystemExit):
    def __init__(self, msg):
        print(f"organum-hub: {msg}", file=sys.stderr)
        super().__init__(2)


def _now_z() -> str:
    return ho.now_z()


def _ops(fn, *args, **kw):
    """hub_ops 호출의 구조화 오류 → CLI 오류(종전 문구 그대로 stderr, exit 2)."""
    try:
        return fn(*args, **kw)
    except ho.HubOpsError as e:
        raise HubCliError(str(e))


def _read_seed(path) -> bytes:
    return _ops(ho.read_seed, path)


# ── 상태 로드/저장 — 본체는 hub_ops(0.6.0), 여기는 오류 번역만 ─────────────

def _load(dirpath, *, receipt_seckey=None):
    return _ops(ho.load_hub, dirpath, receipt_seckey=receipt_seckey)


_append = ho.append_record
_admit_and_log = ho.admit_and_log


def _print_result(r: dict):
    out = {k: r[k] for k in ("admitted", "duplicate", "event_id", "accepted_seq",
                             "authority_projected")}
    if not r["admitted"]:
        out["stage"], out["problems"] = r["stage"], r["problems"]
    if r.get("receipt"):
        out["receipt"] = r["receipt"]
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if r["admitted"] else 1


def _build_envelope(cfg, kind, payload, *, signer, key_id, epoch, subject):
    return ho.build_envelope(cfg, kind, payload, signer=signer, key_id=key_id,
                             epoch=epoch, subject=subject)


def _sign_admit(d, cfg, hub, env, seed) -> int:
    return _print_result(ho.sign_and_admit(d, cfg, hub, env, seed))


# ── subcommands ──────────────────────────────────────────────────────────────

def cmd_keygen(a):
    seed = secrets.token_bytes(32)
    seed_p = Path(f"{a.name}.seed")
    if seed_p.exists():
        raise HubCliError(f"{seed_p} 이미 존재 — 덮어쓰지 않는다")
    fd = os.open(seed_p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(seed.hex())
    pub = sp.public_key(seed).hex()
    Path(f"{a.name}.pub").write_text(pub + "\n", encoding="utf-8")
    print(json.dumps({"seed_file": str(seed_p), "pubkey": pub}))
    return 0


def cmd_init(a):
    d = Path(a.dir)
    if (d / "hub.json").exists():
        raise HubCliError(f"{d}/hub.json 이미 존재")
    d.mkdir(parents=True, exist_ok=True)
    claims_doc = (json.loads(Path(a.claims).read_text(encoding="utf-8"))
                  if a.claims else DEFAULT_CLAIMS)
    cfg = {"source_domain": a.source_domain, "claims": claims_doc,
           "claims_sha256": he.canonical_sha(claims_doc),
           "machine_id": "m-" + secrets.token_hex(8),      # §6: CSPRNG, 경로/계정 비파생
           "keys": []}
    (d / "hub.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1) + "\n",
                                encoding="utf-8")
    (d / "events.jsonl").touch()
    print(json.dumps({"dir": str(d), "source_domain": a.source_domain,
                      "claims_sha256": cfg["claims_sha256"]}))
    return 0


def cmd_register_key(a):
    """bootstrap provisioning — **admitted log가 비어 있을 때만**(Orin C1).

    seq가 한 번이라도 섰으면 새 키는 valid_from_seq=0 bootstrap으로 소급 편입되는
    lifecycle/log 우회가 되므로 거부하고 `rotate-key`를 안내한다. 거부 경로에서는
    hub.json bytes가 바뀌지 않는다(검증이 쓰기보다 먼저)."""
    d, cfg, hub = _load(a.dir)                              # 검증 겸 로드
    if hub.log is not None and hub.log.tree_size > 0:
        raise HubCliError(
            "admitted 이벤트가 이미 있어 bootstrap 등록 불가 — 기존 signer의 새 키는 "
            "`organum-hub rotate-key`, 신규 signer 도입은 "
            "`organum-hub introduce-signer`(둘 다 admitted 이벤트로 lifecycle 전이)")
    # 쓰기 전에 registry 불변식으로 검증(shape·pubkey/tuple 유일성) — 실패 시 무변.
    probe = he.KeyRegistry()
    for k in cfg["keys"]:
        probe.register(k["pubkey"], signer_id=k["signer_id"], key_id=k["key_id"],
                       key_epoch=k["key_epoch"])
    try:
        probe.register(a.pubkey, signer_id=a.signer, key_id=a.key_id,
                       key_epoch=a.epoch)
    except he.HubEnvelopeError as e:
        raise HubCliError(f"등록 거부: {e}")
    cfg["keys"].append({"pubkey": a.pubkey, "signer_id": a.signer,
                        "key_id": a.key_id, "key_epoch": a.epoch})
    (d / "hub.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1) + "\n",
                                encoding="utf-8")
    print(json.dumps({"registered": a.pubkey, "signer": a.signer}))
    return 0


def cmd_attest(a):
    seed = _read_seed(a.key)
    d, cfg, hub = _load(a.dir, receipt_seckey=_read_seed(a.receipt_key)
                        if a.receipt_key else None)
    f = Path(a.file)
    if not f.is_file():
        raise HubCliError(f"파일 없음: {a.file}")
    data = f.read_bytes()
    subject_id = a.subject or ("artifact:" + "".join(
        c if c.isalnum() or c in "._-" else "-" for c in f.name))
    payload = {"artifact": {"role": a.role, "schema_id": a.schema_id,
                            "sha256": hashlib.sha256(data).hexdigest(),
                            "byte_length": len(data),
                            "media_type": a.media_type},
               "bindings": [], "causal": {},
               "claim": {"type": a.claim, "scope": a.scope,
                         "attests_ordering_of": "emission",
                         "evidence_basis": {"method": "raw-bytes-sha256",
                                            "verifier_schema": a.schema_id,
                                            "body_custody": "repo",
                                            "locator_authority": False}}}
    env = _build_envelope(cfg, "artifact.attested", payload, signer=a.signer,
                          key_id=a.key_id, epoch=a.epoch,
                          subject={"type": "artifact", "id": subject_id})
    return _sign_admit(d, cfg, hub, env, seed)


def cmd_message(a):
    """message.posted 봉투 빌더 — 만남의 기본 동사. hub는 본문을 싣지 않으므로(§5)
    본문 파일은 봉투와 **나란히** 보내고, 봉투가 (수신자, 본문 digest, 누가·언제)를
    서명으로 결속한다. 수신 쪽은 본문의 sha256을 봉투와 대조하면 된다."""
    seed = _read_seed(a.key)
    d, cfg, hub = _load(a.dir, receipt_seckey=_read_seed(a.receipt_key)
                        if a.receipt_key else None)
    body_p = Path(a.body_file)
    if not body_p.is_file():
        raise HubCliError(f"본문 파일 없음: {a.body_file}")
    env = ho.build_message_envelope(
        cfg, signer=a.signer, key_id=a.key_id, epoch=a.epoch,
        to_lab=a.to_lab, to_id=a.to_id, to_epoch=a.to_epoch,
        body=body_p.read_bytes(),
        body_locator=a.body_locator or f"file://{body_p.name}",
        media_type=a.media_type)
    return _sign_admit(d, cfg, hub, env, seed)


def cmd_sign(a):
    """봉투 파일에 서명만 — 상대에게 (envelope, sig, pubkey)를 건네는 발신 측 절반."""
    seed = _read_seed(a.key)
    env = json.loads(Path(a.envelope).read_text(encoding="utf-8"))
    raw = he.canonical_bytes(env)
    sig = sp.sign(hashlib.sha256(raw).digest(), seed)
    print(json.dumps({"envelope_sha256": hashlib.sha256(raw).hexdigest(),
                      "sig": sig.hex(), "pubkey": sp.public_key(seed).hex()}))
    return 0


def _registry_pubkey_for(hub, signer) -> str | None:
    """현재 활성 키만(admit 전용) — 본체 hub_ops.registry_pubkey_for(0.4.8·0.4.13)."""
    return ho.registry_pubkey_for(hub, signer)


def _registry_binding_for_audit(hub, signer) -> dict | None:
    """exact 결속(폐기 무관, 감사용) — 본체 hub_ops.registry_binding_for_audit."""
    return ho.registry_binding_for_audit(hub, signer)


def cmd_admit(a):
    d, cfg, hub = _load(a.dir, receipt_seckey=_read_seed(a.receipt_key)
                        if a.receipt_key else None)
    if bool(a.sig) == bool(a.sig_file):
        raise HubCliError("--sig 또는 --sig-file 중 하나만")
    sig = a.sig or Path(a.sig_file).read_text(encoding="utf-8").strip()
    env = json.loads(Path(a.envelope).read_text(encoding="utf-8"))
    reg_pub = _registry_pubkey_for(hub, env.get("signer"))
    if a.pubkey is None:
        if reg_pub is None:
            raise HubCliError(
                "--pubkey 필요 — 이 signer 좌표는 registry에 결속이 없다. "
                "첫인상(TOFU) admit은 키를 명시적으로 건네는 것이 맞다")
        pubkey = reg_pub
    else:
        if reg_pub is not None and a.pubkey != reg_pub:
            raise HubCliError(
                f"제공한 pubkey가 registry 결속과 다르다 — "
                f"registry {reg_pub[:16]}…, 제공 {a.pubkey[:16]}…. "
                "등록 signer는 --pubkey 생략이 안전하다(registry에서 파생)")
        pubkey = a.pubkey
    # 0.4.5(비수신자 admit 실사고 3건): addressed 봉투의 target lab이 이 hub의 운영
    # lab과 다르면 기본 거부 — 회람 증인 admit은 --accept-foreign-target으로 **명시**
    # 한다(admit=증인, 재판정=별도 행위 관례). 판정은 CLI 층이다: 봉투 층은 witness
    # hub를 금지하지 않으므로 정책이 아니라 소비 규율로 건다.
    # 층 분리 명문(Ludex 0.4.5 판정 ③): 이 게이트는 **수신(cmd_admit) 전용**이다 —
    # 발신(cmd_message 등 _sign_admit 자기서명 경로)은 타 lab을 target하는 것이
    # 본래 목적이므로 게이트 밖이 의도다. 리팩터 시 이 경계를 옮기지 말 것.
    tgt = ((env.get("payload") or {}).get("target") or {}) \
        if env.get("event_kind") in he.ADDRESSED_KINDS else {}
    if tgt.get("lab_id") and not a.accept_foreign_target:
        own = hub._introducer_authority()      # 운영 lab 파생(r3 규칙 공유)
        # r2(LxM R1·Orin 반례): own=None을 `and own`으로 단락시키면 fail-open —
        # 보호가 가장 필요한(파생 안 되는) hub가 정확히 보호를 못 받는다.
        # r3 계보 그대로 파생 불가 = fail-closed.
        if own is None:
            raise HubCliError(
                "운영 lab 파생 불가(fail-closed) — 이 hub의 source_domain에서 "
                "운영 lab을 세울 수 없어 수신자 판정이 불가합니다. addressed 봉투 "
                "수용은 --accept-foreign-target을 명시하세요")
        if tgt["lab_id"] != own:
            raise HubCliError(
                f"수신자가 아니다 — target {tgt['lab_id']}, 이 hub 운영 lab {own}. "
                "회람 증인으로 수용하려면 --accept-foreign-target을 명시하세요")
    return _print_result(_admit_and_log(d, hub, he.canonical_bytes(env), sig, pubkey))


def cmd_verify_envelope(a):
    """장부 무접촉 검증(0.4.5, LxM 제안) — 본체는 hub_ops.verify_quad(0.6.0).
    `--dir`이면 registry 결속에서 키를 파생하고 `--pubkey`는 대조만(0.4.12·0.4.13);
    hub 없는 첫인상(TOFU) 확인은 `--pubkey` 단독. 로그를 전진시키지 않는다."""
    if bool(a.sig) == bool(a.sig_file):
        raise HubCliError("--sig 또는 --sig-file 중 하나만")
    sig = a.sig or Path(a.sig_file).read_text(encoding="utf-8").strip()
    env = json.loads(Path(a.envelope).read_text(encoding="utf-8"))
    hub = _load(a.dir)[2] if a.dir else None           # 읽기 전용 replay
    body = Path(a.body).read_bytes() if a.body else None
    out = _ops(ho.verify_quad, env, sig, hub=hub, pubkey=a.pubkey, body=body)
    ok = out.pop("ok")
    print(json.dumps(out, ensure_ascii=False, indent=1))
    # 종료코드는 **암호적 사실 + 봉투 무결성**만 본다. 폐기된 키의 과거 봉투도
    # 서명은 참이므로 0이다 — lifecycle은 별도 필드로 읽는다(0.4.13, 판정 분리).
    return 0 if ok else 1


def cmd_rotate_key(a):
    seed = _read_seed(a.key)
    d, cfg, hub = _load(a.dir)
    env = _build_envelope(cfg, "key.rotated",
                          {"new_key_id": a.new_key_id, "new_key_epoch": a.new_epoch,
                           "new_pubkey": a.new_pubkey},
                          signer=a.signer, key_id=a.key_id, epoch=a.epoch,
                          subject={"type": "machine", "id": "machine:" + cfg["machine_id"]})
    return _sign_admit(d, cfg, hub, env, seed)


def cmd_introduce_signer(a):
    """신규 서명자 도입(0.4.2) — admitted 이벤트로 lifecycle 전이. **hub 운영
    lab(source_domain의 lab)만** 서명할 수 있다(Orin I1: 등록 peer 불가·권한 자동
    상속 없음). 결속의 valid_from_seq는 이벤트 좌표의 다음(n+1)이다 — bootstrap
    register-key(valid_from_seq=0 소급)와 달리 log 우회가 없어 C1과 같은 불변식을
    지킨다. 도입 전 게이트 수준으로 검증한 봉투는 도입 뒤 재-admit으로 정식화 가능.
    주의(소비자): 도입 결속은 hub.json에 안 쓰인다 — 로그 재생 투영으로 산다.
    유효 서명자 집합 = bootstrap keys + 로그 투영(진실 표면은 `list`)."""
    seed = _read_seed(a.key)
    d, cfg, hub = _load(a.dir)
    env = _build_envelope(cfg, "signer.introduced",
                          {"signer_id": a.new_signer, "key_id": a.new_key_id,
                           "key_epoch": a.new_epoch, "pubkey": a.new_pubkey},
                          signer=a.signer, key_id=a.key_id, epoch=a.epoch,
                          subject={"type": "machine", "id": "machine:" + cfg["machine_id"]})
    return _sign_admit(d, cfg, hub, env, seed)


def cmd_revoke_key(a):
    seed = _read_seed(a.key)
    d, cfg, hub = _load(a.dir)
    env = _build_envelope(cfg, "key.revoked",
                          {"key_id": a.revoke_key_id, "key_epoch": a.revoke_epoch,
                           "reason": a.reason},
                          signer=a.signer, key_id=a.key_id, epoch=a.epoch,
                          subject={"type": "machine", "id": "machine:" + cfg["machine_id"]})
    return _sign_admit(d, cfg, hub, env, seed)


def cmd_export(a):
    """우체통 quad 내보내기 — 본체는 hub_ops.export_quad(0.6.0): 번호 3~6자리 전체
    파싱·기존 파일 덮어쓰기 금지·범위 초과 명시 오류(Orin 040 §4 결함 수정)."""
    d, cfg, hub = _load(a.dir)
    r = _ops(ho.export_quad, d, a.out, event_id=a.event_id, body_path=a.body)
    print(json.dumps(r, ensure_ascii=False))
    return 0


def _signer_grade(hub, signer_id: str) -> str:
    """이 서명자의 키가 **어떻게 이 hub에 들어왔나** — 0.4.7(Ludex 지적).

    두 등급은 신뢰 근거가 다른데 원장이 똑같이 인쇄하고 있었다: bootstrap 결속
    (`valid_from_seq == 0`)은 init 때 우리가 **직접 받아들인 첫인상(TOFU)**이고,
    도입 결속(`> 0`)은 **이 hub 안의 누군가가 보증한** 것이다. Ludex 실측 —
    수용 54건 중 31건이 TOFU였고 **아무도 그렇게 결정한 적이 없었다**(소개하는 사람이
    없으면 기본값이 된다). 세기 전에 몰랐던 이유가 이 표시의 부재였다.

    첫 구현은 bootstrap 전부를 `tofu`로 셌는데, 우리 원장에 대보니 31건이 **전부
    우리 자신**이었다 — **자기 등록을 첫인상으로 세는 것도 거짓말**이라 셋으로 가른다:
    `self`(이 hub의 운영 lab) · `tofu`(남인데 init 때 직접 받아들임) ·
    `introduced@N`(hub 안의 누군가가 좌표 N에서 보증)."""
    bindings = hub.keys.bindings_of(signer_id)
    if not bindings:
        return "unregistered"
    if signer_id == hub._introducer_authority():
        return "self"                      # 자기 자신 — 신뢰 판단이 아니다
    if any(b["valid_from_seq"] == 0 for b in bindings):
        return "tofu"                      # 남을 init 때 직접 받아들임 = 첫인상
    return f"introduced@{min(b['valid_from_seq'] for b in bindings) - 1}"


def cmd_list(a):
    d, cfg, hub = _load(a.dir)
    for line in (d / "events.jsonl").read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        env = (json.loads(rec["raw"]) if rec["transport"] == "direct"
               else json.loads(rec["event"]["content"]))
        eid = he.event_id_of((rec["raw"] if rec["transport"] == "direct"
                              else rec["event"]["content"]).encode("utf-8"))
        r = hub.get(eid)
        print(f"{r['accepted_seq']:>4}  {env['event_kind']:<26} {env['signer']['id']:<20} "
              f"{_signer_grade(hub, env['signer']['id']):<14} {eid[:16]}…  "
              f"{'authority' if r['authority_projected'] else 'transport-only'}")
    return 0


def cmd_was_valid(a):
    d, cfg, hub = _load(a.dir)
    v = hub.keys.was_valid(a.pubkey, a.seq)
    print(json.dumps({"pubkey": a.pubkey, "seq": a.seq,
                      "valid": v, "note": None if v is not None else "미등록 키(관측 불가)"}))
    return 0


def cmd_prove(a):
    d, cfg, hub = _load(a.dir)
    rec = hub.get(a.event_id)
    if rec is None:
        raise HubCliError("event_id가 admitted 이벤트가 아님")
    idx = rec["accepted_seq"] - 1
    size = hub.log.tree_size
    print(json.dumps({"event_id": a.event_id, "leaf_index": idx, "tree_size": size,
                      "root": hub.log.root().hex(),
                      "proof": [p.hex() for p in hub.log.inclusion_proof(idx, size)]}))
    return 0


def cmd_verify_proof(a):
    env = json.loads(Path(a.envelope).read_text(encoding="utf-8"))
    pf = json.loads(Path(a.proof).read_text(encoding="utf-8"))
    raw = he.canonical_bytes(env)
    ok = hl.verify_inclusion(raw, pf["leaf_index"], pf["tree_size"],
                             [bytes.fromhex(p) for p in pf["proof"]],
                             bytes.fromhex(pf["root"]))
    print(json.dumps({"included": ok, "event_id": he.event_id_of(raw)}))
    return 0 if ok else 1


def cmd_receipt_verify(a):
    rc = json.loads(Path(a.receipt).read_text(encoding="utf-8"))
    ok = he.verify_relay_receipt(rc, bytes.fromhex(a.hub_pubkey),
                                 expected_source_domain=a.source_domain)
    print(json.dumps({"valid": ok}))
    return 0 if ok else 1


def cmd_wire_out(a):
    seed = _read_seed(a.key)
    env = json.loads(Path(a.envelope).read_text(encoding="utf-8"))
    ev = hw.build_wire_event(he.canonical_bytes(env), seckey=seed,
                             created_at=a.created_at or int(time.time()))
    print(json.dumps(ev, ensure_ascii=False))
    return 0


def cmd_wire_in(a):
    d, cfg, hub = _load(a.dir, receipt_seckey=_read_seed(a.receipt_key)
                        if a.receipt_key else None)
    ev = json.loads(Path(a.event).read_text(encoding="utf-8"))
    r = hw.admit_wire(hub, ev)
    if r["admitted"] and not r["duplicate"]:
        _append(d, {"transport": "wire", "event": ev})
    return _print_result(r)


def cmd_serve(a):
    """HTTP 우체통 서버 — git 없는 전달의 수신처. 한쪽(또는 중립 호스트)이 이 한
    줄을 올리면, 상대는 push/pull만으로 봉투를 주고받는다. 서버는 dumb하다:
    봉투를 검증하지 않는다(검증은 수신 hub의 admit). 기본 bind는 127.0.0.1 —
    외부 노출은 --bind 0.0.0.0을 **직접** 선택해야 한다. 토큰별 rate limit
    기본 60/분(hosted 비용 유계) — self-host P2P는 --rate-limit 0으로 꺼도 된다."""
    try:
        srv = hd.make_server(a.root, a.token_file, bind=a.bind, port=a.port,
                             rate_limit_per_minute=a.rate_limit)
    except (ValueError, OSError) as e:
        raise HubCliError(str(e))
    print(json.dumps({"profile": hd.DROP_PROFILE, "root": str(Path(a.root)),
                      "bind": a.bind, "port": srv.server_address[1],
                      "rate_limit_per_minute": a.rate_limit},
                     ensure_ascii=False), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


def cmd_push(a):
    token = hd.load_tokens(a.token_file)[0]
    st: dict = {}
    try:
        r = hd.push_quad(a.url, token, a.quad, timeout=a.timeout, stats=st,
                         warmup=not a.no_warmup,
                         allow_foreign_door=a.accept_foreign_door)
    except (ValueError, hd.DropError) as e:
        raise HubCliError(str(e))
    print(json.dumps({**r, **st}, ensure_ascii=False))
    return 0


def cmd_pull(a):
    token = hd.load_tokens(a.token_file)[0]
    st: dict = {}
    try:
        ns = hd.pull_quads(a.url, token, a.dest, since=a.since, timeout=a.timeout,
                           stats=st, warmup=not a.no_warmup)
    except (ValueError, hd.DropError) as e:
        raise HubCliError(str(e))
    print(json.dumps({"pulled": ns, "dest": str(Path(a.dest)), **st},
                     ensure_ascii=False))
    return 0


def cmd_channels(a):
    """서버의 channel/sender 트리 — 수거 목록을 기억이 아니라 서버에 묻는다(0.4.9)."""
    token = hd.load_tokens(a.token_file)[0]
    st: dict = {}
    try:
        r = hd.list_channels(a.url, token, timeout=a.timeout, stats=st,
                             warmup=not a.no_warmup)
    except (ValueError, hd.DropError) as e:
        raise HubCliError(str(e))
    print(json.dumps({**r, **st}, ensure_ascii=False))
    return 0


def _crashproof_console():
    """Windows 첫 실측 수확(2026-08-16, Ray 랩): cp949 콘솔에서 help의 em-dash가
    UnicodeEncodeError로 CLI를 죽였다. cp949는 한글을 다 담으므로 인코딩은 그대로 두고
    **담지 못하는 문자만 ?로 대체** — 어떤 콘솔 인코딩에서도 출력이 크래시하지 않는다."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass


_WRITE_CMDS = {"register-key", "attest", "message", "admit", "rotate-key",
               "introduce-signer", "revoke-key", "wire-in"}


def main(argv=None) -> int:
    _crashproof_console()
    ap = argparse.ArgumentParser(
        prog="organum-hub",
        description="서명 증거 봉투 — 수용·영수증·투명 로그 (experimental)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    # (이름, 핸들러, [(플래그, kwargs), ...]) — 평평한 선언부
    SPECS = [
        ("keygen", cmd_keygen, [("name", {})]),
        ("init", cmd_init, [("--dir", {"required": True}),
                            ("--source-domain", {"required": True}),
                            ("--claims", {"default": None})]),
        ("register-key", cmd_register_key, [("--dir", {"required": True}),
                                            ("--signer", {"required": True}),
                                            ("--key-id", {"required": True}),
                                            ("--epoch", {"type": int, "required": True}),
                                            ("--pubkey", {"required": True})]),
        ("attest", cmd_attest, [("--dir", {"required": True}),
                                ("--key", {"required": True}),
                                ("--signer", {"required": True}),
                                ("--key-id", {"required": True}),
                                ("--epoch", {"type": int, "required": True}),
                                ("--file", {"required": True}),
                                ("--claim", {"required": True}),
                                ("--scope", {"required": True}),
                                ("--subject", {"default": None}),
                                ("--role", {"default": "artifact"}),
                                ("--schema-id", {"default": "raw/v1"}),
                                ("--media-type", {"default": "application/octet-stream"}),
                                ("--receipt-key", {"default": None})]),
        ("message", cmd_message, [("--dir", {"required": True}),
                                  ("--key", {"required": True}),
                                  ("--signer", {"required": True}),
                                  ("--key-id", {"required": True}),
                                  ("--epoch", {"type": int, "required": True}),
                                  ("--to-lab", {"required": True}),
                                  ("--to-id", {"required": True}),
                                  ("--to-epoch", {"type": int, "required": True}),
                                  ("--body-file", {"required": True}),
                                  ("--body-locator", {"default": None}),
                                  ("--media-type", {"default": "text/markdown"}),
                                  ("--receipt-key", {"default": None})]),
        ("sign", cmd_sign, [("--key", {"required": True}),
                            ("--envelope", {"required": True})]),
        ("admit", cmd_admit, [("--dir", {"required": True}),
                              ("--envelope", {"required": True}),
                              ("--sig", {"default": None}),
                              ("--sig-file", {"default": None}),
                              ("--pubkey", {"default": None}),
                              ("--receipt-key", {"default": None}),
                              ("--accept-foreign-target",
                               {"action": "store_true"})]),
        ("verify-envelope", cmd_verify_envelope,
         [("--envelope", {"required": True}),
          ("--sig", {"default": None}),
          ("--sig-file", {"default": None}),
          ("--pubkey", {"default": None}),
          ("--dir", {"default": None}),
          ("--body", {"default": None})]),
        ("export", cmd_export, [("--dir", {"required": True}),
                                ("--out", {"required": True}),
                                ("--event-id", {"default": None}),
                                ("--body", {"default": None})]),
        ("serve", cmd_serve, [("--root", {"required": True}),
                              ("--token-file", {"required": True}),
                              ("--bind", {"default": "127.0.0.1"}),
                              ("--port", {"type": int, "default": 8642}),
                              ("--rate-limit",
                               {"type": int,
                                "default": hd.RATE_LIMIT_PER_MINUTE})]),
        ("push", cmd_push, [("--url", {"required": True}),
                            ("--quad", {"required": True}),
                            ("--token-file", {"required": True}),
                            ("--timeout",
                             {"type": int,
                              "default": hd.CLIENT_TIMEOUT_SECONDS}),
                            ("--no-warmup", {"action": "store_true"}),
                            ("--accept-foreign-door",
                             {"action": "store_true"})]),
        ("pull", cmd_pull, [("--url", {"required": True}),
                            ("--dest", {"required": True}),
                            ("--token-file", {"required": True}),
                            ("--since", {"default": None}),
                            ("--timeout",
                             {"type": int,
                              "default": hd.CLIENT_TIMEOUT_SECONDS}),
                            ("--no-warmup", {"action": "store_true"})]),
        ("channels", cmd_channels, [("--url", {"required": True}),
                                    ("--token-file", {"required": True}),
                                    ("--timeout",
                                     {"type": int,
                                      "default": hd.CLIENT_TIMEOUT_SECONDS}),
                                    ("--no-warmup", {"action": "store_true"})]),
        ("rotate-key", cmd_rotate_key, [("--dir", {"required": True}),
                                        ("--key", {"required": True}),
                                        ("--signer", {"required": True}),
                                        ("--key-id", {"required": True}),
                                        ("--epoch", {"type": int, "required": True}),
                                        ("--new-key-id", {"required": True}),
                                        ("--new-epoch", {"type": int, "required": True}),
                                        ("--new-pubkey", {"required": True})]),
        ("introduce-signer", cmd_introduce_signer,
         [("--dir", {"required": True}),
          ("--key", {"required": True}),
          ("--signer", {"required": True}),
          ("--key-id", {"required": True}),
          ("--epoch", {"type": int, "required": True}),
          ("--new-signer", {"required": True}),
          ("--new-key-id", {"required": True}),
          ("--new-epoch", {"type": int, "required": True}),
          ("--new-pubkey", {"required": True})]),
        ("revoke-key", cmd_revoke_key, [("--dir", {"required": True}),
                                        ("--key", {"required": True}),
                                        ("--signer", {"required": True}),
                                        ("--key-id", {"required": True}),
                                        ("--epoch", {"type": int, "required": True}),
                                        ("--revoke-key-id", {"required": True}),
                                        ("--revoke-epoch", {"type": int, "required": True}),
                                        ("--reason", {"default": "rotated out"})]),
        ("list", cmd_list, [("--dir", {"required": True})]),
        ("was-valid", cmd_was_valid, [("--dir", {"required": True}),
                                      ("--pubkey", {"required": True}),
                                      ("--seq", {"type": int, "required": True})]),
        ("prove", cmd_prove, [("--dir", {"required": True}),
                              ("--event-id", {"required": True})]),
        ("verify-proof", cmd_verify_proof, [("--envelope", {"required": True}),
                                            ("--proof", {"required": True})]),
        ("receipt-verify", cmd_receipt_verify, [("--receipt", {"required": True}),
                                                ("--hub-pubkey", {"required": True}),
                                                ("--source-domain", {"default": None})]),
        ("wire-out", cmd_wire_out, [("--envelope", {"required": True}),
                                    ("--key", {"required": True}),
                                    ("--created-at", {"type": int, "default": None})]),
        ("wire-in", cmd_wire_in, [("--dir", {"required": True}),
                                  ("--event", {"required": True}),
                                  ("--receipt-key", {"default": None})]),
    ]
    for name, fn, args in SPECS:
        sp_ = sub.add_parser(name)
        for flag, kw in args:
            sp_.add_argument(flag, **kw)
        sp_.set_defaults(fn=fn)

    a = ap.parse_args(argv)
    # 0.6.0(Orin 041 R1): 원장을 쓰는 명령은 load/replay 전부터 append까지 한 락 아래.
    if a.cmd in _WRITE_CMDS and getattr(a, "dir", None):
        with ho.hub_write_lock(a.dir):
            return a.fn(a)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
