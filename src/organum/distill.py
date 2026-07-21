"""distill — 세션 자료 → worldmodel/<domain>.md (기관 1-3, 포맷 §3.5).

**form>content는 보편 법칙이 아니라 도메인-특정이다.** MUD/탐험 도메인에서 form-shaped 세계모델이
prose 요약을 이긴다(exact-p=.0096, d=1.61). 그러나 이 이득은 코딩 도메인으로 **전이되지 않았다**
(P3 사전등록 null — prose 근소 우위, estimation-only; experiments/p3-pilot/CONFIRMATORY-RESULTS.md).
따라서 form 강제는 **프로파일**이다(보편 강제 아님):
- `map`(라이브러리 API 하위호환 기본; public CLI는 --profile 명시 필수): form-shaped
  (Map/Frontier/Claims) 강제 — 탐험형 도메인에 검증됨.
- `prose`: 산문 세계모델 허용 — 코딩 등 form 이득이 없는(또는 미측정) 도메인에 정직한 선택.

두 프로파일 공통: 위임이 내용을 생성하고, guard가 error-fallback(에러 문자열·auth·빈 산출·
컨텍스트 잔재)을 저장 경계에서 거른다(§7). map 프로파일만 추가로 wm-shape를 검증한다.
**주의: prompt injection은 guard가 막지 못한다 — 알려진 gap**(guard_rules에 injection 규칙 없음,
injection 샘플은 통과; tests/fixtures/injection_samples.jsonl). 저장된 산출물이 이후 context로
주입되는 persistent-injection 경계는 별도 threat model이다. 인과 효능 주장은 프로파일-특정이지
도구의 보편 전제가 아니다(critic 감사 수용, 2026-07-16).
"""

from __future__ import annotations

import re
from pathlib import Path

from organum import FORMAT_VERSION, delegate, guard
from organum import state as st

PROFILES = ("map", "prose")
DEFAULT_PROFILE = "map"

MAP_SYSTEM_PROMPT = """\
당신은 세계모델을 엄격한 '형태'로 산출한다. 이 도메인은 form-shaped 프로파일이다 — 산문 문단 금지.
허용 섹션은 정확히 셋, 이 순서로:

## Map
구조 줄만. 형식: "- <영역>: <연결>→<대상> · <연결>→?"  ("?" = 미탐험)

## Frontier
지금 바로 시도 가능한 행동 항목만. "- <행동>"

## Claims
"- [tentative|confirmed|well-supported] <주장> (evidence: <근거>)"
모든 항목에 confidence 태그와 (evidence: ...)가 필수다.

규칙:
- 산문 문단 절대 금지. 모든 줄은 위 세 형식 중 하나.
- 판별 기준: "계획 없이 다음 행동으로 컴파일되는가?" — 다단계 서술은 Claims의 조건부 주장 +
  Frontier의 첫 행동으로 분해하라.
- 출력은 '## Map'부터 시작한다. front matter나 '# WM:' 제목은 쓰지 마라 (organum이 붙인다).
"""

PROSE_SYSTEM_PROMPT = """\
당신은 이 도메인의 세계모델을 산출한다. 이 도메인은 prose 프로파일이다 — 서술형 요약이 허용된다.
목표는 다음 세션이 빠르게 맥락을 회복하는 것: 어떻게 돌아가는지, 무엇을 확인했고 무엇이 아직
불확실한지, 어디를 아직 안 가봤는지. 구조가 도움이 되면 소제목·목록을 자유로이 써도 된다.
근거 없는 단정보다 관찰의 신뢰도를 함께 적어라. front matter나 '# WM:' 제목은 쓰지 마라
(organum이 붙인다)."""

SYSTEM_PROMPT = MAP_SYSTEM_PROMPT  # 하위호환 별칭


def build_user_prompt(domain: str, material: str, prior: str | None,
                      profile: str = DEFAULT_PROFILE) -> str:
    parts = [f"도메인: {domain}", ""]
    if prior:
        parts += ["현재 세계모델 (이것을 갱신하되 유효한 것은 보존):", prior, ""]
    tail = ("위 자료로 세계모델 본문(## Map / ## Frontier / ## Claims)을 산출하라."
            if profile == "map"
            else "위 자료로 세계모델 본문(서술형)을 산출하라.")
    parts += ["자료:", material, "", tail]
    return "\n".join(parts)


def assemble(domain: str, body: str) -> str:
    """front matter + 제목 + 본문 → 완전한 worldmodel 파일 (§3.5). LLM이 넣은 제목/FM은 벗긴다."""
    lines = body.strip().splitlines()
    if lines and lines[0].strip() == "---":
        try:
            lines = lines[lines.index("---", 1) + 1:]
        except ValueError:
            pass
    lines = [l for l in lines if not l.lstrip().startswith("# WM:")]
    body = "\n".join(lines).strip()
    fm = (
        f"---\norganum-format: {FORMAT_VERSION}\ndomain: {domain}\n"
        f"updated: {st.utc_now_iso()}\n---\n"
    )
    return f"{fm}# WM: {domain}\n\n{body}\n"


class DistillError(SystemExit):
    pass


# 불변조건 ③(critic 감사): 모든 파일 목적지는 허용된 루트(worldmodel/) 안에만 —
# domain은 슬러그다. 경로 구분자·점 선행은 traversal 벡터라 여기서 거부한다.
_DOMAIN_RE = re.compile(r"[0-9A-Za-z가-힣][0-9A-Za-z가-힣._-]{0,59}\Z")


def validate_domain(domain: str) -> str:
    d = (domain or "").strip()
    if not _DOMAIN_RE.match(d) or "/" in d or "\\" in d or ".." in d:
        raise DistillError(
            f"organum: --domain {domain!r}은 슬러그가 아닙니다 (영숫자·한글·._- 만, "
            "경로 문자 불가 — worldmodel/ 밖으로 나갈 수 없습니다).")
    return d


def distill(
    state_dir: Path,
    domain: str,
    material: str,
    *,
    profile: str = DEFAULT_PROFILE,
    generate=None,
    model: str | None = None,
    max_budget_usd: float = 1.0,
    override_streak: bool = False,
) -> dict:
    domain = validate_domain(domain)
    if profile not in PROFILES:
        raise DistillError(f"organum: 미지 프로파일 {profile!r} (허용: {', '.join(PROFILES)})")
    wm_path = state_dir / "worldmodel" / f"{domain}.md"
    prior = wm_path.read_text(encoding="utf-8") if wm_path.is_file() else None
    user_prompt = build_user_prompt(domain, material, prior, profile)
    system_prompt = MAP_SYSTEM_PROMPT if profile == "map" else PROSE_SYSTEM_PROMPT

    gen = generate
    if gen is None:
        def gen(system, user):
            return delegate.delegate(
                user, state_dir=state_dir, system_prompt=system, model=model,
                max_budget_usd=max_budget_usd, allowed_tools=[],  # 순수 생성 — 도구 불필요
                override_streak=override_streak,
            )

    result = gen(system_prompt, user_prompt)
    if not result.ok:
        raise DistillError(f"organum: distill 위임 실패 ({result.subtype}): {result.error}")

    body = result.text
    # 1. 저장 경계 — 위임 결과가 에러 문자열일 수 있다 (§7.1). 두 프로파일 공통.
    v = guard.evaluate(body)
    if not v.ok:
        guard.record(state_dir, v, "worldmodel", body)
        guard.mark_streak_if_reached(state_dir)
        raise DistillError(f"organum: distill 산출물 guard 차단 ({v.rule}) — {v.reason}")

    # 2. 형태 계약 (§3.5) — **map 프로파일만**. form>content는 도메인-특정(P3 null)이라
    #    prose 프로파일에는 강제하지 않는다(critic 감사 수용).
    assembled = assemble(domain, body)
    if profile == "map":
        violations = guard.check_wm_shape(assembled)
        if violations:
            guard.record(
                state_dir, guard.Verdict("blocked", "wm-shape", "; ".join(violations[:3])),
                "worldmodel", body,
            )
            raise DistillError(
                "organum: distill 산출물이 형태 계약 위반 (§3.5, map 프로파일) — prose가 필요하면 "
                "--profile prose:\n  " + "\n  ".join(violations[:5])
            )

    wm_path.write_text(assembled, encoding="utf-8")
    st.append_event(  # 이 distill 이벤트가 streak 리셋 경계다(§7.2). 프로파일 기록(critic D)
        state_dir, "distill",
        f"distill → worldmodel/{domain}.md ({profile}, cost {result.cost_usd})",
        tags=[f"place:worldmodel/{domain}.md", f"profile:{profile}"],
    )
    return {"domain": domain, "path": str(wm_path), "cost_usd": result.cost_usd,
            "profile": profile,
            "billing": result.billing}  # 과금 경로를 최종 출력까지 운반 (불변조건 ⑥)
