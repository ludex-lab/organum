"""위임 모듈 — LLM이 필요한 작업(distill/reflect 내용 생성)을 사용자의 CLI에 서브프로세스로.

organum은 자체 LLM 오케스트레이션을 하지 않는다 (경계 규율). 이 모듈은 사용자가 이미 쓰는
CLI(claude 등)를 호출하는 얇은 층이다. 검증된 레시피: docs/spike-recheck-2026-07-04.md.

안전 다중:
1. streak 게이트 — guard streak 활성 시 위임 거부 (호스트 outage 중 쿼타 소모 방지, §7.2)
2. is_error 선분기 — budget abort는 result 키 자체가 없다. .result 접근 전 is_error 확인
3. 예산 캡 — --max-budget-usd 하드 핀 (콜드캐시 기준: spike-recheck §3)
4. auth 경로 보존 — organum은 ANTHROPIC_API_KEY를 주입하지 않는다 (과금 경로 변경 사고 방지;
   '격리'가 아니다 — 사용자 env는 그대로 전달되고, organum은 자격·예산·실행권을 *빌릴* 뿐이다);
   과금 tier를 결과에 노출해 조용한 metered 전환을 막는다 (기관 1-5).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from organum import guard

DEFAULT_CLI = "claude"
# 콜드캐시 기준 최소 캡 (spike-recheck §3: 콜드 $0.12 vs 웜 $0.055 — 캡은 콜드 기준).
MIN_BUDGET_USD = 0.25


class StreakBlocked(Exception):
    """guard streak 활성 중 위임 시도 — 호스트 outage 가능성 (§7.2)."""


@dataclass
class DelegationResult:
    ok: bool
    text: str = ""
    error: str | None = None
    subtype: str | None = None
    cost_usd: float | None = None
    num_turns: int | None = None
    billing: str | None = None
    raw: dict = field(default_factory=dict)


def billing_tier(env: dict | None = None) -> str:
    """과금 경로. ANTHROPIC_API_KEY 존재만으로 구독→metered로 바뀌는 사고를 노출한다."""
    env = os.environ if env is None else env
    return "metered" if env.get("ANTHROPIC_API_KEY") else "subscription/oauth"


def _subprocess_env() -> dict:
    """auth 경로 보존(격리 아님): 사용자 환경을 그대로 전달하되 자격/과금 변수를 주입하지 않는다.

    --bare를 쓰지 않으므로 사용자의 OAuth/구독 경로가 그대로 유지된다 (spike gotcha 3).
    """
    return dict(os.environ)


def build_cmd(
    cli: str,
    budget: float,
    *,
    model: str | None = None,
    system_prompt: str | None = None,
    allowed_tools: list[str] | None = None,
) -> list[str]:
    cmd = [
        cli, "-p",
        "--output-format", "json",
        "--no-session-persistence",
        "--max-budget-usd", f"{budget:g}",
    ]
    if model:
        cmd += ["--model", model]
    if system_prompt:
        cmd += ["--append-system-prompt", system_prompt]
    if allowed_tools is not None:
        cmd += ["--allowed-tools", " ".join(allowed_tools)]
    return cmd


def parse_result(stdout: str, returncode: int = 0) -> DelegationResult:
    """CLI JSON 출력 → DelegationResult. **is_error를 .result보다 먼저 본다** — 계약이다.

    budget abort(error_max_budget_usd)는 result 키 자체가 없다 (spike-recheck §2).
    """
    try:
        d = json.loads(stdout)
    except json.JSONDecodeError:
        return DelegationResult(
            ok=False, error=f"위임 출력 JSON 파싱 실패 (rc={returncode})",
            raw={"stdout": stdout[:500]},
        )
    if not isinstance(d, dict):
        return DelegationResult(ok=False, error="위임 출력이 오브젝트가 아님", raw={"value": d})

    if d.get("is_error"):
        errs = d.get("errors") or []
        return DelegationResult(
            ok=False,
            error="; ".join(errs) if errs else (d.get("subtype") or "unknown error"),
            subtype=d.get("subtype"),
            cost_usd=d.get("total_cost_usd"),
            raw=d,
        )
    return DelegationResult(
        ok=True,
        text=d.get("result", ""),
        subtype=d.get("subtype"),
        cost_usd=d.get("total_cost_usd"),
        num_turns=d.get("num_turns"),
        raw=d,
    )


def delegate(
    prompt: str,
    *,
    state_dir: Path | None = None,
    cli: str = DEFAULT_CLI,
    model: str | None = None,
    system_prompt: str | None = None,
    max_budget_usd: float = 1.0,
    allowed_tools: list[str] | None = None,
    override_streak: bool = False,
    timeout: int = 300,
) -> DelegationResult:
    """사용자 CLI로 프롬프트를 위임. 프롬프트는 stdin으로 (spike gotcha 2: escape 안전)."""
    if state_dir is not None and not override_streak and guard.streak_active(state_dir):
        raise StreakBlocked(
            f"guard streak 활성 (연속 {guard.streak_count(state_dir)}회 차단) — "
            "호스트/설정 점검 전 위임 거부. override_streak로 강제."
        )
    budget = max(max_budget_usd, MIN_BUDGET_USD)
    if budget != max_budget_usd:  # 조용한 바닥은 표시 정직성 위반(불변조건 ⑥) — 올리더라도 시끄럽게
        print(f"organum delegate: 예산 ${max_budget_usd:g} < 최소 ${MIN_BUDGET_USD:g} — "
              f"${budget:g}로 상향 (콜드캐시 1회 호출 보호선, spike-recheck §3)", file=sys.stderr)
    cmd = build_cmd(
        cli, budget, model=model, system_prompt=system_prompt, allowed_tools=allowed_tools
    )
    billing = billing_tier()

    def _fail(res: DelegationResult) -> DelegationResult:
        # 불변조건 ⑦: 실제 delegation 실패가 guard streak까지 이어져야 반복 실패가
        # (timeout·budget·CLI 부재) 조용히 연쇄하지 않는다 — 성공 저장(distill 등)이 리셋.
        # events.jsonl에만 남긴다(위임 실패는 저장 경계가 아니라 guard.jsonl §3.7 대상 아님).
        if state_dir is not None:
            guard.record_delegation_failure(
                state_dir, f"{cli}", f"{res.subtype or 'error'}: {res.error or ''}")
            guard.mark_streak_if_reached(state_dir)
        return res

    try:
        proc = subprocess.run(
            cmd, input=prompt.encode("utf-8"),
            capture_output=True, timeout=timeout, env=_subprocess_env(),
        )
    except FileNotFoundError:
        return _fail(DelegationResult(ok=False, error=f"CLI '{cli}' 없음 — cli 인자로 지정하세요.",
                                      subtype="cli-missing", billing=billing))
    except subprocess.TimeoutExpired:
        return _fail(DelegationResult(ok=False, error=f"위임 타임아웃 ({timeout}s)",
                                      subtype="timeout", billing=billing))

    result = parse_result(proc.stdout.decode("utf-8", "replace"), proc.returncode)
    result.billing = billing
    return result if result.ok else _fail(result)
