"""reflect — 세션 회고 → self.md carry-forward (기관 1-7, 포맷 §3.2).

organum은 회고 '내용'을 만들지 않는다 (위임 모듈 또는 에이전트의 몫). reflect가 강제하는
것은 carry-forward 규율: 섹션 단위 갱신, 전면 재작성 금지, 섹션당 항목 상한(12), guard 경유.
두 실패 모드 모두 차단 — 무한 성장(상한), 전면 리셋(기존 항목은 resolve로만 제거).
"""

from __future__ import annotations

from pathlib import Path

from organum import guard
from organum import state as st

SECTIONS = ("Patterns", "Lessons", "Open questions")
SECTION_LIMIT = 12  # §3.2


class ReflectError(SystemExit):
    pass


def _parse(text: str):
    """self.md → (title, head_lines, {section: {'comments': [...], 'items': [...]}}, order)."""
    title = ""
    head: list[str] = []
    sections: dict[str, dict] = {}
    order: list[str] = []
    cur: str | None = None
    for ln in text.splitlines():
        if ln.startswith("# ") and not ln.startswith("## "):
            title = ln
        elif ln.startswith("## "):
            cur = ln[3:].strip()
            sections[cur] = {"comments": [], "items": []}
            order.append(cur)
        elif cur is None:
            head.append(ln)
        else:
            s = ln.strip()
            if s.startswith("- "):
                sections[cur]["items"].append(s)
            elif s.startswith("<!--"):
                sections[cur]["comments"].append(ln)
            # 그 외(빈 줄 등)는 렌더에서 정규화하며 버린다
    return title, head, sections, order


def _render(title: str, head: list[str], sections: dict, order: list[str]) -> str:
    out = [title]
    out.extend(h for h in head if h.strip() or True)  # head(=Last reflection 등) 보존
    # head 끝의 잉여 빈 줄 정리 후 단일 빈 줄
    while out and out[-1].strip() == "":
        out.pop()
    for name in order:
        out.append("")
        out.append(f"## {name}")
        out.extend(sections[name]["comments"])
        out.extend(sections[name]["items"])
    return "\n".join(out) + "\n"


def apply(
    state_dir: Path,
    *,
    patterns: list[str] | None = None,
    lessons: list[str] | None = None,
    questions: list[str] | None = None,
    resolve: list[str] | None = None,
    trigger: str | None = None,
) -> dict:
    self_path = state_dir / "self.md"
    if not self_path.is_file():
        raise ReflectError("organum: self.md가 없습니다.")
    title, head, sections, order = _parse(self_path.read_text(encoding="utf-8"))
    for name in SECTIONS:
        sections.setdefault(name, {"comments": [], "items": []})
        if name not in order:
            order.append(name)

    additions = {
        "Patterns": list(patterns or []),
        "Lessons": list(lessons or []),
        "Open questions": list(questions or []),
    }

    # 1. guard — 추가 항목 전부 저장 경계 통과 (all-or-nothing)
    for items in additions.values():
        for item in items:
            v = guard.evaluate(item)
            if not v.ok:
                guard.record(state_dir, v, "self", item)
                guard.mark_streak_if_reached(state_dir)
                raise ReflectError(f"organum: reflect guard 차단 ({v.rule}) — {v.reason}: {item[:60]!r}")

    # 2. resolve — Open questions에서 매치 제거 (evidence-bearing 제거의 유일 경로)
    resolved = []
    unmatched = []
    for r in resolve or []:
        oq = sections["Open questions"]["items"]
        hit = [i for i in oq if r in i]
        if not hit:
            unmatched.append(r)
            continue
        sections["Open questions"]["items"] = [i for i in oq if r not in i]
        resolved.extend(hit)
    if unmatched:
        raise ReflectError(
            "organum: resolve 매치 없음 (기존 Open questions와 불일치): " + " · ".join(unmatched)
        )

    # 3. carry-forward 추가 + 섹션 상한 검사
    for name, items in additions.items():
        new_items = sections[name]["items"] + [f"- {i}" for i in items]
        if len(new_items) > SECTION_LIMIT:
            raise ReflectError(
                f"organum: '{name}' 섹션 상한({SECTION_LIMIT}) 초과 "
                f"({len(sections[name]['items'])}→{len(new_items)}) — 통합(consolidate) 먼저."
            )
        sections[name]["items"] = new_items

    # 4. Last reflection 갱신
    trig = trigger or "-"
    head = [
        f"Last reflection: {st.utc_now_iso()} (trigger: {trig})"
        if h.startswith("Last reflection:")
        else h
        for h in head
    ]

    self_path.write_text(_render(title, head, sections, order), encoding="utf-8")
    n_added = sum(len(v) for v in additions.values())
    st.append_event(
        state_dir, "reflect",
        f"reflect: +{n_added} 항목 · resolve {len(resolved)} (trigger: {trig})",
        tags=["field:reflect"],
    )
    return {"added": n_added, "resolved": len(resolved), "trigger": trig}
