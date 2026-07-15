"""자립형 HTML 리포트 — inspector·observatory 공용 렌더러.

파일 = 산출물. 서버·데몬·JS 없이 순수 HTML+CSS 한 파일로 떨어져서, 브라우저로
열고, 팀에 공유하고, 케이스 스터디처럼 보관한다 — 사후 계측의 정체성(끝난 일의
기록)에 맞는 형태. stdlib only. 표기 규율은 CLI와 동일: 미측정 None='—'(0 아님).
"""

from __future__ import annotations

import datetime
import html as _html
import time

from organum.inspector import _dur_s, _fmt_dur, _fmt_tok

VENDOR_HUES = {"claude": "#B4543B", "codex": "#2B6E6A", "grok": "#3A3A3A",
               "agy": "#3B5BA5", "opencode": "#557A3F"}

_CSS = """
:root{--paper:#ECEFEA;--panel:#F4F6F1;--ink:#161A17;--ink2:#3C433B;--slate:#6B726A;
--rule:#CBD1C7;--mono:ui-monospace,'SF Mono',Menlo,Consolas,monospace}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);
font:15px/1.55 -apple-system,'Pretendard',system-ui,sans-serif;padding:2rem 1rem}
.wrap{max-width:60rem;margin:0 auto}h1{font-size:1.25rem;margin:0}
.sub{color:var(--slate);font-size:.85rem;margin:.25rem 0 1.2rem}
.chips{display:flex;flex-wrap:wrap;gap:.5rem;margin:0 0 1.4rem}
.chip{background:var(--panel);border:1px solid var(--rule);border-radius:.4rem;
padding:.25rem .6rem;font-family:var(--mono);font-size:.8rem}
h2{font-size:.8rem;letter-spacing:.08em;text-transform:uppercase;color:var(--slate);
border-top:1px solid var(--rule);padding-top:1rem;margin:1.6rem 0 .7rem}
table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:.8rem}
th{color:var(--slate);text-align:left;font-weight:600;padding:.3rem .5rem;
border-bottom:1px solid var(--rule)}td{padding:.3rem .5rem;border-bottom:1px solid var(--panel)}
td.n,th.n{text-align:right}.dim{color:var(--slate)}
.tl{position:relative;height:1.35rem;margin:.15rem 0;background:var(--panel);
border-radius:.25rem;overflow:hidden}
.tl .bar{position:absolute;top:.2rem;bottom:.2rem;border-radius:.2rem;opacity:.85}
.tl .lb{position:relative;z-index:1;font-family:var(--mono);font-size:.72rem;
line-height:1.35rem;padding-left:.45rem;color:var(--ink2);white-space:nowrap}
.vbar{height:.55rem;border-radius:.2rem;margin:.15rem 0 .5rem}
.legend{color:var(--slate);font-size:.78rem;border-top:1px solid var(--rule);
margin-top:1.6rem;padding-top:.8rem}
.overflow{overflow-x:auto}
"""


def _hue(vendor: str) -> str:
    return VENDOR_HUES.get(vendor, "#666")


def _e(s) -> str:
    return _html.escape(str(s if s is not None else "—"), quote=True)


def _ts_epoch(ts) -> float | None:
    try:
        return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _page(title: str, sub: str, body: str) -> str:
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{_e(title)}</title><style>{_CSS}</style></head><body><div class='wrap'>"
            f"<h1>{_e(title)}</h1><p class='sub'>{_e(sub)}</p>{body}"
            f"<p class='legend'>'—' = unmeasured: the vendor doesn't record it on disk"
            f" — never a silent zero. Token semantics differ per vendor; duration, tools"
            f" and files are the safe cross-vendor axes. Read-only report by organum.<br>"
            f"'—' = 미측정: 그 벤더가 디스크에 안 남긴 값이며, 조용한 0이 아닙니다."
            f" 토큰 계수 의미는 벤더별로 달라 교차 비교는 시간·툴·파일이 안전합니다."
            f" organum이 만든 read-only 리포트.</p>"
            f"</div></body></html>")


def _chips(pairs: list) -> str:
    return "<div class='chips'>" + "".join(
        f"<span class='chip'>{_e(k)} {_e(v)}</span>" for k, v in pairs) + "</div>"


def _session_table(cells: list) -> str:
    rows = []
    for c in cells:
        start = (c.get("first_ts") or "")[5:16].replace("T", " ") or "—"
        rows.append(
            f"<tr><td style='color:{_hue(c['vendor'])}'>{_e(c['vendor'])}</td>"
            f"<td>{_e(c.get('model'))}</td><td class='dim'>{_e(c.get('origin'))}</td>"
            f"<td>{_e(start)}</td><td class='n'>{_e(_fmt_dur(c.get('duration_s')))}</td>"
            f"<td class='n'>{_e(_fmt_tok(c.get('in_tok')))}</td>"
            f"<td class='n'>{_e(_fmt_tok(c.get('out_tok')))}</td>"
            f"<td class='n'>{_e(_fmt_tok(c.get('cache')))}</td>"
            f"<td class='n'>{_e(c.get('tool_calls', sum((c.get('tools') or {}).values())))}</td>"
            f"<td class='n'>{_e(len(c.get('files') or []))}</td></tr>")
    return ("<div class='overflow'><table><tr><th>vendor</th><th>model</th><th>origin</th>"
            "<th>start</th><th class='n'>duration</th><th class='n'>in</th>"
            "<th class='n'>out</th><th class='n'>cache</th><th class='n'>tools</th>"
            "<th class='n'>files</th></tr>" + "".join(rows) + "</table></div>")


def _timeline(cells: list) -> str:
    spans = [(c, _ts_epoch(c.get("first_ts")), _ts_epoch(c.get("last_ts")))
             for c in cells]
    spans = [(c, a, b) for c, a, b in spans if a is not None and b is not None]
    if not spans:
        return ""
    t0 = min(a for _, a, _ in spans)
    t1 = max(b for _, _, b in spans)
    total = max(t1 - t0, 1.0)
    rows = []
    for c, a, b in sorted(spans, key=lambda x: x[1]):
        left = (a - t0) / total * 100
        width = max((b - a) / total * 100, 0.6)
        label = f"{c['vendor']} · {c.get('model') or '—'} · {_fmt_dur(c.get('duration_s') or (b - a))}"
        rows.append(f"<div class='tl'><div class='bar' style='left:{left:.2f}%;"
                    f"width:{width:.2f}%;background:{_hue(c['vendor'])}'></div>"
                    f"<div class='lb'>{_e(label)}</div></div>")
    return "<h2>Timeline</h2>" + "".join(rows)


def _vendor_rollup(cells: list) -> str:
    vendors: dict = {}
    for c in cells:
        vendors.setdefault(c["vendor"], []).append(c)
    if len(vendors) < 2:
        return ""
    stats = []
    for v, vs in sorted(vendors.items()):
        durs = [c.get("duration_s") for c in vs if c.get("duration_s") is not None]
        ins_ = [c.get("in_tok") for c in vs if c.get("in_tok") is not None]
        stats.append((v, len(vs), sum(durs) if durs else None, sum(ins_) if ins_ else None,
                      sum(sum((c.get("tools") or {}).values()) for c in vs)))
    top_d = max((s[2] or 0) for s in stats) or 1
    out = ["<h2>By vendor</h2>"]
    for v, n, dur, in_t, tools in stats:
        pct = (dur or 0) / top_d * 100
        out.append(f"<div class='chip' style='border:0;background:none;padding:0'>"
                   f"<b style='color:{_hue(v)}'>{_e(v)}</b> · {n} sessions ·"
                   f" duration {_e(_fmt_dur(dur))} · in {_e(_fmt_tok(in_t))} · tools {tools}</div>"
                   f"<div class='vbar' style='width:{max(pct, 1):.1f}%;background:{_hue(v)}'></div>")
    return "".join(out)


def inspector_page(cells: list, project: str, window_days: float,
                   generated_at: str | None = None) -> str:
    gen = generated_at or time.strftime("%Y-%m-%d %H:%M %Z")
    measured_in = [c["in_tok"] for c in cells if c.get("in_tok") is not None]
    durs = [c["duration_s"] for c in cells if c.get("duration_s") is not None]
    body = _chips([("sessions", len(cells)),
                   ("Σ duration", _fmt_dur(sum(durs)) if durs else "—"),
                   ("Σ in", _fmt_tok(sum(measured_in)) if measured_in else "—"),
                   ("window", f"{window_days:g}d")])
    body += _timeline(cells)
    body += "<h2>Sessions</h2>" + _session_table(cells)
    body += _vendor_rollup(cells)
    return _page(f"organum inspector · {project}",
                 f"post-hoc metering · generated {gen}", body)


def observatory_page(live_cells: list, recs: list, project: str, days: float,
                     generated_at: str | None = None) -> str:
    """observatory 리포트의 HTML판 — 지금(live)/역사(축적) 분리 밴드, 합산 없음."""
    gen = generated_at or time.strftime("%Y-%m-%d %H:%M %Z")
    for r in recs:  # 레코드를 세션 테이블 모양으로 (files_touched → files 개수 표시용)
        r.setdefault("duration_s", _dur_s(r.get("first_ts"), r.get("last_ts")))
        r.setdefault("tools", {})
        r.setdefault("files", [None] * (r.get("files_touched") or 0))
        r.setdefault("tool_calls", sum((r.get("tools") or {}).values()))
    out_meas = [r["out_tok"] for r in recs if r.get("out_tok") is not None]
    body = _chips([("live now", len(live_cells)), ("history sessions", len(recs)),
                   ("Σ out (history)", _fmt_tok(sum(out_meas)) if out_meas else "—"),
                   ("window", f"{days:g}d")])
    body += "<h2>Now — live sessions (30-min window, lifetime totals)</h2>"
    body += _session_table(live_cells) if live_cells else "<p class='dim'>none</p>"
    body += "<h2>History — accumulated snapshots (survives vendor cleanup)</h2>"
    body += _session_table(recs) if recs else "<p class='dim'>no snapshots yet — run `organum observatory sync`</p>"
    body += _timeline(recs)
    body += _vendor_rollup(recs)
    return _page(f"organum observatory · {project}",
                 f"now vs history · generated {gen}", body)
