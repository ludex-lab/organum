"""organum bench — 협업벤치 피어저널 수확·집계 (인사팀의 읽기 눈).

organ-effect-matrix v0.1.1(§2 peer 차원·§4 provenance 봉투)의 organum 조각:
사이트 somas에 흩어진 세션 피어저널(관점-로컬 single-writer)을 **결정적으로**
수확해 피어별로 모은다 — 여러 세션·여러 평가자의 관측이 처음으로 한 화면에.

경계(정직 규율):
- **텍스트→축 코딩 없음**: peer 축({execution·initiative·coordination·review·pacing})은
  strengths/frictions *서술*의 해석 코딩이 필요하다 — 판정은 organum 밖(사람 또는 사용자
  CLI 위임) 몫. 여기선 verbatim 보존 + 카운트 + provenance만(measured ≠ asserted).
- **label ≠ identity**: peers[].peer는 자유 라벨(R2 실물 "engine·codex"). canonical cell
  문법에 맞고 이 사이트 선언 셀에 실존할 때만 resolved — 아니면 label-only 그대로,
  fuzzy 병합 없음(identity 6라운드 교훈).
- **포화 강등(§4)**: 축 값 하나가 >90%면 변별 축이 아니라 보조 플래그다 —
  would_pair_again 실측 포화(R2 26건 T24/N2/F0). 리포트가 saturated를 계산해 단다.
- read-only — 수확·집계가 어떤 기록도 남기지 않는다(관제탑 결).
"""

from __future__ import annotations

import datetime
from pathlib import Path

SATURATION = 0.9   # §4 포화 강등 문턱 — 한 값의 비율이 이걸 넘으면 보조 플래그


def harvest(state_dir: Path) -> list[dict]:
    """사이트 전 somas(owner + cells/*)의 세션 피어저널 → 평탄 엔트리.
    엔트리 = 피어 노트 1건 + 작성자 provenance(rater·sid·ended_at·direction)."""
    from organum import session as _sess
    out = []
    for soma in _sess._all_soma_dirs(state_dir):
        for p in _sess._iter_paths(soma):
            try:
                rec = _sess._load(p)
            except (OSError, ValueError):
                continue
            if not isinstance(rec, dict):
                continue
            for pe in rec.get("peers") or []:
                if not isinstance(pe, dict) or not str(pe.get("peer", "")).strip():
                    continue
                out.append({
                    "rater": rec.get("cell"), "rater_role": rec.get("role"),
                    "sid": rec.get("sid"), "ended_at": rec.get("ended_at"),
                    "peer_label": str(pe["peer"]).strip(),
                    "strengths": [str(s).strip() for s in (pe.get("strengths") or [])
                                  if str(s).strip()],
                    "frictions": [str(s).strip() for s in (pe.get("frictions") or [])
                                  if str(s).strip()],
                    "would_pair_again": pe.get("would_pair_again"),
                    "role_fit": str(pe.get("role_fit") or "").strip(),
                    "direction": pe.get("direction") or "peer",
                })
    return out


def _peer_key(label: str, declared: set) -> tuple[str, bool]:
    """집계 키: canonical 문법 + 선언 셀 실존일 때만 cell_key로 resolve.
    아니면 라벨 그대로(label-only) — 추측 병합 없음."""
    from organum.state import cell_key, valid_cell_id
    if valid_cell_id(label) and cell_key(label) in declared:
        return cell_key(label), True
    return label, False


def report(state_dir: Path, since_days: float | None = None) -> dict:
    """피어별 집계 + 전역 포화 판정. {peers:[...], entries_n, wpa, wpa_saturated}."""
    from organum import session as _sess
    from organum.state import cell_key, valid_cell_id
    entries = harvest(state_dir)
    if since_days is not None:
        cutoff = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(days=since_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        entries = [e for e in entries if (e.get("ended_at") or "") >= cutoff]
    # 선언측도 canonical 문법 게이트(critic blocker): 손상/legacy 세션의 cell="ENGINE!"이
    # cell_key sanitize("engine")로 정규화돼 정상 라벨을 resolved로 승격하던 것 차단 —
    # ingress(session.start)가 검증해도 디스크 파일은 신뢰 경계 밖이라 read 시 재검증(fail-closed).
    # isinstance 게이트(critic 비차단 hardening): 손상 JSON의 cell이 비-문자열이면
    # valid_cell_id의 re.match가 TypeError — false resolve는 없었지만 읽기 뷰가 죽는다.
    declared = {cell_key(s["cell"]) for s in _sess.sessions_for_join(state_dir)
                if isinstance(s.get("cell"), str) and valid_cell_id(s["cell"])}
    # 전역 포화(§4): 값 분포에서 최대 비율 > 문턱 → 변별 축 아님(R2 실측이 낳은 규칙)
    wpa = {"true": 0, "false": 0, "null": 0}
    for e in entries:
        v = e["would_pair_again"]
        wpa["true" if v is True else "false" if v is False else "null"] += 1
    saturated = bool(entries) and max(wpa.values()) / len(entries) > SATURATION

    groups: dict = {}
    for e in entries:
        key, resolved = _peer_key(e["peer_label"], declared)
        g = groups.setdefault(key, {"peer": key, "resolved": resolved, "labels": set(),
                                    "raters": set(), "entries": []})
        g["labels"].add(e["peer_label"])
        rater = e.get("rater")
        if rater:
            # 같은 결함 계열 방어: invalid rater가 sanitize로 정상 rater 키와 충돌해
            # raters_n(교차-rater 수렴 증거)을 왜곡하지 않게 — valid만 정규화, 아니면 raw.
            # 비-문자열(손상 JSON)은 str()로 계수만 (isinstance 게이트 — TypeError 방지)
            g["raters"].add(cell_key(rater) if isinstance(rater, str) and valid_cell_id(rater)
                            else str(rater))
        g["entries"].append(e)
    peers = []
    for key in sorted(groups, key=lambda k: (-len(groups[k]["entries"]), k)):
        g = groups[key]
        es = g["entries"]
        w = {"true": 0, "false": 0, "null": 0}
        dirs: dict = {}
        for e in es:
            v = e["would_pair_again"]
            w["true" if v is True else "false" if v is False else "null"] += 1
            dirs[e["direction"]] = dirs.get(e["direction"], 0) + 1
        prov = lambda e: {"rater": e.get("rater"), "rater_role": e.get("rater_role"),  # noqa: E731
                          "sid": e.get("sid"), "ended_at": e.get("ended_at"),
                          "direction": e.get("direction")}
        peers.append({
            "peer": key, "resolved": g["resolved"], "labels": sorted(g["labels"]),
            "raters_n": len(g["raters"]), "journals_n": len(es),
            "directions": dirs,
            "strengths_n": sum(len(e["strengths"]) for e in es),
            "frictions_n": sum(len(e["frictions"]) for e in es),
            "would_pair_again": w,
            "role_fit": [{"text": e["role_fit"], **prov(e)} for e in es if e["role_fit"]],
            "strengths": [{"text": t, **prov(e)} for e in es for t in e["strengths"]],
            "frictions": [{"text": t, **prov(e)} for e in es for t in e["frictions"]],
        })
    return {"entries_n": len(entries), "peers": peers,
            "wpa": wpa, "wpa_saturated": saturated}


def _wpa_str(w: dict) -> str:
    return f"T{w['true']}/F{w['false']}/N{w['null']}"


def render(rep: dict, peer: str | None = None) -> str:
    """요약 표(기본) 또는 한 피어 상세(verbatim + provenance)."""
    lines = [f"bench peers — 저널 엔트리 {rep['entries_n']} · 피어 {len(rep['peers'])}"
             f" · would_pair_again {_wpa_str(rep['wpa'])}"
             + (" ⚠포화→보조 플래그(§4: 변별 축 아님)" if rep["wpa_saturated"] else "")]
    if not rep["peers"]:
        lines.append("  (피어저널 없음 — session end --peer-json 으로 시작)")
        return "\n".join(lines)
    if peer is None:
        for p in rep["peers"]:
            tag = "" if p["resolved"] else " [label-only]"
            lab = f" ({', '.join(p['labels'])})" if p["labels"] != [p["peer"]] else ""
            lines.append(f"  {p['peer']}{tag}{lab} — 평가자 {p['raters_n']} · 저널 {p['journals_n']}"
                         f" · 강점 {p['strengths_n']} · 마찰 {p['frictions_n']}"
                         f" · wpa {_wpa_str(p['would_pair_again'])}")
        lines.append("  → 상세: organum bench peers --peer <이름> (verbatim + provenance)")
        lines.append("  (축 코딩 없음 — 서술→축({execution·initiative·…}) 해석은 사람/위임 몫)")
        return "\n".join(lines)
    hit = next((p for p in rep["peers"] if p["peer"] == peer or peer in p["labels"]), None)
    if hit is None:
        lines.append(f"  피어 '{peer}' 없음")
        return "\n".join(lines)
    tag = "resolved" if hit["resolved"] else "label-only"
    lines.append(f"■ {hit['peer']} [{tag}] — 평가자 {hit['raters_n']} · 저널 {hit['journals_n']}"
                 f" · wpa {_wpa_str(hit['would_pair_again'])} · 방향 {hit['directions']}")
    for sec, items in (("role_fit", hit["role_fit"]), ("strengths", hit["strengths"]),
                       ("frictions", hit["frictions"])):
        if not items:
            continue
        lines.append(f"  {sec}:")
        for it in items:
            who = f"{it['rater'] or '?'}·{it['direction']}"
            lines.append(f"    - {it['text']}  ({who}, {it['sid']})")
    return "\n".join(lines)


# ── claim cell 집계 (organ-effect-matrix v0.1.1 §1·§2·§6 — effect_vector 집계기 v0) ──
def cells(state_dir: Path, since_days: float | None = None) -> dict:
    """observation row(passive) → claim cell 집계. 키 = brain × role × problem_type × loadout.

    정직 규율(스키마 문서가 아니라 데이터 모양이 강제):
    - **§6 joint_observed**: worker row는 role이 problem_type을 함의(교란) — 셀이 필드로
      운반하고, role 효과와 problem_type 효과를 분리 주장할 수 없다.
    - **§1 contrast**: 같은 (brain·role·problem_type) 안에 loadout 변주 + graded score가
      있을 때만 파생 가능. 현 RWE row엔 score 원천이 없어 **항상 None** — contrast_status가
      이유를 명시(no-loadout-variation | varied-no-score). 서술 셀은 organ 효과를 주장 못 한다.
    - **cost는 C2**: 측정분만 합산 + 미측정 수 병기. evidence_grade='rwe-observational' 고정.
    - **passive/reported 분리 유지**: reported row도 집계하되 **source가 셀 키의 축** —
      usage 의미가 다른 두 증거(transcript 파생 vs broker 계측)는 같은 셀로 절대 안 섞인다.
      reported의 loadout/problem_type은 declared cell의 선언 세션에서 단일-후보 규율로 조인
      (세션 role ≠ reported role이면 fail-closed로 축 None).
    read-only — 집계가 기록을 남기지 않는다."""
    from organum import session as _sess
    from organum.observatory import _role_of_cell, load, load_reported
    from organum.state import cell_key
    rows = [dict(r, _source="passive") for r in load(state_dir, since_days=since_days)]
    by_cell: dict = {}
    for sess in _sess.sessions_for_join(state_dir):
        c = sess.get("cell")
        if isinstance(c, str) and c:
            by_cell.setdefault(cell_key(c), []).append(sess)
    for r in load_reported(state_dir, since_days=since_days):
        rr = dict(r, _source="reported", loadout=None, problem_type=None)
        d = r.get("declared")
        if isinstance(d, str) and d:
            j = _role_of_cell(by_cell.get(cell_key(d)) or [])
            if j["role"] and r.get("role") and j["role"] == r["role"]:
                rr["loadout"] = j.get("loadout")
                rr["problem_type"] = j.get("problem_type")
        rows.append(rr)
    groups: dict = {}
    for r in rows:
        lo = r.get("loadout")
        key = (r.get("model"), r.get("role"), r.get("problem_type"),
               tuple(sorted(lo)) if isinstance(lo, list) else None, r["_source"])
        groups.setdefault(key, []).append(r)
    out = []
    for key in sorted(groups, key=lambda k: (-len(groups[k]), str(k))):
        model, role, ptype, lo, source = key
        rs = groups[key]
        cost: dict = {}
        for k in ("in_tok", "out_tok", "cache"):
            vals = [r[k] for r in rs if r.get(k) is not None]
            cost[k] = sum(vals) if vals else None
            cost[f"{k}_unmeasured"] = len(rs) - len(vals)
        out.append({
            "brain": model, "role": role, "problem_type": ptype,
            "loadout": list(lo) if lo is not None else None,
            "source": source,
            "n": len(rs),
            # 레거시 row(joint_observed 키 이전)도 worker 세팅 — role 있으면 joint(정직 상수)
            "joint_observed": any(r.get("joint_observed") or r.get("role") for r in rs),
            "declared_cells": sorted({r["declared"] for r in rs if r.get("declared")}),
            "sessions": [r.get("session_id") or r.get("run_id") for r in rs],
            "cost": cost,
            "tools_n": sum(sum((r.get("tools") or {}).values()) for r in rs),
            "files_n": sum(r.get("files_touched") or 0 for r in rs),
            "evidence_grade": "rwe-observational",
        })
    # contrast 파생 자리 — 같은 (brain·role·problem_type) 형제 셀에 loadout 변주가 있는가
    by_brp: dict = {}
    for c in out:  # contrast 형제 = 같은 (brain·role·problem_type·source) — 증거 종류 안 섞음
        by_brp.setdefault((c["brain"], c["role"], c["problem_type"], c["source"]), []).append(c)
    for sibs in by_brp.values():
        varied = len({tuple(c["loadout"]) if c["loadout"] is not None else None
                      for c in sibs}) > 1
        for c in sibs:
            c["contrast"] = None      # graded score 원천 없음 — delta 주장 안 함 (§1 수정 B)
            c["contrast_status"] = "varied-no-score" if varied else "no-loadout-variation"
    return {"cells": out, "rows_n": len(rows)}


def _fmt_mb_tok(v) -> str:
    if v is None:
        return "—"
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v / 1_000:.1f}K"
    return str(v)


def render_cells(rep: dict) -> str:
    lines = [f"bench cells — row {rep['rows_n']} → cell {len(rep['cells'])}"
             " (brain × role × problem_type × loadout · evidence=rwe-observational)"]
    if not rep["cells"]:
        lines.append("  (observation row 없음 — observatory sync 로 시작)")
        return "\n".join(lines)
    for c in rep["cells"]:
        lo = ("bare" if c["loadout"] == [] else
              ",".join(c["loadout"]) if c["loadout"] else "—")
        cells_s = f" · cells {','.join(c['declared_cells'][:3])}" if c["declared_cells"] else ""
        lines.append(f"  [{c['source']}] {c['brain'] or '—'} × {c['role'] or '—'}"
                     f" × {c['problem_type'] or '—'} × [{lo}] — n {c['n']}{cells_s}")
        cost = c["cost"]
        lines.append(f"    cost: in {_fmt_mb_tok(cost['in_tok'])} · out {_fmt_mb_tok(cost['out_tok'])}"
                     f" · cache {_fmt_mb_tok(cost['cache'])} · tools {c['tools_n']}"
                     f" · files {c['files_n']} · contrast {c['contrast_status']}")
    lines.append("  (joint_observed: worker row는 role↔problem_type 교란 — 분리 효과 주장 불가(§6)."
                 " contrast 없는 셀은 서술 셀 — organ 효과 주장 없음(§1))")
    return "\n".join(lines)
