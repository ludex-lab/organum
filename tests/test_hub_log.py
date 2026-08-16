"""hub transparency log 회귀 — Merkle inclusion/consistency · signed tree head ·
**fixture 4: relay fork/tree-head equivocation**.

속성 검사가 주력이다: 생성기와 검증기가 같은 모듈에 있으므로, 전 사이즈(1..32)의
전 (index,size)·(old,new) 쌍을 소진해 내적 정합을 세우고 변조 거부로 fail-closed를
세운다. RFC 6962/9162와의 wire-호환은 P1 relay 대조 대상(모듈 docstring 참조).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from organum import hub_log as hl  # noqa: E402
from organum import schnorr_pure as sp  # noqa: E402

SEC = (7).to_bytes(32, "big")
PUB = sp.public_key(SEC)


def _log(n):
    log = hl.TransparencyLog()
    for i in range(n):
        log.append(f"leaf-{i}".encode())
    return log


def test_inclusion_전수_1_32():
    log = _log(32)
    for size in range(1, 33):
        root = log.root(size)
        for idx in range(size):
            proof = log.inclusion_proof(idx, size)
            assert hl.verify_inclusion(f"leaf-{idx}".encode(), idx, size, proof, root), \
                (idx, size)


def test_consistency_전수_1_32():
    log = _log(32)
    for new in range(1, 33):
        for old in range(1, new + 1):
            proof = log.consistency_proof(old, new)
            assert hl.verify_consistency(old, log.root(old), new, log.root(new), proof), \
                (old, new)


def test_inclusion_변조_거부():
    log = _log(20)
    root = log.root()
    proof = log.inclusion_proof(7)
    assert not hl.verify_inclusion(b"leaf-8", 7, 20, proof, root)          # 다른 leaf
    assert not hl.verify_inclusion(b"leaf-7", 8, 20, proof, root)          # 다른 index
    assert not hl.verify_inclusion(b"leaf-7", 7, 20, proof[:-1], root)     # 잘린 proof
    assert not hl.verify_inclusion(b"leaf-7", 7, 20, proof, log.root(19))  # 다른 root
    mutated = [proof[0][::-1]] + proof[1:]
    assert not hl.verify_inclusion(b"leaf-7", 7, 20, mutated, root)        # 변조 노드


def test_consistency_변조_거부():
    log = _log(20)
    proof = log.consistency_proof(13, 20)
    assert not hl.verify_consistency(13, log.root(12), 20, log.root(20), proof)
    assert not hl.verify_consistency(13, log.root(13), 20, log.root(19), proof)
    assert not hl.verify_consistency(13, log.root(13), 20, log.root(20), proof[:-1])
    assert not hl.verify_consistency(13, log.root(13), 20, log.root(20), [])
    # 과거를 바꾼 로그는 같은 size에서 다른 root — consistency의 전제 확인
    forked = _log(13)
    forked._leaves[5] = hl._leaf(b"tampered")
    assert forked.root(13) != log.root(13)


def test_같은_size_같은_root_빈_proof():
    log = _log(9)
    assert hl.verify_consistency(9, log.root(9), 9, log.root(9), [])
    assert not hl.verify_consistency(9, log.root(9), 9, log.root(8), [])


def test_signed_tree_head_라운드트립():
    log = _log(10)
    sth = hl.sign_tree_head(tree_size=10, root=log.root(), seckey=SEC)
    assert hl.verify_tree_head(sth, PUB)
    bad = {"body": dict(sth["body"], tree_size=11), "signature": sth["signature"]}
    assert not hl.verify_tree_head(bad, PUB)                # body 변조
    assert not hl.verify_tree_head(sth, sp.public_key((11).to_bytes(32, "big")))


def test_f04_relay_fork_equivocation():
    """[fixture 4] 같은 tree_size에 다른 root를 서명해 나눠준 relay — 클라이언트 간
    tree head 비교로 탐지."""
    log_a = _log(10)
    log_b = _log(9)
    log_b.append(b"tampered-leaf")                          # 같은 size, 다른 내용
    sth_a = hl.sign_tree_head(tree_size=10, root=log_a.root(), seckey=SEC)
    sth_b = hl.sign_tree_head(tree_size=10, root=log_b.root(), seckey=SEC)
    r = hl.detect_fork(sth_a, sth_b, PUB)
    assert r["fork"] is True and "equivocation" in r["reason"]


def test_f04b_append_only_위반_fork():
    """과거를 다시 쓴 relay — 옛 head가 새 head의 prefix가 아니면 fork."""
    honest = _log(16)
    sth_old = hl.sign_tree_head(tree_size=10, root=honest.root(10), seckey=SEC)
    rewritten = _log(9)
    rewritten._leaves[3] = hl._leaf(b"rewritten")
    for i in range(9, 16):
        rewritten.append(f"leaf-{i}".encode())
    sth_new = hl.sign_tree_head(tree_size=16, root=rewritten.root(), seckey=SEC)
    r = hl.detect_fork(sth_old, sth_new, PUB,
                       consistency=rewritten.consistency_proof(10, 16))
    assert r["fork"] is True and "append-only 위반" in r["reason"]


def test_f04c_미검증_head로는_fork_판정하지_않는다():
    """서명 안 된 head의 불일치는 fork의 증거가 아니라 판정 불능(null) — false≠null."""
    log = _log(10)
    sth = hl.sign_tree_head(tree_size=10, root=log.root(), seckey=SEC)
    forged = {"body": {"schema": hl.TREE_HEAD_SCHEMA, "tree_size": 10,
                       "root": "00" * 32}, "signature": "00" * 64}
    r = hl.detect_fork(sth, forged, PUB)
    assert r["fork"] is None


def test_정상_prefix는_fork_아님():
    log = _log(24)
    a = hl.sign_tree_head(tree_size=10, root=log.root(10), seckey=SEC)
    b = hl.sign_tree_head(tree_size=24, root=log.root(24), seckey=SEC)
    r = hl.detect_fork(a, b, PUB, consistency=log.consistency_proof(10, 24))
    assert r["fork"] is False
    # proof 없이 크기 다른 두 head는 판정 불능(fork 단정 금지)
    assert hl.detect_fork(a, b, PUB)["fork"] is None
