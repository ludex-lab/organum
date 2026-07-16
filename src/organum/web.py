"""organum web — 관제탑 (localhost 공유-인지 뷰, stdlib only, 관측 read-only · 게시판 human-write).

경계: **관제탑이지 관제사가 아니다** — 모든 세포를 *보여준다*(pull, read-only). 조종·디스패치 없음.
데이터: `find_all_transcripts` + `Vitals` (inspect.py 재사용). 웹 v1 = 관찰 수렴을 tmux 없이 브라우저로
(크로스-플랫폼·공개 얼굴). 각 세션 transcript = 관점-로컬 기록, 여기서 read-only 수렴 (§2.1-⑤).
"""

from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from organum import relay
from organum.inspect import ts_age_seconds

# 서버는 단일 스레드(HTTPServer) → 요청 간 공유 dict 안전. Vitals.update는 증분 tail.
_parent_cache: dict[str, str] = {}
_declared_cache: dict[str, tuple[float, str]] = {}  # path → (checked_at, 선언 id 또는 "")


def _find_parent(child_full_id: str, cli_paths: list) -> str | None:
    """subagent의 부모 = 그 세션 id를 자기 transcript에 언급하는 터미널 세션(=spawn한 쪽).
    transcript가 부모를 직접 기록하지 않아 cross-reference 휴리스틱. 결과 캐시(부모 불변)."""
    if child_full_id in _parent_cache:
        return _parent_cache[child_full_id] or None
    parent = ""
    for p in cli_paths:
        try:
            if child_full_id in Path(p).read_text(encoding="utf-8", errors="replace"):
                parent = Path(p).stem[:8]
                break
        except OSError:
            continue
    _parent_cache[child_full_id] = parent
    return parent or None


def _find_declared(path: str, cids: list[str]) -> str | None:
    """관찰 셀(transcript) ↔ 선언 세포 id 잇기 — `organum join`이 출력한 `ORGANUM_CELL=<id>` 마커를
    transcript 텍스트에서 찾는 cross-ref 휴리스틱(_find_parent와 같은 결, best-effort).
    join은 세션 초반이 보통이라 head 512KB(+tail 64KB)만 본다. 매치는 캐시, 미매치는 10초마다 재확인."""
    now = time.time()
    hit = _declared_cache.get(path)
    if hit and (hit[1] or now - hit[0] < 10.0):
        return hit[1] or None
    found = ""
    try:
        p = Path(path)
        size = p.stat().st_size
        with open(p, encoding="utf-8", errors="replace") as fh:
            text = fh.read(524288)
            if size > 524288 + 65536:
                fh.seek(size - 65536)
                text += fh.read()
    except OSError:
        text = ""
    for cid in cids:
        if f"ORGANUM_CELL={cid}" in text:
            found = cid
            break
    _declared_cache[path] = (now, found)
    return found or None


def escalations(cwd: Path) -> list:
    """열린 에스컬레이션 — 세포/chief가 'human 필요'를 플래그한 편지(escalate: true, 미보관).
    relay+agora 합산, 최신순. '처리' = human의 보관(archive, 가역) — 엔벨로프는 불변, 상태 mutate 없음."""
    from organum import agora as _agora
    out = []
    for fld, msgs in (("relay", relay.list_all(cwd)), ("agora", _agora.list_all(cwd))):
        out += [{**m, "field": fld} for m in msgs if m.get("escalate")]
    out.sort(key=lambda m: m.get("ts") or "", reverse=True)
    return out


def payload(cwd: Path) -> dict:
    from organum import adapters as _ad
    from organum import alarm as _alarm
    from organum import session as _sess
    from organum import state as _st

    raw = _ad.snapshot(cwd, window_min=30.0)  # 벤더별 관찰(Claude·Codex…)을 정규화 Cell로
    now = time.time()
    state_dir = cwd / _st.STATE_DIR_NAME  # 세션은 soma(read-only 스캔)
    sessions = _sess.open_sessions(state_dir) if state_dir.exists() else []
    retros = _sess.recent_retros(state_dir) if state_dir.exists() else []
    sess_by_cell = {s["cell"]: s for s in sessions if s.get("cell")}
    branch = None
    # 패밀리 부모 후보 = Claude 터미널 세션의 transcript 경로 (cross-ref 휴리스틱은 Claude 전용)
    cli_paths = [c["path"] for c in raw
                 if c["vendor"] == "claude" and c["origin"] == "terminal" and c.get("path")]
    cells = []
    for c in sorted(raw, key=lambda x: x["last_ts"] or "", reverse=True):
        # **파일 mtime이 아니라 마지막 내용 timestamp 기준** — 유령 배제.
        age = ts_age_seconds(c["last_ts"], now)
        if age is None or age > 1800:  # 30분 내용 창 밖 = 유령 → 안 보임
            continue
        if c.get("branch"):
            branch = c["branch"]
        tdict = c["tools"] if isinstance(c["tools"], dict) else {}
        origin = c["origin"]
        # 어댑터가 경로/DB에서 확정한 parent 우선; 없으면 텍스트 cross-ref 휴리스틱(구형 SDK-spawn 톱레벨)
        parent = c.get("parent")
        if parent is None and c["vendor"] == "claude" and origin == "subagent" and c.get("path"):
            parent = _find_parent(Path(c["path"]).stem, cli_paths)
        # 관찰 id ↔ 선언 id 재조정: 직접 매치 → ORGANUM_CELL 마커 cross-ref (텍스트 transcript만)
        declared = None
        sess = sess_by_cell.get(c["id"])
        if sess is None and sessions and str(c.get("path") or "").endswith(".jsonl"):
            declared = _find_declared(c["path"], [s["cell"] for s in sessions if s.get("cell")])
            sess = sess_by_cell.get(declared) if declared else None
        cells.append({
            "id": c["id"], "vendor": c["vendor"],
            "model": c["model"] or "—",
            "in": c["in_tok"], "out": c["out_tok"], "cache": c["cache"], "tools": sum(tdict.values()),
            "touch": len(c["files"]), "last_ts": c["last_ts"], "last": "",
            "age": int(age), "live": age < 90,
            "origin": origin, "parent": parent,
            "skills": dict(sorted(c["skills"].items(), key=lambda kv: -kv[1])[:4]) if isinstance(c["skills"], dict) else {},
            "tool_breakdown": dict(sorted(tdict.items(), key=lambda kv: -kv[1])[:6]),
            "fallback": bool(c.get("fallback")),
            "declared": declared,  # join한 선언 id — 칩·relay 수신인이 이걸 써야 편지가 닿는다
            "session": sess,       # 직접 id 매치 또는 선언 id cross-ref (best-effort)
        })
    def _agg(key):  # None=미측정(벤더가 안 남김)은 합계에서 제외 — 0으로 뭉개지 않는다
        vals = [c[key] for c in cells if c[key] is not None]
        return sum(vals) if vals else None
    try:  # 폴링에 편승하는 관측 영속화 — settle된(idle≥90s) 셀만, 실패해도 관제탑은 산다
        from organum import observatory as _obs
        _obs.record(state_dir, raw, reason="web", only_idle_sec=90.0)
    except Exception:
        pass
    return {
        "project": cwd.name, "branch": branch, "cells": len(cells),
        "aggregate": {
            "in": _agg("in"),
            "out": _agg("out"),
            "cache": _agg("cache"),
            "tools": sum(c["tools"] for c in cells),
        },
        "cell_list": cells,
        "sessions": sessions,
        "retros": retros,
        "escalations": escalations(cwd),
        "alarms": _alarm.active(cwd),
    }


# 우체통 로직(list/send/archive/inbox/read-cursor)은 organum/relay.py로 일원화.

PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>organum · 관제탑</title>
<style>
:root{--paper:#ECEFEA;--panel:#F4F6F1;--ink:#161A17;--ink2:#3C433B;--slate:#6B726A;
--rule:#CBD1C7;--carmine:#A12A32;--moss:#556A4B;
--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
--sans:-apple-system,"Helvetica Neue","Segoe UI",system-ui,sans-serif;
--serif:"Iowan Old Style",Palatino,Georgia,serif;}
@media(prefers-color-scheme:dark){:root{--paper:#0F1311;--panel:#161B18;--ink:#E8E5DD;
--ink2:#AAB0A5;--slate:#868D82;--rule:#28302B;--carmine:#D0585E;--moss:#8FA681;}}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--sans);font-size:15px;line-height:1.5;-webkit-font-smoothing:antialiased}
.wrap{max-width:1000px;margin:0 auto;padding:1.6rem clamp(1rem,4vw,2.5rem)}
header{display:flex;align-items:baseline;gap:.8rem;flex-wrap:wrap;border-bottom:1px solid var(--rule);padding-bottom:1rem;margin-bottom:1.2rem}
.brand{font-family:var(--serif);font-size:1.3rem;font-weight:600}
.brand b{color:var(--carmine)} .dot{color:var(--carmine)}
.meta{font-family:var(--mono);font-size:.8rem;color:var(--slate)}
.beat{margin-left:auto;font-family:var(--mono);font-size:.78rem;color:var(--slate)}
.beat .s{color:var(--carmine)}
.agg{font-family:var(--mono);font-size:.86rem;color:var(--ink2);background:var(--panel);border:1px solid var(--rule);border-radius:10px;padding:.7rem 1rem;margin-bottom:1.3rem}
.agg b{color:var(--ink)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:1rem}
.cell{border:1px solid var(--rule);border-radius:12px;background:var(--panel);padding:1rem 1.1rem;display:flex;flex-direction:column;gap:.45rem}
.cell.stale{opacity:.55}
.cell h2{margin:0;font-family:var(--mono);font-size:.9rem;font-weight:600;color:var(--carmine);display:flex;justify-content:space-between;gap:.5rem}
.cell h2 .m{color:var(--ink2);font-weight:400}
.cell h2 .orig{font-size:.66rem;padding:.05rem .38rem;border-radius:5px;border:1px solid var(--rule);color:var(--slate)}
.cell h2 .orig.sub{color:var(--moss);border-color:var(--moss)}
.row{font-family:var(--mono);font-size:.8rem;color:var(--ink2)}
.row .k{color:var(--ink)} .q{color:var(--moss)}
.last{font-family:var(--mono);font-size:.78rem;color:var(--slate);border-top:1px dashed var(--rule);padding-top:.45rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.empty{font-family:var(--mono);color:var(--slate);padding:2rem 0}
#board{margin-top:2.2rem;border-top:1px solid var(--rule);padding-top:1.4rem}
.bhead{font-family:var(--serif);font-size:1.05rem;margin:0 0 .8rem;color:var(--ink)}
.bhead .sub{font-family:var(--mono);font-size:.72rem;color:var(--slate);font-weight:400}
#compose{display:flex;flex-direction:column;gap:.5rem;margin-bottom:1.2rem}
.frow{display:flex;gap:.5rem;flex-wrap:wrap}
#compose input,#compose textarea{font-family:var(--mono);font-size:.82rem;background:var(--panel);color:var(--ink);border:1px solid var(--rule);border-radius:8px;padding:.5rem .7rem}
#compose input{flex:1;min-width:120px}
#compose textarea{min-height:68px;resize:vertical}
#compose button{align-self:flex-start;font-family:var(--mono);font-size:.82rem;background:var(--carmine);color:#fff;border:0;border-radius:8px;padding:.5rem 1.1rem;cursor:pointer}
#compose button:hover{opacity:.9}
#tobar{display:flex;align-items:center;gap:.5rem;flex-wrap:wrap}
.tolab{font-family:var(--mono);font-size:.76rem;color:var(--slate)}
#tochips{display:flex;flex-wrap:wrap;gap:.4rem}
.chip{font-family:var(--mono);font-size:.76rem;border:1px solid var(--rule);border-radius:100px;padding:.26rem .7rem;cursor:pointer;color:var(--ink2);user-select:none}
.chip.on{background:var(--carmine);color:#fff;border-color:var(--carmine)}
.chip.dim{opacity:.4;cursor:not-allowed}
.msg{border:1px solid var(--rule);border-left:2px solid var(--moss);border-radius:8px;background:var(--panel);margin-bottom:.55rem;overflow:hidden}
.msg summary{list-style:none;cursor:pointer;padding:.55rem .9rem;font-family:var(--mono);font-size:.72rem;color:var(--slate);display:flex;justify-content:space-between;gap:.6rem}
.msg summary::-webkit-details-marker{display:none}
.msg summary b{color:var(--carmine)}
.msg summary:hover{background:var(--paper)}
.msg[open] summary{border-bottom:1px dashed var(--rule)}
.msg .mb{font-family:var(--mono);font-size:.8rem;color:var(--ink2);white-space:pre-wrap;word-break:break-word;padding:.55rem .9rem}
.marc{padding:0 .9rem .55rem;text-align:right}
.arcbtn{font-family:var(--mono);font-size:.7rem;color:var(--slate);background:none;border:1px solid var(--rule);border-radius:6px;padding:.18rem .55rem;cursor:pointer}
.arcbtn:hover{color:var(--carmine);border-color:var(--carmine)}
footer{margin-top:2rem;font-family:var(--mono);font-size:.72rem;color:var(--slate);border-top:1px solid var(--rule);padding-top:1rem}
.langbtn{font-family:var(--mono);font-size:.72rem;color:var(--slate);background:none;border:1px solid var(--rule);border-radius:6px;padding:.15rem .5rem;cursor:pointer;margin-left:.6rem}
.langbtn:hover{color:var(--carmine);border-color:var(--carmine)}
#alerts{margin:0 0 1rem}
.ebox{border:1px solid var(--carmine);border-left:3px solid var(--carmine);border-radius:10px;background:var(--panel);padding:.55rem .9rem;margin-bottom:.5rem}
.ebox .eh{font-family:var(--mono);font-size:.74rem;color:var(--carmine);display:flex;justify-content:space-between;gap:.6rem}
.ebox .eh .lvl{border:1px solid var(--carmine);border-radius:5px;padding:.02rem .35rem;font-size:.66rem}
.ebox .eb{font-family:var(--mono);font-size:.8rem;color:var(--ink2);white-space:pre-wrap;word-break:break-word;margin-top:.25rem}
.ebox .ea{text-align:right;margin-top:.3rem}
.ebox.notice{border-color:var(--moss);border-left-color:var(--moss)}
.ebox.notice .eh{color:var(--moss)} .ebox.notice .eh .lvl{border-color:var(--moss)}
#alarmlab{display:flex;align-items:center;gap:.3rem;font-family:var(--mono);font-size:.76rem;color:var(--carmine);cursor:pointer;user-select:none}
#sessions{margin:.5rem 0 .2rem}
#sessions .sh{font-family:var(--mono);font-size:.74rem;color:var(--slate);margin:.1rem 0 .3rem;letter-spacing:.03em}
.srow{font-family:var(--mono);font-size:.8rem;color:var(--ink2);padding:.1rem 0;white-space:pre-wrap;word-break:break-word}
.srole{color:var(--moss)}
.sidle,.scell{color:var(--slate)}
#sessions .sret{font-family:var(--mono);font-size:.73rem;color:var(--slate);margin-top:.3rem}
</style></head>
<body><div class="wrap">
<header>
  <span class="brand"><span class="dot">&#9673;</span> organ<b>um</b></span>
  <span class="meta" id="proj">&#8230;</span>
  <span class="beat"><span class="s" id="spin">&#10287;</span> <span id="cells">0 cells</span> &middot; <span id="idle">&ndash;</span></span>
  <button id="lang" class="langbtn">EN</button>
</header>
<div class="agg" id="agg">&#8230;</div>
<section id="alerts"></section>
<section id="sessions"></section>
<div class="grid" id="grid"></div>
<section id="board">
  <h3 class="bhead">&#9993; <span data-i18n="board">board · mailbox</span> <span class="sub" data-i18n="boardSub"></span></h3>
  <form id="compose">
    <div id="tobar"><span class="tolab">to</span><span id="tochips"></span>
      <label id="alarmlab"><input type="checkbox" id="alarmck"> &#9888; <span data-i18n="alarmCk">pause alarm</span></label></div>
    <input id="topic" data-i18nph="topicPh" placeholder="topic">
    <textarea id="body" data-i18nph="bodyPh" placeholder=""></textarea>
    <button type="submit" data-i18n="drop">drop</button>
  </form>
  <div id="msgs"></div>
</section>
<footer>&#12288;<span data-i18n="footer"></span></footer>
</div>
<script>
var I18N={
 en:{converge:"one site · read-only convergence",noCells:"no active cells — run claude in this folder and they appear",
  ago:"ago",board:"board · mailbox",boardSub:"— human drops a letter, cells pull it (pull)",
  topicPh:"topic (optional)",bodyPh:"a letter to the cells in this site…",drop:"drop",noMsg:"no letters",archive:"archive",
  sessTitle:"sessions",sessActive:"active",sessBeats:"beats",sessRecent:"recent",idle:"idle",
  alertEsc:"needs human",alertAlarm:"alarm",resolve:"resolve",alarmCk:"pause alarm",
  footer:"control tower (cells read-only) + board (human drops · cells pull) · organum is medium+view, not control · Ctrl-C to quit",
  disc:"server disconnected (organum web stopped?)",title:"organum · control tower"},
 ko:{converge:"한 현장 read-only 수렴",noCells:"활성 세포 없음 — 이 폴더에서 claude를 띄우면 나타납니다",
  ago:"전",board:"게시판 · 우체통",boardSub:"— 사람이 편지를 드롭, 세포가 당겨 읽는다 (pull)",
  topicPh:"topic (선택)",bodyPh:"이 현장의 세포들에게 남길 편지…",drop:"드롭",noMsg:"편지 없음",archive:"보관",
  sessTitle:"세션",sessActive:"활성",sessBeats:"비트",sessRecent:"최근",idle:"idle",
  alertEsc:"human 필요",alertAlarm:"경보",resolve:"처리",alarmCk:"pause 경보",
  footer:"관제탑(세포 read-only) + 게시판(사람이 드롭 · 세포가 pull) · organum은 매체+뷰지 조종 아님 · Ctrl-C로 종료",
  disc:"서버 연결 끊김 (organum web 종료됨?)",title:"organum · 관제탑"}
};
var LANG=localStorage.getItem("organum-lang")||(((navigator.language||"").slice(0,2)==="ko")?"ko":"en");
function t(k){return (I18N[LANG]||I18N.en)[k]||k;}
function applyI18n(){
 document.documentElement.lang=LANG;document.title=t("title");
 document.querySelectorAll("[data-i18n]").forEach(function(el){el.textContent=t(el.getAttribute("data-i18n"));});
 document.querySelectorAll("[data-i18nph]").forEach(function(el){el.placeholder=t(el.getAttribute("data-i18nph"));});
 var lb=document.getElementById("lang");if(lb)lb.textContent=(LANG==="en"?"한국어":"EN");
 chipKey="";msgSig="";prev="";
}
const SP="\\u280b\\u2819\\u2839\\u2838\\u283c\\u2834\\u2826\\u2827".split("");
let si=0,lastChange=Date.now(),prev="";
let currentCells=[],toSel=new Set(["all"]),chipKey="",msgSig="";
function fmt(n){if(n==null)return "\\u2014";if(n>=1e6)return (n/1e6).toFixed(1)+"M";if(n>=1e3)return (n/1e3).toFixed(1)+"k";return ""+n;}
function esc(s){return (s||"").replace(/[&<>]/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;"}[c];});}
async function tick(){
  si=(si+1)%SP.length;document.getElementById("spin").textContent=SP[si];
  try{
    const r=await fetch("/vitals",{cache:"no-store"});const d=await r.json();
    const sig=JSON.stringify(d.cell_list.map(function(c){return [c.id,c.out,c.tools];}));
    if(sig!==prev){lastChange=Date.now();prev=sig;}
    document.getElementById("idle").textContent="idle "+Math.round((Date.now()-lastChange)/1000)+"s";
    document.getElementById("proj").textContent="\\u00b7 "+d.project+(d.branch?(" \\u00b7 \\u2325"+d.branch):"");
    document.getElementById("cells").textContent=d.cells+" cell"+(d.cells===1?"":"s");
    const a=d.aggregate;
    document.getElementById("agg").innerHTML="\\u03a3 in <b>"+fmt(a["in"])+"</b> \\u00b7 out <b>"+fmt(a.out)+"</b> \\u00b7 cache <b>"+fmt(a.cache)+"</b> \\u00b7 tools <b>"+a.tools+"</b> &nbsp;\\u00b7&nbsp; "+esc(t("converge"));
    currentCells=d.cell_list;renderChips(currentCells);renderAlerts(d.alarms,d.escalations);renderSessions(d.sessions,d.retros);
    const g=document.getElementById("grid");
    if(!d.cell_list.length){g.innerHTML='<div class="empty">'+esc(t("noCells"))+'</div>';return;}
    g.innerHTML=d.cell_list.map(function(c){
      const skills=Object.keys(c.skills||{}).map(function(k){return k+"\\u00d7"+c.skills[k];}).join(" \\u00b7 ");
      const tb=c.tool_breakdown||{};
      const tools=Object.keys(tb).map(function(k){return k+"\\u00d7"+tb[k];}).join(" \\u00b7 ");
      const dot='<span style="color:'+(c.live?"var(--moss)":"var(--slate)")+'">\\u25cf</span> ';
      const age=c.live?"live":(c.age<3600?Math.round(c.age/60)+"m "+t("ago"):Math.round(c.age/3600)+"h "+t("ago"));
      return '<div class="cell'+(c.live?"":" stale")+'"><h2><span>'+dot+esc(c.id)+(c.declared&&c.declared!==c.id?" \\u00b7 "+esc(c.declared):"")+(c.fallback?" \\u26a0":"")+'</span><span class="m">'+esc((c.vendor?c.vendor+" · ":"")+c.model)+' <span class="orig'+(c.origin==="subagent"?" sub":"")+'">'+esc(c.origin==="subagent"?("subagent"+(c.parent?" \\u2190 "+c.parent:"")):c.origin)+'</span></span></h2>'
        +(c.session?'<div class="row"><span class="srole">['+esc(c.session.role||"\\u2014")+']</span> '+esc(c.session.intent)+'</div>':"")
        +'<div class="row">in <span class="k">'+fmt(c["in"])+'</span> \\u00b7 out <span class="k">'+fmt(c.out)+'</span> \\u00b7 cache '+fmt(c.cache)+' \\u00b7 tools <span class="k">'+c.tools+'</span> \\u00b7 touch '+c.touch+'</div>'
        +(skills?'<div class="row">skills: '+esc(skills)+'</div>':"")
        +'<div class="row">'+esc(tools||"\\u2014")+'</div>'
        +'<div class="last">\\u25b8 '+esc(c.last||"\\u2014")+'<span style="float:right">'+age+'</span></div></div>';
    }).join("");
  }catch(e){document.getElementById("agg").textContent=t("disc");}
}
tick();setInterval(tick,1500);
async function relayTick(){
  try{
    const r=await fetch("/relay",{cache:"no-store"});const m=await r.json();
    const el=document.getElementById("msgs");
    if(!m.length){el.innerHTML='<div style="font-family:var(--mono);font-size:.78rem;color:var(--slate)">'+esc(t("noMsg"))+'</div>';msgSig="";return;}
    var sig=m.map(function(x){return x.file;}).join("|");
    if(sig===msgSig)return;msgSig=sig;
    el.innerHTML=m.map(function(x){
      var tt=(x.ts||"").replace("T"," ").replace("Z","");
      return '<details class="msg"><summary><span>'+(x.escalate?'\\u26a0 ':'')+'<b>'+esc(x.from)+'</b> \\u2192 '+esc(x.to)+(x.topic?' \\u00b7 '+esc(x.topic):"")+'</span><span>'+esc(tt)+'</span></summary><div class="mb">'+esc(x.body)+'</div><div class="marc"><button class="arcbtn" data-file="'+esc(x.file)+'" data-field="'+esc(x.field||"relay")+'">'+esc(t("archive"))+'</button></div></details>';
    }).join("");
  }catch(e){}
}
function syncChips(){
  document.querySelectorAll("#tochips .chip").forEach(function(el){
    var id=el.dataset.id;el.classList.toggle("on",toSel.has(id));
    if(id!=="all"){var c=currentCells.find(function(x){return (x.declared||x.id)===id;});el.classList.toggle("dim",!(c&&c.live));}
  });
}
function renderAlerts(al,es){
  al=al||[];es=es||[];
  var el=document.getElementById("alerts");if(!el)return;
  document.title=((al.length+es.length)?"\\u26a0"+(al.length+es.length)+" ":"")+t("title");
  if(!al.length&&!es.length){el.innerHTML="";return;}
  function box(x,cls,tag,fld){
    var tt=(x.ts||"").replace("T"," ").replace("Z","");
    return '<div class="ebox '+cls+'"><div class="eh"><span>\\u26a0 <span class="lvl">'+esc(tag)+'</span> <b>'+esc(x.from)+'</b>'+(x.to&&x.to!=="all"&&x.to!=="field"?' \\u2192 '+esc(x.to):"")+'</span><span>'+esc(tt)+'</span></div><div class="eb">'+esc(x.body)+'</div><div class="ea"><button class="arcbtn rsv" data-file="'+esc(x.file)+'" data-field="'+esc(fld)+'">'+esc(t("resolve"))+'</button></div></div>';
  }
  el.innerHTML=al.map(function(x){return box(x,x.level==="pause"?"":"notice",t("alertAlarm")+" \\u00b7 "+x.level,"alarm");}).join("")
    +es.map(function(x){return box(x,"",t("alertEsc"),x.field);}).join("");
}
document.getElementById("alerts").addEventListener("click",async function(ev){
  var b=ev.target.closest(".rsv");if(!b)return;ev.preventDefault();
  await fetch("/relay/archive",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({file:b.dataset.file,field:b.dataset.field})});
  tick();
});
function renderSessions(ss,rr){
  var el=document.getElementById("sessions");if(!el)return;
  ss=ss||[];rr=rr||[];
  if(!ss.length&&!rr.length){el.innerHTML="";return;}
  var head='<div class="sh">\\u25c8 '+esc(t("sessTitle"))+' \\u00b7 '+ss.length+' '+esc(t("sessActive"))+'</div>';
  var rows=ss.map(function(s){
    var idle=s.idle_min<1?"live":(s.idle_min+"m "+t("idle"));
    return '<div class="srow">\\u25cf <span class="srole">['+esc(s.role||"\\u2014")+']</span> '+esc(s.intent)
      +' <span class="sidle">\\u00b7 '+idle+' \\u00b7 '+s.beats+' '+esc(t("sessBeats"))+'</span>'
      +(s.cell?' <span class="scell">\\u00b7 '+esc(s.cell)+'</span>':"")+'</div>';
  }).join("");
  var ret=rr.length?('<div class="sret">'+esc(t("sessRecent"))+': '+rr.map(function(r){
    return '['+esc(r.role||"\\u2014")+'] '+esc(r.intent)+' \\u2713'+r.shipped+' ~'+r.peers+' \\u00b7 '+r.duration_min+'m';
  }).join(" \\u00b7 ")+'</div>'):"";
  el.innerHTML=head+rows+ret;
}
function renderChips(allCells){
  var cells=allCells.filter(function(c){return c.origin==="terminal";});
  var ids=["all"].concat(cells.map(function(c){return c.declared||c.id;}));  // 선언 id 우선 — 편지가 join 정체성에 닿게
  var liveOK=new Set(["all"].concat(cells.filter(function(c){return c.live;}).map(function(c){return c.declared||c.id;})));
  toSel=new Set([...toSel].filter(function(x){return liveOK.has(x);}));if(!toSel.size)toSel.add("all");
  var key=ids.join(",");
  if(key!==chipKey){chipKey=key;document.getElementById("tochips").innerHTML=ids.map(function(id){return '<span class="chip" data-id="'+esc(id)+'">'+esc(id)+'</span>';}).join("");}
  syncChips();
}
document.getElementById("tochips").addEventListener("click",function(ev){
  var el=ev.target.closest(".chip");if(!el)return;var id=el.dataset.id;
  if(id!=="all"){var c=currentCells.find(function(x){return (x.declared||x.id)===id;});if(!(c&&c.live))return;}
  if(id==="all")toSel=new Set(["all"]);
  else{toSel.delete("all");if(toSel.has(id))toSel.delete(id);else toSel.add(id);if(!toSel.size)toSel.add("all");}
  syncChips();
});
document.getElementById("compose").addEventListener("submit",async function(ev){
  ev.preventDefault();
  var body=document.getElementById("body").value||"";
  if(!body.trim())return;
  var to=toSel.has("all")?"all":[...toSel].join(",");
  var ck=document.getElementById("alarmck");
  if(ck&&ck.checked){  // 긴급 = 경보 필드로 (human 발동, pause 권고)
    await fetch("/alarm",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({to:to,body:body,level:"pause"})});
    ck.checked=false;document.getElementById("body").value="";tick();return;
  }
  await fetch("/relay",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({from:"human",to:to,topic:document.getElementById("topic").value||"",body:body})});
  document.getElementById("body").value="";relayTick();
});
relayTick();setInterval(relayTick,3000);
document.getElementById("msgs").addEventListener("click",async function(ev){
  var b=ev.target.closest(".arcbtn");if(!b)return;ev.preventDefault();ev.stopPropagation();
  await fetch("/relay/archive",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({file:b.dataset.file,field:b.dataset.field||"relay"})});
  msgSig="";relayTick();
});
document.getElementById("lang").addEventListener("click",function(){
  LANG=(LANG==="en"?"ko":"en");localStorage.setItem("organum-lang",LANG);applyI18n();tick();relayTick();
});
applyI18n();
</script>
</body></html>"""


# 읽기/쓰기 권한의 *의미*는 세 표면(서버 배너·CLI 도움말·문서)에서 일치해야 한다(불변조건 ⑤).
# 서버 배너는 이 상수를 소비하고, CLI/문서는 같은 문구를 담는다 — 테스트가 그 의미 일치를 강제.
ROLE_LABEL = "관측 read-only · 게시판 human-write"
MAX_POST_BYTES = 1_000_000  # 게시판 본문 상한 — localhost 도구지만 무한 수신은 계약이 아니다
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 조용히
        pass

    def _post_allowed(self) -> tuple[bool, int, str, int]:
        """게시판 쓰기 전 게이트(불변조건 ④⑤). 반환 (ok, http코드, 사유, 검증된_본문길이):
        ① 초기화된 현장에서만(반쪽 .organum 금지) ② 원격 바인드 쓰기 거부(Origin은 CSRF 완화일 뿐
        접근통제가 아님 — 비-loopback 바인드면 쓰기 자체를 차단) ③ Content-Length 0..상한 1회 검증
        (음수 우회 금지, read에 재사용) ④ Origin은 URL 파싱한 hostname을 정확 loopback allowlist와 비교
        (substring 'localhost.evil.example' 우회 금지)."""
        from urllib.parse import urlsplit
        sd = self.server.state_dir
        if sd is None or not (sd / "meta.json").is_file():
            return False, 400, "organum init 필요 — 게시판 쓰기는 초기화된 현장에서만 (관측은 init 불요)", 0
        if self.server.bind_host not in _LOOPBACK and not self.server.allow_remote_write:
            return False, 403, ("원격 바인드에서 게시판 쓰기 거부 — 관측은 read-only로 노출되지만 "
                                "쓰기는 로컬 전용. 명시적 위험 승인은 --allow-remote-write."), 0
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            return False, 400, "잘못된 Content-Length", 0
        if n < 0 or n > MAX_POST_BYTES:
            return False, 413, f"본문 길이 {n}이 허용 범위(0..{MAX_POST_BYTES}) 밖", 0
        origin = self.headers.get("Origin")
        if origin:
            host = urlsplit(origin).hostname or ""
            if host not in _LOOPBACK:
                return False, 403, "교차 출처 게시 거부 (관제탑 게시판은 로컬 브라우저에서만)", 0
        return True, 200, "", n

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.server.last_request = time.time()  # 뷰어 감지 — 브라우저가 열려 있으면 폴링이 계속 온다
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def do_GET(self):
        if self.path.startswith("/vitals"):
            body = json.dumps(payload(self.server.cwd), ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
        elif self.path.startswith("/relay"):
            from organum import agora as agora_mod  # 게시판 = relay(지향) + agora(개방) 합쳐 보여줌
            msgs = ([{**m, "field": "relay"} for m in relay.list_all(self.server.cwd)]
                    + [{**m, "field": "agora"} for m in agora_mod.list_all(self.server.cwd)])
            msgs.sort(key=lambda m: m.get("ts") or "", reverse=True)
            body = json.dumps(msgs, ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
        elif self.path == "/" or self.path.startswith("/index"):
            self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")

    def do_POST(self):
        if not (self.path.startswith("/relay") or self.path.startswith("/alarm")):
            self._send(404, "text/plain; charset=utf-8", b"not found")
            return
        ok, code, why, n = self._post_allowed()
        if not ok:
            self._send(code, "application/json; charset=utf-8",
                       json.dumps({"ok": False, "error": why}, ensure_ascii=False).encode("utf-8"))
            return
        try:  # 길이는 게이트가 검증한 n만 사용 (음수/초과 우회 차단, 불변조건 ⑤)
            data = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            data = {}
        cwd = self.server.cwd
        if self.path.startswith("/alarm"):  # 관제탑은 human의 표면 — 발동자는 항상 human
            from organum import alarm as alarm_mod
            try:
                fname = alarm_mod.sound(cwd, self.server.state_dir or cwd, str(data.get("body", "")),
                                        frm="human", to=str(data.get("to", "all") or "all"),
                                        level=str(data.get("level", "notice") or "notice"), src="alarm-web")
            except alarm_mod.AlarmError:
                fname = None
            self._send(200 if fname else 400, "application/json; charset=utf-8",
                       json.dumps({"ok": bool(fname), "file": fname}, ensure_ascii=False).encode("utf-8"))
        elif self.path.startswith("/relay/archive"):  # human 게시판/에스컬레이션/경보 정리 = 소프트 보관(가역)
            from organum import field as field_mod
            fld = str(data.get("field") or "relay")
            ok = fld in ("relay", "agora", "alarm") and field_mod.archive(cwd, fld, str(data.get("file") or ""))
            self._send(200 if ok else 400, "application/json; charset=utf-8",
                       json.dumps({"ok": ok}).encode("utf-8"))
        else:
            to = str(data.get("to", "all") or "all").strip()
            body = data.get("body", "")
            frm = data.get("from", "human")
            topic = data.get("topic", "")
            if to in ("all", "field", ""):  # 개방 = agora(토론장, 모두 읽음) — 관측 폴링하는 세포가 본다
                from organum import agora as agora_mod
                fname = agora_mod.post(cwd, body, frm=frm, topic=topic, src="agora-web")
            else:                            # 특정 세포 = relay(지향 우체통)
                fname = relay.send(cwd, body, frm=frm, to=to, topic=topic, src="relay-web")
            self._send(200 if fname else 400, "application/json; charset=utf-8",
                       json.dumps({"ok": bool(fname), "file": fname} if fname else {"error": "빈 본문"},
                                  ensure_ascii=False).encode("utf-8"))


class _Server(HTTPServer):
    def __init__(self, addr, cwd: Path, state_dir: Path | None,
                 allow_remote_write: bool = False):
        super().__init__(addr, _Handler)
        # stateful root를 state_dir.parent로 고정(불변조건 ④): 하위 디렉터리에서 web을 띄워도
        # 게시판 쓰기가 자식 cwd에 반쪽 .organum을 만들지 않고 부모 현장에 닿는다.
        self.cwd = state_dir.parent if state_dir is not None else cwd
        self.state_dir = state_dir
        self.bind_host = addr[0]
        self.allow_remote_write = allow_remote_write
        self.last_request = time.time()


def _reap_when_idle(httpd, idle_min: float, tick_s: float = 30.0):
    """idle 자멸(relay watch와 같은 규율) — 뷰어 요청이 idle_min분 없으면 스스로 종료.

    신호는 '셀 활동'이 아니라 '뷰어 존재': 열린 브라우저는 몇 초마다 폴링하므로
    마지막 HTTP 요청 시각이 관전자 감지기다. 아무도 안 보는 관제탑은 관측 기록도
    돌지 않는(기록은 요청이 와야 돈다) 잊힌 프로세스 — 조용히 자리를 비켜준다."""
    import threading

    def loop():
        while True:
            time.sleep(tick_s)
            idle = time.time() - httpd.last_request
            if idle >= idle_min * 60:
                print(f"\norganum web: idle 자멸 — {idle_min:g}분간 뷰어 요청 없음. "
                      f"(다시 보려면: organum web · 끄려면: --idle-timeout 0)")
                httpd.shutdown()
                return
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


def serve(cwd: Path, state_dir: Path | None, port: int = 7332, host: str = "127.0.0.1",
          idle_timeout_min: float = 120.0, allow_remote_write: bool = False) -> int:
    httpd = None
    for p in range(port, port + 10):  # 포트 사용 중이면 다음 것 시도
        try:
            httpd = _Server((host, p), cwd, state_dir, allow_remote_write=allow_remote_write)
            port = p
            break
        except OSError:
            continue
    if httpd is None:
        print(f"organum web: {port}~{port + 9} 포트가 모두 사용 중 — --port로 지정하세요.")
        return 1
    print(f"organum web — 관제탑 ({ROLE_LABEL}) · {cwd.name}")
    print(f"  → http://{host}:{port}/    (Ctrl-C 종료)")
    if idle_timeout_min > 0:
        print(f"  idle 자멸: 뷰어 없음 {idle_timeout_min:g}분 후 (조절: --idle-timeout, 0=끄기)")
        _reap_when_idle(httpd, idle_timeout_min)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\norganum web: 종료.")
    finally:
        httpd.server_close()
    return 0
