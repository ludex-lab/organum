"""checkup — 상태 건강 점검 (기관 1-8). 판정만 하고 고치지 않는다 (진단이 처방보다 먼저)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from organum import FORMAT_VERSION, guard
from organum import state as st

BACKUP_WARN_DAYS = 7
SELF_SECTION_LIMIT = 12  # §3.2 항목 상한
TENTATIVE_DECAY_DAYS = 30  # 구조적 망각: 이 기간 미강화된 tentative 기억은 decay 후보 (advisory)
STALE_SESSION_MIN = 120  # 이 이상 idle인 열린 세션 = end 없이 죽은 셀 후보 (advisory)

OK, WARN, ERROR = "ok", "warn", "error"


def _parse_ts(ts: str | None):
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _jsonl_integrity(path: Path) -> tuple[int, int]:
    if not path.is_file():
        return 0, 0
    good = bad = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            json.loads(line)
            good += 1
        except json.JSONDecodeError:
            bad += 1
    return good, bad


def run(state_dir: Path) -> list[tuple[str, str]]:
    """(level, message) 목록. ERROR가 하나라도 있으면 CLI는 비-0 종료."""
    project_root = state_dir.parent
    findings: list[tuple[str, str]] = []

    # 0. streak — 최상단 (§7.2: 조용한 연쇄 실패 금지)
    n = guard.streak_count(state_dir)
    if guard.streak_active(state_dir):
        findings.append((ERROR, f"STREAK 발동 중: 연속 {n}회 저장 차단 — 호스트/설정 점검 전 위임 금지"))
    elif n > 0:
        findings.append((WARN, f"최근 연속 차단 {n}회 (streak 문턱 {guard.STREAK_N})"))
    else:
        findings.append((OK, "guard streak 없음"))

    # 1. 포맷 버전
    meta = st.load_meta(state_dir)
    v = meta.get("format_version")
    if v == FORMAT_VERSION:
        findings.append((OK, f"format v{v} (동결 v0)"))
    else:
        findings.append((ERROR, f"format_version {v} ≠ 지원 {FORMAT_VERSION} — migrate 필요"))

    # 2. JSONL 무결성
    for name, rel in (("events", "memory/events.jsonl"), ("memories", "memory/memories.jsonl"),
                      ("guard", "guard.jsonl")):
        good, bad = _jsonl_integrity(state_dir / rel)
        if bad:
            findings.append((ERROR, f"{name}: 파싱 불가 레코드 {bad}건 (정상 {good})"))
        else:
            findings.append((OK, f"{name}: {good} 레코드 무결"))

    # 3. map — 존재 · sync 필요 · sha drift
    repo_map = st.load_repo_map(state_dir)
    if repo_map is None:
        findings.append((WARN, "map/repo.map.json 없음 — organum init이 시드했어야 함"))
    else:
        _, stats = st.sync_repo_map(project_root, repo_map)
        if stats["added"] or stats["removed"]:
            findings.append(
                (WARN, f"map 열거 불일치: 신규 {stats['added']} · 소실 {stats['removed']} → 'organum map sync'")
            )
        else:
            findings.append((OK, f"map 열거 일치 ({stats['total']} files)"))
        drift = []
        for path, node in repo_map.get("nodes", {}).items():
            if node.get("status") == "read" and node.get("sha"):
                cur = st.git_blob_sha(project_root, path)
                if cur and cur != node["sha"]:
                    drift.append(path)
        if drift:
            findings.append((WARN, f"read 마킹 후 변경된 파일 {len(drift)}: " + " · ".join(drift[:5])))
        else:
            findings.append((OK, "read 노드 sha drift 없음"))

    # 4. self.md 섹션 상한 (§3.2)
    self_path = state_dir / "self.md"
    if self_path.is_file():
        section, count, over = None, 0, []
        for line in self_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("## "):
                section, count = line[3:].strip(), 0
            elif line.startswith("- ") and section:
                count += 1
                if count == SELF_SECTION_LIMIT + 1:
                    over.append(section)
        if over:
            findings.append((WARN, f"self.md 섹션 상한({SELF_SECTION_LIMIT}) 초과: {', '.join(over)} — 통합 먼저"))
        else:
            findings.append((OK, "self.md 섹션 상한 내"))
    else:
        findings.append((ERROR, "self.md 없음"))

    # 5. 백업 최근성 (1-8: 백업 = 회복력)
    events = guard._read_jsonl(state_dir / "memory" / "events.jsonl")
    last_backup = max((e["ts"] for e in events if e.get("kind") == "backup"), default=None)
    if last_backup is None:
        findings.append((WARN, "백업 이벤트 없음 — 'organum backup' (day 1 기능)"))
    else:
        age = datetime.now(timezone.utc) - datetime.strptime(last_backup, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        if age > timedelta(days=BACKUP_WARN_DAYS):
            findings.append((WARN, f"마지막 백업이 {age.days}일 전 ({last_backup})"))
        else:
            findings.append((OK, f"마지막 백업 {last_backup}"))

    # 6. 카드 staleness (§8 — MTI cross-test 3케이스: e8c73a9)
    agents_dir = state_dir / "agents"
    if agents_dir.is_dir():
        agent_model = meta.get("agent_model")
        for card_path in sorted(agents_dir.glob("*.card.json")):
            try:
                card = json.loads(card_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                findings.append((ERROR, f"카드 파싱 불가: {card_path.name}"))
                continue
            fmt = card.get("card_format")
            if fmt != "mti.card/v0":
                continue  # unknown → ignore, no error (§8)
            if not card.get("provenance"):
                findings.append((WARN, f"{card_path.name}: provenance 없음 — invalid, 무시됨"))
                continue
            brain = card.get("brain_model")
            # TIER-SPLIT 안전: 정확-매칭이라 벤더가 family를 티어로 쪼개도(gpt-5.5→gpt-5.6-solar)
            # brain≠agent_model → STALE(재측정)로 기운다. silent-current 불가 (ludex 항체 2026-07-10).
            # ⚠ family/fuzzy 매칭으로 "개선" 금지 — 그 순간 silent-current 취약점이 생긴다.
            if agent_model is None:
                findings.append((WARN, f"{card_path.name}: agent_model 미설정 — staleness 판정 불가 (soft-warn)"))
            elif brain != agent_model:  # 양끝 동일 술어: brain_model ≠ agent_model
                findings.append((WARN, f"{card_path.name}: STALE — 측정 brain {brain} ≠ 현재 {agent_model}"))
            else:
                findings.append((OK, f"{card_path.name}: fresh ({brain})"))

    # 7. 기억 decay — stale tentative (구조적 망각, SAGE/Ebbinghaus-lite). 자기개선 안전:
    #    자기 궤적 학습의 오류 누적을 막는 위생. **advisory만 — 자동 삭제 없음** (판정≠처방;
    #    caretaker가 reflect/consolidate로 승격·강등 결정). 동결 포맷 불변 (confidence·ts 읽기만).
    mems = guard._read_jsonl(state_dir / "memory" / "memories.jsonl")
    superseded = {m["supersedes"] for m in mems if m.get("supersedes")}
    cutoff = datetime.now(timezone.utc) - timedelta(days=TENTATIVE_DECAY_DAYS)
    stale_tentative = [
        m for m in mems
        if m.get("confidence") == "tentative"
        and m.get("id") not in superseded
        and (_parse_ts(m.get("ts")) or datetime.now(timezone.utc)) < cutoff
    ]
    if stale_tentative:
        findings.append((WARN,
            f"stale tentative 기억 {len(stale_tentative)}개 (>{TENTATIVE_DECAY_DAYS}일 미강화·미대체) "
            "— reflect/consolidate로 승격·통합·강등 검토 (구조적 망각, advisory)"))
    elif mems:
        findings.append((OK, "stale tentative 기억 없음"))

    # 8. 열린 세션 staleness — session end 없이 조용히 죽은 셀 흔적 (warren Round Two 갭).
    #    advisory만 — organum이 세션을 닫거나 재촉하지 않는다 (판정≠처방; 닫기는 그 셀/human).
    from organum import session as _session
    open_s = _session.open_sessions(state_dir)
    stale_s = [s for s in open_s if s["idle_min"] >= STALE_SESSION_MIN]
    if stale_s:
        detail = " · ".join(
            f"[{s.get('role') or '—'}] {s.get('cell') or '?'} idle {s['idle_min']}m" for s in stale_s[:5])
        findings.append((WARN,
            f"stale 열린 세션 {len(stale_s)}개 (idle ≥{STALE_SESSION_MIN}분): {detail} "
            "— 셀이 end 없이 죽었나 확인, 'organum session end --for <id>'로 회고 마감 (advisory)"))
    elif open_s:
        findings.append((OK, f"열린 세션 {len(open_s)}개 모두 활성 (idle <{STALE_SESSION_MIN}분)"))
    return findings


def render(findings: list[tuple[str, str]]) -> tuple[str, bool]:
    icon = {OK: "✓", WARN: "⚠", ERROR: "✗"}
    lines = [f"{icon[lvl]} {msg}" for lvl, msg in findings]
    n_err = sum(1 for lvl, _ in findings if lvl == ERROR)
    n_warn = sum(1 for lvl, _ in findings if lvl == WARN)
    lines.append(f"— checkup: {len(findings)} 항목 · 경고 {n_warn} · 오류 {n_err}")
    return "\n".join(lines), n_err > 0
