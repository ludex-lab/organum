"""organum hub — transparency log 검증 (v0.1 §3: 전역 순서의 authority).

전역 순서는 wrapping의 transparency log가 소유한다: 단조 `accepted_seq` · `tree_size` ·
`previous_root→current_root` · **Merkle inclusion/consistency proof + signed tree head**.
클라이언트끼리 tree head를 비교해 relay equivocation/fork를 탐지한다.

이 모듈은 **검증자 관점**이다 — relay(Buzz wrapping)가 낼 proof를 소비·검증하고,
기준 구현으로서 같은 proof를 생성도 한다(생성이 있어야 검증이 시험 가능하다: 산출물 먼저).

해시 구조는 RFC 6962(Certificate Transparency)와 동일한 leaf/node 도메인 분리:
`leaf = SHA-256(0x00 ‖ bytes)` · `node = SHA-256(0x01 ‖ left ‖ right)`. 빈 트리 root는
SHA-256(빈 문자열). 이 선택 자체가 P1 byte-level 대조 대상이며, relay 구현이 다른 profile을
쓰면 여기의 profile 상수가 갈라진 것을 드러낸다.
"""

from __future__ import annotations

import hashlib

try:
    from organum import schnorr_pure as _schnorr
    from organum import hub_envelope as _env
except ImportError:                                    # 스크립트 직접 실행 경로
    import schnorr_pure as _schnorr
    import hub_envelope as _env

LOG_PROFILE = "organum-hub/merkle-rfc6962-sha256/v1"
TREE_HEAD_SCHEMA = "organum-hub/signed-tree-head/v1"


class HubLogError(ValueError):
    pass


def _leaf(data: bytes) -> bytes:
    return hashlib.sha256(b"\x00" + data).digest()


def _node(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def _root(leaves: list) -> bytes:
    """RFC 6962 §2.1 — 왼쪽이 2의 최대 거듭제곱."""
    n = len(leaves)
    if n == 0:
        return hashlib.sha256(b"").digest()
    if n == 1:
        return leaves[0]
    k = 1
    while k * 2 < n:
        k *= 2
    return _node(_root(leaves[:k]), _root(leaves[k:]))


class TransparencyLog:
    """append-only log. leaf bytes = admitted event의 canonical envelope bytes."""

    def __init__(self):
        self._leaves = []                       # leaf hash들
        self._entries = []                      # 원 bytes (proof 재생성용)

    @property
    def tree_size(self) -> int:
        return len(self._leaves)

    def root(self, size: int = None) -> bytes:
        size = self.tree_size if size is None else size
        if not 0 <= size <= self.tree_size:
            raise HubLogError("root: size 범위 밖")
        return _root(self._leaves[:size])

    def append(self, data: bytes) -> int:
        """append 후 (0-기반) leaf index 반환. accepted_seq와의 관계: seq = index + 1."""
        self._leaves.append(_leaf(bytes(data)))
        self._entries.append(bytes(data))
        return len(self._leaves) - 1

    # ── inclusion proof (RFC 6962 §2.1.1) ──
    def inclusion_proof(self, index: int, size: int = None) -> list:
        size = self.tree_size if size is None else size
        if not 0 <= index < size <= self.tree_size:
            raise HubLogError("inclusion: index/size 범위 밖")

        def path(m, leaves):
            n = len(leaves)
            if n == 1:
                return []
            k = 1
            while k * 2 < n:
                k *= 2
            if m < k:
                return path(m, leaves[:k]) + [_root(leaves[k:])]
            return path(m - k, leaves[k:]) + [_root(leaves[:k])]

        return path(index, self._leaves[:size])

    # ── consistency proof (RFC 6962 §2.1.2) ──
    def consistency_proof(self, old_size: int, new_size: int = None) -> list:
        new_size = self.tree_size if new_size is None else new_size
        if not 0 < old_size <= new_size <= self.tree_size:
            raise HubLogError("consistency: size 범위 밖")
        if old_size == new_size:
            return []

        def sub(m, leaves, complete):
            n = len(leaves)
            if m == n:
                return [] if complete else [_root(leaves)]
            k = 1
            while k * 2 < n:
                k *= 2
            if m <= k:
                return sub(m, leaves[:k], complete) + [_root(leaves[k:])]
            return sub(m - k, leaves[k:], False) + [_root(leaves[:k])]

        return sub(old_size, self._leaves[:new_size], True)


def verify_inclusion(leaf_data: bytes, index: int, size: int, proof: list,
                     expected_root: bytes) -> bool:
    """leaf가 그 root의 트리에 있는가 — fail-closed(예외 대신 False)."""
    if not 0 <= index < size:
        return False
    h = _leaf(bytes(leaf_data))
    fn, sn = index, size - 1
    for p in proof:
        if sn == 0:
            return False
        if fn & 1 or fn == sn:
            h = _node(p, h)
            if not fn & 1:
                while True:
                    fn >>= 1
                    sn >>= 1
                    if fn & 1 or fn == 0:
                        break
        else:
            h = _node(h, p)
        fn >>= 1
        sn >>= 1
    return sn == 0 and h == expected_root


def verify_consistency(old_size: int, old_root: bytes, new_size: int, new_root: bytes,
                       proof: list) -> bool:
    """옛 트리가 새 트리의 prefix인가 — append-only의 증명(RFC 9162 §2.1.4.2).
    fail-closed(예외 대신 False)."""
    if old_size == new_size:
        return old_root == new_root and not proof
    if not 0 < old_size < new_size:
        return False
    if not proof:
        return False
    p = list(proof)
    if old_size & (old_size - 1) == 0:      # old가 2^k이면 old root가 proof의 암묵 첫 항
        p = [old_root] + p
    fn, sn = old_size - 1, new_size - 1
    while fn & 1:
        fn >>= 1
        sn >>= 1
    fr = sr = p[0]
    for c in p[1:]:
        if sn == 0:
            return False
        if fn & 1 or fn == sn:
            fr = _node(c, fr)
            sr = _node(c, sr)
            if not fn & 1:
                while True:
                    fn >>= 1
                    sn >>= 1
                    if fn & 1 or fn == 0:
                        break
        else:
            sr = _node(sr, c)
        fn >>= 1
        sn >>= 1
    return fr == old_root and sr == new_root and sn == 0


# ── signed tree head + fork 탐지 ─────────────────────────────────────────────

def sign_tree_head(*, tree_size: int, root: bytes, seckey: bytes) -> dict:
    body = {"schema": TREE_HEAD_SCHEMA, "tree_size": tree_size, "root": root.hex()}
    raw = _env.canonical_bytes(body)
    sig = _schnorr.sign(hashlib.sha256(raw).digest(), seckey)
    return {"body": body, "signature": sig.hex()}


def verify_tree_head(sth: dict, pubkey: bytes) -> bool:
    try:
        body, sig = sth["body"], bytes.fromhex(sth["signature"])
        if body["schema"] != TREE_HEAD_SCHEMA:
            return False
        raw = _env.canonical_bytes(body)
    except (KeyError, TypeError, ValueError, _env.HubEnvelopeError):
        return False
    return _schnorr.verify(sig, hashlib.sha256(raw).digest(), pubkey)


def detect_fork(sth_a: dict, sth_b: dict, pubkey: bytes, *,
                consistency: list = None) -> dict:
    """두 signed tree head의 equivocation 판정 — 클라이언트 간 비교(§3).

    반환 {fork: bool|None, reason}. **미검증 head로는 판정하지 않는다**(None) —
    서명 안 된 head의 불일치는 fork의 증거가 아니라 판정 불능이다."""
    if not (verify_tree_head(sth_a, pubkey) and verify_tree_head(sth_b, pubkey)):
        return {"fork": None, "reason": "서명 미검증 head — 판정 불능(fork 증거 아님)"}
    a, b = sth_a["body"], sth_b["body"]
    if a["tree_size"] == b["tree_size"]:
        if a["root"] != b["root"]:
            return {"fork": True, "reason": "같은 tree_size에 다른 root(equivocation)"}
        return {"fork": False, "reason": "동일 head"}
    small, big = (a, b) if a["tree_size"] < b["tree_size"] else (b, a)
    if consistency is None:
        return {"fork": None, "reason": "consistency proof 없이는 prefix 여부 판정 불능"}
    ok = verify_consistency(small["tree_size"], bytes.fromhex(small["root"]),
                            big["tree_size"], bytes.fromhex(big["root"]), consistency)
    if not ok:
        return {"fork": True, "reason": "consistency proof 실패 — append-only 위반"}
    return {"fork": False, "reason": "옛 head가 새 head의 prefix"}
