"""organum agora — 개방(open) 토론장 정책. field substrate 위의 얇은 층 (field='agora').

**지향 주소지정이 없다** — 필드에 게시하면 *모두가 읽는다*(register/목소리 지속, Ludex Agora 계보).
relay와 엔벨로프·스레드·watch·읽음커서·가입을 [[field]]에서 공유하되, feed는 **directed=False**(주소 필터
없음). 허심탄회한 열린 심의 — 디스패처도 없고, "다 보임"이 버그가 아니라 기능이다.

경계: 매체(필드)+규율만; 세포가 스스로 pull(read/watch)·post. 다양성 붕괴를 막는 관점-로컬(각자 자기
파일 append, 검증으로 수렴 — 단일 가변 클로버링 없음, §2.1-⑤).
"""

from __future__ import annotations

import time
from pathlib import Path

from organum import field as _f

FIELD = "agora"


def agora_dir(cwd: Path) -> Path:
    return _f.field_dir(cwd, FIELD)


def post(cwd: Path, body: str, frm: str = "cell", topic: str = "", src: str = "agora-cli",
         thread: str = "", reply_to: str = "", escalate: bool = False, from_id: str = "",
         idem_key: str = "") -> str | None:
    """토론장 게시(개방). to는 항상 `field`(주소지정 안 함) — 모두가 읽는다.
    escalate=True = human 개입 요청 — 관제탑 에스컬레이션 패널에 뜬다.
    from_id = canonical sender identity(display frm과 분리 — 자기제외 판정용).
    idem_key = 멱등 토큰(재전송 dedup)."""
    return _f.post(cwd, FIELD, body, frm=frm, to="field", topic=topic, src=src, from_id=from_id,
                   thread=thread, reply_to=reply_to, escalate=escalate, idem_key=idem_key)


def list_all(cwd: Path, limit: int = 60) -> list:
    return _f.list_all(cwd, FIELD, limit=limit)


GOAL_TOPIC = "goal"


def latest_goal(cwd: Path, topic: str = GOAL_TOPIC) -> dict | None:
    """현재 canonical goal — agora의 `topic:goal` 글 중 **최신 한 건**(전체 envelope) 또는 None.
    **cursor/join과 무관**(전체 agora 스캔) — 늦게 join한 셀도 pre-existing goal을 복구한다(R2).
    일반 post는 goal을 교체 안 함(topic 필터). 최신 = `(ts, file)` 최대(동률도 파일명 tie-break로
    deterministic). frontmatter만 훑어 최신 goal 파일을 고른 뒤 그 한 건만 본문까지 읽는다."""
    d = _f.field_dir(cwd, FIELD)
    if not d.is_dir():
        return None
    topic = topic.strip().lower()
    best_key = best_file = None
    for p in d.glob("*.md"):
        meta = _f.get_meta(cwd, FIELD, p.name)
        if not meta or (meta.get("topic") or "").strip().lower() != topic:
            continue
        # 최신순 = (ts, mtime, file). ts=초-granularity라 같은 초 갱신은 mtime(sub-second, 생성순)으로
        # tie-break — 파일명만 쓰면 '-2.md'<'.md'(0x2d<0x2e)라 최신을 못 고른다(hub와 같은 함정).
        try:
            key = (meta.get("ts") or "", p.stat().st_mtime, p.name)
        except OSError:
            continue
        if best_key is None or key > best_key:
            best_key, best_file = key, p.name
    if best_file is None:
        return None
    try:
        meta, body = _f.parse_msg((d / best_file).read_text(encoding="utf-8"))
    except OSError:
        return None
    return {
        "file": best_file, "from": meta.get("from", "?"), "from_id": meta.get("from_id", ""),
        "to": meta.get("to", "field"), "topic": meta.get("topic", ""), "ts": meta.get("ts", ""),
        "thread": meta.get("thread", ""), "in_reply_to": meta.get("in_reply_to", ""),
        "escalate": (meta.get("escalate", "").lower() == "true"),
        "body": body.strip()[:4000],
    }


def archive(cwd: Path, filename: str) -> bool:
    return _f.archive(cwd, FIELD, filename)


def mark_read(cwd: Path, for_id: str, filename: str) -> None:
    return _f.mark_read(cwd, FIELD, for_id, filename)


def mark_join(cwd: Path, for_id: str, reset: bool = False) -> str:
    return _f.mark_join(cwd, FIELD, for_id, reset=reset)


def read(cwd: Path, for_id: str, include_read: bool = False) -> list:
    """필드의 안 읽은 새 글 — 내 것 제외·가입 이후, 오래된 순. **주소 필터 없음(모두 읽음, open).**"""
    return _f.feed(cwd, FIELD, for_id, include_read=include_read, directed=False)


def watch(cwd: Path, for_id: str, on_msg, interval: float = 3.0, idle: float = 600.0,
          mark: bool = True, _sleep=time.sleep, _now=time.time, max_polls: int | None = None) -> int:
    """무데몬 저지연 폴러 — 토론장의 새 글을 오는 대로 on_msg(m)에 (open, directed=False)."""
    return _f.watch(cwd, FIELD, for_id, on_msg, interval=interval, idle=idle, mark=mark,
                    directed=False, _sleep=_sleep, _now=_now, max_polls=max_polls)
