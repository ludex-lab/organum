"""guard 저장 경계 (docs/format-v0.md §7).

두 층: §7.1 per-artifact 규칙 (데이터: guard_rules.json) + §7.2 streak window.
모든 영속 쓰기 경로(remember/distill/reflect)는 evaluate()를 통과해야 한다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from organum import state as st

STREAK_N = 5  # §7.2 기본값 (ludex 실측과 동일, 성공 저장 시 리셋)
EXCERPT_LIMIT = 200  # §3.7 — 오염물 전체를 면역 로그에 복제하지 않는다

# 성공 저장으로 간주되어 streak를 리셋하는 이벤트 kind (§7.2)
_SUCCESS_KINDS = {"remember", "distill", "reflect"}


@dataclass
class Verdict:
    decision: str  # "pass" | "blocked" | "flagged"
    rule: str | None = None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.decision != "blocked"


def _load_rules() -> dict:
    raw = resources.files("organum").joinpath("guard_rules.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    for r in data["rules"]:
        flags = re.IGNORECASE | (re.DOTALL if r["where"] == "trailing" else 0)
        r["_re"] = re.compile(r["regex"], flags)
    return data


_RULES = _load_rules()


def evaluate(content: str, *, is_error: bool | None = None) -> Verdict:
    """§7.1 판정. is_error는 CLI 위임 결과의 is_error 필드 (있으면 패턴보다 우선)."""
    if is_error:
        return Verdict("blocked", "error-fallback", "delegation result has is_error=true")
    if not content or not content.strip():
        return Verdict("blocked", "empty-content", "성공의 부재 ≠ 성공")

    stripped = content.strip()
    for r in _RULES["rules"]:
        where, rx = r["where"], r["_re"]
        if where == "leading":
            hit = rx.match(stripped) is not None
        elif where == "trailing":
            hit = rx.search(stripped) is not None and stripped.rstrip().endswith("]")
        else:
            hit = rx.search(stripped) is not None
        if not hit:
            continue
        # 차단은 고정밀 신호만: 앵커·무맥락 토큰. 비앵커 키워드는 표시(flagged) —
        # "에러를 이야기하는 기억"은 파괴하지 않고, "에러 그 자체"는 앵커가 잡는다
        # (rules_version 2: 길이 문턱 폐기 — CJK 편향 + 정당 단문 하드차단 near-miss).
        if r.get("always_block") or where in ("leading", "trailing"):
            return Verdict("blocked", r["id"], f"{where} match: /{r['regex']}/")
        return Verdict("flagged", r["id"], f"keyword mention: /{r['regex']}/")
    return Verdict("pass")


# --- §3.5 형태 계약 검사 (wm-shape) ---

_CLAIM_RE = re.compile(r"^- \[(tentative|confirmed|well-supported)\] .+ \(evidence: .+\)\s*$")
_STRUCT_LINE = re.compile(r"^(#{1,6} |- |\| |<!--)")


def check_wm_shape(text: str) -> list[str]:
    """worldmodel 산출물의 §3.5 위반 목록 (빈 목록 = 통과)."""
    violations: list[str] = []
    lines = text.splitlines()
    # front matter
    fm: dict[str, str] = {}
    body_start = 0
    if lines and lines[0] == "---":
        try:
            end = lines.index("---", 1)
            for ln in lines[1:end]:
                if ":" in ln:
                    k, v = ln.split(":", 1)
                    fm[k.strip()] = v.strip()
            body_start = end + 1
        except ValueError:
            violations.append("front matter 미종결")
    else:
        violations.append("front matter 없음")
    for key in ("organum-format", "domain", "updated"):
        if key not in fm:
            violations.append(f"front matter {key} 결손")

    body = lines[body_start:]
    for sec in ("## Map", "## Frontier", "## Claims"):
        if sec not in body:
            violations.append(f"필수 섹션 결손: {sec}")

    in_claims = False
    for ln in body:
        s = ln.rstrip()
        if not s:
            continue
        if s.startswith("## "):
            in_claims = s == "## Claims"
            continue
        if not _STRUCT_LINE.match(s):
            violations.append(f"산문 문단 (구조 줄 아님): {s[:60]!r}")
        elif in_claims and s.startswith("- ") and not _CLAIM_RE.match(s):
            violations.append(f"Claims 형식 위반 (confidence 태그/evidence 필수): {s[:60]!r}")
    return violations


# --- 기록 + streak (§7.2) ---


def record(state_dir: Path, verdict: Verdict, target: str, content: str) -> None:
    """차단/표시를 guard.jsonl + events.jsonl에 기록 (§3.7). pass는 기록하지 않는다."""
    if verdict.decision == "pass":
        return
    rec = {
        "ts": st.utc_now_iso(),
        "decision": verdict.decision,
        "rule": verdict.rule,
        "target": target,
        "excerpt": content.strip()[:EXCERPT_LIMIT],
    }
    with (state_dir / "guard.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    # streak는 events.jsonl 삽입 순서로 계산된다(§7.2, critic 재감사-2 ⑦: v0 호환+같은 초).
    # blocked=실패, flagged=저장됨(경계). guard.jsonl은 frozen v0 §3.7 그대로 둔다.
    if verdict.decision == "blocked":
        st.append_event(
            state_dir, "guard_block", f"guard blocked ({verdict.rule}) → {target}: {verdict.reason}"
        )
    elif verdict.decision == "flagged":
        st.append_event(
            state_dir, "guard_flagged", f"guard flagged ({verdict.rule}) → {target} (저장됨)"
        )


def record_delegation_failure(state_dir: Path, source: str, reason: str) -> None:
    """위임 실패를 streak 로그(events.jsonl)에만 남긴다 — 저장 경계가 아니므로 guard.jsonl
    (면역 로그, §3.7 target enum)에는 넣지 않는다. blocked 이벤트로 streak에 산입된다."""
    st.append_event(state_dir, "guard_block", f"delegation failed ({source}): {reason}"[:EXCERPT_LIMIT])


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


# streak 경계 = 성공 저장(§_SUCCESS_KINDS) 또는 flagged(저장됨). guard_streak=알림용(미산입).
_STREAK_RESET_KINDS = _SUCCESS_KINDS | {"guard_flagged"}


def streak_count(state_dir: Path) -> int:
    """마지막 성공 저장 이후 꼬리 연속 위임/저장 실패 수 (§7.2). flagged = 저장됨 = 리셋.

    **events.jsonl 삽입 순서만** 본다(timestamp 비교 없음 → 같은 초 뒤집힘 없음; frozen v0
    호환 → guard.jsonl 스키마 불변, legacy 상태도 그대로 읽힌다 — critic 재감사-2 ⑦).
    역순으로 걸으며 guard_block(실패)을 세다가 성공 경계를 만나면 멈춘다. STREAK 알림
    (guard_streak)은 실제 실패가 아니므로 세지도 멈추지도 않는다."""
    count = 0
    for rec in reversed(_read_jsonl(state_dir / "memory" / "events.jsonl")):
        kind = rec.get("kind")
        if kind == "guard_block":
            # legacy 0.1.2가 STREAK 알림도 guard_block으로 남겼다 — 실패가 아니므로 건너뛴다
            # (critic 재감사-3 ②: legacy 상태를 정확히 읽는다). 현행 알림은 guard_streak.
            if str(rec.get("content") or "").startswith("STREAK:"):
                continue
            count += 1
        elif kind in _STREAK_RESET_KINDS:
            break
        # 그 외(note·guard_streak 등)는 실패도 경계도 아님 → 건너뜀
    return count


def streak_active(state_dir: Path, n: int = STREAK_N) -> bool:
    return streak_count(state_dir) >= n


def _streak_already_notified(state_dir: Path) -> bool:
    """이번 streak 구간(마지막 리셋 경계 이후)에 이미 알림을 냈나 — 반복 알림 방지.
    현행 guard_streak + legacy 0.1.2 알림(guard_block/content='STREAK:') 둘 다 인식
    (critic 재감사-4 비차단 후속: legacy 활성 streak 업그레이드 시 중복 알림 방지)."""
    for rec in reversed(_read_jsonl(state_dir / "memory" / "events.jsonl")):
        kind = rec.get("kind")
        if kind == "guard_streak":
            return True
        if kind == "guard_block" and str(rec.get("content") or "").startswith("STREAK:"):
            return True                          # legacy 알림도 '이미 알림함'으로 인정
        if kind in _STREAK_RESET_KINDS:
            break
    return False


def mark_streak_if_reached(state_dir: Path, n: int = STREAK_N) -> bool:
    """저장/위임 실패 기록 직후 호출 — streak 도달 시 알림 이벤트 + True. 조용한 연쇄 실패 금지.

    알림은 events.jsonl의 guard_streak 하나로 일원화(guard.jsonl 마커 폐지 — 그 마커의
    target='delegation'이 frozen v0 §3.7 enum 위반이었다, critic 재감사-3 ①). guard.jsonl은
    이제 저장 경계 결정(blocked|flagged)만 담는다."""
    if streak_count(state_dir) < n:
        return False
    if not _streak_already_notified(state_dir):  # 이번 구간 1회만
        st.append_event(state_dir, "guard_streak",
                        f"STREAK: 연속 {n}회 저장/위임 차단 — window guard 발동")
    return True
