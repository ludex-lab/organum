"""organum field — 공유 조율 필드의 제네릭 substrate (relay·agora·council이 얹히는 바닥).

한 필드 = `.organum/<field>/`의 provenance-태그 엔벨로프 모음(.md 파일) + reader별 읽음 커서 + 가입(join).
**정책(지향 vs 개방)은 feed()의 `directed` 플래그 하나로 갈린다** — relay=directed(주소지정), agora=open
(모두 읽음). 관점-로컬(각자 자기 파일, 공유 가변 클로버링 없음, §2.1-⑤) · 무데몬(watch=짧게 사는 폴러) ·
스레드(thread/in_reply_to, additive) · format v0 호환.

**경계: organum은 매체(필드)+규율만; 세포가 스스로 pull(feed/watch)·post 한다. 라이브 버스/데몬 아님.**
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import time
from pathlib import Path

from organum import state as st


class PostConflict(ValueError):
    """같은 idem-key·같은 발신자인데 payload가 다름 — exactly-once 위반(fail-closed).
    조용히 옛 봉투를 반환하면 caller가 새 payload를 게시했다고 오인한다."""


def _idem_fp(body: str, to: str, topic: str, thread: str, reply_to: str,
             escalate: bool, extra: dict | None) -> str:
    """멱등 payload 지문 — 같은 idem-key 재사용이 같은 의미 payload인지 검증용. body + 목적지 스냅샷
    (to=확정 to_id, extra의 to_epoch 등) + 스레딩/escalate. **event_id는 매 호출 새 uuid라 제외**
    (포함하면 모든 재시도가 conflict)."""
    parts = [body, to, topic, thread, reply_to, "1" if escalate else "0"]
    if extra:
        for k in sorted(extra):
            if k == "event_id":
                continue
            parts.append(f"{k}={extra[k]}")
    return hashlib.sha256("\x00".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]

_SLUG_RE = re.compile(r"[^0-9A-Za-z가-힣_-]+")


def field_dir(cwd: Path, field: str) -> Path:
    return cwd / ".organum" / field


def slug(s: str, default: str = "msg") -> str:
    s = _SLUG_RE.sub("-", (s or "").strip()).strip("-")[:40]
    return s or default


def _fm_safe(s: str) -> str:
    """frontmatter 값 새니타이즈 — 직렬화 경계와 파서(splitlines)의 '줄 정의'를 맞춘다.
    \\r\\n뿐 아니라 splitlines가 줄로 보는 모든 유니코드/제어 구분자(LS·PS·NEL·VT·FF·RS…)를
    합쳐야 메타데이터 주입이 막힌다(critic 재감사 ②: parse_msg가 splitlines를 쓴다)."""
    return " ".join(str(s).splitlines()).strip()


def parse_msg(text: str) -> tuple[dict, str]:
    meta, body = {}, text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                if ":" in line:
                    k, _, val = line.partition(":")
                    meta[k.strip()] = val.strip()
            body = text[end + 4:]
    return meta, body


def get_meta(cwd: Path, field: str, filename: str) -> dict | None:
    """엔벨로프 frontmatter만 읽는다(스레드 상속용). 경로 주입 차단."""
    if not (filename.endswith(".md") and "/" not in filename and "\\" not in filename
            and not filename.startswith(".")):
        return None
    p = field_dir(cwd, field) / filename
    if not p.is_file():
        return None
    try:
        meta, _ = parse_msg(p.read_text(encoding="utf-8"))
        return meta
    except OSError:
        return None


def _find_idem(cwd: Path, field: str, idem_key: str, from_id: str = "") -> str | None:
    """멱등키로 기존 게시 찾기 — 같은 idem(있으면 같은 from_id)의 봉투가 이미 있으면 그 파일명.
    재전송(post/send timeout 후) 중복 방지 = exactly-once publish. **순차 재시도 가정**: 진짜 동시
    동일-idem은 원자 dedup 안 함(v0 — 단일 actor의 재시도는 순차라 무해)."""
    if not idem_key:
        return None
    d = field_dir(cwd, field)
    if not d.is_dir():
        return None
    for p in sorted(d.glob("*.md")):
        try:
            meta, _ = parse_msg(p.read_text(encoding="utf-8"))
        except OSError:
            continue
        if (meta.get("idem", "") or "").strip() != idem_key:
            continue
        ex_fid = (meta.get("from_id", "") or "").strip()
        if ex_fid != from_id:
            continue  # scope=(from_id, idem-key) 정확 비교 — 빈 발신자도 식별 발신자와 별개(critic A2)
        return p.name
    return None


def post(cwd: Path, field: str, body: str, frm: str = "cell", to: str = "all", topic: str = "",
         src: str = "cli", thread: str = "", reply_to: str = "", escalate: bool = False,
         from_id: str = "", idem_key: str = "", extra: dict | None = None) -> str | None:
    """엔벨로프 드롭. 파일명은 서버가 정한다(경로 주입 차단). 빈 본문이면 None.

    thread/reply_to = 대화 스레딩. reply_to를 주면 부모의 thread를 상속(없으면 부모 파일명이 루트).
    escalate = human 개입 요청 플래그 — 관제탑이 표면화한다. '처리'는 human의 archive(엔벨로프 불변).
    idem_key = 멱등 토큰: 같은 키의 봉투가 이미 있으면 새로 만들지 않고 그 파일명을 반환(재전송 dedup).
    extra = 필드-특정 추가 frontmatter(허브의 to_id/epoch/event_id 등). 키는 [a-z][a-z0-9_]*, 값은
    _fm_safe로 새니타이즈(개행 제거)·200자 캡 — 메타데이터 주입 차단."""
    body = (body or "").strip()
    if not body:
        return None
    idem_key = _fm_safe(idem_key)[:80]
    # canonical sender identity — display frm과 분리(A-P1). **raw 값을 변형 전에 검증**(재감사5
    # A-blocker1): valid_cell_id로 먼저 거른 뒤에만 cell_key 정규화 → invalid는 봉투에서 생략(변형 금지).
    fid_norm = st.cell_key(from_id) if (from_id and st.valid_cell_id(from_id)) else ""
    frm = _fm_safe(frm)[:40]
    to = _fm_safe(to)[:80]
    topic = _fm_safe(topic)[:80]
    src = _fm_safe(src)[:40]
    thread = _fm_safe(thread)[:120]
    reply_to = _fm_safe(reply_to)[:120]
    if reply_to and not thread:
        pm = get_meta(cwd, field, reply_to)
        thread = ((pm.get("thread") if pm else "") or "").strip() or reply_to
    idem_fp = ""
    if idem_key:  # 멱등: 같은 키(+발신자) 봉투가 있으면 payload 지문 일치 시 그 파일명, 다르면 conflict
        idem_fp = _idem_fp(body, to, topic, thread, reply_to, escalate, extra)
        dup = _find_idem(cwd, field, idem_key, fid_norm)
        if dup:
            try:
                dmeta, _ = parse_msg((field_dir(cwd, field) / dup).read_text(encoding="utf-8"))
            except OSError:
                dmeta = {}
            if (dmeta.get("idem_fp") or "") != idem_fp:  # 같은 키·다른 payload = conflict(fail-closed)
                raise PostConflict(
                    f"idem-key '{idem_key}' 재사용에 다른 payload — conflict(exactly-once 위반).")
            return dup
    d = field_dir(cwd, field)
    d.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    stem = (f"{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"
            f"-{slug(frm, 'cell')}-to-{slug(to, 'all')}-{slug(topic)}")
    fm = f"---\nfrom: {frm}\nto: {to}\nts: {ts}\ntopic: {topic}\nsrc: {src}\n"
    # canonical sender identity — display 'from'과 분리(A-P1). self-exclusion이 이것만 본다.
    # fid_norm은 위에서 raw 검증 후 정규화(invalid=""). marker `{1,40}` 절단류 승격을 원천 차단.
    if fid_norm:
        fm += f"from_id: {fid_norm}\n"
    if idem_key:  # 멱등 토큰 — 재전송 dedup의 근거(_find_idem이 이 필드를 본다)
        fm += f"idem: {idem_key}\n"
    if idem_fp:  # payload 지문 — 같은 키 재사용의 payload 일치 검증(conflict 감지)
        fm += f"idem_fp: {idem_fp}\n"
    if extra:  # 필드-특정 frontmatter (허브 to_id/epoch/event_id 등) — 키·값 새니타이즈
        for k in sorted(extra):
            if not re.fullmatch(r"[a-z][a-z0-9_]*", k):
                continue
            v = _fm_safe(str(extra[k]))[:200]
            if v:
                fm += f"{k}: {v}\n"
    if escalate:
        fm += "escalate: true\n"
    if thread:
        fm += f"thread: {thread}\n"
    if reply_to:
        fm += f"in_reply_to: {reply_to}\n"
    content = fm + f"---\n{body}\n"
    # 원자적 append-before-publish: 완결된 temp에 하드링크를 걸어 최종 파일이 *완전한 상태로*
    # 나타나게 한다(live reader가 partial frontmatter를 못 본다). O_EXCL 대신 os.link —
    # 대상 존재 시 FileExistsError로 덮어쓰기 금지(불변조건 ①)까지 함께 지킨다. temp는
    # '.'+'.tmp'라 *.md 글롭에 절대 안 잡힌다. (Codex live-transport 조사 P0.)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        for i in range(1, 1000):  # 같은 초·같은 조합이면 -2, -3… 유일 접미
            fname = f"{stem}.md" if i == 1 else f"{stem}-{i}.md"
            try:
                os.link(tmp, d / fname)
                return fname
            except FileExistsError:
                continue
        return None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def list_all(cwd: Path, field: str, limit: int = 60) -> list:
    d = field_dir(cwd, field)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
        try:
            meta, body = parse_msg(p.read_text(encoding="utf-8"))
        except OSError:
            continue
        out.append({
            "file": p.name, "from": meta.get("from", "?"), "to": meta.get("to", "all"),
            "from_id": meta.get("from_id", ""),   # canonical sender identity(있으면) — 자기제외 판정용
            "topic": meta.get("topic", ""), "ts": meta.get("ts", ""),
            "thread": meta.get("thread", ""), "in_reply_to": meta.get("in_reply_to", ""),
            "idem": meta.get("idem", ""),         # 멱등 토큰(있으면) — 재전송 상관용
            "escalate": meta.get("escalate", "").lower() == "true",
            "body": body.strip()[:4000],
        })
    return out


def archive(cwd: Path, field: str, filename: str) -> bool:
    """소프트 삭제 — 하드 삭제 대신 `.archive/`로 이동(가역성 보존, §2.1-④). 엔벨로프는 불변 provenance라
    *수정은 없다*; human의 정리는 보관으로만. 경로 주입 차단."""
    if not (filename.endswith(".md") and "/" not in filename and "\\" not in filename
            and not filename.startswith(".")):
        return False
    d = field_dir(cwd, field)
    src = d / filename
    if not src.is_file():
        return False
    arc = d / ".archive"
    arc.mkdir(parents=True, exist_ok=True)
    src.rename(arc / filename)
    return True


def _read_path(cwd: Path, field: str, for_id: str) -> Path:
    return field_dir(cwd, field) / (".read-" + st.cell_key(for_id))  # cell_key 통일(옛 slug은 a.b/a-b 충돌)


def read_set(cwd: Path, field: str, for_id: str) -> set:
    p = _read_path(cwd, field, for_id)
    if not p.is_file():
        return set()
    try:
        return {l.strip() for l in p.read_text(encoding="utf-8").splitlines() if l.strip()}
    except OSError:
        return set()


def mark_read(cwd: Path, field: str, for_id: str, filename: str) -> None:
    p = _read_path(cwd, field, for_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    if filename not in read_set(cwd, field, for_id):
        with open(p, "a", encoding="utf-8") as f:
            f.write(filename + "\n")


def _join_path(cwd: Path, field: str, for_id: str) -> Path:
    return field_dir(cwd, field) / (".join-" + st.cell_key(for_id))


def mark_join(cwd: Path, field: str, for_id: str, reset: bool = False) -> str:
    """세포 가입(입장) 기록 — 이 시각 이후 글만 feed에 뜬다(가입 전 옛 것 무시). 기본은 최초 join
    시각 보존(멱등). reset=True면 커서를 now로 갱신 — cell id 재사용 시 옛 라운드 커서 상속으로
    history flood 나던 것 차단(dogfood: critic이 옛 세션 커서로 300줄 익사). organum join의
    새 세션 온보딩만 reset=True로 부른다."""
    p = _join_path(cwd, field, for_id)
    if p.is_file() and not reset:
        return join_ts(cwd, field, for_id) or ""
    p.parent.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    p.write_text(ts, encoding="utf-8")
    return ts


def join_ts(cwd: Path, field: str, for_id: str) -> str | None:
    p = _join_path(cwd, field, for_id)
    if not p.is_file():
        return None
    try:
        return p.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def addressed(to: str, for_id: str) -> bool:
    if to == "all":
        return True
    # full cell identity로 비교(옛 [:8]은 1~40자 계약을 8자 prefix로 붕괴 → 오배송, critic A-blocker).
    me = st.cell_key(for_id)
    return any(st.cell_key(t) == me for t in to.split(","))


def feed(cwd: Path, field: str, for_id: str, include_read: bool = False,
         directed: bool = True) -> list:
    """안 읽은 새 글 — **가입 이후 · 내 것 제외 · 안 읽음**, 오래된 순.

    directed=True(relay): to=all/내 id인 것만(주소지정). directed=False(agora): 주소 필터 없음(모두 읽음).
    """
    read = set() if include_read else read_set(cwd, field, for_id)
    join = join_ts(cwd, field, for_id)  # 가입 전 옛 글 무시 (ISO ts 문자열 비교)
    out = []
    for m in list_all(cwd, field, limit=200):
        if join and (m["ts"] or "") < join:
            continue
        if directed and not addressed(m["to"], for_id):
            continue
        # 자기 글 제외 — canonical sender identity(from_id)가 **있을 때만** 판정(재감사4 Blocker3).
        # from_id 없음(자유 display이든 legacy이든)은 identity 미상 → 제외 안 함. `--from Alice` 같은
        # canonical-looking 자유 이름을 legacy fallback으로 identity 승격시켜 false-exclude하던 것 차단
        # ("false exclusion보다 replay가 안전"). 전부 cell_key(case-insensitive) 비교.
        fid = (m.get("from_id") or "").strip()
        if fid and st.cell_key(fid) == st.cell_key(for_id or ""):
            continue
        if m["file"] in read:
            continue
        out.append(m)
    return list(reversed(out))


def watch(cwd: Path, field: str, for_id: str, on_msg, interval: float = 3.0, idle: float = 600.0,
          mark: bool = True, directed: bool = True, _sleep=time.sleep, _now=time.time,
          max_polls: int | None = None) -> int:
    """무데몬 저지연 폴러 — 새 글을 오는 대로 on_msg(m)에 넘긴다.

    **상주 서비스가 아니라** 세포가 띄우는 짧게 사는 자식: idle초 동안 새 글이 없으면 자멸한다
    (agmsg 'push=짧은 폴러' 패턴 — 저지연 비동기를 데몬·소켓 없이). mark=True면 전달 즉시 읽음 표시
    → 다음 폴은 *새* 것만. 반환 = 전달한 글 수. (max_polls는 테스트용 상한.)"""
    seen = 0
    last = _now()
    polls = 0
    while max_polls is None or polls < max_polls:
        polls += 1
        msgs = feed(cwd, field, for_id, directed=directed)
        for m in msgs:
            on_msg(m)
            if mark:
                mark_read(cwd, field, for_id, m["file"])
            seen += 1
        if msgs:
            last = _now()
        elif _now() - last >= idle:
            break
        _sleep(interval)
    return seen
