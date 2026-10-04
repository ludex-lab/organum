"""hub drop 0.7.0 회귀 — 토큰별 범위·감사 기록·입력 닻.

설계: docs/drop-scope-and-mcp-front-v0-design.md (컷 A). 계약:
1. 범위를 적지 않은 줄은 0.6.0과 똑같이 동작한다(호환 줄). `id=`만 붙여도 호환 줄이다.
2. `write=`/`read=` 가운데 하나라도 적은 줄은 범위 줄이다. 적지 않은 축은 권한 없음이다.
3. 범위 밖은 403이고 아무것도 쓰지 않는다. 문 목록은 읽을 수 있는 문만 보인다.
4. 서버는 여전히 봉투를 열지 않는다 — 범위는 URL 경로의 접근 목록이다.
5. 감사 기록은 운반 트리 밖에, 응답 뒤에, 본문·토큰 없이 남는다. 실패해도 운반을 막지 않는다.
6. 끝 줄바꿈이 붙은 sig·n·body_name은 400이다(0.6.0 결함, Jdot HQ 2026-10-04).
"""

import base64
import hashlib
import http.client
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
from organum import hub_drop as hd  # noqa: E402

CLI = [sys.executable, "-m", "organum.hub_cli"]
SIG = "ab" * 64
ENV = b'{"signer":{"id":"lab:jdot-hq"},"x":1}'      # 서버는 봉투를 열지 않는다 — 내용은 아무래도 좋다
NOW = 1791072000.0                                   # 2026-10-04T00:00:00Z


def _serve(tmp_path, token_lines, *, audit=True, rate=0, **kw):
    tok = tmp_path / "tokens.txt"
    tok.write_text("\n".join(token_lines) + "\n", encoding="utf-8")
    root = tmp_path / "drops"
    adir = tmp_path / "audit" if audit else None
    srv = hd.make_server(root, tok, bind="127.0.0.1", port=0, rate_limit_per_minute=rate,
                         audit_dir=adir, now=lambda: NOW, **kw)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"127.0.0.1:{srv.server_address[1]}", root, adir


def _req(host, method, path, token, body=None, headers=None):
    c = http.client.HTTPConnection(host, timeout=10)
    h = {"Authorization": f"Bearer {token}", **(headers or {})}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    if data:
        h["Content-Type"] = "application/json"
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    out = (r.status, json.loads(r.read().decode("utf-8")))
    c.close()
    return out


def _bundle(n="001", sig=SIG, env=ENV, body_name=None, body=None):
    b = {"n": n, "envelope_b64": base64.b64encode(env).decode("ascii"), "sig": sig}
    if body_name is not None:
        b["body_name"] = body_name
        b["body_b64"] = base64.b64encode(body or b"").decode("ascii")
    return b


def _audit_lines(adir):
    out = []
    for f in sorted(adir.glob("audit-*.jsonl")):
        out += [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines()]
    return out


JDOT = "tok-jdot  id=jdot-hq  write=hub-ops/from-jdot-hq  read=hub-ops/from-organum"
LEGACY = "tok-legacy"


# ── 1. 호환 줄 ────────────────────────────────────────────────────────────────

def test_범위_없는_줄은_전부_열리고_id만_붙여도_호환_줄이다(tmp_path):
    srv, host, root, _ = _serve(tmp_path, [LEGACY, "tok-named id=organum"])
    try:
        for tok in ("tok-legacy", "tok-named"):
            for door in ("from-a", "from-b"):
                st, r = _req(host, "POST", f"/v0/hub-ops/{door}", tok, _bundle())
                assert st == 200 and r["stored"] is True
            assert _req(host, "GET", "/v0/hub-ops/from-a", tok)[0] == 200
            st, r = _req(host, "GET", "/v0/channels", tok)
            assert r["channels"] == {"hub-ops": ["from-a", "from-b"]}
    finally:
        srv.shutdown()


# ── 2·3. 범위 줄 ──────────────────────────────────────────────────────────────

def test_쓰기_범위_밖은_403이고_아무것도_쓰지_않는다(tmp_path):
    srv, host, root, _ = _serve(tmp_path, [JDOT, LEGACY])
    try:
        st, r = _req(host, "POST", "/v0/hub-ops/from-jdot-hq", "tok-jdot", _bundle())
        assert (st, r["stored"]) == (200, True)
        for path in ("/v0/hub-ops/from-organum", "/v0/letters/from-jdot-hq", "/v0/hub-ops/from-lxm"):
            st, r = _req(host, "POST", path, "tok-jdot", _bundle())
            assert st == 403 and "쓰기 범위 밖" in r["error"], path
        assert sorted(p.name for p in (root / "hub-ops").iterdir()) == ["from-jdot-hq"]
        assert not (root / "letters").exists()          # 디렉터리조차 만들지 않는다
    finally:
        srv.shutdown()


def test_읽기_범위_밖은_403이고_문_목록은_읽을_수_있는_문만_보인다(tmp_path):
    srv, host, root, _ = _serve(tmp_path, [JDOT, LEGACY])
    try:
        for path in ("/v0/hub-ops/from-organum", "/v0/hub-ops/from-lxm", "/v0/letters/from-ludex",
                     "/v0/hub-ops/from-jdot-hq"):
            assert _req(host, "POST", path, "tok-legacy", _bundle())[0] == 200
        st, r = _req(host, "GET", "/v0/hub-ops/from-organum", "tok-jdot")
        assert st == 200 and [q["n"] for q in r["quads"]] == ["001"]
        for path in ("/v0/hub-ops/from-lxm", "/v0/letters/from-ludex", "/v0/hub-ops/from-jdot-hq"):
            st, r = _req(host, "GET", path, "tok-jdot")
            assert st == 403 and "읽기 범위 밖" in r["error"], path
        # 자기 쓰기 문도 read=에 없으면 목록에 없다 — 축은 서로 독립이다
        assert _req(host, "GET", "/v0/channels", "tok-jdot")[1]["channels"] == {
            "hub-ops": ["from-organum"]}
        assert _req(host, "GET", "/v0/channels", "tok-legacy")[1]["channels"] == {
            "hub-ops": ["from-jdot-hq", "from-lxm", "from-organum"], "letters": ["from-ludex"]}
    finally:
        srv.shutdown()


def test_적지_않은_축은_권한_없음이다(tmp_path):
    """Jdot HQ 검토: read=만 적은 토큰이 전체 쓰기를 얻으면 안 된다."""
    srv, host, root, _ = _serve(tmp_path, ["tok-r read=hub-ops/*", "tok-w write=*/from-w", LEGACY])
    try:
        assert _req(host, "POST", "/v0/hub-ops/from-w", "tok-w", _bundle())[0] == 200
        assert _req(host, "POST", "/v0/hub-ops/from-w", "tok-r", _bundle("002"))[0] == 403
        assert _req(host, "GET", "/v0/hub-ops/from-w", "tok-r")[0] == 200
        assert _req(host, "GET", "/v0/hub-ops/from-w", "tok-w")[0] == 403
        assert _req(host, "GET", "/v0/channels", "tok-w")[1]["channels"] == {}
    finally:
        srv.shutdown()


@pytest.mark.parametrize("pattern,ok,bad", [
    ("hub-ops/from-x", [("hub-ops", "from-x")], [("hub-ops", "from-y"), ("letters", "from-x")]),
    ("hub-ops/*", [("hub-ops", "from-x"), ("hub-ops", "from-y")], [("letters", "from-x")]),
    ("*/from-x", [("hub-ops", "from-x"), ("letters", "from-x")], [("hub-ops", "from-y")]),
    ("*", [("hub-ops", "from-x"), ("letters", "from-y")], []),
    ("hub-ops/from-x,letters/*", [("hub-ops", "from-x"), ("letters", "from-z")], [("hub-ops", "from-z")]),
])
def test_문_패턴_셋과_별(tmp_path, pattern, ok, bad):
    (tmp_path / "t.txt").write_text(f"tok write={pattern}\n", encoding="utf-8")
    e = hd.load_token_entries(tmp_path / "t.txt")[0]
    assert all(e.allows("write", ch, d) for ch, d in ok)
    assert not any(e.allows("write", ch, d) for ch, d in bad)
    assert e.read == () and not e.legacy


# ── 문법 오류 = 기동 거부 ─────────────────────────────────────────────────────

@pytest.mark.parametrize("lines", [
    ["s3cret-TOKEN write=hub-ops/fromx"],             # 문 이름 문법
    ["s3cret-TOKEN write=*/*"],                       # 전부는 `*` 하나로만
    ["s3cret-TOKEN write="],                          # 빈 값
    ["s3cret-TOKEN write= hub-ops/from-x"],           # = 뒤 공백 → 빈 값 + 낯선 조각
    ["s3cret-TOKEN scope=all"],                       # 모르는 필드
    ["s3cret-TOKEN id=Bad_Id"],                       # id 문법
    ["s3cret-TOKEN id=a id=b"],                       # 같은 필드 두 번
    ["s3cret-TOKEN read=* read=hub-ops/*"],
    ["s3cret-TOKEN", "s3cret-TOKEN id=x"],            # 같은 토큰 두 줄
    ["tok-a id=same", "tok-b id=same"],               # 같은 id 두 줄
    ["s3cret TOKEN with spaces"],                     # 공백 든 옛 토큰 — 조용히 받지 않는다
    ["s3cret-TOKEN revoked"],                         # 쓸 수 있는 토큰이 없다
])
def test_문법이_틀린_토큰_파일로는_서버가_뜨지_않고_오류에_토큰이_없다(tmp_path, lines):
    tok = tmp_path / "t.txt"
    tok.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ValueError) as e:
        hd.make_server(tmp_path / "r", tok, port=0)
    msg = str(e.value)
    for secret in ("s3cret", "TOKEN", "tok-a", "tok-b", "spaces"):
        assert secret not in msg, msg


def test_load_tokens는_범위_줄에서도_토큰_값만_준다(tmp_path):
    """클라이언트(push·pull·channels)는 줄의 첫 조각만 쓴다."""
    (tmp_path / "t.txt").write_text(f"# c\n{JDOT}\n{LEGACY}\n", encoding="utf-8")
    assert hd.load_tokens(tmp_path / "t.txt") == ["tok-jdot", "tok-legacy"]


# ── 순서: 401 → 429 → 404 → 403 ───────────────────────────────────────────────

def test_범위_밖_요청은_멤버의_요청이라_예산을_쓴다(tmp_path):
    srv, host, root, adir = _serve(tmp_path, [JDOT], rate=1)
    try:
        assert _req(host, "GET", "/v0/hub-ops/from-lxm", "nope")[0] == 401      # 예산 무소비
        assert _req(host, "GET", "/v0/hub-ops/from-lxm", "tok-jdot")[0] == 403  # 예산 1
        assert _req(host, "GET", "/v0/hub-ops/from-lxm", "tok-jdot")[0] == 429
        assert _req(host, "GET", "/v0/not-a-door", "tok-jdot")[0] == 429
        assert [l["status"] for l in _audit_lines(adir)] == [403, 429, 429]     # 401은 안 남는다
    finally:
        srv.shutdown()


# ── 감사 기록 ─────────────────────────────────────────────────────────────────

def test_감사_줄의_모양과_본문_토큰_부재(tmp_path):
    srv, host, root, adir = _serve(tmp_path, [JDOT, LEGACY])
    body = "비밀스러운 본문 SECRETBODY".encode("utf-8")
    try:
        b = _bundle(body_name="body.md", body=body)
        assert _req(host, "POST", "/v0/hub-ops/from-jdot-hq", "tok-jdot", b)[0] == 200
        assert _req(host, "POST", "/v0/hub-ops/from-jdot-hq", "tok-jdot", b)[0] == 200      # dedup
        other = _bundle(env=b'{"other":true}')
        assert _req(host, "POST", "/v0/hub-ops/from-jdot-hq", "tok-jdot", other)[0] == 409
        assert _req(host, "POST", "/v0/hub-ops/from-organum", "tok-jdot", b)[0] == 403
        assert _req(host, "POST", "/v0/hub-ops/from-organum", "tok-legacy", b)[0] == 200
        assert _req(host, "GET", "/v0/hub-ops/from-organum?since=000", "tok-jdot",
                    headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.1"})[0] == 200
        assert _req(host, "GET", "/v0/channels", "tok-jdot")[0] == 200
        assert _req(host, "GET", "/v0/hub-ops/from-organum", "wrong")[0] == 401
    finally:
        srv.shutdown()
    files = sorted(p.name for p in adir.iterdir())
    assert files == ["audit-20261004.jsonl"]                 # UTC 날짜별 파일
    raw = (adir / files[0]).read_text(encoding="utf-8")
    for leak in ("tok-jdot", "tok-legacy", "SECRETBODY", "wrong", "203.0.113.9"):
        assert leak not in raw
    L = _audit_lines(adir)
    assert len(L) == 7                                        # 401은 남지 않는다
    env_sha, other_sha = hashlib.sha256(ENV).hexdigest(), hashlib.sha256(b'{"other":true}').hexdigest()
    assert L[0] == {"utc": "2026-10-04T00:00:00Z", "id": "jdot-hq", "method": "POST", "status": 200,
                    "src": L[0]["src"], "channel": "hub-ops", "door": "from-jdot-hq", "n": "001",
                    "envelope_sha256": env_sha, "stored": True, "dedup": False}
    assert (L[1]["status"], L[1]["dedup"]) == (200, True)
    # 409로 거부된 시도는 내용이 남지 않는다 — 무엇을 올리려 했는지는 지문으로만 남는다(LxM 118)
    assert (L[2]["status"], L[2]["stored"], L[2]["envelope_sha256"]) == (409, False, other_sha)
    assert (L[3]["status"], L[3]["door"], L[3]["id"]) == (403, "from-organum", "jdot-hq")
    assert "n" not in L[3]                                    # 403은 요청 본문을 읽지 않는다
    assert L[4]["id"] == hashlib.sha256(b"tok-legacy").hexdigest()[:16]   # id 없는 줄 = 토큰 지문
    assert (L[5]["method"], L[5]["since"], L[5]["quads"]) == ("GET", "000", 1)
    assert L[5]["src"] == hashlib.sha256(b"203.0.113.9").hexdigest()[:16] != L[0]["src"]
    assert L[6]["op"] == "channels"


def test_폐기_줄은_401이고_예산을_먹지_않되_감사에_남는다(tmp_path):
    srv, host, root, adir = _serve(tmp_path, ["tok-old id=jdot-hq-old revoked", JDOT], rate=1)
    try:
        for _ in range(3):
            st, r = _req(host, "POST", "/v0/hub-ops/from-jdot-hq", "tok-old", _bundle())
            assert st == 401 and r == {"error": "bearer 토큰 필요"}    # 모르는 토큰과 같은 응답
        assert not root.exists()
        assert _req(host, "GET", "/v0/channels", "tok-jdot")[0] == 200  # 남의 예산에 영향 없음
    finally:
        srv.shutdown()
    L = _audit_lines(adir)
    assert [(l["id"], l["status"], l.get("revoked")) for l in L[:3]] == [("jdot-hq-old", 401, True)] * 3


def test_감사_디렉터리가_운반_트리_안이면_뜨지_않는다(tmp_path):
    (tmp_path / "t.txt").write_text("tok\n", encoding="utf-8")
    root = tmp_path / "drops"
    for bad in (root, root / "audit", root / "hub-ops" / "from-x"):
        with pytest.raises(ValueError, match="운반 트리"):
            hd.make_server(root, tmp_path / "t.txt", port=0, audit_dir=bad)


def test_감사_기록이_실패해도_저장과_응답은_그대로다(tmp_path):
    """Jdot HQ 검토: 부가 기록 실패가 저장된 전송을 실패로 보이게 하면, 보낸 쪽이 새 번호로 다시 보낸다."""
    srv, host, root, adir = _serve(tmp_path, [LEGACY])
    try:
        import shutil
        shutil.rmtree(adir)
        adir.write_text("디렉터리 자리에 파일", encoding="utf-8")      # 쓰기가 반드시 실패한다
        st, r = _req(host, "POST", "/v0/hub-ops/from-a", "tok-legacy", _bundle())
        assert (st, r) == (200, {"n": "001", "stored": True, "dedup": False})
        assert (root / "hub-ops" / "from-a" / "001-envelope.json").read_bytes() == ENV
        assert _req(host, "POST", "/v0/hub-ops/from-a", "tok-legacy", _bundle())[1]["dedup"] is True
    finally:
        srv.shutdown()


def test_감사를_켜지_않으면_아무것도_남기지_않는다(tmp_path):
    srv, host, root, adir = _serve(tmp_path, [LEGACY], audit=False)
    try:
        assert _req(host, "POST", "/v0/hub-ops/from-a", "tok-legacy", _bundle())[0] == 200
    finally:
        srv.shutdown()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["drops", "tokens.txt"]


# ── 6. 입력 닻 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bundle", [
    _bundle(sig=SIG + "\n"),
    _bundle(n="001\n"),
    _bundle(body_name="body.md\n", body=b"x"),
    _bundle(sig=SIG + " "),
    _bundle(n="001\r"),
])
def test_끝에_줄바꿈이_붙은_입력은_400이고_아무것도_쓰지_않는다(tmp_path, bundle):
    srv, host, root, _ = _serve(tmp_path, [LEGACY], audit=False)
    try:
        assert _req(host, "POST", "/v0/hub-ops/from-a", "tok-legacy", bundle)[0] == 400
        door = root / "hub-ops" / "from-a"
        assert not door.exists() or list(door.iterdir()) == []
        # 대조군: 같은 값에서 줄바꿈만 뺀 것은 저장되고, 재전송은 dedup으로 수렴한다
        good = _bundle()
        assert _req(host, "POST", "/v0/hub-ops/from-a", "tok-legacy", good)[1]["dedup"] is False
        assert _req(host, "POST", "/v0/hub-ops/from-a", "tok-legacy", good)[1]["dedup"] is True
    finally:
        srv.shutdown()


def test_since와_경로에도_끝_줄바꿈이_통하지_않는다(tmp_path):
    srv, host, root, _ = _serve(tmp_path, [LEGACY], audit=False)
    try:
        assert _req(host, "GET", "/v0/hub-ops/from-a?since=001%0A", "tok-legacy")[0] == 400
        assert _req(host, "GET", "/v0/hub-ops/from-a%0A", "tok-legacy")[0] == 404
    finally:
        srv.shutdown()


def test_수거는_서버가_준_n과_body_name의_끝_줄바꿈을_거부한다(tmp_path, monkeypatch):
    for q in ({"n": "001\n", "envelope_b64": "e30=", "sig": SIG},
              {"n": "001", "envelope_b64": "e30=", "sig": SIG, "body_name": "body.md\n", "body_b64": ""}):
        monkeypatch.setattr(hd, "fetch_page", lambda *a, _q=q, **k: {"quads": [_q], "more": False})
        with pytest.raises(hd.DropError) as e:
            hd.pull_quads("http://x/v0/hub-ops/from-a", "t", tmp_path / "dest", warmup=False)
        assert e.value.status == 502
    assert list((tmp_path / "dest").iterdir()) == []


def test_발신함_번호_스캔은_줄바꿈_든_파일_이름을_세지_않는다(tmp_path):
    from organum import hub_ops as ho
    assert ho.quad_number("003-envelope.json") == 3
    assert ho.quad_number("003\n-envelope.json") is None


# ── CLI 배선 ──────────────────────────────────────────────────────────────────

def _cli(args):
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    return subprocess.run(CLI + args, env=env, capture_output=True, text=True, timeout=120)


def test_CLI_check_tokens는_값_없이_id와_범위만_보인다(tmp_path):
    tok = tmp_path / "t.txt"
    tok.write_text(f"{LEGACY}\n{JDOT}\ntok-old id=old revoked\n", encoding="utf-8")
    r = _cli(["check-tokens", "--token-file", str(tok)])
    assert r.returncode == 0, r.stderr
    for secret in ("tok-legacy", "tok-jdot", "tok-old"):
        assert secret not in r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["ok"] is True and [e["mode"] for e in out["entries"]] == ["legacy", "scoped", "revoked"]
    assert out["entries"][1] == {"id": "jdot-hq", "mode": "scoped", "write": ["hub-ops/from-jdot-hq"],
                                 "read": ["hub-ops/from-organum"]}
    assert out["entries"][2]["write"] == [] and out["entries"][2]["read"] == []

    tok.write_text("s3cret-TOKEN wirte=hub-ops/from-x\n", encoding="utf-8")       # 오타
    r = _cli(["check-tokens", "--token-file", str(tok)])
    assert r.returncode != 0 and "1번째 줄" in r.stderr and "s3cret" not in r.stderr + r.stdout
