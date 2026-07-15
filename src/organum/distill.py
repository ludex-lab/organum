"""distill — 세션 자료 → worldmodel/<domain>.md (기관 1-3, 포맷 §3.5).

form>content (exact-p=.0096, d=1.61): prose 요약은 소비되지 않는다. distill의 유일한 임무는
'형태를 강제하는 것'이다 — 내용은 위임(delegate)이 생성하되, organum이 그 산출물을 wm-shape
guard로 검증해 prose를 거부한다. LLM이 형태를 어기면 저장하지 않는다. 이것이 organum이
'상태와 규율의 도구'인 지점: 생성이 아니라 형태의 보증.
"""

from __future__ import annotations

from pathlib import Path

from organum import FORMAT_VERSION, delegate, guard
from organum import state as st

SYSTEM_PROMPT = """\
당신은 세계모델을 엄격한 '형태'로 산출한다. 산문 요약은 금지 — 측정 결과 소비되지 않는다.
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


def build_user_prompt(domain: str, material: str, prior: str | None) -> str:
    parts = [f"도메인: {domain}", ""]
    if prior:
        parts += ["현재 세계모델 (이것을 갱신하되 유효한 것은 보존):", prior, ""]
    parts += [
        "자료:", material, "",
        "위 자료로 세계모델 본문(## Map / ## Frontier / ## Claims)을 산출하라.",
    ]
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


def distill(
    state_dir: Path,
    domain: str,
    material: str,
    *,
    generate=None,
    model: str | None = None,
    max_budget_usd: float = 1.0,
    override_streak: bool = False,
) -> dict:
    wm_path = state_dir / "worldmodel" / f"{domain}.md"
    prior = wm_path.read_text(encoding="utf-8") if wm_path.is_file() else None
    user_prompt = build_user_prompt(domain, material, prior)

    gen = generate
    if gen is None:
        def gen(system, user):
            return delegate.delegate(
                user, state_dir=state_dir, system_prompt=system, model=model,
                max_budget_usd=max_budget_usd, allowed_tools=[],  # 순수 생성 — 도구 불필요
                override_streak=override_streak,
            )

    result = gen(SYSTEM_PROMPT, user_prompt)
    if not result.ok:
        raise DistillError(f"organum: distill 위임 실패 ({result.subtype}): {result.error}")

    body = result.text
    # 1. 저장 경계 — 위임 결과가 에러 문자열일 수 있다 (§7.1)
    v = guard.evaluate(body)
    if not v.ok:
        guard.record(state_dir, v, "worldmodel", body)
        guard.mark_streak_if_reached(state_dir)
        raise DistillError(f"organum: distill 산출물 guard 차단 ({v.rule}) — {v.reason}")

    # 2. 형태 계약 (§3.5) — prose면 거부, 저장 안 함. distill의 존재 이유.
    assembled = assemble(domain, body)
    violations = guard.check_wm_shape(assembled)
    if violations:
        guard.record(
            state_dir, guard.Verdict("blocked", "wm-shape", "; ".join(violations[:3])),
            "worldmodel", body,
        )
        raise DistillError(
            "organum: distill 산출물이 형태 계약 위반 (§3.5) — prose는 저장하지 않는다:\n  "
            + "\n  ".join(violations[:5])
        )

    wm_path.write_text(assembled, encoding="utf-8")
    st.append_event(
        state_dir, "distill", f"distill → worldmodel/{domain}.md (cost {result.cost_usd})",
        tags=[f"place:worldmodel/{domain}.md"],
    )
    return {"domain": domain, "path": str(wm_path), "cost_usd": result.cost_usd}
