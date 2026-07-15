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
    if verdict.decision == "blocked":
        st.append_event(
            state_dir, "guard_block", f"guard blocked ({verdict.rule}) → {target}: {verdict.reason}"
        )


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


def streak_count(state_dir: Path) -> int:
    """마지막 성공 저장 이후 꼬리 연속 blocked 수 (§7.2). flagged = 저장됨 = 리셋."""
    events = _read_jsonl(state_dir / "memory" / "events.jsonl")
    last_success = max(
        (e["ts"] for e in events if e.get("kind") in _SUCCESS_KINDS), default=""
    )
    count = 0
    for rec in reversed(_read_jsonl(state_dir / "guard.jsonl")):
        if rec.get("rule") == "streak":  # streak 마커 자신은 세지 않는다
            continue
        if rec.get("decision") != "blocked" or rec.get("ts", "") <= last_success:
            break
        count += 1
    return count


def streak_active(state_dir: Path, n: int = STREAK_N) -> bool:
    return streak_count(state_dir) >= n


def mark_streak_if_reached(state_dir: Path, n: int = STREAK_N) -> bool:
    """blocked 기록 직후 호출 — streak 도달 시 마커 기록 + True. 조용한 연쇄 실패 금지."""
    if streak_count(state_dir) != n:  # 정확히 도달한 순간만 마커 (반복 마커 방지)
        return streak_count(state_dir) > n
    rec = {
        "ts": st.utc_now_iso(),
        "decision": "blocked",
        "rule": "streak",
        "target": "delegation",
        "excerpt": f"연속 blocked {n}회 — 호스트 outage 가능성. distill/reflect는 --override 필요.",
    }
    with (state_dir / "guard.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    st.append_event(state_dir, "guard_block", f"STREAK: 연속 {n}회 저장 차단 — window guard 발동")
    return True
