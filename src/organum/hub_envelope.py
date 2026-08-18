"""organum hub — 봉투 v0.2 기준 구현 (canonical bytes · guard · idempotency · claim registry).

스펙: `docs/hub-envelope-v0.1-draft.md` + `docs/hub-envelope-v0.2-delta.md`
(design freeze candidate 3/3 — Orin·Ludex·LxM). 이 모듈은 그 스펙의 **실행 가능한 기준**이다:
P1 final critic이 요구한 byte-level 증거(canonical bytes fixture · signer binding · idem
round trip · negative fixture 17 fail-closed)를 코드와 회귀로 세운다.

## 이 구현이 pin하는 것 / 미루는 것

- **pin**: 봉투 canonical bytes profile(`organum-hub/canonical-json/v1`) · 봉투/kind 스키마 ·
  guard 순서(서명→스키마→정책) · idempotency fingerprint · claim registry 의미론 ·
  role/plane 격납.
- **미룸(P1 Buzz byte-level 대조)**: outer Nostr event가 서명하는 exact byte 범위 · relay
  구현과의 wire 대조. 여기의 서명은 **canonical envelope bytes의 SHA-256**에 대한 BIP-340
  이다 — Nostr `content` 필드에 이 bytes가 실리는 것이 후보 매핑이고, 그 대조가 P1이다.

## 신뢰 구조 (스펙 §2·§3)

- `event_id` = canonical bytes의 SHA-256 — **identity를 주지 origin을 주지 않는다.**
  origin은 outer 서명 + key registry 대조에서만 온다.
- guard 순서는 ① outer 서명 → ② 스키마 → ③ 정책이며, 실패 이벤트는 authority index에
  **절대 편입되지 않는다**(격리 스트림에 보존).
- `created_at`은 서술값이다. **순서/유효성 authority는 accepted_seq·anchor·event ref**이며
  caller wall-clock을 authority로 승격하는 경로는 스키마 수준에서 거부된다.
- false ≠ null: 관측 근거가 없는 값은 지어내지 않고 null로 적는다
  (`install_observed_at`), 파생 가능한 값은 caller 입력을 믿지 않고 재도출한다
  (`canary.result.passed`).
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime

try:
    from organum import schnorr_pure as _schnorr
except ImportError:                                    # 스크립트 직접 실행 경로
    import schnorr_pure as _schnorr

CANONICAL_PROFILE = "organum-hub/canonical-json/v1"
ENVELOPE_SCHEMA = "organum-hub/envelope/v0.2"
# v2(Orin R2): body에 wire_event_id가 들어가며 **schema bump + exact shape 검증** —
# 같은 버전 아래 두 비호환 body가 동시에 유효하면 consumer가 검증 성공 뒤에 깨진다.
# v1 legacy verifier는 두지 않는다(배포된 v1 소비자 없음 — 기준 구현 내부 교체).
RECEIPT_SCHEMA = "organum-hub/relay-receipt/v2"
_RECEIPT_BODY_KEYS = {"schema", "source_domain", "event_id", "content_sha256",
                      "wire_event_id", "idempotency_fingerprint", "accepted_seq",
                      "tree_size", "root"}

_MAX_DEPTH = 64
_MAX_INT = 2**53          # JS/Nostr 생태계 interop — 이 밖의 정수는 wire에서 깨진다

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_LAB_ID = re.compile(r"lab:[a-z0-9][a-z0-9._-]{0,63}\Z")
# LxM R1/R2(0.4.4): IGNORECASE는 ASCII가 아니라 유니코드 케이스 폴딩이다 — 켈빈 K(U+212A)·
# long s(U+017F)·İ(U+0130)가 통과했고, 문법은 case-insensitive인데 정체성 tuple은
# case-sensitive라 'k1'/'K1' confusable 쌍이 성립했다. 교정: 플래그 제거 + 명시 클래스
# (대문자는 클래스가 드러내고, 정체성은 exact) — 이름과 술어가 한 이야기를 한다.
_KEY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SUBJECT_PREFIX = {"run": "run:", "artifact": "artifact:", "creature": "creature:",
                   "machine": "machine:", "message": "message:", "route": "route:"}
_SUBJECT_ID = re.compile(r"[a-z]+:[A-Za-z0-9][A-Za-z0-9._/-]{0,255}\Z")


class HubEnvelopeError(ValueError):
    pass


# ── canonical bytes profile v1 ───────────────────────────────────────────────

def _scan(obj, depth: int = 0) -> None:
    if depth > _MAX_DEPTH:
        raise HubEnvelopeError("canonical: 깊이 한계 초과")
    if obj is None or obj is True or obj is False:
        return
    if type(obj) is int:
        if abs(obj) >= _MAX_INT:
            raise HubEnvelopeError("canonical: 정수가 2^53 밖(interop 불가)")
        return
    if type(obj) is float:
        raise HubEnvelopeError("canonical: float 금지(표현 비결정성)")
    if type(obj) is str:
        try:
            obj.encode("utf-8")
        except UnicodeEncodeError:
            raise HubEnvelopeError("canonical: lone surrogate 금지")
        return
    if type(obj) is list:
        for v in obj:
            _scan(v, depth + 1)
        return
    if type(obj) is dict:
        for k, v in obj.items():
            if type(k) is not str or not k:
                raise HubEnvelopeError("canonical: key는 비어있지 않은 str")
            if not all(0x21 <= ord(c) <= 0x7E for c in k):
                raise HubEnvelopeError(f"canonical: key는 printable ASCII만 — {k!r}")
            _scan(v, depth + 1)
        return
    raise HubEnvelopeError(f"canonical: 허용되지 않는 타입 {type(obj).__name__}")


def canonical_bytes(obj: dict) -> bytes:
    """봉투 canonical 직렬화 — 같은 논리 내용이면 같은 bytes, 아니면 예외.

    규칙(profile v1): dict top-level · key 사전순 · 구분자 압축(`,` `:`) · UTF-8 ·
    ensure_ascii=False · float/NaN 금지 · |정수| < 2^53 · lone surrogate 금지 ·
    key는 printable ASCII · 깊이 ≤ 64."""
    if type(obj) is not dict:
        raise HubEnvelopeError("canonical: top-level은 dict")
    _scan(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def canonical_sha(obj: dict) -> str:
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()


def event_id_of(raw: bytes) -> str:
    """content-addressed identity. **origin이 아니다** — origin은 서명+registry."""
    return hashlib.sha256(bytes(raw)).hexdigest()


# ── kind 목록·평면 분류 (v0.1 §6 + v0.2 Δ1·Δ6 개명) ──────────────────────────

EVIDENCE_KINDS = ("artifact.attested", "toolchain.observed", "canary.result",
                  "provider.route.observed", "hub.anchor",
                  "key.rotated", "key.revoked", "signer.introduced",
                  "machine.rekeyed", "machine.superseded",
                  "run_set.launch_authorized", "run_set.completed")
COORDINATION_KINDS = ("message.posted", "message.read", "delivery.semantic_ack")
ALL_KINDS = EVIDENCE_KINDS + COORDINATION_KINDS
ADDRESSED_KINDS = ("message.posted", "delivery.semantic_ack")


def plane_of(kind: str) -> str:
    if kind in EVIDENCE_KINDS:
        return "evidence"
    if kind in COORDINATION_KINDS:
        return "coordination"
    raise HubEnvelopeError(f"알 수 없는 kind: {kind}")


# ── 봉투 공통 스키마 ─────────────────────────────────────────────────────────

_ENVELOPE_KEYS = {"envelope_schema", "event_kind", "signer", "subject", "provenance",
                  "idempotency_key", "created_at", "payload"}
_SIGNER_KEYS = {"id", "key_id", "key_epoch"}
_SUBJECT_KEYS = {"type", "id"}
_PROVENANCE_KEYS = {"lab", "machine", "platform", "adapter", "cli_version", "capture"}
_CAPTURE_KEYS = {"capture_artifact_sha256"}
# 반례 C(LxM) 제안 1: 버전은 자유 문자열이 아니라 **주소 있는 주장**이다 — 값과 함께
# 그 값이 나온 capture artifact를 지목해야 검증자가 대조할 주소를 갖는다.
# install_observed_at에 내린 처방(결정 3)의 확장.
_CLI_VERSION_KEYS = {"value", "capture_artifact_sha256"}

_ORDERING_LEVELS = ("emission", "execution_gated", "bracketed_execution")
# caller wall-clock을 validity authority로 승격 금지(v0.1 §3·§8) — 허용 basis enum.
_AUTHORITY_BASES = ("accepted_seq", "anchor", "event_ref")


def _is_str(v) -> bool:
    return type(v) is str and bool(v)


_RFC3339_Z = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z\Z")


def _is_rfc3339_z(v) -> bool:
    """RFC3339 UTC-Z 시각인가 — **suffix가 아니라 문법 + 달력/시각 유효성까지**.

    r5 delta HOLD(Orin) 교정: 종전 predicate가 `endswith("Z")` 하나라 "아무말Z"·
    "2026-99-99T99:99:99Z"가 통과해 capture 맵을 선점할 수 있었다 — 이름(RFC3339-Z)과
    구현이 갈라져 있었다. regex가 lexical 모양을 pin하고 strptime이 달력을 검증한다.

    profile: `YYYY-MM-DDTHH:MM:SS(.f{1,6})?Z` — fractional seconds 허용,
    offset 표기(+00:00)·leap second 미허용. schema(created_at·observed_at)와
    collector 자격 불변식(_eligible)이 **같은 predicate**를 쓴다."""
    if type(v) is not str or not _RFC3339_Z.fullmatch(v):
        return False
    body = v[:-1]
    fmt = "%Y-%m-%dT%H:%M:%S.%f" if "." in body else "%Y-%m-%dT%H:%M:%S"
    try:
        datetime.strptime(body, fmt)
    except ValueError:
        return False
    return True


def _bad_keys(obj, exact: set, where: str) -> list:
    if not isinstance(obj, dict):
        return [f"{where}: dict 아님"]
    if set(obj) != exact:
        return [f"{where}: key set 위반 — got {sorted(obj)}, want {sorted(exact)}"]
    return []


def validate_envelope(env: dict) -> list:
    """② 스키마 층. 공통 봉투 + per-kind payload(strict tagged union). problems 반환."""
    problems = _bad_keys(env, _ENVELOPE_KEYS, "envelope")
    if problems:
        return problems
    if env["envelope_schema"] != ENVELOPE_SCHEMA:
        return [f"envelope_schema != {ENVELOPE_SCHEMA}"]
    kind = env["event_kind"]
    if kind not in ALL_KINDS:
        return [f"event_kind가 enum 밖: {kind!r}"]

    problems += _bad_keys(env["signer"], _SIGNER_KEYS, "signer")
    if not problems:
        s = env["signer"]
        if not (_is_str(s["id"]) and _LAB_ID.fullmatch(s["id"])):
            problems.append("signer.id가 lab: grammar 밖")
        if not (_is_str(s["key_id"]) and _KEY_ID.fullmatch(s["key_id"])):
            problems.append("signer.key_id grammar 위반")
        if not (type(s["key_epoch"]) is int and s["key_epoch"] >= 1):
            problems.append("signer.key_epoch는 양의 정수")

    problems += _bad_keys(env["subject"], _SUBJECT_KEYS, "subject")
    if not problems:
        sub = env["subject"]
        prefix = _SUBJECT_PREFIX.get(sub["type"])
        if prefix is None:
            problems.append(f"subject.type이 enum 밖: {sub['type']!r}")
        elif not (_is_str(sub["id"]) and sub["id"].startswith(prefix)
                  and _SUBJECT_ID.fullmatch(sub["id"])):
            problems.append("subject.id가 type↔prefix strict grammar 위반")

    problems += _bad_keys(env["provenance"], _PROVENANCE_KEYS, "provenance")
    if not problems:
        pv = env["provenance"]
        for k in ("lab", "machine", "platform", "adapter"):
            if not _is_str(pv[k]):
                problems.append(f"provenance.{k} 비어있음")
        cv = pv["cli_version"]
        # 반례 C: null(관측 없음 — 정직) 또는 {value, capture 주소}. 자유 문자열 금지 —
        # 주소 없는 버전 주장은 성실히 파생한 값과 지어낸 값을 구별 불가능하게 만든다.
        if cv is not None:
            cvp = _bad_keys(cv, _CLI_VERSION_KEYS, "provenance.cli_version")
            problems += cvp
            if not cvp:
                if not _is_str(cv["value"]):
                    problems.append("cli_version.value 비어있음")
                if not (_is_str(cv["capture_artifact_sha256"])
                        and _HEX64.fullmatch(cv["capture_artifact_sha256"])):
                    problems.append("cli_version capture sha256 hex64 아님")
        cap = pv["capture"]
        if cap is not None:
            problems += _bad_keys(cap, _CAPTURE_KEYS, "provenance.capture")
            if not problems and not _HEX64.fullmatch(cap["capture_artifact_sha256"]):
                problems.append("provenance.capture sha256 hex64 아님")

    if not _is_str(env["idempotency_key"]) or len(env["idempotency_key"]) > 128:
        problems.append("idempotency_key 위반")
    if not _is_rfc3339_z(env["created_at"]):
        problems.append("created_at RFC3339-Z 위반(서술값이어도 모양은 지킨다)")

    if problems:
        return problems
    return _validate_payload(kind, env["payload"])


# ── per-kind payload 스키마 ──────────────────────────────────────────────────
# v0.1 §6은 "요지"라 exact key set은 이 기준 구현의 제안이다 — final critic 대상.

_ARTIFACT_KEYS = {"role", "schema_id", "sha256", "byte_length", "media_type"}
_CLAIM_KEYS = {"type", "scope", "evidence_basis", "attests_ordering_of"}
_CLAIM_OPT = {"evidence_availability"}
_CAUSAL_ALLOWED = {"prereg_ref", "supersedes", "revokes", "anchored_after"}
_EVIDENCE_BASIS_KEYS = {"method", "verifier_schema", "body_custody", "locator_authority"}
_TARGET_KEYS = {"lab_id", "to_id", "to_epoch"}
_INSTALL_OBS_KEYS = {"observed_at", "capture_artifact_sha256", "observation_method"}
_SEMANTICS_KEYS = {"registry_artifact_sha256", "version"}
_POLICY_KEYS = {"registry_artifact_sha256"}
_ANCHOR_PROOF_KEYS = {"anchor_event_id", "anchor_seq"}
_RUNNER_DIGEST_KEYS = {"binary_sha256", "config_sha256", "schema_sha256"}
_WINDOW_KEYS = {"basis", "max_span"}


def _payload_artifact_attested(p) -> list:
    keys = set(p) if isinstance(p, dict) else set()
    required = {"artifact", "bindings", "claim", "causal"}
    if not isinstance(p, dict) or keys != required:
        return [f"artifact.attested payload key set 위반: {sorted(keys)}"]
    problems = []
    a = p["artifact"]
    akeys = set(a) if isinstance(a, dict) else set()
    if not isinstance(a, dict) or not _ARTIFACT_KEYS <= akeys \
            or akeys - _ARTIFACT_KEYS - {"locator"}:      # locator만 선택
        problems.append(f"artifact key set 위반: {sorted(akeys)}")
    else:
        if not (_is_str(a["sha256"]) and _HEX64.fullmatch(a["sha256"])):
            problems.append("artifact.sha256 hex64 아님")
        if not (type(a["byte_length"]) is int and a["byte_length"] >= 0):
            problems.append("artifact.byte_length 위반")
    if not isinstance(p["bindings"], list):
        problems.append("bindings는 list")
    c = p["claim"]
    if not isinstance(c, dict) or not _CLAIM_KEYS <= set(c) or \
            set(c) - _CLAIM_KEYS - _CLAIM_OPT:
        problems.append("claim key set 위반")
    else:
        if c["attests_ordering_of"] not in _ORDERING_LEVELS:
            problems.append(f"attests_ordering_of가 enum 밖: {c['attests_ordering_of']!r}")
        eb = c["evidence_basis"]
        ebp = _bad_keys(eb, _EVIDENCE_BASIS_KEYS, "evidence_basis")
        problems += ebp
        if not ebp:
            if eb["method"] != "raw-bytes-sha256":
                problems.append("evidence_basis.method는 raw-bytes-sha256")
            if eb["locator_authority"] is not False:
                problems.append("locator는 authority가 아니다 — locator_authority는 false 고정")
    cz = p["causal"]
    if not isinstance(cz, dict) or set(cz) - _CAUSAL_ALLOWED:
        problems.append(f"causal 허용 밖 key: {sorted(set(cz) - _CAUSAL_ALLOWED) if isinstance(cz, dict) else cz!r}")
    return problems


def _payload_toolchain_observed(p) -> list:
    required = {"backend", "backend_version", "provider_route", "binary_digest",
                "observation_method", "install_observed_at", "version_capture"}
    problems = _bad_keys(p, required, "toolchain.observed payload")
    if problems:
        return problems
    # 반례 D(LxM): 이 payload의 값들은 r3부터 capture 맵에 **권위를 세운다** — 검증되지
    # 않은 값이 맵에 오르면 부주의한 발신자(빈 값·오타·초기값)가 정직한 후속 주장을
    # 영구 봉쇄한다. 맵에 오르는 필드는 봉투 층과 같은 규율을 받는다(등록 자격 = 이
    # 스키마를 통과한 admitted 값만).
    for k in ("backend", "backend_version", "provider_route", "observation_method"):
        if not _is_str(p[k]):
            problems.append(f"toolchain.{k}는 비어있지 않은 str")
    if not (_is_str(p["binary_digest"]) and _HEX64.fullmatch(p["binary_digest"])):
        problems.append("toolchain.binary_digest hex64 아님")
    if problems:
        return problems
    vc = p["version_capture"]
    problems += _bad_keys(vc, {"capture_artifact_sha256"}, "version_capture")
    if not problems and not _HEX64.fullmatch(vc["capture_artifact_sha256"]):
        problems.append("version_capture sha256 hex64 아님")
    io = p["install_observed_at"]
    # Ludex 반례 2: 값 없음을 없다고 기록 — null 필수 필드. Δ3: 값이 있으면 capture 결속.
    if io is not None:
        iop = _bad_keys(io, _INSTALL_OBS_KEYS, "install_observed_at")
        problems += iop
        if not iop:
            if not _HEX64.fullmatch(io["capture_artifact_sha256"]):
                problems.append("install_observed_at capture sha256 hex64 아님")
            if not _is_str(io["observation_method"]):
                problems.append("install_observed_at.observation_method 비어있음")
            # 반례 D-2(LxM): observed_at은 맵에 오르는 값인데 유일하게 미검증이었다.
            # created_at 선례 그대로 — 서술값이어도 모양은 지킨다. r5 HOLD 교정:
            # suffix가 아니라 _is_rfc3339_z(문법+달력)로 — schema와 collector 동일 predicate.
            if not _is_rfc3339_z(io["observed_at"]):
                problems.append("install_observed_at.observed_at RFC3339-Z 위반")
    return problems


def _payload_canary_result(p) -> list:
    required = {"toolchain_event_id", "canary_artifact_sha256", "leak", "act", "alive",
                "canary_semantics", "permission_policy", "passed"}
    problems = _bad_keys(p, required, "canary.result payload")
    if problems:
        return problems
    if not (_is_str(p["toolchain_event_id"]) and _HEX64.fullmatch(p["toolchain_event_id"])):
        problems.append("toolchain_event_id hex64 아님")
    if not _HEX64.fullmatch(p.get("canary_artifact_sha256") or ""):
        problems.append("canary_artifact_sha256 hex64 아님")
    for k in ("leak", "act", "alive", "passed"):
        if type(p[k]) is not bool:
            problems.append(f"canary.{k}는 bool")
    # Δ3: semantics/policy는 자유 문자열이 아니라 digest-pinned registry artifact 참조.
    sp_ = _bad_keys(p["canary_semantics"], _SEMANTICS_KEYS, "canary_semantics")
    problems += sp_
    if not sp_ and not _HEX64.fullmatch(p["canary_semantics"]["registry_artifact_sha256"]):
        problems.append("canary_semantics registry digest hex64 아님")
    pp_ = _bad_keys(p["permission_policy"], _POLICY_KEYS, "permission_policy")
    problems += pp_
    if not pp_ and not _HEX64.fullmatch(p["permission_policy"]["registry_artifact_sha256"]):
        problems.append("permission_policy registry digest hex64 아님")
    return problems


def _payload_message_posted(p) -> list:
    required = {"target", "body_locator", "body_sha256", "body_media_type"}
    problems = _bad_keys(p, required, "message.posted payload")
    if problems:
        return problems
    tp = _bad_keys(p["target"], _TARGET_KEYS, "target")
    problems += tp
    if not tp:
        t = p["target"]
        if not (_is_str(t["lab_id"]) and _LAB_ID.fullmatch(t["lab_id"])):
            problems.append("target.lab_id grammar 위반")
        if not _is_str(t["to_id"]):
            problems.append("target.to_id 비어있음")
        if not (type(t["to_epoch"]) is int and t["to_epoch"] >= 1):
            problems.append("target.to_epoch는 양의 정수")
    # v0.1 §5: 본문 비탑재 — locator+digest만. 평문 `body` 필드는 위의 exact key set이
    # 구조로 차단한다(별도 검사 불필요 — must-not이 스키마에 내장).
    if not _HEX64.fullmatch(p.get("body_sha256") or ""):
        problems.append("body_sha256 hex64 아님")
    return problems


def _payload_run_set_launch(p) -> list:
    required = {"walk_id", "prereg_anchor_proof", "runner_digests",
                "gate_canary_event_id", "nonce", "window", "attests_ordering_of"}
    problems = _bad_keys(p, required, "run_set.launch_authorized payload")
    if problems:
        return problems
    ap = _bad_keys(p["prereg_anchor_proof"], _ANCHOR_PROOF_KEYS, "prereg_anchor_proof")
    problems += ap
    if not ap:
        pr = p["prereg_anchor_proof"]
        if not _HEX64.fullmatch(pr.get("anchor_event_id") or ""):
            problems.append("prereg anchor_event_id hex64 아님")
        if not (type(pr["anchor_seq"]) is int and pr["anchor_seq"] >= 1):
            problems.append("prereg anchor_seq는 양의 정수")
    rd = _bad_keys(p["runner_digests"], _RUNNER_DIGEST_KEYS, "runner_digests")
    problems += rd
    if not rd and not all(_HEX64.fullmatch(p["runner_digests"][k])
                          for k in _RUNNER_DIGEST_KEYS):
        problems.append("runner_digests hex64 아님")
    if not _HEX64.fullmatch(p.get("gate_canary_event_id") or ""):
        problems.append("gate_canary_event_id hex64 아님")
    if not _is_str(p["nonce"]):
        problems.append("nonce 비어있음")
    wp = _bad_keys(p["window"], _WINDOW_KEYS, "window")
    problems += wp
    if not wp:
        # caller-time escalation 차단: window basis는 accepted_seq만 — wall-clock 거부.
        if p["window"]["basis"] not in _AUTHORITY_BASES:
            problems.append(f"window.basis가 authority enum 밖(caller-time 승격 금지): "
                            f"{p['window']['basis']!r}")
        if not (type(p["window"]["max_span"]) is int and p["window"]["max_span"] >= 1):
            problems.append("window.max_span은 양의 정수")
    if p["attests_ordering_of"] not in _ORDERING_LEVELS:
        problems.append("attests_ordering_of enum 밖")
    return problems


def _payload_run_set_completed(p) -> list:
    required = {"gate_event_id", "run_refs", "results_digest", "anchored_after",
                "attests_ordering_of"}
    problems = _bad_keys(p, required, "run_set.completed payload")
    if problems:
        return problems
    if not _HEX64.fullmatch(p.get("gate_event_id") or ""):
        problems.append("gate_event_id hex64 아님")
    if not (isinstance(p["run_refs"], list) and p["run_refs"]
            and all(_is_str(r) for r in p["run_refs"])):
        problems.append("run_refs는 비어있지 않은 str list")
    if not _HEX64.fullmatch(p.get("results_digest") or ""):
        problems.append("results_digest hex64 아님")
    # report_about_act — anchored_after 필수(Ludex 반례 1). null이면 스키마에서 거부.
    if not (_is_str(p["anchored_after"]) and _HEX64.fullmatch(p["anchored_after"])):
        problems.append("run_set.completed는 report_about_act — anchored_after hex64 필수")
    if p["attests_ordering_of"] not in _ORDERING_LEVELS:
        problems.append("attests_ordering_of enum 밖")
    return problems


_PAYLOAD_VALIDATORS = {
    "artifact.attested": _payload_artifact_attested,
    "toolchain.observed": _payload_toolchain_observed,
    "canary.result": _payload_canary_result,
    "provider.route.observed": lambda p: _bad_keys(
        p, {"route", "observation_method"}, "provider.route.observed payload"),
    "message.posted": _payload_message_posted,
    "message.read": lambda p: _bad_keys(p, {"cursor"}, "message.read payload"),
    "delivery.semantic_ack": lambda p: (
        _bad_keys(p, {"event_id", "payload_sha256", "target_cell", "target_epoch",
                      "outcome"}, "delivery.semantic_ack payload")
        or (["semantic_ack.outcome enum 밖"]
            if p["outcome"] not in ("applied", "deferred", "rejected") else [])),
    "hub.anchor": lambda p: _bad_keys(
        p, {"root", "tree_size", "seq_range", "anchor_service",
            "proof_artifact_sha256", "verification_method"}, "hub.anchor payload"),
    "key.rotated": lambda p: (
        _bad_keys(p, {"new_key_id", "new_key_epoch", "new_pubkey"}, "key.rotated payload")
        or ([] if (_is_str(p["new_key_id"]) and _KEY_ID.fullmatch(p["new_key_id"])
                   and type(p["new_key_epoch"]) is int and p["new_key_epoch"] >= 1
                   and _is_str(p["new_pubkey"]) and _HEX64.fullmatch(p["new_pubkey"]))
            else ["key.rotated 필드 shape 위반(key_id grammar·epoch≥1·pubkey hex64)"])),
    "key.revoked": lambda p: (
        _bad_keys(p, {"key_id", "key_epoch", "reason"}, "key.revoked payload")
        or ([] if (_is_str(p["key_id"]) and _KEY_ID.fullmatch(p["key_id"])
                   and type(p["key_epoch"]) is int and p["key_epoch"] >= 1
                   and _is_str(p["reason"]))
            else ["key.revoked 필드 shape 위반"])),
    "signer.introduced": lambda p: (
        _bad_keys(p, {"signer_id", "key_id", "key_epoch", "pubkey"},
                  "signer.introduced payload")
        or ([] if (_is_str(p["signer_id"]) and _LAB_ID.fullmatch(p["signer_id"])
                   and _is_str(p["key_id"]) and _KEY_ID.fullmatch(p["key_id"])
                   and type(p["key_epoch"]) is int and p["key_epoch"] >= 1
                   and _is_str(p["pubkey"]) and _HEX64.fullmatch(p["pubkey"]))
            else ["signer.introduced 필드 shape 위반(lab grammar·key_id·epoch≥1·"
                  "pubkey hex64)"])),
    "machine.rekeyed": lambda p: _bad_keys(
        p, {"machine_id", "new_key_id"}, "machine.rekeyed payload"),
    "machine.superseded": lambda p: _bad_keys(
        p, {"machine_id", "successor_machine_id"}, "machine.superseded payload"),
    "run_set.launch_authorized": _payload_run_set_launch,
    "run_set.completed": _payload_run_set_completed,
}


def _validate_payload(kind: str, p) -> list:
    problems = _PAYLOAD_VALIDATORS[kind](p)
    # target/subject conflation(Orin C7): addressed kind만 target을 갖는다.
    if kind not in ADDRESSED_KINDS and isinstance(p, dict) and "target" in p:
        problems.append(f"{kind}는 addressed kind가 아니다 — target 금지(subject≠target)")
    return problems


# ── canary passed 재도출 (Orin C8: free caller boolean 금지) ─────────────────

def derive_canary_passed(observed: dict, rule: dict) -> bool:
    """사전등록 rule 아래 관측 사실에서 passed를 재도출한다. rule은 digest-pinned
    registry artifact의 내용(`{"pass_when": {"leak": false, "act": false, "alive": true}}`)."""
    pw = rule.get("pass_when")
    if not isinstance(pw, dict) or set(pw) != {"leak", "act", "alive"}:
        raise HubEnvelopeError("canary rule 모양 위반 — pass_when {leak,act,alive} bool")
    return all(observed[k] == pw[k] for k in ("leak", "act", "alive"))


# ── claim registry (v0.1 §4) ────────────────────────────────────────────────

_CORE_NAMESPACES = ("core",)
_LAB_NAMESPACES = ("ludex", "lxm", "organum-code", "organum")


class ClaimRegistry:
    """versioned·digest-pinned claim registry. unknown claim은 보존·전달 가능하되
    authority/verification projection에서 fail-closed 제외."""

    def __init__(self, doc: dict, *, expected_sha256: str):
        raw = canonical_bytes(doc)
        actual = hashlib.sha256(raw).hexdigest()
        if actual != expected_sha256:
            raise HubEnvelopeError(f"claim registry digest 불일치: {actual}")
        self.sha256 = actual
        self._claims = {}
        for name, spec in doc.get("claims", {}).items():
            ns = name.split(":", 1)[0] if ":" in name else "core"
            if ns not in _CORE_NAMESPACES + _LAB_NAMESPACES:
                raise HubEnvelopeError(f"claim namespace 밖: {name}")
            need = {"act_class", "subject_types", "ordering_levels", "capture_required",
                    "revocation_authority"}
            if set(spec) != need:
                raise HubEnvelopeError(f"claim {name} spec key set 위반")
            if spec["act_class"] not in ("self_attesting", "report_about_act"):
                raise HubEnvelopeError(f"claim {name} act_class enum 밖")
            # B5(Orin): 미지원 값은 fail-open이 아니라 생성 시점 거부다. 이 profile이
            # 구현한 exact enum만 받는다 — "allow-anything" 같은 값이 무권한 통과로
            # 새는 경로를 constructor에서 막는다.
            if spec["revocation_authority"] != "same_signer":
                raise HubEnvelopeError(
                    f"claim {name} revocation_authority 미지원 값(구현 enum: same_signer): "
                    f"{spec['revocation_authority']!r}")
            if not (isinstance(spec["ordering_levels"], list) and spec["ordering_levels"]
                    and set(spec["ordering_levels"]) <= set(_ORDERING_LEVELS)):
                raise HubEnvelopeError(f"claim {name} ordering_levels enum 밖")
            if not (isinstance(spec["subject_types"], list) and spec["subject_types"]
                    and set(spec["subject_types"]) <= set(_SUBJECT_PREFIX)):
                raise HubEnvelopeError(f"claim {name} subject_types enum 밖")
            if type(spec["capture_required"]) is not bool:
                raise HubEnvelopeError(f"claim {name} capture_required는 bool")
            self._claims[name] = dict(spec, namespace=ns)

    def get(self, claim_type: str):
        return self._claims.get(claim_type)      # None = unknown → 소비측 fail-closed


# ── key registry + guard + idempotency + authority index ────────────────────

class KeyRegistry:
    """outer pubkey → logical signer 결속 + **lifecycle 이력**(§2, 종전 UNPROVEN → 구현).

    각 결속은 seq 좌표를 갖는다: `valid_from_seq`(0 = log 이전 bootstrap provisioning) ·
    `revoked_at_seq`(None = 유효). **"그때 유효했나"의 authority는 accepted_seq**다 —
    caller wall-clock이 아니다(§3 규율 그대로). lifecycle 전이는 admitted
    `key.rotated`/`key.revoked` 이벤트가 몰고, 이 클래스의 mutator는 그 이벤트 처리와
    bootstrap만 부른다."""

    def __init__(self):
        self._by_pubkey = {}

    def register(self, pubkey_hex: str, *, signer_id: str, key_id: str, key_epoch: int,
                 valid_from_seq: int = 0):
        """결속 등록 — 불변식은 **여기서** 강제한다(Orin C1: CLI/replay/library 어느
        entry point도 우회 불가): pubkey 유일 · (signer,key_id,epoch) tuple 유일 ·
        shape(grammar) 검증."""
        if not (_is_str(pubkey_hex) and _HEX64.fullmatch(pubkey_hex)):
            raise HubEnvelopeError("pubkey hex64 아님")
        if not (_is_str(signer_id) and _LAB_ID.fullmatch(signer_id)):
            raise HubEnvelopeError(f"signer_id가 lab: grammar 밖: {signer_id!r}")
        if not (_is_str(key_id) and _KEY_ID.fullmatch(key_id)):
            raise HubEnvelopeError(f"key_id grammar 위반: {key_id!r}")
        if not (type(key_epoch) is int and key_epoch >= 1):
            raise HubEnvelopeError("key_epoch는 양의 정수")
        if not (type(valid_from_seq) is int and valid_from_seq >= 0):
            raise HubEnvelopeError("valid_from_seq는 0 이상 정수")
        if pubkey_hex in self._by_pubkey:
            # 조용한 재사용 금지(§6 machine 계보와 같은 규율) — pubkey는 한 결속뿐.
            raise HubEnvelopeError("pubkey가 이미 결속됨(조용한 재결속 금지)")
        for e in self._by_pubkey.values():
            if (e["signer_id"], e["key_id"], e["key_epoch"]) == \
                    (signer_id, key_id, key_epoch):
                raise HubEnvelopeError(
                    f"(signer,key_id,epoch) tuple이 이미 결속됨: "
                    f"({signer_id},{key_id},{key_epoch})")
        self._by_pubkey[pubkey_hex] = {"signer_id": signer_id, "key_id": key_id,
                                       "key_epoch": key_epoch,
                                       "valid_from_seq": valid_from_seq,
                                       "revoked_at_seq": None}

    def revoke(self, pubkey_hex: str, *, at_seq: int = None):
        """revoked_at_seq 기록. at_seq=None은 bootstrap/테스트용 즉시 무효(=0)."""
        e = self._by_pubkey.get(pubkey_hex)
        if e is not None and e["revoked_at_seq"] is None:
            e["revoked_at_seq"] = 0 if at_seq is None else at_seq

    def was_valid(self, pubkey_hex: str, at_seq: int):
        """seq 좌표에서 이 키가 유효했나 — bool, 미등록이면 None(관측 불가≠거짓)."""
        e = self._by_pubkey.get(pubkey_hex)
        if e is None:
            return None
        if at_seq < e["valid_from_seq"]:
            return False
        return e["revoked_at_seq"] is None or at_seq < e["revoked_at_seq"]

    def bindings_of(self, signer_id: str) -> list:
        """한 signer의 전체 키 이력(감사용 읽기)."""
        return [dict(e, pubkey=pk) for pk, e in self._by_pubkey.items()
                if e["signer_id"] == signer_id]

    def lookup(self, pubkey_hex: str):
        e = self._by_pubkey.get(pubkey_hex)
        if e is None:
            return None
        return dict(e, revoked=e["revoked_at_seq"] is not None)


def idempotency_fingerprint(raw: bytes) -> str:
    """exact signed content bytes의 SHA-256(§3). 서버 부여 필드는 애초에 봉투 밖이라
    fingerprint에 들어올 수 없다."""
    return hashlib.sha256(bytes(raw)).hexdigest()


class HubIndex:
    """authority index + guard + transparency log 결속. admit()이 §2 guard 순서를
    집행한다: ① outer 서명 → ② 스키마 → ③ 정책. 실패는 stage와 함께 격리(quarantine)되고
    authority index·log 어느 쪽에도 편입되지 않는다.

    Orin final critic B1–B5(2026-08-15) 반영:
    - **B1**: byte-identical retry는 최초 admission 결과로 **수렴**한다(`duplicate: true`) —
      seq 증가·record 덮어쓰기·log append·새 receipt 전부 0.
    - **B2**: unknown claim은 transport admission과 authority projection을 **분리**한다 —
      admitted(전달·보존 가능) but `authority_projected=False`. authority ref 조회는
      projected 이벤트만 인정한다.
    - **B3**: validator의 타입 예외는 ingress 경계에서 quarantine으로 닫힌다(crash 이탈 0).
    - **B4**: 최초 admission → exact-once log append → signed relay receipt가 **한 경로**다.
      `accepted_seq == leaf_index + 1`이며, log가 admission 밖에서 전진하면 결속 위반으로
      멈춘다. reject/quarantine/retry는 tree를 바꾸지 않는다.
    - **B5**: registry의 미지원 enum·미등록 digest는 fail-open이 아니라 거부다.
    """

    def __init__(self, *, key_registry: KeyRegistry, claim_registry: ClaimRegistry,
                 log=None, receipt_seckey: bytes = None,
                 source_domain: str = "organum-hub/local"):
        if receipt_seckey is not None and log is None:
            raise HubEnvelopeError("relay receipt는 log 결속 없이 발행할 수 없다")
        self.keys = key_registry
        self.claims = claim_registry
        self._log = log                     # 주입식 — hub_log.TransparencyLog 호환
        self._receipt_seckey = receipt_seckey
        self._source_domain = source_domain
        self._by_id = {}                    # event_id → record (transport-admitted만)
        self._idem = {}                     # (signer_id, kind, idem_key) → {fingerprint, result}
        self._seq = 0
        self.quarantine = []                # 실패 이벤트 보존 — authority 아님
        self._canary_rules = {}             # digest-pinned canary rule artifact 등록부
        self._permission_policies = {}      # digest-pinned permission policy artifact 등록부
        self._capture_versions = {}         # 반례 C: capture digest → 그것이 낳은 버전 값
        self._capture_install_times = {}    # 동일 규율: capture digest → observed_at

    # ── 조회 ──
    @property
    def log(self):
        """결속된 transparency log(읽기용) — 영수증의 root에 대한 포함/일관성 증명은
        여기서 뽑는다. append는 admit 경로의 몫이다(외부 append는 결속 위반으로 잡힌다)."""
        return self._log

    def get(self, event_id: str):
        """transport-admitted record. **authority 근거로 쓰지 말 것** — authority()를 쓰라."""
        return self._by_id.get(event_id)

    def idem_prior(self, signer_id: str, event_kind: str, idempotency_key: str):
        """idem scope의 최초 admission 결과 — 있으면 duplicate 표기로 반환.
        발신 도구가 **서명·재전송 전에** 수렴 여부를 묻는 자리다(B1 계약의 조회면):
        같은 (signer, kind, idem key)의 주장은 bytes가 서술 필드(created_at)만 달라도
        새 이벤트가 아니라 최초로 수렴해야 한다."""
        e = self._idem.get((signer_id, event_kind, idempotency_key))
        return dict(e["result"], duplicate=True) if e else None

    def authority(self, event_id: str):
        """authority-projected record만. unknown claim 등 미투영 이벤트는 None(B2)."""
        rec = self._by_id.get(event_id)
        return rec if rec is not None and rec["authority_projected"] else None

    def _quarantined(self, stage, problems, raw):
        self.quarantine.append({"stage": stage, "problems": problems,
                                "event_id": event_id_of(raw)})
        return {"admitted": False, "stage": stage, "problems": problems,
                "event_id": event_id_of(raw), "accepted_seq": None, "duplicate": False,
                "authority_projected": False, "authority_reason": None, "receipt": None}

    def admit(self, raw: bytes, sig_hex: str, pubkey_hex: str) -> dict:
        """직접(비-wire) 경로 — outer 서명 = sha256(canonical envelope bytes)의 BIP-340."""
        # ① outer 서명 — 무엇보다 먼저.
        try:
            sig = bytes.fromhex(sig_hex)
            pub = bytes.fromhex(pubkey_hex)
        except ValueError:
            return self._quarantined("signature", ["sig/pubkey hex 아님"], raw)
        msg = hashlib.sha256(bytes(raw)).digest()
        if not _schnorr.verify(sig, msg, pub):
            return self._quarantined("signature", ["outer 서명 검증 실패"], raw)
        return self._admit_authenticated(raw, pubkey_hex, wire_event_id=None)

    def _admit_authenticated(self, raw: bytes, pubkey_hex: str, *,
                             wire_event_id) -> dict:
        """outer 서명이 **이미 검증된** bytes의 공통 admission 경로 (Orin W1).

        호출자는 둘뿐이다: ① `admit`(직접 경로 — envelope-hash 서명 검증 직후)
        ② `hub_wire.admit_wire`(wire 경로 — NIP-01 id 재계산 + wire Schnorr 검증 직후,
        v0.1 §2의 outer 서명을 Nostr wire 서명으로 해석). **이 메서드를 서명 검증 없이
        직접 부르는 것은 guard 순서 위반이다** — private 이름이 그 계약이다.
        wire_event_id는 receipt에 결속된다(직접 경로는 None — 관측 없음, 지어내지 않음)."""
        try:
            env = json.loads(bytes(raw))
        except ValueError:
            return self._quarantined("schema", ["JSON 파싱 실패"], raw)
        try:
            if canonical_bytes(env) != bytes(raw):
                return self._quarantined(
                    "schema", ["서명 bytes가 canonical form이 아님(재직렬화 digest 금지)"], raw)
        except HubEnvelopeError as e:
            return self._quarantined("schema", [str(e)], raw)

        # ② 스키마 — validator의 타입 예외는 crash가 아니라 quarantine(B3).
        try:
            problems = validate_envelope(env)
        except Exception as e:                       # noqa: BLE001 — ingress fail-closed
            return self._quarantined(
                "schema", [f"validator 예외 fail-closed: {type(e).__name__}: {e}"], raw)
        if problems:
            return self._quarantined("schema", problems, raw)

        # ③ 정책 1: exact retry 수렴(B1·Orin C3) — **binding 거부보다 먼저**.
        # 서명·canonical·schema를 통과한 exact bytes의 재전송은, 최초 admission의
        # authority pubkey까지 일치할 때만 최초 결과로 수렴한다(상태 무변 — 폐기된
        # 키에 신규 권한을 주는 게 아니라 과거 영수증을 돌려주는 것). pubkey가 다르면
        # (타 키 재서명 재전송) 수렴이 아니라 아래 binding/conflict 경로다.
        key = (env["signer"]["id"], env["event_kind"], env["idempotency_key"])
        fp = idempotency_fingerprint(raw)
        prior = self._idem.get(key)
        if prior is not None and prior["fingerprint"] == fp \
                and prior["pubkey"] == pubkey_hex:
            return dict(prior["result"], duplicate=True)

        # ③ 정책 2: signer binding — validity는 **부여될 좌표(seq+1)의 was_valid**로
        # (LxM: 감사 표면과 admission이 같은 술어를 쓴다 — 불리언 평탄화 제거).
        bind = self._binding_problems(env, pubkey_hex, at_seq=self._seq + 1)
        if bind:
            return self._quarantined("policy", bind, raw)

        # ③ 정책 3: idempotency conflict — 같은 scope에 다른 bytes.
        if prior is not None:
            return self._quarantined(
                "policy", ["idempotency conflict: 같은 (signer,kind,key)에 다른 bytes"], raw)

        # ③ 정책 3: kind/claim — 예외도 fail-closed(B3).
        try:
            problems, projected, reason = self._policy(env)
        except HubEnvelopeError as e:
            problems, projected, reason = [str(e)], False, None
        except Exception as e:                       # noqa: BLE001 — ingress fail-closed
            return self._quarantined(
                "policy", [f"policy 예외 fail-closed: {type(e).__name__}: {e}"], raw)
        if problems:
            return self._quarantined("policy", problems, raw)

        # admission — log append와 seq는 한 원자적 결과(B4). seq = leaf_index + 1.
        # C1(Orin r3): 오염 검사는 append **전**이다. 뒤에 하면 미수용 candidate bytes가
        # tree에 들어가고 재시도마다 tree가 자란다 — 탐지했는데 오염시키는 순서였다.
        # 오염 상태에서는 candidate를 건드리지 않고 멈춘다(per-event 격리가 아니라
        # 상태 손상 HOLD — 이 로그는 더 이상 admission의 결과가 아니다).
        eid = event_id_of(raw)
        if self._log is not None:
            if self._log.tree_size != self._seq:
                raise HubEnvelopeError(
                    f"log가 admission 밖에서 전진(결속 위반): tree_size "
                    f"{self._log.tree_size} != seq {self._seq} — candidate 미수용, tree 불변")
            index = self._log.append(bytes(raw))
            self._seq = index + 1
        else:
            self._seq += 1
        rec = {"event_id": eid, "accepted_seq": self._seq, "envelope": env,
               "pubkey": pubkey_hex, "fingerprint": fp,
               "authority_projected": projected, "authority_reason": reason}
        self._by_id[eid] = rec
        self._register_capture_claims(env)
        self._apply_key_lifecycle(env, self._seq)
        result = {"admitted": True, "stage": None, "problems": [], "event_id": eid,
                  "accepted_seq": self._seq, "duplicate": False,
                  "authority_projected": projected, "authority_reason": reason,
                  "receipt": self._issue_receipt(eid, fp, wire_event_id)}
        self._idem[key] = {"fingerprint": fp, "result": result, "pubkey": pubkey_hex}
        return result

    # ── relay receipt (v0.1 §2 + fingerprint echo §3 + Orin W1 identity 분리) ──
    def _issue_receipt(self, event_id: str, fingerprint: str, wire_event_id=None):
        if self._receipt_seckey is None:
            return None
        body = {"schema": RECEIPT_SCHEMA, "source_domain": self._source_domain,
                "event_id": event_id,                 # envelope identity(sha256 canonical)
                "content_sha256": event_id,           # exact content bytes — 현 매핑에선 동일
                # W1: wire와 envelope의 identity는 실제로 갈라진다(같은 봉투를 다른
                # created_at으로 재전송하면 wire id만 달라진다). receipt가 둘 다 결속하고,
                # 직접 경로는 관측이 없으므로 null(지어내지 않는다).
                "wire_event_id": wire_event_id,
                "idempotency_fingerprint": fingerprint,
                "accepted_seq": self._seq,
                "tree_size": self._log.tree_size,
                "root": self._log.root().hex()}
        sig = _schnorr.sign(hashlib.sha256(canonical_bytes(body)).digest(),
                            self._receipt_seckey)
        return {"body": body, "signature": sig.hex()}

    # ── 정책 층 ──
    def _binding_problems(self, env: dict, pubkey_hex: str, *, at_seq: int) -> list:
        """outer pubkey → registry → 내부 signer exact 일치(§2). self-assertion 불인정.
        validity는 **좌표 술어**(was_valid @ 부여될 seq) — 감사 표면과 같은 함수를
        쓴다(LxM: 두 술어의 일치를 관례가 아니라 호출로)."""
        entry = self.keys.lookup(pubkey_hex)
        if entry is None:
            return ["pubkey가 key registry에 없음(self-assertion 불인정)"]
        if not self.keys.was_valid(pubkey_hex, at_seq):
            if entry["revoked"]:
                return ["revoked key로 서명됨(이 좌표에서 무효)"]
            return [f"키가 좌표 {at_seq}에서 아직 유효하지 않음"
                    f"(valid_from_seq={entry['valid_from_seq']})"]
        s = env["signer"]
        if (entry["signer_id"], entry["key_id"], entry["key_epoch"]) != \
                (s["id"], s["key_id"], s["key_epoch"]):
            return ["inner signer ≠ outer key registry 결속(exact 일치 실패)"]
        return []

    def _policy(self, env: dict) -> tuple:
        """kind/claim 정책. 반환 (problems, authority_projected, authority_reason)."""
        base = self._capture_consistency(env)          # kind 무관 내적 정합(반례 C)
        kind = env["event_kind"]
        if kind == "artifact.attested":
            problems, projected, reason = self._policy_claim(env)
            return base + problems, projected, reason
        if kind == "key.rotated":
            return base + self._policy_key_rotated(env), True, None
        if kind == "key.revoked":
            return base + self._policy_key_revoked(env), True, None
        if kind == "signer.introduced":
            return base + self._policy_signer_introduced(env), True, None
        if kind == "canary.result":
            return base + self._policy_canary(env), True, None
        if kind == "run_set.launch_authorized":
            return base + self._policy_gate(env), True, None
        if kind == "run_set.completed":
            return base + self._policy_completed(env), True, None
        return base, True, None

    # ── 반례 C(LxM) 제안 2: capture 내적 정합 ──
    #
    # 반례 D-2(LxM) 처방 — "필드를 쫓지 말고 등록 지점에서 불변식으로": D와 D-2는 다른
    # 결함이 아니라 같은 실수의 두 사례였다. 값을 집어가는 자리와 검증하는 자리가 갈라져
    # 있으면 맵이 하나 늘 때마다 같은 반례가 재발한다. 그래서 **collector가 값을 내놓는
    # 그 지점**에서 자격을 강제한다 — 스키마가 구멍이어도 여기서 걸리고(정책 단계
    # quarantine), 새 맵을 추가하며 검증을 잊으면 그 kind의 모든 봉투가 시끄럽게 죽는다.

    @staticmethod
    def _eligible(digest, value, *, value_kind: str) -> tuple:
        """capture 맵 등록 자격 불변식. 위반은 예외 — 조용한 등록 없음."""
        if not (_is_str(digest) and _HEX64.fullmatch(digest)):
            raise HubEnvelopeError(f"capture 맵 등록 자격 위반: digest hex64 아님 — {digest!r}")
        if value_kind == "version":
            if not _is_str(value):
                raise HubEnvelopeError(
                    f"capture 맵 등록 자격 위반: 버전 값이 비어있지 않은 str 아님 — {value!r}")
        elif value_kind == "time":
            if not _is_rfc3339_z(value):
                raise HubEnvelopeError(
                    f"capture 맵 등록 자격 위반: 관측시각이 RFC3339-Z 아님 — {value!r}")
        else:
            raise HubEnvelopeError(f"알 수 없는 value_kind: {value_kind!r}")
        return digest, value

    @classmethod
    def _version_claims_of(cls, env: dict) -> list:
        """이 봉투가 세우는 (capture digest → 버전 값) 주장들. 전부 자격 검사 통과."""
        out = []
        cv = env["provenance"]["cli_version"]
        if isinstance(cv, dict):
            out.append(cls._eligible(cv["capture_artifact_sha256"], cv["value"],
                                     value_kind="version"))
        if env["event_kind"] == "toolchain.observed":
            p = env["payload"]
            out.append(cls._eligible(p["version_capture"]["capture_artifact_sha256"],
                                     p["backend_version"], value_kind="version"))
        return out

    @classmethod
    def _install_claims_of(cls, env: dict) -> list:
        if env["event_kind"] != "toolchain.observed":
            return []
        io = env["payload"]["install_observed_at"]
        if isinstance(io, dict):
            return [cls._eligible(io["capture_artifact_sha256"], io["observed_at"],
                                  value_kind="time")]
        return []

    def _capture_consistency(self, env: dict) -> list:
        """같은 capture artifact가 서로 다른 값을 낳을 수 없다 — index가 이미 가진
        정보만으로 판정되는 순수 내적 정합. idempotency conflict와 같은 층이다.

        ①(지어낸 값 + 형식상 완전한 결속)은 봉투 층에서 원리적으로 못 막는다 —
        재도출 재료(artifact bytes)가 봉투 밖이다. 그 정직 경계는 문서 §UNPROVEN.
        ②(같은 근거에서 다른 주장)는 여기서 막는다."""
        problems = []
        seen_v, seen_i = {}, {}
        for digest, value in self._version_claims_of(env):
            known = self._capture_versions.get(digest, seen_v.get(digest))
            if known is not None and known != value:
                problems.append(f"capture 모순: {digest[:12]}…는 이미 버전 "
                                f"{known!r}를 낳았는데 {value!r} 주장")
            seen_v[digest] = value
        for digest, value in self._install_claims_of(env):
            known = self._capture_install_times.get(digest, seen_i.get(digest))
            if known is not None and known != value:
                problems.append(f"capture 모순: {digest[:12]}…는 이미 관측시각 "
                                f"{known!r}를 낳았는데 {value!r} 주장")
            seen_i[digest] = value
        return problems

    def _register_capture_claims(self, env: dict) -> None:
        """capture 맵 등록 자격(반례 D 제안 2, 규약으로 명시):

        1. **schema를 통과해 admitted된 봉투의 값만** 오른다 — 격리된 이벤트는 아무것도
           세우지 못하고, 맵에 오르는 필드(backend_version·cli_version.value·
           install observed_at)는 스키마 층이 비어있지 않은 str/hex64임을 보장한다.
        2. 등록은 **first-writer-wins이며 되돌림(unwind) 경로가 현재 없다** — 이것은
           의도가 아니라 한계다(문서 §UNPROVEN). frozen §4 revocation은 claim event만
           대상이라(C2) 관측 이벤트의 값 주장을 내릴 스펙 경로가 없다. unwind 설계는
           dispute claim type과 같은 슬롯(원 signer만 자기 주장을 내리는 방향)."""
        for digest, value in self._version_claims_of(env):
            self._capture_versions[digest] = value
        for digest, value in self._install_claims_of(env):
            self._capture_install_times[digest] = value

    def _policy_claim(self, env) -> tuple:
        problems = []
        c = env["payload"]["claim"]
        spec = self.claims.get(c["type"])
        if spec is None:
            # B2: unknown claim은 전달·보존 가능(admitted)하되 authority에 투영되지 않는다.
            # claim-spec 의존 검사는 spec 없이는 평가 불가라 수행하지 않는다 — 그 미평가가
            # 곧 미투영의 이유다.
            return [], False, "unknown_claim"
        if env["subject"]["type"] not in spec["subject_types"]:
            problems.append("claim이 허용하지 않는 subject type")
        if c["attests_ordering_of"] not in spec["ordering_levels"]:
            problems.append("claim이 허용하지 않는 ordering level")
        causal = env["payload"]["causal"]
        if spec["act_class"] == "report_about_act":
            aa = causal.get("anchored_after")
            if not (_is_str(aa) and _HEX64.fullmatch(aa)):
                problems.append("report_about_act인데 causal.anchored_after 없음")
            elif self.authority(aa) is None:
                problems.append("anchored_after가 authority-projected event를 가리키지 않음")
        rv = causal.get("revokes")
        if rv is not None:
            problems += self._policy_revocation(env, rv, spec)
        if spec["capture_required"] and env["provenance"]["capture"] is None:
            problems.append("capture-required claim인데 provenance.capture=null")
        return problems, True, None

    def _policy_revocation(self, env, revoked_id, spec) -> list:
        """C2(Orin r3): frozen v0.1 §4 — revocation은 **같은 claim namespace/scope의 권한
        있는 signer**만. same_signer 결정은 namespace-authority signer 모드를 안 만든다는
        뜻이지 namespace/scope 결속을 삭제한다는 뜻이 아니었다. 순서: target이 claim
        event인가 → namespace 일치 → scope exact 일치 → 발행 signer 일치."""
        target = self.authority(revoked_id) if _is_str(revoked_id) else None
        if target is None:
            return ["revokes가 authority-projected event를 가리키지 않음"]
        tenv = target["envelope"]
        if tenv["event_kind"] != "artifact.attested":
            return ["revokes target이 claim event(artifact.attested)가 아님 — "
                    "claim 아닌 이벤트에는 revocation 개념이 없다"]
        tclaim, rclaim = tenv["payload"]["claim"], env["payload"]["claim"]
        t_ns = tclaim["type"].split(":", 1)[0]
        r_ns = rclaim["type"].split(":", 1)[0]
        if t_ns != r_ns:
            return [f"revokes cross-namespace 금지: {r_ns} 클레임이 {t_ns} 클레임을 취소 시도"]
        if tclaim["scope"] != rclaim["scope"]:
            return [f"revokes scope 불일치: {rclaim['scope']!r} ≠ {tclaim['scope']!r}"]
        # B5: ClaimRegistry가 same_signer만 admit하므로 여기 도달한 spec은 그 모드다.
        if tenv["signer"]["id"] != env["signer"]["id"]:
            return ["unauthorized revocation: 발행 signer가 아님"]
        return []

    def _policy_key_rotated(self, env) -> list:
        """key lifecycle(§2): rotation은 **현재 유효한 자기 키의 서명**으로만(봉투 signer
        binding이 그것을 이미 보장) + new_pubkey는 미결속이어야 한다(조용한 재사용 금지)."""
        p = env["payload"]
        problems = []
        if self.keys.lookup(p["new_pubkey"]) is not None:
            problems.append("new_pubkey가 이미 결속됨(다른 키의 조용한 재사용 금지)")
        for b in self.keys.bindings_of(env["signer"]["id"]):
            if (b["key_id"], b["key_epoch"]) == (p["new_key_id"], p["new_key_epoch"]):
                problems.append("같은 signer에 (key_id, epoch)가 이미 존재")
        return problems

    def _policy_key_revoked(self, env) -> list:
        """revocation은 **자기 signer의 키만**(same_signer 규율) — 대상 (key_id, epoch)이
        그 signer의 유효 결속이어야 한다."""
        p = env["payload"]
        mine = [b for b in self.keys.bindings_of(env["signer"]["id"])
                if (b["key_id"], b["key_epoch"]) == (p["key_id"], p["key_epoch"])]
        if not mine:
            return ["revoke 대상 (key_id, epoch)가 이 signer의 결속이 아님"]
        if mine[0]["revoked_at_seq"] is not None:
            return ["이미 revoked된 키"]
        return []

    def _introducer_authority(self) -> str | None:
        """도입 authority = **hub 운영 lab** — source_domain에서 구조적으로 파생
        (Orin I1 폐쇄 + Ludex r2 반증 교정). 파생 규칙(r3):
        ① 첫 조각이 `lab:x` 문법이면 그것 — 자기 선언, bootstrap pin은
          source_domain 자체가 진다.
        ② 첫 조각 n이 문법 밖(**bare name** — 공개 CLI init이 허용하는 표면)이면
          `lab:n`이 **bootstrap 결속**(valid_from_seq=0, init 시점 C1-guarded
          hub.json pin)으로 존재할 때만 그것으로 파생. bare name은 자기 선언이
          아니므로 registry 교차 확인을 요구하고, 도입 결속(valid_from_seq>0)은
          자격이 없어 authority가 로그 순서에 좌우되지 않는다(결정적).
        어느 쪽도 아니면 None = 아무도 도입 못 함(fail-closed — 라이브러리 기본
        "organum-hub/local"이 이 경우). 설정 필드가 아니라 파생이라 silent
        post-log mutation 표면이 없고(hub.json 손편집 마이그레이션은 배제가
        규율이다), direct admit·wire·CLI·replay가 전부 이 한 술어를 지난다.
        위임/allowlist 확장은 명시적 다음 정책 슬롯 — "registry에 있으면
        누구나"는 금지."""
        head = self._source_domain.split("/", 1)[0]
        if _LAB_ID.fullmatch(head):
            return head
        cand = f"lab:{head}"
        if _LAB_ID.fullmatch(cand) and any(
                b["valid_from_seq"] == 0 for b in self.keys.bindings_of(cand)):
            return cand
        return None

    def _policy_signer_introduced(self, env) -> list:
        """신규 서명자 도입(0.4.2 — Ludex 실사용이 밟은 구멍: log가 선 hub에
        제3자 signer를 들일 경로 부재). 규율 넷:
        ① **도입 = 로컬 membership 결정**(Orin I1 문장) — 서명자 admission
          eligibility를 부여한다. 개별 claim의 진실·역할 권위는 해당 kind/claim
          정책이 별도로 정한다(개방은 프로토콜에, 큐레이션은 정책에).
        ② **도입 authority는 hub 운영 lab만**(I1) — 등록 peer는 도입 못 하고,
          도입된 signer도 권한을 자동 상속하지 않는다(재귀 membership CA 금지·
          signer ID 선점 금지).
        ③ 도입 대상은 **결속이 전혀 없는 signer만** — 기존 signer의 키 추가는 그
          signer 자신의 rotate-key 전용(타인이 남의 authority를 늘릴 수 없다).
        ④ 도입자 revoke의 **연쇄는 프리미티브가 아니다** — 도입은 사실 기록이지
          살아있는 의존이 아니고, 연쇄 여부는 정책 층 결정으로 남긴다."""
        p = env["payload"]
        problems = []
        auth = self._introducer_authority()
        if auth is None or env["signer"]["id"] != auth:
            problems.append(f"도입 authority 없음 — introducer는 hub 운영 lab"
                            f"({auth})만, 등록 peer 불가(I1 fail-closed)")
        if p["signer_id"] == env["signer"]["id"]:
            problems.append("자기 도입 금지 — 자기 키는 rotate-key로")
        elif self.keys.bindings_of(p["signer_id"]):
            problems.append("이미 결속 있는 signer — 신규 도입 아님"
                            "(기존 signer 키는 rotate-key로)")
        if self.keys.lookup(p["pubkey"]) is not None:
            problems.append("pubkey가 이미 결속됨(조용한 재사용 금지)")
        return problems

    def _apply_key_lifecycle(self, env: dict, accepted_seq: int) -> None:
        """admitted lifecycle 이벤트만 registry를 전이시킨다.

        **전이 효력은 이벤트 좌표의 다음(n+1)부터다**(Orin C2) — 그래야 "그때
        유효했나"가 event-coordinate 의미로 성립한다: rotation 이벤트 자신은 옛 키가
        서명했으니 새 키는 n에선 아직 무효고, revocation 이벤트를 서명한 옛 키는 자기
        이벤트 좌표 n에서 유효하다. 불변식: 모든 admitted record에 대해
        `was_valid(record.pubkey, record.accepted_seq) is True`."""
        kind = env["event_kind"]
        p = env["payload"]
        if kind == "key.rotated":
            self.keys.register(p["new_pubkey"], signer_id=env["signer"]["id"],
                               key_id=p["new_key_id"], key_epoch=p["new_key_epoch"],
                               valid_from_seq=accepted_seq + 1)
        elif kind == "key.revoked":
            for b in self.keys.bindings_of(env["signer"]["id"]):
                if (b["key_id"], b["key_epoch"]) == (p["key_id"], p["key_epoch"]):
                    self.keys.revoke(b["pubkey"], at_seq=accepted_seq + 1)
        elif kind == "signer.introduced":
            # bootstrap(valid_from_seq=0)과 달리 소급이 없다 — C1이 지키는 것과
            # 같은 불변식. 도입 전 게이트 수준으로 검증해 둔 봉투는 도입 뒤
            # 재-admit으로 정식화 가능하다(admit 좌표가 도입 뒤면 위반 아님).
            self.keys.register(p["pubkey"], signer_id=p["signer_id"],
                               key_id=p["key_id"], key_epoch=p["key_epoch"],
                               valid_from_seq=accepted_seq + 1)

    def register_canary_rule(self, rule: dict) -> str:
        sha = canonical_sha(rule)
        self._canary_rules[sha] = rule
        return sha

    def register_permission_policy(self, policy: dict) -> str:
        sha = canonical_sha(policy)
        self._permission_policies[sha] = policy
        return sha

    def _policy_canary(self, env) -> list:
        problems = []
        p = env["payload"]
        eid = event_id_of(canonical_bytes(env))
        if p["toolchain_event_id"] == eid:
            problems.append("canary self-reference(자기 event를 근거로 사용)")
        tc = self.authority(p["toolchain_event_id"])
        if tc is None:
            problems.append("toolchain_event_id가 authority-projected event가 아님")
        elif tc["envelope"]["event_kind"] != "toolchain.observed":
            problems.append("canary의 predecessor는 toolchain.observed여야 함")
        rule = self._canary_rules.get(p["canary_semantics"]["registry_artifact_sha256"])
        if rule is None:
            problems.append("canary semantics registry artifact 미등록(자유 문자열 금지)")
        else:
            derived = derive_canary_passed(
                {k: p[k] for k in ("leak", "act", "alive")}, rule)
            if p["passed"] != derived:
                problems.append(f"canary passed mismatch: 기재 {p['passed']} ≠ 재도출 {derived}")
        # B5: permission policy도 digest 모양이 아니라 **등록된 artifact 해석**이 조건.
        if p["permission_policy"]["registry_artifact_sha256"] not in self._permission_policies:
            problems.append("permission policy registry artifact 미등록(digest 모양만으론 불충분)")
        return problems

    def _policy_gate(self, env) -> list:
        problems = []
        p = env["payload"]
        anchor = self.authority(p["prereg_anchor_proof"]["anchor_event_id"])
        if anchor is None:
            return ["prereg anchor proof가 authority-projected event를 가리키지 않음 — launch 0"]
        if anchor["envelope"]["event_kind"] != "hub.anchor":
            problems.append("prereg anchor ref가 hub.anchor가 아님")
        if anchor["accepted_seq"] != p["prereg_anchor_proof"]["anchor_seq"]:
            problems.append("anchor_seq가 admitted 기록과 불일치")
        canary = self.authority(p["gate_canary_event_id"])
        if canary is None:
            problems.append("gate canary가 authority-projected event가 아님")
        elif canary["envelope"]["event_kind"] != "canary.result":
            problems.append("gate canary ref가 canary.result가 아님")
        if p["attests_ordering_of"] == "bracketed_execution" and canary is not None:
            if canary["accepted_seq"] <= anchor["accepted_seq"]:
                problems.append("bracketed_execution인데 gate canary가 prereg 앵커보다 이름")
        return problems

    def _policy_completed(self, env) -> list:
        problems = []
        p = env["payload"]
        gate = self.authority(p["gate_event_id"])
        if gate is None:
            return ["completed의 gate_event_id가 authority-projected event가 아님"]
        if gate["envelope"]["event_kind"] != "run_set.launch_authorized":
            problems.append("gate ref가 run_set.launch_authorized가 아님")
        if p["anchored_after"] != p["gate_event_id"]:
            problems.append("completed.anchored_after는 자기 gate여야 함(Δ6-1)")
        if p["attests_ordering_of"] == "bracketed_execution" and \
                gate["envelope"]["payload"]["attests_ordering_of"] != "bracketed_execution":
            problems.append("bracketed 라벨인데 gate가 bracketed로 admitted되지 않음")
        return problems


def verify_relay_receipt(receipt: dict, pubkey: bytes,
                         *, expected_source_domain: str = None) -> bool:
    """hub relay receipt v2 검증(fail-closed) — 서명만이 아니라 **exact body contract**까지
    (Orin R2: shape를 구분하지 않는 verifier는 같은 버전 아래 비호환 body를 다 통과시킨다).
    consumer는 fingerprint echo를 자기 계산과 대조하고, expected_source_domain을 주면
    구성된 도메인과 대조한다(지어낸 기본값이 production source로 읽히지 않게)."""
    try:
        if not isinstance(receipt, dict) or set(receipt) != {"body", "signature"}:
            return False
        body, sig = receipt["body"], bytes.fromhex(receipt["signature"])
        if len(sig) != 64:
            return False
        if not isinstance(body, dict) or set(body) != _RECEIPT_BODY_KEYS:
            return False
        if body["schema"] != RECEIPT_SCHEMA:
            return False
        if not (_is_str(body["source_domain"])):
            return False
        if expected_source_domain is not None and \
                body["source_domain"] != expected_source_domain:
            return False
        for k in ("event_id", "content_sha256", "idempotency_fingerprint", "root"):
            if not (_is_str(body[k]) and _HEX64.fullmatch(body[k])):
                return False
        w = body["wire_event_id"]
        if w is not None and not (_is_str(w) and _HEX64.fullmatch(w)):
            return False
        if body["content_sha256"] != body["event_id"]:      # 현 profile 의미 assert
            return False
        for k in ("accepted_seq", "tree_size"):
            if not (type(body[k]) is int and body[k] >= 1):
                return False
        raw = canonical_bytes(body)
    except (KeyError, TypeError, ValueError, HubEnvelopeError):
        return False
    return _schnorr.verify(sig, hashlib.sha256(raw).digest(), pubkey)


# ── role/plane 격납 (v0.1 §5 + v0.2 Δ4) ─────────────────────────────────────

ROLES = ("substrate", "lab_operator", "measured_creature")


def admit_to_role(role: str, envelope: dict, *, target_cell: str = None,
                  target_epoch: int = None) -> dict:
    """이벤트를 role의 컨텍스트에 넣어도 되는가 — **구조적 거부**를 코드로.

    - substrate: 전부(raw log/API/key 접근은 여기만).
    - lab_operator: exact target+epoch를 통과한 coordination payload만.
    - measured_creature: **Hub-유래 payload 절대 금지**(evidence plane은 물론
      coordination도 — 측정 protocol이 사전등록한 task input만)."""
    if role not in ROLES:
        raise HubEnvelopeError(f"알 수 없는 role: {role}")
    kind = envelope["event_kind"]
    plane = plane_of(kind)
    if role == "substrate":
        return {"admitted": True, "reason": "substrate 경계"}
    if role == "measured_creature":
        return {"admitted": False,
                "reason": "measured_creature에는 Hub-유래 payload 주입 금지(Δ4)"}
    # lab_operator
    if plane != "coordination":
        return {"admitted": False, "reason": "lab_operator에 evidence plane 금지(Δ4)"}
    if kind != "message.posted":
        # read 커서·semantic_ack은 transport 기록 — adapter/substrate의 것.
        return {"admitted": False, "reason": "lab_operator에는 addressed message만 전달"}
    t = envelope["payload"]["target"]
    if t["to_id"] != target_cell or t["to_epoch"] != target_epoch:
        return {"admitted": False,
                "reason": "target exact binding 불일치(alias 재해석 금지)"}
    return {"admitted": True, "reason": "exact target+epoch 일치"}


HUB_PLANE_SOURCES = ("plane:evidence", "plane:coordination")


def assert_source_allowlist(sources: list, *, allowlist: tuple) -> None:
    """LxM assert 패턴(Δ4 must-not): 피험체 프롬프트는 알려진 source-allowlist에서만
    조립되고 hub plane은 그 allowlist에 절대 없다. 위반은 예외 — 조용한 통과 없음."""
    for s in sources:
        if s in HUB_PLANE_SOURCES:
            raise HubEnvelopeError(f"피험체 컨텍스트에 hub plane 소스 유입: {s}")
        if s not in allowlist:
            raise HubEnvelopeError(f"allowlist 밖 소스: {s}")
