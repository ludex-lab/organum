"""organum roster — 현장(work-site)의 세포 presence 로스터.

누가 서식지에 있고 · 무엇을 하며 · 말을 걸 수 있는지. 모든 회람(relay·agora·council)이 이걸 참조한다.

**경계: 서술적 로스터지 배정이 아니다** (shared-cognition §8) — 누가 있나를 *보여줄* 뿐, 태스크를
*지정*하지 않는다. 이게 관제탑↔관제사 선.

두 겹:
- **선언(declared) presence** — 세포가 스스로 쓰는 의도: `.organum/roster/<id8>.json`
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

_ID_RE = re.compile(r"[^0-9A-Za-z_-]+")
_DECL_FIELDS = ("name", "focus", "open_to", "joined_at", "last_beat", "brain")


def roster_dir(cwd: Path) -> Path:
    return cwd / ".organum" / "roster"


def _id8(s: str) -> str:
    return (_ID_RE.sub("", (s or "").strip()) or "x")[:8]


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
    i8 = _id8(for_id)
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


def merge(declared: list, derived: list | None = None, live_secs: float = 90.0,
          now: float | None = None) -> list:
    """선언 presence + 파생 관찰(transcript)을 id8로 병합.

    파생 항목 형태(주입): {id, brain, origin, last_ts, age, live}. 로스터 모듈은 transcript를 직접
    읽지 않는다 — 파생은 호출자(cli)가 inspect로 만들어 넘긴다(디커플링). 선언만 있는 세포는 last_beat로
    live 판정. live → 이름순 정렬."""
    now = time.time() if now is None else now
    by: dict[str, dict] = {}
    for e in derived or []:
        if e.get("id"):
            by[e["id"]] = dict(e)
    for e in declared:
        i = e.get("id")
        if not i:
            continue
        m = by.get(i, {"id": i})
        for k in _DECL_FIELDS:
            if e.get(k) is not None:
                m[k] = e[k]  # 선언(의도)이 파생을 덮는다 (name/focus/open_to는 transcript에 없음)
        m["declared"] = True
        if "live" not in m:  # 파생 관찰 없음 → beat로 present/live 판정
            ba = beat_age(e, now)
            m["age"] = ba
            m["live"] = bool(ba is not None and ba <= live_secs)
        by[i] = m
    return sorted(by.values(), key=lambda x: (not x.get("live"), (x.get("name") or x["id"])))


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


def render(cells: list, site: str) -> str:
    live = sum(1 for c in cells if c.get("live"))
    lines = [f"◉ organum roster · {site} · {len(cells)} cells ({live} live)"]
    if not cells:
        lines.append('  (아직 아무도 — `organum roster me --for <id> --focus "…"` 로 등장)')
        return "\n".join(lines)
    for c in cells:
        dot = "●" if c.get("live") else "○"
        when = "live" if c.get("live") else _ago(c.get("age"))
        vend = (c.get("vendor") + "/") if c.get("vendor") else ""
        lines.append(f"─ {dot} {c['id']} · {vend}{c.get('origin', '?')} · {_short_model(c.get('brain'))} · {when} ─")
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
