"""organum-inspector — 사후 계측 CLI (organum 제품군의 read-only 슬라이스).

이미 끝난 작업도 소급해서 잰다: 임의 프로젝트 폴더를 가리키면 6벤더(Claude Code·
Codex·agy/Gemini·Grok·OpenCode·Cursor) 세션 기록을 발견·전량 파싱해 소요시간·토큰·툴·
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
                  " Token semantics differ per vendor; duration, tools and files are the safe axes."
                  " c% = cache/(in+cache), a cost lever — measured inputs only.",
        "legend.blind": "  Supervised (ACP) harness runs are outside passive metering — records"
                        " accepted via 'organum observatory ingest' appear in the reported band.",
        "cost.legend": "  cost ≈ user-supplied prices (--prices · ORGANUM_PRICES · ~/.organum/prices.json),"
                       " cache-write excluded.",
        "help.prices": "user price table JSON path (default: ORGANUM_PRICES env, then ~/.organum/prices.json)",
        "col.role": "role",
        "rep.hdr": "\n  ─ reported — supervisor-harness runs (separate evidence, never merged) ─",
        "rep.legend": "  reported c% = cached/input (broker-metered). These runs are invisible to"
                      " passive metering above — two evidence kinds, side by side.",
        "health.line": "  ⚠ substrate-health: {n} finding(s) on this machine's agent stores —"
                       " see 'organum observatory health'",
        "integ.hdr": "\n  ─ core-integrity audit (bless = git commit · reconstructive) ─",
        "integ.incomplete": "  ⚠ scan incomplete — core-manifest is corrupt; declared core dropped. "
                            "Partial result; not 'all blessed'.",
        "integ.legend": "  detection, not verdict — attribution is reconstructive (git + transcripts), "
                        "not organum provenance. Sessions shown are active around the last bless.",
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
                  " 토큰 계수 의미는 벤더별로 다릅니다(교차 비교는 시간·툴·파일이 안전)."
                  " c% = cache/(in+cache), 비용 레버 — 측정분만.",
        "legend.blind": "  supervised(ACP) 하네스 run은 passive 계측 밖입니다 — 'organum observatory"
                        " ingest'로 수용된 기록이 reported 밴드로 나타납니다.",
        "cost.legend": "  비용 ≈ 사용자 단가표(--prices · ORGANUM_PRICES · ~/.organum/prices.json),"
                       " 캐시 쓰기 제외.",
        "help.prices": "사용자 단가표 JSON 경로 (기본: ORGANUM_PRICES env → ~/.organum/prices.json)",
        "col.role": "역할",
        "rep.hdr": "\n  ─ reported — supervisor 하네스 run (별도 증거, 합산 안 함) ─",
        "rep.legend": "  reported c% = cached/input (broker 계측). 이 run들은 위 passive 계측에"
                      " 보이지 않습니다 — 두 증거를 나란히 둘 뿐 섞지 않습니다.",
        "health.line": "  ⚠ substrate-health: 이 머신의 에이전트 store에서 {n}건 발견 —"
                       " 'organum observatory health'로 확인",
        "integ.hdr": "\n  ─ core-integrity 감사 (bless = git commit · 재구성) ─",
        "integ.incomplete": "  ⚠ scan 불완전 — core-manifest 손상으로 선언 core 탈락. "
                            "부분 결과이며 'all blessed' 아님.",
        "integ.legend": "  탐지지 판결 아님 — 귀속은 재구성적(git + transcript)이지 organum provenance "
                        "아님. 표시된 세션은 마지막 bless 무렵 활성이던 것.",
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


def _cache_pct(in_tok, cache) -> float | None:
    """cache/(in+cache) — 캐시 회수 비율(비용 레버). 어느 쪽이든 미측정이면 None(C2)."""
    if in_tok is None or cache is None or (in_tok + cache) <= 0:
        return None
    return cache / (in_tok + cache) * 100.0


def _fmt_pct(v) -> str:
    return "—" if v is None else f"{v:.0f}%"


def collect(path: Path, window_days: float) -> list:
    """cwd=path의 전 벤더 세션을 deep 파싱으로 수집, duration_s·cache_pct 부착. read-only."""
    from organum import adapters
    cells = adapters.snapshot(str(path), window_min=window_days * 24 * 60, deep=True)
    for c in cells:
        c["duration_s"] = _dur_s(c.get("first_ts"), c.get("last_ts"))
        c["tool_calls"] = sum((c.get("tools") or {}).values())
        c["cache_pct"] = _cache_pct(c.get("in_tok"), c.get("cache"))
    return sorted(cells, key=lambda c: c.get("first_ts") or c.get("last_ts") or "")


def attribute(path: Path, cells: list) -> None:
    """선언 귀속(additive) — 대상 프로젝트에 .organum이 있으면 declared cell·role·loadout을
    세션에 주석. 없으면 no-op(no-setup 명제 불변). 실패는 조용히 미귀속(관측 정직성)."""
    sd = path / ".organum"
    if not sd.is_dir():
        return
    try:
        from organum.observatory import _declared_join
        joins = _declared_join(sd, cells)
    except Exception:
        return
    for c in cells:
        j = joins.get(c["id"]) or {}
        c["declared"], c["role"], c["loadout"] = j.get("declared"), j.get("role"), j.get("loadout")


def reported_runs(path: Path) -> list:
    """reported 밴드(별도 증거) — 프로젝트가 supervisor 하네스 관측을 ingest했으면 낸다.
    passive와 절대 합산하지 않는다(이중계산 방지 — observatory --source 분리와 동일 규율)."""
    sd = path / ".organum"
    if not (sd / "observatory").is_dir():
        return []
    try:
        from organum.observatory import load_reported
        return load_reported(sd)
    except Exception:
        return []


def _sessions_at(cells: list, iso: str | None) -> list:
    """iso 시각에 활성이던 재구성 세션(first_ts ≤ iso ≤ last_ts). reconstructive two-lens 귀속 evidence."""
    if not iso:
        return []
    try:
        t = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return []
    out = []
    for c in cells:
        try:
            a = datetime.datetime.fromisoformat(str(c.get("first_ts")).replace("Z", "+00:00")) \
                if c.get("first_ts") else None
            b = datetime.datetime.fromisoformat(str(c.get("last_ts")).replace("Z", "+00:00")) \
                if c.get("last_ts") else None
        except (ValueError, TypeError):
            continue
        if a and b and a <= t <= b:
            out.append({"vendor": c["vendor"], "model": c.get("model"), "id": c.get("id")})
    return out


def core_integrity(path: Path, cells: list) -> list:
    """core-integrity 감사 — integrity.report(git-기반, state 불요·아무 폴더나) + 마지막 bless를 재구성
    세션과 교차(reconstructive two-lens). inspector 고유: organum provenance 없이 transcript로 귀속.
    반환 [{path, status, last_commit, active_sessions}]. git 저장소 아니면 []."""
    from organum import integrity as _integ
    if not _integ.is_git_repo(path):
        return []
    rep = _integ.report(path / ".organum")   # .organum 있으면 manifest, 없으면 AUTO_CORE만
    for r in rep:
        lc = r.get("last_commit")
        r["active_sessions"] = _sessions_at(cells, lc.get("date") if lc else None)
    return rep


def render(cells: list, path: Path, window_days: float, integ: list | None = None,
           integ_incomplete: bool = False, reported: list | None = None,
           prices: dict | None = None) -> str:
    prices = prices or {}

    def _sess_cost(c) -> float | None:
        pr = prices.get(c.get("model") or "")
        if not pr:
            return None
        return ((c.get("in_tok") or 0) * pr["in"] + (c.get("out_tok") or 0) * pr["out"]
                + (c.get("cache") or 0) * pr["cache_read"]) / 1_000_000

    lines = [_t("hdr", name=path.name, days=window_days, n=len(cells))]
    any_role = any(c.get("role") for c in cells)
    any_cost = any(_sess_cost(c) is not None for c in cells)
    if not cells:
        lines.append(_t("empty"))
    else:
        role_h = f" {_t('col.role'):<8}" if any_role else ""
        hdr = (f"  {'vendor':<9} {'model':<24}{role_h} {_t('col.start'):<12} {_t('col.dur'):>7}"
               f" {'in':>8} {'out':>7} {'cache':>7} {'c%':>4} {'tools':>5} {'files':>5}")
        lines += [hdr, "  " + "─" * (len(hdr) - 2)]
        for c in cells:
            start = (c.get("first_ts") or "")[5:16].replace("T", " ") or "—"
            model = (c.get("model") or "—")[:24]
            role_c = f" {(c.get('role') or '—')[:8]:<8}" if any_role else ""
            lines.append(f"  {c['vendor']:<9} {model:<24}{role_c} {start:<12} {_fmt_dur(c['duration_s']):>7}"
                         f" {_fmt_tok(c.get('in_tok')):>8} {_fmt_tok(c.get('out_tok')):>7}"
                         f" {_fmt_tok(c.get('cache')):>7} {_fmt_pct(c.get('cache_pct')):>4}"
                         f" {c['tool_calls']:>5} {len(c.get('files') or []):>5}")
        vendors = sorted({c["vendor"] for c in cells})  # 벤더 합계 (2벤더 이상 — 비교가 존재 이유)
        if len(vendors) > 1:
            lines.append("")
            for v in vendors:
                vs = [c for c in cells if c["vendor"] == v]
                durs = [c["duration_s"] for c in vs if c["duration_s"] is not None]
                ins_ = [c["in_tok"] for c in vs if c.get("in_tok") is not None]
                caches = [(c["in_tok"], c["cache"]) for c in vs
                          if c.get("in_tok") is not None and c.get("cache") is not None]
                pct = _cache_pct(sum(a for a, _ in caches), sum(b for _, b in caches))                     if caches else None
                costs = [x for x in (_sess_cost(c) for c in vs) if x is not None]
                extra = (f" · c% {_fmt_pct(pct)}" if pct is not None else "") +                         (f" · ${sum(costs):.2f}" if costs else "")
                lines.append(_t("sum", vendor=f"{v:<7}", n=len(vs),
                                dur=_fmt_dur(sum(durs)) if durs else "—",
                                in_=_fmt_tok(sum(ins_)) if ins_ else "—",
                                tools=sum(c["tool_calls"] for c in vs),
                                files=sum(len(c.get("files") or []) for c in vs)) + extra)
        lines.append(_t("legend"))
        if any_cost:
            lines.append(_t("cost.legend"))
        if not reported:
            lines.append(_t("legend.blind"))
    if reported:  # 별도 증거 밴드 — passive와 절대 합산하지 않는다
        lines.append(_t("rep.hdr"))
        for r in reported:
            rin, rc = r.get("in_tok"), r.get("cache")
            rpct = (rc / rin * 100.0) if (rin and rc is not None and rin > 0) else None
            lines.append(f"  {(r.get('backend') or '—'):<12} {(r.get('model') or '—')[:20]:<20}"
                         f" {(r.get('run_status') or '—'):<9}"
                         f" in {_fmt_tok(rin):>8} · out {_fmt_tok(r.get('out_tok')):>7}"
                         f" · cache {_fmt_tok(rc):>7} · c% {_fmt_pct(rpct):>4}"
                         f" · gate {(r.get('gate') or '—')}")
        lines.append(_t("rep.legend"))
    if integ or integ_incomplete:  # core-integrity 감사 섹션 (state 불요 — 아무 폴더나)
        lines.append(_t("integ.hdr"))
        if integ_incomplete:  # 손상 manifest → 선언 core 탈락, 부분 결과임을 명시(critic 재감사3 B5-c)
            lines.append(_t("integ.incomplete"))
        for r in integ:
            mark = {"blessed": "●", "unblessed": "◐", "unprotected": "◌"}.get(r["status"], "○")
            lc = r.get("last_commit")
            bless = f"{lc['author']}·{lc['date'][:10]}" if lc else "—"
            act = r.get("active_sessions") or []
            who = ("  · bless 무렵 세션: " + ", ".join(s["vendor"] for s in act)) if act else ""
            lines.append(f"  {mark} {r['path']} · {r['status']} · bless {bless}{who}")
        lines.append(_t("integ.legend"))
    return "\n".join(lines)


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(prog="organum-inspector", description=_t("desc"))
    ap.add_argument("path", nargs="?", default=".", help=_t("help.path"))
    ap.add_argument("--window", type=float, default=45, help=_t("help.window"))
    ap.add_argument("--json", action="store_true", help=_t("help.json"))
    ap.add_argument("--html", metavar="FILE", help=_t("help.html"))
    ap.add_argument("--prices", metavar="FILE", default=None, help=_t("help.prices"))
    args = ap.parse_args(argv)
    path = Path(args.path).expanduser().resolve()
    if not path.is_dir():
        print(_t("err.nodir", path=path), file=sys.stderr)
        return 1
    cells = collect(path, args.window)
    attribute(path, cells)              # .organum 있으면 declared cell·role 주석 (additive)
    reported = reported_runs(path)      # ingest된 supervisor 하네스 관측 (별도 증거)
    from organum.inspect import effective_prices
    prices = effective_prices(args.prices)
    integ = core_integrity(path, cells)   # core-integrity 감사 (git-기반, reconstructive 세션 교차)
    from organum import integrity as _integ
    integ_incomplete = _integ.is_git_repo(path) and not _integ.manifest_ok(path / ".organum")
    if args.html:
        from organum.htmlreport import inspector_page
        out = Path(args.html).expanduser()
        out.write_text(inspector_page(cells, path.name, args.window, reported=reported),
                       encoding="utf-8")
        print(_t("html.saved", path=out, n=len(cells)))
        return 0
    if args.json:  # shipped 계약 유지 — sessions 리스트 그대로(추가 필드는 additive).
        print(json.dumps(cells, ensure_ascii=False, indent=1))  # reported는 observatory stats --source reported --json
    else:
        print(render(cells, path, args.window, integ, integ_incomplete,
                     reported=reported, prices=prices))
        try:  # substrate-health 한 줄 (text 뷰만 · 실패는 침묵 — 계측을 막지 않는다)
            from organum import health as _health
            hrep = _health.measure()
            if hrep["findings"]:
                print(_t("health.line", n=len(hrep["findings"])))
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
