"""hub Nostr wire 매핑 **후보** 회귀 (Stage 0) — 직렬화 결정론·이중 서명·봉투 왕복.

라벨 정직성: 이 회귀는 rust-nostr 0.44 `EventId::new` 원문(소스 확인)과의 **의미 동치
후보**를 세우는 것이다. 라이브 relay와의 byte 대조(Stage 1)가 pin이며, 여기의 결정론
fixture가 그때 갈라지면 이 파일이 먼저 깨져야 한다.
"""
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from organum import hub_envelope as he  # noqa: E402
from organum import hub_log as hl  # noqa: E402
from organum import hub_wire as hw  # noqa: E402
from organum import schnorr_pure as sp  # noqa: E402

SEC = (7).to_bytes(32, "big")
PUB = sp.public_key(SEC).hex()


# ── NIP-01 직렬화 의미론 (serde_json compact 동치 후보) ─────────────────────

def test_직렬화_모양_공백없음_비ASCII_원문():
    raw = hw.nip01_serialized(PUB, 1755000000, 1, [["h", "chan-1"]], "한글 content")
    assert raw == (f'[0,"{PUB}",1755000000,1,[["h","chan-1"]],"한글 content"]'
                   ).encode("utf-8")
    assert b" " not in raw.replace("한글 content".encode(), b"")   # 공백 없음
    assert "한글".encode("utf-8") in raw                            # \uXXXX 아님


def test_escape_의미론_serde_json_동치():
    """escape 집합: " \\ 그리고 제어문자(축약 5종 + \\u00XX). 그 외 원문."""
    content = '따옴표" 역슬래시\\ 탭\t 개행\n DEL\x7f 유닛\x1f'
    raw = hw.nip01_serialized(PUB, 1, 1, [], content)
    s = raw.decode("utf-8")
    assert '\\"' in s and "\\\\" in s and "\\t" in s and "\\n" in s
    assert "\\u001f" in s                       # 축약 없는 제어문자
    assert "\x7f" in s and "\\u007f" not in s   # DEL은 escape 안 함(serde_json 동치)


def test_event_id_결정론_pin():
    """이 fixture가 Stage 1 라이브 대조의 기준점이다 — 바뀌면 직렬화가 바뀐 것."""
    eid = hw.nip01_event_id(PUB, 1755000000, 1, [], "organum-hub stage0")
    assert eid == hashlib.sha256(
        f'[0,"{PUB}",1755000000,1,[],"organum-hub stage0"]'.encode()).hexdigest()
    assert hw.nip01_event_id(PUB, 1755000000, 1, [], "organum-hub stage0") == eid


@pytest.mark.parametrize("bad_kw", [
    {"created_at": 1.5}, {"created_at": -1}, {"created_at": 2**53},
    {"kind": -1}, {"kind": 70000}, {"kind": "1"},
])
def test_직렬화_입력_경계(bad_kw):
    kw = {"pubkey_hex": PUB, "created_at": 1, "kind": 1, "tags": [], "content": "c"}
    kw.update(bad_kw)
    with pytest.raises((hw.HubWireError, TypeError)):
        hw.nip01_serialized(**kw)


# ── wire event 빌드·검증 ────────────────────────────────────────────────────

def _envelope_raw():
    env = {"envelope_schema": he.ENVELOPE_SCHEMA, "event_kind": "message.read",
           "signer": {"id": "lab:organum-cody", "key_id": "k1", "key_epoch": 1},
           "subject": {"type": "run", "id": "run:cal-01"},
           "provenance": {"lab": "lab:organum-cody", "machine": "m-01",
                          "platform": "darwin", "adapter": "organum-hub/0.1",
                          "cli_version": None, "capture": None},
           "idempotency_key": "wire-1", "created_at": "2026-08-15T00:00:00Z",
           "payload": {"cursor": "c-한글"}}
    return he.canonical_bytes(env)


def test_build_verify_왕복():
    raw = _envelope_raw()
    ev = hw.build_wire_event(raw, seckey=SEC, created_at=1755000000)
    assert hw.verify_wire_event(ev) == []
    assert hw.extract_envelope(ev) == raw               # byte-identical 왕복
    assert hw.is_carrier(ev)                            # 기본 build = carrier profile


def test_이중_서명은_다른_대상이다():
    """wire 서명은 NIP-01 id를, 봉투 서명은 sha256(canonical bytes)를 서명한다 —
    같은 키를 써도 서명 대상이 다르므로 서로를 대체할 수 없다."""
    raw = _envelope_raw()
    ev = hw.build_wire_event(raw, seckey=SEC, created_at=1755000000)
    envelope_msg = hashlib.sha256(raw).digest()
    wire_msg = bytes.fromhex(ev["id"])
    assert envelope_msg != wire_msg
    env_sig = sp.sign(envelope_msg, SEC)
    assert not sp.verify(env_sig, wire_msg, bytes.fromhex(PUB))   # 교차 대입 불가


def test_변조_거부():
    raw = _envelope_raw()
    ev = hw.build_wire_event(raw, seckey=SEC, created_at=1755000000)
    for mut in [{"content": ev["content"] + " "}, {"created_at": 1755000001},
                {"kind": 9}, {"tags": [["h", "x"]]},
                {"id": "0" * 64}, {"sig": "0" * 128},
                {"pubkey": sp.public_key((11).to_bytes(32, "big")).hex()}]:
        bad = {**ev, **mut}
        assert hw.verify_wire_event(bad), mut
        with pytest.raises(hw.HubWireError):
            hw.extract_envelope(bad)


def test_비UTF8_봉투_거부():
    with pytest.raises(hw.HubWireError, match="UTF-8"):
        hw.build_wire_event(b"\xff\xfe", seckey=SEC, created_at=1)


# ── e2e: wire → authority 인계 (Orin W1 조립 — 수신자 재서명 없음) ──────────

def _mk_hub():
    keys = he.KeyRegistry()
    keys.register(PUB, signer_id="lab:organum-cody", key_id="k1", key_epoch=1)
    claims_doc = {"claims": {}}
    claims = he.ClaimRegistry(claims_doc, expected_sha256=he.canonical_sha(claims_doc))
    return he.HubIndex(key_registry=keys, claim_registry=claims,
                       log=hl.TransparencyLog(), receipt_seckey=(17).to_bytes(32, "big"))


def test_e2e_admit_wire_실수신_경로(monkeypatch=None):
    """[W1] wire 검증 → registry 결속 → schema/policy → receipt. **수신자 재서명 없음** —
    outer 서명은 NIP-01 wire 서명 그 자체다(§2 해석). receipt가 wire/envelope identity를
    둘 다 결속하고 inclusion proof까지 잇는다."""
    hub = _mk_hub()
    raw = _envelope_raw()
    ev = hw.build_wire_event(raw, seckey=SEC, created_at=1755000000)
    rebuilt = json.loads(json.dumps(ev, ensure_ascii=True))       # relay 재직렬화 시뮬레이션
    r = hw.admit_wire(hub, rebuilt)
    assert r["admitted"], r["problems"]
    body = r["receipt"]["body"]
    assert body["wire_event_id"] == ev["id"]                      # wire identity
    assert body["event_id"] == hashlib.sha256(raw).hexdigest()    # envelope identity
    assert body["event_id"] != body["wire_event_id"]              # 실제로 갈라진다
    assert he.verify_relay_receipt(r["receipt"], sp.public_key((17).to_bytes(32, "big")))
    # authority index에 섰다 — 직접 경로와 같은 기록
    assert hub.authority(r["event_id"]) is not None


def test_w1_같은_봉투_다른_wire_created_at은_최초로_수렴():
    """[W1] wire id가 달라져도 authority retry는 최초 event/seq/receipt로 수렴 —
    log append 없음."""
    hub = _mk_hub()
    raw = _envelope_raw()
    first = hw.admit_wire(hub, hw.build_wire_event(raw, seckey=SEC, created_at=1755000000))
    assert first["admitted"] and not first["duplicate"]
    size = hub._log.tree_size
    again = hw.admit_wire(hub, hw.build_wire_event(raw, seckey=SEC, created_at=1755000777))
    assert again["duplicate"] is True
    assert (again["event_id"], again["accepted_seq"]) == \
        (first["event_id"], first["accepted_seq"])
    assert again["receipt"] == first["receipt"]        # 최초 receipt(최초 wire id) 그대로
    assert hub._log.tree_size == size


def test_w1_수신자는_봉투_서명_없이_admit_wire로만_들어온다():
    """직접 admit(봉투-hash 서명 경로)은 wire에서 불가능하다 — wire에는 그 서명이 없다.
    잘못된 서명으로 직접 admit을 시도하면 signature 단계에서 죽는 것을 확인(회귀 문서화)."""
    hub = _mk_hub()
    raw = _envelope_raw()
    ev = hw.build_wire_event(raw, seckey=SEC, created_at=1755000000)
    r = hub.admit(raw, ev["sig"], PUB)                 # wire 서명을 봉투 서명 자리에 오용
    assert not r["admitted"] and r["stage"] == "signature"


def test_w2_carrier_아니면_authority_인계_거부():
    hub = _mk_hub()
    raw = _envelope_raw()
    for kw in ({"tags": []}, {"tags": [["t", "other"]]}, {"kind": 9}):
        ev = hw.build_wire_event(raw, seckey=SEC, created_at=1755000000, **kw)
        r = hw.admit_wire(hub, ev)
        assert not r["admitted"] and r["stage"] == "wire-carrier", kw
    assert hub._log.tree_size == 0                     # 거부는 tree 무접촉


def test_w2_carrier_profile_pin():
    """Stage 0 발견 유지(regular kind) + W2 discriminator exact pin + v1 라벨."""
    assert hw.WIRE_PROFILE == "organum-hub/nostr-wire/v1"
    assert hw.CARRIER_KIND == 1
    assert hw.CARRIER_TAGS == (("t", "organum-hub-v1"),)
    assert not 30000 <= hw.CARRIER_KIND <= 39999       # addressable 금지
    assert not 20000 <= hw.CARRIER_KIND <= 29999       # ephemeral 금지


@pytest.mark.parametrize("mut", [
    {"id": "0" * 63}, {"id": ("A" + "0" * 63)},        # 길이·대문자
    {"pubkey": "zz" * 32}, {"sig": "0" * 127},
    {"tags": [["h", 1]]},                              # Orin W3 재현: int tag
    {"tags": [["h", None]]}, {"tags": "not-a-list"}, {"tags": [{"k": "v"}]},
    {"content": 12345}, {"created_at": 1.5},
])
def test_w3_strict_shape_거부(mut):
    """[W3·LxM] relay parser가 거부할 모양은 local도 거부 — 동치 영역을 술어로 강제."""
    raw = _envelope_raw()
    ev = hw.build_wire_event(raw, seckey=SEC, created_at=1755000000)
    bad = {**ev, **mut}
    assert hw.verify_wire_event(bad), mut
    hub = _mk_hub()
    assert not hw.admit_wire(hub, bad)["admitted"]


def test_lxm_NaN_Infinity_태그는_직렬화_경계에서_죽는다():
    """[LxM] allow_nan 기본값이면 비-RFC8259 토큰(NaN/Infinity)이 bytes가 되고 그 위에
    서명까지 갔다 — 어떤 준수 구현도 낳지 않을 bytes. 이제 직렬화 자체가 거부한다."""
    for bad in (float("nan"), float("inf"), 1.5):
        with pytest.raises(hw.HubWireError):
            hw.nip01_serialized(PUB, 1, 1, [["h", bad]], "c")     # tags 계약이 먼저 잡음
    # content에는 float/int 자체가 올 수 없다(str 강제) — 층 간 규율 대칭.
    with pytest.raises(hw.HubWireError):
        hw.nip01_serialized(PUB, 1, 1, [], 12345)
    # 그리고 백스톱: 계약을 우회해도 json.dumps 자체가 allow_nan=False로 거부한다는
    # 것을 직접 확인(LxM: "규칙을 이름으로 주장하면 그 이름을 세우는 술어를 함께 건다").
    with pytest.raises(ValueError):
        json.dumps([float("nan")], allow_nan=False)


# ═══ Orin R1·R2 (wire v1 재판정 correction) ═════════════════════════════════

def test_r1_lone_surrogate_content가_crash가_아니라_거부():
    """[R1] JSON parser는 lone surrogate str을 만들 수 있다 — 외부 event의 그 content가
    verifier에서 UnicodeEncodeError로 이탈했다. 이제 problems 반환(fail-closed)."""
    surrogate = json.loads('"\\ud800"')               # lone surrogate str
    raw = _envelope_raw()
    ev = hw.build_wire_event(raw, seckey=SEC, created_at=1755000000)
    bad = {**ev, "content": surrogate}
    problems = hw.verify_wire_event(bad)                # 예외 없이
    assert problems and any("직렬화 불가" in q for q in problems)
    hub = _mk_hub()
    r = hw.admit_wire(hub, bad)                         # ingress도 fail-closed
    assert not r["admitted"] and r["stage"] == "wire-signature"


def test_r1_lone_surrogate_tag도_거부():
    surrogate = json.loads('"\\udc00"')
    raw = _envelope_raw()
    ev = hw.build_wire_event(raw, seckey=SEC, created_at=1755000000)
    bad = {**ev, "tags": [["t", surrogate]]}
    assert hw.verify_wire_event(bad)                    # 예외 없이 problems


def test_r1_직렬화_경계가_surrogate를_술어로_거부():
    surrogate = json.loads('"\\ud800"')
    with pytest.raises(hw.HubWireError, match="lone surrogate"):
        hw.nip01_serialized(PUB, 1, 1, [], surrogate)
    with pytest.raises(hw.HubWireError, match="lone surrogate"):
        hw.nip01_serialized(PUB, 1, 1, [["t", surrogate]], "c")


def test_r2_구형_v1_body는_hub_key_서명이어도_거부():
    """[R2] wire_event_id 없는 구형 body를 Hub key로 서명해도 v2 verifier가 거부 —
    같은 버전 아래 비호환 body 동시 유효 금지."""
    hub = _mk_hub()
    r = hw.admit_wire(hub, hw.build_wire_event(_envelope_raw(), seckey=SEC,
                                               created_at=1755000000))
    good = r["receipt"]
    legacy_body = {k: v for k, v in good["body"].items() if k != "wire_event_id"}
    legacy_body["schema"] = "organum-hub/relay-receipt/v1"
    legacy_sig = sp.sign(hashlib.sha256(he.canonical_bytes(legacy_body)).digest(),
                         (17).to_bytes(32, "big"))
    assert not he.verify_relay_receipt(
        {"body": legacy_body, "signature": legacy_sig.hex()},
        sp.public_key((17).to_bytes(32, "big")))
    # v1 schema 문자열만 v2로 바꿔도(키 부재) 여전히 거부
    legacy_body2 = dict(legacy_body, schema=he.RECEIPT_SCHEMA)
    sig2 = sp.sign(hashlib.sha256(he.canonical_bytes(legacy_body2)).digest(),
                   (17).to_bytes(32, "big"))
    assert not he.verify_relay_receipt(
        {"body": legacy_body2, "signature": sig2.hex()},
        sp.public_key((17).to_bytes(32, "big")))


def test_r2_receipt_shape_배터리():
    hub = _mk_hub()
    r = hw.admit_wire(hub, hw.build_wire_event(_envelope_raw(), seckey=SEC,
                                               created_at=1755000000))
    good = r["receipt"]
    hub_pub = sp.public_key((17).to_bytes(32, "big"))
    assert he.verify_relay_receipt(good, hub_pub)
    hub_sec = (17).to_bytes(32, "big")

    def signed(body):
        sig = sp.sign(hashlib.sha256(he.canonical_bytes(body)).digest(), hub_sec)
        return {"body": body, "signature": sig.hex()}

    muts = [
        dict(good["body"], accepted_seq=0),                     # 양수 위반
        dict(good["body"], tree_size="1"),                      # 타입 위반
        dict(good["body"], wire_event_id="not-hex"),
        dict(good["body"], content_sha256="b" * 64),            # event_id와 불일치
        dict(good["body"], root="B" * 64),                      # 대문자 hex
        dict(good["body"], source_domain=""),
        {**good["body"], "extra": 1},                           # 잉여 키
    ]
    for body in muts:
        assert not he.verify_relay_receipt(signed(body), hub_pub), body


def test_r2_source_domain_대조():
    """지어낸 기본값이 production source로 읽히지 않게 — 기대 도메인 대조."""
    hub = _mk_hub()
    r = hw.admit_wire(hub, hw.build_wire_event(_envelope_raw(), seckey=SEC,
                                               created_at=1755000000))
    hub_pub = sp.public_key((17).to_bytes(32, "big"))
    assert he.verify_relay_receipt(r["receipt"], hub_pub,
                                   expected_source_domain="organum-hub/local")
    assert not he.verify_relay_receipt(r["receipt"], hub_pub,
                                       expected_source_domain="organum-hub/prod")


def test_content_byte_상한():
    big = "가" * (hw.CONTENT_MAX_BYTES // 3 + 10)      # UTF-8 3바이트 × n > 상한
    with pytest.raises(hw.HubWireError, match="상한"):
        hw.nip01_serialized(PUB, 1, 1, [], big)
