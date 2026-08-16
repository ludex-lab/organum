"""organum hub — Nostr wire profile **v1** (3랩 FINAL ACCEPT, 2026-08-16).

acceptance base = `75851a3` (Orin exact-tree 재판정 136 pass · LxM 동치 스윕 65,550/0
[서러게이트 포함 — 2,051개 전부 술어 거부·예외 이탈 0 확인] · Ludex 무이견). **v1이 고정하는 의미**:

    carrier            = regular kind 1 + exact [["t","organum-hub-v1"]]
    content            = canonical envelope UTF-8 · local cap 65,536B
    outer authority    = NIP-01 wire 서명 + key registry 결속 (§2 해석)
    guard              = wire 서명/shape → carrier → 봉투 schema → policy
    receipt            = v2 · wire/envelope identity 동시 결속
    retry              = wire id가 달라도 최초 authority event/seq/receipt로 수렴
    deployment 경계     = lab-only relay의 hash/locator-only substrate carrier
                         (sealed coordination·public relay 승인 아님)

잔여(명시적 다음 범위, 숨은 결함 아님): relay 실제 content acceptance 경계 · sealed
coordination · storage 동시성/CAS · capture resolver · gate window · key history ·
unwind/dispute · 독립 BIP-340/Merkle 교차검증.

아래는 Stage 0(Buzz 소스 정독) 당시의 근거 기록이다:

- Buzz relay는 ingest에서 `buzz_core::verification::verify_event`로 **event id 재계산 +
  Schnorr 검증**을 한다(`crates/buzz-relay/src/handlers/{event,ingest}.rs`).
- id 재계산은 rust-nostr **0.44**의 `EventId::new`이며, 그 구현은(v0.44.0 태그 원문 확인):

      json!([0, public_key, created_at, kind, tags, content]).to_string() → SHA-256

  serde_json compact 직렬화 = 공백 없음 · 비ASCII 원문(UTF-8) · escape는 `"` `\\` 와
  제어문자(\\b \\t \\n \\f \\r 축약, 그 외 <0x20은 \\u00XX). Python
  `json.dumps(separators=(",", ":"), ensure_ascii=False)`가 이 영역에서 동치다 —
  그 동치성 자체가 Stage 1 라이브 대조의 검증 대상이다.
- 서명은 BIP-340 Schnorr, 메시지는 **32-byte event id 그 자체**(재해시 없음).
- Buzz 저장은 분해 컬럼(`migrations/0001`: content TEXT)이라 event JSON bytes는 재조립돼
  나가지만 **content 문자열 값은 보존**된다 — 검증이 wire bytes가 아니라 값에서 id를
  재계산하므로 매핑이 성립한다.

## 매핑 (후보)

    Nostr event.content = 봉투 canonical bytes의 UTF-8 디코드 (문자열 값)
    event.id            = NIP-01 직렬화의 SHA-256  ← **wire가 서명하는 범위**
    event.sig           = BIP-340(seckey, id bytes)
    event.pubkey        = 봉투 signer의 outer key (key registry 결속은 hub 층)

**이중 서명 구조를 명시한다**: 봉투 내부 서명(sha256(canonical bytes)에 대한 BIP-340,
`hub_envelope` guard가 검증)과 wire 서명(NIP-01 id에 대한 BIP-340)은 **다른 대상**이다.
wire 서명은 transport 수용의 조건이고, 봉투 서명은 authority의 조건이다 — relay가
event를 재포장해도 봉투 authority는 불변이다.

## Stage 0에서 확인된 Buzz 제약 (carrier 결정에 영향)

- **kind allowlist가 닫혀 있다**: `required_scope_for_kind`가 미지 kind에 Err → 거부.
  봉투 carrier는 기존 kind 중 선택해야 한다.
- **30023(long-form)은 addressable/replaceable** — 같은 (pubkey, kind, d-tag)의 새 이벤트가
  옛것을 교체한다. **append-only 증거에 부적합.** carrier 후보는 regular event(kind 1
  TEXT_NOTE, 또는 채널 문맥이 필요하면 kind 9 + `#h` 태그)다.
- NIP-42 인증 + rate limit이 ingest 앞에 있다(Stage 1에서 실측).
"""

from __future__ import annotations

import hashlib
import json

try:
    from organum import hub_envelope as _env
    from organum import schnorr_pure as _schnorr
except ImportError:                                    # 스크립트 직접 실행 경로
    import hub_envelope as _env
    import schnorr_pure as _schnorr

WIRE_PROFILE = "organum-hub/nostr-wire/v1"   # FINAL ACCEPT 2026-08-16, base 75851a3
# ── carrier profile (Orin W2: exact pin) ──
# kind 1(regular) + exact discriminator tag. tags=[]인 raw TEXT_NOTE는 일반 feed와 Hub
# carrier를 구분할 표지가 없다 — consumer는 이 exact set/order만 받는다.
# 경계 명시: 이 profile은 lab-only relay에서 hash/locator-only 봉투를 나르는 substrate
# carrier다. sealed coordination·public relay를 승인하는 pin이 아니다(community-global
# 가시성은 본문 비탑재 원칙과 정합 — content에는 locator+digest만 실린다).
CARRIER_KIND = 1
CARRIER_TAGS = (("t", "organum-hub-v1"),)
# 봉투 byte 상한(보수값) — relay 자체의 acceptance limit은 별개이며 그 경계의 라이브
# 확인은 P1 잔여로 명시한다(Orin W3 선택지 중 후자).
CONTENT_MAX_BYTES = 65536

_HEX64 = __import__("re").compile(r"[0-9a-f]{64}\Z")
_HEX128 = __import__("re").compile(r"[0-9a-f]{128}\Z")


class HubWireError(ValueError):
    pass


def _utf8_len_or_raise(v: str, where: str) -> int:
    """UTF-8 인코딩 가능성 술어(Orin R1). JSON parser는 lone surrogate가 든 str을 만들 수
    있고(`json.loads('"\\ud800"')`), 그 str은 encode에서 예외로 이탈한다 — 봉투 canonical
    `_scan`과 같은 규율을 wire 직렬화 경계에도 건다."""
    try:
        return len(v.encode("utf-8"))
    except UnicodeEncodeError:
        raise HubWireError(f"{where}: lone surrogate — UTF-8 인코딩 불가")


def _check_tags(tags) -> None:
    """NIP-01 태그 계약: list[list[str]] + 각 str은 UTF-8 인코딩 가능(R1).
    (LxM 크리틱 — 선언한 동치 영역을 술어로 강제. rust-nostr의 tag는 string vector라
    다른 타입의 local ACCEPT는 relay와 동치가 아니다)."""
    if not isinstance(tags, list):
        raise HubWireError(f"tags는 list여야 함: {type(tags).__name__}")
    for t in tags:
        if not isinstance(t, list) or not all(type(v) is str for v in t):
            raise HubWireError(f"tag는 list[str]여야 함: {t!r}")
        for v in t:
            _utf8_len_or_raise(v, "tag")


def nip01_serialized(pubkey_hex: str, created_at: int, kind: int, tags: list,
                     content: str) -> bytes:
    """NIP-01 event id가 서명하는 exact bytes — rust-nostr 0.44 `EventId::new` 동치 후보.

    serde_json compact와의 동치는 우리 값 영역(printable+비ASCII 문자열·ASCII 태그·
    정수)에서 성립하며, 그 동치성이 Stage 1 라이브 대조의 1번 항목이다."""
    if not (type(created_at) is int and 0 <= created_at < 2**53):
        raise HubWireError("created_at은 unix 초 정수")
    if not (type(kind) is int and 0 <= kind <= 65535):
        raise HubWireError("kind 범위 밖")
    _check_tags(tags)
    if type(content) is not str:
        raise HubWireError("content는 str")
    if _utf8_len_or_raise(content, "content") > CONTENT_MAX_BYTES:
        raise HubWireError(f"content가 {CONTENT_MAX_BYTES}B 상한 초과")
    # allow_nan=False(LxM): 기본값이면 NaN/Infinity라는 비-RFC8259 토큰을 뱉고 그 bytes에
    # id·서명까지 붙는다 — 어떤 준수 구현도 낳지 않을 bytes다. 봉투 canonical과 같은 규율.
    return json.dumps([0, pubkey_hex, created_at, kind, tags, content],
                      separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def nip01_event_id(pubkey_hex: str, created_at: int, kind: int, tags: list,
                   content: str) -> str:
    return hashlib.sha256(
        nip01_serialized(pubkey_hex, created_at, kind, tags, content)).hexdigest()


def build_wire_event(envelope_raw: bytes, *, seckey: bytes, created_at: int,
                     kind: int = CARRIER_KIND, tags: list = None,
                     aux_rand: bytes = b"\x00" * 32) -> dict:
    """봉투 canonical bytes → Nostr wire event. content는 bytes의 UTF-8 디코드 —
    build→verify round-trip에서 byte-identical이어야 한다."""
    tags = tags if tags is not None else [list(t) for t in CARRIER_TAGS]
    try:
        content = bytes(envelope_raw).decode("utf-8")
    except UnicodeDecodeError:
        raise HubWireError("봉투 bytes가 UTF-8이 아님 — canonical profile 위반")
    if content.encode("utf-8") != bytes(envelope_raw):
        raise HubWireError("content 왕복이 byte-identical하지 않음")
    pubkey = _schnorr.public_key(seckey).hex()
    eid = nip01_event_id(pubkey, created_at, kind, tags, content)
    sig = _schnorr.sign(bytes.fromhex(eid), seckey, aux_rand)
    return {"id": eid, "pubkey": pubkey, "created_at": created_at, "kind": kind,
            "tags": tags, "content": content, "sig": sig.hex()}


def verify_wire_event(event: dict) -> list:
    """wire 층 검증 — Buzz `verify_event`와 같은 두 검사(id 재계산·Schnorr) + strict
    shape(Orin W3: relay parser가 거부할 모양을 local이 통과시키면 동치가 아니다).
    problems 반환(fail-closed)."""
    problems = []
    need = {"id", "pubkey", "created_at", "kind", "tags", "content", "sig"}
    if not isinstance(event, dict) or set(event) != need:
        return ["wire event key set 위반"]
    if not (type(event["id"]) is str and _HEX64.fullmatch(event["id"])):
        return ["id가 lowercase hex64 아님"]
    if not (type(event["pubkey"]) is str and _HEX64.fullmatch(event["pubkey"])):
        return ["pubkey가 lowercase hex64 아님"]
    if not (type(event["sig"]) is str and _HEX128.fullmatch(event["sig"])):
        return ["sig가 lowercase hex128 아님"]
    try:
        computed = nip01_event_id(event["pubkey"], event["created_at"], event["kind"],
                                  event["tags"], event["content"])
    # R1: UnicodeEncodeError(ValueError 계열) 포함 — ingress에서 예외 이탈 없이 거부.
    except (HubWireError, TypeError, ValueError) as e:
        return [f"NIP-01 직렬화 불가: {type(e).__name__}: {e}"]
    if computed != event["id"]:
        problems.append(f"event id 불일치: 재계산 {computed[:12]}… ≠ 기재 {str(event['id'])[:12]}…")
    try:
        ok = _schnorr.verify(bytes.fromhex(event["sig"]), bytes.fromhex(event["id"]),
                             bytes.fromhex(event["pubkey"]))
    except (ValueError, TypeError):
        ok = False
    if not ok:
        problems.append("wire Schnorr 서명 검증 실패")
    return problems


def extract_envelope(event: dict) -> bytes:
    """wire event → 봉투 canonical bytes(wire 검증 포함). authority 인계는 `admit_wire`."""
    problems = verify_wire_event(event)
    if problems:
        raise HubWireError(f"wire 검증 실패: {problems}")
    return event["content"].encode("utf-8")


def is_carrier(event: dict) -> bool:
    """Hub carrier profile인가 — exact kind + exact tag set/order(Orin W2)."""
    return (isinstance(event, dict) and event.get("kind") == CARRIER_KIND
            and event.get("tags") == [list(t) for t in CARRIER_TAGS])


def admit_wire(hub, event: dict) -> dict:
    """**wire → authority 인계의 유일한 공식 경로** (Orin W1 조립).

    v0.1 §2의 outer 서명을 **Nostr wire 서명으로 해석**한다 — content(=canonical envelope
    bytes)는 NIP-01 id에 덮이고, 그 id를 wire Schnorr가 서명하므로 봉투 bytes의 origin이
    wire 서명 하나로 선다. 별도 봉투-hash 서명을 wire에 싣지 않으며, **수신자 재서명은
    존재하지 않는다**(그건 W1이 잡은 결함이었다).

    guard 순서 유지: ① wire 서명(id 재계산+Schnorr, strict shape 포함) → ② carrier
    profile → ③ schema→policy(HubIndex). event.pubkey가 key registry의 outer pubkey로
    내부 signer와 exact 결속된다. 같은 봉투를 다른 created_at으로 재전송해 wire id가
    달라도 authority는 최초 event/seq/receipt로 수렴한다(fingerprint=envelope bytes)."""
    problems = verify_wire_event(event)
    if problems:
        return {"admitted": False, "stage": "wire-signature", "problems": problems,
                "event_id": None, "accepted_seq": None, "duplicate": False,
                "authority_projected": False, "authority_reason": None, "receipt": None}
    if not is_carrier(event):
        return {"admitted": False, "stage": "wire-carrier",
                "problems": [f"carrier profile 아님: kind={event['kind']} tags={event['tags']}"],
                "event_id": None, "accepted_seq": None, "duplicate": False,
                "authority_projected": False, "authority_reason": None, "receipt": None}
    raw = event["content"].encode("utf-8")
    return hub._admit_authenticated(raw, event["pubkey"], wire_event_id=event["id"])
