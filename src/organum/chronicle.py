"""Git-blob-fingerprinted structured ledger & markdown chronicle parser + diff engine.

Graduated from Ludex (Collective Organs Wave 2). Strictly read-only, Python
standard library only (zero PyYAML dependency), zero network or LLM calls.

정직 경계 — 과대주장 금지 (Honest Boundary):
  - Binds every parsed entry to its source path, 1-based line range, and exact
    Git blob SHA-1 fingerprint (``sha1("blob <len>\\0" + bytes)``) plus canonical
    entry SHA-256 digest.
  - Fails fast with ``ChronicleParseError(source_path, line_number, detail)`` on
    malformed lines, unterminated quotes, tab indentation, duplicate IDs, or
    missing required keys.
  - YAML mode parses a strict flat list-of-mappings ledger subset (e.g.
    ``switches:``, ``entries:``, ``items:``) with inline ``# comment`` handling;
    it is NOT a general-purpose nested YAML 1.2 parser.
  - Does NOT verify Git commit signatures or author identity — blob fingerprints
    prove content binding only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Sequence

SCHEMA = "organum.chronicle/v1"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MD_HEADING_RE = re.compile(r"^#{1,3}\s+(\d{4}-\d{2}-\d{2})(?:\s+.*)?$")
_MD_PIPE_RE = re.compile(r"^-\s+(\d{4}-\d{2}-\d{2})\s*\|\s*([^|\s][^|]*?)\s*\|\s*(.+?)\s*$")
_MD_BULLET_ID_RE = re.compile(r"^-\s+\[([^\]]+)\]\s+(.+?)\s*$")
_YAML_ROOT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(?:\s*(\[\]))?(?:\s+#.*)?$")
_YAML_KV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(?:\s*(.*))?$")

_DEFAULT_SWITCH_REQUIRED: tuple[str, ...] = ("id", "key", "what")


class ChronicleParseError(ValueError):
    """Fail-fast parse error naming the exact source file and 1-based line."""

    def __init__(self, source_path: str, line_number: int, detail: str) -> None:
        self.source_path = source_path
        self.line_number = line_number
        self.detail = detail
        super().__init__(f"{source_path}:{line_number}: {detail}")


@dataclass(frozen=True)
class ChronicleEntry:
    """Single verified chronicle entry bound to its source and Git blob digest."""

    entry_id: str
    date: str
    title: str
    fields: dict[str, str]
    source_path: str
    line_start: int
    line_end: int
    blob_fingerprint: str
    content_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "date": self.date,
            "title": self.title,
            "fields": dict(self.fields),
            "source_path": self.source_path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "blob_fingerprint": self.blob_fingerprint,
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True)
class ChronicleSnapshot:
    """Immutable snapshot of a parsed chronicle source with blob provenance."""

    label: str
    source_path: str
    git_rev: str | None
    blob_fingerprint: str
    entries: tuple[ChronicleEntry, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "label": self.label,
            "source_path": self.source_path,
            "git_rev": self.git_rev,
            "blob_fingerprint": self.blob_fingerprint,
            "entry_count": len(self.entries),
            "entries": [e.to_dict() for e in self.entries],
        }


@dataclass(frozen=True)
class ChangedEntry:
    """Entry present in both snapshots whose content changed."""

    entry_id: str
    before: ChronicleEntry
    after: ChronicleEntry
    field_changes: dict[str, dict[str, str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "before_lines": [self.before.line_start, self.before.line_end],
            "after_lines": [self.after.line_start, self.after.line_end],
            "field_changes": dict(self.field_changes),
        }


@dataclass(frozen=True)
class ChronicleDiff:
    """Exact diff between two fingerprinted snapshots."""

    before_label: str
    after_label: str
    source_path: str
    before_blob: str
    after_blob: str
    added: tuple[ChronicleEntry, ...]
    removed: tuple[ChronicleEntry, ...]
    changed: tuple[ChangedEntry, ...]
    unchanged_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "source_path": self.source_path,
            "before": {"label": self.before_label, "blob_fingerprint": self.before_blob},
            "after": {"label": self.after_label, "blob_fingerprint": self.after_blob},
            "counts": {
                "added": len(self.added),
                "removed": len(self.removed),
                "changed": len(self.changed),
                "unchanged": self.unchanged_count,
            },
            "added": [e.to_dict() for e in self.added],
            "removed": [e.to_dict() for e in self.removed],
            "changed": [c.to_dict() for c in self.changed],
        }


def git_blob_sha1(data: bytes) -> str:
    """Compute exact Git blob SHA-1 matching ``git hash-object``."""
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def _entry_digest(entry_id: str, date: str, title: str, fields: dict[str, str]) -> str:
    canonical = json.dumps(
        {"id": entry_id, "date": date, "title": title, "fields": fields},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _check_trailing_yaml_comment(tail: str, source_path: str, lineno: int, raw: str) -> None:
    if not tail:
        return
    if tail[0] in (" ", "\t"):
        stripped = tail.lstrip(" \t")
        if not stripped or stripped.startswith("#"):
            return
    raise ChronicleParseError(source_path, lineno, f"unexpected trailing content after quoted scalar: {raw!r}")


def _unquote_yaml_scalar(raw: str, source_path: str, lineno: int) -> str:
    val = raw.strip()
    if not val:
        return ""
    if val[0] == '"':
        out: list[str] = []
        i = 1
        closed = False
        while i < len(val):
            ch = val[i]
            if ch == "\\" and i + 1 < len(val):
                nxt = val[i + 1]
                esc_map = {'"': '"', "\\": "\\", "n": "\n", "t": "\t", "r": "\r"}
                out.append(esc_map.get(nxt, "\\" + nxt))
                i += 2
            elif ch == '"':
                closed = True
                i += 1
                break
            else:
                out.append(ch)
                i += 1
        if not closed:
            raise ChronicleParseError(source_path, lineno, f"unterminated double-quoted scalar: {val!r}")
        _check_trailing_yaml_comment(val[i:], source_path, lineno, val)
        return "".join(out)
    if val[0] == "'":
        out_sq: list[str] = []
        i = 1
        closed = False
        while i < len(val):
            if val[i] == "'":
                if i + 1 < len(val) and val[i + 1] == "'":
                    out_sq.append("'")
                    i += 2
                else:
                    closed = True
                    i += 1
                    break
            else:
                out_sq.append(val[i])
                i += 1
        if not closed:
            raise ChronicleParseError(source_path, lineno, f"unterminated single-quoted scalar: {val!r}")
        _check_trailing_yaml_comment(val[i:], source_path, lineno, val)
        return "".join(out_sq)
    m = re.search(r"[ \t]+#", val)
    if m:
        val = val[: m.start()].rstrip(" \t")
    return val


def parse_yaml_ledger(
    text: str,
    *,
    source_path: str,
    root_key: str | None = None,
    required_keys: Sequence[str] = ("id",),
    date_keys: Sequence[str] = ("landed", "date"),
    title_keys: Sequence[str] = ("what", "title", "summary"),
    blob_fingerprint: str | None = None,
    label: str = "ledger",
    git_rev: str | None = None,
) -> ChronicleSnapshot:
    """Parse a flat YAML list-of-mappings ledger strictly with line ranges.

    When ``root_key`` is ``None``, accepts any top-level ``<name>:`` header (e.g.
    ``switches:``, ``entries:``, ``items:``); otherwise requires that exact header.
    """
    raw_bytes = text.encode("utf-8")
    blob = blob_fingerprint or git_blob_sha1(raw_bytes)
    lines = text.splitlines()

    saw_root = False
    req = tuple(required_keys)
    entries: list[ChronicleEntry] = []
    seen_ids: set[str] = set()
    cur_fields: dict[str, str] | None = None
    cur_start = 0
    cur_end = 0

    def _flush() -> None:
        nonlocal cur_fields, cur_start, cur_end
        if cur_fields is None:
            return
        missing = [k for k in req if not cur_fields.get(k)]
        if missing:
            raise ChronicleParseError(
                source_path,
                cur_start,
                f"ledger entry missing required keys: {', '.join(missing)}",
            )
        eid = cur_fields.get("id", "")
        if not eid:
            raise ChronicleParseError(source_path, cur_start, "ledger entry missing non-empty 'id'")
        if eid in seen_ids:
            raise ChronicleParseError(source_path, cur_start, f"duplicate entry id: {eid!r}")
        seen_ids.add(eid)
        date = ""
        for dk in date_keys:
            if cur_fields.get(dk):
                date = cur_fields[dk]
                break
        if date and not _DATE_RE.match(date):
            raise ChronicleParseError(source_path, cur_start, f"malformed date {date!r} for {eid!r}")
        title = ""
        for tk in title_keys:
            if cur_fields.get(tk):
                title = cur_fields[tk]
                break
        entries.append(ChronicleEntry(
            entry_id=eid,
            date=date,
            title=title or eid,
            fields=dict(cur_fields),
            source_path=source_path,
            line_start=cur_start,
            line_end=cur_end,
            blob_fingerprint=blob,
            content_sha256=_entry_digest(eid, date, title or eid, cur_fields),
        ))
        cur_fields = None
        cur_start = 0
        cur_end = 0

    for lineno, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not saw_root:
            m_root = _YAML_ROOT_RE.match(stripped)
            if m_root and (root_key is None or m_root.group(1) == root_key):
                saw_root = True
                continue
            expected = f"{root_key}:" if root_key else "<root>:"
            raise ChronicleParseError(source_path, lineno, f"expected '{expected}' root header, got {stripped!r}")

        if raw.startswith("\t"):
            raise ChronicleParseError(source_path, lineno, "tab indentation is not allowed in YAML ledger")
        indent = len(raw) - len(raw.lstrip(" "))

        if stripped.startswith("- "):
            if indent < 2:
                raise ChronicleParseError(source_path, lineno, f"unexpected list item indentation ({indent})")
            _flush()
            rest = stripped[2:].strip()
            m = _YAML_KV_RE.match(rest)
            if not m:
                raise ChronicleParseError(source_path, lineno, f"malformed list item: {stripped!r}")
            k, v = m.group(1), _unquote_yaml_scalar(m.group(2) or "", source_path, lineno)
            cur_fields = {k: v}
            cur_start = lineno
            cur_end = lineno
            continue

        if cur_fields is None:
            raise ChronicleParseError(source_path, lineno, f"unexpected line before first list item: {stripped!r}")
        if indent < 4:
            raise ChronicleParseError(source_path, lineno, f"unexpected field indentation ({indent}) for {stripped!r}")
        m = _YAML_KV_RE.match(stripped)
        if not m:
            raise ChronicleParseError(source_path, lineno, f"malformed key: value line: {stripped!r}")
        k, v = m.group(1), _unquote_yaml_scalar(m.group(2) or "", source_path, lineno)
        if k in cur_fields:
            raise ChronicleParseError(source_path, lineno, f"duplicate key {k!r} inside entry")
        cur_fields[k] = v
        cur_end = lineno

    if not saw_root:
        expected = f"{root_key}:" if root_key else "<root>:"
        raise ChronicleParseError(source_path, 1, f"missing '{expected}' root header")
    _flush()

    return ChronicleSnapshot(
        label=label,
        source_path=source_path,
        git_rev=git_rev,
        blob_fingerprint=blob,
        entries=tuple(entries),
    )


def parse_switches_off_yaml(
    text: str,
    *,
    source_path: str,
    blob_fingerprint: str | None = None,
    label: str = "switches-off",
    git_rev: str | None = None,
) -> ChronicleSnapshot:
    """Strict wrapper for ``switches:`` ledgers requiring ``(id, key, what)``."""
    return parse_yaml_ledger(
        text,
        source_path=source_path,
        root_key="switches",
        required_keys=_DEFAULT_SWITCH_REQUIRED,
        date_keys=("landed", "date"),
        title_keys=("what", "title"),
        blob_fingerprint=blob_fingerprint,
        label=label,
        git_rev=git_rev,
    )


def parse_markdown_chronicle(
    text: str,
    *,
    source_path: str,
    blob_fingerprint: str | None = None,
    label: str = "chronicle",
    git_rev: str | None = None,
) -> ChronicleSnapshot:
    """Parse a dated markdown chronicle strictly (fail-fast on malformed lines).

    Accepted entry formats:
      1. Inline pipe line: ``- YYYY-MM-DD | entry-id | Title text``
      2. Sectioned bullet under ``## YYYY-MM-DD``: ``- [entry-id] Title text``
    """
    raw_bytes = text.encode("utf-8")
    blob = blob_fingerprint or git_blob_sha1(raw_bytes)
    lines = text.splitlines()

    cur_date: str | None = None
    entries: list[ChronicleEntry] = []
    seen_ids: set[str] = set()

    for lineno, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("<!--"):
            continue
        if stripped.startswith("#"):
            m_head = _MD_HEADING_RE.match(stripped)
            if m_head:
                cur_date = m_head.group(1)
            elif re.match(r"^#{1,3}\s+\d{4}-", stripped):
                raise ChronicleParseError(source_path, lineno, f"malformed date heading: {stripped!r}")
            continue
        if stripped.startswith("- "):
            m_pipe = _MD_PIPE_RE.match(stripped)
            if m_pipe:
                date, eid, title = m_pipe.group(1), m_pipe.group(2).strip(), m_pipe.group(3).strip()
            else:
                m_bul = _MD_BULLET_ID_RE.match(stripped)
                if not m_bul:
                    raise ChronicleParseError(source_path, lineno, f"malformed chronicle bullet: {stripped!r}")
                if cur_date is None:
                    raise ChronicleParseError(source_path, lineno, "chronicle bullet appears before any YYYY-MM-DD heading")
                date, eid, title = cur_date, m_bul.group(1).strip(), m_bul.group(2).strip()

            if not _DATE_RE.match(date):
                raise ChronicleParseError(source_path, lineno, f"malformed date {date!r}")
            if not eid or not title:
                raise ChronicleParseError(source_path, lineno, "chronicle entry requires non-empty id and title")
            if eid in seen_ids:
                raise ChronicleParseError(source_path, lineno, f"duplicate chronicle id: {eid!r}")
            seen_ids.add(eid)
            fields = {"id": eid, "date": date, "title": title}
            entries.append(ChronicleEntry(
                entry_id=eid,
                date=date,
                title=title,
                fields=fields,
                source_path=source_path,
                line_start=lineno,
                line_end=lineno,
                blob_fingerprint=blob,
                content_sha256=_entry_digest(eid, date, title, fields),
            ))
            continue

    return ChronicleSnapshot(
        label=label,
        source_path=source_path,
        git_rev=git_rev,
        blob_fingerprint=blob,
        entries=tuple(entries),
    )


def diff_snapshots(before: ChronicleSnapshot, after: ChronicleSnapshot) -> ChronicleDiff:
    """Compute exact added / removed / changed / unchanged entries across snapshots."""
    before_map = {e.entry_id: e for e in before.entries}
    after_map = {e.entry_id: e for e in after.entries}

    added = tuple(e for e in after.entries if e.entry_id not in before_map)
    removed = tuple(e for e in before.entries if e.entry_id not in after_map)

    changed: list[ChangedEntry] = []
    unchanged = 0
    for e_after in after.entries:
        e_before = before_map.get(e_after.entry_id)
        if e_before is None:
            continue
        if e_before.content_sha256 == e_after.content_sha256:
            unchanged += 1
            continue
        all_keys = sorted(set(e_before.fields.keys()) | set(e_after.fields.keys()))
        field_delta: dict[str, dict[str, str]] = {}
        for k in all_keys:
            vb = e_before.fields.get(k, "")
            va = e_after.fields.get(k, "")
            if vb != va:
                field_delta[k] = {"before": vb, "after": va}
        changed.append(ChangedEntry(
            entry_id=e_after.entry_id,
            before=e_before,
            after=e_after,
            field_changes=field_delta,
        ))

    return ChronicleDiff(
        before_label=before.label,
        after_label=after.label,
        source_path=after.source_path or before.source_path,
        before_blob=before.blob_fingerprint,
        after_blob=after.blob_fingerprint,
        added=added,
        removed=removed,
        changed=tuple(changed),
        unchanged_count=unchanged,
    )


def read_git_blob_text(repo: Path, rev: str, rel_path: str) -> tuple[str, str]:
    """Read ``<rev>:<rel_path>`` from Git read-only and return ``(text, git_blob_sha1)``."""
    spec = f"{rev}:{rel_path}"
    proc = subprocess.run(
        ["git", "-C", str(repo), "show", spec],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise FileNotFoundError(f"git show {spec} failed: {err}")
    data = proc.stdout
    return data.decode("utf-8"), git_blob_sha1(data)


def parse_snapshot_auto(
    text: str,
    *,
    source_path: str,
    fmt: str = "auto",
    root_key: str | None = None,
    blob_fingerprint: str | None = None,
    label: str = "snapshot",
    git_rev: str | None = None,
) -> ChronicleSnapshot:
    """Dispatch to YAML ledger or markdown chronicle parser."""
    chosen = fmt
    if chosen == "auto":
        chosen = "yaml" if source_path.endswith((".yaml", ".yml")) else "markdown"
    if chosen == "switches-yaml":
        return parse_switches_off_yaml(
            text,
            source_path=source_path,
            blob_fingerprint=blob_fingerprint,
            label=label,
            git_rev=git_rev,
        )
    if chosen == "yaml":
        return parse_yaml_ledger(
            text,
            source_path=source_path,
            root_key=root_key,
            blob_fingerprint=blob_fingerprint,
            label=label,
            git_rev=git_rev,
        )
    if chosen == "markdown":
        return parse_markdown_chronicle(
            text,
            source_path=source_path,
            blob_fingerprint=blob_fingerprint,
            label=label,
            git_rev=git_rev,
        )
    raise ValueError(f"unsupported chronicle format: {fmt!r}")


def load_snapshot_file(
    path: Path,
    *,
    fmt: str = "auto",
    root_key: str | None = None,
    label: str | None = None,
) -> ChronicleSnapshot:
    """Read a chronicle/ledger file from disk (read-only) with exact Git blob SHA-1."""
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"chronicle file not found: {target}")
    raw = target.read_bytes()
    return parse_snapshot_auto(
        raw.decode("utf-8"),
        source_path=str(target),
        fmt=fmt,
        root_key=root_key,
        blob_fingerprint=git_blob_sha1(raw),
        label=label or target.name,
    )


def render_diff_markdown(diff: ChronicleDiff) -> str:
    """Render a compact human-readable markdown diff with Git blob provenance."""
    lines = [
        f"# Chronicle Diff — `{diff.source_path}`",
        "",
        f"- **Before:** `{diff.before_label}` (blob `{diff.before_blob[:12]}`)",
        f"- **After:** `{diff.after_label}` (blob `{diff.after_blob[:12]}`)",
        f"- **Counts:** added={len(diff.added)} · removed={len(diff.removed)} · "
        f"changed={len(diff.changed)} · unchanged={diff.unchanged_count}",
    ]
    if diff.added:
        lines.extend(["", "## Added"])
        for e in diff.added:
            d = f" ({e.date})" if e.date else ""
            lines.append(
                f"- `{e.entry_id}`{d} [L{e.line_start}-L{e.line_end}, blob `{e.blob_fingerprint[:12]}`]: {e.title}"
            )
    if diff.removed:
        lines.extend(["", "## Removed"])
        for e in diff.removed:
            d = f" ({e.date})" if e.date else ""
            lines.append(
                f"- `{e.entry_id}`{d} [L{e.line_start}-L{e.line_end}, blob `{e.blob_fingerprint[:12]}`]: {e.title}"
            )
    if diff.changed:
        lines.extend(["", "## Changed"])
        for c in diff.changed:
            keys = ", ".join(sorted(c.field_changes.keys()))
            lines.append(
                f"- `{c.entry_id}` [L{c.after.line_start}-L{c.after.line_end}]: fields changed ({keys})"
            )
    return "\n".join(lines) + "\n"
