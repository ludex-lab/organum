"""키 수명 기록의 고정(0.10.0) — 소비자의 수용 규칙.

형식은 한 signer의 유효한 키를 가리지 않는다. 위임받은 키(서명 서비스의 전용 키)가 그 signer의 새
키를 들이거나 다른 키를 끊는 기록에 서명하면 형식으로는 선다. 그것을 막는 것은 받는 쪽의 규칙이다
(Orin 061 ②).

계약:
1. 고정한 signer의 `key.rotated`·`key.revoked`는 고정한 결속(공개키·key_id·epoch)의 서명만 받는다.
2. 거절은 registry를 바꾸는 admit보다 앞이다. 거절된 기록은 원장도 registry도 바꾸지 않는다.
3. 고정은 그것을 건 다음 좌표부터 든다. 앞서 받아들인 기록은 재생에서 그대로 선다.
4. 고정할 수 있는 것은 이 원장이 이미 믿는 유효한 결속뿐이다.
5. 고정의 모양이 틀린 hub.json은 열리지 않는다.
6. 서명 키가 다른 기기에 있으면 봉투만 내놓고(`--unsigned-out`) 그 기기에서 서명해 넣는다.

실제 키는 쓰지 않는다. 시험마다 새 키를 만든다.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from organum import hub_cli as cli  # noqa: E402
from organum import hub_ops as ho  # noqa: E402

X = "lab:x"


def run(capsys, *argv):
    try:
        rc = cli.main(list(argv))
    except SystemExit as e:
        rc = e.code
    cap = capsys.readouterr()
    out = cap.out.strip()
    try:
        return rc, (json.loads(out) if out else None), cap.err
    except ValueError:
        return rc, out, cap.err


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _keygen(capsys, name):
    return run(capsys, "keygen", name)[1]["pubkey"]


def _setup(capsys):
    """운영 연구소 a의 원장에 lab:x를 들인다. x는 복구 키 k1과, k1이 들인 운영 키 k2를 지닌다."""
    a = _keygen(capsys, "a1")
    x1, x2 = _keygen(capsys, "x1"), _keygen(capsys, "x2")
    assert run(capsys, "init", "--dir", "hub", "--source-domain", "lab:a/hub")[0] == 0
    assert run(capsys, "register-key", "--dir", "hub", "--signer", "lab:a", "--key-id", "k1",
               "--epoch", "1", "--pubkey", a)[0] == 0
    rc, r, _ = run(capsys, "introduce-signer", "--dir", "hub", "--key", "a1.seed", "--signer", "lab:a",
                   "--key-id", "k1", "--epoch", "1", "--new-signer", X, "--new-key-id", "k1",
                   "--new-epoch", "1", "--new-pubkey", x1)
    assert rc == 0 and r["admitted"]
    assert _rotate(capsys, "x1", "k1", "k2", x2)[1]["admitted"]
    return x1, x2


def _rotate(capsys, seed, key_id, new_id, new_pub):
    return run(capsys, "rotate-key", "--dir", "hub", "--key", f"{seed}.seed", "--signer", X,
               "--key-id", key_id, "--epoch", "1", "--new-key-id", new_id, "--new-epoch", "1",
               "--new-pubkey", new_pub)


def _revoke(capsys, seed, key_id, target):
    return run(capsys, "revoke-key", "--dir", "hub", "--key", f"{seed}.seed", "--signer", X,
               "--key-id", key_id, "--epoch", "1", "--revoke-key-id", target, "--revoke-epoch", "1")


def _pin(capsys, key_id="k1", *more):
    return run(capsys, "pin-lifecycle", "--dir", "hub", "--signer", X, "--key-id", key_id,
               "--epoch", "1", *more)


def _lines():
    return Path("hub/events.jsonl").read_text(encoding="utf-8").splitlines()


def _bound(pubkey):
    return ho.load_hub("hub")[2].keys.lookup(pubkey)


def test_a_pinned_signer_takes_lifecycle_records_only_from_the_pinned_key(capsys, ws):
    x1, x2 = _setup(capsys)
    rc, pinned, _ = _pin(capsys)
    assert rc == 0 and pinned["already"] is False
    assert pinned["pinned"] == {"signer_id": X, "key_id": "k1", "key_epoch": 1, "pubkey": x1,
                                "from_seq": len(_lines()) + 1}
    before = _lines()
    # 운영 키 k2가 새 키를 들이려 한다 — 형식으로는 서는 기록이다
    x3 = _keygen(capsys, "x3")
    rc, r, _ = _rotate(capsys, "x2", "k2", "k3", x3)
    assert rc == 1 and r["admitted"] is False and r["stage"] == "policy"
    assert "lifecycle pin" in r["problems"][0]
    assert _lines() == before and _bound(x3) is None          # 원장도 registry도 그대로다
    # k2가 복구 키를 끊으려 한다
    rc, r, _ = _revoke(capsys, "x2", "k2", "k1")
    assert rc == 1 and "lifecycle pin" in r["problems"][0]
    assert _lines() == before and _bound(x1)["revoked"] is False
    # 복구 키 k1이 들이는 것은 받는다
    rc, r, _ = _rotate(capsys, "x1", "k1", "k3", x3)
    assert rc == 0 and r["admitted"] and _bound(x3)["key_id"] == "k3"
    # 새로 든 k3도 수명 기록에는 서명하지 못한다
    x4 = _keygen(capsys, "x4")
    assert _rotate(capsys, "x3", "k3", "k4", x4)[0] == 1 and _bound(x4) is None
    # 편지는 고정과 무관하다. k2와 k3이 그대로 서명한다
    Path("b.txt").write_text("hello", encoding="utf-8")
    for seed, key_id in (("x2", "k2"), ("x3", "k3")):
        Path("b.txt").write_text(f"hello from {key_id}", encoding="utf-8")
        rc, r, _ = run(capsys, "message", "--dir", "hub", "--key", f"{seed}.seed", "--signer", X,
                       "--key-id", key_id, "--epoch", "1", "--to-lab", "lab:a", "--to-id", "A",
                       "--to-epoch", "1", "--body-file", "b.txt", "--body-locator", "file://b.txt",
                       "--media-type", "text/plain")
        assert rc == 0 and r["admitted"], key_id
    # 고정하지 않은 signer는 전과 같다
    a2 = _keygen(capsys, "a2")
    rc, r, _ = run(capsys, "rotate-key", "--dir", "hub", "--key", "a1.seed", "--signer", "lab:a",
                   "--key-id", "k1", "--epoch", "1", "--new-key-id", "k2", "--new-epoch", "1",
                   "--new-pubkey", a2)
    assert rc == 0 and r["admitted"]
    # k1은 k2를 끊을 수 있다. 끊긴 뒤에도 원장은 재생된다
    rc, r, _ = _revoke(capsys, "x1", "k1", "k2")
    assert rc == 0 and r["admitted"] and _bound(x2)["revoked"] is True
    assert run(capsys, "list", "--dir", "hub")[0] == 0


def test_a_pin_starts_at_the_next_coordinate_so_the_log_still_replays(capsys, ws):
    x1, x2 = _setup(capsys)
    # 고정하기 전에 k2가 k3을 들였다. 그때는 받아들일 기록이었다
    x3 = _keygen(capsys, "x3")
    assert _rotate(capsys, "x2", "k2", "k3", x3)[1]["admitted"]
    n = len(_lines())
    assert _pin(capsys)[1]["pinned"]["from_seq"] == n + 1
    assert run(capsys, "list", "--dir", "hub")[0] == 0          # 앞의 기록은 그대로 선다
    x4 = _keygen(capsys, "x4")
    assert _rotate(capsys, "x3", "k3", "k4", x4)[0] == 1          # 그 뒤로는 k1만
    # 고정의 좌표를 앞으로 당기면 이미 받아들인 기록이 규칙에 걸려 원장이 열리지 않는다
    cfg = json.loads(Path("hub/hub.json").read_text(encoding="utf-8"))
    cfg["lifecycle_pins"][0]["from_seq"] = 1
    Path("hub/hub.json").write_text(json.dumps(cfg), encoding="utf-8")
    rc, _, err = run(capsys, "list", "--dir", "hub")
    assert rc == 2 and "로그 재생 실패" in err and "lifecycle pin" in err


def test_only_a_binding_this_ledger_already_trusts_can_be_pinned(capsys, ws):
    x1, x2 = _setup(capsys)
    cfg_before = Path("hub/hub.json").read_bytes()
    rc, _, err = _pin(capsys, "k9")
    assert rc == 2 and "결속이 이 원장에 없다" in err
    rc, _, err = _pin(capsys, "k1", "--pubkey", x2)              # 다른 길로 확인한 값과 다르다
    assert rc == 2 and "다르다" in err
    rc, _, err = run(capsys, "pin-lifecycle", "--dir", "hub", "--signer", "lab:nobody",
                     "--key-id", "k1", "--epoch", "1")
    assert rc == 2
    assert Path("hub/hub.json").read_bytes() == cfg_before       # 거절은 아무것도 쓰지 않는다
    rc, r, _ = _pin(capsys, "k1", "--pubkey", x1)
    assert rc == 0 and r["pinned"]["pubkey"] == x1 and r["already"] is False
    rc, r, _ = _pin(capsys)                                      # 같은 고정을 다시 걸면 그대로다
    assert rc == 0 and r["already"] is True
    assert len(json.loads(Path("hub/hub.json").read_text(encoding="utf-8"))["lifecycle_pins"]) == 1
    # 폐기된 결속은 고정하지 못한다
    assert _revoke(capsys, "x1", "k1", "k2")[1]["admitted"]
    rc, _, err = _pin(capsys, "k2")
    assert rc == 2 and "유효하지 않다" in err


def test_a_malformed_pin_refuses_to_open_the_ledger(capsys, ws):
    x1, _ = _setup(capsys)
    good = {"signer_id": X, "key_id": "k1", "key_epoch": 1, "pubkey": x1, "from_seq": 1}
    cfg = json.loads(Path("hub/hub.json").read_text(encoding="utf-8"))
    for bad in ({"not": "a list"}, 7, "k1", [dict(good, pubkey="zz")], [dict(good, from_seq=0)],
                [dict(good, key_epoch="1")], [dict(good, signer_id="x")],
                [{k: v for k, v in good.items() if k != "from_seq"}], [dict(good, extra=1)], ["k1"]):
        Path("hub/hub.json").write_text(json.dumps(dict(cfg, lifecycle_pins=bad)), encoding="utf-8")
        rc, _, err = run(capsys, "list", "--dir", "hub")
        assert rc == 2 and "lifecycle_pins" in err, bad
    Path("hub/hub.json").write_text(json.dumps(dict(cfg, lifecycle_pins=[])), encoding="utf-8")
    assert run(capsys, "list", "--dir", "hub")[0] == 0
    # 모양은 맞지만 원장의 결속과 다른 공개키를 적은 고정. 이름이 같다고 받아 주지 않는다 —
    # 그 signer의 수명 기록은 아무것도 서지 않는다(닫힌 쪽으로 실패한다)
    n = len(_lines())
    wrong = dict(good, pubkey="ab" * 32, from_seq=n + 1)
    Path("hub/hub.json").write_text(json.dumps(dict(cfg, lifecycle_pins=[wrong])), encoding="utf-8")
    x3 = _keygen(capsys, "x3")
    rc, r, _ = _rotate(capsys, "x1", "k1", "k3", x3)
    assert rc == 1 and "lifecycle pin" in r["problems"][0] and _bound(x3) is None
    # 고정이 여럿이면 그 가운데 하나에 맞으면 된다
    right = dict(good, from_seq=n + 1)
    Path("hub/hub.json").write_text(json.dumps(dict(cfg, lifecycle_pins=[wrong, right])), encoding="utf-8")
    assert _rotate(capsys, "x1", "k1", "k3", x3)[1]["admitted"]


def test_an_explicit_null_is_not_the_same_as_no_pins(capsys, ws):
    """Orin 062 R2. 고정을 건 뒤 `hub.json`에 `"lifecycle_pins": null`을 적는다. 그것을 「칸이 없다」로
    읽으면 원장이 정상으로 열리고 규칙이 조용히 빠진다 — 앞 pin에서는 k2가 서명한 키의 기록이 들어갔다."""
    x1, x2 = _setup(capsys)
    cfg = json.loads(Path("hub/hub.json").read_text(encoding="utf-8"))
    assert "lifecycle_pins" not in cfg                              # 칸이 없는 원장(0.9.0)은 그대로 열린다
    assert run(capsys, "list", "--dir", "hub")[0] == 0
    assert _pin(capsys)[0] == 0
    pinned = json.loads(Path("hub/hub.json").read_text(encoding="utf-8"))
    before = _lines()
    Path("hub/hub.json").write_text(json.dumps(dict(pinned, lifecycle_pins=None)), encoding="utf-8")
    x3 = _keygen(capsys, "x3")
    rc, _, err = _rotate(capsys, "x2", "k2", "k3", x3)
    assert rc == 2 and "lifecycle_pins" in err and "null" in err      # 원장이 열리지 않는다
    assert _lines() == before
    for cmd in (("list", "--dir", "hub"), ("pin-lifecycle", "--dir", "hub", "--signer", X, "--key-id", "k1",
                                           "--epoch", "1")):
        rc, _, err = run(capsys, *cmd)
        assert rc == 2 and "lifecycle_pins" in err, cmd
    with pytest.raises(ho.HubOpsError, match="lifecycle_pins"):
        ho.load_hub("hub")
    # 비우려면 빈 목록을 적는다. 그것은 밝힌 값이다. 칸을 빼는 것도 같다
    for cleared in (dict(pinned, lifecycle_pins=[]), {k: v for k, v in pinned.items() if k != "lifecycle_pins"}):
        Path("hub/hub.json").write_text(json.dumps(cleared), encoding="utf-8")
        assert run(capsys, "list", "--dir", "hub")[0] == 0
    # 고정이 그대로면 k2의 기록은 거절이다
    Path("hub/hub.json").write_text(json.dumps(pinned), encoding="utf-8")
    rc, r, _ = _rotate(capsys, "x2", "k2", "k3", x3)
    assert rc == 1 and "lifecycle pin" in r["problems"][0] and _bound(x3) is None
    # 원장을 여는 함수 밖에서 봉투의 층을 바로 부를 때도 모양을 본다. 그 층에서 None은 「주지 않았다」다
    from organum import hub_envelope as he
    assert he._lifecycle_pins_by_signer(None) == {} and he._lifecycle_pins_by_signer([]) == {}
    for bad in ({"a": 1}, "k1", 7, [None]):
        with pytest.raises(he.HubEnvelopeError):
            he._lifecycle_pins_by_signer(bad)


def test_the_signing_key_can_stay_on_another_machine(capsys, ws):
    """서명 서비스가 봉투를 짓고, 복구 키를 지닌 기기가 서명하고, 서비스가 넣는다. 키는 옮기지 않는다."""
    x1, x2 = _setup(capsys)
    assert _pin(capsys)[0] == 0
    x3 = _keygen(capsys, "x3")
    before = _lines()
    base = ("rotate-key", "--dir", "hub", "--signer", X, "--epoch", "1", "--new-key-id", "k3",
            "--new-epoch", "1", "--new-pubkey", x3)
    rc, out, _ = run(capsys, *base, "--key-id", "k1", "--unsigned-out", "rot.json")
    assert rc == 0 and out["unsigned"] == "rot.json" and out["event_kind"] == "key.rotated"
    assert _lines() == before and _bound(x3) is None             # 짓기만 했다
    # 다른 기기에서: 봉투와 씨앗만 있으면 된다
    rc, sig, _ = run(capsys, "sign", "--key", "x1.seed", "--envelope", "rot.json")
    assert rc == 0 and sig["pubkey"] == x1 and sig["envelope_sha256"] == out["envelope_sha256"]
    # 다시 서비스에서: 서명을 넣는다. 공개키는 원장의 결속에서 나온다
    rc, r, _ = run(capsys, "admit", "--dir", "hub", "--envelope", "rot.json", "--sig", sig["sig"])
    assert rc == 0 and r["admitted"] and _bound(x3)["key_id"] == "k3"
    # 같은 길로 k2가 서명한 수명 기록을 넣으려 하면 고정에 걸린다
    x4 = _keygen(capsys, "x4")
    rc, out, _ = run(capsys, "rotate-key", "--dir", "hub", "--signer", X, "--epoch", "1",
                     "--new-key-id", "k4", "--new-epoch", "1", "--new-pubkey", x4,
                     "--key-id", "k2", "--unsigned-out", "rot2.json")
    assert rc == 0
    sig2 = run(capsys, "sign", "--key", "x2.seed", "--envelope", "rot2.json")[1]
    rc, r, _ = run(capsys, "admit", "--dir", "hub", "--envelope", "rot2.json", "--sig", sig2["sig"])
    assert rc == 1 and "lifecycle pin" in r["problems"][0] and _bound(x4) is None
    # 폐기의 봉투도 같은 길로 낸다
    rc, out, _ = run(capsys, "revoke-key", "--dir", "hub", "--signer", X, "--key-id", "k1",
                     "--epoch", "1", "--revoke-key-id", "k2", "--revoke-epoch", "1",
                     "--unsigned-out", "rev.json")
    assert rc == 0 and out["event_kind"] == "key.revoked"
    sig3 = run(capsys, "sign", "--key", "x1.seed", "--envelope", "rev.json")[1]
    assert run(capsys, "admit", "--dir", "hub", "--envelope", "rev.json", "--sig", sig3["sig"])[1]["admitted"]
    assert _bound(x2)["revoked"] is True
    # 둘 다 주거나 둘 다 안 주면 거절이다. 있는 파일은 덮어쓰지 않는다
    assert run(capsys, *base, "--key-id", "k1")[0] == 2
    assert run(capsys, *base, "--key-id", "k1", "--key", "x1.seed", "--unsigned-out", "z.json")[0] == 2
    assert run(capsys, *base, "--key-id", "k1", "--unsigned-out", "rot.json")[0] == 2
