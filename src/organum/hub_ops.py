"""organum hub_ops — hub CLI의 봉투 생성·서명·admit·export·검증 본체 (0.6.0).

0.5.x까지 이 논리는 `hub_cli`의 명령 함수 안에 인라인이었다. 그래서 실험 bbs wire가
게시판을 읽고 쓰려면 CLI를 **서브프로세스로 다시 부르고 그 stdout을 파싱**해야 했고,
Orin 037은 그 모양을 제품 부적격이라 판정했다. 0.6.0(Orin 040 §1)은 본체를 여기로
옮긴다: 이 모듈의 함수는 **구조화된 결과/오류를 돌려주고**, argparse·출력·exit는
`hub_cli`가, 게시판 의미는 `bbs_wire`가 맡는다. core→hub_cli 역참조는 없다.

계약(040 §1):
- `build_*`는 **순수 빌더**다 — 서명도 admit도 하지 않는다.
- `sign_and_admit`이 유일한 **쓰기**다(서명 → 자기 원장 admit → append). 같은
  idempotency scope의 재시도는 최초 결과로 수렴한다(B1).
- `verify_quad`는 **읽기 전용**이다 — registry의 재생 결속만 보고 원장을 전진시키지
  않는다. 서명 성공은 암호적 사실이지 authority-valid나 저자성 verified가 아니다.
- `export_quad`는 quad 번호를 **3~6자리 전체**로 읽고, 기존 파일을 **절대 덮어쓰지
  않으며**, 범위 초과를 명시 오류로 낸다(040 §4 — 구판이 999 다음 서로 다른 두
  이벤트에 1000·1000을 배정하고 첫 파일을 덮어쓴 결함의 수정).

CLI 출력 byte-identical: `hub_cli`의 명령 함수는 이 모듈을 부르고 종전과 같은 JSON을
찍는다 — 회귀는 tests/test_hub_ops.py의 골든(구판 5bd9bcc에서 채집)이 지킨다.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    import fcntl                                   # POSIX
except ImportError:                                # Windows
    fcntl = None
try:
    import msvcrt                                  # Windows
except ImportError:
    msvcrt = None

try:
    from organum import hub_envelope as he
    from organum import hub_log as hl
    from organum import hub_wire as hw
    from organum import schnorr_pure as sp
except ImportError:                                    # 스크립트 직접 실행 경로
    import hub_envelope as he
    import hub_log as hl
    import hub_wire as hw
    import schnorr_pure as sp

ADAPTER = "organum-hub-cli/0.3"
LOCK_FILE = ".write.lock"                          # 상태 디렉터리 안, 원장 밖
_QUAD_N_RE = re.compile(r"^[0-9]{3,6}$")
QUAD_N_MAX = 999999


class HubOpsError(ValueError):
    """구조화된 실패 — 메시지는 CLI가 종전 문구 그대로 stderr에 찍는다."""


def now_z() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── 쓰기 직렬화(Orin 041 R1) ─────────────────────────────────────────────────

@contextlib.contextmanager
def hub_write_lock(dirpath):
    """상태 디렉터리의 **쓰기 임계구역** — load/replay 전부터 idempotency 확인·append(그리고
    호출자의 outbox 배정)까지 한 락 아래 둔다. O_EXCL은 같은 파일명의 덮어쓰기만 막지 원장
    중복을 막지 못한다(041 R1: 각자 stale hub를 읽은 두 writer가 같은 이벤트를 두 번 append해
    로그를 재생 불가로 만들었다). 락은 파일 락(POSIX flock / Windows msvcrt.locking)이라
    프로세스·스레드 모두 직렬화한다. 디렉터리가 없으면 잠글 것이 없다(load_hub가 종전
    문구로 거절한다)."""
    d = Path(dirpath)
    if not d.is_dir():
        yield
        return
    fd = os.open(d / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        elif msvcrt is not None:
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)      # ~10초 재시도 뒤 OSError
                    break
                except OSError:
                    continue
        yield
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            elif msvcrt is not None:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)


@contextlib.contextmanager
def hub_writer(dirpath, *, receipt_seckey=None):
    """락 아래에서 (dir, cfg, hub)를 연다 — 쓰기 경로의 표준 진입(prepare_post·hub CLI 쓰기 명령)."""
    with hub_write_lock(dirpath):
        yield load_hub(dirpath, receipt_seckey=receipt_seckey)


# ── 키·상태 ──────────────────────────────────────────────────────────────────

def read_seed(path) -> bytes:
    raw = Path(path).read_bytes().strip()
    try:
        seed = bytes.fromhex(raw.decode())
    except (ValueError, UnicodeDecodeError):
        raise HubOpsError(f"seed 파일이 hex64가 아님: {path}")
    if len(seed) != 32:
        raise HubOpsError(f"seed는 32바이트: {path}")
    return seed


def load_hub(dirpath, *, receipt_seckey=None):
    """상태 디렉터리 → (dir, cfg, hub). 로그가 곧 상태 — 매번 재생해 복원한다."""
    d = Path(dirpath)
    cfg_p = d / "hub.json"
    if not cfg_p.is_file():
        raise HubOpsError(f"hub 상태가 없음(먼저 init): {d}")
    cfg = json.loads(cfg_p.read_text(encoding="utf-8"))
    claims_doc = cfg["claims"]
    actual = he.canonical_sha(claims_doc)
    if actual != cfg["claims_sha256"]:
        raise HubOpsError(f"claim registry 드리프트: 기록 {cfg['claims_sha256'][:12]}… "
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
    log_p = d / "events.jsonl"
    if log_p.is_file():
        for i, line in enumerate(log_p.read_text(encoding="utf-8").splitlines(), 1):
            rec = json.loads(line)
            if rec["transport"] == "direct":
                r = hub.admit(rec["raw"].encode("utf-8"), rec["sig"], rec["pubkey"])
            else:
                r = hw.admit_wire(hub, rec["event"])
            if not (r["admitted"] and not r["duplicate"]):
                raise HubOpsError(f"로그 재생 실패(줄 {i}): {r['problems']} — 로그 손상")
    return d, cfg, hub


def append_record(d: Path, rec: dict) -> None:
    with (d / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def admit_and_log(d, hub, raw: bytes, sig_hex: str, pubkey_hex: str) -> dict:
    r = hub.admit(raw, sig_hex, pubkey_hex)
    if r["admitted"] and not r["duplicate"]:
        append_record(d, {"transport": "direct", "raw": raw.decode("utf-8"),
                          "sig": sig_hex, "pubkey": pubkey_hex})
    return r


# ── 봉투 빌더(순수) ──────────────────────────────────────────────────────────

def build_envelope(cfg, kind, payload, *, signer, key_id, epoch, subject,
                   created_at: str | None = None) -> dict:
    """봉투 dict를 만든다 — 서명·admit 없음. idempotency는 내용에서 결정론 파생."""
    idem = he.canonical_sha({"kind": kind, "subject": subject, "payload": payload})[:32]
    return {"envelope_schema": he.ENVELOPE_SCHEMA, "event_kind": kind,
            "signer": {"id": signer, "key_id": key_id, "key_epoch": epoch},
            "subject": subject,
            "provenance": {"lab": signer, "machine": cfg["machine_id"],
                           "platform": sys.platform, "adapter": ADAPTER,
                           "cli_version": None, "capture": None},
            "idempotency_key": idem, "created_at": created_at or now_z(),
            "payload": payload}


def build_message_envelope(cfg, *, signer, key_id, epoch, to_lab, to_id, to_epoch,
                           body: bytes, body_locator: str, media_type: str,
                           created_at: str | None = None) -> dict:
    """message.posted 봉투(순수 빌더). 본문은 봉투와 나란히 가고 digest만 결속된다."""
    digest = hashlib.sha256(body).hexdigest()
    payload = {"target": {"lab_id": to_lab, "to_id": to_id, "to_epoch": to_epoch},
               "body_locator": body_locator,
               "body_sha256": digest,
               "body_media_type": media_type}
    return build_envelope(cfg, "message.posted", payload, signer=signer,
                          key_id=key_id, epoch=epoch,
                          subject={"type": "message", "id": "message:" + digest[:24]},
                          created_at=created_at)


# ── 쓰기 ─────────────────────────────────────────────────────────────────────

def sign_and_admit(d, cfg, hub, env: dict, seed: bytes) -> dict:
    """서명 → 자기 원장 admit → append. 유일한 쓰기 경로.

    재시도 수렴(B1): 같은 idem scope의 최초 결과가 있으면 서명·재기록 없이 그것을
    돌려준다 — 반환 dict는 hub.admit 결과 모양(admitted·duplicate·event_id·
    accepted_seq·authority_projected[·receipt])이다."""
    prior = hub.idem_prior(env["signer"]["id"], env["event_kind"],
                           env["idempotency_key"])
    if prior is not None:
        return prior
    raw = he.canonical_bytes(env)
    sig = sp.sign(hashlib.sha256(raw).digest(), seed)
    return admit_and_log(d, hub, raw, sig.hex(), sp.public_key(seed).hex())


# ── registry 술어 ────────────────────────────────────────────────────────────

def registry_binding_for_audit(hub, signer) -> dict | None:
    """봉투 signer 좌표의 exact 결속 — 폐기 여부와 무관하게(감사용, 0.4.13)."""
    if not isinstance(signer, dict):
        return None
    for b in hub.keys.bindings_of(signer.get("id") or ""):
        if (b["key_id"] == signer.get("key_id")
                and b["key_epoch"] == signer.get("key_epoch")):
            return b
    return None


def registry_pubkey_for(hub, signer) -> str | None:
    """현재 활성 키만(admit 전용) — 폐기된 키로 서명된 새 봉투를 막는다."""
    b = registry_binding_for_audit(hub, signer)
    return b["pubkey"] if b is not None and b["revoked_at_seq"] is None else None


# ── 읽기 전용 검증 ───────────────────────────────────────────────────────────

def verify_quad(env: dict, sig_hex: str, *, hub=None, pubkey: str | None = None,
                body: bytes | None = None) -> dict:
    """장부 무접촉 검증 — 서명·event_id·스키마·(옵션) body digest·target·key lifecycle.

    `hub`가 있으면 registry 결속에서 키를 파생하고 `pubkey`는 대조만 한다(다르면
    검증 **전에** 오류). hub 없는 첫인상(TOFU) 확인은 `pubkey` 단독.
    반환 `ok` = 암호적 사실 + 봉투 무결성만(폐기 키의 과거 봉투도 서명은 참)."""
    raw = he.canonical_bytes(env)
    lifecycle: dict = {"key_valid_from_seq": None, "key_revoked_at_seq": None}
    if hub is not None:
        binding = registry_binding_for_audit(hub, env.get("signer"))
        reg_pub = binding["pubkey"] if binding else None
        if binding is not None:
            lifecycle = {"key_valid_from_seq": binding["valid_from_seq"],
                         "key_revoked_at_seq": binding["revoked_at_seq"]}
        if reg_pub is None:
            if not pubkey:
                raise HubOpsError(
                    "이 signer 좌표는 --dir의 registry에 결속이 없다 — 첫인상(TOFU) "
                    "확인이면 --pubkey를 명시하세요(결정이어야 하니까)")
        elif pubkey and pubkey != reg_pub:
            raise HubOpsError(
                f"제공한 pubkey가 registry 결속과 다르다 — "
                f"registry {reg_pub[:16]}…, 제공 {pubkey[:16]}…. "
                "등록 signer는 --pubkey 생략이 안전하다(장부에서 파생)")
        else:
            pubkey = reg_pub
    elif not pubkey:
        raise HubOpsError("--pubkey 또는 --dir 중 하나는 필요하다")
    try:
        sig_ok = sp.verify(bytes.fromhex(sig_hex), hashlib.sha256(raw).digest(),
                           bytes.fromhex(pubkey))
    except (ValueError, TypeError):
        sig_ok = False
    payload = env.get("payload")
    payload = payload if isinstance(payload, dict) else {}     # 타입 오류는 schema_problems가 말한다
    body_match = None
    if body is not None:
        want = payload.get("body_sha256")
        body_match = bool(want) and hashlib.sha256(body).hexdigest() == want
    out = {"valid_signature": sig_ok,
           "event_id": he.event_id_of(raw),
           "signer": env.get("signer"),
           "event_kind": env.get("event_kind"),
           "target": (payload.get("target")
                      if env.get("event_kind") in he.ADDRESSED_KINDS else None),
           # payload가 object가 아니면 per-kind 검증기를 부르지 않는다(AttributeError 방지, 041 R2)
           "schema_problems": (he.validate_envelope(env)
                               if isinstance(env.get("payload"), dict)
                               else ["payload가 object가 아님"]),
           "body_sha256_match": body_match,
           **lifecycle,
           "ledger_touched": False}
    out["ok"] = bool(sig_ok and not out["schema_problems"]
                     and body_match in (None, True))
    return out


# ── quad 내보내기 ────────────────────────────────────────────────────────────

def quad_number(name: str) -> int | None:
    """파일명의 quad 번호 — `NNN-…`의 앞 세그먼트가 3~6자리 숫자일 때만."""
    head = name.split("-", 1)[0]
    return int(head) if _QUAD_N_RE.match(head) else None


def next_quad_number(out: Path) -> str:
    used = [n for n in (quad_number(f.name) for f in out.iterdir()
                        if f.name != f.name.split("-", 1)[0]) if n is not None]
    nxt = (max(used) + 1) if used else 1
    if nxt > QUAD_N_MAX:
        raise HubOpsError(f"quad 번호 범위 초과 — 다음 번호 {nxt} > {QUAD_N_MAX}"
                          "(문법 3~6자리); 새 outbox 디렉터리를 쓰세요")
    return f"{nxt:03d}"


def _write_new(path: Path, data: bytes) -> None:
    """기존 파일은 절대 덮어쓰지 않는다 — 동시 export의 번호 충돌은 명시 거부."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        raise HubOpsError(f"{path.name} 이미 존재 — quad는 덮어쓰지 않는다(번호 충돌: "
                          "동시 export이거나 트리가 손으로 바뀜)")
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def export_quad(d: Path, out_dir, *, event_id: str | None = None,
                body_path=None) -> dict:
    """admitted 이벤트 → transport quad(`NNN-envelope.json`+`NNN-sig.txt`[+`NNN-body*`]).

    바이트 정확(텍스트 모드 금지 — Windows CRLF, 0.4.14). 쓰는 순서는 sig → body →
    envelope: envelope가 **완결 표지**라 중간에 죽어도 미완성 quad는 envelope 없이
    남고, 읽는 쪽은 그것을 완결로 세지 않는다."""
    lines = [json.loads(l) for l in
             (d / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    if not lines:
        raise HubOpsError("내보낼 admitted 이벤트가 없다")
    if event_id:
        matches = [r for r in lines if r["transport"] == "direct"
                   and he.event_id_of(r["raw"].encode("utf-8")) == event_id]
        if not matches:
            raise HubOpsError("event_id가 direct-path admitted 이벤트가 아님")
        rec = matches[-1]
    else:
        rec = lines[-1]
    if rec["transport"] != "direct":
        raise HubOpsError("wire 경유 이벤트는 wire event JSON을 그대로 전달하라 — "
                          "quad는 direct-path용")
    body_src = None
    if body_path is not None:
        body_src = Path(body_path)
        if not body_src.is_file():
            raise HubOpsError(f"body 파일 없음: {body_path}")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    nnn = next_quad_number(out)
    env_p = out / f"{nnn}-envelope.json"
    sig_p = out / f"{nnn}-sig.txt"
    if env_p.exists() or sig_p.exists():
        raise HubOpsError(f"quad {nnn} 이미 존재 — 덮어쓰지 않는다")
    written = [str(env_p), str(sig_p)]
    _write_new(sig_p, (rec["sig"] + "\n").encode("utf-8"))
    if body_src is not None:
        body_p = out / f"{nnn}-body{body_src.suffix or '.md'}"
        _write_new(body_p, body_src.read_bytes())
        written.append(str(body_p))
    _write_new(env_p, rec["raw"].encode("utf-8"))
    return {"exported": written, "nnn": nnn,
            "event_id": he.event_id_of(rec["raw"].encode("utf-8")),
            "pubkey": rec["pubkey"]}
