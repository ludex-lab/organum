"""organum alarm — 경보 필드 (개미 경보 페로몬 계보). field substrate 위의 세 번째 정책 (field='alarm').

이상 신호·개입이 필요할 때 **human/chief만 발동(sound)**하고 모두가 읽는 긴급 채널.
level=pause 경보(전체 또는 특정 셀 지정)가 활성이면 워커는 진행 중인 원자 작업만 마치고
멈춘 뒤 ACK한다 — **정지는 organum이 강제하는 게 아니라 세포의 규율**(COORDINATION_DISCIPLINE).

경계: 매체+규율만. 발동 권한은 도구 층(CLI/MCP/web)에서 집행한다 — 파일 매체 자체는 열려
있으므로 soft enforcement임을 정직하게 남긴다(roster single-writer와 같은 결). 해제(resolve)는
human의 보관(archive, 가역) — 엔벨로프 불변.
"""

from __future__ import annotations

import time
from pathlib import Path

from organum import field as _f

FIELD = "alarm"
LEVELS = ("notice", "pause")


class AlarmError(Exception):
    """경보 규율 위반 (발동 권한 없음 · 잘못된 level)."""


def alarm_dir(cwd: Path) -> Path:
    return _f.field_dir(cwd, FIELD)


def can_sound(state_dir: Path, frm: str) -> bool:
    """발동 권한: human 또는 열린 세션 역할이 chief인 세포. (도구-층 집행, soft)"""
    if (frm or "").strip().lower() == "human":
        return True
    from organum import session as _sess
    from organum import state as _st
    soma = _st.soma_dir(state_dir, frm)
    s = _sess.status(soma)
    return bool(s and s.get("role") == "chief")


def sound(cwd: Path, state_dir: Path, body: str, frm: str, to: str = "all",
          level: str = "notice", src: str = "alarm-cli") -> str | None:
    """경보 발동 — human/chief만. level: notice(주의) · pause(정지 권고). 빈 본문이면 None.

    level은 엔벨로프 topic 자리에 실린다(substrate 무변경 — alarm 정책의 규약)."""
    if level not in LEVELS:
        raise AlarmError(f"level은 {'/'.join(LEVELS)} 중 하나 — '{level}'은 없습니다.")
    if not can_sound(state_dir, frm):
        raise AlarmError(f"'{frm}'은 경보를 발동할 수 없습니다 — human 또는 chief(열린 세션)만.")
    return _f.post(cwd, FIELD, body, frm=frm, to=to, topic=level, src=src)


def active(cwd: Path, for_id: str | None = None) -> list:
    """활성(미해제) 경보, 최신순. for_id를 주면 그 세포에게 유효한 것만(to=all/내 id)."""
    out = []
    for m in _f.list_all(cwd, FIELD, limit=60):
        if for_id and not _f.addressed(m["to"], for_id):
            continue
        out.append({**m, "level": m.get("topic") or "notice"})
    return out


def resolve(cwd: Path, filename: str) -> bool:
    """경보 해제 = 소프트 보관 (가역, 엔벨로프 불변)."""
    return _f.archive(cwd, FIELD, filename)


def mark_read(cwd: Path, for_id: str, filename: str) -> None:
    return _f.mark_read(cwd, FIELD, for_id, filename)


def watch(cwd: Path, for_id: str, on_msg, interval: float = 3.0, idle: float = 600.0,
          mark: bool = True, _sleep=time.sleep, _now=time.time, max_polls: int | None = None) -> int:
    """무데몬 저지연 폴러 — 나에게 유효한(to=all/내 id) 새 경보를 오는 대로 on_msg(m)에."""
    return _f.watch(cwd, FIELD, for_id, on_msg, interval=interval, idle=idle, mark=mark,
                    directed=True, _sleep=_sleep, _now=_now, max_polls=max_polls)
