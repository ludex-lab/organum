"""organum field — 공유 조율 필드의 제네릭 substrate (relay·agora·council이 얹히는 바닥).

한 필드 = `.organum/<field>/`의 provenance-태그 엔벨로프 모음(.md 파일) + reader별 읽음 커서 + 가입(join).
**정책(지향 vs 개방)은 feed()의 `directed` 플래그 하나로 갈린다** — relay=directed(주소지정), agora=open
(모두 읽음). 관점-로컬(각자 자기 파일, 공유 가변 클로버링 없음, §2.1-⑤) · 무데몬(watch=짧게 사는 폴러) ·
스레드(thread/in_reply_to, additive) · format v0 호환.

**경계: organum은 매체(필드)+규율만; 세포가 스스로 pull(feed/watch)·post 한다. 라이브 버스/데몬 아님.**
"""

from __future__ import annotations

import re
import time
from pathlib import Path

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


def post(cwd: Path, field: str, body: str, frm: str = "cell", to: str = "all", topic: str = "",
         src: str = "cli", thread: str = "", reply_to: str = "", escalate: bool = False) -> str | None:
    """엔벨로프 드롭. 파일명은 서버가 정한다(경로 주입 차단). 빈 본문이면 None.

    thread/reply_to = 대화 스레딩. reply_to를 주면 부모의 thread를 상속(없으면 부모 파일명이 루트).
    escalate = human 개입 요청 플래그 — 관제탑이 표면화한다. '처리'는 human의 archive(엔벨로프 불변)."""
    body = (body or "").strip()
    if not body:
        return None
    frm = _fm_safe(frm)[:40]
    to = _fm_safe(to)[:80]
    topic = _fm_safe(topic)[:80]
    src = _fm_safe(src)[:40]
    thread = _fm_safe(thread)[:120]
    reply_to = _fm_safe(reply_to)[:120]
    if reply_to and not thread:
        pm = get_meta(cwd, field, reply_to)
        thread = ((pm.get("thread") if pm else "") or "").strip() or reply_to
    d = field_dir(cwd, field)
    d.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    stem = (f"{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"
            f"-{slug(frm, 'cell')}-to-{slug(to, 'all')}-{slug(topic)}")
    fm = f"---\nfrom: {frm}\nto: {to}\nts: {ts}\ntopic: {topic}\nsrc: {src}\n"
    if escalate:
        fm += "escalate: true\n"
    if thread:
        fm += f"thread: {thread}\n"
    if reply_to:
        fm += f"in_reply_to: {reply_to}\n"
    # append-only 계약(불변조건 ①): 편지 한 건은 절대 다른 편지를 덮어쓰지 않는다 —
    # 같은 초·같은 조합이면 -2, -3… 유일 접미. O_EXCL("x")이 경쟁 세포 간 원자성 보증.
    for i in range(1, 1000):
        fname = f"{stem}.md" if i == 1 else f"{stem}-{i}.md"
        try:
            with open(d / fname, "x", encoding="utf-8") as f:
                f.write(fm + f"---\n{body}\n")
            return fname
        except FileExistsError:
            continue
    return None


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
            "topic": meta.get("topic", ""), "ts": meta.get("ts", ""),
            "thread": meta.get("thread", ""), "in_reply_to": meta.get("in_reply_to", ""),
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
    return field_dir(cwd, field) / (".read-" + slug(for_id, "x"))


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
    return field_dir(cwd, field) / (".join-" + slug(for_id, "x"))


def mark_join(cwd: Path, field: str, for_id: str) -> str:
    """세포 가입(입장) 기록 — 이 시각 이후 글만 feed에 뜬다(가입 전 옛 것 무시). 최초 join 시각 보존."""
    p = _join_path(cwd, field, for_id)
    if p.is_file():
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
    f8 = (for_id or "")[:8]
    return any(t.strip()[:8] == f8 for t in to.split(","))


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
        if (m["from"] or "")[:8] == (for_id or "")[:8]:
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
