"""migrate — 포맷 버전 마이그레이션 (포맷 §10). 동결 이후 스키마 변경의 유일 경로.

정책: 도구는 지원 버전 N(=FORMAT_VERSION)을 안다. 디렉터리 버전 V에 대해 —
  V == N: 할 일 없음.  V > N: 거부(organum 업그레이드).  V < N: 자동 backup → 순차 변환.
현재 v0가 유일 버전이라 _MIGRATIONS는 비어 있다 (실제 v1 도입 시 채워진다 — dead code 금지).
self-versioned 파일 3종(meta/repo.map/worldmodel front matter)은 항상 같은 값으로 이동한다(§4).
"""

from __future__ import annotations

import json
from pathlib import Path

from organum import FORMAT_VERSION
from organum import state as st

# 버전 V → V+1 변환 함수(state_dir를 제자리 변환). 실제 v1 도입 전까지 비어 있음.
_MIGRATIONS: dict[int, "callable"] = {}


class MigrateError(SystemExit):
    pass


def plan(current: int, target: int, registry: dict) -> list[int]:
    """current→target 순차 스텝 목록. 중간에 빠진 스텝이 있으면 raise."""
    steps = []
    v = current
    while v < target:
        if v not in registry:
            raise MigrateError(
                f"organum: v{v}→v{v + 1} 마이그레이션 경로가 없습니다 "
                f"(도구가 v{target}를 지원한다 주장하나 스텝 결손) — organum 버그."
            )
        steps.append(v)
        v += 1
    return steps


def _bump_self_versioned(state_dir: Path, target: int) -> None:
    """meta / repo.map / worldmodel front matter의 버전 필드를 target으로 동기 갱신 (§4)."""
    mp = state_dir / "meta.json"
    meta = json.loads(mp.read_text(encoding="utf-8"))
    meta["format_version"] = target
    st.write_json(mp, meta)

    rm = state_dir / "map" / "repo.map.json"
    if rm.is_file():
        m = json.loads(rm.read_text(encoding="utf-8"))
        m["format_version"] = target
        st.write_json(rm, m)

    wm_dir = state_dir / "worldmodel"
    if wm_dir.is_dir():
        for wm in wm_dir.glob("*.md"):
            lines = wm.read_text(encoding="utf-8").splitlines()
            if lines and lines[0] == "---":
                try:
                    end = lines.index("---", 1)
                except ValueError:
                    continue
                for i in range(1, end):
                    if lines[i].startswith("organum-format:"):
                        lines[i] = f"organum-format: {target}"
                wm.write_text("\n".join(lines) + "\n", encoding="utf-8")


def migrate(
    state_dir: Path,
    *,
    target_version: int = FORMAT_VERSION,
    registry: dict | None = None,
    backup_dir: Path | None = None,
) -> dict:
    registry = _MIGRATIONS if registry is None else registry
    meta = st.load_meta(state_dir)
    current = meta.get("format_version")
    if current is None:
        raise MigrateError("organum: meta.json에 format_version이 없습니다 — 손상된 상태.")

    if current == target_version:
        return {"status": "current", "version": current}
    if current > target_version:
        raise MigrateError(
            f"organum: 디렉터리 v{current} > 지원 v{target_version} — organum을 업그레이드하세요 "
            "(구버전 도구로 미래 포맷을 변환하지 않는다)."
        )

    steps = plan(current, target_version, registry)  # 결손 스텝이면 여기서 거부

    # 자동 backup 먼저 (§5, 1-8: 백업 = 회복력). 변환 전 안전망.
    dest = backup_dir if backup_dir is not None else st.default_backup_dir(state_dir.parent)
    archive = st.create_backup(state_dir, dest)

    for v in steps:
        registry[v](state_dir)  # v→v+1 제자리 변환
    _bump_self_versioned(state_dir, target_version)

    st.append_event(
        state_dir, "migrate",
        f"migrate v{current} → v{target_version} (backup: {archive})",
        tags=["field:migrate"],
    )
    return {"status": "migrated", "from": current, "to": target_version, "backup": str(archive)}
