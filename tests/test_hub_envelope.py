"""hub 봉투 v0.2 기준 구현 회귀 — **negative fixture 17종 fail-closed** + 정상 체인.

fixture 번호는 v0.1 §9(Orin 재제출 9 + Ludex 2 + LxM 2 = 13) + v0.2 Δ5(+4 = 17)의
시험 목록을 따른다. 각 테스트 docstring에 [fixture N] 표기. P1 final critic의 소비 대상.
"""
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from organum import hub_envelope as he  # noqa: E402
from organum import hub_log as hl  # noqa: E402
from organum import schnorr_pure as sp  # noqa: E402

SEC_A = (7).to_bytes(32, "big")
SEC_B = (11).to_bytes(32, "big")
SEC_HUB = (17).to_bytes(32, "big")                    # relay receipt 서명용 hub key
PUB_A = sp.public_key(SEC_A).hex()
PUB_B = sp.public_key(SEC_B).hex()
PUB_HUB = sp.public_key(SEC_HUB)
H = "a" * 64
PERMISSION_POLICY = {"allowed_acts": ["read"], "version": "v1"}

CLAIMS_DOC = {"claims": {
    "core:artifact.frozen": {
        "act_class": "self_attesting", "subject_types": ["artifact"],
        "ordering_levels": ["emission"], "capture_required": False,
        "revocation_authority": "same_signer"},
    "core:measurement.result": {
        "act_class": "report_about_act", "subject_types": ["run"],
        "ordering_levels": ["emission"], "capture_required": True,
        "revocation_authority": "same_signer"},
    "organum:note.frozen": {                       # C2 cross-namespace 회귀용
        "act_class": "self_attesting", "subject_types": ["artifact"],
        "ordering_levels": ["emission"], "capture_required": False,
        "revocation_authority": "same_signer"},
}}
CANARY_RULE = {"pass_when": {"leak": False, "act": False, "alive": True}}


def _mk_hub(*, with_log=True):
    keys = he.KeyRegistry()
    keys.register(PUB_A, signer_id="lab:organum-cody", key_id="k1", key_epoch=1)
    keys.register(PUB_B, signer_id="lab:ludex-cody", key_id="k1", key_epoch=1)
    claims = he.ClaimRegistry(CLAIMS_DOC, expected_sha256=he.canonical_sha(CLAIMS_DOC))
    log = hl.TransparencyLog() if with_log else None
    return he.HubIndex(key_registry=keys, claim_registry=claims, log=log,
                       receipt_seckey=SEC_HUB if with_log else None)


@pytest.fixture
def hub():
    """B4 조립 상태가 기본: log 결속 + relay receipt 발행."""
    return _mk_hub()


_N = [0]


def _envelope(kind, payload, *, signer="lab:organum-cody", subject=None, capture=None):
    _N[0] += 1
    return {
        "envelope_schema": he.ENVELOPE_SCHEMA, "event_kind": kind,
        "signer": {"id": signer, "key_id": "k1", "key_epoch": 1},
        "subject": subject or {"type": "run", "id": "run:cal-01"},
        "provenance": {"lab": signer, "machine": "m-01", "platform": "darwin",
                       "adapter": "organum-hub/0.1", "cli_version": None,
                       "capture": capture},
        "idempotency_key": f"idem-{_N[0]}",
        "created_at": "2026-08-15T00:00:00Z",
        "payload": payload,
    }


def _admit(hub, env, sec=SEC_A, pub=None, raw=None):
    raw = raw if raw is not None else he.canonical_bytes(env)
    sig = sp.sign(hashlib.sha256(raw).digest(), sec)
    return hub.admit(raw, sig.hex(), pub or sp.public_key(sec).hex())


# ── 정상 체인 재료 ──────────────────────────────────────────────────────────

def _anchor_payload():
    return {"root": H, "tree_size": 4, "seq_range": [1, 4], "anchor_service": "ots",
            "proof_artifact_sha256": H, "verification_method": "ots-verify"}


def _toolchain_payload():
    return {"backend": "opencode", "backend_version": "1.18.3",
            "provider_route": "upstage/solar-pro4", "binary_digest": H,
            "observation_method": "exec-capture", "install_observed_at": None,
            "version_capture": {"capture_artifact_sha256": H}}


def _canary_payload(toolchain_id, rule_sha, policy_sha, **facts):
    obs = {"leak": False, "act": False, "alive": True, **facts}
    return {"toolchain_event_id": toolchain_id, "canary_artifact_sha256": H,
            **obs, "passed": he.derive_canary_passed(obs, CANARY_RULE),
            "canary_semantics": {"registry_artifact_sha256": rule_sha, "version": "v1"},
            "permission_policy": {"registry_artifact_sha256": policy_sha}}


def _gate_payload(anchor_id, anchor_seq, canary_id, ordering="bracketed_execution"):
    return {"walk_id": "walk-01",
            "prereg_anchor_proof": {"anchor_event_id": anchor_id, "anchor_seq": anchor_seq},
            "runner_digests": {"binary_sha256": H, "config_sha256": H, "schema_sha256": H},
            "gate_canary_event_id": canary_id, "nonce": "n-01",
            "window": {"basis": "accepted_seq", "max_span": 100},
            "attests_ordering_of": ordering}


def _chain(hub):
    """prereg anchor → toolchain → canary → gate 체인을 admitted 상태로 세운다."""
    rule_sha = hub.register_canary_rule(CANARY_RULE)
    policy_sha = hub.register_permission_policy(PERMISSION_POLICY)
    anchor = _admit(hub, _envelope("hub.anchor", _anchor_payload(),
                                   subject={"type": "route", "id": "route:hub-log"}))
    tc = _admit(hub, _envelope("toolchain.observed", _toolchain_payload(),
                               subject={"type": "machine", "id": "machine:m-01"}))
    canary = _admit(hub, _envelope("canary.result",
                                   _canary_payload(tc["event_id"], rule_sha, policy_sha)))
    gate = _admit(hub, _envelope("run_set.launch_authorized",
                                 _gate_payload(anchor["event_id"], anchor["accepted_seq"],
                                               canary["event_id"])))
    return anchor, tc, canary, gate, rule_sha


# ═══ 정상 경로 ═══════════════════════════════════════════════════════════════

def test_전체_체인_admitted(hub):
    anchor, tc, canary, gate, _ = _chain(hub)
    for r in (anchor, tc, canary, gate):
        assert r["admitted"], r
    done = _admit(hub, _envelope("run_set.completed", {
        "gate_event_id": gate["event_id"], "run_refs": ["run:cal-01"],
        "results_digest": H, "anchored_after": gate["event_id"],
        "attests_ordering_of": "bracketed_execution"}))
    assert done["admitted"], done
    assert done["accepted_seq"] == 5          # 전역 순서는 admitted 순서


def test_self_attesting_claim_admitted(hub):
    env = _envelope("artifact.attested", {
        "artifact": {"role": "prereg", "schema_id": "s/v1", "sha256": H,
                     "byte_length": 10, "media_type": "application/json"},
        "bindings": [], "causal": {},
        "claim": {"type": "core:artifact.frozen", "scope": "organum",
                  "attests_ordering_of": "emission",
                  "evidence_basis": {"method": "raw-bytes-sha256",
                                     "verifier_schema": "v/1", "body_custody": "repo",
                                     "locator_authority": False}}},
        subject={"type": "artifact", "id": "artifact:prereg-v1"})
    assert _admit(hub, env)["admitted"]


def test_guard_순서가_기록된다(hub):
    """실패 stage(signature/schema/policy)가 격리 기록에 남고 index에 안 들어간다."""
    env = _envelope("message.read", {"cursor": "c-1"})
    raw = he.canonical_bytes(env)
    bad_sig = "0" * 128
    r = hub.admit(raw, bad_sig, PUB_A)
    assert not r["admitted"] and r["stage"] == "signature"
    assert hub.get(r["event_id"]) is None
    assert hub.quarantine[-1]["stage"] == "signature"


def test_비canonical_bytes는_서명이_맞아도_거부(hub):
    """서명 대상 bytes가 canonical form 그 자체여야 한다 — 재직렬화 digest 금지."""
    env = _envelope("message.read", {"cursor": "c-1"})
    raw = json.dumps(env, indent=2).encode()          # 논리 동일·bytes 다름
    r = _admit(hub, env, raw=raw)
    assert not r["admitted"] and r["stage"] == "schema"
    assert any("canonical" in p for p in r["problems"])


# ═══ negative fixtures 1–17 ═════════════════════════════════════════════════

def test_f01_wrong_kind_fields(hub):
    """[fixture 1] kind A의 payload 필드를 kind B에 — strict tagged union 거부."""
    env = _envelope("message.read", {"cursor": "c", "walk_id": "w"})
    r = _admit(hub, env)
    assert not r["admitted"] and r["stage"] == "schema"


def test_f02_inner_outer_signer_mismatch(hub):
    """[fixture 2] outer key는 lab:ludex-cody 등록인데 내부 signer는 organum-cody."""
    env = _envelope("message.read", {"cursor": "c"}, signer="lab:organum-cody")
    r = _admit(hub, env, sec=SEC_B)                   # B의 키로 서명
    assert not r["admitted"] and r["stage"] == "policy"
    assert any("exact 일치 실패" in p for p in r["problems"])


def test_f02b_미등록_pubkey는_self_assertion_불인정(hub):
    sec_c = (13).to_bytes(32, "big")
    env = _envelope("message.read", {"cursor": "c"})
    r = _admit(hub, env, sec=sec_c)
    assert not r["admitted"]
    assert any("self-assertion 불인정" in p for p in r["problems"])


def test_f03_same_idem_different_bytes(hub):
    """[fixture 3] 같은 (signer,kind,idem_key)에 다른 bytes = conflict."""
    env = _envelope("message.read", {"cursor": "c-1"})
    assert _admit(hub, env)["admitted"]
    env2 = dict(env, payload={"cursor": "c-2"})       # 같은 idem key, 다른 내용
    r = _admit(hub, env2)
    assert not r["admitted"]
    assert any("idempotency conflict" in p for p in r["problems"])


def test_f03b_같은_bytes_재전송은_최초_결과로_수렴(hub):
    """[B1] exact retry는 같은 authority event로 수렴 — seq 증가·덮어쓰기·재발행 0."""
    env = _envelope("message.read", {"cursor": "c-1"})
    first = _admit(hub, env)
    assert first["admitted"] and first["duplicate"] is False
    r = _admit(hub, env)                              # byte-identical 재전송
    assert r["admitted"] and r["duplicate"] is True
    assert r["event_id"] == first["event_id"]
    assert r["accepted_seq"] == first["accepted_seq"]  # seq 재증가 없음
    assert r["receipt"] == first["receipt"]            # 새 receipt 발행 없음
    # 이후 이벤트가 retry 때문에 seq를 건너뛰지 않는다
    nxt = _admit(hub, _envelope("message.read", {"cursor": "c-2"}))
    assert nxt["accepted_seq"] == first["accepted_seq"] + 1


# fixture 4(relay fork/tree-head)는 test_hub_log.py에서 — log 층의 것.


def test_f05_caller_time_escalation(hub):
    """[fixture 5] window basis를 wall-clock으로 — authority 승격 거부."""
    anchor, tc, canary, gate, rule = _chain(hub)
    p = _gate_payload(anchor["event_id"], anchor["accepted_seq"], canary["event_id"])
    p["window"] = {"basis": "wall_clock", "max_span": 100}
    r = _admit(hub, _envelope("run_set.launch_authorized", p))
    assert not r["admitted"] and r["stage"] == "schema"
    assert any("caller-time 승격 금지" in p_ for p_ in r["problems"])


def test_f06_unauthorized_revocation(hub):
    """[fixture 6] 다른 signer의 이벤트를 revoke — 권한 없는 revocation 거부."""
    frozen = _envelope("artifact.attested", {
        "artifact": {"role": "prereg", "schema_id": "s/v1", "sha256": H,
                     "byte_length": 10, "media_type": "application/json"},
        "bindings": [], "causal": {},
        "claim": {"type": "core:artifact.frozen", "scope": "organum",
                  "attests_ordering_of": "emission",
                  "evidence_basis": {"method": "raw-bytes-sha256",
                                     "verifier_schema": "v/1", "body_custody": "repo",
                                     "locator_authority": False}}},
        subject={"type": "artifact", "id": "artifact:prereg-v1"})
    ra = _admit(hub, frozen)
    assert ra["admitted"]
    # C2 이후 scope/namespace가 먼저 걸리므로, signer 검사에 도달하도록 scope를 맞춘다.
    revoke = _envelope("artifact.attested", {
        "artifact": {"role": "prereg", "schema_id": "s/v1", "sha256": H,
                     "byte_length": 10, "media_type": "application/json"},
        "bindings": [], "causal": {"revokes": ra["event_id"]},
        "claim": {"type": "core:artifact.frozen", "scope": "organum",
                  "attests_ordering_of": "emission",
                  "evidence_basis": {"method": "raw-bytes-sha256",
                                     "verifier_schema": "v/1", "body_custody": "repo",
                                     "locator_authority": False}}},
        signer="lab:ludex-cody",
        subject={"type": "artifact", "id": "artifact:prereg-v1"})
    r = _admit(hub, revoke, sec=SEC_B)
    assert not r["admitted"]
    assert any("unauthorized revocation" in p for p in r["problems"])


def test_f07_target_subject_conflation(hub):
    """[fixture 7] evidence kind에 target을 실어 수신 지정 — subject≠target 구조 차단."""
    env = _envelope("artifact.attested", {
        "artifact": {"role": "x", "schema_id": "s", "sha256": H, "byte_length": 1,
                     "media_type": "t"},
        "bindings": [], "causal": {},
        "target": {"lab_id": "lab:ludex-cody", "to_id": "cell-1", "to_epoch": 1},
        "claim": {"type": "core:artifact.frozen", "scope": "organum",
                  "attests_ordering_of": "emission",
                  "evidence_basis": {"method": "raw-bytes-sha256",
                                     "verifier_schema": "v/1", "body_custody": "repo",
                                     "locator_authority": False}}},
        subject={"type": "artifact", "id": "artifact:x"})
    r = _admit(hub, env)
    assert not r["admitted"] and r["stage"] == "schema"


def test_f08_creature에는_coordination도_금지():
    """[fixture 8] measured_creature에는 Hub-유래 payload가 **어느 평면이든** 금지."""
    msg = _envelope("message.posted", {
        "target": {"lab_id": "lab:organum-cody", "to_id": "cell-1", "to_epoch": 1},
        "body_locator": "repo://x", "body_sha256": H, "body_media_type": "text/markdown"})
    r = he.admit_to_role("measured_creature", msg)
    assert not r["admitted"]


def test_f09_canary_passed_mismatch(hub):
    """[fixture 9] 관측 사실과 rule 파생이 다른 passed — free caller boolean 거부."""
    rule_sha = hub.register_canary_rule(CANARY_RULE)
    policy_sha = hub.register_permission_policy(PERMISSION_POLICY)
    tc = _admit(hub, _envelope("toolchain.observed", _toolchain_payload(),
                               subject={"type": "machine", "id": "machine:m-01"}))
    p = _canary_payload(tc["event_id"], rule_sha, policy_sha, leak=True)   # leak인데
    p["passed"] = True                                          # passed 주장
    r = _admit(hub, _envelope("canary.result", p))
    assert not r["admitted"]
    assert any("passed mismatch" in q for q in r["problems"])


def test_f10_report_about_act_without_anchored_after(hub):
    """[fixture 10 · Ludex 반례 1] 과거-보고 claim이 선행 앵커 결속 없이."""
    env = _envelope("artifact.attested", {
        "artifact": {"role": "result", "schema_id": "s", "sha256": H, "byte_length": 1,
                     "media_type": "t"},
        "bindings": [], "causal": {},                 # anchored_after 없음
        "claim": {"type": "core:measurement.result", "scope": "organum",
                  "attests_ordering_of": "emission",
                  "evidence_basis": {"method": "raw-bytes-sha256",
                                     "verifier_schema": "v/1", "body_custody": "repo",
                                     "locator_authority": False}}},
        capture={"capture_artifact_sha256": H})
    r = _admit(hub, env)
    assert not r["admitted"]
    assert any("anchored_after 없음" in p for p in r["problems"])


def test_f11_install_observed_at_fabricated(hub):
    """[fixture 11 · Ludex 반례 2] capture 결속 없는 관측 시각 — 값 지어내기 거부.
    근거가 없으면 null이어야 하고, 값이 있으면 capture+method가 있어야 한다."""
    p = _toolchain_payload()
    p["install_observed_at"] = {"observed_at": "2026-08-01T00:00:00Z",
                                "capture_artifact_sha256": "없음",
                                "observation_method": "guess"}
    r = _admit(hub, _envelope("toolchain.observed", p,
                              subject={"type": "machine", "id": "machine:m-01"}))
    assert not r["admitted"] and r["stage"] == "schema"


def test_f12_provenance_without_capture(hub):
    """[fixture 12 · LxM 반례 A] capture-required claim의 provenance.capture=null."""
    _chain(hub)
    anchor_id = next(iter(hub._by_id))                # 아무 admitted event나 앵커로
    env = _envelope("artifact.attested", {
        "artifact": {"role": "result", "schema_id": "s", "sha256": H, "byte_length": 1,
                     "media_type": "t"},
        "bindings": [], "causal": {"anchored_after": anchor_id},
        "claim": {"type": "core:measurement.result", "scope": "organum",
                  "attests_ordering_of": "emission",
                  "evidence_basis": {"method": "raw-bytes-sha256",
                                     "verifier_schema": "v/1", "body_custody": "repo",
                                     "locator_authority": False}}},
        capture=None)                                 # 결속 없음
    r = _admit(hub, env)
    assert not r["admitted"]
    assert any("provenance.capture=null" in p for p in r["problems"])


def test_f13_hub_plane_소스가_피험체_allowlist에_유입():
    """[fixture 13 · LxM 반례 B] 피험체 프롬프트 조립 소스에 hub plane — 구조적 assert."""
    with pytest.raises(he.HubEnvelopeError, match="hub plane"):
        he.assert_source_allowlist(["task:mission", "plane:coordination"],
                                   allowlist=("task:mission", "obs:workdir"))
    with pytest.raises(he.HubEnvelopeError, match="allowlist 밖"):
        he.assert_source_allowlist(["task:mission", "web:random"],
                                   allowlist=("task:mission",))
    he.assert_source_allowlist(["task:mission"], allowlist=("task:mission",))


def test_f14_gate_없는_completed_와_비gate_ref(hub):
    """[fixture 14] 사전-발사 gate 없이 사후 보고만 — reject."""
    anchor, tc, canary, gate, _ = _chain(hub)
    r = _admit(hub, _envelope("run_set.completed", {
        "gate_event_id": canary["event_id"],          # gate가 아니라 canary를 ref
        "run_refs": ["run:cal-01"], "results_digest": H,
        "anchored_after": canary["event_id"],
        "attests_ordering_of": "execution_gated"}))
    assert not r["admitted"]
    assert any("run_set.launch_authorized가 아님" in p for p in r["problems"])


def test_f15a_canary_self_reference(hub):
    """[fixture 15] canary가 자기 자신을 toolchain 근거로 — cycle 거부."""
    rule_sha = hub.register_canary_rule(CANARY_RULE)
    policy_sha = hub.register_permission_policy(PERMISSION_POLICY)
    # 자기 event id는 내용에 의존하므로 고정점을 만들 수 없다 — 임의 미등록 id로
    # self-검사와 미등록-검사 둘 다 걸리는 경로를 확인.
    p = _canary_payload("f" * 64, rule_sha, policy_sha)
    r = _admit(hub, _envelope("canary.result", p))
    assert not r["admitted"]
    assert any("authority-projected event가 아님" in q for q in r["problems"])


def test_f15b_자유_문자열_semantics(hub):
    """[fixture 15] digest-pinned registry 참조가 아닌 자유 문자열 semantics."""
    policy_sha = hub.register_permission_policy(PERMISSION_POLICY)
    tc = _admit(hub, _envelope("toolchain.observed", _toolchain_payload(),
                               subject={"type": "machine", "id": "machine:m-01"}))
    p = _canary_payload(tc["event_id"], "b" * 64, policy_sha)     # 미등록 digest
    r = _admit(hub, _envelope("canary.result", p))
    assert not r["admitted"]
    assert any("registry artifact 미등록" in q for q in r["problems"])
    p2 = _canary_payload(tc["event_id"], "b" * 64, policy_sha)
    p2["canary_semantics"] = "v1-loose"               # 문자열 자체
    r2 = _admit(hub, _envelope("canary.result", p2))
    assert not r2["admitted"] and r2["stage"] == "schema"


def test_f16_creature에_evidence_plane_주입_불가(hub):
    """[fixture 16] measured_creature 컨텍스트에 evidence-plane 이벤트 admission 불가."""
    _, tc, _, _, _ = _chain(hub)
    ev = hub.get(tc["event_id"])["envelope"]
    r = he.admit_to_role("measured_creature", ev)
    assert not r["admitted"]
    # 대조: lab_operator도 evidence는 못 받는다. exact target의 message만 받는다.
    assert not he.admit_to_role("lab_operator", ev, target_cell="c", target_epoch=1)["admitted"]


def test_f17_게이트_산물이_prereg보다_이른_배터리(hub):
    """[fixture 17 · Ludex 반례 3] canary가 prereg 앵커보다 먼저 admitted된 bracketed gate."""
    rule_sha = hub.register_canary_rule(CANARY_RULE)
    policy_sha = hub.register_permission_policy(PERMISSION_POLICY)
    tc = _admit(hub, _envelope("toolchain.observed", _toolchain_payload(),
                               subject={"type": "machine", "id": "machine:m-01"}))
    canary = _admit(hub, _envelope("canary.result",
                                   _canary_payload(tc["event_id"], rule_sha, policy_sha)))
    anchor = _admit(hub, _envelope("hub.anchor", _anchor_payload(),
                                   subject={"type": "route", "id": "route:hub-log"}))
    # canary(seq 2) < anchor(seq 3) — 포위 조건 위반
    r = _admit(hub, _envelope("run_set.launch_authorized",
                              _gate_payload(anchor["event_id"], anchor["accepted_seq"],
                                            canary["event_id"])))
    assert not r["admitted"]
    assert any("prereg 앵커보다 이름" in p for p in r["problems"])
    # emission 라벨로는 같은 구조가 admitted — 라벨이 사실을 과장하지 않으면 통과.
    r2 = _admit(hub, _envelope("run_set.launch_authorized",
                               _gate_payload(anchor["event_id"], anchor["accepted_seq"],
                                             canary["event_id"], ordering="emission")))
    assert r2["admitted"]


# ═══ role/plane·기타 계약 ═══════════════════════════════════════════════════

def test_lab_operator는_exact_target만(hub):
    msg = _envelope("message.posted", {
        "target": {"lab_id": "lab:organum-cody", "to_id": "cell-1", "to_epoch": 2},
        "body_locator": "repo://x", "body_sha256": H, "body_media_type": "text/markdown"})
    ok = he.admit_to_role("lab_operator", msg, target_cell="cell-1", target_epoch=2)
    assert ok["admitted"]
    for cell, epoch in [("cell-1", 1), ("cell-2", 2)]:   # epoch/cell 불일치
        r = he.admit_to_role("lab_operator", msg, target_cell=cell, target_epoch=epoch)
        assert not r["admitted"]


def _unknown_claim_env():
    return _envelope("artifact.attested", {
        "artifact": {"role": "x", "schema_id": "s", "sha256": H, "byte_length": 1,
                     "media_type": "t"},
        "bindings": [], "causal": {},
        "claim": {"type": "ludex:unregistered.claim", "scope": "x",
                  "attests_ordering_of": "emission",
                  "evidence_basis": {"method": "raw-bytes-sha256",
                                     "verifier_schema": "v/1", "body_custody": "repo",
                                     "locator_authority": False}}},
        subject={"type": "artifact", "id": "artifact:x"})


def test_b2_unknown_claim_transport와_authority_분리(hub):
    """[B2] frozen v0.1 §4: unknown claim은 **보존·전달 가능**, authority projection에서만
    fail-closed 제외. transport admission과 projection이 독립으로 관측된다."""
    r = _admit(hub, _unknown_claim_env())
    assert r["admitted"] is True                       # transport: 보존·전달 가능
    assert r["authority_projected"] is False           # authority: 투영 안 됨
    assert r["authority_reason"] == "unknown_claim"
    assert hub.get(r["event_id"]) is not None          # transport index에 있음
    assert hub.authority(r["event_id"]) is None        # authority 조회는 None


def test_b2_unknown_claim은_authority_근거로_쓸_수_없다(hub):
    """미투영 이벤트를 anchored_after 근거로 참조하면 거부 — 전달 가능≠근거 가능."""
    ru = _admit(hub, _unknown_claim_env())
    assert ru["admitted"] and not ru["authority_projected"]
    env = _envelope("artifact.attested", {
        "artifact": {"role": "result", "schema_id": "s", "sha256": H, "byte_length": 1,
                     "media_type": "t"},
        "bindings": [], "causal": {"anchored_after": ru["event_id"]},
        "claim": {"type": "core:measurement.result", "scope": "organum",
                  "attests_ordering_of": "emission",
                  "evidence_basis": {"method": "raw-bytes-sha256",
                                     "verifier_schema": "v/1", "body_custody": "repo",
                                     "locator_authority": False}}},
        capture={"capture_artifact_sha256": H})
    r = _admit(hub, env)
    assert not r["admitted"]
    assert any("authority-projected event를 가리키지 않음" in p for p in r["problems"])


def test_claim_registry_digest_불일치는_생성_거부():
    with pytest.raises(he.HubEnvelopeError, match="digest 불일치"):
        he.ClaimRegistry(CLAIMS_DOC, expected_sha256="0" * 64)


def test_idempotency_fingerprint는_exact_bytes(hub):
    """[idem round trip] fingerprint = 서명된 exact bytes의 SHA-256 — created_at만
    바꿔도 다른 bytes = 같은 idem key면 conflict."""
    env = _envelope("message.read", {"cursor": "c"})
    raw = he.canonical_bytes(env)
    assert he.idempotency_fingerprint(raw) == hashlib.sha256(raw).hexdigest()
    assert _admit(hub, env)["admitted"]
    env2 = dict(env, created_at="2026-08-15T00:00:01Z")   # 내용 동일·시각만 변경
    r = _admit(hub, env2)
    assert not r["admitted"]
    assert any("idempotency conflict" in p for p in r["problems"])


def test_평문_본문_탑재는_스키마가_차단(hub):
    p = {"target": {"lab_id": "lab:organum-cody", "to_id": "c", "to_epoch": 1},
         "body_locator": "repo://x", "body_sha256": H, "body_media_type": "t",
         "body": "비밀 본문"}
    r = _admit(hub, _envelope("message.posted", p))
    assert not r["admitted"] and r["stage"] == "schema"


def test_revoked_key는_정책에서_거부(hub):
    hub.keys.revoke(PUB_A)
    r = _admit(hub, _envelope("message.read", {"cursor": "c"}))
    assert not r["admitted"]
    assert any("revoked key" in p for p in r["problems"])


# ═══ Orin final critic B1–B5 (2026-08-15) ═══════════════════════════════════

def test_b1_retry_수렴_end_to_end(hub):
    """[B1] exact retry: event_id·seq·receipt 동일 + tree_size 불변 + duplicate 표기."""
    env = _envelope("message.read", {"cursor": "c-1"})
    first = _admit(hub, env)
    size_after_first = hub._log.tree_size
    again = _admit(hub, env)
    assert again["duplicate"] is True
    assert (again["event_id"], again["accepted_seq"]) == \
        (first["event_id"], first["accepted_seq"])
    assert again["receipt"] == first["receipt"]
    assert hub._log.tree_size == size_after_first      # log append 없음


def test_b3_타입_혼동_배터리는_crash가_아니라_quarantine(hub):
    """[B3] canonical-valid하지만 타입이 어긋난 봉투 — 예외 이탈 0, 전부 schema 격리.
    Orin 재현: subject.type=[] → TypeError(unhashable). 대표 반례를 공통 필드와
    nested object 전반에 배치한다."""
    base = _envelope("message.read", {"cursor": "c"})
    mutations = [
        {"subject": {"type": [], "id": "run:x"}},          # Orin 재현 그대로
        {"subject": {"type": None, "id": "run:x"}},
        {"subject": "run:x"},                              # dict여야 할 곳에 str
        {"signer": None},
        {"signer": {"id": ["lab:x"], "key_id": "k1", "key_epoch": 1}},
        {"signer": {"id": "lab:organum-cody", "key_id": "k1", "key_epoch": "1"}},
        {"payload": []},                                   # dict여야 할 곳에 list
        {"payload": 7},
        {"provenance": {"lab": "x", "machine": "m", "platform": "p", "adapter": "a",
                        "cli_version": "v", "capture": 3}},   # capture가 int
        {"idempotency_key": None},
        {"created_at": 1723680000},
    ]
    for i, mut in enumerate(mutations):
        env = {**_envelope("message.read", {"cursor": f"c-{i}"}), **mut}
        try:
            raw = he.canonical_bytes(env)
        except he.HubEnvelopeError:
            continue                                       # canonical조차 못 되면 그 층 소관
        r = _admit(hub, env, raw=raw)
        assert not r["admitted"], f"타입 혼동이 통과: {mut}"
        assert r["stage"] == "schema", f"{mut} → {r['stage']}: {r['problems']}"


def test_b3_gate_window_null도_quarantine(hub):
    anchor, tc, canary, gate, _ = _chain(hub)
    p = _gate_payload(anchor["event_id"], anchor["accepted_seq"], canary["event_id"])
    p["window"] = None
    r = _admit(hub, _envelope("run_set.launch_authorized", p))
    assert not r["admitted"] and r["stage"] == "schema"


def test_b4_admit_append_receipt_treehead_한_흐름(hub):
    """[B4·seam④] 최초 admission → exact-once append → signed receipt가 한 원자적 결과.
    receipt의 seq/tree_size/root/fingerprint를 log와 교차 검증하고 inclusion proof까지."""
    env = _envelope("message.read", {"cursor": "c"})
    raw = he.canonical_bytes(env)
    r = _admit(hub, env)
    rc = r["receipt"]
    assert he.verify_relay_receipt(rc, PUB_HUB)
    body = rc["body"]
    assert body["event_id"] == body["content_sha256"] == r["event_id"]
    assert body["idempotency_fingerprint"] == he.idempotency_fingerprint(raw)
    assert body["accepted_seq"] == r["accepted_seq"] == hub._log.tree_size  # seq=index+1
    assert body["tree_size"] == hub._log.tree_size
    assert body["root"] == hub._log.root().hex()
    proof = hub._log.inclusion_proof(body["accepted_seq"] - 1, body["tree_size"])
    assert hl.verify_inclusion(raw, body["accepted_seq"] - 1, body["tree_size"],
                               proof, bytes.fromhex(body["root"]))
    # 변조된 receipt는 검증 실패
    forged = {"body": dict(body, accepted_seq=99), "signature": rc["signature"]}
    assert not he.verify_relay_receipt(forged, PUB_HUB)


def test_b4_reject와_quarantine은_tree를_바꾸지_않는다(hub):
    _admit(hub, _envelope("message.read", {"cursor": "c"}))
    size = hub._log.tree_size
    _admit(hub, _envelope("message.read", {"cursor": "c2", "extra": 1}))   # schema 거부
    bad = _envelope("message.read", {"cursor": "c3"})
    raw = he.canonical_bytes(bad)
    hub.admit(raw, "0" * 128, PUB_A)                                       # 서명 거부
    _admit(hub, _envelope("message.read", {"cursor": "c4"}), sec=SEC_B)    # binding 거부
    assert hub._log.tree_size == size


def test_b4_log가_admission_밖에서_전진하면_결속_위반(hub):
    """조립 보증: log는 admit 경로만 전진시킨다 — 외부 append 오염을 다음 admit이 잡는다."""
    _admit(hub, _envelope("message.read", {"cursor": "c"}))
    hub._log.append(b"not-an-admitted-envelope")          # 외부 오염
    with pytest.raises(he.HubEnvelopeError, match="결속 위반"):
        _admit(hub, _envelope("message.read", {"cursor": "c2"}))


def test_b4_receipt는_log_없이_발행_불가():
    keys = he.KeyRegistry()
    claims = he.ClaimRegistry(CLAIMS_DOC, expected_sha256=he.canonical_sha(CLAIMS_DOC))
    with pytest.raises(he.HubEnvelopeError, match="log 결속 없이"):
        he.HubIndex(key_registry=keys, claim_registry=claims, receipt_seckey=SEC_HUB)


def test_b5_registry_미지원_revocation_enum은_생성_거부():
    """[B5] "allow-anything" 같은 미지원 값이 무권한 통과로 새지 않는다 — ctor 거부."""
    doc = {"claims": {"core:x": {
        "act_class": "self_attesting", "subject_types": ["artifact"],
        "ordering_levels": ["emission"], "capture_required": False,
        "revocation_authority": "allow-anything"}}}
    with pytest.raises(he.HubEnvelopeError, match="revocation_authority 미지원"):
        he.ClaimRegistry(doc, expected_sha256=he.canonical_sha(doc))


@pytest.mark.parametrize("mut", [
    {"ordering_levels": ["emission", "yolo"]},
    {"ordering_levels": []},
    {"subject_types": ["creature", "everything"]},
    {"capture_required": "yes"},
])
def test_b5_registry_spec_enum_밖은_생성_거부(mut):
    doc = {"claims": {"core:x": {**{
        "act_class": "self_attesting", "subject_types": ["artifact"],
        "ordering_levels": ["emission"], "capture_required": False,
        "revocation_authority": "same_signer"}, **mut}}}
    with pytest.raises(he.HubEnvelopeError):
        he.ClaimRegistry(doc, expected_sha256=he.canonical_sha(doc))


# ═══ LxM 반례 C (2026-08-15) — 버전 결속: 형식이 아니라 주장의 주소와 내적 정합 ═══

def _versioned(cursor, value, digest):
    env = _envelope("message.read", {"cursor": cursor})
    env["provenance"]["cli_version"] = {"value": value, "capture_artifact_sha256": digest}
    return env


def test_c_버전은_주소_있는_주장이어야_한다(hub):
    """[반례 C 제안 1] 자유 문자열 cli_version은 schema 거부 — 어느 capture에서 왔는지
    말하지 않는 버전 주장은 성실한 파생과 지어낸 값을 구별 불가능하게 만든다."""
    env = _envelope("message.read", {"cursor": "c"})
    env["provenance"]["cli_version"] = "1.2.3"                  # 자유 문자열
    r = _admit(hub, env)
    assert not r["admitted"] and r["stage"] == "schema"
    env2 = _envelope("message.read", {"cursor": "c2"})
    env2["provenance"]["cli_version"] = {"value": "1.2.3"}      # 주소 없음
    r2 = _admit(hub, env2)
    assert not r2["admitted"] and r2["stage"] == "schema"
    assert _admit(hub, _versioned("c3", "1.2.3", "d" * 64))["admitted"]   # 완전 구조
    assert _admit(hub, _envelope("message.read", {"cursor": "c4"}))["admitted"]  # null 정직


def test_c_같은_capture가_다른_버전을_낳으면_conflict(hub):
    """[반례 C 제안 2·재현 ②] 같은 capture digest에 다른 버전 — 내적 모순 거부."""
    tc1 = _admit(hub, _envelope("toolchain.observed", _toolchain_payload(),
                                subject={"type": "machine", "id": "machine:m-01"}))
    assert tc1["admitted"]
    p2 = _toolchain_payload()
    p2["backend_version"] = "2.99.0"                            # 같은 capture(H), 다른 버전
    r = _admit(hub, _envelope("toolchain.observed", p2,
                              subject={"type": "machine", "id": "machine:m-02"}))
    assert not r["admitted"]
    assert any("capture 모순" in q for q in r["problems"])


def test_c_같은_capture_같은_버전은_통과(hub):
    _admit(hub, _envelope("toolchain.observed", _toolchain_payload(),
                          subject={"type": "machine", "id": "machine:m-01"}))
    r = _admit(hub, _envelope("toolchain.observed", _toolchain_payload(),
                              subject={"type": "machine", "id": "machine:m-02"}))
    assert r["admitted"]


def test_c_cli_version과_backend_version_교차_모순도_잡는다(hub):
    """버전 주장 맵은 필드를 가리지 않는다 — 같은 artifact가 어디서 인용되든 한 값."""
    _admit(hub, _envelope("toolchain.observed", _toolchain_payload(),
                          subject={"type": "machine", "id": "machine:m-01"}))  # H→1.18.3
    r = _admit(hub, _versioned("c", "9.9.9", H))                # 같은 H에 다른 값
    assert not r["admitted"]
    assert any("capture 모순" in q for q in r["problems"])
    assert _admit(hub, _versioned("c2", "1.18.3", H))["admitted"]   # 일치는 통과


def test_c_한_봉투_안의_자기모순도_잡는다(hub):
    env = _envelope("toolchain.observed", _toolchain_payload(),
                    subject={"type": "machine", "id": "machine:m-01"})
    env["provenance"]["cli_version"] = {"value": "다른값", "capture_artifact_sha256": H}
    r = _admit(hub, env)                                        # H가 backend 1.18.3과 충돌
    assert not r["admitted"]
    assert any("capture 모순" in q for q in r["problems"])


def test_c_install_observed_at도_같은_규율(hub):
    p1 = _toolchain_payload()
    p1["install_observed_at"] = {"observed_at": "2026-08-01T00:00:00Z",
                                 "capture_artifact_sha256": "e" * 64,
                                 "observation_method": "stat"}
    assert _admit(hub, _envelope("toolchain.observed", p1,
                                 subject={"type": "machine", "id": "machine:m-01"}))["admitted"]
    p2 = _toolchain_payload()
    p2["install_observed_at"] = {"observed_at": "2026-08-09T09:09:09Z",   # 같은 capture, 다른 시각
                                 "capture_artifact_sha256": "e" * 64,
                                 "observation_method": "stat"}
    r = _admit(hub, _envelope("toolchain.observed", p2,
                              subject={"type": "machine", "id": "machine:m-02"}))
    assert not r["admitted"]
    assert any("capture 모순" in q for q in r["problems"])


def test_c_격리된_이벤트는_주장을_세우지_못한다(hub):
    """quarantine된 봉투의 버전 주장이 맵에 남으면 뒤의 정직한 이벤트가 억울하게 죽는다."""
    bad = _versioned("c", "9.9.9", "b" * 64)
    bad["subject"] = {"type": "run", "id": "artifact:wrong-prefix"}   # schema 거부 유도
    assert not _admit(hub, bad)["admitted"]
    assert _admit(hub, _versioned("c2", "1.0.0", "b" * 64))["admitted"]   # 모순 아님


def test_c_정직_경계_지어낸_버전_완전_결속은_봉투가_못_막는다(hub):
    """[반례 C 재현 ①] 재도출 재료(artifact bytes)가 봉투 밖 — 이 admitted가 그 경계의
    pin이다. 값↔bytes 대조는 capture artifact resolver(P1, §UNPROVEN)의 몫."""
    p = _toolchain_payload()
    p["backend_version"] = "999.999.999-지어낸값"
    r = _admit(hub, _envelope("toolchain.observed", p, capture=None,
                              subject={"type": "machine", "id": "machine:m-01"}))
    assert r["admitted"]                       # 봉투 층의 정직한 한계 — 문서에 명시


def test_f13b_hub_plane은_allowlist에_있어도_거부():
    """[LxM 우선순위 잠금] plane 검사가 멤버십 검사보다 먼저 — allowlist 설정 오류로
    plane:*가 들어가 있어도 그 경로로는 못 들어온다."""
    with pytest.raises(he.HubEnvelopeError, match="hub plane"):
        he.assert_source_allowlist(
            ["plane:coordination"],
            allowlist=("plane:coordination", "task:mission"))   # 잘못 들어간 allowlist


# ═══ Orin r3 재판정 C1·C2 (2026-08-15) ══════════════════════════════════════

def _frozen_claim(*, ctype="core:artifact.frozen", scope="organum", revokes=None,
                  signer="lab:organum-cody", sid="artifact:prereg-v1"):
    causal = {} if revokes is None else {"revokes": revokes}
    return _envelope("artifact.attested", {
        "artifact": {"role": "prereg", "schema_id": "s/v1", "sha256": H,
                     "byte_length": 10, "media_type": "application/json"},
        "bindings": [], "causal": causal,
        "claim": {"type": ctype, "scope": scope,
                  "attests_ordering_of": "emission",
                  "evidence_basis": {"method": "raw-bytes-sha256",
                                     "verifier_schema": "v/1", "body_custody": "repo",
                                     "locator_authority": False}}},
        signer=signer, subject={"type": "artifact", "id": sid})


def test_c1_오염_상태에서는_candidate가_tree에_들어가지_않는다(hub):
    """[Orin C1] 종전에는 오염을 탐지하되 **append 뒤**라 미수용 bytes가 tree를 키웠고
    재시도마다 자랐다. 이제 검사가 append 앞이다 — candidate 미수용·tree 불변."""
    _admit(hub, _envelope("message.read", {"cursor": "c"}))
    hub._log.append(b"contamination")                 # 외부 오염 (tree_size 2)
    size = hub._log.tree_size
    cand = _envelope("message.read", {"cursor": "c2"})
    for _ in range(3):                                # 반복 시도에도 tree 불변
        with pytest.raises(he.HubEnvelopeError, match="candidate 미수용"):
            _admit(hub, cand)
        assert hub._log.tree_size == size
    assert hub.get(he.event_id_of(he.canonical_bytes(cand))) is None


def test_c2_cross_scope_revocation_거부(hub):
    ra = _admit(hub, _frozen_claim(scope="scope-a"))
    assert ra["admitted"]
    r = _admit(hub, _frozen_claim(scope="scope-b", revokes=ra["event_id"]))
    assert not r["admitted"]
    assert any("scope 불일치" in p for p in r["problems"])


def test_c2_비claim_target_revocation_거부(hub):
    """[Orin C2 재현 2] coordination event에는 claim namespace/scope 자체가 없다."""
    rm = _admit(hub, _envelope("message.read", {"cursor": "c"}))
    assert rm["admitted"]
    r = _admit(hub, _frozen_claim(revokes=rm["event_id"]))
    assert not r["admitted"]
    assert any("claim event(artifact.attested)가 아님" in p for p in r["problems"])


def test_c2_cross_namespace_revocation_거부(hub):
    ra = _admit(hub, _frozen_claim(ctype="core:artifact.frozen"))
    r = _admit(hub, _frozen_claim(ctype="organum:note.frozen",
                                  revokes=ra["event_id"], sid="artifact:note-1"))
    assert not r["admitted"]
    assert any("cross-namespace 금지" in p for p in r["problems"])


def test_c2_같은_namespace_scope_signer는_통과(hub):
    ra = _admit(hub, _frozen_claim())
    r = _admit(hub, _frozen_claim(revokes=ra["event_id"]))
    assert r["admitted"], r["problems"]


# ═══ LxM 반례 D (2026-08-15) — 맵에 권위를 세우는 값의 검증 ══════════════════

@pytest.mark.parametrize("field,value", [
    ("backend_version", None), ("backend_version", 12345), ("backend_version", ""),
    ("binary_digest", "not-a-hash"), ("provider_route", 12345), ("backend", None),
    ("observation_method", ""),
])
def test_d_payload_스칼라_미검증_전부_schema_거부(hub, field, value):
    """[반례 D 제안 1] 맵에 주장을 세우는 필드는 봉투 층과 같은 규율."""
    p = _toolchain_payload()
    p[field] = value
    r = _admit(hub, _envelope("toolchain.observed", p,
                              subject={"type": "machine", "id": "machine:m-01"}))
    assert not r["admitted"] and r["stage"] == "schema", (field, value)


def test_d_빈_값이_정직한_기록을_봉쇄하는_커플링_차단(hub):
    """[반례 D 핵심 재현] 종전: backend_version="" admitted → 맵에 D→'' → 정직한
    "1.18.3"이 capture 모순으로 영구 거부. 이제 ①이 schema에서 죽어 맵이 오염되지
    않고 정직한 기록이 선다."""
    p_bad = _toolchain_payload()
    p_bad["backend_version"] = ""
    r_bad = _admit(hub, _envelope("toolchain.observed", p_bad,
                                  subject={"type": "machine", "id": "machine:m-01"}))
    assert not r_bad["admitted"]                       # ① 봉쇄원이 먼저 죽는다
    r_ok = _admit(hub, _envelope("toolchain.observed", _toolchain_payload(),
                                 subject={"type": "machine", "id": "machine:m-02"}))
    assert r_ok["admitted"]                            # ② 정직한 기록이 선다


# ═══ LxM 반례 D-2 (2026-08-15) — 형제 맵(_capture_install_times)의 같은 구멍 ═══

@pytest.mark.parametrize("observed", [None, 12345, "", "아무말"])
def test_d2_observed_at_미검증_값_전부_schema_거부(hub, observed):
    """[반례 D-2] observed_at은 맵에 오르는 값인데 유일하게 미검증이었다 —
    created_at 선례(서술값이어도 모양은 지킨다) 그대로 RFC3339-Z 강제."""
    p = _toolchain_payload()
    p["install_observed_at"] = {"observed_at": observed,
                                "capture_artifact_sha256": "e" * 64,
                                "observation_method": "stat"}
    r = _admit(hub, _envelope("toolchain.observed", p,
                              subject={"type": "machine", "id": "machine:m-01"}))
    assert not r["admitted"] and r["stage"] == "schema", observed


def test_d2_빈_시각이_정직한_기록을_봉쇄하는_커플링_차단(hub):
    """[반례 D-2 커플링] 종전: observed_at="" admitted → 맵에 D→'' → 정직한 시각이
    capture 모순으로 영구 거부. 이제 봉쇄원이 schema에서 죽는다."""
    p_bad = _toolchain_payload()
    p_bad["install_observed_at"] = {"observed_at": "",
                                    "capture_artifact_sha256": "e" * 64,
                                    "observation_method": "stat"}
    assert not _admit(hub, _envelope("toolchain.observed", p_bad,
                                     subject={"type": "machine", "id": "machine:m-01"}))["admitted"]
    p_ok = _toolchain_payload()
    p_ok["install_observed_at"] = {"observed_at": "2026-08-01T00:00:00Z",
                                   "capture_artifact_sha256": "e" * 64,
                                   "observation_method": "stat"}
    assert _admit(hub, _envelope("toolchain.observed", p_ok,
                                 subject={"type": "machine", "id": "machine:m-02"}))["admitted"]


def test_d2_등록_지점_불변식이_스키마와_독립으로_선다():
    """[반례 D-2 처방] "필드를 쫓지 말고 불변식으로" — collector가 값을 내놓는 지점에서
    자격을 강제한다. 스키마를 우회한 값도 여기서 예외로 죽는다(조용한 등록 없음).
    새 맵을 추가하며 검증을 잊으면 이 층이 구조로 막는다."""
    with pytest.raises(he.HubEnvelopeError, match="등록 자격 위반"):
        he.HubIndex._eligible("e" * 64, "", value_kind="version")
    with pytest.raises(he.HubEnvelopeError, match="등록 자격 위반"):
        he.HubIndex._eligible("e" * 64, None, value_kind="version")
    with pytest.raises(he.HubEnvelopeError, match="등록 자격 위반"):
        he.HubIndex._eligible("e" * 64, "아무말", value_kind="time")
    with pytest.raises(he.HubEnvelopeError, match="등록 자격 위반"):
        he.HubIndex._eligible("not-hex", "1.0.0", value_kind="version")
    with pytest.raises(he.HubEnvelopeError, match="value_kind"):
        he.HubIndex._eligible("e" * 64, "1.0.0", value_kind="새맵-검증-잊음")
    # 정상 값은 그대로 통과
    assert he.HubIndex._eligible("e" * 64, "1.0.0", value_kind="version") == ("e" * 64, "1.0.0")
    assert he.HubIndex._eligible("e" * 64, "2026-08-01T00:00:00Z", value_kind="time")[1] \
        == "2026-08-01T00:00:00Z"


def test_d2_스키마를_우회한_값은_정책_단계에서_fail_closed(hub):
    """collector 불변식이 실전 경로에서 도는지 — 스키마가 놓쳐도(가정) 정책 단계
    quarantine으로 닫힌다. 직접 collector를 쳐서 확인한다."""
    env = _envelope("message.read", {"cursor": "c"})
    env["provenance"]["cli_version"] = {"value": "", "capture_artifact_sha256": "e" * 64}
    # cli_version.value=""는 스키마도 잡지만, collector 단독으로도 죽어야 한다:
    with pytest.raises(he.HubEnvelopeError, match="등록 자격 위반"):
        he.HubIndex._version_claims_of(env)


# ═══ Orin r5 delta HOLD (2026-08-15) — RFC3339 suffix bypass ════════════════

def _io_payload(observed):
    p = _toolchain_payload()
    p["install_observed_at"] = {"observed_at": observed,
                                "capture_artifact_sha256": "e" * 64,
                                "observation_method": "stat"}
    return p


@pytest.mark.parametrize("good", ["2026-08-15T00:00:00Z", "2026-08-15T23:59:59.123456Z"])
def test_rfc_유효_시각은_admitted(hub, good):
    r = _admit(hub, _envelope("toolchain.observed", _io_payload(good),
                              subject={"type": "machine", "id": "machine:m-01"}))
    assert r["admitted"], (good, r["problems"])


@pytest.mark.parametrize("bad", [
    "Z", "아무말Z", "2026-08-15Z",                      # Orin 재현 그대로
    "2026-99-99T99:99:99Z",                             # 문법 통과 불가 달력
    "2026-02-30T00:00:00Z",                             # 존재하지 않는 날
    "2026-08-15T24:00:00Z",                             # 존재하지 않는 시각
    "2026-08-15T00:00:00+00:00",                        # offset 표기(non-Z)
    "2026-08-15T00:00:00",                              # Z 부재
])
def test_rfc_suffix_bypass_전부_schema_거부(hub, bad):
    """[Orin r5 HOLD] 종전 predicate가 endswith("Z") 하나라 "아무말Z"가 통과해 맵을
    선점했다 — 이름(RFC3339-Z)과 구현이 갈라져 있었다. 이제 문법+달력까지 본다."""
    r = _admit(hub, _envelope("toolchain.observed", _io_payload(bad),
                              subject={"type": "machine", "id": "machine:m-01"}))
    assert not r["admitted"] and r["stage"] == "schema", bad


def test_rfc_invalid_Z_선점_뒤_정상_시각이_선다(hub):
    """[Orin 재현] "아무말Z" 선점 시도 → 거부 → 같은 capture의 정상 시각 admitted,
    tree/map 비오염."""
    r_bad = _admit(hub, _envelope("toolchain.observed", _io_payload("아무말Z"),
                                  subject={"type": "machine", "id": "machine:m-01"}))
    assert not r_bad["admitted"]
    size = hub._log.tree_size
    r_ok = _admit(hub, _envelope("toolchain.observed",
                                 _io_payload("2026-08-15T00:00:00Z"),
                                 subject={"type": "machine", "id": "machine:m-02"}))
    assert r_ok["admitted"]
    assert hub._log.tree_size == size + 1               # 거부는 tree에 흔적 없음


def test_rfc_eligible_불변식도_같은_predicate(hub):
    for bad in ("아무말Z", "2026-99-99T99:99:99Z", "2026-08-15Z"):
        with pytest.raises(he.HubEnvelopeError, match="등록 자격 위반"):
            he.HubIndex._eligible("e" * 64, bad, value_kind="time")
    assert he.HubIndex._eligible("e" * 64, "2026-08-15T00:00:00Z", value_kind="time")


def test_rfc_created_at도_같은_helper(hub):
    """Orin: created_at도 같은 suffix-only 선례였다 — 같은 helper로 교체해 규칙의
    이름과 구현을 일치시킨다."""
    env = _envelope("message.read", {"cursor": "c"})
    env["created_at"] = "아무말Z"
    r = _admit(hub, env)
    assert not r["admitted"] and r["stage"] == "schema"
    assert any("created_at" in p for p in r["problems"])


def test_b5_미등록_permission_policy_digest는_canary_거부(hub):
    """[B5] Δ3 "digest-pinned registry artifact"는 64자리 모양이 아니라 실제 등록·해석."""
    rule_sha = hub.register_canary_rule(CANARY_RULE)
    tc = _admit(hub, _envelope("toolchain.observed", _toolchain_payload(),
                               subject={"type": "machine", "id": "machine:m-01"}))
    p = _canary_payload(tc["event_id"], rule_sha, "c" * 64)   # 모양은 hex64, 등록 안 됨
    r = _admit(hub, _envelope("canary.result", p))
    assert not r["admitted"]
    assert any("permission policy registry artifact 미등록" in q for q in r["problems"])


# ═══ key lifecycle (§2 "그때 유효했나" — UNPROVEN → 구현, 2026-08-16) ═══════

SEC_NEW = (23).to_bytes(32, "big")
PUB_NEW = sp.public_key(SEC_NEW).hex()


def _rotate_env(new_pub, *, key_id="k2", epoch=2, idem=None):
    e = _envelope("key.rotated", {"new_key_id": key_id, "new_key_epoch": epoch,
                                  "new_pubkey": new_pub},
                  subject={"type": "machine", "id": "machine:m-01"})
    if idem:
        e["idempotency_key"] = idem
    return e


def _revoke_env(key_id, epoch, *, idem=None):
    e = _envelope("key.revoked", {"key_id": key_id, "key_epoch": epoch,
                                  "reason": "rotation 완료"},
                  subject={"type": "machine", "id": "machine:m-01"})
    if idem:
        e["idempotency_key"] = idem
    return e


def test_kl_rotation으로_새_키가_서고_그_seq부터_유효(hub):
    r = _admit(hub, _rotate_env(PUB_NEW))
    assert r["admitted"], r["problems"]
    seq = r["accepted_seq"]
    # 새 키로 새 봉투 admit — key_id/epoch를 새 결속으로
    env = _envelope("message.read", {"cursor": "c-new"})
    env["signer"] = {"id": "lab:organum-cody", "key_id": "k2", "key_epoch": 2}
    r2 = _admit(hub, env, sec=SEC_NEW)
    assert r2["admitted"], r2["problems"]
    # "그때 유효했나"(C2: 효력은 n+1부터): rotation 이벤트 좌표에선 아직 무효 —
    # 그 이벤트는 옛 키가 서명했으니까. 다음 좌표부터 유효.
    assert hub.keys.was_valid(PUB_NEW, seq) is False
    assert hub.keys.was_valid(PUB_NEW, seq + 1) is True
    assert hub.keys.was_valid("f" * 64, 1) is None      # 미등록 = None(불가≠거짓)


def test_kl_revoke_후_새_admission은_거부_과거는_유효(hub):
    r0 = _admit(hub, _envelope("message.read", {"cursor": "c-before"}))
    assert r0["admitted"]
    rv = _admit(hub, _revoke_env("k1", 1))              # 자기 현재 키를 자기가 내림
    assert rv["admitted"], rv["problems"]
    seq_rv = rv["accepted_seq"]
    # 새 admission은 거부
    r1 = _admit(hub, _envelope("message.read", {"cursor": "c-after"}))
    assert not r1["admitted"]
    assert any("revoked" in p_ for p_ in r1["problems"])
    # 과거 좌표에서는 여전히 유효 — 소급 무효화 아님. 그리고 C2: revocation 이벤트를
    # 서명한 그 키는 **자기 이벤트 좌표에서 유효**하다(효력은 n+1부터).
    assert hub.keys.was_valid(PUB_A, r0["accepted_seq"]) is True
    assert hub.keys.was_valid(PUB_A, seq_rv) is True
    assert hub.keys.was_valid(PUB_A, seq_rv + 1) is False


def test_kl_rotate_then_revoke_old_전형_시나리오(hub):
    assert _admit(hub, _rotate_env(PUB_NEW))["admitted"]
    rv = _admit(hub, _revoke_env("k1", 1))              # 옛 키를 내리고
    assert rv["admitted"]
    env = _envelope("message.read", {"cursor": "c"})     # 새 키로 계속
    env["signer"] = {"id": "lab:organum-cody", "key_id": "k2", "key_epoch": 2}
    assert _admit(hub, env, sec=SEC_NEW)["admitted"]
    hist = hub.keys.bindings_of("lab:organum-cody")
    assert len(hist) == 2                                # 이력이 남는다 — 덮어쓰기 아님


def test_kl_자기_결속에_없는_키는_revoke_불가(hub):
    """key 이름공간은 signer별이다 — revoke 대상은 언제나 자기 (key_id, epoch)로만
    지목되고, 자기 결속에 없는 지목은 거부된다(남의 키를 건드릴 표현 자체가 없다)."""
    rv = _revoke_env("k9", 9)
    rv["signer"] = {"id": "lab:ludex-cody", "key_id": "k1", "key_epoch": 1}
    r = _admit(hub, rv, sec=SEC_B)
    assert not r["admitted"]
    assert any("이 signer의 결속이 아님" in p_ for p_ in r["problems"])


def test_kl_pubkey_재사용_금지(hub):
    r = _admit(hub, _rotate_env(PUB_B))                  # 이미 ludex에 결속된 pubkey
    assert not r["admitted"]
    assert any("이미 결속" in p_ for p_ in r["problems"])


def test_kl_같은_signer_같은_key_id_epoch_중복_금지(hub):
    r = _admit(hub, _rotate_env(PUB_NEW, key_id="k1", epoch=1))
    assert not r["admitted"]
    assert any("이미 존재" in p_ for p_ in r["problems"])


def test_kl_이중_revoke_거부(hub):
    assert _admit(hub, _rotate_env(PUB_NEW))["admitted"]  # 새 키 확보(잠기지 않게)
    assert _admit(hub, _revoke_env("k1", 1))["admitted"]
    env = _revoke_env("k1", 1, idem="kl-again")
    env["signer"] = {"id": "lab:organum-cody", "key_id": "k2", "key_epoch": 2}
    r = _admit(hub, env, sec=SEC_NEW)
    assert not r["admitted"]
    assert any("이미 revoked" in p_ for p_ in r["problems"])


@pytest.mark.parametrize("payload", [
    {"new_key_id": "k2", "new_key_epoch": 0, "new_pubkey": "a" * 64},   # epoch<1
    {"new_key_id": "k2", "new_key_epoch": 2, "new_pubkey": "짧음"},      # hex 아님
    {"new_key_id": "", "new_key_epoch": 2, "new_pubkey": "a" * 64},
])
def test_kl_rotated_shape_거부(hub, payload):
    e = _envelope("key.rotated", payload,
                  subject={"type": "machine", "id": "machine:m-01"})
    r = _admit(hub, e)
    assert not r["admitted"] and r["stage"] == "schema"


# ═══ Orin 0.3.0 재판정 C1·C2·C3 + LxM 술어 단일화 (2026-08-16) ═══════════════

def test_c2_불변식_모든_admitted_record는_자기_좌표에서_유효(hub):
    """[Orin C2 불변식] rotation·revocation을 겪은 이력 전체에서, 모든 admitted
    record의 서명 키는 자기 accepted_seq 좌표에서 was_valid=True."""
    _admit(hub, _envelope("message.read", {"cursor": "c0"}))
    _admit(hub, _rotate_env(PUB_NEW))
    rv = _revoke_env("k1", 1)
    _admit(hub, rv)
    env = _envelope("message.read", {"cursor": "c1"})
    env["signer"] = {"id": "lab:organum-cody", "key_id": "k2", "key_epoch": 2}
    _admit(hub, env, sec=SEC_NEW)
    for rec in hub._by_id.values():
        assert hub.keys.was_valid(rec["pubkey"], rec["accepted_seq"]) is True, \
            (rec["envelope"]["event_kind"], rec["accepted_seq"])


def test_c3_revoke_후_exact_retry는_최초로_수렴(hub):
    """[Orin C3] 이미 수용된 exact bytes의 재전송은 키가 나중에 폐기됐어도 최초
    결과로 수렴한다 — 과거 영수증 반환이지 신규 권한 부여가 아니다. 상태 무변."""
    env = _envelope("message.read", {"cursor": "c-first"})
    raw = he.canonical_bytes(env)
    sig = sp.sign(hashlib.sha256(raw).digest(), SEC_A)
    first = hub.admit(raw, sig.hex(), PUB_A)
    assert first["admitted"]
    assert _admit(hub, _rotate_env(PUB_NEW))["admitted"]
    assert _admit(hub, _revoke_env("k1", 1))["admitted"]
    size = hub._log.tree_size
    again = hub.admit(raw, sig.hex(), PUB_A)             # 동일 raw·sig·pubkey
    assert again["admitted"] and again["duplicate"] is True
    assert (again["event_id"], again["accepted_seq"]) == \
        (first["event_id"], first["accepted_seq"])
    assert again["receipt"] == first["receipt"]
    assert hub._log.tree_size == size                    # 상태 무변


def test_c3_다른_키의_재서명_재전송은_수렴이_아니라_거부(hub):
    """pubkey 일치 조건 — 타 등록 키가 남의 bytes를 재서명해 보내면 수렴 경로가
    아니라 binding/conflict 경로다."""
    env = _envelope("message.read", {"cursor": "c-first"})
    raw = he.canonical_bytes(env)
    sig_a = sp.sign(hashlib.sha256(raw).digest(), SEC_A)
    assert hub.admit(raw, sig_a.hex(), PUB_A)["admitted"]
    sig_b = sp.sign(hashlib.sha256(raw).digest(), SEC_B)  # B가 같은 bytes 재서명
    r = hub.admit(raw, sig_b.hex(), PUB_B)
    assert not r["admitted"] and r["duplicate"] is False


def test_lxm_방향1_미래_valid_from_키는_그_좌표_전_admission_거부():
    """[LxM 방향 1] admission이 감사와 같은 좌표 술어를 쓴다 — valid_from이 미래인
    키의 서명은 그 좌표 전엔 들어오지 못한다(불리언 평탄화였으면 통과했을 자리)."""
    keys = he.KeyRegistry()
    keys.register(PUB_A, signer_id="lab:organum-cody", key_id="k1", key_epoch=1,
                  valid_from_seq=1000)
    claims = he.ClaimRegistry(CLAIMS_DOC, expected_sha256=he.canonical_sha(CLAIMS_DOC))
    hub2 = he.HubIndex(key_registry=keys, claim_registry=claims,
                       log=hl.TransparencyLog(), receipt_seckey=SEC_HUB)
    r = _admit(hub2, _envelope("message.read", {"cursor": "c"}))
    assert not r["admitted"]
    assert any("아직 유효하지 않음" in p_ for p_ in r["problems"])


def test_lxm_방향2_미래_revoke_예약_키는_그_전_좌표에서_수용():
    """[LxM 방향 2] revoked_at이 미래면 지금 좌표에선 유효 — 감사가 유효라 하는
    좌표에서 기록이 들어온다(불리언 평탄화였으면 거부했을 자리)."""
    keys = he.KeyRegistry()
    keys.register(PUB_A, signer_id="lab:organum-cody", key_id="k1", key_epoch=1)
    keys.revoke(PUB_A, at_seq=9999)
    claims = he.ClaimRegistry(CLAIMS_DOC, expected_sha256=he.canonical_sha(CLAIMS_DOC))
    hub2 = he.HubIndex(key_registry=keys, claim_registry=claims,
                       log=hl.TransparencyLog(), receipt_seckey=SEC_HUB)
    r = _admit(hub2, _envelope("message.read", {"cursor": "c"}))
    assert r["admitted"], r["problems"]
    assert hub2.keys.was_valid(PUB_A, r["accepted_seq"]) is True


def test_c1_registry_tuple_유일성은_모든_entry_point에서():
    """[Orin C1] (signer,key_id,epoch) 중복은 register 자체가 거부 — bootstrap
    경로도 우회 불가."""
    keys = he.KeyRegistry()
    keys.register(PUB_A, signer_id="lab:organum-cody", key_id="k1", key_epoch=1)
    with pytest.raises(he.HubEnvelopeError, match="tuple이 이미 결속"):
        keys.register(PUB_B, signer_id="lab:organum-cody", key_id="k1", key_epoch=1)


@pytest.mark.parametrize("kw", [
    {"signer_id": "no-prefix", "key_id": "k1", "key_epoch": 1},
    {"signer_id": "lab:x", "key_id": "한글키", "key_epoch": 1},
    {"signer_id": "lab:x", "key_id": "k1", "key_epoch": 0},
    {"signer_id": "lab:x", "key_id": "k1", "key_epoch": 1, "valid_from_seq": -1},
])
def test_c1_registry_shape_불변식(kw):
    keys = he.KeyRegistry()
    with pytest.raises(he.HubEnvelopeError):
        keys.register("c" * 64, **kw)


# ═══ LxM R1/R2 (0.4.4) — _KEY_ID 이름=술어 정렬 ═══════════════════════════════

@pytest.mark.parametrize("bad", [
    "K1",       # 켈빈 부호 K — IGNORECASE 유니코드 폴딩이 통과시키던 것(R1)
    "ſs",       # long s ſ
    "İstanbul", # İ
    "키1",           # 비ASCII 일반
])
def test_r1_key_id는_명시_ASCII_클래스만(bad):
    keys = he.KeyRegistry()
    with pytest.raises(he.HubEnvelopeError):
        keys.register("d" * 64, signer_id="lab:x", key_id=bad, key_epoch=1)


def test_r1_대소문자는_클래스가_드러내고_정체성은_exact():
    """(a)안 계약: 'K1'은 합법(클래스에 명시)이고 'k1'과 별개 정체성 — 플래그가
    "같다"고 주장하지 않으므로 이름과 술어가 한 이야기를 한다."""
    keys = he.KeyRegistry()
    keys.register("e" * 64, signer_id="lab:x", key_id="k1", key_epoch=1)
    keys.register("f" * 64, signer_id="lab:x", key_id="K1", key_epoch=1)  # 합법·별개
    assert len(keys.bindings_of("lab:x")) == 2
