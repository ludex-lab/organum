"""organum integrity — core/canonical 산출물의 무결성 감시 (memory-surveillance v0, git-추적 tier).

에세이 "sediment": append-only ≠ memory; 코드엔 verifier가 있는데 memory엔 없다. **Memory Injection** =
core 산출물을 고의 오염하면 검증 없는 disk를 진실로 읽는 후속 에이전트에게 명령이 된다. 방어의
**core-integrity tier**: 전부가 아니라 **정의된 core만** 무결성+authorization(injection은 core를 노리지
sediment를 안 노림). 설계: docs/memory-surveillance-v0.md.

**v0 = git-추적 core**: git이 무결성·저자·bless(=commit)·tamper-evidence를 공짜로 준다. 검사규칙 =
"core 변경이 저자와 함께 commit됐나(blessed), 아니면 unblessed로 떠 있나(누구도 아직 답 안 함)".

**정직 경계(과대주장 금지)**: 탐지지 예방·판결 아님(한 머신 같은 UID라 '누가'를 증명 못 함,
anomaly-for-review) · hard-enforce 아님(raw shell 못 막음, 탐지+규율) · baseline=git(tamper-evident) ·
사고성 관측(북마크·헌법 역행) vs exploit은 threat-model. 비-git core(soma)는 v0.1.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

MANIFEST = "core-manifest.json"   # .organum/core-manifest.json (사용자 큐레이트 = answerable 소유자)
# 자동 core (선언 불요, project-root 상대): commons 지식베이스(.organum/ 아래) + 프로젝트 헌법/지침.
# map(.organum/map)은 derived(재생성)라 core 아님 — hand-crafted canonical만.
AUTO_CORE = (".organum/worldmodel", ".organum/roles", "CONTRACT.md", "AGENTS.md", "CLAUDE.md")


def _git(project_root: Path, args: list[str]) -> str | None:
    """git 서브프로세스 (state.py 패턴 재사용). 실패=None."""
    try:
        out = subprocess.run(["git", *args], cwd=project_root, capture_output=True, check=True)
        return out.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.CalledProcessError):
        return None


def is_git_repo(project_root: Path) -> bool:
    return _git(project_root, ["rev-parse", "--is-inside-work-tree"]) is not None


def _tracked(project_root: Path, path: str) -> bool:
    return _git(project_root, ["ls-files", "--error-unmatch", "--", path]) is not None


def _contained(project_root: Path, rel: str) -> bool:
    """resolve 기반 containment — 상대 탈출·절대·**symlink escape**(project 밖)를 차단(critic B5).
    lexical `..`만이 아니라 resolved 경로가 project canonical root 안이어야."""
    if not rel or rel.startswith("/") or ".." in rel.split("/"):
        return False
    try:
        target = (project_root / rel).resolve()   # symlink 해소
        target.relative_to(project_root.resolve())
        return True
    except (ValueError, OSError, RuntimeError):
        return False


# 허용된 core status domain — classifier와 log parser가 **공유**(critic 재감사2 B5-b: 미지 status 차단)
VALID_STATUS = frozenset({
    "blessed", "unblessed", "unprotected", "missing", "unsupported", "scan-error", "no-git"})


def _manifest_items(state_dir: Path):
    """core-manifest → (items:[(path, authority)], ok:bool). **단일 검증**(critic 재감사2 B5-a:
    core_paths·manifest_ok가 path 계약에서 갈라지지 않게). ok=False = 파싱/shape/path 손상(선언이
    조용히 탈락 → 소비자가 complete를 주장하면 안 됨). path는 trim non-empty + `_contained`(탈출·symlink escape 차단)."""
    mf = state_dir / MANIFEST
    if not mf.is_file():
        return [], True
    project = state_dir.parent
    try:
        data = json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return [], False
    if not isinstance(data, dict):
        return [], False
    core = data.get("core")
    if core is None:
        return [], True                     # core 키 없음 = 빈 선언(정상)
    if not isinstance(core, list):
        return [], False
    items, ok = [], True
    for it in core:
        if not isinstance(it, dict):
            ok = False
            continue
        path = it.get("path")
        if not isinstance(path, str) or not path.strip() or not _contained(project, path.strip()):
            ok = False                       # 빈/절대/`..`/symlink escape → drop + 손상 표면화
            continue
        items.append((path.strip(), str(it.get("authority") or "declared")))
    return items, ok


def manifest_ok(state_dir: Path) -> bool:
    """core-manifest가 없거나 모든 선언 item이 유효(파싱·shape·path 계약)하면 True.
    손상이 선언 core를 조용히 탈락시키면 **False**(critic B5). core_paths와 동일 검증 공유."""
    return _manifest_items(state_dir)[1]


def core_paths(state_dir: Path) -> dict:
    """core 산출물 — AUTO_CORE + core-manifest 선언. path → {authority, source}.
    **삭제 탐지(critic B1)**: 선언 core와 git-추적 auto core는 **현재 없어도** inventory에 남겨
    (`exists()`가 삭제를 숨기지 않게). manifest 검증은 `_manifest_items`로 단일화(B5-a)."""
    project = state_dir.parent
    git = is_git_repo(project)
    out: dict[str, dict] = {}
    for p in AUTO_CORE:  # 존재하거나 git-추적(삭제됐어도 추적분은 남겨 missing으로)
        if (project / p).exists() or (git and _tracked(project, p)):
            out[p] = {"authority": "commons", "source": "auto"}
    for path, authority in _manifest_items(state_dir)[0]:
        out[path] = {"authority": authority, "source": "manifest"}
    return out


def _last_commit(project_root: Path, path: str) -> dict | None:
    log = _git(project_root, ["log", "-1", "--format=%h%x00%an%x00%aI", "--", path])
    if log and log.strip():
        parts = log.strip().split("\0")
        if len(parts) == 3:
            return {"rev": parts[0], "author": parts[1], "date": parts[2]}
    return None


def classify(project_root: Path, path: str) -> dict:
    """git 관점 core 상태 (bless=commit) — **의심스러우면 fail-closed**(critic B1: false-clean 금지):
    - **blessed**: 추적·클린(regular file, committed) — 마지막 bless=git author.
    - **unblessed**: 추적·워킹트리 수정(uncommitted).
    - **unprotected**: 미추적(존재) · assume-unchanged/skip-worktree(git 힌트를 신뢰로 안 씀) ·
      디렉터리에 untracked/ignored 자식(git 밖 내용 유입).
    - **missing**: 선언 core인데 디스크에 없음(삭제=변경).
    - **unsupported**: symlink(v0 안전 검증 불가) — blessed 아님.
    - **scan-error**: git 명령 실패(clean과 구분).
    - **no-git**: git 저장소 아님."""
    if not is_git_repo(project_root):
        return {"status": "no-git", "last_commit": None}
    fp = project_root / path
    if fp.is_symlink():  # symlink target 변경은 blob 불변 → blessed로 새면 안 됨 → fail-closed
        return {"status": "unsupported", "last_commit": None}
    if not _tracked(project_root, path):
        return {"status": ("missing" if not fp.exists() else "unprotected"), "last_commit": None}
    last = _last_commit(project_root, path)
    v = _git(project_root, ["ls-files", "-v", "--", path])  # assume-unchanged(소문자)·skip-worktree('S')
    if v:
        tags = {ln[0] for ln in v.splitlines() if ln}
        if any(t.islower() for t in tags) or "S" in tags:
            return {"status": "unprotected", "last_commit": last}
    # tracked entry(자기 또는 디렉터리 descendant)의 git mode — **symlink(120000)·gitlink/submodule(160000)은
    # v0에서 content 보호를 검증 못 함**(link blob·gitlink만 clean이라 target/submodule 변경이 blessed로
    # 새는 false-clean, critic 재감사 B1). regular blob(100644/755)만 blessed 허용, 나머지 fail-closed.
    modes_out = _git(project_root, ["ls-files", "-s", "--", path])
    if modes_out is None:
        return {"status": "scan-error", "last_commit": last}
    modes = {ln.split(maxsplit=1)[0] for ln in modes_out.splitlines() if ln.strip()}
    if modes & {"120000", "160000"}:
        return {"status": "unsupported", "last_commit": last}
    if fp.is_dir():  # 디렉터리 core: ignored(!!)·untracked(??) 자식 = git 밖 내용
        others = _git(project_root, ["status", "--porcelain", "--ignored", "--", path])
        if others is None:
            return {"status": "scan-error", "last_commit": last}
        if any(ln[:2] in ("!!", "??") for ln in others.splitlines()):
            return {"status": "unprotected", "last_commit": last}
        return {"status": ("unblessed" if others.strip() else "blessed"), "last_commit": last}
    porcelain = _git(project_root, ["status", "--porcelain", "--", path])
    if porcelain is None:  # git 실패 → clean 아님(fail-closed)
        return {"status": "scan-error", "last_commit": last}
    return {"status": ("unblessed" if porcelain.strip() else "blessed"), "last_commit": last}


def report(state_dir: Path) -> list[dict]:
    """core 무결성 리포트 — [{path, authority, source, status, last_commit}] (path 정렬).
    observatory(축적 감시)·checkup(시점 WARN)이 소비하는 구조화 API."""
    project = state_dir.parent
    return [
        {"path": path, "authority": m["authority"], "source": m["source"], **classify(project, path)}
        for path, m in sorted(core_paths(state_dir).items())
    ]
