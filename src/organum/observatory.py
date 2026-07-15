"""organum observatory — 관측 영속화 (docs/observatory-design.md).

벤더 transcript는 ~30일 시한부(실측: 청소 주기 밖 증거 부재) — 정규화 Cell의
요약 레코드를 `.organum/observatory/<YYYY-MM>.jsonl` 월 샤드에 append하여 세션
소비 통계에 시간축을 준다. 데몬 없음: checkup·web·`observatory sync`가 같은
멱등 record()에 편승한다.

규율 상속: read-only 관측의 영속화(벤더 파일 불변) · None=미측정 그대로(C2) ·
불완전 관측(last_ts 없음)은 기록하지 않음(guard — 오염 방지) · raw transcript
비보관(집계 가능한 요약만).
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

DIR_NAME = "observatory"

# 프로세스-로컬 캐시: (state_dir, vendor, session_id) → 마지막 기록 last_ts.
# web 폴링이 후보 0일 때 디스크를 안 건드리게 하는 1차 게이트 — 진실은 샤드.
_recorded: dict[tuple, str] = {}


def _dir(state_dir: Path) -> Path:
    return state_dir / DIR_NAME


def _shard(state_dir: Path, last_ts: str) -> Path:
    return _dir(state_dir) / f"{last_ts[:7]}.jsonl"


def _shard_index(state_dir: Path) -> dict:
    """전체 샤드에서 (vendor, session_id) → 최신 last_ts. 멱등성의 진실 소스."""
    idx: dict = {}
    d = _dir(state_dir)
    if not d.is_dir():
        return idx
    for p in sorted(d.glob("*.jsonl")):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    k = (r.get("vendor"), r.get("session_id"))
                    ts = r.get("last_ts") or ""
                    if ts > (idx.get(k) or ""):
                        idx[k] = ts
        except OSError:
            continue
    return idx


def _declared_join(state_dir: Path, cells: list) -> dict:
    """관찰 셀 ↔ 선언 세션 조인 (web payload와 같은 결: 직접 id → ORGANUM_CELL 마커).
    반환: cell id → {declared, role, intent, sid_declared}. best-effort, 실패=빈 조인."""
    try:
        from organum import session as _sess
        from organum import web as _web
        sessions = _sess.open_sessions(state_dir)
    except Exception:
        return {}
    if not sessions:
        return {}
    by_cell = {s["cell"]: s for s in sessions if s.get("cell")}
    cids = list(by_cell)
    out = {}
    for c in cells:
        sess = by_cell.get(c["id"])
        declared = c["id"] if sess else None
        if sess is None and str(c.get("path") or "").endswith(".jsonl"):
            try:
                declared = _web._find_declared(c["path"], cids)
            except Exception:
                declared = None
            sess = by_cell.get(declared) if declared else None
        if sess:
            out[c["id"]] = {"declared": declared, "role": sess.get("role"),
                            "intent": sess.get("intent"), "sid_declared": sess.get("sid")}
    return out


def record(state_dir: Path, cells: list, reason: str,
           only_idle_sec: float | None = None, now: float | None = None) -> int:
    """정규화 Cell들을 월 샤드에 멱등 append. 기록한 수를 반환.

    only_idle_sec: 마지막 내용 ts가 그보다 최근인 셀은 건너뜀 — web 폴링이
    활동 중 셀을 매 폴마다 찍지 않고 settle된 상태만 남기게(최종본은 checkup/sync가 보증).
    """
    from organum.inspect import ts_age_seconds
    from organum.state import utc_now_iso

    if not state_dir.is_dir():
        return 0
    candidates = []
    for c in cells:
        if not c.get("last_ts"):  # 불완전 관측 미기록 (guard)
            continue
        if only_idle_sec is not None:
            age = ts_age_seconds(c["last_ts"], now)
            if age is None or age < only_idle_sec:
                continue
        key = (str(state_dir), c["vendor"], c.get("session_id"))
        if _recorded.get(key) == c["last_ts"]:  # 1차 게이트: 프로세스 캐시
            continue
        candidates.append((key, c))
    if not candidates:
        return 0
    idx = _shard_index(state_dir)  # 2차 게이트: 샤드 진실
    joins = _declared_join(state_dir, [c for _, c in candidates])
    n = 0
    for key, c in candidates:
        prev = idx.get((c["vendor"], c.get("session_id"))) or ""
        if c["last_ts"] <= prev:
            _recorded[key] = prev
            continue
        j = joins.get(c["id"]) or {}
        rec = {
            "v": 1, "vendor": c["vendor"], "session_id": c.get("session_id"),
            "id": c["id"], "model": c["model"], "origin": c["origin"],
            "parent": c.get("parent"),
            "in_tok": c["in_tok"], "out_tok": c["out_tok"], "cache": c["cache"],
            "tools": c["tools"] or {}, "skills": c["skills"] or {},
            "files_touched": len(c["files"] or []), "branch": c["branch"],
            "first_ts": c.get("first_ts"), "last_ts": c["last_ts"],
            "declared": j.get("declared"), "role": j.get("role"),
            "intent": j.get("intent"), "sid_declared": j.get("sid_declared"),
            "captured_at": utc_now_iso(), "capture_reason": reason,
        }
        shard = _shard(state_dir, c["last_ts"])
        shard.parent.mkdir(parents=True, exist_ok=True)
        with open(shard, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _recorded[key] = c["last_ts"]
        n += 1
    return n


def load(state_dir: Path, since_days: float | None = None) -> list:
    """샤드 전체 → (vendor, session_id)별 최신 last_ts 레코드만(라스트-라이트-윈),
    since_days 안의 것만. last_ts 오름차순."""
    latest: dict = {}
    d = _dir(state_dir)
    if not d.is_dir():
        return []
    for p in sorted(d.glob("*.jsonl")):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    k = (r.get("vendor"), r.get("session_id"))
                    prev = (latest.get(k) or {}).get("last_ts") or ""
                    if (r.get("last_ts") or "") >= prev:
                        latest[k] = r
        except OSError:
            continue
    recs = sorted(latest.values(), key=lambda r: r.get("last_ts") or "")
    if since_days is not None:
        cutoff = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(days=since_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        recs = [r for r in recs if (r.get("last_ts") or "") >= cutoff]
    return recs


def _sum_measured(recs: list, key: str) -> tuple[int | None, int]:
    """(측정된 것만의 합 또는 None, 미측정 수) — C2: None은 0으로 뭉개지 않는다."""
    vals = [r.get(key) for r in recs if r.get(key) is not None]
    return (sum(vals) if vals else None), len(recs) - len(vals)


def _cost(recs: list) -> float | None:
    """단가표(inspect.PRICES) 있는 모델만 합산. cache 쓰기 단가는 원천 미보관 → 제외(근사)."""
    from organum.inspect import PRICES
    total, any_priced = 0.0, False
    for r in recs:
        pr = PRICES.get(r.get("model") or "")
        if not pr:
            continue
        any_priced = True
        total += ((r.get("in_tok") or 0) * pr["in"] + (r.get("out_tok") or 0) * pr["out"]
                  + (r.get("cache") or 0) * pr["cache_read"]) / 1_000_000
    return total if any_priced else None


def stats(recs: list, by: str | None = None) -> dict:
    """집계 — 세션 수·origin 분해·토큰(측정분만)·비용 근사·그룹별(by=model|role|origin|vendor)."""
    out: dict = {"sessions": len(recs),
                 "terminal": sum(1 for r in recs if r.get("origin") == "terminal"),
                 "subagent": sum(1 for r in recs if r.get("origin") == "subagent")}
    for k in ("in_tok", "out_tok", "cache"):
        out[k], out[f"{k}_unmeasured"] = _sum_measured(recs, k)
    out["cost_usd"] = _cost(recs)
    if by:
        groups: dict = {}
        for r in recs:
            g = str(r.get(by) or "—")
            groups.setdefault(g, []).append(r)
        out["by"] = {g: {"sessions": len(rs),
                         "in_tok": _sum_measured(rs, "in_tok")[0],
                         "out_tok": _sum_measured(rs, "out_tok")[0],
                         "cache": _sum_measured(rs, "cache")[0],
                         "cost_usd": _cost(rs)}
                     for g, rs in sorted(groups.items(), key=lambda kv: -len(kv[1]))}
    return out


def _fmt_tok(v) -> str:
    if v is None:
        return "—"
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v / 1_000:.1f}K"
    return str(v)


def _local_date(ts: str | None) -> str | None:
    """ISO UTC → 사람의 하루(로컬 날짜). 리포트의 '오늘'·일별 추이는 로컬 기준이 정직하다."""
    if not ts:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d")
    except (ValueError, OSError):
        return None


SPARK = "▁▂▃▄▅▆▇█"


def _spark(vals: list) -> str:
    top = max((v for v in vals if v), default=0)
    if not top:
        return "▁" * len(vals)
    return "".join(SPARK[min(int((v or 0) / top * (len(SPARK) - 1) + 0.5), 7)] for v in vals)


def report(state_dir: Path, cwd: Path, days: float = 30) -> str:
    """작업 모니터 리포트 — '지금'(transcript 직독)과 '역사'(observatory 축적)를 분리된
    밴드로. 밴드끼리 합산하지 않는다(현재 세션은 settle 후 역사에도 편입 — 이중계산 방지).
    read-only: 리포트 생성이 기록을 남기지 않는다."""
    from organum import adapters as _ad
    from organum.inspect import ts_age_seconds

    lines = [f"━ organum report · {cwd.name} · 창 {days:g}일"]

    # ── 지금: 관측 창(30분) 안의 세션, 각 세션 생애 누적 (관제탑과 같은 눈)
    live = []
    for c in _ad.snapshot(cwd, window_min=30.0):
        age = ts_age_seconds(c.get("last_ts"))
        if age is not None and age <= 1800:
            live.append((c, age))
    lines.append(f"\n■ 지금 — 살아있는 세션 {len(live)} (관측 창 30분, 세션 생애 누적)")
    for c, age in sorted(live, key=lambda x: x[1]):
        state = "live" if age < 90 else f"idle {int(age // 60)}m"
        lines.append(f"  {c['id']}  {c['model'] or '—'} · {c['origin']}"
                     f" · out {_fmt_tok(c['out_tok'])} · cache {_fmt_tok(c['cache'])}"
                     f" · tools {sum((c['tools'] or {}).values())} · {state}")

    # ── 역사: observatory 축적 (transcript 청소를 넘어 살아남는 쪽)
    recs = load(state_dir, since_days=days)
    if not recs:
        lines.append("\n■ 역사 — 스냅샷 없음 (organum observatory sync 로 시작)")
        return "\n".join(lines)
    today = datetime.datetime.now().astimezone().strftime("%Y-%m-%d")
    today_recs = [r for r in recs if _local_date(r.get("last_ts")) == today]
    ts_ = stats(today_recs)
    lines.append(f"\n■ 오늘 — {ts_['sessions']} 세션 스냅샷"
                 f" · out {_fmt_tok(ts_['out_tok'])} · cache {_fmt_tok(ts_['cache'])}"
                 f" (서브에이전트 {ts_['subagent']})")

    s = stats(recs, by="model")
    lines.append(f"\n■ 역사 — {s['sessions']} 세션 · out {_fmt_tok(s['out_tok'])}"
                 f" · cache {_fmt_tok(s['cache'])}"
                 f" (터미널 {s['terminal']} · 서브에이전트 {s['subagent']})")
    if s.get("cost_usd") is not None:
        lines.append(f"  비용 근사: ${s['cost_usd']:.2f} (단가표 등재 모델만, 캐시 쓰기 제외)")
    # 일별 추이 (out 기준) — 대형 세션 지배 구조가 한눈에
    by_day: dict = {}
    for r in recs:
        d = _local_date(r.get("last_ts"))
        if d:
            by_day[d] = by_day.get(d, 0) + (r.get("out_tok") or 0)
    days_sorted = sorted(by_day)
    lines.append(f"  일별 out: {_spark([by_day[d] for d in days_sorted])}"
                 f"  ({days_sorted[0][5:]}~{days_sorted[-1][5:]}, 峰 {_fmt_tok(max(by_day.values()))})")
    # 모델 믹스 + 대형 세션 top3
    mix = " · ".join(f"{m} {g['sessions']}" for m, g in list(s["by"].items())[:5])
    lines.append(f"  모델 믹스: {mix}")
    top = sorted(recs, key=lambda r: r.get("out_tok") or 0, reverse=True)[:3]
    lines.append("  대형 세션:")
    for r in top:
        who = f" ({r['declared']} · {r['role']})" if r.get("declared") else ""
        lines.append(f"    {r['id']}{who}  {_local_date(r.get('last_ts'))}"
                     f" · out {_fmt_tok(r.get('out_tok'))} · cache {_fmt_tok(r.get('cache'))}"
                     f" · {r.get('model') or '—'}")
    return "\n".join(lines)


def render_stats(s: dict, days: float, by: str | None = None) -> str:
    lines = [f"observatory — 최근 {days:g}일 · {s['sessions']} 세션"
             f" (터미널 {s['terminal']} · 서브에이전트 {s['subagent']})"]
    unm = [f"{k.replace('_tok', '')} {n}셀" for k, n in
           ((x, s[f"{x}_unmeasured"]) for x in ("in_tok", "out_tok", "cache")) if n]
    lines.append(f"  토큰: in {_fmt_tok(s['in_tok'])} · out {_fmt_tok(s['out_tok'])}"
                 f" · cache {_fmt_tok(s['cache'])}"
                 + (f"  (미측정 제외: {', '.join(unm)})" if unm else ""))
    if s.get("cost_usd") is not None:
        lines.append(f"  비용 근사: ${s['cost_usd']:.2f} (단가표 등재 모델만, 캐시 쓰기 제외)")
    if by and s.get("by"):
        lines.append(f"  --by {by}:")
        w = max(len(g) for g in s["by"])
        for g, gs in s["by"].items():
            cost = f" · ${gs['cost_usd']:.2f}" if gs.get("cost_usd") is not None else ""
            lines.append(f"    {g:<{w}}  {gs['sessions']:>4}세션 · in {_fmt_tok(gs['in_tok'])}"
                         f" · out {_fmt_tok(gs['out_tok'])} · cache {_fmt_tok(gs['cache'])}{cost}")
    return "\n".join(lines)
