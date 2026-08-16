"""BIP-340 순수 구현 회귀.

정직 라벨: 공개키 유도는 BIP-340 공식 test vector 0과 대조(secret 3 → F9308A…).
서명 bytes는 자체 결정론 회귀로 pin — 공식 서명 벡터와의 전수 대조는 P1에서 감사된
라이브러리와 함께(당시 이 pin이 다르면 구현 결함이 드러난다). 상수시간 아님 —
good-faith 위협 모델 전용(모듈 docstring).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from organum import schnorr_pure as sp  # noqa: E402


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
