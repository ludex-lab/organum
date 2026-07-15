"""`.organum/` 상태 디렉터리 접근 계층. 스키마의 진실은 docs/format-v0.md."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from organum import FORMAT_VERSION

STATE_DIR_NAME = ".organum"

# docs/format-v0.md §2 — personal/shared 경계의 집행 지점
GITIGNORE_BODY = """\
# organum personal state — do not commit (see docs/format-v0.md §2)
meta.json
self.md
memory/
guard.jsonl
sessions/
cells/
observatory/
archive/
tmp/
# 조율 substrate — 덧없는 멀티-writer 런타임 트래픽 (tmp/과 동류)
relay/
agora/
roster/
alarm/
"""

SELF_MD_TEMPLATE = """\
# {agent} — Self-Understanding
Last reflection: never (trigger: -)

## Patterns
<!-- 반복 관찰된 자기 경향 — 증거 있는 것만. 항목당 한 줄, 끝에 (evidence: ...) -->

## Lessons
<!-- 경험에서 추출된 교훈 -->

## Open questions
<!-- 아직 모르는 것. 비워두는 것도 정보다 -->
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def init_state_dir(project_root: Path, agent: str) -> tuple[Path, dict]:
    """`.organum/` 골격 생성 + 지도 시드 + init 이벤트. 출력은 안 한다 (호출자 몫).

    반환: (state_dir, repo_map). 이미 있으면 FileExistsError.
    """
    from organum import FORMAT_VERSION, __version__

    state_dir = project_root / STATE_DIR_NAME
    if state_dir.exists():
        raise FileExistsError(state_dir)

    (state_dir / "memory").mkdir(parents=True)
    (state_dir / "worldmodel").mkdir()
    (state_dir / "map").mkdir()
    (state_dir / "tmp").mkdir()
    (state_dir / ".gitignore").write_text(GITIGNORE_BODY, encoding="utf-8")
    write_json(state_dir / "meta.json", {
        "format_version": FORMAT_VERSION,
        "organum_version": __version__,
        "created_at": utc_now_iso(),
        "project": project_root.name,
        "agent": agent,
    })
    (state_dir / "self.md").write_text(SELF_MD_TEMPLATE.format(agent=agent), encoding="utf-8")
    (state_dir / "memory" / "events.jsonl").touch()
    (state_dir / "memory" / "memories.jsonl").touch()
    (state_dir / "guard.jsonl").touch()

    repo_map = seed_repo_map(project_root)
    write_json(state_dir / "map" / "repo.map.json", repo_map)
    n_files = sum(1 for n in repo_map["nodes"].values() if n["kind"] == "file")
    append_event(state_dir, "init", f"organum init (map seeded: {n_files} files, {repo_map['seed_source']})")
    return state_dir, repo_map


def find_state_dir(start: Path) -> Path | None:
    """start에서 위로 올라가며 .organum/을 찾는다."""
    for candidate in [start, *start.parents]:
        state = candidate / STATE_DIR_NAME
        if state.is_dir():
            return state
    return None


def require_state_dir(start: Path) -> Path:
    """조율 명령(relay/agora/roster me)용 — 초기화된 .organum/을 위로 찾아 반환.

    없거나 meta.json이 빠진 '반쪽 초기화'면 명확히 실패한다. 조율 명령이 init 없이
    .organum/<field>만 mkdir하면 meta.json 없는 유령 상태가 생겨 context가 깨진다 —
    그 유령을 만들지도, 못 본 척 넘어가지도 않고 여기서 잡는다.
    """
    state_dir = find_state_dir(start)
    if state_dir is None:
        raise SystemExit(
            "organum: 여기서 .organum/이 초기화되지 않았습니다 — 먼저 'organum init'을 실행하세요."
        )
    if not (state_dir / "meta.json").exists():
        raise SystemExit(
            f"organum: {state_dir}이 반쪽 초기화 상태입니다 (meta.json 없음). "
            "'organum init'로 완성하거나 해당 .organum/을 정리하세요."
        )
    return state_dir


def load_meta(state_dir: Path) -> dict:
    return json.loads((state_dir / "meta.json").read_text(encoding="utf-8"))


_CELL_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def _cell_slug(cell_id: str) -> str:
    # strip("-.")로 선행/후행 점을 제거 → "." ".." 같은 경로 traversal id가 루트로 새지 않게
    return _CELL_SLUG_RE.sub("-", cell_id).strip("-.")[:40] or "cell"


def soma_dir(state_dir: Path, cell_id: str | None = None) -> Path:
    """세포의 개인 기관(soma: self·memory·guard)이 사는 디렉터리 (§2.3 soma/commons/field).

    owner(meta.json agent)와 지정 없음은 루트 = v0 단일-세포 호환. 그 외 공존 게스트 세포는
    `cells/<id>/`. commons(map·worldmodel)·field(relay·agora·roster)는 항상 루트 = 공유이므로
    이 함수는 개인 기관에만 쓴다. 한 현장에 여러 세포가 살아도 각자 soma는 single-writer.
    """
    if cell_id:
        try:
            owner = load_meta(state_dir).get("agent")
        except (OSError, ValueError):
            owner = None
        if cell_id != owner:
            return state_dir / "cells" / _cell_slug(cell_id)
    return state_dir


def ensure_soma(state_dir: Path, cell_id: str | None = None) -> Path:
    """soma_dir을 반환하되, 아직 없는 게스트 세포면 개인 기관 골격을 만든다 (쓰기 명령용)."""
    d = soma_dir(state_dir, cell_id)
    if d != state_dir and not (d / "self.md").exists():
        (d / "memory").mkdir(parents=True, exist_ok=True)
        (d / "self.md").write_text(SELF_MD_TEMPLATE.format(agent=cell_id), encoding="utf-8")
        (d / "memory" / "events.jsonl").touch()
        (d / "memory" / "memories.jsonl").touch()
        (d / "guard.jsonl").touch()
    return d


def check_format_version(meta: dict) -> str | None:
    """docs/format-v0.md §9. 반환값은 경고 문자열(있으면), 미래 버전이면 raise."""
    v = meta.get("format_version")
    if v is None or v > FORMAT_VERSION:
        raise SystemExit(
            f"organum: .organum/ format_version={v}는 이 organum(지원 버전 "
            f"{FORMAT_VERSION})보다 새 포맷입니다. organum을 업그레이드하세요."
        )
    if v < FORMAT_VERSION:
        return (
            f"경고: .organum/ 포맷이 구버전입니다 (v{v} < v{FORMAT_VERSION}). "
            "'organum migrate'를 실행하세요."
        )
    return None


def append_event(state_dir: Path, kind: str, content: str, tags: list[str] | None = None) -> None:
    record = {"ts": utc_now_iso(), "kind": kind, "content": content, "tags": tags or []}
    path = state_dir / "memory" / "events.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def git_ls_files(project_root: Path) -> list[str] | None:
    """git 저장소면 파일 목록(추적 + 미추적, ignore 제외), 아니면 None.

    완전 열거가 목적이므로(GIVEN-MAP oracle) 커밋 여부와 무관하게 센다.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard", "-z"],
            cwd=project_root,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return [p for p in out.stdout.decode("utf-8").split("\0") if p]


def seed_repo_map(project_root: Path) -> dict:
    """GIVEN-MAP 시드 (docs/format-v0.md §3.6): 정적 열거로 턴1 완전 지도."""
    files = git_ls_files(project_root)
    seed_source = "git ls-files" if files is not None else "none"
    nodes: dict[str, dict] = {}
    for f in files or []:
        # .organum / .organum.pre-restore-* / .organum.tmp-* — 자기 상태는 지도 밖
        if f.split("/", 1)[0].startswith(STATE_DIR_NAME):
            continue
        parts = f.split("/")
        for i in range(1, len(parts)):
            nodes.setdefault("/".join(parts[:i]) + "/", {"kind": "dir"})
        nodes[f] = {"kind": "file", "status": "unvisited"}
    return {
        "format_version": FORMAT_VERSION,
        "seeded_at": utc_now_iso(),
        "seed_source": seed_source,
        "nodes": dict(sorted(nodes.items())),
        "edges": [],
    }


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_repo_map(state_dir: Path) -> dict | None:
    path = state_dir / "map" / "repo.map.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def git_blob_sha(project_root: Path, rel_path: str) -> str | None:
    """워킹트리 파일의 git blob sha (staleness 대조용, §3.6)."""
    try:
        out = subprocess.run(
            ["git", "hash-object", "--", rel_path],
            cwd=project_root,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.decode("utf-8").strip() or None


def sync_repo_map(project_root: Path, current: dict) -> tuple[dict, dict]:
    """재열거 병합: 신규=unvisited, 삭제 노드 제거, 기존 read 정보 보존."""
    fresh = seed_repo_map(project_root)
    merged: dict[str, dict] = {}
    added = 0
    for path, node in fresh["nodes"].items():
        old = current.get("nodes", {}).get(path)
        if old is not None:
            merged[path] = old
        else:
            merged[path] = node
            if node["kind"] == "file":
                added += 1
    removed = sum(
        1
        for p, n in current.get("nodes", {}).items()
        if p not in fresh["nodes"] and n.get("kind") == "file"
    )
    new_map = {
        **current,
        "seeded_at": fresh["seeded_at"],
        "seed_source": fresh["seed_source"],
        "nodes": merged,
    }
    return new_map, {"added": added, "removed": removed, "total": len(
        [p for p, n in merged.items() if n["kind"] == "file"]
    )}


# --- snapshot/restore (docs/format-v0.md §5) ---


def default_backup_dir(project_root: Path) -> Path:
    digest = hashlib.sha256(str(project_root.resolve()).encode("utf-8")).hexdigest()[:8]
    return Path.home() / ".organum" / "backups" / f"{project_root.name}-{digest}"


def create_backup(state_dir: Path, dest_dir: Path) -> Path:
    """tmp/ 제외 전체를 tar.gz로. 아카이브 루트는 state dir의 내용물 (§5)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    meta = load_meta(state_dir)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = dest_dir / f"organum-{meta.get('project', state_dir.parent.name)}-{ts}.tar.gz"
    with tarfile.open(archive, "x:gz") as tar:
        for child in sorted(state_dir.iterdir()):
            if child.name == "tmp":
                continue
            tar.add(child, arcname=child.name)
    return archive


def read_archive_meta(archive: Path) -> dict:
    with tarfile.open(archive, "r:gz") as tar:
        try:
            member = tar.extractfile("meta.json")
        except KeyError:
            member = None
        if member is None:
            raise SystemExit(
                f"organum: {archive}는 organum 스냅샷이 아닙니다 (meta.json 없음)."
            )
        return json.load(member)


def extract_archive(archive: Path, target: Path) -> None:
    with tarfile.open(archive, "r:gz") as tar:
        try:
            tar.extractall(target, filter="data")
        except TypeError:  # PEP 706 filter 미지원 파이썬 — 수동 경로 검증
            for m in tar.getmembers():
                parts = Path(m.name).parts
                if Path(m.name).is_absolute() or ".." in parts:
                    raise SystemExit(f"organum: 아카이브에 위험한 경로: {m.name}")
            tar.extractall(target)
