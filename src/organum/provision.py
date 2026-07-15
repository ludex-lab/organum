"""provision — 변환된 Agent Skill에 organum 조직을 배선 (cargo-bridge, experiments 졸업).

    Skills = 운송 (agentskills.io 2025-12-18, 이식 가능·무상태)
    organum = 화물 (지속하는 조직 — 스펙이 비운 statefulness를 채운다)

Skill이 SKILL.md `metadata.organum-requires`로 조직 의존성을 선언하면(스펙의 sanctioned 확장
훅), provision이 읽어 annex를 배선한다. 프리즈된 v0 포맷은 안 건드린다 — provision은 기존
조직을 오케스트레이션할 뿐, 스키마를 바꾸지 않는다. 감사는 §3.3의 열린 kind 'provision'으로 기록.

보안 (Anthropic "trusted sources only" + 면역 규율 1-5): provision은 공격면을 넓히므로 경계에서
감사한다. 두 통제의 역할이 다르다 (Ludex 정렬 2026-07-05, 두 랩 실측):
- **주 통제 = trusted-SOURCES-only.** 신뢰는 skill 파일이 아니라 **외부 레지스트리**에서 온다.
  frontmatter의 `ludex_source`는 provenance *주장*이지 검증된 신뢰가 아니다 (자기선언은 스푸핑
  가능). audit는 주장을 레지스트리에 대조한다 — organum은 읽기만, 판정 안 함. 레지스트리
  미도착 시 아무것도 미검증 → fail-closed(운영자 --trust 없으면 거부).
- **보조(심층방어) = 스크립트 스캔.** curl/base64/annex-반출 같은 명백한 exfil 패턴만.
  **injection(명령 override) 탐지기가 아니다** — 두 랩 실측 결과 content-scan은 injection을
  못 잡는다("Ignore all previous instructions…"가 통과). injection 규칙 클래스는 다음 fixture
  라운드 후보(실측 후 규칙, L1과 같은 규율). 그래서 주 통제가 source-trust인 것이다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from organum import state as st

# organ 이름 → 그 조직을 제공하는 organum 명령 (allowlist = blast-radius 한계)
KNOWN_ORGANS = {
    "memory": ["remember", "recall"],
    "map": ["map"],
    "worldmodel": ["distill"],
}
# 심층방어 exfil 스캔 (주 통제 아님 — injection은 못 잡는다, 위 docstring 참고)
SUSPICIOUS = [
    (r"\bcurl\b|\bwget\b", "외부 네트워크 fetch"),
    (r"/dev/tcp/", "raw 소켓 유출 경로"),
    (r"\bnc\b\s|\bncat\b", "netcat"),
    (r"base64\s+-d|base64\s+--decode", "난독화 페이로드 디코드"),
    (r"ANTHROPIC_API_KEY|AWS_SECRET|_TOKEN\b", "자격증명 참조"),
    (r"\.organum/.*\|", "annex 내용 파이프 반출"),
    (r"\beval\b", "동적 실행"),
]


class ProvisionError(SystemExit):
    pass


@dataclass
class Audit:
    findings: list[str] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.refusals


def parse_frontmatter(skill_md: Path) -> dict:
    """SKILL.md YAML frontmatter 최소 파서 (top-level + metadata 1-레벨 중첩)."""
    lines = skill_md.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        raise ProvisionError(f"organum: {skill_md}에 frontmatter 없음.")
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise ProvisionError(f"organum: {skill_md} frontmatter 미종결.")
    fm: dict = {}
    cur: dict | None = None
    for raw in lines[1:end]:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        key, _, val = raw.strip().partition(":")
        key, val = key.strip(), val.strip()
        if raw[0] in " \t" and cur is not None:
            cur[key] = val
        elif val == "":
            cur = {}
            fm[key] = cur
        else:
            fm[key] = val
            cur = None
    return fm


def source_claim(meta: dict) -> str:
    """skill이 실어온 provenance 주장 (ludex 운송 측 스탬프 `ludex_source`). '주장'이지 '신뢰' 아님."""
    return meta.get("ludex_source") or meta.get("organum-source-trust") or ""


def is_trusted(claim: str, registry) -> bool:
    """신뢰 판정 — **skill 파일이 아니라 외부 레지스트리에서** (Ludex 정렬 2026-07-05).

    자기선언은 스푸핑 가능하므로 신뢰 근거로 쓰지 않는다. 레지스트리는 Ludex가 export하는
    read-only 신뢰 집합(미도착 시 빈 집합 = 아무것도 미검증). organum은 대조만, 판정 안 함.
    """
    return bool(claim) and claim in (registry or frozenset())


def audit_skill(skill_dir: Path, fm: dict, *, trust_override: bool, registry=None) -> Audit:
    a = Audit()
    meta = fm.get("metadata") or {}

    claim = source_claim(meta) or "없음"
    if is_trusted(source_claim(meta), registry):
        a.findings.append(f"신뢰 출처 (레지스트리 검증): {claim}")
    elif trust_override:
        a.findings.append(f"운영자 --trust 오버라이드 — provenance 주장 {claim!r} 미검증 (레지스트리 없음)")
    else:
        a.refusals.append(
            f"provenance 주장 {claim!r} 자기선언·미검증 — 운영자 --trust 필요 "
            "(자기선언 ≠ 신뢰, 스푸핑 가능; 신뢰는 외부 크리처 레지스트리에서 온다)")

    required = (meta.get("organum-requires") or "").split()
    if not required:
        a.refusals.append("organum-requires 선언 없음")
    for organ in required:
        if organ not in KNOWN_ORGANS:
            a.refusals.append(f"미지 조직 요구: {organ!r} (allowlist: {sorted(KNOWN_ORGANS)})")

    scripts = sorted((skill_dir / "scripts").glob("*")) if (skill_dir / "scripts").is_dir() else []
    for s in scripts:
        if not s.is_file():
            continue
        body = s.read_text(encoding="utf-8", errors="replace")
        for pat, label in SUSPICIOUS:
            if re.search(pat, body, re.IGNORECASE):
                a.refusals.append(f"스크립트 {s.name}: 의심 패턴 [{label}]")
    a.findings.append(f"스크립트 {len(scripts)}개 스캔")
    return a


def _log_provision(state_dir: Path, name: str, audit: Audit, decision: str) -> None:
    detail = "; ".join(audit.findings + [f"REFUSE:{r}" for r in audit.refusals])[:400]
    st.append_event(state_dir, "provision", f"provision {decision} '{name}': {detail}",
                    tags=["field:provision", f"src:skill:{name}"])


def provision(skill_dir: Path, workdir: Path, *, trust_override: bool = False, registry=None) -> dict:
    fm = parse_frontmatter(skill_dir / "SKILL.md")
    name = fm.get("name", skill_dir.name)
    meta = fm.get("metadata") or {}
    required = (meta.get("organum-requires") or "").split()

    audit = audit_skill(skill_dir, fm, trust_override=trust_override, registry=registry)

    # 이미 annex가 있으면 거부도 면역 이벤트로 기록 (없으면 기록할 유기체가 없음 — caller가 받음)
    existing = st.find_state_dir(workdir)
    if not audit.ok:
        if existing is not None:
            _log_provision(existing, name, audit, "refused")
        raise ProvisionError(
            f"organum: '{name}' provision 거부 — 감사 실패:\n  "
            + "\n  ".join(audit.refusals)
        )

    # organum CLI가 조직을 실제로 제공하는지 (자기 명령 표면으로 검증)
    known_cmds = build_parser_choices()
    for organ in required:
        missing = [c for c in KNOWN_ORGANS[organ] if c not in known_cmds]
        if missing:
            raise ProvisionError(f"organum: 조직 '{organ}' 배선 실패 — 명령 결손 {missing}.")

    state_dir = existing if existing is not None else st.init_state_dir(workdir, name)[0]
    _log_provision(state_dir, name, audit, "provisioned")
    return {"skill": name, "annex": str(state_dir), "wired": required,
            "tools": fm.get("allowed-tools", ""), "audit": audit.findings}


def build_parser_choices() -> set[str]:
    """organum이 노출하는 서브명령 집합 (조직 배선 검증용)."""
    from organum.cli import build_parser
    parser = build_parser()
    for action in parser._actions:
        if hasattr(action, "choices") and action.choices:
            return set(action.choices)
    return set()
