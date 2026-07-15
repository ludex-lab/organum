"""organum-inspector — 사후 계측 CLI (organum 제품군의 read-only 슬라이스).

이미 끝난 작업도 소급해서 잰다: 임의 프로젝트 폴더를 가리키면 5벤더(Claude Code·
Codex·agy/Gemini·Grok·OpenCode) 세션 기록을 발견·전량 파싱해 소요시간·토큰·툴·
파일을 표로 낸다. "같은 과제를 두 에이전트에게 시켰는데 누가 얼마나 쓰고 얼마나
걸렸나"가 대표 질문. `organum init` 불요 — 대상 폴더에 아무것도 쓰지 않는다.

경계(organum 헌법 그대로): 관측만. 세션을 시작/지휘하지 않는다. 미측정은 '—'로
정직하게(0이 아니다). 벤더마다 토큰 계수 의미가 다르므로(누적 총량 vs 컨텍스트
지표) 교차 비교의 안전축은 시간·툴·파일이다.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path


def _dur_s(first_ts, last_ts) -> float | None:
    if not first_ts or not last_ts:
        return None
    try:
        a = datetime.datetime.fromisoformat(str(first_ts).replace("Z", "+00:00"))
        b = datetime.datetime.fromisoformat(str(last_ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (b - a).total_seconds())


def _fmt_dur(s) -> str:
    if s is None:
        return "—"
    if s >= 3600:
        return f"{s / 3600:.1f}h"
    if s >= 60:
        return f"{s / 60:.1f}m"
    return f"{s:.0f}s"


def _fmt_tok(v) -> str:
    if v is None:
        return "—"
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v / 1_000:.1f}K"
    return str(v)


def collect(path: Path, window_days: float) -> list:
    """cwd=path의 전 벤더 세션을 deep 파싱으로 수집, duration_s 부착. read-only."""
    from organum import adapters
    cells = adapters.snapshot(str(path), window_min=window_days * 24 * 60, deep=True)
    for c in cells:
        c["duration_s"] = _dur_s(c.get("first_ts"), c.get("last_ts"))
        c["tool_calls"] = sum((c.get("tools") or {}).values())
    return sorted(cells, key=lambda c: c.get("first_ts") or c.get("last_ts") or "")


def render(cells: list, path: Path, window_days: float) -> str:
    lines = [f"━ organum inspector · {path.name} · 창 {window_days:g}일 · {len(cells)} 세션"]
    if not cells:
        lines.append("  세션 없음 — 이 폴더를 cwd로 돈 에이전트 기록을 못 찾았습니다"
                     " (창을 넓히려면 --window)")
        return "\n".join(lines)
    hdr = f"  {'vendor':<9} {'model':<24} {'시작':<12} {'소요':>7} {'in':>8} {'out':>7} {'cache':>7} {'tools':>5} {'files':>5}"
    lines += [hdr, "  " + "─" * (len(hdr) - 2)]
    for c in cells:
        start = (c.get("first_ts") or "")[5:16].replace("T", " ") or "—"
        model = (c.get("model") or "—")[:24]
        lines.append(f"  {c['vendor']:<9} {model:<24} {start:<12} {_fmt_dur(c['duration_s']):>7}"
                     f" {_fmt_tok(c.get('in_tok')):>8} {_fmt_tok(c.get('out_tok')):>7}"
                     f" {_fmt_tok(c.get('cache')):>7} {c['tool_calls']:>5}"
                     f" {len(c.get('files') or []):>5}")
    # 벤더 합계 (2벤더 이상일 때만 — 비교가 이 도구의 존재 이유)
    vendors = sorted({c["vendor"] for c in cells})
    if len(vendors) > 1:
        lines.append("")
        for v in vendors:
            vs = [c for c in cells if c["vendor"] == v]
            durs = [c["duration_s"] for c in vs if c["duration_s"] is not None]
            tot_d = _fmt_dur(sum(durs)) if durs else "—"
            ins_ = [c["in_tok"] for c in vs if c.get("in_tok") is not None]
            lines.append(f"  Σ {v:<7} {len(vs)}세션 · 소요 {tot_d}"
                         f" · in {_fmt_tok(sum(ins_)) if ins_ else '—'}"
                         f" · tools {sum(c['tool_calls'] for c in vs)}"
                         f" · files {sum(len(c.get('files') or []) for c in vs)}")
    lines.append("\n  '—' = 미측정(그 벤더가 디스크에 안 남김) — 0이 아닙니다."
                 " 토큰 계수 의미는 벤더별로 다릅니다(교차 비교는 시간·툴·파일이 안전).")
    return "\n".join(lines)


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="organum-inspector",
        description="사후 계측 — 이 폴더에서 돌았던 AI 에이전트 세션들의 소요시간·토큰·툴 사용을 "
                    "소급 집계 (read-only, 설치 외 아무것도 쓰지 않음)")
    ap.add_argument("path", nargs="?", default=".", help="프로젝트 폴더 (기본: 현재 폴더)")
    ap.add_argument("--window", type=float, default=45,
                    help="발견 창(일, 기본 45 — 벤더 transcript 보존 기간보다 넓게)")
    ap.add_argument("--json", action="store_true", help="기계용 JSON 출력 (AI 분석 파이프에 바로)")
    args = ap.parse_args(argv)
    path = Path(args.path).expanduser().resolve()
    if not path.is_dir():
        print(f"organum-inspector: 폴더가 없습니다: {path}", file=sys.stderr)
        return 1
    cells = collect(path, args.window)
    if args.json:
        print(json.dumps(cells, ensure_ascii=False, indent=1))
    else:
        print(render(cells, path, args.window))
    return 0


if __name__ == "__main__":
    sys.exit(main())
