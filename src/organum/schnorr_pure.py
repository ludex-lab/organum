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


# ── 점 연산 (야코비 좌표) ───────────────────────────────────────────────────
#
# 야코비 `(X, Y, Z)`는 아핀 `(X/Z², Y/Z³)`를 뜻하고, 무한원점은 `Z == 0`이다.
#
# **왜 아핀을 버렸나 (2026-08-26, 실측이 시킨 일)**: 아핀 덧셈은 기울기를 구할 때마다
# 모듈러 역원 `pow(x, P-2, P)`를 부른다 — 256비트 모듈러 거듭제곱이 **덧셈 한 번마다**
# 한 번이다. 검증 1회에 ~1,500번이 쌓여 이벤트당 113ms가 됐고, "로그가 곧 상태"라
# 매 명령이 전체 로그를 재생하는 hub에서 이게 벽으로 나타났다: 97 이벤트 재생 11초,
# 그중 **10.9초가 여기**(프로파일: `pow()` 74,638회). 하루 11.5건씩 쌓이던 속도로는
# 석 달 뒤 명령 하나에 2분이었다. 야코비는 역원을 **마지막 한 번**으로 미룬다.
#
# 바뀌지 않는 것: 서명 형식도, 검증 판정도 비트 단위로 같다. 그 동일성은 공식
# BIP-340 벡터 19건(**거부되어야 하는 음성 10건 포함**)이 매 실행 고정한다 —
# 최적화가 음성 케이스를 조용히 열어젖히는 것이 이런 전환의 전형적 사고라서,
# 벡터를 먼저 깔고 전환했다. 상수시간이 아닌 것은 그대로다(good-faith 위협 모델).

_INFINITY = (0, 0, 0)


def _jac_double(p):
    x, y, z = p
    if z == 0 or y == 0:
        return _INFINITY
    a = x * x % P
    b = y * y % P
    c = b * b % P
    d = 2 * ((x + b) * (x + b) - a - c) % P
    e = 3 * a % P
    f = e * e % P
    x3 = (f - 2 * d) % P
    return x3, (e * (d - x3) - 8 * c) % P, 2 * y * z % P


def _jac_add(p, q):
    if p[2] == 0:
        return q
    if q[2] == 0:
        return p
    x1, y1, z1 = p
    x2, y2, z2 = q
    z1z1 = z1 * z1 % P
    z2z2 = z2 * z2 % P
    u1 = x1 * z2z2 % P
    u2 = x2 * z1z1 % P
    s1 = y1 * z2 % P * z2z2 % P
    s2 = y2 * z1 % P * z1z1 % P
    if u1 == u2:
        # 같은 x — 같은 점이면 두 배, 아니면 서로의 역원이라 무한원점이다.
        return _jac_double(p) if s1 == s2 else _INFINITY
    h = (u2 - u1) % P
    i = 4 * h * h % P
    j = h * i % P
    r = 2 * (s2 - s1) % P
    v = u1 * i % P
    x3 = (r * r - j - 2 * v) % P
    y3 = (r * (v - x3) - 2 * s1 * j) % P
    z3 = ((z1 + z2) * (z1 + z2) - z1z1 - z2z2) % P * h % P
    return x3, y3, z3


def _to_affine(p):
    """야코비 → 아핀. **여기서만** 모듈러 역원을 한 번 쓴다. 무한원점은 None."""
    x, y, z = p
    if z == 0:
        return None
    zinv = pow(z, P - 2, P)
    zinv2 = zinv * zinv % P
    return x * zinv2 % P, y * zinv2 % P * zinv % P


def _jac_mul(point, k: int):
    """아핀 점 × 스칼라 → **야코비**(호출자가 필요할 때 한 번만 아핀으로 내린다)."""
    r = _INFINITY
    q = (point[0], point[1], 1)
    while k:
        if k & 1:
            r = _jac_add(r, q)
        q = _jac_double(q)
        k >>= 1
    return r


def _point_mul(point, k: int):
    """아핀 in → 아핀 out(무한원점 None). 종전 API·의미를 그대로 보존한다."""
    return _to_affine(_jac_mul(point, k))


def _point_add(a, b):
    """아핀 덧셈 — 종전 API 보존. 뜨거운 경로(verify)는 야코비로 직접 간다."""
    if a is None:
        return b
    if b is None:
        return a
    return _to_affine(_jac_add((a[0], a[1], 1), (b[0], b[1], 1)))


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
    # R = s·G - e·P — 두 스칼라곱과 덧셈을 야코비로 잇고 **마지막에 한 번만** 내린다
    rp = _to_affine(_jac_add(_jac_mul((GX, GY), s), _jac_mul(point, N - e)))
    if rp is None:
        return False
    rx, ry = rp
    return ry % 2 == 0 and rx == r
