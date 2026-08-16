"""BIP-340 Schnorr(secp256k1) 순수 파이썬 참조 구현 — hub 봉투 서명용.

왜 이 곡선인가: hub transport 계약은 "서명 이벤트 append+구독"이고 그 자리를 Buzz(Nostr
기반)가 채운다(`docs/open-stack-integration-v0.md`). Nostr 서명은 BIP-340이다. P1
byte-level 대조가 미루는 것은 **outer event가 서명하는 exact byte 범위**이지 곡선이 아니다.

정직 경계(bench `ed25519_pure`와 동일 규율):
- good-faith 위협 모델 전용 — 상수시간 아님, side-channel 저항 없음. hub의 위협 모델
  자체가 "자기 기만과 사후 서사를 막는 것이지 악의적 랩을 막는 것이 아니다"(v0.2 Δ2).
- 서명 형식은 표준 BIP-340이라 실 배포에서 감사된 라이브러리(secp256k1 바인딩)로
  교체해도 호환된다.
- private key는 substrate/OS keystore 경계에 있고 모델 런타임에 절대 전달되지 않는다
  (봉투 스키마 §2 custody 불변식). 이 모듈은 그 substrate가 쓰는 도구다.
"""

from __future__ import annotations

import hashlib

# secp256k1 도메인 파라미터
P = 2**256 - 2**32 - 977
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8


def _tagged_hash(tag: str, msg: bytes) -> bytes:
    t = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(t + t + msg).digest()


# ── 점 연산 (Jacobian 없이 아핀 — 참조 구현의 명료성 우선) ──────────────────

def _point_add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    ax, ay = a
    bx, by = b
    if ax == bx and (ay + by) % P == 0:
        return None
    if a == b:
        lam = (3 * ax * ax) * pow(2 * ay, P - 2, P) % P
    else:
        lam = (by - ay) * pow(bx - ax, P - 2, P) % P
    x = (lam * lam - ax - bx) % P
    return x, (lam * (ax - x) - ay) % P


def _point_mul(point, k: int):
    r = None
    while k:
        if k & 1:
            r = _point_add(r, point)
        point = _point_add(point, point)
        k >>= 1
    return r


def _lift_x(x: int):
    """x-only 공개키 → even-y 점. 곡선 밖이면 None."""
    if x >= P:
        return None
    y_sq = (pow(x, 3, P) + 7) % P
    y = pow(y_sq, (P + 1) // 4, P)
    if y * y % P != y_sq:
        return None
    return x, y if y % 2 == 0 else P - y


def _int_from(b: bytes) -> int:
    return int.from_bytes(b, "big")


def _bytes_from(i: int) -> bytes:
    return i.to_bytes(32, "big")


def public_key(seckey: bytes) -> bytes:
    """secret 32B → x-only 공개키 32B."""
    d = _int_from(seckey)
    if not 1 <= d <= N - 1:
        raise ValueError("secret key 범위 밖")
    px, _ = _point_mul((GX, GY), d)
    return _bytes_from(px)


def sign(msg: bytes, seckey: bytes, aux_rand: bytes = b"\x00" * 32) -> bytes:
    """BIP-340 서명 64B. msg는 임의 길이(hub는 canonical envelope bytes의 SHA-256을 넘긴다)."""
    d0 = _int_from(seckey)
    if not 1 <= d0 <= N - 1:
        raise ValueError("secret key 범위 밖")
    if len(aux_rand) != 32:
        raise ValueError("aux_rand는 32바이트")
    px, py = _point_mul((GX, GY), d0)
    d = d0 if py % 2 == 0 else N - d0
    t = d ^ _int_from(_tagged_hash("BIP0340/aux", aux_rand))
    k0 = _int_from(_tagged_hash("BIP0340/nonce",
                                _bytes_from(t) + _bytes_from(px) + msg)) % N
    if k0 == 0:
        raise RuntimeError("nonce 0 — aux_rand를 바꿔 재시도")
    rx, ry = _point_mul((GX, GY), k0)
    k = k0 if ry % 2 == 0 else N - k0
    e = _int_from(_tagged_hash("BIP0340/challenge",
                               _bytes_from(rx) + _bytes_from(px) + msg)) % N
    sig = _bytes_from(rx) + _bytes_from((k + e * d) % N)
    if not verify(sig, msg, _bytes_from(px)):      # 자체 검증 — 잘못된 서명을 내보내지 않는다
        raise RuntimeError("생성된 서명이 자체 검증 실패")
    return sig


def verify(sig: bytes, msg: bytes, pubkey: bytes) -> bool:
    """BIP-340 검증. 예외 대신 False — 검증자는 fail-closed로 소비한다."""
    if len(sig) != 64 or len(pubkey) != 32:
        return False
    point = _lift_x(_int_from(pubkey))
    if point is None:
        return False
    r, s = _int_from(sig[:32]), _int_from(sig[32:])
    if r >= P or s >= N:
        return False
    e = _int_from(_tagged_hash("BIP0340/challenge",
                               sig[:32] + pubkey + msg)) % N
    # R = s·G - e·P
    sg = _point_mul((GX, GY), s)
    ep = _point_mul(point, N - e)
    rp = _point_add(sg, ep)
    if rp is None:
        return False
    rx, ry = rp
    return ry % 2 == 0 and rx == r
