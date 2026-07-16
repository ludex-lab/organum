"""기억 기관 read/write 층 — remember(§3.4, guard 경유) / recall --when(§3.3)."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from organum import guard
from organum import state as st

_WINDOW_RE = re.compile(r"^(\d+)([mhdw])$")
_UNIT = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}

VALID_TYPES = ("episodic", "semantic", "procedural")
VALID_CONFIDENCE = ("tentative", "confirmed", "well-supported")


def parse_window(spec: str) -> timedelta:
    m = _WINDOW_RE.match(spec.strip())
    if not m:
        raise SystemExit(f"organum: 시간창 형식이 아닙니다: {spec!r} (예: 30m, 24h, 7d, 2w)")
    return timedelta(**{_UNIT[m.group(2)]: int(m.group(1))})


def remember(
    state_dir: Path,
    content: str,
    *,
    mem_type: str = "episodic",
    tags: list[str] | None = None,
    confidence: str = "tentative",
    supersedes: str | None = None,
) -> tuple[guard.Verdict, str | None]:
    """guard 통과 시 memories.jsonl append. 반환: (판정, 저장된 id|None)."""
    verdict = guard.evaluate(content)
    if not verdict.ok:
        guard.record(state_dir, verdict, "memories", content)
        guard.mark_streak_if_reached(state_dir)
        return verdict, None

    record = {
        "id": uuid.uuid4().hex[:12],
        "ts": st.utc_now_iso(),
        "content": content,
        "type": mem_type,
        "tags": tags or [],
        "confidence": confidence,
        "supersedes": supersedes,
    }
    if verdict.decision == "flagged":
        record["x_guard_flagged"] = True  # §0: 스펙 외 필드는 x_ 접두
        guard.record(state_dir, verdict, "memories", content)
    with (state_dir / "memory" / "memories.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    st.append_event(  # 이 remember 이벤트가 streak 리셋 경계다(events 삽입 순서, §7.2)
        state_dir, "remember", content if len(content) <= 80 else content[:79] + "…", tags=tags
    )
    return verdict, record["id"]


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def recall_window(state_dir: Path, window: timedelta) -> list[dict]:
    """시간창 내 events + memories를 ts 순으로. superseded 기억은 제외 (§3.4)."""
    cutoff = (datetime.now(timezone.utc) - window).strftime("%Y-%m-%dT%H:%M:%SZ")
    events = [
        {**e, "_src": "event"} for e in _read_jsonl(state_dir / "memory" / "events.jsonl")
    ]
    memories = _read_jsonl(state_dir / "memory" / "memories.jsonl")
    superseded = {m["supersedes"] for m in memories if m.get("supersedes")}
    mems = [
        {**m, "_src": "memory"} for m in memories if m.get("id") not in superseded
    ]
    merged = [r for r in events + mems if r.get("ts", "") >= cutoff]
    return sorted(merged, key=lambda r: r.get("ts", ""))


def render_recall(records: list[dict], window_spec: str) -> str:
    n_ev = sum(1 for r in records if r["_src"] == "event")
    n_mem = len(records) - n_ev
    lines = [f"[Recall] last {window_spec} · events {n_ev} · memories {n_mem}"]
    for r in records:
        ts = r.get("ts", "?")[:16].replace("T", " ")
        if r["_src"] == "event":
            label = r.get("kind", "?")
        else:
            label = f"mem:{r.get('type', '?')}" + ("⚑" if r.get("x_guard_flagged") else "")
        content = str(r.get("content", "")).replace("\n", " ")
        if len(content) > 160:
            content = content[:159] + "…"
        tag_str = " [" + ",".join(r["tags"]) + "]" if r.get("tags") else ""
        lines.append(f"{ts}  {label:<14} {content}{tag_str}")
    return "\n".join(lines)
