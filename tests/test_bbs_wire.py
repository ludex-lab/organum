"""organum bbs 0.6.0 게시판 클라이언트 — 로컬 드롭 서버 위 왕복 + Orin 040 반례.

합성이 아니다: 실제 drop 서버(make_server)·실제 hub 원장·실제 서명으로 post → pull →
read를 돈다. 서브프로세스 0(037의 B 슬롯 조건). experiments/bbs-v0/test_board_wire의
왕복 시험은 여기로 이주했다."""

import base64
import hashlib
import json
import os
import sys
import threading
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from organum import bbs  # noqa: E402
from organum import bbs_cli  # noqa: E402
from organum import bbs_wire as bw  # noqa: E402
from organum import hub_cli as cli  # noqa: E402
from organum import hub_drop as hd  # noqa: E402
from organum import hub_ops as ho  # noqa: E402
from organum import schnorr_pure as sp  # noqa: E402

SEEDS = {"naru": bytes([21]) * 32, "organum": bytes([22]) * 32, "evil": bytes([23]) * 32}
TOKEN = "secret-t0ken"
T = 5                                                 # 로컬 서버 timeout


@pytest.fixture
def drop(tmp_path):
    tok = tmp_path / "tokens.txt"
    tok.write_text(f"# t\n{TOKEN}\n", encoding="utf-8")
    root = tmp_path / "drops"
    srv = hd.make_server(root, tok, bind="127.0.0.1", port=0, rate_limit_per_minute=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield (f"http://127.0.0.1:{srv.server_address[1]}", tok, root)
    srv.shutdown()


def _lab(tmp_path, name, register=()):
    """랩 하나: 고정 seed + hub init + 자기 키(+남의 키) bootstrap 등록. (home, hub_dir, seed_path)"""
    home = tmp_path / f"{name}-home"
    home.mkdir()
    seed_p = home / f"{name}.seed"
    seed_p.write_bytes(SEEDS[name].hex().encode())
    seed_p.chmod(0o600)
    hub = home / "hub"
    assert cli.main(["init", "--dir", str(hub), "--source-domain", f"lab:{name}/hub"]) == 0
    for other in (name, *register):
        assert cli.main(["register-key", "--dir", str(hub), "--signer", f"lab:{other}",
                         "--key-id", "k1", "--epoch", "1",
                         "--pubkey", sp.public_key(SEEDS[other]).hex()]) == 0
    return home, hub, seed_p


CARE = {"lab": "lab:naru", "id": "Cody"}
GUEST = {"lab": "lab:organum", "id": "Cody"}


def _post(home, hub, seed_p, name, url, tok, board, ev, **kw):
    rec = bw.prepare_post(hub_dir=hub, seed_path=seed_p, signer=f"lab:{name}",
                          key_id="k1", epoch=1, board=board, event=ev, url_base=url,
                          to_lab="lab:naru", to_id="board", outbox=home / "outbox",
                          created_at="2026-09-12T00:00:00Z", **kw)
    return bw.push_outbox(home / "outbox", board, rec["n"], TOKEN, timeout=T, warmup=False)


def _raw_push(home, hub, seed_p, name, url, board, body_obj, *, n_dir="outbox",
              door=None, media="application/json", body_bytes=None):
    """클라이언트를 우회한 '손 push' — 반례 주입용(서명은 진짜, 내용만 계약 밖)."""
    d, cfg, hubx = ho.load_hub(hub)
    body = body_bytes if body_bytes is not None else bw.event_body_bytes(body_obj)
    env = ho.build_message_envelope(cfg, signer=f"lab:{name}", key_id="k1", epoch=1,
                                    to_lab="lab:naru", to_id="board", to_epoch=1,
                                    body=body, body_locator="file://x.json",
                                    media_type=media, created_at="2026-09-12T00:00:00Z")
    r = ho.sign_and_admit(d, cfg, hubx, env, SEEDS[name])
    assert r["admitted"]
    out = bw.outbox_dir(home / n_dir, board)          # prepare_post와 번호를 같이 센다
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / ".b"
    tmp.write_bytes(body)
    exp = ho.export_quad(d, out, event_id=r["event_id"], body_path=tmp)
    tmp.unlink()
    door = door or f"from-{name}"
    return hd.push_quad(f"{url}/v0/{board}/{door}", TOKEN, out / exp["nnn"],
                        timeout=T, warmup=False, allow_foreign_door=True)


def _seed_board(tmp_path, url, tok, board="bbs-t"):
    host = _lab(tmp_path, "naru", register=("organum",))
    guest = _lab(tmp_path, "organum")
    for ev in [
        {"kind": "board.created", "board": board, "caretaker": CARE, "at": "t01"},
        {"kind": "board.member.admitted", "board": board, "member": CARE,
         "acting_agent": CARE, "at": "t02"},
        {"kind": "board.member.admitted", "board": board, "member": GUEST,
         "acting_agent": CARE, "at": "t03"},
        {"kind": "board.post", "board": board, "post_id": "p1", "author": CARE,
         "reply_to": None, "at": "t10", "text": "회차 개시"},
    ]:
        assert _post(*host, "naru", url, tok, board, ev)["status"] == "stored"
    ok = {"kind": "board.post", "board": board, "post_id": "p2", "author": GUEST,
          "reply_to": "p1", "at": "t11", "text": "칸 1 제출"}
    assert _post(*guest, "organum", url, tok, board, ok)["status"] == "stored"
    return host, guest


# ── 왕복 ─────────────────────────────────────────────────────────────────────

def test_왕복_post_pull_read가_project_board와_같다(drop, tmp_path):
    url, tok, root = drop
    host, guest = _seed_board(tmp_path, url, tok)
    # §3 반례: 다른 board 좌표로 서명된 글을 bbs-t 문에 '손으로' 밀어 넣는다(운반은 dumb)
    smug = {"kind": "board.post", "board": "other", "post_id": "px", "author": GUEST,
            "signer_lab": "lab:organum", "reply_to": None, "at": "t12", "text": "x"}
    assert _raw_push(*guest, "organum", url, "bbs-t", smug)["stored"]
    tree = tmp_path / "tree"
    rnd = bw.pull_channel(url, TOKEN, "bbs-t", tree, timeout=T, warmup=False,
                          round_at="2026-09-12T01:00:00Z")
    assert rnd["planned"] == ["from-naru", "from-organum"] and not rnd["untried"]
    assert all(r["ok"] for r in rnd["results"].values())
    assert rnd["results"]["from-naru"]["pulled"] == ["001", "002", "003", "004"]
    assert rnd["results"]["from-naru"]["last_complete"] == "004"
    st = bw.read_board(tree, "bbs-t", ho.load_hub(host[1])[2])
    assert [p["post_id"] for p in st["posts"]] == ["p1", "p2"]
    assert st["posts"][1]["text"] == "칸 1 제출"                      # 본문-bearing
    assert st["posts"][1]["provenance"]["door"] == "from-organum"
    assert st["posts"][1]["provenance"]["signer"] == "lab:organum"
    assert st["posts"][1]["sort_key"] == ["t11", "lab:organum", 1]
    assert st["threads"] == {"p1": ["p2"]}
    assert st["members"] == [["lab:naru", "Cody"], ["lab:organum", "Cody"]]
    # 040 §2B: 다른 board 좌표는 reducer에 넘기지 않는다 — 전송 문제로 남고 상태 무영향
    assert st["rejected"] == [] and st["rejected_count"] == 0
    assert any("040 §2B" in p["why"] for p in st["transport_problems"])
    assert st["transport_problem_count"] == 1
    assert st["round"]["round_at"] == "2026-09-12T01:00:00Z"
    # read 결과 == 같은 이벤트의 project_board(037: CLI=core 같은 함수)
    loaded = bw.load_channel(tree, "bbs-t", ho.load_hub(host[1])[2])
    direct = bbs.project_board(loaded["events"])
    assert [p["post_id"] for p in direct["posts"]] == [p["post_id"] for p in st["posts"]]


def test_read는_결정적이고_원장을_건드리지_않는다(drop, tmp_path):
    url, tok, root = drop
    host, guest = _seed_board(tmp_path, url, tok)
    tree = tmp_path / "tree"
    bw.pull_channel(url, TOKEN, "bbs-t", tree, timeout=T, warmup=False, round_at="r1")
    ledger = (host[1] / "events.jsonl").read_bytes()
    hub = ho.load_hub(host[1])[2]
    a = json.dumps(bw.read_board(tree, "bbs-t", hub), sort_keys=True, ensure_ascii=False)
    b = json.dumps(bw.read_board(tree, "bbs-t", hub), sort_keys=True, ensure_ascii=False)
    assert a == b and (host[1] / "events.jsonl").read_bytes() == ledger
    assert "2026-" not in a.replace("2026-09-12T00:00:00Z", "")   # read가 시각을 넣지 않는다


# ── Orin 040 §2 반례 ─────────────────────────────────────────────────────────

def test_A_body_signer_lab이_검증된_서명자와_다르면_배제(drop, tmp_path):
    url, tok, root = drop
    host, guest = _seed_board(tmp_path, url, tok)
    forged = {"kind": "board.member.admitted", "board": "bbs-t", "at": "t04",
              "member": {"lab": "lab:organum", "id": "Mole"},
              "acting_agent": CARE, "signer_lab": "lab:naru"}   # 진짜 서명은 organum
    assert _raw_push(*guest, "organum", url, "bbs-t", forged)["stored"]
    tree = tmp_path / "tree"
    bw.pull_channel(url, TOKEN, "bbs-t", tree, timeout=T, warmup=False)
    st = bw.read_board(tree, "bbs-t", ho.load_hub(host[1])[2])
    assert ["lab:organum", "Mole"] not in st["members"]
    assert any("040 §2A" in p["why"] for p in st["transport_problems"])
    # signer_lab 없는 body는 검증된 서명자로 채워진다(투영용 provenance는 envelope에서)
    assert all(p["signer_lab"] == p["provenance"]["signer"] for p in st["posts"])


def test_B_다른_board의_closed가_이_게시판을_닫지_못한다(drop, tmp_path):
    url, tok, root = drop
    host, guest = _seed_board(tmp_path, url, tok)
    assert _raw_push(*host, "naru", url, "bbs-t",
                     {"kind": "board.closed", "board": "other", "at": "t05"})["stored"]
    tree = tmp_path / "tree"
    bw.pull_channel(url, TOKEN, "bbs-t", tree, timeout=T, warmup=False)
    st = bw.read_board(tree, "bbs-t", ho.load_hub(host[1])[2])
    assert st["status"] == "active" and [p["post_id"] for p in st["posts"]] == ["p1", "p2"]
    assert any("board.closed" in p["why"] or "040 §2B" in p["why"]
               for p in st["transport_problems"])


def test_C_한_봉투의_실패는_격리되고_나머지는_보인다(drop, tmp_path):
    url, tok, root = drop
    host, guest = _seed_board(tmp_path, url, tok)
    # 유효 서명 + JSON 배열 body / JSON 아님 / at 없음 — 셋 다 quad별 문제로만 남는다
    assert _raw_push(*guest, "organum", url, "bbs-t", None, body_bytes=b"[]")["stored"]
    assert _raw_push(*guest, "organum", url, "bbs-t", None, body_bytes=b"# md\n",
                     media="text/markdown")["stored"]
    assert _raw_push(*guest, "organum", url, "bbs-t",
                     {"kind": "board.post", "board": "bbs-t", "post_id": "p9",
                      "author": GUEST, "reply_to": None, "text": "no at"})["stored"]
    tree = tmp_path / "tree"
    bw.pull_channel(url, TOKEN, "bbs-t", tree, timeout=T, warmup=False)
    st = bw.read_board(tree, "bbs-t", ho.load_hub(host[1])[2])
    assert [p["post_id"] for p in st["posts"]] == ["p1", "p2"]
    whys = " | ".join(p["why"] for p in st["transport_problems"])
    assert "JSON object가 아님" in whys and "JSON이 아님" in whys and "at이 없거나" in whys
    assert st["transport_problem_count"] == 3


def test_서명_훼손과_미등록_키는_전송_문제로_남고_원문은_보존(drop, tmp_path):
    url, tok, root = drop
    host, guest = _seed_board(tmp_path, url, tok)
    evil = _lab(tmp_path, "evil")
    assert _raw_push(*evil, "evil", url, "bbs-t",
                     {"kind": "board.post", "board": "bbs-t", "post_id": "pe",
                      "author": {"lab": "lab:evil", "id": "E"}, "reply_to": None,
                      "at": "t20", "text": "unregistered"})["stored"]
    tree = tmp_path / "tree"
    bw.pull_channel(url, TOKEN, "bbs-t", tree, timeout=T, warmup=False)
    sig_p = tree / "bbs-t" / "from-organum" / "001-sig.txt"
    good = sig_p.read_text().strip()
    sig_p.write_text(("f" if good[0] != "f" else "0") + good[1:] + "\n")
    st = bw.read_board(tree, "bbs-t", ho.load_hub(host[1])[2])
    assert [p["post_id"] for p in st["posts"]] == ["p1"]
    whys = " | ".join(p["why"] for p in st["transport_problems"])
    assert "서명 무효" in whys and "검증 불가" in whys
    assert (tree / "bbs-t" / "from-organum" / "001-body.json").exists()   # 지우지 않는다


# ── §3 since·정렬 ────────────────────────────────────────────────────────────

def test_since는_상태_계산_뒤_표시만_좁힌다(drop, tmp_path):
    url, tok, root = drop
    host, guest = _seed_board(tmp_path, url, tok)
    tree = tmp_path / "tree"
    bw.pull_channel(url, TOKEN, "bbs-t", tree, timeout=T, warmup=False)
    hub = ho.load_hub(host[1])[2]
    st = bw.read_board(tree, "bbs-t", hub, since="t10")
    assert [p["post_id"] for p in st["posts"]] == ["p2"]        # 표시만 좁힘
    assert st["post_count_total"] == 2 and st["rejected_count"] == 0
    assert len(st["members"]) == 2                              # 가입 이력 유지
    st2 = bw.read_board(tree, "bbs-t", hub, after="lab:naru:4")
    assert [p["post_id"] for p in st2["posts"]] == ["p2"]
    with pytest.raises(bw.BbsWireError, match="lab:<name>:<n>"):
        bw.read_board(tree, "bbs-t", hub, after="nonsense")


def test_정렬은_at_lab_n이고_같은_문에서_at_역행은_n을_뒤집는다(drop, tmp_path):
    url, tok, root = drop
    host = _lab(tmp_path, "naru")
    for ev in [{"kind": "board.created", "board": "b", "caretaker": CARE, "at": "t02"},
               {"kind": "board.metadata", "board": "b", "at": "t01", "name": "earlier"}]:
        assert _post(*host, "naru", url, tok, "b", ev)["status"] == "stored"
    tree = tmp_path / "tree"
    bw.pull_channel(url, TOKEN, "b", tree, timeout=T, warmup=False)
    loaded = bw.load_channel(tree, "b", ho.load_hub(host[1])[2])
    assert [r["n"] for r in loaded["records"]] == ["002", "001"]   # n 순서가 뒤집힌다
    assert loaded["sort_rule"] == "(at, lab, n)"


# ── §4 회차·재개·재전송 ──────────────────────────────────────────────────────

def test_미완성_quad는_완결로_세지_않고_재개가_빈자리를_채운다(drop, tmp_path):
    url, tok, root = drop
    host, guest = _seed_board(tmp_path, url, tok)
    tree = tmp_path / "tree"
    bw.pull_channel(url, TOKEN, "bbs-t", tree, timeout=T, warmup=False)
    door = tree / "bbs-t" / "from-naru"
    (door / "002-envelope.json").unlink()                  # 페이지 중간 실패 흉내
    last, inc = bw.door_completeness(door)
    assert last == "001" and inc == [{"n": "002", "why": inc[0]["why"]}]
    assert bw.resume_point(door) == "001"
    st = bw.read_board(tree, "bbs-t", ho.load_hub(host[1])[2])
    assert any(p["n"] == "002" and "미완성" in p["why"] for p in st["transport_problems"])
    rnd = bw.pull_channel(url, TOKEN, "bbs-t", tree, timeout=T, warmup=False)
    assert rnd["results"]["from-naru"]["since"] == "001"
    assert (door / "002-envelope.json").exists()
    assert rnd["results"]["from-naru"]["last_complete"] == "004"
    assert rnd["results"]["from-naru"]["incomplete"] == []
    assert rnd["pages_total"] >= 2


def test_한_문의_실패가_다른_문을_막지_않고_회차에_남는다(drop, tmp_path, monkeypatch):
    url, tok, root = drop
    host, guest = _seed_board(tmp_path, url, tok)
    tree = tmp_path / "tree"
    real = hd.pull_quads

    def flaky(u, *a, **k):
        if u.endswith("/from-naru"):
            raise urllib.error.URLError("simulated outage")
        return real(u, *a, **k)
    monkeypatch.setattr(hd, "pull_quads", flaky)
    rnd = bw.pull_channel(url, TOKEN, "bbs-t", tree, timeout=T, warmup=False)
    assert rnd["results"]["from-naru"]["ok"] is False
    assert "URLError" in rnd["results"]["from-naru"]["error"]
    assert rnd["results"]["from-organum"]["ok"] is True
    assert json.loads((tree / "bbs-t" / ".round.json").read_bytes())["results"]["from-naru"]["ok"] is False
    assert len((tree / "bbs-t" / ".rounds.jsonl").read_bytes().splitlines()) == 1


def test_post_응답_유실은_unknown_재시도는_같은_bytes로_수렴(drop, tmp_path, monkeypatch):
    url, tok, root = drop
    host = _lab(tmp_path, "naru")
    ev = {"kind": "board.created", "board": "z", "caretaker": CARE, "at": "t01"}
    rec = bw.prepare_post(hub_dir=host[1], seed_path=host[2], signer="lab:naru",
                          key_id="k1", epoch=1, board="z", event=ev, url_base=url,
                          to_lab="lab:naru", outbox=host[0] / "outbox",
                          created_at="2026-09-12T00:00:00Z")
    assert rec["status"] == "pending" and rec["n"] == "001"
    ob = host[0] / "outbox" / "out-z"
    env_bytes = (ob / "001-envelope.json").read_bytes()
    real = hd.push_quad

    def drop_response(*a, **k):
        real(*a, **k)                                   # 서버는 저장했지만 응답이 유실
        raise urllib.error.URLError("timed out")
    monkeypatch.setattr(hd, "push_quad", drop_response)
    r = bw.push_outbox(host[0] / "outbox", "z", "001", TOKEN, timeout=T, warmup=False,
                       attempt_at="a1")
    assert r["status"] == "unknown" and r["retry"]["n"] == "001"
    monkeypatch.setattr(hd, "push_quad", real)
    r2 = bw.push_outbox(host[0] / "outbox", "z", "001", TOKEN, timeout=T, warmup=False,
                        attempt_at="a2")
    assert r2["status"] == "stored" and r2["dedup"] is True and r2["attempts"] == 2
    assert (ob / "001-envelope.json").read_bytes() == env_bytes
    # 같은 이벤트로 prepare_post 재호출 → 새 message를 만들지 않고 같은 quad
    again = bw.prepare_post(hub_dir=host[1], seed_path=host[2], signer="lab:naru",
                            key_id="k1", epoch=1, board="z", event=ev, url_base=url,
                            to_lab="lab:naru", outbox=host[0] / "outbox",
                            created_at="2026-09-12T00:00:00Z")
    assert again["n"] == "001" and again["event_id"] == rec["event_id"]
    assert sorted(p.name for p in ob.iterdir()) == [
        "001-body.json", "001-envelope.json", "001-sig.txt", "001.outbox.json"]


def test_post_충돌은_자동_우회하지_않는다(drop, tmp_path):
    url, tok, root = drop
    host = _lab(tmp_path, "naru")
    # 서버의 from-naru/001 자리에 먼저 다른 bytes를 앉힌다
    assert _raw_push(*host, "naru", url, "z",
                     {"kind": "board.metadata", "board": "z", "at": "t00", "name": "first"})["stored"]
    ev = {"kind": "board.created", "board": "z", "caretaker": CARE, "at": "t01"}
    rec = bw.prepare_post(hub_dir=host[1], seed_path=host[2], signer="lab:naru",
                          key_id="k1", epoch=1, board="z", event=ev, url_base=url,
                          to_lab="lab:naru", outbox=host[0] / "outbox2",
                          created_at="2026-09-12T00:00:00Z")
    r = bw.push_outbox(host[0] / "outbox2", "z", rec["n"], TOKEN, timeout=T, warmup=False)
    assert r["status"] == "conflict" and "409" in r["error"]


def test_post는_계약_위반을_서명_전에_거부한다(drop, tmp_path):
    url, tok, root = drop
    host = _lab(tmp_path, "naru")
    before = (host[1] / "events.jsonl").read_bytes()
    with pytest.raises(bw.BbsWireError, match="board 좌표"):
        bw.prepare_post(hub_dir=host[1], seed_path=host[2], signer="lab:naru",
                        key_id="k1", epoch=1, board="z", url_base=url, to_lab="lab:naru",
                        outbox=host[0] / "o",
                        event={"kind": "board.post", "board": "other", "post_id": "p",
                               "author": CARE, "at": "t1"})
    with pytest.raises(bw.BbsWireError, match="계약 위반"):
        bw.prepare_post(hub_dir=host[1], seed_path=host[2], signer="lab:naru",
                        key_id="k1", epoch=1, board="z", url_base=url, to_lab="lab:naru",
                        outbox=host[0] / "o",
                        event={"kind": "board.member.admitted", "board": "z",
                               "member": CARE, "at": "t1"})       # acting_agent 없음
    with pytest.raises(bw.BbsWireError, match="signer_lab"):
        bw.prepare_post(hub_dir=host[1], seed_path=host[2], signer="lab:naru",
                        key_id="k1", epoch=1, board="z", url_base=url, to_lab="lab:naru",
                        outbox=host[0] / "o",
                        event={"kind": "board.created", "board": "z", "caretaker": CARE,
                               "at": "t1", "signer_lab": "lab:other"})
    assert (host[1] / "events.jsonl").read_bytes() == before      # 서명 0


# ── §4 발견 ──────────────────────────────────────────────────────────────────

def test_boards_종류는_내용이_정하고_확정하지_않는다(drop, tmp_path):
    url, tok, root = drop
    host, guest = _seed_board(tmp_path, url, tok)
    # 전화번호부 채널: profile.announced / 봉투 레인: markdown / 상충: board+profile
    prof = {"kind": "profile.announced", "at": "t1", "subject": {"lab": "lab:naru", "id": "Cody"},
            "author": {"lab": "lab:naru", "id": "Cody"}, "profile": {"kind": "creature"}}
    assert _raw_push(*host, "naru", url, "directory", prof)["stored"]
    assert _raw_push(*host, "naru", url, "hub-ops", None, body_bytes=b"# letter\n",
                     media="text/markdown")["stored"]
    assert _raw_push(*host, "naru", url, "mixed", prof)["stored"]
    assert _raw_push(*host, "naru", url, "mixed",
                     {"kind": "board.post", "board": "mixed", "post_id": "q", "author": CARE,
                      "at": "t2"})["stored"]
    st: dict = {}
    found = bw.discover(url, TOKEN, timeout=T, warmup=False, stats=st)
    assert found["bbs-t"]["kind"] == "board" and found["bbs-t"]["inferred"] is True
    assert found["bbs-t"]["doors"] == ["from-naru", "from-organum"]
    assert found["directory"]["kind"] == "directory"
    assert found["hub-ops"]["kind"] == "unknown" and found["hub-ops"]["non_contract"] == 1
    assert found["mixed"]["kind"] == "ambiguous"
    assert st["probe_gets"] == 5                                  # 문마다 1, 따로 계수
    off = bw.discover(url, TOKEN, timeout=T, warmup=False, probe=False)
    assert all(v["kind"] == "unknown" for v in off.values())


def test_directory_read는_compiled_at을_호출자가_준다(drop, tmp_path):
    url, tok, root = drop
    host = _lab(tmp_path, "naru")
    prof = {"kind": "profile.announced", "at": "t1", "subject": {"lab": "lab:naru", "id": "Cody"},
            "author": {"lab": "lab:naru", "id": "Cody"},
            "profile": {"kind": "creature", "display_name": "Cody"}}
    assert _post(*host, "naru", url, tok, "directory", prof)["status"] == "stored"
    tree = tmp_path / "tree"
    bw.pull_channel(url, TOKEN, "directory", tree, timeout=T, warmup=False)
    out = bw.read_directory(tree, "directory", ho.load_hub(host[1])[2], compiled_at="c1")
    assert out["row_count"] == 1 and out["rows"][0]["compiled_at"] == "c1"
    assert out["rows"][0]["subject_claimed"] is True
    assert "subject_authority_verified" not in out["rows"][0]      # claimed core


# ── CLI ──────────────────────────────────────────────────────────────────────

def _cli(capsys, *argv):
    capsys.readouterr()                                   # 앞선 hub CLI 출력 버림
    rc = bbs_cli.main(list(argv))
    cap = capsys.readouterr()
    return rc, (json.loads(cap.out) if cap.out.strip() else None), cap.err


def test_cli_pull_read_post_boards(drop, tmp_path, capsys):
    url, tok, root = drop
    host, guest = _seed_board(tmp_path, url, tok)
    tree = tmp_path / "tree"
    rc, out, _ = _cli(capsys, "pull", "bbs-t", "--url", url, "--token-file", str(tok),
                      "--tree", str(tree), "--no-warmup", "--timeout", str(T),
                      "--round-at", "r1")
    assert rc == 0 and out["planned"] == ["from-naru", "from-organum"]
    rc, out, _ = _cli(capsys, "read", "bbs-t", "--tree", str(tree), "--hub", str(host[1]))
    assert rc == 0 and [p["post_id"] for p in out["posts"]] == ["p1", "p2"]
    assert out["offline"] is True and out["round"]["round_at"] == "r1"
    # 전송 문제가 있으면 rc 1, --allow-problems로만 0(나루 112 조건 3)
    (tree / "bbs-t" / "from-organum" / "001-envelope.json").unlink()
    rc, out, _ = _cli(capsys, "read", "bbs-t", "--tree", str(tree), "--hub", str(host[1]))
    assert rc == 1 and out["transport_problem_count"] == 1
    rc, out, _ = _cli(capsys, "read", "bbs-t", "--tree", str(tree), "--hub", str(host[1]),
                      "--allow-problems")
    assert rc == 0
    # post via CLI(게스트 답글) → 서버 저장 → pull → read에 보인다
    ev = tmp_path / "ev.json"
    ev.write_text(json.dumps({"kind": "board.post", "board": "bbs-t", "post_id": "p3",
                              "author": GUEST, "reply_to": "p1", "at": "t13",
                              "text": "cli post"}), encoding="utf-8")
    rc, out, _ = _cli(capsys, "post", "bbs-t", "--url", url, "--token-file", str(tok),
                      "--outbox", str(guest[0] / "outbox"), "--event", str(ev),
                      "--hub", str(guest[1]), "--key", str(guest[2]),
                      "--signer", "lab:organum", "--key-id", "k1", "--to-lab", "lab:naru",
                      "--no-warmup", "--timeout", str(T))
    assert rc == 0 and out["status"] == "stored" and out["n"] == "002"
    rc, out, _ = _cli(capsys, "post", "bbs-t", "--url", url, "--token-file", str(tok),
                      "--outbox", str(guest[0] / "outbox"), "--retry", "002",
                      "--no-warmup", "--timeout", str(T))
    assert rc == 0 and out["dedup"] is True
    _cli(capsys, "pull", "bbs-t", "--url", url, "--token-file", str(tok),
         "--tree", str(tree), "--no-warmup", "--timeout", str(T))
    rc, out, _ = _cli(capsys, "read", "bbs-t", "--tree", str(tree), "--hub", str(host[1]))
    assert [p["post_id"] for p in out["posts"]] == ["p1", "p2", "p3"]
    rc, out, _ = _cli(capsys, "boards", "--url", url, "--token-file", str(tok),
                      "--no-warmup", "--timeout", str(T))
    assert rc == 0 and out["channels"]["bbs-t"]["kind"] == "board"
    # 잘못된 입력은 nonzero + stderr JSON
    rc, out, err = _cli(capsys, "read", "bbs-t", "--tree", str(tree), "--hub", "nohub")
    assert rc == 1 and out is None and "error" in err
    rc, out, err = _cli(capsys, "read", "directory", "--tree", str(tree),
                        "--hub", str(host[1]), "--as", "directory")
    assert rc == 1 and "compiled-at" in err


# ── Ray 101 F1 · 여울 085 — 090 후보 판정에서 온 회귀 ───────────────────────────

@pytest.mark.parametrize("suffix", ["body.json", "sig.txt"])
def test_F1_envelope만_남은_quad는_재수거가_동반_파일을_채운다(drop, tmp_path, suffix):
    """Ray 101 F1: envelope가 있으면 hub_drop.pull이 sig/body 쓰기를 건너뛰어 복구가 안 됐다.
    닫힘 기준 — 원래 bytes로 채우고 마지막 완결이 전진하며, 남은 파일은 덮어쓰지 않는다."""
    url, tok, root = drop
    _seed_board(tmp_path, url, tok)
    tree = tmp_path / "mirror"
    bw.pull_channel(url, TOKEN, "bbs-t", tree, timeout=T, warmup=False)
    door = tree / "bbs-t" / "from-naru"
    missing = door / ("001-" + suffix)
    original = missing.read_bytes()
    missing.unlink()
    assert (door / "001-envelope.json").is_file()
    assert bw.resume_point(door) == "000"
    result = bw.pull_channel(url, TOKEN, "bbs-t", tree, timeout=T, warmup=False)
    assert missing.is_file(), result
    assert missing.read_bytes() == original
    r = result["results"]["from-naru"]
    assert r["incomplete"] == [] and r["ok"] is True and r["repaired"] == 1
    assert r["last_complete"] == "004" and "001" in r["pulled"]


def test_F1_남은_파일이_서버와_다르면_명시_오류이고_회차는_성공이_아니다(drop, tmp_path, capsys):
    url, tok, root = drop
    host, _ = _seed_board(tmp_path, url, tok)
    tree = tmp_path / "mirror"
    bw.pull_channel(url, TOKEN, "bbs-t", tree, timeout=T, warmup=False)
    door = tree / "bbs-t" / "from-naru"
    (door / "001-body.json").unlink()                      # 복구 대상
    (door / "001-sig.txt").write_bytes(b"tampered\n")        # 남은 파일이 서버와 다름
    result = bw.pull_channel(url, TOKEN, "bbs-t", tree, timeout=T, warmup=False)
    r = result["results"]["from-naru"]
    assert r["ok"] is False and "덮어쓰지 않는다" in r["error"]
    assert (door / "001-sig.txt").read_bytes() == b"tampered\n"     # 손대지 않음
    assert any(i["n"] == "001" for i in r["incomplete"])
    # CLI: 복구가 안 된 회차는 성공 exit로 닫지 않는다
    capsys.readouterr()
    rc = bbs_cli.main(["pull", "bbs-t", "--url", url, "--token-file", str(tok),
                       "--tree", str(tree), "--no-warmup", "--timeout", str(T)])
    assert rc == 1


def test_여울1_실패한_갱신_뒤_read는_기본_exit_1(drop, tmp_path, monkeypatch, capsys):
    """여울 085 §1: 캐시 quad가 전부 정상이라 파일 검증 문제는 0인데 회차에 실패한 문이 있다.
    부분 갱신은 종료코드로 구별돼야 한다(--allow-problems로만 0)."""
    url, tok, root = drop
    host, _ = _seed_board(tmp_path, url, tok)
    tree = tmp_path / "tree"
    bw.pull_channel(url, TOKEN, "bbs-t", tree, timeout=T, warmup=False, round_at="complete")
    real = hd.pull_quads

    def fail_one(u, *a, **k):
        if u.endswith("/from-naru"):
            raise urllib.error.URLError("simulated refresh outage")
        return real(u, *a, **k)
    monkeypatch.setattr(hd, "pull_quads", fail_one)
    rnd = bw.pull_channel(url, TOKEN, "bbs-t", tree, timeout=T, warmup=False,
                          round_at="failed-refresh")
    assert rnd["results"]["from-naru"]["ok"] is False and rnd["status"] == "complete"
    rc, out, _ = _cli(capsys, "read", "bbs-t", "--tree", str(tree), "--hub", str(host[1]))
    assert rc == 1 and out["transport_problem_count"] == 0
    assert out["round_problem_count"] == 1 and out["round_problems"][0]["door"] == "from-naru"
    assert len(out["posts"]) == 2                            # 확보된 글은 계속 보인다
    rc, out, _ = _cli(capsys, "read", "bbs-t", "--tree", str(tree), "--hub", str(host[1]),
                      "--allow-problems")
    assert rc == 0


def test_여울2_회차_중단은_현재_계획과_미시도를_디스크에_남긴다(drop, tmp_path, monkeypatch):
    """여울 085 §2: 회차 파일은 첫 문 호출 전에 running으로 게시하고 문마다 갱신한다."""
    url, tok, root = drop
    _seed_board(tmp_path, url, tok)
    tree = tmp_path / "tree"
    bw.pull_channel(url, TOKEN, "bbs-t", tree, timeout=T, warmup=False,
                    round_at="previous-complete-round")
    real = hd.pull_quads

    def interrupt_second(u, *a, **k):
        if u.endswith("/from-organum"):
            raise KeyboardInterrupt("simulated operator interruption")
        return real(u, *a, **k)
    monkeypatch.setattr(hd, "pull_quads", interrupt_second)
    with pytest.raises(KeyboardInterrupt):
        bw.pull_channel(url, TOKEN, "bbs-t", tree, timeout=T, warmup=False,
                        round_at="current-interrupted-round")
    durable = bw.load_round(tree, "bbs-t")
    assert durable["round_at"] == "current-interrupted-round"
    assert durable["status"] == "running" and durable["untried"] == ["from-organum"]
    assert durable["results"]["from-naru"]["ok"] is True
    assert not (tree / "bbs-t" / ".round.json.tmp").exists()          # 원자 교체
    # 완료된 회차만 이력에 남는다(이전 완료 1줄), 중단 회차는 .round.json에만
    assert len((tree / "bbs-t" / ".rounds.jsonl").read_bytes().splitlines()) == 1
    assert bw.round_problems(durable)[0]["why"].startswith("회차가 닫히지 않음")


def test_채널_발견_실패는_예정_문_미확정으로_기록(drop, tmp_path, monkeypatch):
    url, tok, root = drop
    tree = tmp_path / "tree"
    monkeypatch.setattr(hd, "list_channels",
                        lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("dark")))
    with pytest.raises(bw.BbsWireError, match="예정 문 미확정"):
        bw.pull_channel(url, TOKEN, "bbs-t", tree, timeout=T, warmup=False, round_at="r")
    rnd = bw.load_round(tree, "bbs-t")
    assert rnd["status"] == "discovery-failed" and rnd["planned"] is None
    assert bw.round_problems(rnd)[0]["why"].startswith("회차가 닫히지 않음")


def test_회차_상태에_유효_timeout과_warmup이_남는다(drop, tmp_path):
    url, tok, root = drop
    _seed_board(tmp_path, url, tok)
    rnd = bw.pull_channel(url, TOKEN, "bbs-t", tmp_path / "t", timeout=7, warmup=False)
    assert rnd["timeout_s"] == 7 and rnd["warmup"] is False and rnd["status"] == "complete"


# ── Orin 041 — R1 동시성 · R2 구조 검사 · R4 완결성 ──────────────────────────

def test_R1_동시_같은_게시는_원장_한_행·같은_quad로_수렴한다(tmp_path, monkeypatch):
    """041 R1: 두 writer가 각자 stale hub를 읽고 같은 이벤트를 두 번 append → 재생 불가.
    락이 load/replay 전부터 append·outbox 배정까지 덮으면 뒤쪽은 앞쪽의 결과로 수렴한다."""
    import threading
    import time
    host = _lab(tmp_path, "naru")
    real = ho.load_hub

    def slow_load(*a, **k):
        v = real(*a, **k)
        time.sleep(0.3)                      # 락 없이는 여기서 둘이 같은 stale 상태를 든다
        return v
    monkeypatch.setattr(ho, "load_hub", slow_load)
    ev = {"kind": "board.created", "board": "race", "caretaker": CARE, "at": "t01"}
    results = []

    def post():
        results.append(bw.prepare_post(
            hub_dir=host[1], seed_path=host[2], signer="lab:naru", key_id="k1", epoch=1,
            board="race", event=ev, url_base="http://127.0.0.1:1", to_lab="lab:naru",
            outbox=host[0] / "outbox", created_at="2026-09-12T00:00:00Z"))
    ts = [threading.Thread(target=post) for _ in range(2)]
    [t.start() for t in ts]; [t.join(timeout=20) for t in ts]
    assert len(results) == 2 and results[0]["n"] == results[1]["n"] == "001"
    assert results[0]["event_id"] == results[1]["event_id"]
    assert len((host[1] / "events.jsonl").read_text().splitlines()) == 1
    real(host[1])                                                   # 재생 가능
    # 서로 다른 이벤트 둘도 동시에 — 두 행·두 번호, 재생 가능
    results.clear()
    evs = [dict(ev, kind="board.metadata", at="t02", name=f"m{i}") for i in (1, 2)]

    def post2(e):
        results.append(bw.prepare_post(
            hub_dir=host[1], seed_path=host[2], signer="lab:naru", key_id="k1", epoch=1,
            board="race", event=e, url_base="http://127.0.0.1:1", to_lab="lab:naru",
            outbox=host[0] / "outbox", created_at="2026-09-12T00:00:00Z"))
    ts = [threading.Thread(target=post2, args=(e,)) for e in evs]
    [t.start() for t in ts]; [t.join(timeout=20) for t in ts]
    assert sorted(r["n"] for r in results) == ["002", "003"]
    assert len((host[1] / "events.jsonl").read_text().splitlines()) == 3
    real(host[1])


def test_R1_hub_CLI_쓰기_명령도_락_아래(tmp_path, monkeypatch, capsys):
    import threading
    import time
    host = _lab(tmp_path, "naru")
    (tmp_path / "b.md").write_bytes(b"same body\n")
    real = ho.load_hub

    def slow_load(*a, **k):
        v = real(*a, **k)
        time.sleep(0.3)
        return v
    monkeypatch.setattr(ho, "load_hub", slow_load)
    rcs = []

    def msg():
        rcs.append(cli.main(["message", "--dir", str(host[1]), "--key", str(host[2]),
                             "--signer", "lab:naru", "--key-id", "k1", "--epoch", "1",
                             "--to-lab", "lab:x", "--to-id", "X", "--to-epoch", "1",
                             "--body-file", str(tmp_path / "b.md")]))
    ts = [threading.Thread(target=msg) for _ in range(2)]
    [t.start() for t in ts]; [t.join(timeout=20) for t in ts]
    assert rcs == [0, 0]
    assert len((host[1] / "events.jsonl").read_text().splitlines()) == 1
    real(host[1])


def test_R2_유효_서명된_불량_구조_한_건은_격리되고_정상_글은_보인다(drop, tmp_path, capsys):
    url, tok, root = drop
    host, guest = _seed_board(tmp_path, url, tok)
    bad = {"kind": "board.post", "board": "bbs-t", "post_id": "bad", "author": {},
           "at": "t12", "text": "bad author shape"}
    # 게시 경로: 서명 전에 거부(원장 무접촉)
    before = (host[1] / "events.jsonl").read_bytes()
    with pytest.raises(bw.BbsWireError, match="author"):
        _post(*host, "naru", url, tok, "bbs-t", bad)
    assert (host[1] / "events.jsonl").read_bytes() == before
    # 인입 경로: 손 push된 불량 구조(유효 서명) → quad별 문제, read는 계속 선다
    assert _raw_push(*host, "naru", url, "bbs-t", bad)["stored"]
    for weird in ({"kind": "board.member.admitted", "board": "bbs-t", "at": "t13",
                   "member": "not-a-coord", "acting_agent": CARE},
                  {"kind": "board.metadata", "board": "bbs-t", "at": "t14",
                   "scale": ["nope"]},
                  {"kind": "board.notice", "board": "bbs-t", "at": "t15", "notice_id": 7}):
        assert _raw_push(*host, "naru", url, "bbs-t", weird)["stored"]
    tree = tmp_path / "tree"
    bw.pull_channel(url, TOKEN, "bbs-t", tree, timeout=T, warmup=False)
    rc, out, err = _cli(capsys, "read", "bbs-t", "--tree", str(tree), "--hub", str(host[1]))
    assert out is not None and [p["post_id"] for p in out["posts"]] == ["p1", "p2"]
    assert out["transport_problem_count"] == 4 and rc == 1
    whys = " | ".join(p["why"] for p in out["transport_problems"])
    assert "author" in whys and "member" in whys and "scale" in whys and "notice_id" in whys


def test_R2_봉투_payload_타입_오류는_문제로_남고_읽기는_죽지_않는다(drop, tmp_path):
    url, tok, root = drop
    host, guest = _seed_board(tmp_path, url, tok)
    tree = tmp_path / "tree"
    bw.pull_channel(url, TOKEN, "bbs-t", tree, timeout=T, warmup=False)
    door = tree / "bbs-t" / "from-naru"
    env = json.loads((door / "004-envelope.json").read_bytes())
    env["payload"] = ["not", "a", "dict"]
    (door / "004-envelope.json").write_bytes(json.dumps(env).encode())
    (door / "005-envelope.json").write_bytes(b"[1,2,3]")          # 봉투 모양 아님
    (door / "005-sig.txt").write_bytes(b"ab" * 64 + b"\n")
    st = bw.read_board(tree, "bbs-t", ho.load_hub(host[1])[2])
    assert [p["post_id"] for p in st["posts"]] == ["p2"]         # 004(p1)만 빠진다
    whys = " | ".join(p["why"] for p in st["transport_problems"])
    assert "봉투 모양이 아님" in whys and ("서명 무효" in whys or "스키마" in whys)


def test_R4_첫_수거에서_한_문이_실패하면_read는_partial이고_exit_1(drop, tmp_path, monkeypatch, capsys):
    url, tok, root = drop
    host, _ = _seed_board(tmp_path, url, tok)
    tree = tmp_path / "tree"
    real = hd.fetch_page

    def fail_host(u, *a, **k):
        if u.endswith("/from-naru"):
            raise urllib.error.URLError("synthetic network outage")
        return real(u, *a, **k)
    monkeypatch.setattr(hd, "fetch_page", fail_host)
    rc, out, _ = _cli(capsys, "pull", "bbs-t", "--url", url, "--token-file", str(tok),
                      "--tree", str(tree), "--no-warmup", "--timeout", str(T))
    assert rc == 1
    rc, out, _ = _cli(capsys, "read", "bbs-t", "--tree", str(tree), "--hub", str(host[1]))
    assert rc == 1 and out["completeness"] == "partial"
    assert out["post_count_total"] == 0 and out["transport_problem_count"] == 0
    assert out["round_problems"][0]["door"] == "from-naru"


def test_R4_회차_기록이_없는_외부_미러는_unknown이고_exit_0(drop, tmp_path, capsys):
    url, tok, root = drop
    host, _ = _seed_board(tmp_path, url, tok)
    tree = tmp_path / "tree"
    bw.pull_channel(url, TOKEN, "bbs-t", tree, timeout=T, warmup=False)
    (tree / "bbs-t" / ".round.json").unlink()                     # 남의 수거기가 만든 미러처럼
    rc, out, _ = _cli(capsys, "read", "bbs-t", "--tree", str(tree), "--hub", str(host[1]))
    assert rc == 0 and out["completeness"] == "unknown" and out["round"] is None
    assert [p["post_id"] for p in out["posts"]] == ["p1", "p2"]


# ── Orin 042 잔여 — R2A·R2B·R3 envelope 충돌 · LxM 069 warm 계측 ─────────────────

def test_R2A_profile_kind_배열은_서명_전_구조_거부이고_인입은_격리(drop, tmp_path):
    url, tok, root = drop
    host = _lab(tmp_path, "naru")
    good = {"kind": "profile.announced", "at": "t1",
            "subject": {"lab": "lab:naru", "id": "Cody"},
            "author": {"lab": "lab:naru", "id": "Cody"},
            "profile": {"kind": "creature", "display_name": "Cody"}}
    assert _post(*host, "naru", url, tok, "directory", good)["status"] == "stored"
    bad = dict(good, at="t2", subject={"lab": "lab:naru", "id": "Bent"},
               author={"lab": "lab:naru", "id": "Bent"}, profile={"kind": []})
    before = (host[1] / "events.jsonl").read_bytes()
    with pytest.raises(bw.BbsWireError, match="profile.kind"):          # 구조화된 거부
        _post(*host, "naru", url, tok, "directory", bad)
    assert (host[1] / "events.jsonl").read_bytes() == before
    assert _raw_push(*host, "naru", url, "directory", bad)["stored"]     # 유효 서명 손 push
    tree = tmp_path / "tree"
    bw.pull_channel(url, TOKEN, "directory", tree, timeout=T, warmup=False)
    out = bw.read_directory(tree, "directory", ho.load_hub(host[1])[2], compiled_at="c1")
    assert out["row_count"] == 1 and out["rows"][0]["subject"]["id"] == "Cody"
    assert out["transport_problem_count"] == 1
    assert "profile.kind" in out["transport_problems"][0]["why"]


def test_R2B_payload_body_sha256_배열은_문제로_남고_정상_글은_보인다(drop, tmp_path, capsys):
    from organum import hub_envelope as he
    url, tok, root = drop
    host, guest = _seed_board(tmp_path, url, tok)
    tree = tmp_path / "tree"
    bw.pull_channel(url, TOKEN, "bbs-t", tree, timeout=T, warmup=False)
    d, cfg, hubx = ho.load_hub(host[1])
    body = bw.event_body_bytes({"kind": "board.post", "board": "bbs-t", "post_id": "p9",
                                "author": CARE, "reply_to": None, "at": "t19"})
    env = ho.build_envelope(cfg, "message.posted",
                            {"target": {"lab_id": "lab:naru", "to_id": "board", "to_epoch": 1},
                             "body_locator": "file://x.json", "body_sha256": ["not-a-digest"],
                             "body_media_type": "application/json"},
                            signer="lab:naru", key_id="k1", epoch=1,
                            subject={"type": "message", "id": "message:" + "0" * 24},
                            created_at="2026-09-12T00:00:00Z")
    raw = he.canonical_bytes(env)
    sig = sp.sign(hashlib.sha256(raw).digest(), SEEDS["naru"]).hex()
    door = tree / "bbs-t" / "from-naru"
    (door / "009-sig.txt").write_bytes((sig + "\n").encode())
    (door / "009-body.json").write_bytes(body)
    (door / "009-envelope.json").write_bytes(raw)
    rc, out, err = _cli(capsys, "read", "bbs-t", "--tree", str(tree), "--hub", str(host[1]))
    assert out is not None and [p["post_id"] for p in out["posts"]] == ["p1", "p2"]
    assert rc == 1 and out["transport_problem_count"] == 1
    assert "body_sha256" in out["transport_problems"][0]["why"]


def test_R3_잔여_기존_envelope가_서버와_다르면_복구하지_않고_명시_오류(drop, tmp_path, capsys):
    url, tok, root = drop
    host, _ = _seed_board(tmp_path, url, tok)
    tree = tmp_path / "tree"
    bw.pull_channel(url, TOKEN, "bbs-t", tree, timeout=T, warmup=False)
    door = tree / "bbs-t" / "from-naru"
    (door / "002-body.json").unlink()
    env = json.loads((door / "002-envelope.json").read_bytes())
    env["created_at"] = "2026-01-01T00:00:00Z"                     # 정상 형식, 서버와 다름
    tampered = json.dumps(env, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    (door / "002-envelope.json").write_bytes(tampered)
    rnd = bw.pull_channel(url, TOKEN, "bbs-t", tree, timeout=T, warmup=False)
    r = rnd["results"]["from-naru"]
    assert r["ok"] is False and "envelope" in r["error"] and "덮어쓰지 않는다" in r["error"]
    assert r["repaired"] == 0 and not (door / "002-body.json").exists()   # 충돌 quad 복구 0
    assert (door / "002-envelope.json").read_bytes() == tampered           # 보존
    capsys.readouterr()
    assert bbs_cli.main(["pull", "bbs-t", "--url", url, "--token-file", str(tok),
                         "--tree", str(tree), "--no-warmup", "--timeout", str(T)]) == 1


def test_발견이_덥힌_워밍_계측이_회차에_남는다(drop, tmp_path):
    url, tok, root = drop
    _seed_board(tmp_path, url, tok)
    rnd = bw.pull_channel(url, TOKEN, "bbs-t", tmp_path / "t", timeout=T, warmup=True)
    assert rnd["warm_ms"] is not None and rnd["warm_ok"] is True   # LxM 069 §3-2
    assert rnd["warm_budget_s"] == T
