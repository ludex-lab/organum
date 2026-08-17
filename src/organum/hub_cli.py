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
    organum-hub serve / push / pull               # git 없는 전달 — HTTP 우체통(drop v0)

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
    from organum import hub_wire as hw
    from organum import schnorr_pure as sp
except ImportError:                                    # 스크립트 직접 실행 경로
    import hub_drop as hd
    import hub_envelope as he
    import hub_log as hl
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
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_seed(path) -> bytes:
    raw = Path(path).read_bytes().strip()
    try:
        seed = bytes.fromhex(raw.decode())
    except (ValueError, UnicodeDecodeError):
        raise HubCliError(f"seed 파일이 hex64가 아님: {path}")
    if len(seed) != 32:
        raise HubCliError(f"seed는 32바이트: {path}")
    return seed


# ── 상태 로드/저장 ───────────────────────────────────────────────────────────

def _load(dirpath, *, receipt_seckey=None):
    d = Path(dirpath)
    cfg_p = d / "hub.json"
    if not cfg_p.is_file():
        raise HubCliError(f"hub 상태가 없음(먼저 init): {d}")
    cfg = json.loads(cfg_p.read_text(encoding="utf-8"))
    claims_doc = cfg["claims"]
    actual = he.canonical_sha(claims_doc)
    if actual != cfg["claims_sha256"]:
        raise HubCliError(f"claim registry 드리프트: 기록 {cfg['claims_sha256'][:12]}… "
                          f"≠ 실제 {actual[:12]}…")
    keys = he.KeyRegistry()
    for k in cfg["keys"]:
        keys.register(k["pubkey"], signer_id=k["signer_id"], key_id=k["key_id"],
                      key_epoch=k["key_epoch"])
    hub = he.HubIndex(
        key_registry=keys,
        claim_registry=he.ClaimRegistry(claims_doc, expected_sha256=cfg["claims_sha256"]),
        log=hl.TransparencyLog(), receipt_seckey=receipt_seckey,
        source_domain=cfg["source_domain"])
    # 재생 — 로그가 곧 상태. 재생 실패는 로그 손상이므로 크게 멈춘다.
    log_p = d / "events.jsonl"
    if log_p.is_file():
        for i, line in enumerate(log_p.read_text(encoding="utf-8").splitlines(), 1):
            rec = json.loads(line)
            if rec["transport"] == "direct":
                r = hub.admit(rec["raw"].encode("utf-8"), rec["sig"], rec["pubkey"])
            else:
                r = hw.admit_wire(hub, rec["event"])
            if not (r["admitted"] and not r["duplicate"]):
                raise HubCliError(f"로그 재생 실패(줄 {i}): {r['problems']} — 로그 손상")
    return d, cfg, hub


def _append(d: Path, rec: dict):
    with (d / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _admit_and_log(d, hub, raw: bytes, sig_hex: str, pubkey_hex: str) -> dict:
    r = hub.admit(raw, sig_hex, pubkey_hex)
    if r["admitted"] and not r["duplicate"]:
        _append(d, {"transport": "direct", "raw": raw.decode("utf-8"),
                    "sig": sig_hex, "pubkey": pubkey_hex})
    return r


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
    # idempotency: 내용에서 결정론 파생 — 같은 주장 재시도는 자연 수렴한다.
    idem = he.canonical_sha({"kind": kind, "subject": subject, "payload": payload})[:32]
    return {"envelope_schema": he.ENVELOPE_SCHEMA, "event_kind": kind,
            "signer": {"id": signer, "key_id": key_id, "key_epoch": epoch},
            "subject": subject,
            "provenance": {"lab": signer, "machine": cfg["machine_id"],
                           "platform": sys.platform, "adapter": "organum-hub-cli/0.3",
                           "cli_version": None, "capture": None},
            "idempotency_key": idem, "created_at": _now_z(), "payload": payload}


def _sign_admit(d, cfg, hub, env, seed) -> int:
    # 재시도 수렴(B1): 같은 idem scope의 최초 결과가 있으면 서명·재기록 없이 그것으로.
    prior = hub.idem_prior(env["signer"]["id"], env["event_kind"],
                           env["idempotency_key"])
    if prior is not None:
        return _print_result(prior)
    raw = he.canonical_bytes(env)
    sig = sp.sign(hashlib.sha256(raw).digest(), seed)
    return _print_result(_admit_and_log(d, hub, raw, sig.hex(),
                                        sp.public_key(seed).hex()))


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
            "admitted 이벤트가 이미 있어 bootstrap 등록 불가 — 새 키는 "
            "`organum-hub rotate-key`(admitted 이벤트로 lifecycle 전이)를 쓰세요")
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
    body = body_p.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    payload = {"target": {"lab_id": a.to_lab, "to_id": a.to_id, "to_epoch": a.to_epoch},
               "body_locator": a.body_locator or f"file://{body_p.name}",
               "body_sha256": digest,
               "body_media_type": a.media_type}
    subject_id = "message:" + digest[:24]
    env = _build_envelope(cfg, "message.posted", payload, signer=a.signer,
                          key_id=a.key_id, epoch=a.epoch,
                          subject={"type": "message", "id": subject_id})
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


def cmd_admit(a):
    d, cfg, hub = _load(a.dir, receipt_seckey=_read_seed(a.receipt_key)
                        if a.receipt_key else None)
    if bool(a.sig) == bool(a.sig_file):
        raise HubCliError("--sig 또는 --sig-file 중 하나만")
    sig = a.sig or Path(a.sig_file).read_text(encoding="utf-8").strip()
    env = json.loads(Path(a.envelope).read_text(encoding="utf-8"))
    return _print_result(_admit_and_log(d, hub, he.canonical_bytes(env), sig, a.pubkey))


def cmd_rotate_key(a):
    seed = _read_seed(a.key)
    d, cfg, hub = _load(a.dir)
    env = _build_envelope(cfg, "key.rotated",
                          {"new_key_id": a.new_key_id, "new_key_epoch": a.new_epoch,
                           "new_pubkey": a.new_pubkey},
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
    """우체통 quad 내보내기 — admitted 이벤트를 transport 폴더 관례
    (`NNN-envelope.json` + `NNN-sig.txt` [+ `NNN-body*`])로 싼다. events.jsonl에서
    raw를 손으로 꺼내는 일이 없게 하는, 일반 채널 절차의 발신 절반."""
    d, cfg, hub = _load(a.dir)
    lines = [json.loads(l) for l in
             (d / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    if not lines:
        raise HubCliError("내보낼 admitted 이벤트가 없다")
    if a.event_id:
        matches = [r for r in lines if r["transport"] == "direct"
                   and he.event_id_of(r["raw"].encode("utf-8")) == a.event_id]
        if not matches:
            raise HubCliError("event_id가 direct-path admitted 이벤트가 아님")
        rec = matches[-1]
    else:
        rec = lines[-1]
    if rec["transport"] != "direct":
        raise HubCliError("wire 경유 이벤트는 wire event JSON을 그대로 전달하라 — "
                          "quad는 direct-path용")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    used = [int(f.name[:3]) for f in out.iterdir()
            if f.name[:3].isdigit() and len(f.name) > 3]
    nnn = f"{(max(used) + 1) if used else 1:03d}"
    env_p = out / f"{nnn}-envelope.json"
    sig_p = out / f"{nnn}-sig.txt"
    env_p.write_text(rec["raw"], encoding="utf-8")
    sig_p.write_text(rec["sig"] + "\n", encoding="utf-8")
    written = [str(env_p), str(sig_p)]
    if a.body:
        body_src = Path(a.body)
        if not body_src.is_file():
            raise HubCliError(f"body 파일 없음: {a.body}")
        body_p = out / f"{nnn}-body{body_src.suffix or '.md'}"
        body_p.write_bytes(body_src.read_bytes())
        written.append(str(body_p))
    print(json.dumps({"exported": written, "nnn": nnn,
                      "event_id": he.event_id_of(rec["raw"].encode("utf-8")),
                      "pubkey": rec["pubkey"]}, ensure_ascii=False))
    return 0


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
              f"{eid[:16]}…  {'authority' if r['authority_projected'] else 'transport-only'}")
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
    try:
        r = hd.push_quad(a.url, token, a.quad)
    except (ValueError, hd.DropError) as e:
        raise HubCliError(str(e))
    print(json.dumps(r, ensure_ascii=False))
    return 0


def cmd_pull(a):
    token = hd.load_tokens(a.token_file)[0]
    try:
        ns = hd.pull_quads(a.url, token, a.dest, since=a.since)
    except (ValueError, hd.DropError) as e:
        raise HubCliError(str(e))
    print(json.dumps({"pulled": ns, "dest": str(Path(a.dest))}, ensure_ascii=False))
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
                              ("--pubkey", {"required": True}),
                              ("--receipt-key", {"default": None})]),
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
                            ("--token-file", {"required": True})]),
        ("pull", cmd_pull, [("--url", {"required": True}),
                            ("--dest", {"required": True}),
                            ("--token-file", {"required": True}),
                            ("--since", {"default": None})]),
        ("rotate-key", cmd_rotate_key, [("--dir", {"required": True}),
                                        ("--key", {"required": True}),
                                        ("--signer", {"required": True}),
                                        ("--key-id", {"required": True}),
                                        ("--epoch", {"type": int, "required": True}),
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
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
