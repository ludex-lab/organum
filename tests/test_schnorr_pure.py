"""BIP-340 순수 구현 회귀.

**공식 벡터 전수 대조**(2026-08-26 추가): `tests/vectors/bip340-test-vectors.csv`
— bitcoin/bips의 BIP-340 규범 부속물을 원시 바이트로 받아 벤더링했고, 파일 자체를
content SHA로 결속한다(`tests/vectors/PIN.md`). 19 벡터 중 10건이 **반드시 거부**
되어야 하는 음성 케이스라, 이 배터리는 "서명이 통과한다"만이 아니라 "통과하면 안
되는 것이 통과하지 않는다"를 함께 고정한다 — 야코비 좌표 전환 같은 최적화가
음성 케이스를 조용히 열어젖히는 것을 막는 안전망이다.

정직 라벨: 상수시간 아님 — good-faith 위협 모델 전용(모듈 docstring).
"""
import csv
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from organum import schnorr_pure as sp  # noqa: E402

VECTORS = Path(__file__).resolve().parent / "vectors" / "bip340-test-vectors.csv"
VECTORS_SHA256 = "34c9d1d9c3a88d524bc80778540dc43f8306ec249a7485293063c376db851c2d"


def _load_vectors():
    raw = VECTORS.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == VECTORS_SHA256, (
        "벤더링한 BIP-340 벡터의 content SHA가 PIN과 다르다 — 벡터가 바뀐 것인지 "
        "파일이 손상된 것인지 확인하기 전에는 이 배터리의 판정을 믿지 말 것")
    return list(csv.DictReader(raw.decode("utf-8").splitlines()))


def test_공식_벡터_파일_결속과_모양():
    """근거 파일이 먼저 자기 자신을 증명한다 — 음성 케이스가 실제로 들어 있는지까지.
    (배터리가 조용히 양성만 남으면 '초록불 거짓말'이 된다.)"""
    rows = _load_vectors()
    assert len(rows) == 19
    assert sum(r["verification result"] == "FALSE" for r in rows) == 10
    assert sum(bool(r["secret key"]) for r in rows) == 8


def test_공식_벡터_전수_검증():
    """19 벡터 전부 — 통과해야 할 것은 통과하고, 거부해야 할 것은 거부한다."""
    for r in _load_vectors():
        idx, comment = r["index"], r["comment"]
        pub = bytes.fromhex(r["public key"])
        msg = bytes.fromhex(r["message"])
        sig = bytes.fromhex(r["signature"])
        expected = r["verification result"] == "TRUE"
        assert sp.verify(sig, msg, pub) is expected, \
            f"벡터 {idx} 검증 결과가 공식값과 다르다 ({comment or '주석 없음'})"


def test_공식_벡터_서명_재생성():
    """secret key가 있는 8 벡터는 서명 bytes까지 재생성해 대조한다 — 검증만 맞고
    생성이 어긋나는 구현을 잡는다(크기 0·1·17·100 메시지 포함)."""
    signed = 0
    for r in _load_vectors():
        if not r["secret key"]:
            continue
        sec = bytes.fromhex(r["secret key"])
        aux = bytes.fromhex(r["aux_rand"])
        msg = bytes.fromhex(r["message"])
        assert sp.public_key(sec).hex().upper() == r["public key"].upper(), \
            f"벡터 {r['index']} 공개키 유도 불일치"
        assert sp.sign(msg, sec, aux).hex().upper() == r["signature"].upper(), \
            f"벡터 {r['index']} 서명 bytes 불일치"
        signed += 1
    assert signed == 8


def _affine_mul_reference(point, k: int):
    """**전환 전 아핀 구현**(2026-08-26 야코비 전환 이전의 그 코드 그대로).

    야코비 전환은 순수 최적화이므로 증명 의무는 "같은 답을 낸다"이다. 공식 벡터는
    구현이 BIP-340인지를 말해 주지만, 이 참조는 **전환이 무엇도 바꾸지 않았는지**를
    말해 준다 — 다른 질문이라 둘 다 필요하다. 느려서 소수 표본만 돌린다."""
    def add(a, b):
        if a is None:
            return b
        if b is None:
            return a
        ax, ay = a
        bx, by = b
        if ax == bx and (ay + by) % sp.P == 0:
            return None
        if a == b:
            lam = (3 * ax * ax) * pow(2 * ay, sp.P - 2, sp.P) % sp.P
        else:
            lam = (by - ay) * pow(bx - ax, sp.P - 2, sp.P) % sp.P
        x = (lam * lam - ax - bx) % sp.P
        return x, (lam * (ax - x) - ay) % sp.P

    r = None
    while k:
        if k & 1:
            r = add(r, point)
        point = add(point, point)
        k >>= 1
    return r


def test_야코비_전환은_아핀과_비트단위로_같다():
    """차등 시험: 옛 아핀 구현과 새 야코비 구현이 같은 점을 낸다(무한원점 포함)."""
    g = (sp.GX, sp.GY)
    scalars = [1, 2, 3, 7, 2**128 + 1, sp.N - 1,
               0x1E2F3A4B5C6D7E8F90A1B2C3D4E5F60718293A4B5C6D7E8F90A1B2C3D4E5F607]
    for k in scalars:
        assert sp._point_mul(g, k) == _affine_mul_reference(g, k), f"k={k:#x}"
    assert sp._point_mul(g, 0) is None                     # 무한원점
    assert sp._point_mul(g, sp.N) is None                  # n·G = 무한원점
    # 덧셈: P + (-P) = 무한원점 · P + P = 2P(두 배 경로로 빠지는지)
    gx, gy = g
    assert sp._point_add(g, (gx, sp.P - gy)) is None
    assert sp._point_add(g, g) == sp._point_mul(g, 2)
    assert sp._point_add(g, None) == g and sp._point_add(None, g) == g


def test_공식_벡터0_공개키():
    sec = bytes.fromhex("00" * 31 + "03")
    assert sp.public_key(sec).hex() == \
        "f9308a019258c31049344f85f89d5229b531c845836f99b08601f113bce036f9"


def test_서명_결정론_pin():
    """같은 (msg, seckey, aux) → 같은 서명. 이 bytes가 바뀌면 구현이 바뀐 것이다."""
    sec = bytes.fromhex("00" * 31 + "03")
    sig = sp.sign(bytes(32), sec, bytes(32))
    assert sig.hex() == ("e907831f80848d1069a5371b402410364bdf1c5f8307b0084c55f1ce"
                         "2dca821525f66a4a85ea8b71e482a74f382d2ce5ebeee8fdb2172f47"
                         "7df4900d310536c0")
    assert sp.sign(bytes(32), sec, bytes(32)) == sig


def test_라운드트립_및_변조_거부():
    for k in (2, 5, 1000, 2**200):
        sec = k.to_bytes(32, "big")
        pub = sp.public_key(sec)
        msg = f"organum-hub-{k}".encode()
        sig = sp.sign(msg, sec)
        assert sp.verify(sig, msg, pub)
        assert not sp.verify(sig, msg + b"x", pub)
        assert not sp.verify(bytes([sig[0] ^ 1]) + sig[1:], msg, pub)
        assert not sp.verify(sig[:32] + bytes([sig[32] ^ 1]) + sig[33:], msg, pub)
        assert not sp.verify(sig, msg, sp.public_key((k + 1).to_bytes(32, "big")))


def test_잘못된_입력은_False_예외_아님():
    sec = (7).to_bytes(32, "big")
    pub = sp.public_key(sec)
    sig = sp.sign(b"m", sec)
    assert not sp.verify(sig[:63], b"m", pub)          # 길이 위반
    assert not sp.verify(sig, b"m", pub[:31])
    assert not sp.verify(b"\xff" * 64, b"m", pub)      # r ≥ p
    assert not sp.verify(sig, b"m", b"\xff" * 32)      # 곡선 밖 x


def test_범위_밖_seckey_거부():
    with pytest.raises(ValueError):
        sp.public_key(bytes(32))                       # 0
    with pytest.raises(ValueError):
        sp.sign(b"m", sp.N.to_bytes(32, "big"))        # n
