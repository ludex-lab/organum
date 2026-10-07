"""hub drop 0.8.0 — 서버 쪽: 상태 칸(`/v0/state`)·문 색인(`?index=1`)·표지 읽기.

설계: docs/hub-state-snapshot-restore-reconcile-v0-design.md §6·§8.1. 계약:
1. 칸은 토큰 줄의 `id=`마다 하나다. 줄에 적은 id만 칸의 이름이 된다. 자기 칸만 읽고 쓴다.
2. 올리는 조건은 세대와 지문 둘이다. 어긋나면 409이고 지금 세대와 그 지문을 알려 준다.
3. 세대 번호는 되돌아가지 않는다(`floor_generation`).
4. 같은 것을 같은 조건으로 다시 올리면 중복으로 받는다.
5. 한 세대가 파일 하나다. 그 이름이 없을 때만 놓는다. 지금 세대는 요청마다 디렉터리에서 읽는다.
6. 서버는 묶음을 열지 않는다. 표지는 읽기만 한다.
7. 문 색인은 본문 없이 번호와 저장된 바이트의 지문만 준다. 기본은 처음부터다.

Jdot HQ의 순서 모형 시험(상태 칸 33개·v0.8 34개·v0.10 25개)에서 서버 쪽 확인을 옮겨 왔다.
그 시험은 가짜 서버에 대한 것이었고 이것은 제품 서버에 대한 것이다.
"""

import base64
import hashlib
import http.client
import json
import sys
import threading
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
from organum import hub_drop as hd  # noqa: E402

SIG = "cd" * 64
ENV = b'{"signer":{"id":"lab:jdot-hq"},"x":1}'
NOW = 1791244800.0                                   # 2026-10-06T00:00:00Z

JDOT = "tok-jdot  id=jdot-hq"
ORG = "tok-org  id=organum"
NOID = "tok-noid"
OLD = "tok-old  id=jdot-hq-old  revoked"


def _serve(tmp_path, token_lines, *, state=True, marks=False, **kw):
    tok = tmp_path / "tokens.txt"
    tok.write_text("\n".join(token_lines) + "\n", encoding="utf-8")
    root = tmp_path / "drops"
    sdir = tmp_path / "state" if state else None
    mdir = tmp_path / "marks" if marks else None
    if mdir is not None:
        mdir.mkdir()
    srv = hd.make_server(root, tok, bind="127.0.0.1", port=0, rate_limit_per_minute=0,
                         audit_dir=tmp_path / "audit", now=lambda: NOW,
                         state_dir=sdir, marks_dir=mdir, **kw)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"127.0.0.1:{srv.server_address[1]}", root, sdir, mdir


def _req(host, method, path, token, body=None):
    c = http.client.HTTPConnection(host, timeout=10)
    h = {"Authorization": f"Bearer {token}"}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    if data:
        h["Content-Type"] = "application/json"
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    out = (r.status, json.loads(r.read().decode("utf-8")))
    c.close()
    return out


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def _put(host, token, blob, expect=(0, ""), floor=None, sha=None, sig=SIG):
    body = {"expect_generation": expect[0], "expect_sha256": expect[1],
            "sha256": sha if sha is not None else _sha(blob),
            "blob_b64": base64.b64encode(blob).decode("ascii"), "sig": sig}
    if floor is not None:
        body["floor_generation"] = floor
    return _req(host, "POST", "/v0/state", token, body)


def _audit(tmp_path):
    out = []
    for f in sorted((tmp_path / "audit").glob("audit-*.jsonl")):
        out += [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines()]
    return out


# ── 칸의 주인 ────────────────────────────────────────────────────────────────

def test_empty_slot_is_generation_zero_and_carries_the_limit(tmp_path):
    srv, host, *_ = _serve(tmp_path, [JDOT])
    for path in ("/v0/state", "/v0/state?meta=1"):
        st, body = _req(host, "GET", path, "tok-jdot")
        assert st == 404 and body["generation"] == 0 and body["sha256"] == ""
        assert body["max_bytes"] == hd.STATE_MAX_BYTES and body["kept"] == []
        assert "mirrored" not in body and "settled" not in body      # 표지를 쓰지 않는 배치
    srv.shutdown()


def test_slot_needs_an_explicit_id_and_is_private(tmp_path):
    srv, host, _root, sdir, _ = _serve(tmp_path, [JDOT, ORG, NOID, OLD])
    assert _put(host, "tok-jdot", b"jdot-1")[0] == 200
    # 줄에 id=가 없는 토큰은 칸을 쓰지 못한다 — 서버가 대신 만든 id는 칸의 이름이 아니다
    assert _req(host, "GET", "/v0/state", "tok-noid")[0] == 403
    assert _put(host, "tok-noid", b"x")[0] == 403
    # 폐기 줄은 401이다
    assert _req(host, "GET", "/v0/state", "tok-old")[0] == 401
    # 다른 연구소는 자기 칸만 본다
    st, body = _req(host, "GET", "/v0/state", "tok-org")
    assert st == 404 and body["generation"] == 0
    assert _put(host, "tok-org", b"org-1")[0] == 200
    assert _req(host, "GET", "/v0/state?meta=1", "tok-jdot")[1]["sha256"] == _sha(b"jdot-1")
    assert sorted(p.name for p in sdir.iterdir()) == ["jdot-hq", "organum"]
    srv.shutdown()


def test_same_id_new_token_keeps_the_slot(tmp_path):
    """토큰 값을 바꿔도 id가 같으면 같은 칸이다. 옛 줄은 다른 id로 revoked 한다(같은 id 두 줄은 안 뜬다)."""
    srv, host, *_ = _serve(tmp_path, [JDOT])
    assert _put(host, "tok-jdot", b"one")[0] == 200
    srv.shutdown()
    srv2, host2, *_ = _serve(tmp_path, ["tok-jdot-new  id=jdot-hq", "tok-jdot  id=jdot-hq-old  revoked"])
    st, body = _req(host2, "GET", "/v0/state", "tok-jdot-new")
    assert st == 200 and base64.b64decode(body["blob_b64"]) == b"one"
    assert _req(host2, "GET", "/v0/state", "tok-jdot")[0] == 401
    srv2.shutdown()


def test_without_state_dir_the_slot_does_not_exist(tmp_path):
    srv, host, *_ = _serve(tmp_path, [JDOT], state=False)
    assert _req(host, "GET", "/v0/state", "tok-jdot")[0] == 404
    assert _put(host, "tok-jdot", b"x")[0] == 404
    # `state`라는 이름의 채널(세 조각)과는 부딪치지 않는다
    bundle = {"n": "001", "envelope_b64": base64.b64encode(ENV).decode("ascii"), "sig": SIG}
    assert _req(host, "POST", "/v0/state/from-jdot-hq", "tok-jdot", bundle)[0] == 200
    srv.shutdown()


# ── 조건: 세대와 지문 ─────────────────────────────────────────────────────────

def test_cas_on_generation_and_fingerprint(tmp_path):
    srv, host, _root, sdir, _ = _serve(tmp_path, [JDOT])
    st, body = _put(host, "tok-jdot", b"one")
    assert (st, body) == (200, {"generation": 1, "stored": True, "dedup": False})
    before = sorted(p.name for p in (sdir / "jdot-hq").iterdir())
    # 낡은 조건
    st, body = _put(host, "tok-jdot", b"two", expect=(0, ""))
    assert st == 409 and body["generation"] == 1 and body["sha256"] == _sha(b"one")
    # 세대는 맞고 지문만 다른 조건도 409다 — 번호가 되돌아가면 같은 번호가 두 번 쓰인다
    st, body = _put(host, "tok-jdot", b"two", expect=(1, _sha(b"other")))
    assert st == 409 and body["generation"] == 1
    # 앞선 세대를 조건으로
    assert _put(host, "tok-jdot", b"two", expect=(9, _sha(b"one")))[0] == 409
    assert sorted(p.name for p in (sdir / "jdot-hq").iterdir()) == before       # 아무것도 바뀌지 않았다
    st, body = _put(host, "tok-jdot", b"two", expect=(1, _sha(b"one")))
    assert st == 200 and body["generation"] == 2
    st, got = _req(host, "GET", "/v0/state", "tok-jdot")
    assert st == 200 and got["generation"] == 2 and got["prev_generation"] == 1
    assert got["prev_sha256"] == _sha(b"one") and got["sig"] == SIG and got["size"] == 3
    assert base64.b64decode(got["blob_b64"]) == b"two"
    srv.shutdown()


def test_retry_of_the_same_upload_is_a_duplicate(tmp_path):
    """응답을 잃고 같은 것을 같은 조건으로 다시 올린다. 지문이 다르면 409다."""
    srv, host, *_ = _serve(tmp_path, [JDOT])
    assert _put(host, "tok-jdot", b"one")[1]["generation"] == 1
    st, body = _put(host, "tok-jdot", b"one")
    assert (st, body) == (200, {"generation": 1, "stored": True, "dedup": True})
    assert _put(host, "tok-jdot", b"different")[0] == 409
    assert _put(host, "tok-jdot", b"two", expect=(1, _sha(b"one")))[1]["generation"] == 2
    # 중복은 바로 앞 세대에 대해서만 선다
    assert _put(host, "tok-jdot", b"two", expect=(1, _sha(b"one")))[1]["dedup"] is True
    assert _put(host, "tok-jdot", b"one")[0] == 409
    srv.shutdown()


def test_generation_numbers_do_not_go_back(tmp_path):
    """올리는 쪽이 보낸 적 있는 가장 큰 번호를 낸다. 서버는 그보다 큰 번호를 매긴다(LxM 131)."""
    srv, host, *_ = _serve(tmp_path, [JDOT])
    assert _put(host, "tok-jdot", b"base")[1]["generation"] == 1
    st, body = _put(host, "tok-jdot", b"again", expect=(1, _sha(b"base")), floor=19)
    assert st == 200 and body["generation"] == 20
    st, got = _req(host, "GET", "/v0/state?meta=1", "tok-jdot")
    assert got["generation"] == 20 and got["prev_generation"] == 1 and got["prev_sha256"] == _sha(b"base")
    # 건너뛴 뒤에도 같은 것을 다시 올리면 중복이다 — 조건은 머리에 적힌 앞 세대와 견준다
    st, body = _put(host, "tok-jdot", b"again", expect=(1, _sha(b"base")), floor=19)
    assert (st, body["generation"], body["dedup"]) == (200, 20, True)
    # floor가 지금 세대보다 낮으면 그냥 다음 번호다
    assert _put(host, "tok-jdot", b"next", expect=(20, _sha(b"again")), floor=3)[1]["generation"] == 21
    srv.shutdown()


def test_floor_has_an_upper_bound(tmp_path):
    srv, host, *_ = _serve(tmp_path, [JDOT], state_max_jump=100)
    st, body = _put(host, "tok-jdot", b"x", floor=101)
    assert st == 400 and body["generation"] == 0
    assert _req(host, "GET", "/v0/state?meta=1", "tok-jdot")[0] == 404          # 칸이 그대로다
    st, body = _put(host, "tok-jdot", b"x", floor=100)
    assert st == 200 and body["generation"] == 101
    srv.shutdown()


def test_two_writers_at_the_same_generation_one_wins(tmp_path):
    srv, host, *_ = _serve(tmp_path, [JDOT])
    assert _put(host, "tok-jdot", b"base")[0] == 200
    cond = (1, _sha(b"base"))
    assert _put(host, "tok-jdot", b"writer-a", expect=cond)[1]["generation"] == 2
    st, body = _put(host, "tok-jdot", b"writer-b", expect=cond)
    assert st == 409 and body == {"error": body["error"], "generation": 2, "sha256": _sha(b"writer-a")}
    srv.shutdown()


# ── 모양과 한도 ───────────────────────────────────────────────────────────────

def test_server_does_not_open_the_bundle(tmp_path):
    """바이트와 지문과 세대만 다룬다. 서명은 실어 나를 뿐이다."""
    srv, host, *_ = _serve(tmp_path, [JDOT])
    raw = b"not a compressed bundle at all \x00\xff"
    assert _put(host, "tok-jdot", raw, sig="00" * 64)[0] == 200
    got = _req(host, "GET", "/v0/state", "tok-jdot")[1]
    assert base64.b64decode(got["blob_b64"]) == raw and got["sig"] == "00" * 64
    srv.shutdown()


def test_wrong_fingerprint_is_400_and_nothing_changes(tmp_path):
    srv, host, _root, sdir, _ = _serve(tmp_path, [JDOT])
    assert _put(host, "tok-jdot", b"one", sha="0" * 64)[0] == 400
    assert not (sdir / "jdot-hq").exists() or not list((sdir / "jdot-hq").iterdir())
    for bad in ({"sig": "zz"}, {"sig": SIG + "\n"}, {"sha": "abc"}):
        assert _put(host, "tok-jdot", b"one", **bad)[0] == 400
    st, _ = _req(host, "POST", "/v0/state", "tok-jdot",
                 {"expect_generation": 0, "expect_sha256": "", "sha256": _sha(b"x"),
                  "blob_b64": "@@@", "sig": SIG})
    assert st == 400
    st, _ = _req(host, "POST", "/v0/state", "tok-jdot",
                 {"expect_generation": True, "expect_sha256": "", "sha256": _sha(b"x"),
                  "blob_b64": "eA==", "sig": SIG})
    assert st == 400
    srv.shutdown()


def test_size_limit_exact_and_plus_one(tmp_path):
    srv, host, *_ = _serve(tmp_path, [JDOT], state_max_bytes=1000)
    assert _req(host, "GET", "/v0/state?meta=1", "tok-jdot")[1]["max_bytes"] == 1000
    st, body = _put(host, "tok-jdot", b"x" * 1001)
    assert st == 413 and body["max_bytes"] == 1000
    assert _put(host, "tok-jdot", b"x" * 1000)[0] == 200
    srv.shutdown()


def test_state_max_bytes_must_fit_the_request_limit(tmp_path):
    tok = tmp_path / "t.txt"
    tok.write_text(JDOT + "\n", encoding="utf-8")
    kw = dict(bind="127.0.0.1", port=0, state_dir=tmp_path / "state")
    hd.make_server(tmp_path / "drops", tok, state_max_bytes=1_500_000, **kw).server_close()
    with pytest.raises(ValueError):
        hd.make_server(tmp_path / "drops", tok, state_max_bytes=1_572_864, **kw)      # 1.5 MiB 정각
    with pytest.raises(ValueError):
        hd.make_server(tmp_path / "drops", tok, state_keep=0, **kw)


def test_state_and_marks_dirs_must_not_overlap_anything(tmp_path):
    tok = tmp_path / "t.txt"
    tok.write_text(JDOT + "\n", encoding="utf-8")
    root = tmp_path / "drops"
    for kw in (dict(state_dir=root / "state"), dict(marks_dir=root / "marks"),
               dict(state_dir=tmp_path / "s", marks_dir=tmp_path / "s" / "marks"),
               dict(state_dir=tmp_path / "a" / "s", audit_dir=tmp_path / "a"),
               dict(state_dir=tmp_path)):
        with pytest.raises(ValueError):
            hd.make_server(root, tok, bind="127.0.0.1", port=0, **kw)


# ── 디스크: 한 세대 한 파일 ───────────────────────────────────────────────────

def test_one_file_per_generation_and_keep_k(tmp_path):
    srv, host, _root, sdir, _ = _serve(tmp_path, [JDOT], state_keep=2)
    cond = (0, "")
    for i in range(1, 5):
        blob = f"g{i}".encode()
        assert _put(host, "tok-jdot", blob, expect=cond)[1]["generation"] == i
        cond = (i, _sha(blob))
    files = sorted(p.name for p in (sdir / "jdot-hq").iterdir())
    assert files == ["00000003.state", "00000004.state"]                  # 임시 파일도 포인터도 없다
    st, body = _req(host, "GET", "/v0/state?meta=1", "tok-jdot")
    assert body["generation"] == 4 and [k["generation"] for k in body["kept"]] == [4, 3]
    assert _req(host, "GET", "/v0/state?generation=3", "tok-jdot")[1]["sha256"] == _sha(b"g3")
    st, body = _req(host, "GET", "/v0/state?generation=2", "tok-jdot")
    assert st == 404 and body["generation"] == 4                          # 지운 세대
    assert _req(host, "GET", "/v0/state?generation=x", "tok-jdot")[0] == 400
    # 파일의 첫 줄이 머리이고 그 뒤가 묶음의 바이트다
    raw = (sdir / "jdot-hq" / "00000004.state").read_bytes()
    head, _, blob = raw.partition(b"\n")
    assert json.loads(head)["generation"] == 4 and blob == b"g4"
    srv.shutdown()


def test_current_generation_is_read_from_the_directory_on_every_request(tmp_path):
    """감독기의 다시 읽기가 놓은 세대를 서버가 다음 요청부터 본다(LxM 127 §2)."""
    srv, host, _root, sdir, _ = _serve(tmp_path, [JDOT])
    assert _put(host, "tok-jdot", b"one")[0] == 200
    header = {"generation": 5, "sha256": _sha(b"from-bucket"), "prev_generation": 4,
              "prev_sha256": _sha(b"four"), "size": len(b"from-bucket"), "sig": SIG}
    assert hd._state_place(sdir / "jdot-hq", header, b"from-bucket") is True
    st, body = _req(host, "GET", "/v0/state", "tok-jdot")
    assert body["generation"] == 5 and base64.b64decode(body["blob_b64"]) == b"from-bucket"
    # 옛 조건으로 올리면 409이고 놓인 세대를 알려 준다
    st, body = _put(host, "tok-jdot", b"two", expect=(1, _sha(b"one")))
    assert st == 409 and body["generation"] == 5 and body["sha256"] == _sha(b"from-bucket")
    # 더 낮은 번호의 옛 세대를 놓아도 지금 세대가 바뀌지 않는다
    low = dict(header, generation=3, prev_generation=2)
    assert hd._state_place(sdir / "jdot-hq", low, b"from-bucket") is True
    assert _req(host, "GET", "/v0/state?meta=1", "tok-jdot")[1]["generation"] == 5
    srv.shutdown()


def test_place_only_if_absent_never_overwrites(tmp_path):
    d = tmp_path / "slot"
    d.mkdir()
    h = {"generation": 7, "sha256": _sha(b"first"), "prev_generation": 6, "prev_sha256": _sha(b"six"),
         "size": 5, "sig": SIG}
    assert hd._state_place(d, h, b"first") is True
    h2 = dict(h, sha256=_sha(b"other"))
    assert hd._state_place(d, h2, b"other") is False
    assert [p.name for p in d.iterdir()] == ["00000007.state"]                 # 임시 파일이 남지 않는다
    gens = hd._state_scan(d)
    assert len(gens) == 1 and hd._state_blob(gens[0][1], gens[0][2]) == b"first"


def test_server_loses_the_race_to_a_placed_file_and_answers_409(tmp_path, monkeypatch):
    """서버가 조건을 본 뒤 놓기 전에 감독기가 그 이름을 놓았다. 덮지 않고 409로 답한다."""
    srv, host, _root, sdir, _ = _serve(tmp_path, [JDOT])
    assert _put(host, "tok-jdot", b"one")[0] == 200
    real = hd._state_place

    def racing(dirp, header, blob):
        other = dict(header, sha256=_sha(b"supervisor"), size=len(b"supervisor"))
        assert real(dirp, other, b"supervisor") is True
        return real(dirp, header, blob)

    monkeypatch.setattr(hd, "_state_place", racing)
    st, body = _put(host, "tok-jdot", b"two", expect=(1, _sha(b"one")))
    assert st == 409 and body["generation"] == 2 and body["sha256"] == _sha(b"supervisor")
    monkeypatch.setattr(hd, "_state_place", real)
    assert base64.b64decode(_req(host, "GET", "/v0/state", "tok-jdot")[1]["blob_b64"]) == b"supervisor"
    srv.shutdown()


def test_broken_and_temporary_files_are_not_counted(tmp_path):
    srv, host, _root, sdir, _ = _serve(tmp_path, [JDOT])
    assert _put(host, "tok-jdot", b"one")[0] == 200
    d = sdir / "jdot-hq"
    (d / ".00000009.state.123.abcd.tmp").write_bytes(b"half written")
    (d / "00000008.state").write_bytes(b'{"generation":8}\ntruncated')          # 머리가 모자란다
    good = (d / "00000001.state").read_bytes()
    (d / "00000007.state").write_bytes(good.replace(b'"generation":1', b'"generation":7') + b"x")  # 길이가 다르다
    (d / "00000006.state").write_bytes(good)                                     # 이름과 머리의 번호가 다르다
    assert b'"generation":1' in good
    # 길이는 맞는데 머리가 이름보다 높은 번호를 댄다 — 세면 지금 세대가 9로 뛴다
    (d / "00000005.state").write_bytes(good.replace(b'"generation":1', b'"generation":9'))
    assert _req(host, "GET", "/v0/state?meta=1", "tok-jdot")[1]["generation"] == 1
    # 길이는 맞고 바이트만 바뀐 세대는 내주지 않는다
    head, _, _blob = good.partition(b"\n")
    (d / "00000001.state").write_bytes(head + b"\n" + b"eno")
    assert _req(host, "GET", "/v0/state", "tok-jdot")[0] == 500
    srv.shutdown()


def test_startup_sweeps_leftover_temporary_files(tmp_path):
    slot = tmp_path / "state" / "jdot-hq"
    slot.mkdir(parents=True)
    (slot / ".00000002.state.1.aa.tmp").write_bytes(b"left over")
    srv, host, _root, sdir, _ = _serve(tmp_path, [JDOT])
    assert list((sdir / "jdot-hq").iterdir()) == []
    srv.shutdown()


# ── 표지: 감독기가 쓰고 서버는 읽기만 ─────────────────────────────────────────

def test_marks_are_read_only_signals(tmp_path):
    srv, host, root, sdir, mdir = _serve(tmp_path, [JDOT], marks=True)
    st, body = _req(host, "GET", "/v0/state?meta=1", "tok-jdot")
    assert st == 404 and body["settled"] is False and body["settled_by_timeout"] is False
    assert _put(host, "tok-jdot", b"one")[0] == 200
    body = _req(host, "GET", "/v0/state?meta=1", "tok-jdot")[1]
    assert body["mirrored"] is False and body["kept"] == [
        {"generation": 1, "sha256": _sha(b"one"), "mirrored": False}]
    assert list(mdir.iterdir()) == []                                  # 서버는 표지를 쓰지 않는다
    (mdir / "state" / "jdot-hq").mkdir(parents=True)
    (mdir / "state" / "jdot-hq" / "00000001").write_bytes(b"")
    (mdir / "settled-by-timeout").write_text("boot-a\n", encoding="utf-8")
    body = _req(host, "GET", "/v0/state", "tok-jdot")[1]              # 묶음을 실은 응답에 함께 싣는다
    assert body["mirrored"] is True and body["settled"] is False and body["settled_by_timeout"] is True
    assert "blob_b64" in body and body["kept"][0]["mirrored"] is True
    (mdir / "settled").write_bytes(b"")
    assert _req(host, "GET", "/v0/state?meta=1", "tok-jdot")[1]["settled"] is True
    # 다음 세대에는 아직 표지가 없다
    assert _put(host, "tok-jdot", b"two", expect=(1, _sha(b"one")))[0] == 200
    body = _req(host, "GET", "/v0/state?meta=1", "tok-jdot")[1]
    assert body["mirrored"] is False and [k["mirrored"] for k in body["kept"]] == [False, True]
    srv.shutdown()


# ── 문 색인 ──────────────────────────────────────────────────────────────────

def _post_quad(host, token, door, n, body=None, env=ENV):
    b = {"n": n, "envelope_b64": base64.b64encode(env).decode("ascii"), "sig": SIG}
    if body is not None:
        b["body_name"] = "body.md"
        b["body_b64"] = base64.b64encode(body).decode("ascii")
    return _req(host, "POST", f"/v0/hub-ops/{door}", token, b)


def test_door_index_gives_fingerprints_without_bodies(tmp_path):
    srv, host, root, _sdir, mdir = _serve(tmp_path, [JDOT], marks=True)
    assert _post_quad(host, "tok-jdot", "from-jdot-hq", "001", body=b"hello")[0] == 200
    assert _post_quad(host, "tok-jdot", "from-jdot-hq", "003")[0] == 200            # 본문 없는 quad, 번호가 건너뜀
    door = root / "hub-ops" / "from-jdot-hq"
    (door / "005-sig.txt").write_bytes(b"x")                                         # 봉투가 없는 번호
    st, body = _req(host, "GET", "/v0/hub-ops/from-jdot-hq?index=1", "tok-jdot")
    assert st == 200 and body["more"] is False
    assert [e["n"] for e in body["index"]] == ["001", "003"]
    one, three = body["index"]
    assert one == {"n": "001", "envelope_sha256": _sha(ENV), "sig_sha256": _sha((SIG + "\n").encode()),
                   "body_name": "body.md", "body_sha256": _sha(b"hello"), "body_size": 5,
                   "mirrored": False}
    assert three["body_name"] is None and three["body_sha256"] is None and three["body_size"] is None
    assert "hello" not in json.dumps(body) and "envelope_b64" not in json.dumps(body)
    (mdir / "quads" / "hub-ops" / "from-jdot-hq").mkdir(parents=True)
    (mdir / "quads" / "hub-ops" / "from-jdot-hq" / "001").write_bytes(b"")
    body = _req(host, "GET", "/v0/hub-ops/from-jdot-hq?index=1", "tok-jdot")[1]
    assert [e["mirrored"] for e in body["index"]] == [True, False]
    # 평소의 받기는 그대로다
    st, body = _req(host, "GET", "/v0/hub-ops/from-jdot-hq", "tok-jdot")
    assert st == 200 and [q["n"] for q in body["quads"]] == ["001", "003"]
    srv.shutdown()


def test_door_index_pages_and_respects_read_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(hd, "INDEX_PAGE_SIZE", 2)
    scoped = "tok-s  id=reader  read=hub-ops/from-organum"
    srv, host, root, *_ = _serve(tmp_path, [JDOT, scoped])
    for n in ("001", "002", "003"):
        assert _post_quad(host, "tok-jdot", "from-jdot-hq", n)[0] == 200
    st, body = _req(host, "GET", "/v0/hub-ops/from-jdot-hq?index=1", "tok-jdot")
    assert [e["n"] for e in body["index"]] == ["001", "002"] and body["more"] is True
    assert "mirrored" not in body["index"][0]                           # 표지를 쓰지 않는 배치
    st, body = _req(host, "GET", "/v0/hub-ops/from-jdot-hq?index=1&since=002", "tok-jdot")
    assert [e["n"] for e in body["index"]] == ["003"] and body["more"] is False
    assert _req(host, "GET", "/v0/hub-ops/from-jdot-hq?index=1", "tok-s")[0] == 403
    srv.shutdown()


# ── 감사 기록 ────────────────────────────────────────────────────────────────

def test_audit_lines_for_state_requests_carry_no_bundle_bytes(tmp_path):
    srv, host, *_ = _serve(tmp_path, [JDOT])
    secret = b"SECRET-BUNDLE-BYTES"
    assert _put(host, "tok-jdot", secret)[0] == 200
    assert _put(host, "tok-jdot", b"stale")[0] == 409
    assert _req(host, "GET", "/v0/state", "tok-jdot")[0] == 200
    srv.shutdown()
    lines = [l for l in _audit(tmp_path) if l.get("op", "").startswith("state_")]
    assert [(l["op"], l["status"]) for l in lines] == [
        ("state_put", 200), ("state_put", 409), ("state_get", 200)]
    assert lines[0]["generation"] == 1 and lines[0]["sha256"] == _sha(secret) and lines[0]["id"] == "jdot-hq"
    text = json.dumps(lines)
    assert "SECRET" not in text and base64.b64encode(secret).decode() not in text and "tok-jdot" not in text
