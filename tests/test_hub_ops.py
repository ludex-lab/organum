"""hub_ops 추출(0.6.0, Orin 040 §1) — 구판 대비 byte-identical + export 결함 수정.

골든 `tests/fixtures/hub_ops_golden.json`은 **구판 0.5.1(5bd9bcc)** 이 만든 것이다
(tests/hub_ops_scenario.py 머리말). 같은 새 함수를 두 번 부르는 검사는 회귀 증거가
아니므로, 여기서는 신판이 같은 시나리오로 낸 stdout·stderr·exit code·quad bytes·
원장 bytes를 구판 산출과 대조한다."""

import json
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "src"))
sys.path.insert(0, str(ROOT))
from organum import hub_cli as cli  # noqa: E402
from organum import hub_ops as ho  # noqa: E402
import hub_ops_scenario as scenario  # noqa: E402

GOLDEN = ROOT / "fixtures" / "hub_ops_golden.json"


def test_구판_골든과_byte_identical(tmp_path):
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    cwd = os.getcwd()
    try:
        got = scenario.run_scenario(tmp_path / "w")
    finally:
        os.chdir(cwd)
    assert len(got["steps"]) == len(golden["steps"])
    for i, (g, n) in enumerate(zip(golden["steps"], got["steps"])):
        assert n["argv"] == g["argv"], i
        assert n["rc"] == g["rc"], (i, g["argv"][0], n["stderr"])
        assert n["stdout"] == g["stdout"], (i, g["argv"][0])
        assert n["stderr"] == g["stderr"], (i, g["argv"][0])
    # quad bytes + 원장 bytes(sha256). **명시적 동작 변경 하나**(040 §1 "재현된 기존
    # 결함 수정은 명시 기록"): 구판은 `export --body nope.md`에서 envelope·sig를 먼저 쓰고
    # 나서 body 부재를 알려 **미완성 quad 004가 남았다**(마지막 admitted 이벤트=revoke가
    # 우연히 export됨). 신판은 쓰기 전에 거부한다 — 출력·exit는 같고 부작용만 사라진다.
    KNOWN_DELTA = {"out/004-envelope.json", "out/004-sig.txt"}
    assert KNOWN_DELTA <= set(golden["files"]) and not (KNOWN_DELTA & set(got["files"]))
    assert {k: v for k, v in got["files"].items()} == \
        {k: v for k, v in golden["files"].items() if k not in KNOWN_DELTA}


def test_골든은_실패_경로를_실제로_덮는다():
    """시나리오가 040 §1이 열거한 경로를 실제로 밟았는지 — 성공만 대조하는 골든은 약하다."""
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    rcs = [s["rc"] for s in golden["steps"]]
    assert rcs.count(0) >= 12 and rcs.count(1) >= 3 and rcs.count(2) >= 5
    text = "\n".join(s["stdout"] + s["stderr"] for s in golden["steps"])
    for needle in ('"duplicate": true',                 # 같은 메시지 재시도 수렴
                   '"valid_signature": false',           # 잘못된 서명
                   '"body_sha256_match": false',         # 본문 hash 불일치
                   "registry 결속과 다르다",               # 명시키 불일치
                   "결속이 없다",                          # 미등록 키 TOFU 요구
                   "direct-path admitted 이벤트가 아님"):  # export event-id 오류
        assert needle in text, needle
    # 폐기 키의 과거 봉투 감사: 서명은 참(rc 0)이면서 lifecycle에 폐기 좌표가 찍힌다
    audit = golden["steps"][19]
    assert audit["rc"] == 0 and re.search(r'"key_revoked_at_seq": \d+', audit["stdout"])


# ── export 결함 수정(040 §4): 번호 3~6자리 전체 파싱·덮어쓰기 금지·범위 초과 ────

def _hub_with_two_messages(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    seed = bytes([5]) * 32
    (tmp_path / "k.seed").write_bytes(seed.hex().encode())
    from organum import schnorr_pure as sp
    assert cli.main(["init", "--dir", "hub", "--source-domain", "lab:x/hub"]) == 0
    assert cli.main(["register-key", "--dir", "hub", "--signer", "lab:x",
                     "--key-id", "k1", "--epoch", "1",
                     "--pubkey", sp.public_key(seed).hex()]) == 0
    ids = []
    for i in (1, 2):
        (tmp_path / f"b{i}.json").write_bytes(json.dumps({"n": i}).encode())
        assert cli.main(["message", "--dir", "hub", "--key", "k.seed",
                         "--signer", "lab:x", "--key-id", "k1", "--epoch", "1",
                         "--to-lab", "lab:y", "--to-id", "Y", "--to-epoch", "1",
                         "--body-file", f"b{i}.json",
                         "--media-type", "application/json"]) == 0
    d, cfg, hub = ho.load_hub("hub")
    lines = (tmp_path / "hub" / "events.jsonl").read_text().splitlines()
    from organum import hub_envelope as he
    for l in lines:
        ids.append(he.event_id_of(json.loads(l)["raw"].encode()))
    return d, ids


def test_export_999_다음은_1000_1001이고_덮어쓰지_않는다(tmp_path, monkeypatch):
    d, ids = _hub_with_two_messages(tmp_path, monkeypatch)
    out = tmp_path / "out"
    out.mkdir()
    (out / "999-envelope.json").write_bytes(b"{}")
    r1 = ho.export_quad(d, out, event_id=ids[0], body_path="b1.json")
    r2 = ho.export_quad(d, out, event_id=ids[1], body_path="b2.json")
    assert (r1["nnn"], r2["nnn"]) == ("1000", "1001")
    e1 = (out / "1000-envelope.json").read_bytes()
    e2 = (out / "1001-envelope.json").read_bytes()
    assert e1 != e2 and (out / "999-envelope.json").read_bytes() == b"{}"
    assert (out / "1000-body.json").read_bytes() == b'{"n": 1}'
    assert ho.quad_number("1001-sig.txt") == 1001 and ho.quad_number("x-1.txt") is None
    assert ho.quad_number("12-envelope.json") is None        # 3자리 미만은 quad 아님


def test_export_번호_충돌은_명시_거부(tmp_path, monkeypatch):
    d, ids = _hub_with_two_messages(tmp_path, monkeypatch)
    out = tmp_path / "out"
    out.mkdir()
    ho.export_quad(d, out, event_id=ids[0])
    # 동시 export 경합: 두 프로세스가 같은 번호를 계산한 뒤 한쪽이 sig를 먼저 썼다.
    # 번호 계산은 monkeypatch로 고정(경합 재현) — 뒤쪽은 O_EXCL에서 명시 거부, 덮어쓰기 0.
    (out / "002-sig.txt").write_bytes(b"stale\n")
    orig = ho.next_quad_number
    monkeypatch.setattr(ho, "next_quad_number", lambda out_dir: "002")
    with pytest.raises(ho.HubOpsError, match="덮어쓰지 않는다"):
        ho.export_quad(d, out, event_id=ids[1])
    assert (out / "002-sig.txt").read_bytes() == b"stale\n"
    assert not (out / "002-envelope.json").exists()
    # 정상 경로: 반쯤 남은 002는 사용된 번호로 세어 003으로 간다(번호 재사용 없음)
    monkeypatch.setattr(ho, "next_quad_number", orig)
    r = ho.export_quad(d, out, event_id=ids[1])
    assert r["nnn"] == "003"


def test_export_범위_초과는_오류(tmp_path, monkeypatch):
    d, ids = _hub_with_two_messages(tmp_path, monkeypatch)
    out = tmp_path / "out"
    out.mkdir()
    (out / "999999-envelope.json").write_bytes(b"{}")
    with pytest.raises(ho.HubOpsError, match="범위 초과"):
        ho.export_quad(d, out, event_id=ids[0])
    assert sorted(p.name for p in out.iterdir()) == ["999999-envelope.json"]


def test_export_body_없음은_쓰기_전에_거부(tmp_path, monkeypatch):
    """구판은 envelope·sig를 쓴 뒤 body 부재를 알렸다(미완성 quad 잔류). 신판은 쓰기 전에
    거부한다 — 오류 문구·exit는 같고 부작용만 사라진다(명시적 동작 변경)."""
    d, ids = _hub_with_two_messages(tmp_path, monkeypatch)
    out = tmp_path / "out"
    with pytest.raises(ho.HubOpsError, match="body 파일 없음"):
        ho.export_quad(d, out, event_id=ids[0], body_path="nope.md")
    assert not out.exists() or not any(out.iterdir())


def test_빌더는_순수하고_쓰기는_sign_and_admit뿐(tmp_path, monkeypatch):
    d, ids = _hub_with_two_messages(tmp_path, monkeypatch)
    d, cfg, hub = ho.load_hub("hub")
    before = (tmp_path / "hub" / "events.jsonl").read_bytes()
    env = ho.build_message_envelope(cfg, signer="lab:x", key_id="k1", epoch=1,
                                    to_lab="lab:y", to_id="Y", to_epoch=1,
                                    body=b"x", body_locator="file://x",
                                    media_type="text/plain",
                                    created_at="2026-01-01T00:00:00Z")
    assert env["created_at"] == "2026-01-01T00:00:00Z"
    assert (tmp_path / "hub" / "events.jsonl").read_bytes() == before   # 빌더는 무접촉
    r = ho.sign_and_admit(d, cfg, hub, env, bytes([5]) * 32)
    assert r["admitted"] and not r["duplicate"]
    assert (tmp_path / "hub" / "events.jsonl").read_bytes() != before
    again = ho.sign_and_admit(d, cfg, hub, env, bytes([5]) * 32)
    assert again["duplicate"] and again["event_id"] == r["event_id"]


def test_display_정규화는_JSON_이스케이프_쌍만_바꾼다():
    """Ray 102 F2b: JSON 텍스트의 Windows 경로는 이스케이프 쌍 — 단일 치환은 '//'를 만든다."""
    j = '{"exported": ["out\\\\001-envelope.json"], "note": "a\\nb \\"q\\""}'
    assert scenario.display_json(j, sep="\\") == \
        '{"exported": ["out/001-envelope.json"], "note": "a\\nb \\"q\\""}'
    assert scenario.display_json(j, sep="/") == j                      # POSIX는 identity
    assert scenario.display_text("organum-hub: 없음: out\\x", sep="\\") == "organum-hub: 없음: out/x"
    assert scenario.display_text("no sep here", sep="\\") == "no sep here"
