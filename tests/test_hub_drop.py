"""hub drop v0 회귀 — git 없는 전달이 git 우체통과 byte-exact 등가임을 고정한다.

핵심 계약 셋:
1. 서버는 같은 `from-x/NNN-*` 트리를 물화한다(수신 어댑터 무변경의 근거).
2. 서버는 dumb하다 — 봉투 검증은 수신 hub의 admit이 하고, 실제로 admit이 성공한다.
3. 접근은 토큰이 지고(401), 같은 n 재전송은 dedup 수렴, 다른 내용은 409(먼저 쓴
   것이 남는다), 경로·크기 위생은 400/413, 토큰별 rate limit 초과는 429(+Retry-After)
   — hosted(gated) 티어의 비용 유계.
"""

import base64
import http.client
import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
from organum import hub_drop as hd  # noqa: E402

CLI = [sys.executable, "-m", "organum.hub_cli"]


def _run(args, cwd):
    env = {**os.environ, "PYTHONPATH": str(SRC)}   # cwd가 달라져도 절대경로로
    r = subprocess.run(CLI + args, cwd=cwd, env=env, capture_output=True,
                       text=True, timeout=120)
    assert r.returncode == 0, f"CLI 실패 {args}: {r.stderr}"
    return json.loads(r.stdout) if r.stdout.strip().startswith("{") else r.stdout


@pytest.fixture
def drop(tmp_path):
    """단일 스레드 drop 서버를 스레드에 올려 (base_url, token, root)를 준다."""
    tok = tmp_path / "tokens.txt"
    tok.write_text("# 채널 토큰\nsecret-t0ken\n", encoding="utf-8")
    root = tmp_path / "drops"
    srv = hd.make_server(root, tok, bind="127.0.0.1", port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield (f"http://127.0.0.1:{srv.server_address[1]}", "secret-t0ken", root)
    srv.shutdown()


def _make_quad(tmp_path, out_name="from-ray"):
    """실제 CLI 루프로 진짜 quad를 만든다(합성 아님): keygen→init→register→
    message→export. (quad_prefix, 발신 pubkey, 발신 dir) 반환."""
    lab = tmp_path / "raylab"
    lab.mkdir()
    _run(["keygen", "ray"], lab)
    pub = (lab / "ray.pub").read_text().strip()
    _run(["init", "--dir", "hub", "--source-domain", "lab:ray/hub"], lab)
    _run(["register-key", "--dir", "hub", "--signer", "lab:ray", "--key-id", "k1",
          "--epoch", "1", "--pubkey", pub], lab)
    (lab / "greeting.md").write_text("안녕, Aria — 첫 봉투다.\n", encoding="utf-8")
    _run(["message", "--dir", "hub", "--key", "ray.seed", "--signer", "lab:ray",
          "--key-id", "k1", "--epoch", "1", "--to-lab", "lab:ludex",
          "--to-id", "Aria", "--to-epoch", "1", "--body-file", "greeting.md"], lab)
    _run(["export", "--dir", "hub", "--out", out_name, "--body", "greeting.md"], lab)
    return lab / out_name / "001", pub, lab


def test_왕복이_byte_exact이고_수신_hub가_admit한다(drop, tmp_path):
    url, token, root = drop
    quad, pub, lab = _make_quad(tmp_path)
    r = hd.push_quad(f"{url}/v0/first-contact/from-ray", token, quad)
    assert r == {"n": "001", "stored": True, "dedup": False}

    # 계약 1 — 서버 트리가 git 우체통과 같은 모양·같은 바이트
    sdir = root / "first-contact" / "from-ray"
    for name in ["001-envelope.json", "001-sig.txt", "001-body.md"]:
        assert (sdir / name).read_bytes() == (quad.parent / name).read_bytes()

    # pull → 로컬 트리도 byte-exact
    dest = tmp_path / "recv" / "from-ray"
    assert hd.pull_quads(f"{url}/v0/first-contact/from-ray", token, dest) == ["001"]
    for name in ["001-envelope.json", "001-sig.txt", "001-body.md"]:
        assert (dest / name).read_bytes() == (quad.parent / name).read_bytes()

    # 계약 2 — 검증은 수신 hub가: 별도 hub가 pubkey만으로 admit 성공
    recv = tmp_path / "recv"
    _run(["init", "--dir", "hub", "--source-domain", "lab:ludex/hub"], recv)
    _run(["register-key", "--dir", "hub", "--signer", "lab:ray", "--key-id", "k1",
          "--epoch", "1", "--pubkey", pub], recv)
    r = _run(["admit", "--dir", "hub", "--envelope", "from-ray/001-envelope.json",
              "--sig-file", "from-ray/001-sig.txt", "--pubkey", pub], recv)
    assert r["admitted"] is True and r["accepted_seq"] == 1


def test_토큰_없거나_틀리면_401이고_아무것도_안_쓴다(drop, tmp_path):
    url, _, root = drop
    quad, _, _ = _make_quad(tmp_path)
    for bad in ["wrong", ""]:
        with pytest.raises(hd.DropError) as e:
            hd.push_quad(f"{url}/v0/first-contact/from-ray", bad, quad)
        assert e.value.status == 401
    assert not (root / "first-contact").exists()


def test_같은_n_재전송은_dedup_수렴_다른_내용은_409_먼저_쓴_것이_남는다(drop, tmp_path):
    url, token, root = drop
    quad, _, lab = _make_quad(tmp_path)
    post = f"{url}/v0/first-contact/from-ray"
    hd.push_quad(post, token, quad)
    assert hd.push_quad(post, token, quad)["dedup"] is True  # 재전송 수렴

    forged = lab / "forged"
    forged.mkdir()
    env0 = (quad.parent / "001-envelope.json").read_bytes()
    (forged / "001-envelope.json").write_bytes(env0[:-1] + b" ")
    (forged / "001-sig.txt").write_bytes((quad.parent / "001-sig.txt").read_bytes())
    with pytest.raises(hd.DropError) as e:
        hd.push_quad(post, token, forged / "001")
    assert e.value.status == 409
    assert (root / "first-contact/from-ray/001-envelope.json").read_bytes() == env0


def test_경로_위생_규격_밖_채널과_트래버설은_404(drop):
    url, token, _ = drop
    host = url[len("http://"):]
    for path in ["/v0/first-contact/notfrom-x",     # from- 접두 아님
                 "/v0/First..Contact/from-ray",     # 규격 밖 채널
                 "/v0/first-contact/from-ray/../..",
                 "/v1/first-contact/from-ray"]:
        conn = http.client.HTTPConnection(host, timeout=10)
        conn.request("GET", path, headers={"Authorization": f"Bearer {token}"})
        assert conn.getresponse().status == 404, path
        conn.close()


def test_크기_캡_봉투_초과는_400_요청_초과는_413(drop):
    url, token, _ = drop
    host = url[len("http://"):]
    big_env = base64.b64encode(b"x" * (hd.ENVELOPE_MAX_BYTES + 1)).decode()
    bundle = json.dumps({"n": "001", "envelope_b64": big_env, "sig": "a" * 128})
    conn = http.client.HTTPConnection(host, timeout=10)
    conn.request("POST", "/v0/c/from-x", body=bundle,
                 headers={"Authorization": f"Bearer {token}"})
    assert conn.getresponse().status == 400
    conn.close()
    # 요청 초과: 서버는 본문을 읽지 않고 즉시 거절한다(스트리밍 낭비 금지) —
    # 그래서 raw socket으로 헤더만 보내고 응답을 읽는다
    ip, port = host.rsplit(":", 1)
    s = socket.create_connection((ip, int(port)), timeout=10)
    s.sendall((f"POST /v0/c/from-x HTTP/1.1\r\nHost: {host}\r\n"
               f"Authorization: Bearer {token}\r\n"
               f"Content-Length: {hd.REQUEST_MAX_BYTES + 1}\r\n\r\n").encode())
    assert b" 413 " in s.recv(4096)
    s.close()


def test_since_증분과_로컬_자동_since_이미_받은_것은_안_덮는다(drop, tmp_path):
    url, token, root = drop
    sdir = root / "ch" / "from-a"
    sdir.mkdir(parents=True)
    for i in [1, 2, 3]:
        (sdir / f"{i:03d}-sig.txt").write_text("ab" * 64 + "\n")
        (sdir / f"{i:03d}-envelope.json").write_bytes(b'{"n":%d}' % i)
    get = f"{url}/v0/ch/from-a"
    dest = tmp_path / "in"
    assert hd.pull_quads(get, token, dest, since="001") == ["002", "003"]
    marker = b"local-must-survive"
    (dest / "002-envelope.json").write_bytes(marker)
    assert hd.pull_quads(get, token, dest) == []            # 자동 since = 003
    assert hd.pull_quads(get, token, dest, since="000") == ["001", "002", "003"]
    assert (dest / "002-envelope.json").read_bytes() == marker  # 덮어쓰기 없음


def test_미완성_quad는_목록에_나오지_않는다(drop, tmp_path):
    url, token, root = drop
    sdir = root / "ch" / "from-a"
    sdir.mkdir(parents=True)
    (sdir / "001-sig.txt").write_text("ab" * 64 + "\n")      # envelope 없음 = 미완성
    (sdir / "002-envelope.json").write_bytes(b"")            # 빈 envelope = 미완성
    (sdir / "003-sig.txt").write_text("cd" * 64 + "\n")
    (sdir / "003-envelope.json").write_bytes(b'{"ok":3}')
    assert hd.pull_quads(f"{url}/v0/ch/from-a", token, tmp_path / "in") == ["003"]


def test_빈_토큰_파일은_서버를_열지_않는다(tmp_path):
    tok = tmp_path / "empty.txt"
    tok.write_text("# 주석뿐\n", encoding="utf-8")
    with pytest.raises(ValueError, match="열린 우체통"):
        hd.make_server(tmp_path / "r", tok, port=0)


def test_rate_limiter_고정_창_회전과_끔():
    now = [0.0]
    rl = hd.RateLimiter(2, clock=lambda: now[0])
    assert rl.check("t") is None and rl.check("t") is None   # 예산 2 소비
    assert rl.check("t") == 60                               # 창 첫머리 초과 → 60초
    now[0] = 59.0
    assert rl.check("t") == 1                                # 창 끝머리 → 1초
    now[0] = 60.0
    assert rl.check("t") is None                             # 창 회전 → 예산 복원
    assert rl.check("다른멤버") is None                      # 키는 토큰별
    assert hd.RateLimiter(0).check("t") is None              # 0 = 끔(self-host P2P)


def test_rate_limit_초과는_429_Retry_After_401은_예산을_안_먹고_토큰별_독립(tmp_path):
    tok = tmp_path / "tokens.txt"
    tok.write_text("member-a\nmember-b\n", encoding="utf-8")
    srv = hd.make_server(tmp_path / "drops", tok, bind="127.0.0.1", port=0,
                         rate_limit_per_minute=2)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        host = f"127.0.0.1:{srv.server_address[1]}"

        def get(token):
            conn = http.client.HTTPConnection(host, timeout=10)
            conn.request("GET", "/v0/ch/from-a",
                         headers={"Authorization": f"Bearer {token}"})
            resp = conn.getresponse()
            resp.read()
            retry = resp.getheader("Retry-After")
            conn.close()
            return resp.status, retry

        assert get("member-a")[0] == 200
        assert get("wrong")[0] == 401                # 비멤버는 예산과 무관
        assert get("member-a")[0] == 200             # 401이 예산을 안 먹은 증거
        status, retry = get("member-a")
        assert status == 429
        assert retry is not None and 1 <= int(retry) <= 60
        assert get("member-b")[0] == 200             # 다른 멤버는 독립 예산
    finally:
        srv.shutdown()
