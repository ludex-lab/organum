"""organum-hub drop — git 없는 저마찰 전달: 최소 HTTP 우체통 (v0).

첫 접촉 실험(2026-08-16)의 수확에서 나왔다: git이 transport로서 하던 일 셋
(운반·순서·접근권) 중 순서(seq)와 귀속(서명)은 봉투가 이미 지므로, carrier에
남은 요구는 **dumb 바이트 운반 + 목록 조회**뿐이다. 그 이상을 서버에 두지 않는다.

## 신뢰 분담 (정직하게)

- 위조·변조·순서 방지 = **봉투**(서명·seq·digest). 서버는 봉투를 열지도 검증하지도
  않는다 — 검증은 언제나 수신 hub의 admit이 한다. 서버를 신뢰할 필요가 없다.
- 쓰기 접근 = **bearer 토큰**(스팸·낙서 방지일 뿐, 신뢰가 아니다).
- 읽기 기밀 = **배치**(사설망/TLS/토큰) — 사설 git repo와 같은 노출 등급.
  공개 relay가 아니므로 wire v1의 lab-only 동결 경계와 충돌하지 않는다.

## 설계점: 같은 트리를 물화한다

서버 저장소는 git 우체통과 **동일한** `<channel>/from-x/NNN-{envelope.json,sig.txt,
body.*}` 트리다. 파일이 git pull 대신 HTTP로 도착할 뿐이라, 수신 어댑터는
transport_root 스왑만으로 무변경 동작한다.

## 프로토콜 — organum-hub/drop/v0

- `POST /v0/<channel>/<from-x>`  body = {"n","envelope_b64","sig"[,"body_name",
  "body_b64"]} → 200 {"n","stored","dedup"} · 409(같은 n 다른 내용) · 400/401/413
- `GET  /v0/<channel>/<from-x>?since=NNN` → 200 {"quads":[…], "more"} (n 오름차순,
  페이지 20; envelope가 마지막에 쓰이므로 미완성 quad는 목록에 나오지 않는다)
- 인증: `Authorization: Bearer <token>` (토큰 파일 한 줄 하나, `#` 주석)
- 토큰(=멤버)별 rate limit: 초과는 429 + `Retry-After` 초. hosted(gated) 티어의
  비용 유계 조건 — 인증 실패(401)는 멤버가 아니므로 예산을 먹지 않고, 인증 전
  플러드 방어는 배치 층(에지/방화벽) 몫이다.

서버는 의도적으로 **단일 스레드**다(요청 직렬화 → 쓰기 경쟁 0). 랩 규모
저빈도 전달이 대상이고, 상주 부담은 프로세스 하나 — 내리면 그만이다.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

DROP_PROFILE = "organum-hub/drop/v0"
ENVELOPE_MAX_BYTES = 65536          # hub_wire CONTENT_MAX_BYTES와 같은 값
BODY_MAX_BYTES = 1_048_576
REQUEST_MAX_BYTES = 2 * 1_048_576
PAGE_SIZE = 20
RATE_LIMIT_PER_MINUTE = 60          # 토큰별 기본값 — 0이면 끔(self-host P2P용)
_WINDOW_SECONDS = 60.0

_CHANNEL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SENDER_RE = re.compile(r"^from-[a-z0-9][a-z0-9-]{0,63}$")
_N_RE = re.compile(r"^[0-9]{3,6}$")
_SIG_RE = re.compile(r"^[0-9a-f]{128}$")
_BODY_NAME_RE = re.compile(r"^body\.[a-z0-9]{1,8}$")


class DropError(Exception):
    """drop 클라이언트 실패 — 서버 상태코드와 본문을 담는다."""

    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


def load_tokens(path: str | Path) -> list[str]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    toks = [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]
    if not toks:
        raise ValueError(f"토큰 파일이 비어 있다: {path} — 열린 우체통은 만들지 않는다")
    return toks


def _token_match(tokens: list[str], header: str | None) -> str | None:
    """일치한 토큰을 돌려준다(없으면 None) — rate limit이 멤버 단위로 키를 잡도록."""
    if not header or not header.startswith("Bearer "):
        return None
    given = header[len("Bearer "):].strip()
    for t in tokens:
        if hmac.compare_digest(given, t):
            return t
    return None


class RateLimiter:
    """토큰(=멤버)별 고정 창 카운터. per_minute<=0이면 끔.

    단일 스레드 서버 전제의 in-memory 카운터다 — 프로세스 재시작이면 창도
    리셋된다(비용 유계가 목적이지 정밀 계량이 아니다). 키는 토큰의 sha256
    접두라 자료구조에 비밀 원문을 한 벌 더 들고 있지 않는다."""

    def __init__(self, per_minute: int, clock=time.monotonic):
        self.per_minute = per_minute
        self._clock = clock
        self._windows: dict[str, tuple[float, int]] = {}

    def check(self, token: str) -> int | None:
        """허용이면 None(예산 1 소비), 초과면 Retry-After 초(1..60)."""
        if self.per_minute <= 0:
            return None
        key = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
        now = self._clock()
        start, count = self._windows.get(key, (now, 0))
        if now - start >= _WINDOW_SECONDS:
            start, count = now, 0
        if count >= self.per_minute:
            return max(1, math.ceil(_WINDOW_SECONDS - (now - start)))
        self._windows[key] = (start, count + 1)
        return None


def _quad_files(dirp: Path, n: str) -> dict | None:
    """완성 quad만 번들로. envelope는 마지막에 쓰이므로 존재+비어있지 않음 = 완성."""
    env_p = dirp / f"{n}-envelope.json"
    sig_p = dirp / f"{n}-sig.txt"
    if not (env_p.is_file() and sig_p.is_file()):
        return None
    env_b = env_p.read_bytes()
    if not env_b:
        return None
    bundle = {"n": n,
              "envelope_b64": base64.b64encode(env_b).decode("ascii"),
              "sig": sig_p.read_text(encoding="utf-8").strip(),
              "body_name": None, "body_b64": None}
    bodies = sorted(dirp.glob(f"{n}-body.*"))
    if bodies:
        bundle["body_name"] = bodies[0].name[len(n) + 1:]
        bundle["body_b64"] = base64.b64encode(bodies[0].read_bytes()).decode("ascii")
    return bundle


def _validate_bundle(obj) -> tuple[str, bytes, str, str | None, bytes | None]:
    """POST 본문 검증 — 모양과 크기만. 봉투 내용 검증은 서버의 일이 아니다."""
    if not isinstance(obj, dict):
        raise ValueError("본문은 JSON 객체")
    n = obj.get("n")
    if not (isinstance(n, str) and _N_RE.match(n)):
        raise ValueError("n은 3~6자리 숫자 문자열")
    sig = obj.get("sig")
    if not (isinstance(sig, str) and _SIG_RE.match(sig)):
        raise ValueError("sig는 hex128")
    try:
        env_b = base64.b64decode(obj.get("envelope_b64", ""), validate=True)
    except Exception:
        raise ValueError("envelope_b64 디코드 실패")
    if not env_b or len(env_b) > ENVELOPE_MAX_BYTES:
        raise ValueError(f"envelope는 1..{ENVELOPE_MAX_BYTES} 바이트")
    body_name, body_b = obj.get("body_name"), None
    if body_name is not None:
        if not (isinstance(body_name, str) and _BODY_NAME_RE.match(body_name)):
            raise ValueError("body_name은 body.<ext> 꼴")
        try:
            body_b = base64.b64decode(obj.get("body_b64", ""), validate=True)
        except Exception:
            raise ValueError("body_b64 디코드 실패")
        if len(body_b) > BODY_MAX_BYTES:
            raise ValueError(f"body는 {BODY_MAX_BYTES} 바이트 이하")
    return n, env_b, sig, body_name, body_b


def _split_path(path: str) -> tuple[str, str] | None:
    parts = [p for p in path.split("/") if p]
    if len(parts) != 3 or parts[0] != "v0":
        return None
    channel, sender = parts[1], parts[2]
    if not (_CHANNEL_RE.match(channel) and _SENDER_RE.match(sender)):
        return None
    return channel, sender


class _DropHandler(BaseHTTPRequestHandler):
    server_version = "organum-hub-drop/0"
    root: Path
    tokens: list[str]
    limiter: RateLimiter

    def _send(self, status: int, obj: dict, headers: dict | None = None):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _gate(self) -> tuple[str, str] | None:
        """인증 → rate limit → 경로 순. 실패 시 응답까지 보내고 None."""
        token = _token_match(self.tokens, self.headers.get("Authorization"))
        if token is None:
            self._send(401, {"error": "bearer 토큰 필요"})
            return None
        retry = self.limiter.check(token)
        if retry is not None:
            self._send(429, {"error": f"rate limit — {retry}초 뒤에"},
                       headers={"Retry-After": str(retry)})
            return None
        path = self.path.split("?", 1)[0]
        loc = _split_path(path)
        if loc is None:
            self._send(404, {"error": "경로는 /v0/<channel>/<from-x>"})
            return None
        return loc

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler 계약
        loc = self._gate()
        if loc is None:
            return
        since = "000"
        if "?" in self.path:
            q = dict(p.split("=", 1) for p in
                     self.path.split("?", 1)[1].split("&") if "=" in p)
            since = q.get("since", "000")
            if not _N_RE.match(since) and since != "000":
                self._send(400, {"error": "since는 3~6자리 숫자"})
                return
        dirp = self.root / loc[0] / loc[1]
        ns = []
        if dirp.is_dir():
            ns = sorted({f.name.split("-", 1)[0] for f in dirp.glob("*-envelope.json")
                         if _N_RE.match(f.name.split("-", 1)[0])}, key=int)
        fresh = [n for n in ns if int(n) > int(since)]
        quads = [b for n in fresh[:PAGE_SIZE] if (b := _quad_files(dirp, n))]
        self._send(200, {"quads": quads, "more": len(fresh) > PAGE_SIZE})

    def do_POST(self):  # noqa: N802
        loc = self._gate()
        if loc is None:
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > REQUEST_MAX_BYTES:
            self._send(413, {"error": f"요청은 1..{REQUEST_MAX_BYTES} 바이트"})
            return
        try:
            n, env_b, sig, body_name, body_b = _validate_bundle(
                json.loads(self.rfile.read(length).decode("utf-8")))
        except (ValueError, UnicodeDecodeError) as e:
            self._send(400, {"error": str(e)})
            return
        dirp = self.root / loc[0] / loc[1]
        dirp.mkdir(parents=True, exist_ok=True)
        env_p = dirp / f"{n}-envelope.json"
        if env_p.is_file() and env_p.read_bytes():
            prior = _quad_files(dirp, n)
            same = (prior is not None
                    and base64.b64decode(prior["envelope_b64"]) == env_b
                    and prior["sig"] == sig and prior["body_name"] == body_name
                    and (body_b is None or
                         base64.b64decode(prior["body_b64"] or "") == body_b))
            if same:
                self._send(200, {"n": n, "stored": True, "dedup": True})
            else:
                self._send(409, {"error": f"{n}은 이미 다른 내용으로 존재 — "
                                          "먼저 쓴 것이 남는다"})
            return
        # 쓰기 순서: sig·body 먼저, envelope 마지막 — envelope가 완성 표지
        (dirp / f"{n}-sig.txt").write_text(sig + "\n", encoding="utf-8")
        if body_name is not None:
            (dirp / f"{n}-{body_name}").write_bytes(body_b)
        env_p.write_bytes(env_b)
        self._send(200, {"n": n, "stored": True, "dedup": False})


def make_server(root: str | Path, token_file: str | Path,
                bind: str = "127.0.0.1", port: int = 8642,
                rate_limit_per_minute: int = RATE_LIMIT_PER_MINUTE,
                clock=time.monotonic) -> HTTPServer:
    tokens = load_tokens(token_file)
    handler = type("Handler", (_DropHandler,),
                   {"root": Path(root), "tokens": tokens,
                    "limiter": RateLimiter(rate_limit_per_minute, clock=clock)})
    return HTTPServer((bind, port), handler)


# ── 클라이언트 ──────────────────────────────────────────────────────────────

def _request(url: str, token: str, data: bytes | None = None) -> dict:
    req = urllib.request.Request(
        url, data=data, method="POST" if data is not None else "GET",
        headers={"Authorization": f"Bearer {token}",
                 **({"Content-Type": "application/json"} if data else {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error", "")
        except Exception:
            msg = ""
        raise DropError(e.code, msg or e.reason) from None


def push_quad(url: str, token: str, quad_prefix: str | Path) -> dict:
    """export가 만든 quad(`<dir>/NNN` 접두)를 POST. 같은 내용 재전송은 dedup 수렴."""
    prefix = Path(quad_prefix)
    n = prefix.name
    if not _N_RE.match(n):
        raise ValueError(f"quad 접두는 <dir>/NNN 꼴: {quad_prefix}")
    env_p = prefix.parent / f"{n}-envelope.json"
    sig_p = prefix.parent / f"{n}-sig.txt"
    if not (env_p.is_file() and sig_p.is_file()):
        raise ValueError(f"quad 불완전: {env_p.name} / {sig_p.name} 필요")
    bundle = {"n": n,
              "envelope_b64": base64.b64encode(env_p.read_bytes()).decode("ascii"),
              "sig": sig_p.read_text(encoding="utf-8").strip(),
              "body_name": None, "body_b64": None}
    bodies = sorted(prefix.parent.glob(f"{n}-body.*"))
    if bodies:
        bundle["body_name"] = bodies[0].name[len(n) + 1:]
        bundle["body_b64"] = base64.b64encode(bodies[0].read_bytes()).decode("ascii")
    return _request(url, token, json.dumps(bundle).encode("utf-8"))


def pull_quads(url: str, token: str, dest: str | Path,
               since: str | None = None) -> list[str]:
    """새 quad를 받아 로컬 우체통 트리에 내려쓴다. since 생략 시 로컬 최대 NNN부터.

    로컬도 append-only 우편함이다 — 이미 있는 파일은 절대 덮어쓰지 않는다."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    if since is None:
        have = [int(f.name.split("-", 1)[0]) for f in dest.glob("*-envelope.json")
                if _N_RE.match(f.name.split("-", 1)[0])]
        since = f"{max(have):03d}" if have else "000"
    written: list[str] = []
    while True:
        page = _request(f"{url}?since={since}", token)
        for q in page["quads"]:
            n = q["n"]
            if not _N_RE.match(n):
                raise DropError(502, f"서버가 준 n이 형식 위반: {n!r}")
            env_p = dest / f"{n}-envelope.json"
            if not env_p.exists():
                (dest / f"{n}-sig.txt").write_text(q["sig"] + "\n", encoding="utf-8")
                if q.get("body_name"):
                    if not _BODY_NAME_RE.match(q["body_name"]):
                        raise DropError(502, f"서버가 준 body_name 형식 위반: "
                                             f"{q['body_name']!r}")
                    (dest / f"{n}-{q['body_name']}").write_bytes(
                        base64.b64decode(q["body_b64"]))
                env_p.write_bytes(base64.b64decode(q["envelope_b64"]))
            written.append(n)
            since = n
        if not page.get("more"):
            return written
