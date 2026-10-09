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
- 토큰별 **범위**(0.7.0) = URL 경로에 대한 접근 목록. 어느 문에 쓰고 어느 문을 읽는지를 좁힌다.
  사설 git 저장소의 디렉터리별 권한과 같은 등급이고, 신뢰를 만들지 않는다 — 범위 안에서 올라온
  봉투도 받는 쪽이 서명을 검증한다. 서버는 여전히 봉투를 열지 않는다.

## 설계점: 같은 트리를 물화한다

서버 저장소는 git 우체통과 **동일한** `<channel>/from-x/NNN-{envelope.json,sig.txt,
body.*}` 트리다. 파일이 git pull 대신 HTTP로 도착할 뿐이라, 수신 어댑터는
transport_root 스왑만으로 무변경 동작한다.

## 프로토콜 — organum-hub/drop/v0

- `POST /v0/<channel>/<from-x>`  body = {"n","envelope_b64","sig"[,"body_name",
  "body_b64"]} → 200 {"n","stored","dedup"} · 409(같은 n 다른 내용) · 400/401/413 ·
  403(토큰의 쓰기 범위 밖 — 아무것도 쓰지 않는다, 0.7.0)
- `GET  /v0/<channel>/<from-x>?since=NNN` → 200 {"quads":[…], "more"} (n 오름차순,
  페이지 20; envelope가 마지막에 쓰이므로 미완성 quad는 목록에 나오지 않는다) ·
  403(토큰의 읽기 범위 밖, 0.7.0)
- `GET  /v0/channels` → 200 {"channels": {"<channel>": ["from-x", …], …}} —
  수거기의 문 목록(0.4.9). "채널이 몇 개 있는가"는 서버만 아는데 아무도 물을 수
  없었고, 각 랩이 목록을 기억으로 들다 한 랩이 네 문 중 두 문만 보는 사고가 났다.
  토큰 소지자 전용(예산 1 소비). 정확히 2세그먼트 경로만 예약이라 `channels`라는
  이름의 채널과도 충돌하지 않는다(그 채널의 문은 여전히 3세그먼트).
  0.7.0: 그 토큰이 **읽을 수 있는 문만** 추려서 준다 — 수거기가 이 목록으로 문을 찾는
  지금의 방식이 그대로 최소 권한 수거가 된다.
- 인증: `Authorization: Bearer <token>`. 서버의 토큰 파일은 한 줄에 토큰 하나(`#` 주석)이고,
  0.7.0부터 줄 뒤에 선택 필드를 적는다:
  `<token>  [id=<이름>]  [write=<문 패턴,…>]  [read=<문 패턴,…>]  [revoked]`
  - 문 패턴은 `<channel>/<from-x>` · `<channel>/*` · `*/<from-x>` · `*` 넷뿐(정규식 없음).
  - `write=`도 `read=`도 없는 줄은 **호환 줄**: 0.6.0과 같이 전부 연다(`id=`만 있어도).
  - 둘 중 하나라도 적으면 **범위 줄**: 적지 않은 축은 권한 없음이다. 전부 허용은 `*`로 적는다.
  - `revoked` 줄은 언제나 401이고 예산을 먹지 않는다. 감사 기록에는 남는다.
  - 문법이 틀린 줄·같은 토큰·같은 id가 있으면 서버는 뜨지 않는다. 저장 전에
    `organum-hub check-tokens`로 확인한다(토큰 값은 찍지 않는다).
  - 순서는 인증(401) → 빈도 한도(429) → 경로(404) → 범위(403). 범위 밖 요청은 멤버의
    요청이므로 예산을 쓴다.
- **발신 규약: 자기 문에만 쓴다** — `from-x`는 x가 쓰는 문이고, 다른 집이 읽게
  하려면 자기 문에 올리면 된다(각자 pull한다). 서버는 **봉투를 보고 발신자를 판정하지
  않는다**(dumb carrier가 발신자를 판정하기 시작하면 그게 더 나쁘다) — 클라이언트가
  기본 거부한다(0.4.10 `_check_door`, 실사고 산물). 0.7.0의 `write=` 범위는 그 위에 한 겹을
  더한다: 봉투가 아니라 **경로**를 보는 접근 목록이라 0.4.10의 선을 넘지 않는다. 운영자가
  줄에 범위를 적었을 때만 걸린다.
- **감사 기록**(0.7.0, `serve --audit-log <디렉터리>`): 인증된 요청마다 줄 구분 JSON 한 줄
  (utc·id·method·status·src·channel·door·since|n·envelope_sha256·stored·dedup). 운반 트리
  **밖**이어야 하고(안이면 뜨지 않는다), 본문·봉투 내용·토큰 값·출발지 주소 그대로는 적지 않는다
  (`src`는 주소의 지문). 응답을 보낸 **뒤에** 쓰고 실패해도 삼킨다 — 부가 기록의 실패가 저장된
  전송을 실패로 보이게 하지 않는다. 모르는 토큰의 401은 남기지 않는다. 탐지이지 예방이 아니다.
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
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

DROP_PROFILE = "organum-hub/drop/v0"
ENVELOPE_MAX_BYTES = 65536          # hub_wire CONTENT_MAX_BYTES와 같은 값
BODY_MAX_BYTES = 1_048_576
REQUEST_MAX_BYTES = 2 * 1_048_576
PAGE_SIZE = 20
# ── 상태 칸·문 색인·표지(0.8.0) — 설계: docs/hub-state-snapshot-restore-reconcile-v0-design.md §8.1
STATE_MAX_BYTES = 1_048_576         # 묶음의 한도 기본값 — 압축한, 곧 저장되는 바이트(--state-max-bytes)
STATE_KEEP = 3                      # 남기는 세대 수의 기본(--state-keep)
STATE_MAX_JUMP = 10_000             # floor_generation이 지금 세대를 넘을 수 있는 폭의 기본(--state-max-jump)
STATE_GENERATION_MAX = 99_999_999   # 세대 파일의 이름이 여덟 자리다
INDEX_PAGE_SIZE = 1000              # 문 색인의 한 쪽 — 항목 하나가 250바이트쯤이다(LxM 123 §5)
_STATE_REQUEST_OVERHEAD = 1024      # POST /v0/state에서 묶음을 감싼 것의 여유(실측 343바이트, LxM 124 §4)
RATE_LIMIT_PER_MINUTE = 60          # 토큰별 기본값 — 0이면 끔(self-host P2P용)
_WINDOW_SECONDS = 60.0
# 기본 예산의 출처(0.4.17, Ray 061): "콜드스타트 ~1분"은 천장에 잘린 값이었다 —
# 천장 400에서 완주 4회가 207~248초(Ray, 08-29~30), LxM 정확값 102.1초(08-27).
# 관측 최대 위 ~20% 여유가 이 수의 공식이다(0.4.17: 248→300 · 0.4.18: 292.8→350 ·
# 0.5.1: 352.6→420 — 나루 089·093의 완주가 표를 밀어올린 첫째·둘째 사례; 093은 새
# 기본 350을 하루 만에 넘었다). 술어는 "몇 초면 되나"가 아니라 "분포의 몇 %를
# 덮는가"(Ray 059) — 420은 관측된 완주 전부를 덮지만, ≥400 잘린 값(09-02)이 분포
# 위끝은 그보다 높을 수 있다고 신호한다. 그것도 관측이지 보증이 아니다. 나루 093의
# 다른 읽기("예산은 완주를 덮는 수가 아니라 잘린 값이 안 나올 때까지 올리는 수")는
# 공식 자체를 바꾸자는 제안이라 계측 규칙 소유자(Ray) 판정 몫 — 0.5.1은 공식 유지.
# 죽은 호스트 앞에서 기본 대기가 길어지는 트레이드는 호출별 --timeout이 받는다(0.4.16).
# ★ 이 수의 원장은 tests/test_hub_drop.py의 _COLDSTART_COMPLETIONS 관측표다(0.4.18,
# Ray 062) — 새 완주 관측은 거기에 행으로 추가하라. 표의 최댓값이 이 수를 넘으면
# 회귀가 시끄럽게 말한다(양방향).
CLIENT_TIMEOUT_SECONDS = 420
                                    # — Ludex 관찰: 종전 30s 고정이 콜드스타트와 겹쳐
                                    # 핸드셰이크 EOF로 보였다. 재푸시는 dedup 멱등.

# 무인증 깨우기 GET(0.4.6)의 예산 — **본 호출 예산에서 파생한다**(0.4.14, Ray 045).
#
# 종전엔 20초 독립 상수였는데, 같은 파일 세 줄 위가 콜드스타트를 ~1분으로 적고
# 있었다. **두 상수가 같은 콜드스타트에 대해 서로 다른 말을 하고 있었다**: 워밍이
# 막으라고 있는 상황(진짜로 식은 인스턴스)이 정확히 워밍이 실패하는 상황이었고,
# `_warm`은 설계대로 조용히 삼키므로 실패한 사실이 아무 데도 안 남았다.
# Ray 실측: 무인증 GET이 401을 돌려주기까지 55초 — 20초 예산으로는 구조적으로
# 도달할 수 없다. 같은 날 그들 pull(timeout 170)은 살고 push는 죽었다.
#
# 숫자를 올리지 않고 **결속**한다: 워밍이 존재하는 이유가 본 호출을 살리는 것이므로
# 두 예산이 독립인 것이 결함의 뿌리였다. 이렇게 두면 다음 사람이 이 주석을 안 읽어도
# 두 줄이 어긋날 수 없다(호스트 티어가 바뀌어 90을 고치면 워밍도 따라 움직인다).
# 대가: 인스턴스가 진짜 죽었을 때 최악 벽시계가 워밍+본 호출로 늘어난다 —
# 워밍 실패는 여전히 본 호출을 막지 않으므로 보험의 성질은 그대로다.
WARMUP_TIMEOUT_SECONDS = CLIENT_TIMEOUT_SECONDS

# 끝 닻은 `\Z`다. `$`는 문자열 끝의 줄바꿈 **앞**에서도 맞아서, `re.match`와 함께 쓰면 "값 + LF"가
# 통과한다(0.7.0, Jdot HQ 보고 2026-10-04): LF 붙은 sig는 저장된 뒤 같은 bundle 재전송이 409가 됐고,
# LF 붙은 n은 파일 이름에 줄바꿈을 넣어 같은 번호의 정상 quad와 나란히 저장됐다(409 보호 우회).
_CHANNEL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}\Z")
_SENDER_RE = re.compile(r"^from-[a-z0-9][a-z0-9-]{0,63}\Z")
_N_RE = re.compile(r"^[0-9]{3,6}\Z")
_SIG_RE = re.compile(r"^[0-9a-f]{128}\Z")
_BODY_NAME_RE = re.compile(r"^body\.[a-z0-9]{1,8}\Z")
_SHA_RE = re.compile(r"^[0-9a-f]{64}\Z")
_STATE_FILE_RE = re.compile(r"^([0-9]{8})\.state\Z")


class DropError(Exception):
    """drop 클라이언트 실패 — 서버 상태코드와 본문을 담는다."""

    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


_TOKEN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}\Z")
_SCOPE_ALL = ("*",)


class TokenEntry:
    """토큰 파일의 한 줄(0.7.0). `write`/`read`는 문 패턴의 튜플.

    - **호환 줄**: `write=`도 `read=`도 없는 줄. 0.6.0과 같이 전부 연다(`id=`만 있어도 호환 줄이다).
    - **범위 줄**: 둘 중 하나라도 적은 줄. 적지 않은 축은 **권한 없음**이다 — `read=`만 적은 토큰이
      전체 쓰기를 얻지 않는다. 전부 허용은 `*`로 명시한다.
    - **폐기 줄**: `revoked` 표시. 언제나 401이고, 감사 기록에 `id`와 함께 한 줄 남는다
      (폐기한 토큰이 다시 쓰이는 것이 유출의 가장 확실한 증거다 — LxM 118)."""

    __slots__ = ("token", "id", "write", "read", "revoked", "legacy", "explicit_id")

    def __init__(self, token, id, write, read, revoked, legacy, explicit_id=False):
        self.token, self.id, self.write, self.read = token, id, write, read
        self.revoked, self.legacy = revoked, legacy
        # 줄에 `id=`를 적었는가(0.8.0). 상태 칸의 이름은 적은 id만 된다 — 서버가 대신 만든 id는 아니다.
        self.explicit_id = explicit_id

    def allows(self, axis: str, channel: str, door: str) -> bool:
        return any(_pattern_allows(p, channel, door) for p in getattr(self, axis))

    def state_slot(self) -> str:
        """이 줄이 상태 칸에서 할 수 있는 것(0.9.0, LxM 151 §2). `none` · `read` · `write`.

        상태 칸은 원장을 가진 발신자의 것이고, 발신자의 줄에는 자기 문의 쓰기 범위가 있다. 그래서
        **쓰기 범위가 하나도 없는 범위 줄**은 `id=`가 있어도 상태 칸에 쓰지 못한다 — 읽기만 받은
        줄(창구의 줄 같은 것)이 자기 이름의 칸에 묶음을 쌓지 못하게 한다. 범위를 적지 않은 호환 줄은
        0.8.0과 같이 `id=`가 있으면 쓴다."""
        if self.revoked or not self.explicit_id:
            return "none"
        return "write" if (self.legacy or self.write) else "read"

    def describe(self) -> dict:
        """토큰 값 없는 요약(검사 명령·기동 출력용)."""
        mode = "revoked" if self.revoked else ("legacy" if self.legacy else "scoped")
        none = self.revoked                     # 폐기 줄은 아무것도 열지 않는다
        return {"id": self.id, "mode": mode, "write": [] if none else list(self.write),
                "read": [] if none else list(self.read), "state_slot": self.state_slot()}


def _pattern_allows(pattern: str, channel: str, door: str) -> bool:
    if pattern == "*":
        return True
    ch, dr = pattern.split("/", 1)
    return ch in ("*", channel) and dr in ("*", door)


def _parse_patterns(value: str) -> tuple:
    """`<channel>/<from-x>` · `<channel>/*` · `*/<from-x>` · `*`. 정규식은 받지 않는다."""
    out = []
    for item in value.split(","):
        if item == "*":
            out.append(item)
            continue
        ch, sep, dr = item.partition("/")
        if not (sep and (ch == "*" or _CHANNEL_RE.match(ch))
                and (dr == "*" or _SENDER_RE.match(dr)) and (ch, dr) != ("*", "*")):
            raise ValueError("문 패턴은 <channel>/<from-x> · <channel>/* · */<from-x> · * 가운데 하나")
        out.append(item)
    return tuple(out)


def load_token_entries(path: str | Path) -> list[TokenEntry]:
    """서버용 토큰 파일 파서(0.7.0). 한 줄 = `<token> [id=…] [write=…] [read=…] [revoked]`.

    문법이 틀린 줄이 하나라도 있으면 ValueError — 서버는 뜨지 않는다(열린 채로 뜨는 것보다 낫다).
    **오류 문구에 줄의 내용을 싣지 않는다**: 틀린 줄의 조각이 토큰일 수 있다."""
    entries: list[TokenEntry] = []
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    for lineno, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        token, fields = parts[0], parts[1:]
        id_ = None
        scope: dict = {}
        revoked = False
        try:
            for f in fields:
                if f == "revoked":
                    if revoked:
                        raise ValueError("revoked가 두 번")
                    revoked = True
                    continue
                key, sep, value = f.partition("=")
                if not sep or key not in ("id", "write", "read") or not value:
                    raise ValueError("알 수 없는 필드이거나 값이 비었다(필드는 id= · write= · read= · revoked)")
                if key == "id":
                    if id_ is not None:
                        raise ValueError("id가 두 번")
                    if not _TOKEN_ID_RE.match(value):
                        raise ValueError("id는 [a-z0-9][a-z0-9-]{0,63}")
                    id_ = value
                else:
                    if key in scope:
                        raise ValueError(f"{key}가 두 번")
                    scope[key] = _parse_patterns(value)
        except ValueError as e:
            raise ValueError(f"토큰 파일 {lineno}번째 줄: {e}") from None
        legacy = not scope
        entries.append(TokenEntry(
            token=token,
            id=id_ or hashlib.sha256(token.encode("utf-8")).hexdigest()[:16],
            write=_SCOPE_ALL if legacy else scope.get("write", ()),
            read=_SCOPE_ALL if legacy else scope.get("read", ()),
            revoked=revoked, legacy=legacy, explicit_id=id_ is not None))
    if not entries:
        raise ValueError(f"토큰 파일이 비어 있다: {path} — 열린 우체통은 만들지 않는다")
    if not any(not e.revoked for e in entries):
        raise ValueError(f"쓸 수 있는 토큰이 없다(전부 revoked): {path}")
    for attr, label in (("token", "같은 토큰이 두 줄에 있다"), ("id", "같은 id가 두 줄에 있다")):
        seen: set = set()
        for e in entries:
            v = getattr(e, attr)
            if v in seen:
                raise ValueError(f"토큰 파일: {label}")
            seen.add(v)
    return entries


def load_tokens(path: str | Path) -> list[str]:
    """토큰 **값**만 — 클라이언트(`push`·`pull`·`channels`)가 쓴다. 줄의 첫 조각이 토큰이다
    (0.7.0: 서버용 범위 필드가 붙은 줄도 읽는다. 클라이언트는 필드를 해석하지 않는다)."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    toks = [l.split()[0] for l in lines if l.strip() and not l.strip().startswith("#")]
    if not toks:
        raise ValueError(f"토큰 파일이 비어 있다: {path} — 열린 우체통은 만들지 않는다")
    return toks


def _token_match(entries: list[TokenEntry], header: str | None) -> TokenEntry | None:
    """일치한 토큰 줄을 돌려준다(없으면 None) — rate limit·범위·감사가 멤버 단위로 키를 잡도록.
    전 줄을 끝까지 비교한다(먼저 맞은 줄에서 멈추지 않는다 — 줄 위치가 시간으로 새지 않게)."""
    if not header or not header.startswith("Bearer "):
        return None
    given = header[len("Bearer "):].strip()
    hit = None
    for e in entries:
        if hmac.compare_digest(given.encode("utf-8"), e.token.encode("utf-8")) and hit is None:
            hit = e
    return hit


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


def _quad_files(dirp: Path, n: str, bodies: bool = True) -> dict | None:
    """완성 quad만 번들로. envelope는 마지막에 쓰이므로 존재+비어있지 않음 = 완성.
    `bodies=False`면 본문의 바이트를 읽지도 싣지도 않는다 — `body_b64` 칸이 없고 `body_name`만 있다."""
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
              "body_name": None}
    if bodies:
        bundle["body_b64"] = None
    found = sorted(dirp.glob(f"{n}-body.*"))
    if found:
        bundle["body_name"] = found[0].name[len(n) + 1:]
        if bodies:
            bundle["body_b64"] = base64.b64encode(found[0].read_bytes()).decode("ascii")
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


def _channel_tree(root: Path) -> dict[str, list[str]]:
    """channel/sender 트리 — root 디렉터리 열거를 POST와 같은 문법 필터로 거른다.

    정직한 성질 하나: **첫 봉투가 POST된 문만 보인다**(디렉터리가 그때 생기므로).
    수거기 용도로는 그게 정확히 맞는 의미다 — 약속된 채널이 아니라 실재하는 문.
    문이 하나도 없는 채널은 목록에 없다(POST 경로로는 만들어질 수 없는 모양이라,
    있다면 손이 만든 잔재다)."""
    tree: dict[str, list[str]] = {}
    if not root.is_dir():
        return tree
    for ch in sorted(root.iterdir()):
        if not (ch.is_dir() and _CHANNEL_RE.match(ch.name)):
            continue
        doors = sorted(d.name for d in ch.iterdir()
                       if d.is_dir() and _SENDER_RE.match(d.name))
        if doors:
            tree[ch.name] = doors
    return tree


# ── 상태 칸(0.8.0) ────────────────────────────────────────────────────────────
#
# 한 세대 = 파일 하나: `<state-dir>/<id>/NNNNNNNN.state`. 첫 줄이 머리(JSON 한 줄), 그 뒤가 묶음의
# 바이트다. 서버는 묶음을 열지 않는다. 바뀌는 포인터 파일이 없다 — 지금 세대는 완결된 것 가운데
# 번호가 가장 큰 것이고, **요청마다** 디렉터리에서 읽는다(감독기가 놓은 세대도 다음 요청부터 보인다).

def _state_header(path: Path) -> dict | None:
    """세대 파일의 머리. 번호·지문·길이가 모양에 맞고 바이트의 길이가 머리와 같을 때만 돌려준다 —
    쓰다 끊긴 파일이나 손댄 파일은 세지 않는다. 지문은 여기서 다시 계산하지 않는다(내줄 때 본다)."""
    m = _STATE_FILE_RE.match(path.name)
    if not m:
        return None
    try:
        with open(path, "rb") as f:
            line = f.readline(8192)
        if not line.endswith(b"\n"):
            return None
        h = json.loads(line.decode("utf-8"))
        if not isinstance(h, dict):
            return None
        gen, size = h.get("generation"), h.get("size")
        prev_gen, prev_sha = h.get("prev_generation"), h.get("prev_sha256")
        if not (type(gen) is int and gen == int(m.group(1)) and gen >= 1
                and type(size) is int and size >= 0
                and type(prev_gen) is int and 0 <= prev_gen < gen
                and isinstance(h.get("sha256"), str) and _SHA_RE.match(h["sha256"])
                and isinstance(prev_sha, str) and (prev_sha == "" or _SHA_RE.match(prev_sha))
                and isinstance(h.get("sig"), str) and _SIG_RE.match(h["sig"])):
            return None
        if path.stat().st_size != len(line) + size:
            return None
        h["_offset"] = len(line)
        return h
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _state_scan(dirp: Path) -> list[tuple[int, Path, dict]]:
    """완결된 세대를 번호 순으로. 점으로 시작하는 임시 이름은 보지 않는다."""
    out = []
    if dirp.is_dir():
        for f in dirp.iterdir():
            if _STATE_FILE_RE.match(f.name) and (h := _state_header(f)) is not None:
                out.append((h["generation"], f, h))
    out.sort(key=lambda t: t[0])
    return out


def _state_blob(path: Path, header: dict) -> bytes | None:
    """묶음의 바이트. 머리의 지문과 다르면 None — 깨진 세대를 내주지 않는다."""
    try:
        with open(path, "rb") as f:
            f.seek(header["_offset"])
            blob = f.read()
    except OSError:
        return None
    if len(blob) != header["size"] or hashlib.sha256(blob).hexdigest() != header["sha256"]:
        return None
    return blob


def _state_place(dirp: Path, header: dict, blob: bytes) -> bool:
    """세대 파일을 그 이름이 **없을 때만** 놓는다(LxM 127 §2). 점으로 시작하는 임시 이름으로 끝까지
    쓴 뒤 link로 제 이름을 붙인다 — rename은 있는 파일을 말없이 덮는다. 감독기도 같은 디렉터리에
    세대를 놓으므로, 둘이 부딪치면 하나만 선다. 이미 있으면 False(있던 파일은 그대로다)."""
    final = dirp / f"{header['generation']:08d}.state"
    tmp = dirp / f".{final.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    line = (json.dumps(header, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        with open(tmp, "wb") as f:
            f.write(line)
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, final)
            return True
        except FileExistsError:
            return False
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _state_prune(dirp: Path, keep: int) -> None:
    """최근 `keep`세대만 남긴다. 나이가 아니라 개수로 지운다 — 오래 쉰 연구소가 마지막 묶음까지
    잃지 않게. 지금 세대는 지워지지 않는다(가장 큰 번호다)."""
    for _gen, path, _h in _state_scan(dirp)[:-keep]:
        try:
            path.unlink()
        except OSError:
            pass


def _state_sweep_tmp(state_dir: Path) -> None:
    """뜰 때 남아 있는 임시 파일을 지운다(LxM 124 §1). 감독기는 임시 이름을 옮기지 않으므로
    재시작을 넘지 못하지만, 재시작 없이 오래 도는 배치에서는 쌓인다."""
    if not state_dir.is_dir():
        return
    for sub_dir in state_dir.iterdir():
        if not sub_dir.is_dir():
            continue
        for f in sub_dir.iterdir():
            if f.name.startswith(".") and f.name.endswith(".tmp"):
                try:
                    f.unlink()
                except OSError:
                    pass


def _marked(marks_dir: Path | None, *parts: str) -> bool | None:
    """표지가 있는가(0.8.0). 표지는 감독기가 쓰고 서버는 **읽기만** 한다 — 빈 파일이 있으면 그 quad나
    세대가 바깥 저장소에 옮겨졌다는 뜻이다. 표지를 쓰지 않는 배치(`--marks-dir` 없음)에서는 None이고,
    그때 답에는 그 칸이 없다."""
    if marks_dir is None:
        return None
    return marks_dir.joinpath(*parts).is_file()


def _index_entry(dirp: Path, n: str) -> dict | None:
    """문 색인의 한 항목 — 저장된 **바이트**의 지문. 본문을 싣지 않고 봉투를 열지 않는다.
    완결된 quad만(봉투와 서명이 있는 것). `body_sha256`은 봉투에 적힌 값이 아니라 본문 파일의 지문이다."""
    env_p = dirp / f"{n}-envelope.json"
    sig_p = dirp / f"{n}-sig.txt"
    if not (env_p.is_file() and sig_p.is_file()):
        return None
    env_b = env_p.read_bytes()
    if not env_b:
        return None
    entry = {"n": n, "envelope_sha256": hashlib.sha256(env_b).hexdigest(),
             "sig_sha256": hashlib.sha256(sig_p.read_bytes()).hexdigest(),
             "body_name": None, "body_sha256": None, "body_size": None}
    bodies = sorted(dirp.glob(f"{n}-body.*"))
    if bodies:
        body_b = bodies[0].read_bytes()
        entry.update(body_name=bodies[0].name[len(n) + 1:],
                     body_sha256=hashlib.sha256(body_b).hexdigest(), body_size=len(body_b))
    return entry


def _split_path(path: str) -> tuple[str, str] | None:
    parts = [p for p in path.split("/") if p]
    if len(parts) != 3 or parts[0] != "v0":
        return None
    channel, sender = parts[1], parts[2]
    if not (_CHANNEL_RE.match(channel) and _SENDER_RE.match(sender)):
        return None
    return channel, sender


def _filter_tree(tree: dict, entry: TokenEntry) -> dict:
    """문 목록을 읽기 범위로 추린다 — 읽을 수 없는 문은 이름도 보이지 않는다."""
    out = {}
    for ch, doors in tree.items():
        keep = [d for d in doors if entry.allows("read", ch, d)]
        if keep:
            out[ch] = keep
    return out


class _DropHandler(BaseHTTPRequestHandler):
    server_version = "organum-hub-drop/0"
    root: Path
    entries: list
    limiter: RateLimiter
    audit_dir = None            # Path | None — 운반 트리 밖(make_server가 보증)
    state_dir = None            # Path | None — 상태 칸(0.8.0). 없으면 칸 요청은 404
    state_keep = STATE_KEEP
    state_max_bytes = STATE_MAX_BYTES
    state_max_jump = STATE_MAX_JUMP
    marks_dir = None            # Path | None — 표지(0.8.0). 감독기가 쓰고 서버는 읽기만
    now = staticmethod(time.time)

    def _send(self, status: int, obj: dict, headers: dict | None = None):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    # ── 감사 기록(0.7.0) ──
    def _source(self) -> str:
        """요청 출발지의 **지문**(주소 그대로가 아니다). 프록시 뒤 배치를 위해
        `X-Forwarded-For`의 첫 주소를 먼저 본다. 같은 출발지인지 다른 출발지인지를 가리는 용도다."""
        fwd = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        addr = fwd or (self.client_address[0] if self.client_address else "")
        return hashlib.sha256(addr.encode("utf-8", "replace")).hexdigest()[:16]

    def _audit(self, entry: TokenEntry, status: int, **fields) -> None:
        """인증된(또는 폐기 토큰의) 요청 한 줄. **응답을 보낸 뒤에** 부르고, 실패해도 삼킨다 —
        감사 기록 실패가 이미 저장된 전송을 실패로 보이게 해서는 안 된다(Jdot HQ 검토).
        본문·봉투 내용·토큰 값은 적지 않는다."""
        if self.audit_dir is None:
            return
        try:
            t = time.gmtime(self.now())
            line = {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", t), "id": entry.id,
                    "method": self.command, "status": status, "src": self._source()}
            line.update({k: v for k, v in fields.items() if v is not None})
            path = self.audit_dir / f"audit-{time.strftime('%Y%m%d', t)}.jsonl"
            with open(path, "a", encoding="utf-8", newline="\n") as f:   # Windows에서도 LF 한 글자
                f.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")
        except Exception as e:                    # noqa: BLE001 — 감사는 운반을 막지 않는다
            self.log_error("audit write failed: %s", type(e).__name__)

    def _auth(self) -> TokenEntry | None:
        """인증 → rate limit. 실패 시 응답까지 보내고 None, 통과면 토큰 줄.

        순서가 계약이다: 인증 실패(401)는 limiter 호출 **전**에 반환 — 멤버가
        아니면 예산을 먹지 않는다(무인증 워밍 GET이 공짜인 근거, 0.4.6).
        폐기 줄도 401이고 예산을 먹지 않는다. 다만 감사 줄은 남긴다(0.7.0)."""
        entry = _token_match(self.entries, self.headers.get("Authorization"))
        if entry is None:
            self._send(401, {"error": "bearer 토큰 필요"})
            return None
        if entry.revoked:
            self._send(401, {"error": "bearer 토큰 필요"})
            self._audit(entry, 401, revoked=True, path=self.path.split("?", 1)[0])
            return None
        retry = self.limiter.check(entry.token)
        if retry is not None:
            self._send(429, {"error": f"rate limit — {retry}초 뒤에"},
                       headers={"Retry-After": str(retry)})
            self._audit(entry, 429, path=self.path.split("?", 1)[0])
            return None
        return entry

    def _gate(self, axis: str) -> tuple | None:
        """인증 → rate limit → 경로 → 범위 순. 실패 시 응답까지 보내고 None.
        통과면 (토큰 줄, channel, door)."""
        entry = self._auth()
        if entry is None:
            return None
        path = self.path.split("?", 1)[0]
        loc = _split_path(path)
        if loc is None:
            self._send(404, {"error": "경로는 /v0/<channel>/<from-x>"})
            self._audit(entry, 404, path=path)
            return None
        if not entry.allows(axis, loc[0], loc[1]):
            # 범위 밖(0.7.0): 아무것도 쓰지 않고 읽어 주지 않는다. 봉투를 열어 본 판정이 아니라
            # **경로**에 대한 접근 목록이다 — 발신자 판정은 여전히 수신 hub의 admit 몫이다.
            label = "쓰기" if axis == "write" else "읽기"
            self._send(403, {"error": f"이 토큰의 {label} 범위 밖"})
            self._audit(entry, 403, channel=loc[0], door=loc[1])
            return None
        return entry, loc[0], loc[1]

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler 계약
        if self.path.split("?", 1)[0] == "/v0/channels":
            entry = self._auth()
            if entry is None:
                return
            self._send(200, {"channels": _filter_tree(_channel_tree(self.root), entry)})
            self._audit(entry, 200, op="channels")
            return
        if self.path.split("?", 1)[0] == "/v0/state":
            self._state_get()
            return
        gate = self._gate("read")
        if gate is None:
            return
        entry, channel, door = gate
        since = "000"
        q = self._query()
        if q:
            since = q.get("since", "000")
            if not _N_RE.match(since) and since != "000":
                self._send(400, {"error": "since는 3~6자리 숫자"})
                self._audit(entry, 400, channel=channel, door=door)
                return
        dirp = self.root / channel / door
        ns = []
        if dirp.is_dir():
            ns = sorted({f.name.split("-", 1)[0] for f in dirp.glob("*-envelope.json")
                         if _N_RE.match(f.name.split("-", 1)[0])}, key=int)
        fresh = [n for n in ns if int(n) > int(since)]
        if q.get("index") == "1":
            # 문 색인(0.8.0): 본문 없이 번호와 저장된 바이트의 지문만. 대조가 문마다 요청 한 번이 된다.
            # 기본은 처음부터다 — 낮은 번호가 늦게 올라온 경우를 잡으려면 그래야 한다. `since`는 다음 쪽용.
            index = []
            for n in fresh[:INDEX_PAGE_SIZE]:
                item = _index_entry(dirp, n)
                if item is None:
                    continue
                mirrored = _marked(self.marks_dir, "quads", channel, door, n)
                if mirrored is not None:
                    item["mirrored"] = mirrored
                index.append(item)
            self._send(200, {"index": index, "more": len(fresh) > INDEX_PAGE_SIZE})
            self._audit(entry, 200, op="index", channel=channel, door=door, since=since,
                        quads=len(index))
            return
        # `limit`(0.9.0): 한 쪽의 개수를 부르는 쪽이 줄인다 — 한 통만 받으려는 쪽이 스무 통의 본문을
        # 함께 받지 않게. 늘리지는 못한다.
        page = PAGE_SIZE
        if "limit" in q:
            lim = q["limit"]
            if not (lim.isdigit() and len(lim) <= 3 and 1 <= int(lim) <= PAGE_SIZE):
                self._send(400, {"error": f"limit은 1부터 {PAGE_SIZE}까지"})
                self._audit(entry, 400, channel=channel, door=door)
                return
            page = int(lim)
        # `bodies=0`(0.9.0): 봉투와 서명만 준다. 받는 이를 보려고 봉투만 읽는 쪽이 남의 앞 본문을
        # 받지 않게 한다(Orin 056). 감사 줄에 본문 없이 읽었다는 것이 남는다.
        with_bodies = True
        if "bodies" in q:
            if q["bodies"] not in ("0", "1"):
                self._send(400, {"error": "bodies는 0 또는 1"})
                self._audit(entry, 400, channel=channel, door=door)
                return
            with_bodies = q["bodies"] == "1"
        quads = [b for n in fresh[:page] if (b := _quad_files(dirp, n, with_bodies))]
        self._send(200, {"quads": quads, "more": len(fresh) > page})
        self._audit(entry, 200, channel=channel, door=door, since=since, quads=len(quads),
                    bodies=None if with_bodies else False)

    # ── 상태 칸(0.8.0) ──
    def _query(self) -> dict:
        if "?" not in self.path:
            return {}
        return dict(p.split("=", 1) for p in self.path.split("?", 1)[1].split("&") if "=" in p)

    def _state_gate(self) -> TokenEntry | None:
        """인증 → rate limit → 칸이 있는 배치인가(404) → 줄에 `id=`가 있는가(403). 0.7.0과 같은 순서다.
        칸은 토큰 줄의 `id=`마다 하나이고, 자기 칸만 읽고 쓴다."""
        entry = self._auth()
        if entry is None:
            return None
        if self.state_dir is None:
            self._send(404, {"error": "이 드롭에는 상태 칸이 없다"})
            self._audit(entry, 404, path="/v0/state")
            return None
        if not entry.explicit_id:
            self._send(403, {"error": "상태 칸은 토큰 줄에 id=가 있어야 쓴다"})
            self._audit(entry, 403, path="/v0/state")
            return None
        return entry

    def _state_common(self, entry: TokenEntry, gens: list) -> dict:
        """상태 칸의 답에 언제나 싣는 것 — 이 배치의 한도, 남겨 둔 세대의 목록, 자리를 잡았다는 신호.
        표지를 쓰지 않는 배치에서는 `mirrored`·`settled`·`settled_by_timeout`이 없다."""
        out = {"max_bytes": self.state_max_bytes}
        kept = []
        for gen, _path, h in reversed(gens):
            item = {"generation": gen, "sha256": h["sha256"]}
            mirrored = _marked(self.marks_dir, "state", entry.id, f"{gen:08d}")
            if mirrored is not None:
                item["mirrored"] = mirrored
            kept.append(item)
        out["kept"] = kept
        settled = _marked(self.marks_dir, "settled")
        if settled is not None:
            out["settled"] = settled
            out["settled_by_timeout"] = _marked(self.marks_dir, "settled-by-timeout")
        return out

    def _state_get(self) -> None:
        entry = self._state_gate()
        if entry is None:
            return
        q = self._query()
        gens = _state_scan(self.state_dir / entry.id)
        common = self._state_common(entry, gens)
        want = q.get("generation")
        if want is not None and not (want.isdigit() and len(want) <= 8):
            self._send(400, {"error": "generation은 여덟 자리 이하의 숫자"})
            self._audit(entry, 400, op="state_get")
            return
        if not gens:
            # 빈 칸은 세대 0이고 지문은 빈 문자열이다. 처음 되살리는 환경도 한도와 신호를 여기서 안다.
            self._send(404, {"error": "칸이 비었다", "generation": 0, "sha256": "", **common})
            self._audit(entry, 404, op="state_get", generation=0)
            return
        hit = gens[-1] if want is None else next((g for g in gens if g[0] == int(want)), None)
        if hit is None:
            self._send(404, {"error": "남겨 둔 세대가 아니다", "generation": gens[-1][0],
                             "sha256": gens[-1][2]["sha256"], **common})
            self._audit(entry, 404, op="state_get", generation=int(want))
            return
        gen, path, h = hit
        body = {"generation": gen, "sha256": h["sha256"], "prev_generation": h["prev_generation"],
                "prev_sha256": h["prev_sha256"], "size": h["size"], **common}
        mirrored = _marked(self.marks_dir, "state", entry.id, f"{gen:08d}")
        if mirrored is not None:
            body["mirrored"] = mirrored
        if q.get("meta") != "1":
            blob = _state_blob(path, h)
            if blob is None:
                self._send(500, {"error": "저장된 세대가 머리와 맞지 않는다"})
                self._audit(entry, 500, op="state_get", generation=gen)
                return
            body["blob_b64"] = base64.b64encode(blob).decode("ascii")
            body["sig"] = h["sig"]
        self._send(200, body)
        # 그때 답한 `mirrored`를 감사 줄에 남긴다(0.9.0, LxM 145 §2) — 표지가 선 것을 본 때를 운영자가
        # 미루어 읽지 않고 기록에서 읽는다. 표지를 쓰지 않는 배치에서는 칸이 없다.
        self._audit(entry, 200, op="state_get", generation=gen, sha256=h["sha256"],
                    meta=q.get("meta") == "1" or None, mirrored=mirrored)

    def _state_post(self) -> None:
        entry = self._state_gate()
        if entry is None:
            return
        if entry.state_slot() != "write":
            self._send(403, {"error": "이 토큰 줄은 상태 칸에 쓰지 못한다 — 쓰기 범위가 하나도 없다"})
            self._audit(entry, 403, op="state_put")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > REQUEST_MAX_BYTES:
            self._send(413, {"error": f"요청은 1..{REQUEST_MAX_BYTES} 바이트"})
            self._audit(entry, 413, op="state_put")
            return
        try:
            obj = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(obj, dict):
                raise ValueError("본문은 JSON 객체")
            expect_gen, expect_sha = obj.get("expect_generation"), obj.get("expect_sha256")
            floor, sha, sig = obj.get("floor_generation", 0), obj.get("sha256"), obj.get("sig")
            if not (type(expect_gen) is int and 0 <= expect_gen <= STATE_GENERATION_MAX):
                raise ValueError("expect_generation은 0 이상의 정수")
            if not (isinstance(expect_sha, str)
                    and (_SHA_RE.match(expect_sha) or (expect_sha == "" and expect_gen == 0))):
                raise ValueError("expect_sha256은 지금 세대의 지문(빈 칸이면 빈 문자열)")
            if not (type(floor) is int and 0 <= floor <= STATE_GENERATION_MAX):
                raise ValueError("floor_generation은 0 이상의 정수")
            if not (isinstance(sha, str) and _SHA_RE.match(sha)):
                raise ValueError("sha256은 hex 64자")
            if not (isinstance(sig, str) and _SIG_RE.match(sig)):
                raise ValueError("sig는 hex 128자")
            if not isinstance(obj.get("blob_b64"), str):
                raise ValueError("blob_b64 없음")
            blob = base64.b64decode(obj["blob_b64"], validate=True)
        except (ValueError, UnicodeDecodeError) as e:
            self._send(400, {"error": str(e)})
            self._audit(entry, 400, op="state_put")
            return
        if len(blob) > self.state_max_bytes:
            self._send(413, {"error": f"묶음은 {self.state_max_bytes} 바이트 이하",
                             "max_bytes": self.state_max_bytes})
            self._audit(entry, 413, op="state_put", sha256=sha)
            return
        if hashlib.sha256(blob).hexdigest() != sha:
            self._send(400, {"error": "sha256이 묶음의 바이트와 다르다"})
            self._audit(entry, 400, op="state_put", sha256=sha)
            return
        dirp = self.state_dir / entry.id
        dirp.mkdir(parents=True, exist_ok=True)
        gens = _state_scan(dirp)
        cur_gen, cur = (gens[-1][0], gens[-1][2]) if gens else (0, None)
        cur_sha = cur["sha256"] if cur else ""
        if floor > cur_gen + self.state_max_jump:
            self._send(400, {"error": "floor_generation이 지금 세대보다 너무 크다",
                             "generation": cur_gen})
            self._audit(entry, 400, op="state_put", sha256=sha)
            return
        if (expect_gen, expect_sha) == (cur_gen, cur_sha):
            # 조건은 세대와 지문 둘이다. 새 번호는 되돌아가지 않는다 — 올리는 쪽이 보낸 적 있는
            # 가장 큰 번호(floor)보다 크게 매긴다.
            new_gen = max(cur_gen, floor) + 1
            if new_gen > STATE_GENERATION_MAX:
                self._send(400, {"error": "세대 번호가 끝에 닿았다"})
                self._audit(entry, 400, op="state_put", sha256=sha)
                return
            header = {"generation": new_gen, "sha256": sha, "prev_generation": cur_gen,
                      "prev_sha256": cur_sha, "size": len(blob), "sig": sig}
            if _state_place(dirp, header, blob):
                _state_prune(dirp, self.state_keep)
                self._send(200, {"generation": new_gen, "stored": True, "dedup": False})
                self._audit(entry, 200, op="state_put", generation=new_gen, sha256=sha,
                            stored=True, dedup=False)
                return
            # 그 이름이 방금 생겼다(감독기가 놓았다). 덮지 않는다 — 조건이 어긋난 것으로 답한다.
            gens = _state_scan(dirp)
            cur_gen, cur = (gens[-1][0], gens[-1][2]) if gens else (0, None)
            cur_sha = cur["sha256"] if cur else ""
        elif (cur is not None and (expect_gen, expect_sha) == (cur["prev_generation"],
                                                               cur["prev_sha256"])
              and sha == cur_sha):
            # 같은 것을 같은 조건으로 다시 올렸다 — 응답을 잃은 경우다. 편지의 중복 처리와 같은 뜻.
            self._send(200, {"generation": cur_gen, "stored": True, "dedup": True})
            self._audit(entry, 200, op="state_put", generation=cur_gen, sha256=sha,
                        stored=True, dedup=True)
            return
        self._send(409, {"error": "조건이 지금 세대와 다르다", "generation": cur_gen,
                         "sha256": cur_sha})
        self._audit(entry, 409, op="state_put", generation=cur_gen, sha256=sha, stored=False)

    def do_POST(self):  # noqa: N802
        if self.path.split("?", 1)[0] == "/v0/state":
            self._state_post()
            return
        gate = self._gate("write")
        if gate is None:
            return
        entry, channel, door = gate
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > REQUEST_MAX_BYTES:
            self._send(413, {"error": f"요청은 1..{REQUEST_MAX_BYTES} 바이트"})
            self._audit(entry, 413, channel=channel, door=door)
            return
        try:
            n, env_b, sig, body_name, body_b = _validate_bundle(
                json.loads(self.rfile.read(length).decode("utf-8")))
        except (ValueError, UnicodeDecodeError) as e:
            self._send(400, {"error": str(e)})
            self._audit(entry, 400, channel=channel, door=door)
            return
        # 봉투 **바이트**의 지문 — 봉투를 열지 않는다. 409로 거부된 시도는 내용이 남지 않으므로,
        # 무엇을 올리려 했는지는 이 지문으로만 남는다(LxM 099 §3(b)·118).
        env_sha = hashlib.sha256(env_b).hexdigest()
        dirp = self.root / channel / door
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
                self._audit(entry, 200, channel=channel, door=door, n=n,
                            envelope_sha256=env_sha, stored=True, dedup=True)
            else:
                self._send(409, {"error": f"{n}은 이미 다른 내용으로 존재 — "
                                          "먼저 쓴 것이 남는다"})
                self._audit(entry, 409, channel=channel, door=door, n=n,
                            envelope_sha256=env_sha, stored=False)
            return
        # 쓰기 순서: sig·body 먼저, envelope 마지막 — envelope가 완성 표지
        (dirp / f"{n}-sig.txt").write_bytes((sig + "\n").encode("utf-8"))
        if body_name is not None:
            (dirp / f"{n}-{body_name}").write_bytes(body_b)
        env_p.write_bytes(env_b)
        self._send(200, {"n": n, "stored": True, "dedup": False})
        self._audit(entry, 200, channel=channel, door=door, n=n,
                    envelope_sha256=env_sha, stored=True, dedup=False)


def make_server(root: str | Path, token_file: str | Path,
                bind: str = "127.0.0.1", port: int = 8642,
                rate_limit_per_minute: int = RATE_LIMIT_PER_MINUTE,
                clock=time.monotonic, audit_dir: str | Path | None = None,
                now=time.time, state_dir: str | Path | None = None,
                state_keep: int = STATE_KEEP, state_max_bytes: int = STATE_MAX_BYTES,
                state_max_jump: int = STATE_MAX_JUMP,
                marks_dir: str | Path | None = None) -> HTTPServer:
    entries = load_token_entries(token_file)
    root_p = Path(root)
    root_r = root_p.resolve()
    audit_p = None
    if audit_dir is not None:
        audit_p = Path(audit_dir).resolve()
        if audit_p == root_r or root_r in audit_p.parents:
            # 감사 기록이 운반 트리 안에 있으면 드롭으로 읽힌다 — 뜨지 않는다.
            raise ValueError("감사 디렉터리는 운반 트리(--root) 밖이어야 한다")
        audit_p.mkdir(parents=True, exist_ok=True)
    # 상태 디렉터리와 표지 디렉터리(0.8.0)도 같은 규칙이다. 서로의 안에도 두지 않는다 —
    # 감독기가 디렉터리마다 다른 규칙으로 옮기므로 겹치면 표지가 세대나 편지로 읽힌다.
    extra = {}
    for label, given in (("상태 디렉터리(--state-dir)", state_dir),
                         ("표지 디렉터리(--marks-dir)", marks_dir)):
        if given is None:
            continue
        d = Path(given).resolve()
        others = [("운반 트리(--root)", root_r)] + ([("감사 디렉터리", audit_p)] if audit_p else []) \
            + list(extra.items())
        for other_label, other in others:
            if d == other or other in d.parents or d in other.parents:
                raise ValueError(f"{label}는 {other_label}와 겹치지 않는 자리여야 한다")
        extra[label] = d
    state_p = extra.get("상태 디렉터리(--state-dir)")
    marks_p = extra.get("표지 디렉터리(--marks-dir)")
    if state_keep < 1:
        raise ValueError("--state-keep은 1 이상")
    if state_max_jump < 1:
        raise ValueError("--state-max-jump는 1 이상")
    # 묶음은 base64로 실려 3분의 1이 커진다. 요청 한도는 건드리지 않으므로 값에 천장이 있다.
    if state_max_bytes < 1 or 4 * math.ceil(state_max_bytes / 3) + _STATE_REQUEST_OVERHEAD \
            > REQUEST_MAX_BYTES:
        raise ValueError(f"--state-max-bytes가 요청 한도({REQUEST_MAX_BYTES} 바이트)에 들지 않는다")
    if state_p is not None:
        state_p.mkdir(parents=True, exist_ok=True)
        _state_sweep_tmp(state_p)
    handler = type("Handler", (_DropHandler,),
                   {"root": root_p, "entries": entries,
                    "limiter": RateLimiter(rate_limit_per_minute, clock=clock),
                    "audit_dir": audit_p, "now": staticmethod(now),
                    "state_dir": state_p, "state_keep": state_keep,
                    "state_max_bytes": state_max_bytes, "state_max_jump": state_max_jump,
                    "marks_dir": marks_p})
    return HTTPServer((bind, port), handler)


# ── 클라이언트 ──────────────────────────────────────────────────────────────

def _warm(url: str, timeout: int = WARMUP_TIMEOUT_SECONDS) -> bool:
    """콜드스타트 깨우기 — **무인증 bare GET** (0.4.6, LxM 008–010 + Ludex 명세).

    두 줄이 명세다:
    ① **HTTPError는 생존으로 센다.** 401·404 둘 다 "인스턴스가 섰다"는 증거다 —
      워밍업이 확인하는 것은 인증이 아니라 생존이다. 실패로 세면 **진단이 뒤집힌다**:
      토큰이 틀린 멤버가 "콜드스타트가 안 풀린다"고 보고하게 된다.
    ② **어떤 실패도 본 호출을 죽이지 않는다** — 워밍업은 보험이지 게이트가 아니다.
      깨우기 실패로 봉투를 안 보내면 새 실패 경로를 만드는 셈이다.

    **본체에 두는 이유**(Ludex 발견, LxM 010): 클라이언트마다 각자 구현하면 누군가는
    토큰을 실어 보내고, 그 순간 워밍업이 멤버 예산을 먹기 시작한다. 무인증 GET이
    공짜인 성질(`_gate()`가 인증 실패를 limiter 호출 **전**에 반환)은 "무인증"이
    지켜질 때만 성립하므로, 규율이 아니라 **코드로 고정한다**."""
    try:
        with urllib.request.urlopen(
                urllib.request.Request(url, method="GET"), timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True                      # ① 401/404 = 인스턴스 생존
    except Exception:                    # ② 네트워크·TLS·타임아웃 전부 삼킨다
        return False


def _warm_measured(url: str, stats: dict | None = None, *,
                   timeout: int = WARMUP_TIMEOUT_SECONDS) -> bool:
    """워밍 + **지연 계측**(0.4.6). 호스트가 "이 시각엔 따뜻하다"고 공지할 때,
    그 공지의 생사는 약속이 아니라 **관측**으로 판정돼야 한다 —
    *"등록되어 있다는 것과 오늘 아침에 돌았다는 것은 다른 사실이다"*(LxM 022).

    멤버의 평상 트래픽이 그대로 증거가 된다: 공지 시각에 `warm_ms`가 수십 ms면
    시간표가 돌았고, 수만 ms면 안 돌았다. 추가 요청도 새 인프라도 없다.

    `timeout`은 **호출자의 예산을 관통시킨다**(0.4.16, Orin 026). 0.4.14가
    `WARMUP_TIMEOUT_SECONDS = CLIENT_TIMEOUT_SECONDS`로 **기본값 둘**을 결속했는데,
    호출별 override는 본 요청에만 닿고 워밍은 계속 기본값을 썼다 — 그래서
    `--timeout 7`이 "전체 7초"가 아니라 **"워밍 최대 90초 + 본 요청 7초"**였다.
    결속이 기본값 층에서만 성립하고 호출 층에서 끊겨 있었다는 뜻이다. 예산을
    좁히는 쪽은 대개 급한 쪽인데, 그 사람이 정확히 못 받고 있었다."""
    t0 = time.monotonic()
    ok = _warm(url, timeout=timeout)
    if stats is not None:
        stats["warm_ms"] = int((time.monotonic() - t0) * 1000)
        stats["warm_ok"] = ok
        # 상한 병기(0.4.18, Ray 계측 규칙): "상한 없는 값은 길이인지 절단인지
        # 영원히 모른다" — warm_ms가 실패면 이 값은 길이가 아니라 절단점이다.
        stats["warm_budget_s"] = timeout
    return ok


def _request(url: str, token: str, data: bytes | None = None,
             timeout: int = CLIENT_TIMEOUT_SECONDS) -> dict:
    req = urllib.request.Request(
        url, data=data, method="POST" if data is not None else "GET",
        headers={"Authorization": f"Bearer {token}",
                 **({"Content-Type": "application/json"} if data else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error", "")
        except Exception:
            msg = ""
        raise DropError(e.code, msg or e.reason) from None


def push_quad(url: str, token: str, quad_prefix: str | Path,
              timeout: int = CLIENT_TIMEOUT_SECONDS, warmup: bool = True,
              stats: dict | None = None,
              allow_foreign_door: bool = False) -> dict:
    """export가 만든 quad(`<dir>/NNN` 접두)를 POST. 같은 내용 재전송은 dedup 수렴.

    발신 문 게이트(0.4.10, `_check_door`)가 **네트워크 접촉 전에** 돈다 — 자기 문이
    아니면 아무것도 보내지 않고 거부한다. 정당한 대리 전달은 `allow_foreign_door`.

    `stats` dict를 주면 워밍 계측(`warm_ms`·`warm_ok`)을 채워 준다 — 반환 모양은
    안 바꾼다(교차 랩 소비 계약 보존)."""
    prefix = Path(quad_prefix)
    n = prefix.name
    if not _N_RE.match(n):
        raise ValueError(f"quad 접두는 <dir>/NNN 꼴: {quad_prefix}")
    env_p = prefix.parent / f"{n}-envelope.json"
    sig_p = prefix.parent / f"{n}-sig.txt"
    if not (env_p.is_file() and sig_p.is_file()):
        raise ValueError(f"quad 불완전: {env_p.name} / {sig_p.name} 필요")
    _check_door(url, env_p.read_bytes(), allow_foreign_door)
    if warmup:
        _warm_measured(url, stats, timeout=timeout)
    bundle = {"n": n,
              "envelope_b64": base64.b64encode(env_p.read_bytes()).decode("ascii"),
              "sig": sig_p.read_text(encoding="utf-8").strip(),
              "body_name": None, "body_b64": None}
    bodies = sorted(prefix.parent.glob(f"{n}-body.*"))
    if bodies:
        bundle["body_name"] = bodies[0].name[len(n) + 1:]
        bundle["body_b64"] = base64.b64encode(bodies[0].read_bytes()).decode("ascii")
    return _request(url, token, json.dumps(bundle).encode("utf-8"), timeout=timeout)


def _door_for_signer(signer_id) -> str | None:
    """봉투 서명자 → 그가 쓸 수 있는 문 이름. 파생 불가면 None(호출자가 fail-closed).

    문법은 결정적이다: 봉투 스키마의 signer.id는 `lab:<name>` 고정이므로
    `lab:x` → `from-x`. 다만 lab 문법(`.`·`_` 허용)이 sender 문법(`-`만)보다
    넓어서 파생 결과가 문 이름이 못 되는 경우가 있다 — 그때는 추측하지 않고
    None을 돌려준다(r3 교훈: 파생이 안 서는 자리는 fail-closed)."""
    if not isinstance(signer_id, str) or not signer_id.startswith("lab:"):
        return None
    door = f"from-{signer_id[len('lab:'):]}"
    return door if _SENDER_RE.fullmatch(door) else None


def _check_door(url: str, env_b: bytes, allow_foreign_door: bool) -> None:
    """**발신 문 게이트**(0.4.10) — 자기 문에만 쓴다. 실사고에서 나왔다.

    2026-08-26, 나는 우리 서명 봉투를 `from-ludex`·`from-ray`에 POST했다. 서버는
    dumb carrier라 막지 않았고(설계된 성질이고 좋은 성질이다), append-only라
    철회도 못 했다. 세 번째 문이 409로 막힌 것이 사고를 두 자리에서 멈춰 세웠다.
    **서버가 막지 않는다는 것과 해도 된다는 것은 다르다** — 그 사이를 규율이
    메우고 있었고, 규율은 한 번의 착각으로 무너진다. 그래서 기계로 옮긴다.

    층위(0.4.5 admit 게이트와 같은 자리): **서버가 아니라 클라이언트**다. carrier가
    발신자를 판정하기 시작하면 그게 더 나쁘다. 그리고 라이브러리 본체에 둔다
    (_warm과 같은 논리, Ludex 010): 클라마다 구현하면 누군가는 빼먹는다.

    **축을 혼동하지 말 것**: 이 게이트는 *전송로의 문*을 본다. 0.4.5의
    `--accept-foreign-target`은 *봉투의 수신자*를 본다. 다른 축이고, 발신이 타 lab을
    target하는 것은 여전히 본래 목적이다(그 경계는 옮기지 말 것).

    성공 조건을 명시-나열한다(fail-open 3연발의 교훈 — 게이트는 열거형으로만):
    ① URL이 `/v0/<channel>/<from-x>`로 파싱된다 ② 봉투가 JSON으로 읽힌다
    ③ signer.id에서 문 이름이 파생된다 ④ 파생한 문 == URL의 문. 넷 다 참일 때만
    통과하고, 그 밖은 전부 거부다. 정당한 대리 전달은 `allow_foreign_door`로
    **명시**한다 — 실수가 아니라 결정이 되도록."""
    if allow_foreign_door:
        return
    loc = _split_path(urllib.parse.urlsplit(url).path)
    if loc is None:
        raise ValueError(
            f"push 대상 URL이 /v0/<channel>/<from-x> 꼴이 아니다: {url} — "
            "발신 문을 판정할 수 없어 거부한다(allow_foreign_door로 명시 가능)")
    try:
        signer_id = (json.loads(env_b.decode("utf-8")).get("signer") or {}).get("id")
    except (ValueError, UnicodeDecodeError, AttributeError):
        raise ValueError(
            "봉투를 JSON으로 읽을 수 없어 발신 문을 판정할 수 없다 — 거부한다"
        ) from None
    door = _door_for_signer(signer_id)
    if door is None:
        raise ValueError(
            f"서명자 {signer_id!r}에서 문 이름을 파생할 수 없어 거부한다"
            "(fail-closed) — 의도한 전달이면 allow_foreign_door로 명시하세요")
    if door != loc[1]:
        raise ValueError(
            f"남의 문에 쓰려 한다 — URL의 문 {loc[1]}, 이 봉투의 서명자 "
            f"{signer_id}(자기 문 {door}). 발신은 자기 문에만 하고, 다른 집이 읽게 "
            "하려면 자기 문에 올리면 된다(각자 pull한다). 대리 전달이 정말 의도라면 "
            "allow_foreign_door로 명시하세요")


def list_channels(url: str, token: str,
                  timeout: int = CLIENT_TIMEOUT_SECONDS,
                  warmup: bool = True, stats: dict | None = None) -> dict:
    """서버의 channel/sender 트리를 묻는다(0.4.9). `url`을 **그대로** GET한다 —
    CLI `--url`도 `…/v0/channels` 전체 경로를 받는다(base만 주면 서버가 404로
    "경로는 /v0/<channel>/<from-x>"를 돌려준다 — 09-11 우리 오용 실측, 0.5.1 정정).

    수거 목록을 기억이 아니라 서버에 묻기 위한 한 콜이다. 어느 랩의 수거기가
    채널 목록을 기억으로 들다 네 문 중 두 문만 보게 된 사고가 근거 — "영수증은
    수거가 일어났다고 말하지, 회차가 완전했다고 말한 적이 없다"(Ray). 수거기는
    회차 시작에 이 트리와 자기 목록을 대조하면 같은 병에서 벗어난다."""
    if warmup:
        _warm_measured(url, stats, timeout=timeout)
    return _request(url, token, timeout=timeout)


def fetch_page(url: str, token: str, since: str = "000",
               timeout: int = CLIENT_TIMEOUT_SECONDS,
               stats: dict | None = None) -> dict:
    """한 문의 한 페이지(`?since=NNN`, PAGE_SIZE) — 0.6.0 bbs_wire의 종류 탐색·재개용.
    `stats["pages"]`를 1 올린다(회차 GET 계수, Orin 040 §4)."""
    page = _request(f"{url}?since={since}", token, timeout=timeout)
    if stats is not None:
        stats["pages"] = stats.get("pages", 0) + 1
    return page


def pull_quads(url: str, token: str, dest: str | Path,
               since: str | None = None,
               timeout: int = CLIENT_TIMEOUT_SECONDS,
               warmup: bool = True, stats: dict | None = None) -> list[str]:
    """새 quad를 받아 로컬 우체통 트리에 내려쓴다. since 생략 시 로컬 최대 NNN부터.

    로컬도 append-only 우편함이다 — 이미 있는 파일은 절대 덮어쓰지 않는다.

    0.6.0(Ray 101 F1): envelope는 있는데 sig/body가 **빠진** quad는 서버가 다시 주면
    빠진 파일만 채운다(복구). 남아 있는 파일은 건드리지 않고, 남아 있는 파일의 bytes가
    서버와 다르면 ValueError로 **명시 오류**(손으로 바뀐 트리를 조용히 고치지 않는다).
    복구된 번호도 반환 목록에 들어가고 `stats["repaired"]`가 센다.

    `stats` dict를 주면 워밍 계측(`warm_ms`·`warm_ok`)과 정상 응답 페이지 수(`pages`)를
    채워 준다."""
    if warmup:
        _warm_measured(url, stats, timeout=timeout)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    if since is None:
        have = [int(f.name.split("-", 1)[0]) for f in dest.glob("*-envelope.json")
                if _N_RE.match(f.name.split("-", 1)[0])]
        since = f"{max(have):03d}" if have else "000"
    written: list[str] = []
    while True:
        page = fetch_page(url, token, since, timeout=timeout, stats=stats)
        for q in page["quads"]:
            n = q["n"]
            if not _N_RE.match(n):
                raise DropError(502, f"서버가 준 n이 형식 위반: {n!r}")
            env_p = dest / f"{n}-envelope.json"
            sig_p = dest / f"{n}-sig.txt"
            sig_bytes = (q["sig"] + "\n").encode("utf-8")
            body_p = body_bytes = None
            if q.get("body_name"):
                if not _BODY_NAME_RE.match(q["body_name"]):
                    raise DropError(502, f"서버가 준 body_name 형식 위반: "
                                         f"{q['body_name']!r}")
                body_p = dest / f"{n}-{q['body_name']}"
                body_bytes = base64.b64decode(q["body_b64"])
            if not env_p.exists():
                sig_p.write_bytes(sig_bytes)
                if body_p is not None:
                    body_p.write_bytes(body_bytes)
                env_p.write_bytes(base64.b64decode(q["envelope_b64"]))   # 완결 표지는 마지막
            else:
                # 완결된 로컬 quad(세 파일 다 있음)는 종전 계약대로 **무접촉**(0.4.x: 이미 받은
                # 것은 손대지 않는다 — 변조는 read의 서명 검증이 잡는다). **복구가 필요한
                # quad**(동반 파일이 빠짐)만 042 R3: 남아 있는 파일 전부(envelope 포함)를
                # 먼저 대조하고, 하나라도 서버 bytes와 다르면 아무것도 쓰지 않고 명시 오류 —
                # 충돌하는 quad에 복구 성공 표시가 남지 않는다. 대조는 불투명 bytes 동일성.
                trio = ((env_p, base64.b64decode(q["envelope_b64"]), "envelope"),
                        (sig_p, sig_bytes, "sig"), (body_p, body_bytes, "body"))
                missing = [t for t in trio if t[0] is not None and not t[0].exists()]
                if missing:
                    for path, want, label in trio:
                        if path is not None and path.exists() and path.read_bytes() != want:
                            raise ValueError(
                                f"{path.name}: 로컬 {label} bytes가 서버와 다르다 — 덮어쓰지 "
                                "않는다(손으로 바뀐 트리는 사람이 판정한다)")
                    for path, want, _label in missing:
                        path.write_bytes(want)
                    if stats is not None:
                        stats["repaired"] = stats.get("repaired", 0) + 1
            written.append(n)                  # 반환 = 페이지에서 본 번호(종전 의미 유지)
            since = n
        if not page.get("more"):
            return written
