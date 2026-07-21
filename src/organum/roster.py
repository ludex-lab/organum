"""organum roster — 현장(work-site)의 세포 presence 로스터.

누가 서식지에 있고 · 무엇을 하며 · 말을 걸 수 있는지. 모든 회람(relay·agora·council)이 이걸 참조한다.

**경계: 서술적 로스터지 배정이 아니다** (shared-cognition §8) — 누가 있나를 *보여줄* 뿐, 태스크를
*지정*하지 않는다. 이게 관제탑↔관제사 선.

두 겹:
- **선언(declared) presence** — 세포가 스스로 쓰는 의도: `.organum/roster/<cell_key>.json` (full cell id,
  case-insensitive 소문자 정규화 — 옛 8자 절단 폐지, critic 재감사3)
  (name·focus·open_to). **single-writer per locus** — 각자 *자기* 파일만 쓴다(공유 가변 클로버링 없음,
  §2.1-⑤). atomic write(temp+rename).
- **파생(derived) 관찰** — transcript에서 자동으로 나오는 것(model·origin·live). 이건 inspect가 이미
  계산하며, 여기선 `merge()`로 병합만 한다(로스터 모듈은 transcript에 의존하지 않는다 — 파생은 주입).

liveness는 파일 mtime이 아니라 **마지막 활동/heartbeat 기준**(유령 세포 교훈).
"""

from __future__ import annotations

import datetime
import json
import os
import re
import time
from pathlib import Path

from organum import state as st

_DECL_FIELDS = ("name", "focus", "open_to", "joined_at", "last_beat", "brain")


def roster_dir(cwd: Path) -> Path:
    return cwd / ".organum" / "roster"


def _key(s: str) -> str:
    # presence 파일명·레코드 id = full cell identity(st.cell_key). 옛 _id8의 앞 8자 절단이 1~40자
    # 계약을 8자 equivalence class로 붕괴시켜 prefix-충돌 셀을 한 presence로 뭉갰다(critic A-blocker).
    return st.cell_key(s)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _atomic_write(p: Path, obj: dict) -> None:
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)  # 원자적 교체 — 부분 쓰기 노출 없음


def _read_one(p: Path) -> dict | None:
    if not p.is_file():
        return None
    try:
        o = json.loads(p.read_text(encoding="utf-8"))
        return o if isinstance(o, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def write_presence(cwd: Path, for_id: str, name: str | None = None, focus: str | None = None,
                   open_to: list | None = None, brain: str | None = None) -> dict:
    """내 presence 파일 생성/갱신 (부분 업데이트). 넘긴 필드만 바꾸고 last_beat는 항상 새로고침.
    최초 쓰기 시 joined_at 설정. 인자 없이 호출하면 순수 heartbeat."""
    i8 = _key(for_id)
    d = roster_dir(cwd)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{i8}.json"
    cur = _read_one(p) or {"id": i8, "joined_at": _now()}
    if name is not None:
        cur["name"] = str(name).strip()[:60]
    if focus is not None:
        cur["focus"] = str(focus).strip()[:200]
    if brain is not None:
        cur["brain"] = str(brain).strip()[:60]
    if open_to is not None:
        cur["open_to"] = [str(x).strip()[:24] for x in open_to if str(x).strip()][:6]
    cur["id"] = i8
    cur.setdefault("joined_at", _now())
    cur["last_beat"] = _now()
    _atomic_write(p, cur)
    return cur


def reset_presence(cwd: Path, for_id: str) -> None:
    """presence 파일 삭제 — 새 session epoch 시작 시 stale identity metadata(옛 brain·name·open_to·
    joined_at)를 지운다(critic A5: id 재사용 시 선언된 stale brain이 derived보다 우선하면 관측
    attribution 오염). 이후 write_presence가 fresh presence를 만든다."""
    p = roster_dir(cwd) / f"{_key(for_id)}.json"
    p.unlink(missing_ok=True)


def beat(cwd: Path, for_id: str) -> None:
    """가벼운 heartbeat — presence의 last_beat만 새로고침(다른 organum 명령이 부를 수 있다)."""
    write_presence(cwd, for_id)


def read_presence(cwd: Path) -> list:
    d = roster_dir(cwd)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        o = _read_one(p)
        if o and o.get("id"):
            out.append(o)
    return out


def beat_age(entry: dict, now: float | None = None) -> float | None:
    """last_beat 이후 경과 초. 파싱 실패 시 None."""
    ts = entry.get("last_beat")
    if not ts:
        return None
    try:
        t = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (ValueError, OSError, OverflowError):
        return None
    return (time.time() if now is None else now) - t


def _coord_state(*, transcript_live: bool, field_live: bool,
                 observed: bool, declared: bool) -> str:
    """두-렌즈 조율 상태 — **확장은 여기 한 곳**(dogfood로 blocked·stuck·draining 등 추가 시 이 함수만).
    transcript-live(몸이 지금 움직이나)·field-live(15분 내 관측가능 조율)를 교차. 정직성(measured≠asserted):
    - **unattributed**: 활동 관측O·identity join X → role 미주장(R3 declared=null 규율).
    - **declared-unobserved**: 선언O·transcript 렌즈 없음 → heads-down/idle 판정 불가.
    - 둘 다 있을 때만 완전 2-렌즈(engaged/heads-down/idle)."""
    if observed and not declared:
        return "unattributed"
    if declared and not observed:
        return "engaged" if field_live else "declared-unobserved"
    if field_live:                       # 둘 다 관측 — field-live면 참여 중(transcript 순간 무관)
        return "engaged"
    return "heads-down" if transcript_live else "idle"


def merge(declared: list, derived: list | None = None, field_activity: dict | None = None,
          live_secs: float = 90.0, field_secs: float = 900.0, now: float | None = None) -> list:
    """선언 presence + 파생 관찰(transcript)을 **exact key**로 병합(declared id끼리) + **두-렌즈 조율 상태**.

    파생 항목 형태(주입): {id, brain, origin, last_ts, age, live}. field_activity(주입): {cell_key:
    field_age_secs} = 마지막 관측가능 조율(relay/agora 게시·session note/ship, beat 제외)의 경과 초.
    로스터 모듈은 transcript·field를 직접 읽지 않는다 — 둘 다 호출자(cli)가 만들어 넘긴다(디커플링).
    산출 필드: `live`(하위호환=임의 presence: transcript·beat) + **`transcript_live`·`field_live`·
    `field_age`·`coordination_state`**(두-렌즈). **주의**: 병합은 exact-key라 identity join(cell_key)
    없으면 결합 안 함 — 그건 `ORGANUM_CELL` 마커/observatory join의 몫(critic 재감사3 A-P2)."""
    now = time.time() if now is None else now
    field_activity = field_activity or {}
    by: dict[str, dict] = {}
    observed: set[str] = set()
    for e in derived or []:
        if e.get("id"):
            by[e["id"]] = dict(e)
            observed.add(e["id"])
    for e in declared:
        i = e.get("id")
        if not i:
            continue
        m = by.get(i, {"id": i})
        for k in _DECL_FIELDS:
            if e.get(k) is not None:
                m[k] = e[k]  # 선언(의도)이 파생을 덮는다 (name/focus/open_to는 transcript에 없음)
        m["declared"] = True
        if i not in observed:  # 파생 관찰 없음 → beat로 present/live 판정(하위호환)
            ba = beat_age(e, now)
            m["age"] = ba
            m["live"] = bool(ba is not None and ba <= live_secs)
        by[i] = m
    for i, m in by.items():  # 두-렌즈 신호 (live는 위에서 정해진 값 유지)
        t_live = bool(i in observed and m.get("live"))   # 관측 셀의 live=transcript-liveness
        fa = field_activity.get(i)
        f_live = bool(fa is not None and fa <= field_secs)
        m["transcript_live"] = t_live
        m["field_live"] = f_live
        m["field_age"] = fa
        m["coordination_state"] = _coord_state(
            transcript_live=t_live, field_live=f_live,
            observed=(i in observed), declared=bool(m.get("declared")))
        m["live"] = bool(m.get("live")) or f_live  # field 활동도 presence (하위호환 확장)
    return sorted(by.values(), key=lambda x: (not x.get("live"), (x.get("name") or x["id"])))


def field_activity(cwd: Path, state_dir: Path | None = None) -> dict:
    """셀별 마지막 관측가능 조율 age(초) — two-lens field-live 신호(merge에 주입·web도 소비).
    relay·agora `from_id` 게시 + 열린 세션 note/ship 최근성. **beat 제외**(하트비트≠소통, JJ 결정).
    데이터 소스를 읽으므로 merge와 분리(merge는 주입만 받음 — 디커플링). state_dir 없으면 best-effort 탐색."""
    import datetime as _dt
    from organum import relay as _relay
    from organum import agora as _agora
    from organum import session as _session

    now = time.time()
    acts: dict[str, float] = {}

    def _age(ts) -> float | None:
        try:
            t = _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
            return max(0.0, now - t)
        except (ValueError, OSError, OverflowError, AttributeError):
            return None

    for m in _relay.list_all(cwd) + _agora.list_all(cwd):  # 게시(from_id 있는 것만)
        fid = (m.get("from_id") or "").strip()
        a = _age(m.get("ts")) if fid else None
        if a is None:
            continue
        ck = st.cell_key(fid)
        acts[ck] = min(acts.get(ck, a), a)
    if state_dir is None:
        try:
            state_dir = st.require_state_dir(cwd)
        except SystemExit:
            state_dir = None
    if state_dir is not None and Path(state_dir).exists():  # 열린 세션 note/ship = 관측가능 조율
        for s in _session.open_sessions(state_dir):
            ck = st.cell_key(s.get("cell") or "")
            if not ck:
                continue
            a = float(s.get("idle_min", 0)) * 60.0
            acts[ck] = min(acts.get(ck, a), a)
    return acts


def _short_model(m: str | None) -> str:
    if not m:
        return "?"
    m = str(m).replace("claude-", "")
    m = re.sub(r"-\d{6,}$", "", m)  # 날짜 접미 제거
    return m[:18]


def _ago(secs: float | None) -> str:
    if secs is None:
        return "—"
    if secs < 90:
        return "live"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


# 두-렌즈 조율 상태 → 점 (확장 시 여기 + _coord_state 동시 갱신)
_STATE_DOT = {
    "engaged": "●",             # 참여 중 (건강)
    "heads-down": "◐",          # 일하는데 필드 조용 (넛지 후보)
    "idle": "○",                # 정지
    "declared-unobserved": "◌",  # 선언O·transcript X
    "unattributed": "◍",        # 활동O·join X (role 미주장)
}


def render(cells: list, site: str) -> str:
    live = sum(1 for c in cells if c.get("live"))
    heads = sum(1 for c in cells if c.get("coordination_state") == "heads-down")
    head = f"◉ organum roster · {site} · {len(cells)} cells ({live} live"
    head += f" · {heads} heads-down)" if heads else ")"
    lines = [head]
    if not cells:
        lines.append('  (아직 아무도 — `organum roster me --for <id> --focus "…"` 로 등장)')
        return "\n".join(lines)
    for c in cells:
        state = c.get("coordination_state")
        dot = _STATE_DOT.get(state, "●" if c.get("live") else "○")
        when = "live" if c.get("live") else _ago(c.get("age"))
        # engaged인데 transcript-idle이면 "paused" 부가(곧 idle로 갈 조기 힌트 — 별도 state 아님)
        st_lbl = state or ""
        if state == "engaged" and not c.get("transcript_live"):
            st_lbl = "engaged·paused"
        vend = (c.get("vendor") + "/") if c.get("vendor") else ""
        lines.append(f"─ {dot} {c['id']} · {vend}{c.get('origin', '?')} · {_short_model(c.get('brain'))}"
                     f"{(' · ' + st_lbl) if st_lbl else ''} · {when} ─")
        bits = []
        if c.get("name"):
            bits.append(c["name"])
        if c.get("focus"):
            bits.append(f"focus: {c['focus']}")
        if c.get("open_to"):
            bits.append("open: " + ",".join(c["open_to"]))
        if bits:
            lines.append("  " + "  ·  ".join(bits))
    return "\n".join(lines)
