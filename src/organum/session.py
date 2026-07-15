"""session — 세션 라이프사이클 봉투 (state/discipline, dispatch 아님).

organum은 세션을 *시작·지휘*하지 않는다. 세포가 스스로 의도를 선언(start)하고, 진행을
기록(note)하고, 닫으며 회고(end)하는 규율의 골격만 제공한다. roster me가 presence를
'배정 아닌 서술'로 두는 것과 같은 원리 — 역할은 자기-선언한 헌장이지 organum의 명령이 아니다.

저장 (docs/format-v0.md §2.3 soma/commons/field):
- 세션 레코드·피어 노트 = 세포 **soma**(cells/<slug>/sessions/, single-writer). 피어 노트는
  *작성자* 관점(provenance=작성자)이라 대상 세포 기억엔 절대 쓰지 않는다.
- 역할 헌장 템플릿 = **commons**(state_dir/roles/<role>.md) override, 없으면 기본 헌장.
- format-v0 additive — sessions/·roles/ 신규, 기존 loci 불변.

organum은 세션을 능동적으로 끊거나 재촉하지 않는다. status·idle은 pull(관제탑 표시)이지 push가
아니다. 회고·피어노트는 세포가 친 것을 캡처할 뿐, organum이 생성(LLM 호출)하지 않는다.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from organum import state as st

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


class SessionError(Exception):
    """세션 규율 위반 (열린 세션 중복·빈 의도·잘못된 피어 노트 등)."""


# 기본 역할 헌장 — dry·짧게. commons(state_dir/roles/<slug>.md)로 현장별 override 가능.
ROLE_CHARTERS: dict[str, str] = {
    "engine": (
        "# engine — 구현을 끄는 손\n"
        "- 스펙을 돌아가는 코드로. 테스트 통과가 ground truth.\n"
        "- read-before-write · 레인 준수 · 작은 커밋.\n"
        "- **리드다: 통합을 드라이브하고 넛지를 기다리지 마라. 소비 shape·계약 결정을 주도한다.**\n"
        "- 막히면 agora에 신호 — 혼자 오래 끌지 말 것.\n"
    ),
    "reviewer": (
        "# reviewer — 코드 사이의 눈\n"
        "- 두 빌더 코드 *사이*의 seam 버그를 노린다 (혼자서는 못 보는 것).\n"
        "- **먼저 사냥해라: ship·커밋에 반응해 seam을 대조하라. 물어보길 기다리지 마라. 발견은 owner에게 relay.**\n"
        "- 스펙 경계·회귀 확인. null은 null이라 적는다.\n"
        "- 남의 커밋엔 provenance 존중 — 덮어쓰지 않음.\n"
    ),
    "atelier": (
        "# atelier — 수렴시키는 자리\n"
        "- 흩어진 산출을 하나로 아귀 맞춘다.\n"
        "- 데이터·콘텐츠만 소유(로직·함수 아님). 타입은 계약대로 정밀히.\n"
        "- 충돌은 join·etiquette로 — 지휘가 아니라 조율.\n"
    ),
    "scribe": (
        "# scribe — 기록하는 손\n"
        "- 무슨 일이 있었는지 dry하게 남긴다 (recall·회고용).\n"
        "- 해석 최소, 사실 우선.\n"
        "- 계약 변경은 즉시 문서에 반영(지연 금지). 주어진 교정은 그대로 반영·재논쟁 금지.\n"
    ),
    "facilitator": (
        "# facilitator — 흐름을 트는 자리\n"
        "- 막힌 데를 풀고 다음 비트로 넘긴다 — 대신 결정하지 않음.\n"
        "- 조율자이지 관제사가 아니다.\n"
    ),
    # chief = advisory 관제사-*cell*이지 organum이 관제사가 되는 게 아니다. dispatch·강제 권한 없음 —
    # 넛지·PAUSE 권고·human 에스컬레이트까지. 강제(중단·override)는 언제나 human.
    "chief": (
        "# chief — 세션의 눈 (전담 모니터 · 빌드 레인 없음)\n"
        "- 전담 관찰자다: 코드를 쓰지 않는다. 레인·커밋 없음 — 관측·조언·에스컬레이션이 산출물.\n"
        "- 주기 스윕: agora 새 글 · relay · roster · 세션 status · git log를 훑어 이상을 찾는다\n"
        "  (정지 셀 · 응답 없는 핸드오프 · 미검증 seam · 반복 spiral · 규율 위반).\n"
        "- **개입 사다리 — 항상 가장 가벼운 것부터**: ① 재확인(단일 관찰로 결론 금지) →\n"
        "  ② 넛지(해당 셀에 relay 1통 — 무엇이 이상하고 무엇을 확인해 달라, 구체적으로) →\n"
        "  ③ PAUSE 경보(해로운 방향일 때 `organum alarm sound --level pause --to <셀|all> '<사유>'`\n"
        "     — 발동은 chief/human만, 권고지 강제 아님) →\n"
        "  ④ human 에스컬레이트(강제 중단·권한·판단이 필요한 것은 `--escalate` 편지로).\n"
        "- 금지: 배정·dispatch·코드 수정·남의 레인 커밋·다른 셀 override. 관제사가 아니라 눈이다.\n"
        "- 세션 끝: *모든* 셀의 피어저널을 남긴다 — 전체를 본 유일한 관측자(whole-view).\n"
        "- 소규모 세션(셀 ≤2)엔 전담 chief가 과잉일 수 있다 — human이 직접 이 역할을 해도 된다.\n"
    ),
}


def _sessions_dir(soma: Path) -> Path:
    return soma / "sessions"


def _iter_paths(soma: Path) -> list[Path]:
    d = _sessions_dir(soma)
    return sorted(d.glob("*.json")) if d.exists() else []


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path: Path, rec: dict) -> None:
    path.write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, _TS_FMT).replace(tzinfo=timezone.utc)


def _open(soma: Path) -> tuple[Path | None, dict | None]:
    """열린(ended_at 없는) 세션의 (경로, 레코드). 없으면 (None, None). 규율상 최대 1개."""
    for p in _iter_paths(soma):
        rec = _load(p)
        if rec.get("ended_at") is None:
            return p, rec
    return None, None


def _new_sid(soma: Path) -> str:
    base = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    d = _sessions_dir(soma)
    sid, n = base, 1
    while (d / f"{sid}.json").exists():  # 같은 초 충돌 방어 (희귀)
        n += 1
        sid = f"{base}-{n}"
    return sid


# 모든 역할 공통 조율 규율 — warren Round Two 도그푸드 학습 반영. 모든 charter에 자동 append.
COORDINATION_DISCIPLINE = """
## 조율 규율 (모든 역할 공통)
- **제안은 1회, ACK를 기다려라.** 응답 없다고 같은 걸 재전송하지 마라(agora 스팸 방지).
- **조율은 agora로.** 사람에게 "뭘 할까요?" 묻지 말고 동료와 agora에서 합의해라. 사람은 최종 개입자지 디스패처가 아니다.
- **자기 레인만 쓰고, 착륙하면 즉시 커밋해라.** 남의 레인·문서는 관측만.
- **주어진 교정은 그대로 반영하고 재논쟁하지 마라.** 모호하면 1회 묻고 멈춰라 — 없는 문제를 지어내지 마라.
- **git history를 절대 rewrite하지 마라**(amend·reset·revert). 공유 레포에서 남의 커밋을 파괴한다.
- **경보(alarm)를 존중해라.** 작업 사이사이 `organum alarm active`를 확인하고, human/chief의 pause 경보(전체 또는 너 지정)가 활성이면 진행 중인 원자 작업만 마치고 멈춘 뒤 ACK해라. 동의하지 않으면 사유를 1회 회신하고 human 판단을 기다려라 — 무시하고 계속하지 마라.
- **human의 개입·권한이 필요한 것은 `--escalate` 편지로 표면화해라**(관제탑에 뜬다). 조용히 멈춰서 사람 지시를 기다리지 마라.
"""


def resolve_charter(state_dir: Path, role: str) -> str:
    """역할 헌장: commons override(roles/<slug>.md) → 기본 헌장 → 최소 스텁. 공통 조율 규율을 append."""
    slug = st._cell_slug(role)
    override = state_dir / "roles" / f"{slug}.md"
    if override.exists():
        base = override.read_text(encoding="utf-8")
    else:
        base = ROLE_CHARTERS.get(
            role,
            f"# {role}\n- (역할 헌장 미정 — session note로 채우거나 roles/{slug}.md 추가)\n",
        )
    return base.rstrip() + "\n" + COORDINATION_DISCIPLINE


def start(soma: Path, cell: str, role: str, intent: str, charter: str) -> dict:
    """세션 선언. 이미 열린 세션이 있으면 거부 (한 세포 = 한 열린 세션 = 규율)."""
    _, r_open = _open(soma)
    if r_open is not None:
        raise SessionError(
            f"이미 열린 세션 {r_open['sid']} (역할 {r_open.get('role')}) — 먼저 'organum session end'."
        )
    if not intent.strip():
        raise SessionError("세션 의도(--intent)가 비었습니다 — 왜 이 세션인지 한 줄.")
    _sessions_dir(soma).mkdir(parents=True, exist_ok=True)
    sid = _new_sid(soma)
    rec = {
        "sid": sid,
        "cell": cell,
        "role": role,
        "intent": intent.strip(),
        "charter": charter,
        "started_at": st.utc_now_iso(),
        "ended_at": None,
        "notes": [],
        "shipped": [],
        "peers": [],
        "format": 0,
    }
    _save(_sessions_dir(soma) / f"{sid}.json", rec)
    st.append_event(soma, "session_start", f"[{role}] {intent.strip()}", tags=["session", role])
    return rec


def note(soma: Path, text: str) -> dict:
    """진행 비트 append (자기 규율 체크포인트)."""
    p, rec = _open(soma)
    if rec is None:
        raise SessionError("열린 세션이 없습니다 — 'organum session start' 먼저.")
    if not text.strip():
        raise SessionError("빈 노트입니다.")
    rec["notes"].append({"ts": st.utc_now_iso(), "text": text.strip()})
    _save(p, rec)
    return rec


def status(soma: Path) -> dict | None:
    """열린 세션의 서술적 상태 (read-only pull). 없으면 None."""
    p, rec = _open(soma)
    if rec is None:
        return None
    now = datetime.now(timezone.utc)
    last = rec["notes"][-1]["ts"] if rec["notes"] else rec["started_at"]
    return {
        "sid": rec["sid"],
        "role": rec.get("role"),
        "intent": rec["intent"],
        "notes": len(rec["notes"]),
        "age_min": int((now - _parse_ts(rec["started_at"])).total_seconds() // 60),
        "idle_min": int((now - _parse_ts(last)).total_seconds() // 60),
        "started_at": rec["started_at"],
    }


def _all_soma_dirs(state_dir: Path) -> list[Path]:
    """owner(루트) + 공존 게스트(cells/*) soma 디렉터리 — 사이트 전역 세션 스캔용."""
    dirs = [state_dir]
    cells = state_dir / "cells"
    if cells.exists():
        dirs += [d for d in sorted(cells.iterdir()) if d.is_dir()]
    return dirs


def open_sessions(state_dir: Path) -> list[dict]:
    """사이트의 모든 열린 세션 (관제탑 read-only 뷰용). idle 짧은 순 = 최근 활동 순."""
    now = datetime.now(timezone.utc)
    out = []
    for soma in _all_soma_dirs(state_dir):
        for p in _iter_paths(soma):
            rec = _load(p)
            if rec.get("ended_at") is not None:
                continue
            last = rec["notes"][-1]["ts"] if rec["notes"] else rec["started_at"]
            out.append({
                "sid": rec["sid"], "cell": rec.get("cell"), "role": rec.get("role"),
                "intent": rec["intent"], "beats": len(rec["notes"]),
                "age_min": int((now - _parse_ts(rec["started_at"])).total_seconds() // 60),
                "idle_min": int((now - _parse_ts(last)).total_seconds() // 60),
            })
    out.sort(key=lambda s: s["idle_min"])
    return out


def recent_retros(state_dir: Path, limit: int = 3) -> list[dict]:
    """최근 닫힌 세션 회고 요약 (관제탑 strip). 최신순."""
    closed = []
    for soma in _all_soma_dirs(state_dir):
        for p in _iter_paths(soma):
            rec = _load(p)
            if rec.get("ended_at") is None:
                continue
            closed.append({
                "sid": rec["sid"], "cell": rec.get("cell"), "role": rec.get("role"),
                "intent": rec["intent"], "shipped": len(rec.get("shipped", [])),
                "peers": len(rec.get("peers", [])), "ended_at": rec["ended_at"],
                "duration_min": rec.get("duration_min", 0),
            })
    closed.sort(key=lambda s: s["ended_at"], reverse=True)
    return closed[:limit]


def _validate_peer(obj: dict) -> dict:
    """피어 노트 정규화: peer·strengths[]·frictions[]·would_pair_again·role_fit
    [+direction: peer(기본)·upward(셀→chief 상향)·downward(chief→셀 whole-view)]."""
    if not isinstance(obj, dict):
        raise SessionError("피어 노트는 JSON 객체여야 합니다.")
    peer = str(obj.get("peer", "")).strip()
    if not peer:
        raise SessionError("피어 노트에 'peer'(누구/역할)가 필요합니다.")
    direction = str(obj.get("direction", "") or "peer").strip()
    if direction not in ("peer", "upward", "downward"):
        raise SessionError("direction은 peer(동료)·upward(→chief)·downward(chief→셀) 중 하나입니다.")

    def _as_list(v) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v.strip()] if v.strip() else []
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        raise SessionError("strengths·frictions는 문자열 또는 문자열 배열이어야 합니다.")

    pair = obj.get("would_pair_again")
    if isinstance(pair, str):
        pair = pair.strip().lower() in ("y", "yes", "true", "1")
    elif pair is not None:
        pair = bool(pair)
    return {
        "peer": peer,
        "strengths": _as_list(obj.get("strengths")),
        "frictions": _as_list(obj.get("frictions")),
        "would_pair_again": pair,
        "role_fit": str(obj.get("role_fit", "")).strip(),
        "direction": direction,
    }


def end(soma: Path, shipped: list[str] | None = None, peers: list[dict] | None = None) -> dict:
    """세션 닫기 — 출하물·피어 저널 기록, 소요시간 산출. 피어 노트는 작성자 soma에만 산다."""
    p, rec = _open(soma)
    if rec is None:
        raise SessionError("열린 세션이 없습니다.")
    rec["shipped"] = [s.strip() for s in (shipped or []) if s.strip()]
    rec["peers"] = [_validate_peer(x) for x in (peers or [])]
    rec["ended_at"] = st.utc_now_iso()
    rec["duration_min"] = int(
        (_parse_ts(rec["ended_at"]) - _parse_ts(rec["started_at"])).total_seconds() // 60
    )
    _save(p, rec)
    summary = (
        f"[{rec.get('role')}] {rec['intent']} · {len(rec['notes'])} beats · "
        f"{len(rec['shipped'])} shipped · {len(rec['peers'])} peer-notes · {rec['duration_min']}m"
    )
    st.append_event(soma, "session_end", summary, tags=["session", rec.get("role") or ""])
    return rec
