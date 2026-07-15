"""organum relay — 지향(directed) 우체통 정책. field substrate 위의 얇은 층 (field='relay').

세포가 서로 콕 집어 협업하는 채널(핸드오프·targeted). **경계: organum은 매체+도구만 제공하지
조율하지 않는다** — 세포가 스스로 pull(inbox/watch)하고 reply(send)한다. 편지 = `.organum/relay/`의
.md 1개(from/to/ts/topic/src [+선택 thread/in_reply_to]). 읽음 커서 `.read-<id>` · 가입 `.join-<id>`.
substrate·엔벨로프·watch는 [[field]]가 소유; relay는 directed=True 정책만 고정한다. agora=개방 정책은 형제.
"""

from __future__ import annotations

import time
from pathlib import Path

from organum import field as _f

FIELD = "relay"

# 하위호환 재-export (외부에서 relay.parse_msg/slug를 참조하던 경우)
slug = _f.slug
parse_msg = _f.parse_msg


def relay_dir(cwd: Path) -> Path:
    return _f.field_dir(cwd, FIELD)


def send(cwd: Path, body: str, frm: str = "cell", to: str = "all", topic: str = "",
         src: str = "relay-cli", thread: str = "", reply_to: str = "",
         escalate: bool = False) -> str | None:
    """편지 드롭 (지향). 빈 본문이면 None. thread/reply_to = 스레딩(부모 thread 상속).
    escalate=True = human 개입 요청 — 관제탑 에스컬레이션 패널에 뜬다."""
    return _f.post(cwd, FIELD, body, frm=frm, to=to, topic=topic, src=src,
                   thread=thread, reply_to=reply_to, escalate=escalate)


def list_all(cwd: Path, limit: int = 60) -> list:
    return _f.list_all(cwd, FIELD, limit=limit)


def archive(cwd: Path, filename: str) -> bool:
    return _f.archive(cwd, FIELD, filename)


def mark_read(cwd: Path, for_id: str, filename: str) -> None:
    return _f.mark_read(cwd, FIELD, for_id, filename)


def mark_join(cwd: Path, for_id: str) -> str:
    return _f.mark_join(cwd, FIELD, for_id)


def inbox(cwd: Path, for_id: str, include_read: bool = False) -> list:
    """나(for_id)에게 온 안 읽은 편지 — to=all/내 id, 내 편지 제외, 가입 이후. 오래된 순 (directed)."""
    return _f.feed(cwd, FIELD, for_id, include_read=include_read, directed=True)


def watch(cwd: Path, for_id: str, on_msg, interval: float = 3.0, idle: float = 600.0,
          mark: bool = True, _sleep=time.sleep, _now=time.time, max_polls: int | None = None) -> int:
    """무데몬 저지연 폴러 — 나에게 온 새 편지를 오는 대로 on_msg(m)에 (directed)."""
    return _f.watch(cwd, FIELD, for_id, on_msg, interval=interval, idle=idle, mark=mark,
                    directed=True, _sleep=_sleep, _now=_now, max_polls=max_polls)
