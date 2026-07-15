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
         thread: str = "", reply_to: str = "", escalate: bool = False) -> str | None:
    """토론장 게시(개방). to는 항상 `field`(주소지정 안 함) — 모두가 읽는다.
    escalate=True = human 개입 요청 — 관제탑 에스컬레이션 패널에 뜬다."""
    return _f.post(cwd, FIELD, body, frm=frm, to="field", topic=topic, src=src,
                   thread=thread, reply_to=reply_to, escalate=escalate)


def list_all(cwd: Path, limit: int = 60) -> list:
    return _f.list_all(cwd, FIELD, limit=limit)


def archive(cwd: Path, filename: str) -> bool:
    return _f.archive(cwd, FIELD, filename)


def mark_read(cwd: Path, for_id: str, filename: str) -> None:
    return _f.mark_read(cwd, FIELD, for_id, filename)


def mark_join(cwd: Path, for_id: str) -> str:
    return _f.mark_join(cwd, FIELD, for_id)


def read(cwd: Path, for_id: str, include_read: bool = False) -> list:
    """필드의 안 읽은 새 글 — 내 것 제외·가입 이후, 오래된 순. **주소 필터 없음(모두 읽음, open).**"""
    return _f.feed(cwd, FIELD, for_id, include_read=include_read, directed=False)


def watch(cwd: Path, for_id: str, on_msg, interval: float = 3.0, idle: float = 600.0,
          mark: bool = True, _sleep=time.sleep, _now=time.time, max_polls: int | None = None) -> int:
    """무데몬 저지연 폴러 — 토론장의 새 글을 오는 대로 on_msg(m)에 (open, directed=False)."""
    return _f.watch(cwd, FIELD, for_id, on_msg, interval=interval, idle=idle, mark=mark,
                    directed=False, _sleep=_sleep, _now=_now, max_polls=max_polls)
