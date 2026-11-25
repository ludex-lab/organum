"""organum manifest — 기록·실험 번들 표지(``## Files``) 대 폴더 실물 대조기.

발단: Ludex/나루 연구 번들에서 표지 Markdown의 ``## Files`` 표에 적힌 파일이
``.gitignore``(예: ``*.jsonl``)에 가려 빠지거나 영수증 파일이 누락되는 사고를
결정적으로 잡기 위해 검증·졸업한 기관이다 (``organum.integrity``의 짝).

기능:
- 단일 중괄호 묶음 확장 (예: ``e1_selftest_{agy,haiku,grok}.json``).
- macOS HFS+/APFS NFD 파일명과 NFC 표지 간 Unicode NFC 정규화 대조.
- ``missing_receipt`` · ``missing_named_file``(FAIL)과 ``unparsed_name`` ·
  ``listed_not_named``(INFO)를 구분하고, ``.gitignore`` 규칙 일치 사유를 첨부.
- 표준 라이브러리만 사용, LLM 호출 0, 엄격히 읽기 전용.

정직 경계 (과대주장 금지):
- 이 기관은 **파일 존재 여부(presence)**와 ignore 사유만 검증한다.
- 파일 내용의 암호학적 변조 여부는 검증하지 않는다(변조 감지는 SHA-256 매니페스트
  또는 ``organum integrity``의 git-blessing과 함께 사용한다).
"""
from __future__ import annotations

from dataclasses import dataclass
import fnmatch
from pathlib import Path
import re
from typing import Any, Sequence
import unicodedata

SCHEMA = "organum.record-manifest/v1"

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
_BACKTICK_RE = re.compile(r"`([^`]+)`")
_BRACE_RE = re.compile(r"^(?P<pre>[^{}]*)\{(?P<opts>[^{}]+)\}(?P<post>[^{}]*)$")
_FILENAME_LIKE_RE = re.compile(r"^(?:\.?[A-Za-z0-9@+~_ -]+)\.[A-Za-z0-9_+-]+$")


class ManifestError(ValueError):
    """Fail-fast error raised on malformed cover markdown or path violations."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class ManifestFinding:
    level: str  # "FAIL" or "INFO"
    code: str
    name: str
    group: str = ""
    detail: str = ""
    ignored_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "level": self.level,
            "code": self.code,
            "name": self.name,
            "group": self.group,
            "detail": self.detail,
        }
        if self.ignored_by is not None:
            out["ignored_by"] = self.ignored_by
        return out


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def parse_gitignore_rules(text: str) -> list[tuple[str, bool]]:
    """Parse non-comment ``.gitignore`` lines into ``(pattern, is_negated)`` pairs."""
    rules: list[tuple[str, bool]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        neg = line.startswith("!")
        pat = line[1:].strip() if neg else line
        if pat:
            rules.append((pat, neg))
    return rules


def explain_ignored(filename: str, rules: Sequence[tuple[str, bool]], *, rel_path: str = "") -> str | None:
    """Return the final matching non-negated gitignore pattern explaining why a file is ignored."""
    base = _nfc(Path(filename).name)
    full = _nfc(rel_path or filename).lstrip("./")
    matched: str | None = None
    for pat, is_neg in rules:
        pat_n = _nfc(pat)
        if "/" in pat_n:
            hit = fnmatch.fnmatch(full, pat_n.lstrip("/")) or fnmatch.fnmatch(full, "**/" + pat_n.lstrip("/"))
        else:
            hit = fnmatch.fnmatch(base, pat_n) or fnmatch.fnmatch(full, pat_n)
        if hit:
            matched = None if is_neg else pat
    return matched


def _expand_token(token: str) -> tuple[list[str], str | None]:
    tok = _nfc(token.strip())
    if not tok:
        return [], "empty token"
    if tok.startswith("+") or "*" in tok or "?" in tok or "…" in tok or "..." in tok:
        return [], f"non-concrete token {tok!r}"
    if "{" in tok or "}" in tok:
        m = _BRACE_RE.match(tok)
        if not m:
            return [], f"nested or unbalanced braces in {tok!r}"
        opts = [o.strip() for o in m.group("opts").split(",")]
        if len(opts) < 2 or any(not o for o in opts):
            return [], f"brace group needs >=2 non-empty choices in {tok!r}"
        expanded: list[str] = []
        for opt in opts:
            cand = f"{m.group('pre')}{opt}{m.group('post')}"
            last_seg = cand.rsplit("/", 1)[-1]
            if not _FILENAME_LIKE_RE.match(last_seg):
                return [], f"expanded choice {cand!r} is not a single filename"
            expanded.append(last_seg)
        return expanded, None

    if "," in tok:
        return [], f"comma list token {tok!r}"

    last_seg = tok.rsplit("/", 1)[-1]
    if not _FILENAME_LIKE_RE.match(last_seg):
        return [], f"not a single filename {tok!r}"
    return [last_seg], None


def parse_cover_files_table(markdown_text: str) -> dict[str, Any]:
    """Extract named files and groups from the ``## Files`` Markdown table of a cover."""
    if not isinstance(markdown_text, str) or not markdown_text.strip():
        raise ManifestError("empty_cover", "cover markdown is empty")

    lines = markdown_text.splitlines()
    in_fence = False
    files_section_level: int | None = None
    table_rows: list[tuple[str, str]] = []
    started_table = False

    for raw in lines:
        if _FENCE_RE.match(raw):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m_h = _HEADING_RE.match(raw.strip())
        if m_h:
            level = len(m_h.group(1))
            heading = m_h.group(2).strip().lower()
            if files_section_level is None:
                if heading == "files" or heading.startswith("files "):
                    files_section_level = level
            elif level <= files_section_level:
                break
            continue
        if files_section_level is None:
            continue
        stripped = raw.strip()
        if not stripped:
            if started_table:
                break
            continue
        if not stripped.startswith("|"):
            if started_table:
                break
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 2:
            if started_table:
                break
            continue
        if all(re.match(r"^:?-{2,}:?$", c.replace(" ", "")) for c in cells if c):
            started_table = True
            continue
        if not started_table and cells[0].lower() in ("group", "file", "files", "item", "category", "role"):
            started_table = True
            continue
        started_table = True
        table_rows.append((cells[0], cells[1]))

    if files_section_level is None:
        raise ManifestError("missing_files_section", "cover has no '## Files' section")
    if not table_rows:
        raise ManifestError("missing_files_table", "cover '## Files' section has no table rows")

    named: list[tuple[str, str]] = []
    unparsed: list[tuple[str, str, str]] = []
    groups_seen: list[str] = []

    for group, cell in table_rows:
        grp = _nfc(group.strip()) or "Files"
        if grp not in groups_seen:
            groups_seen.append(grp)
        for m_plus in re.finditer(r"\(\s*\+[^)]+\)", cell):
            unparsed.append((grp, m_plus.group(0), "extra in-file note"))
        tokens = _BACKTICK_RE.findall(cell)
        if not tokens:
            cleaned = re.sub(r"\([^)]*\)", "", cell).strip()
            tokens = [t.strip() for t in cleaned.split() if t.strip()]
        for tok in tokens:
            expanded, err = _expand_token(tok)
            if err is not None:
                unparsed.append((grp, tok, err))
            else:
                for fn in expanded:
                    named.append((grp, fn))

    return {
        "groups": groups_seen,
        "has_corrections_group": any("correction" in g.lower() for g in groups_seen),
        "named": named,
        "unparsed": unparsed,
    }


def check_manifest_presence(
    cover_markdown: str,
    listed_files: Sequence[str],
    *,
    gitignore_text: str = "",
    folder_prefix: str = "",
    receipt_name: str | None = None,
) -> dict[str, Any]:
    """Check that every file named in ``cover_markdown`` exists in ``listed_files``."""
    parsed = parse_cover_files_table(cover_markdown)
    ignore_rules = parse_gitignore_rules(gitignore_text) if gitignore_text else []

    listed_norm = {_nfc(Path(f).name) for f in listed_files if str(f).strip()}
    findings: list[ManifestFinding] = []
    named_set: set[str] = set()

    for grp, raw_tok, why in parsed["unparsed"]:
        findings.append(ManifestFinding("INFO", "unparsed_name", raw_tok, group=grp, detail=why))

    for grp, fname in parsed["named"]:
        named_set.add(fname)
        if fname in listed_norm:
            continue
        is_receipt = ("receipt" in fname.lower()) or (receipt_name is not None and _nfc(receipt_name) == fname)
        code = "missing_receipt" if is_receipt else "missing_named_file"
        rel = f"{folder_prefix.rstrip('/')}/{fname}" if folder_prefix else fname
        rule = explain_ignored(fname, ignore_rules, rel_path=rel) if ignore_rules else None
        detail = f"named in {grp!r} but absent from folder listing"
        if rule:
            detail += f" (ignored_by_pattern {rule})"
        findings.append(ManifestFinding("FAIL", code, fname, group=grp, detail=detail, ignored_by=rule))

    if receipt_name is not None and _nfc(receipt_name) not in named_set:
        findings.append(ManifestFinding(
            "INFO",
            "receipt_not_named",
            _nfc(receipt_name),
            detail="--receipt-name was given but cover does not name it",
        ))

    for extra in sorted(listed_norm - named_set):
        findings.append(ManifestFinding(
            "INFO",
            "listed_not_named",
            extra,
            detail="present in folder listing but not named in cover ## Files table",
        ))

    fail_count = sum(1 for f in findings if f.level == "FAIL")
    return {
        "schema": SCHEMA,
        "verdict": "PASS" if fail_count == 0 else "FAIL",
        "passed": fail_count == 0,
        "named_count": len(named_set),
        "listed_count": len(listed_norm),
        "missing_count": fail_count,
        "has_corrections_group": parsed["has_corrections_group"],
        "findings": [f.to_dict() for f in findings],
    }


def _safe_contained_file(root: Path, rel_or_name: str) -> Path:
    root_res = root.resolve()
    cand = (root_res / rel_or_name).resolve()
    try:
        cand.relative_to(root_res)
    except ValueError:
        raise ManifestError("path_outside_root", f"cover path escapes target directory: {rel_or_name!r}") from None
    if not cand.is_file():
        raise ManifestError("cover_not_found", f"cover file not found: {rel_or_name!r}")
    return cand


def check_record_dir(
    directory: Path,
    *,
    cover_name: str = "README.md",
    gitignore_path: Path | None = None,
    receipt_name: str | None = None,
) -> dict[str, Any]:
    """Read-only check of a record directory against its cover Markdown ``## Files`` table."""
    target = Path(directory)
    if not target.is_dir():
        raise ManifestError("dir_not_found", f"record directory not found: {directory!r}")
    cover_path = _safe_contained_file(target, cover_name)
    cover_text = cover_path.read_text(encoding="utf-8")
    listed = sorted(
        p.name for p in target.iterdir()
        if p.is_file() and p.name != ".DS_Store"
    )
    gi_text = ""
    if gitignore_path is not None and Path(gitignore_path).is_file():
        gi_text = Path(gitignore_path).read_text(encoding="utf-8", errors="ignore")
    rep = check_manifest_presence(
        cover_text,
        listed,
        gitignore_text=gi_text,
        folder_prefix=target.name,
        receipt_name=receipt_name,
    )
    rep["directory"] = str(target)
    rep["cover"] = cover_path.name
    return rep
