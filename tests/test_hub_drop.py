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
import re
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
    # 바이트는 다르되 **JSON으로는 읽히는** 위조 — 0.4.10 발신 문 게이트가 파싱
    # 불가를 먼저 거부하므로, 이 테스트가 겨냥한 서버 409에 실제로 도달하게 한다
    # (게이트는 문만 보고, 내용 판정은 여전히 수신 hub의 admit 몫이다).
    forged_env = json.loads(env0.decode("utf-8"))
    forged_env["payload"]["body_sha256"] = "0" * 64
    (forged / "001-envelope.json").write_bytes(
        json.dumps(forged_env, ensure_ascii=False).encode("utf-8"))
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


def test_클라이언트_timeout_인자_배선(drop, tmp_path):
    """0.4.2: push/pull이 timeout을 받는다. 기본값은 콜드스타트 **분포 관측** 위 —
    90("~1분" 실측)은 천장에 잘린 값들로 정한 수였고(Ray 061: 천장 400에서 완주
    207~248초), 0.4.17에서 관측 최대 위 300으로. 이 단언은 값 자체가 아니라
    "기본값이 관측된 완주 분포를 덮는다"는 성질을 고정한다."""
    url, token, root = drop
    assert hd.CLIENT_TIMEOUT_SECONDS == 300
    assert hd.CLIENT_TIMEOUT_SECONDS > 248, (
        "기본 예산이 관측된 콜드스타트 완주 최대(248s, Ray 061) 아래로 내려왔다 — "
        "내리려면 그 관측을 반박하는 새 분포가 있어야 한다")
    sdir = root / "ch" / "from-a"
    sdir.mkdir(parents=True)
    (sdir / "001-sig.txt").write_text("ab" * 64 + "\n")
    (sdir / "001-envelope.json").write_bytes(b'{"n":1}')
    got = hd.pull_quads(f"{url}/v0/ch/from-a", token, tmp_path / "in", timeout=5)
    assert got == ["001"]


def test_워밍업은_무인증이고_HTTPError를_생존으로_센다(drop):
    """[0.4.6 LxM 008–010 + Ludex 명세] ① 401/404는 '인스턴스가 섰다'는 증거라
    성공으로 센다(실패로 세면 토큰 틀린 멤버가 콜드스타트를 오진한다) ② 어떤 실패도
    예외로 새지 않는다 — 워밍업은 보험이지 게이트가 아니다."""
    url, _token, _root = drop
    assert hd._warm(f"{url}/v0/ch/from-a") is True          # 401이지만 생존
    assert hd._warm(f"{url}/v0/nope/../..") is True         # 404도 생존
    # 죽은 포트 — False를 돌려줄 뿐 예외를 던지지 않는다(본 호출을 못 죽인다)
    assert hd._warm("http://127.0.0.1:1/v0/ch/from-a", timeout=2) is False


def test_인증실패는_limiter_호출_전_반환_무토큰GET_후_잔여예산_불변(tmp_path):
    """[LxM 009/010 확정 문구 — 기전+관측] 앞 절이 기전, 뒤 절이 관측이다:
    게이트 순서를 바꾸면 기전에서 먼저 깨지므로 이 테스트가 증상이 아니라 원인을
    가리킨다. (0.4.1의 '401은 예산을 안 먹는다' 결정이 이제 기계가 된다 —
    무인증 워밍업이 공짜인 성질이 여기에 걸려 있다.)"""
    tok = tmp_path / "tokens.txt"
    tok.write_text("member-a\n", encoding="utf-8")
    srv = hd.make_server(tmp_path / "drops", tok, bind="127.0.0.1", port=0,
                         rate_limit_per_minute=2)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        host = f"127.0.0.1:{srv.server_address[1]}"

        def get(token=None):
            conn = http.client.HTTPConnection(host, timeout=10)
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            conn.request("GET", "/v0/ch/from-a", headers=headers)
            st = conn.getresponse().status
            conn.close()
            return st

        for _ in range(30):                       # 무토큰 GET 30연발 — 전부 401
            assert get() == 401
        assert get("member-a") == 200             # 예산 2 그대로
        assert get("member-a") == 200
        assert get("member-a") == 429             # 소비는 유효 토큰 2회뿐
    finally:
        srv.shutdown()


def test_channels_트리는_POST된_문만_보이고_문법이_거른다(drop, tmp_path):
    """[0.4.9 LxM 036 요청 — Ray 두-문 사고] "채널이 몇 개 있는가"는 서버만
    아는데 아무도 물을 수 없었다. 응답은 채널 목록이 아니라 channel/sender
    **트리**다 — pull URL이 /v0/<channel>/<from-x>라 수거기에 필요한 건 문
    목록이니까. 정직한 성질: 첫 봉투가 POST된 문만 보인다(디렉터리가 그때
    생기므로) — 약속된 채널이 아니라 실재하는 문."""
    url, token, root = drop
    tree_url = f"{url}/v0/channels"
    # 아무것도 POST되기 전 — 빈 트리(404가 아니라 정직한 빈 답)
    assert hd.list_channels(tree_url, token, warmup=False) == {"channels": {}}

    quad, _, _ = _make_quad(tmp_path)
    hd.push_quad(f"{url}/v0/hub-ops/from-ray", token, quad, warmup=False)
    hd.push_quad(f"{url}/v0/first-contact/from-ray", token, quad, warmup=False)
    # 손이 만든 규격 밖 잔재는 열거에서 걸러진다(POST 문법과 같은 필터)
    (root / "hub-ops" / "junk").mkdir()
    (root / ".hidden" / "from-x").mkdir(parents=True)
    (root / "empty-channel").mkdir()              # 문 없는 채널 — POST로는 불가능
    assert hd.list_channels(tree_url, token, warmup=False) == {
        "channels": {"first-contact": ["from-ray"], "hub-ops": ["from-ray"]}}

    # 토큰 소지자 전용 — 비멤버는 401
    with pytest.raises(hd.DropError) as e:
        hd.list_channels(tree_url, "wrong", warmup=False)
    assert e.value.status == 401


def test_channels는_예산을_먹고_같은_이름_채널과_충돌하지_않는다(tmp_path):
    """트리 조회도 멤버 콜이다(rate limit 예산 1 소비 — 회차당 1콜이라 무시
    가능하지만 공짜는 아니다). 그리고 `channels`는 정확히 2세그먼트 경로만
    예약이라, 같은 이름의 채널이 있어도 그 문(3세그먼트)은 그대로 산다."""
    tok = tmp_path / "tokens.txt"
    tok.write_text("member-a\n", encoding="utf-8")
    root = tmp_path / "drops"
    srv = hd.make_server(root, tok, bind="127.0.0.1", port=0,
                         rate_limit_per_minute=2)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        (root / "channels" / "from-a").mkdir(parents=True)   # 이름이 channels인 채널
        r = hd.list_channels(f"{base}/v0/channels", "member-a", warmup=False)
        assert r == {"channels": {"channels": ["from-a"]}}   # 예산 1
        assert hd.pull_quads(f"{base}/v0/channels/from-a", "member-a",
                             tmp_path / "in", warmup=False) == []  # 예산 2 — 문 생존
        with pytest.raises(hd.DropError) as e:
            hd.list_channels(f"{base}/v0/channels", "member-a", warmup=False)
        assert e.value.status == 429                          # 예산 소진 관측
    finally:
        srv.shutdown()


def test_quad_세_파일이_전부_바이너리로_쓰인다(drop, tmp_path, monkeypatch):
    """[0.4.14 — Ray 042, Windows 실측] `Path.write_text`는 텍스트 모드라 Windows에서
    `\\n`을 `\\r\\n`으로 번역한다. quad 셋 중 sig만 그렇게 쓰이고 있어서 **같은 봉투가
    랩마다 다른 바이트로 앉았다**(129B vs 130B 병합 충돌로 발견). 서명은 읽는 쪽이
    strip해서 통과하므로 조용했다.

    **이 결함은 POSIX에서 구조상 침묵이다** — `write_text`와 `write_bytes`가 같은
    바이트를 내므로 바이트를 단언하는 시험은 우리 집에서 공허하게 통과한다(egg-info
    교훈의 재판). 그래서 증상이 아니라 **성질**을 시험한다: quad 경로가 텍스트 모드
    쓰기를 아예 쓰지 않는다는 것. 이 단언은 모든 플랫폼에서 같은 것을 본다."""
    url, token, root = drop
    quad, _, _ = _make_quad(tmp_path)

    real_write_text = Path.write_text

    def _forbidden(self, *a, **kw):                 # quad 경로에서만 금지
        if re.match(r"^\d{3}-(envelope\.json|sig\.txt|body\.)", self.name):
            raise AssertionError(f"quad 파일이 텍스트 모드로 쓰였다: {self.name}")
        return real_write_text(self, *a, **kw)

    monkeypatch.setattr(Path, "write_text", _forbidden)
    # 서버 POST 경로(저장) · 클라 pull 경로(수신) 둘 다 quad를 물화한다
    hd.push_quad(f"{url}/v0/hub-ops/from-ray", token, quad, warmup=False)
    dest = tmp_path / "recv"
    assert hd.pull_quads(f"{url}/v0/hub-ops/from-ray", token, dest,
                         warmup=False) == ["001"]

    # 그리고 실제 바이트도 세 파일 전부 원본과 동일해야 한다(양쪽 트리 교차)
    for name in ["001-envelope.json", "001-sig.txt", "001-body.md"]:
        src = (quad.parent / name).read_bytes()
        assert (root / "hub-ops/from-ray" / name).read_bytes() == src, name
        assert (dest / name).read_bytes() == src, name
        assert b"\r" not in src, name


def test_워밍_예산은_본_호출_예산에서_파생된다(tmp_path):
    """[0.4.14 — Ray 045] 종전 두 상수(워밍 20s / 본 호출 90s)가 **같은 콜드스타트에
    대해 서로 다른 말**을 했다. 워밍이 막으라고 있는 상황이 정확히 워밍이 실패하는
    상황이었고, `_warm`이 설계대로 조용히 삼키므로 실패가 아무 데도 안 남았다
    (Ray 실측: 무인증 GET 401까지 55초).

    숫자를 올리는 대신 **결속**한다 — 다음 사람이 주석을 안 읽어도 두 줄이 어긋날 수
    없도록. 이 시험은 상수 두 줄만 읽어도 참이라 네트워크가 필요 없다."""
    assert hd.WARMUP_TIMEOUT_SECONDS == hd.CLIENT_TIMEOUT_SECONDS, (
        "워밍 예산이 본 호출 예산과 갈라졌다 — 워밍은 본 호출을 살리려고 있으므로 "
        "두 예산이 독립이면 같은 결함이 다시 선다")
    # 결속의 물증: 본 호출 예산을 바꾸면 워밍도 따라 움직인다(파생이지 우연이 아님)
    src = (Path(hd.__file__).read_text(encoding="utf-8"))
    assert "WARMUP_TIMEOUT_SECONDS = CLIENT_TIMEOUT_SECONDS" in src
    # 그래도 워밍은 게이트가 아니다 — 실패해도 예외가 새지 않는다(0.4.6 성질 보존)
    assert hd._warm("http://127.0.0.1:1/v0/ch/from-a", timeout=1) is False


def test_남의_문에는_기본_거부_네트워크_접촉_전에(tmp_path):
    """[0.4.10 실사고 산물 — 2026-08-26] 나는 우리 서명 봉투를 from-ludex·from-ray에
    POST했다. 서버는 dumb carrier라 막지 않았고(설계된 성질), append-only라 철회도
    못 했다. **서버가 막지 않는다는 것과 해도 된다는 것은 다르다** — 그 사이를
    규율이 메우고 있었으므로 기계로 옮긴다.

    죽은 포트를 겨냥해도 ValueError가 나는 것이 증거다: 게이트는 **어떤 요청도
    보내기 전에** 판정한다(워밍 GET조차 나가지 않는다)."""
    quad, _, _ = _make_quad(tmp_path)                    # 서명자 lab:ray
    dead = "http://127.0.0.1:1/v0/hub-ops"
    with pytest.raises(ValueError, match="남의 문"):
        hd.push_quad(f"{dead}/from-ludex", "tok", quad)
    # 자기 문은 게이트를 통과해 네트워크로 나간다(죽은 포트라 그 뒤에서 실패)
    with pytest.raises(Exception) as e:
        hd.push_quad(f"{dead}/from-ray", "tok", quad, timeout=2)
    assert "남의 문" not in str(e.value)


def test_발신_문_게이트_성공조건_열거_파생불가와_파싱불가는_fail_closed(tmp_path):
    """게이트는 성공 조건 명시-나열형으로만(fail-open 3연발의 교훈). 넷 중 하나라도
    안 서면 거부다 — 특히 **판정이 불가능한 경우가 곧 통과가 되면 안 된다**."""
    quad, _, lab = _make_quad(tmp_path)
    env0 = (quad.parent / "001-envelope.json").read_bytes()
    dead = "http://127.0.0.1:1/v0/hub-ops/from-ray"

    # ① URL이 문 꼴이 아니면 거부(판정 불가 = 거부)
    with pytest.raises(ValueError, match="판정할 수 없어"):
        hd.push_quad("http://127.0.0.1:1/v0/channels", "tok", quad)
    # ② 봉투가 JSON이 아니면 거부
    bad = lab / "unparseable"
    bad.mkdir()
    (bad / "001-envelope.json").write_bytes(env0[:-1] + b" ")
    (bad / "001-sig.txt").write_bytes((quad.parent / "001-sig.txt").read_bytes())
    with pytest.raises(ValueError, match="JSON으로 읽을 수 없어"):
        hd.push_quad(dead, "tok", bad / "001")
    # ③ signer에서 문 이름이 파생 안 되면 거부(lab 문법이 sender 문법보다 넓다)
    assert hd._door_for_signer("lab:ray") == "from-ray"
    assert hd._door_for_signer("lab:ludex-village") == "from-ludex-village"
    for undelivered in ["lab:a_b", "lab:a.b", "ray", None, "lab:"]:
        assert hd._door_for_signer(undelivered) is None
    odd = lab / "odd"
    odd.mkdir()
    env_odd = json.loads(env0.decode("utf-8"))
    env_odd["signer"]["id"] = "lab:a_b"
    (odd / "001-envelope.json").write_bytes(
        json.dumps(env_odd, ensure_ascii=False).encode("utf-8"))
    (odd / "001-sig.txt").write_bytes((quad.parent / "001-sig.txt").read_bytes())
    with pytest.raises(ValueError, match="파생할 수 없어"):
        hd.push_quad(dead, "tok", odd / "001")


def test_대리_전달은_명시로_열린다(drop, tmp_path):
    """정당한 대리 전달까지 막으면 새 실패 경로를 만드는 셈이다 — 0.4.5가 회람 증인
    수용을 `--accept-foreign-target`으로 연 것과 같은 모양으로, 명시하면 열린다.
    (실수가 아니라 결정이 되도록 하는 것이 게이트의 목적이다.)"""
    url, token, root = drop
    quad, _, _ = _make_quad(tmp_path)                    # 서명자 lab:ray
    with pytest.raises(ValueError, match="남의 문"):
        hd.push_quad(f"{url}/v0/hub-ops/from-ludex", token, quad, warmup=False)
    r = hd.push_quad(f"{url}/v0/hub-ops/from-ludex", token, quad, warmup=False,
                     allow_foreign_door=True)
    assert r == {"n": "001", "stored": True, "dedup": False}
    assert (root / "hub-ops/from-ludex/001-envelope.json").is_file()


def test_CLI_push_문_게이트_배선(drop, tmp_path):
    url, token, _root = drop
    quad, _, _ = _make_quad(tmp_path)
    tokf = tmp_path / "one-token.txt"
    tokf.write_text(token + "\n", encoding="utf-8")
    args = ["push", "--url", f"{url}/v0/hub-ops/from-ludex", "--quad", str(quad),
            "--token-file", str(tokf)]
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    r = subprocess.run(CLI + args, cwd=tmp_path, env=env, capture_output=True,
                       text=True, timeout=120)
    assert r.returncode != 0 and "남의 문" in r.stderr
    ok = _run(args + ["--accept-foreign-door"], tmp_path)
    assert ok["stored"] is True


def test_호출별_timeout이_워밍_단계까지_관통한다(tmp_path, monkeypatch):
    """[0.4.16 — Orin 026] 0.4.14가 워밍 예산을 본 호출 예산에 결속했는데 그건
    **기본값 둘**의 결속이었고, 호출별 override는 본 요청에만 닿았다. 그래서
    `--timeout 7`이 "전체 7초"가 아니라 **"워밍 최대 90초 + 본 요청 7초"**였다.

    예산을 좁히는 쪽은 대개 급한 쪽인데 그 사람이 정확히 못 받고 있었다.
    세 public 경로(push·pull·channels) 전부에서 고정한다 — 하나만 고치면 형제가
    남는다는 것이 이번 주의 반복 교훈이다."""
    seen: dict = {}
    monkeypatch.setattr(hd, "_warm",
                        lambda url, timeout=hd.WARMUP_TIMEOUT_SECONDS:
                        seen.__setitem__("warm", timeout) or False)
    monkeypatch.setattr(hd, "_request",
                        lambda url, token, data=None,
                        timeout=hd.CLIENT_TIMEOUT_SECONDS:
                        seen.__setitem__("req", timeout) or
                        {"quads": [], "more": False, "channels": {},
                         "n": "001", "stored": True, "dedup": False})
    quad, _, _ = _make_quad(tmp_path)
    url = "http://example.invalid/v0/hub-ops/from-ray"

    for label, call in [
        ("pull", lambda: hd.pull_quads(url, "t", tmp_path / "d", since="000",
                                       timeout=7)),
        ("push", lambda: hd.push_quad(url, "t", quad, timeout=7)),
        ("channels", lambda: hd.list_channels("http://example.invalid/v0/channels",
                                              "t", timeout=7)),
    ]:
        seen.clear()
        call()
        assert seen.get("warm") == 7, f"{label}: 워밍이 호출 예산을 못 받았다"
        assert seen.get("req") == 7, f"{label}: 본 요청이 호출 예산을 못 받았다"

    # 인자를 안 주면 결속된 기본값 그대로(0.4.14 성질 보존)
    seen.clear()
    hd.pull_quads(url, "t", tmp_path / "d2", since="000")
    assert seen["warm"] == hd.WARMUP_TIMEOUT_SECONDS == hd.CLIENT_TIMEOUT_SECONDS


def test_CLI_no_warmup이_워밍을_실제로_끈다(drop, tmp_path):
    """[0.4.15 — Ray 055] 0.4.14가 워밍 예산을 본 호출 예산에 결속한 뒤, **문마다
    워밍하는 클라이언트**는 호스트가 죽은 날 `문 수 × (워밍+본 호출)`을 문다.
    라이브러리엔 `warmup=False`가 있었지만 **CLI가 그걸 노출하지 않아** CLI 소비자
    (우리 수거기 포함)는 회차당 1회 워밍으로 갈 수 없었다 — 계약이 아니라 표면의 구멍.

    선언이 아니라 관측으로 고정한다: `warm_ms`/`warm_ok`가 결과에 실리는 성질을
    이용해, 플래그가 있으면 그 키가 **아예 나오지 않는** 것을 본다."""
    url, token, root = drop
    quad, _, _ = _make_quad(tmp_path)
    tokf = tmp_path / "one-token.txt"
    tokf.write_text(token + "\n", encoding="utf-8")
    post = f"{url}/v0/hub-ops/from-ray"

    r = _run(["push", "--url", post, "--quad", str(quad),
              "--token-file", str(tokf), "--no-warmup"], tmp_path)
    assert r["stored"] is True
    assert "warm_ms" not in r and "warm_ok" not in r      # 워밍이 아예 안 돌았다

    r = _run(["pull", "--url", post, "--dest", str(tmp_path / "in"),
              "--token-file", str(tokf), "--no-warmup"], tmp_path)
    assert r["pulled"] == ["001"] and "warm_ms" not in r

    r = _run(["channels", "--url", f"{url}/v0/channels",
              "--token-file", str(tokf), "--no-warmup"], tmp_path)
    assert r["channels"] == {"hub-ops": ["from-ray"]} and "warm_ms" not in r

    # 플래그 없이는 종전대로 워밍이 돈다(기본값을 안 바꿨다는 물증)
    r = _run(["channels", "--url", f"{url}/v0/channels",
              "--token-file", str(tokf)], tmp_path)
    assert "warm_ms" in r and "warm_ok" in r


def test_CLI_channels_verb_배선(drop, tmp_path):
    url, token, _root = drop
    quad, _, _ = _make_quad(tmp_path)
    hd.push_quad(f"{url}/v0/hub-ops/from-ray", token, quad, warmup=False)
    tokf = tmp_path / "one-token.txt"
    tokf.write_text(token + "\n", encoding="utf-8")
    r = _run(["channels", "--url", f"{url}/v0/channels",
              "--token-file", str(tokf)], tmp_path)
    assert r["channels"] == {"hub-ops": ["from-ray"]}
    assert "warm_ms" in r and "warm_ok" in r      # push/pull과 같은 워밍 계측
