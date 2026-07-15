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
import os
import sys
from pathlib import Path

# 사용자-대면 문자열은 전부 이 표에서 — 로케일 자동(ORGANUM_LANG 우선, LANG 폴백).
# organum 본체 CLI의 이중언어화도 같은 메커니즘으로 확장한다(observatory 제품화 때).
MSG = {
    "en": {
        "desc": "Post-hoc metering — retroactively aggregate duration, tokens, and tool "
                "use of AI agent sessions that ran in this folder (read-only, writes nothing)",
        "help.path": "project folder (default: current)",
        "help.window": "discovery window in days (default 45 — wider than vendor transcript retention)",
        "help.json": "machine-readable JSON (feeds your analysis pipeline)",
        "help.html": "save a self-contained HTML report (no server — open, share, archive)",
        "err.nodir": "organum-inspector: no such folder: {path}",
        "html.saved": "HTML report: {path} ({n} sessions)",
        "hdr": "━ organum inspector · {name} · window {days:g}d · {n} sessions",
        "empty": "  no sessions — no agent records found for this folder as cwd (widen with --window)",
        "col.start": "start", "col.dur": "duration",
        "sum": "  Σ {vendor} {n} sessions · duration {dur} · in {in_} · tools {tools} · files {files}",
        "legend": "\n  '—' = unmeasured (the vendor doesn't record it on disk) — never a silent zero."
                  " Token semantics differ per vendor; duration, tools and files are the safe axes.",
    },
    "ko": {
        "desc": "사후 계측 — 이 폴더에서 돌았던 AI 에이전트 세션들의 소요시간·토큰·툴 사용을 "
                "소급 집계 (read-only, 아무것도 쓰지 않음)",
        "help.path": "프로젝트 폴더 (기본: 현재 폴더)",
        "help.window": "발견 창(일, 기본 45 — 벤더 transcript 보존 기간보다 넓게)",
        "help.json": "기계용 JSON 출력 (AI 분석 파이프에 바로)",
        "help.html": "자립형 HTML 리포트 파일로 저장 (서버 불요 — 브라우저로 열고 공유·보관)",
        "err.nodir": "organum-inspector: 폴더가 없습니다: {path}",
        "html.saved": "HTML 리포트: {path} ({n} 세션)",
        "hdr": "━ organum inspector · {name} · 창 {days:g}일 · {n} 세션",
        "empty": "  세션 없음 — 이 폴더를 cwd로 돈 에이전트 기록을 못 찾았습니다 (창을 넓히려면 --window)",
        "col.start": "시작", "col.dur": "소요",
        "sum": "  Σ {vendor} {n}세션 · 소요 {dur} · in {in_} · tools {tools} · files {files}",
        "legend": "\n  '—' = 미측정(그 벤더가 디스크에 안 남김) — 0이 아닙니다."
                  " 토큰 계수 의미는 벤더별로 다릅니다(교차 비교는 시간·툴·파일이 안전).",
    },
}


def _lang() -> str:
    v = os.environ.get("ORGANUM_LANG") or os.environ.get("LANG") or ""
    return "ko" if v.lower().startswith("ko") else "en"


def _t(key: str, **kw) -> str:
    s = MSG[_lang()].get(key) or MSG["en"][key]
    return s.format(**kw) if kw else s


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
    lines = [_t("hdr", name=path.name, days=window_days, n=len(cells))]
    if not cells:
        lines.append(_t("empty"))
        return "\n".join(lines)
    hdr = (f"  {'vendor':<9} {'model':<24} {_t('col.start'):<12} {_t('col.dur'):>7}"
           f" {'in':>8} {'out':>7} {'cache':>7} {'tools':>5} {'files':>5}")
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
            ins_ = [c["in_tok"] for c in vs if c.get("in_tok") is not None]
            lines.append(_t("sum", vendor=f"{v:<7}", n=len(vs),
                            dur=_fmt_dur(sum(durs)) if durs else "—",
                            in_=_fmt_tok(sum(ins_)) if ins_ else "—",
                            tools=sum(c["tool_calls"] for c in vs),
                            files=sum(len(c.get("files") or []) for c in vs)))
    lines.append(_t("legend"))
    return "\n".join(lines)


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(prog="organum-inspector", description=_t("desc"))
    ap.add_argument("path", nargs="?", default=".", help=_t("help.path"))
    ap.add_argument("--window", type=float, default=45, help=_t("help.window"))
    ap.add_argument("--json", action="store_true", help=_t("help.json"))
    ap.add_argument("--html", metavar="FILE", help=_t("help.html"))
    args = ap.parse_args(argv)
    path = Path(args.path).expanduser().resolve()
    if not path.is_dir():
        print(_t("err.nodir", path=path), file=sys.stderr)
        return 1
    cells = collect(path, args.window)
    if args.html:
        from organum.htmlreport import inspector_page
        out = Path(args.html).expanduser()
        out.write_text(inspector_page(cells, path.name, args.window), encoding="utf-8")
        print(_t("html.saved", path=out, n=len(cells)))
        return 0
    if args.json:
        print(json.dumps(cells, ensure_ascii=False, indent=1))
    else:
        print(render(cells, path, args.window))
    return 0


if __name__ == "__main__":
    sys.exit(main())
