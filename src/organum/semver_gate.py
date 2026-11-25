"""Strict SemVer 2.0.0 & CalVer parser, stable-range policy gate, and CLI upgrade classifier.

Graduated from Ludex (Collective Organs Wave 2). Strictly read-only, Python
standard library only, zero network or LLM calls.

정직 경계 — 과대주장 금지 (Honest Boundary):
  - Enforces strict SemVer 2.0.0 grammar (no leading ``v``, no two-part ``1.0``,
    no leading zeros in numeric core or numeric prerelease identifiers) and
    real calendar-date validation for CalVer (``YYYY.MM.DD``).
  - Applies SemVer 2.0.0 §11 precedence plus an explicit stable-range policy
    (prereleases are excluded from stable ranges like ``>= 1.0.0`` unless
    ``allow_prerelease=True`` or a comparator explicitly targets a prerelease
    on the same ``(major, minor, patch)`` core).
  - Classifies CLI version moves (``same``, ``upgraded_stable``, ``downgraded``,
    ``prerelease_jump``, ``floor_violation``, ``malformed_version``).
  - Does NOT verify binary behavior, API backwards compatibility, or package
    signatures — version strings alone never prove runtime safety.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re
from typing import Any

SCHEMA = "organum.semver-gate/v1"

_SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_DIGITS_RE = re.compile(r"^[0-9]+$")
_COMPARATOR_RE = re.compile(r"^(==|!=|>=|<=|>|<|\^|~)\s*(\S+)$")
_CALVER_RE = re.compile(
    r"^(20[0-9]{2}|21[0-9]{2})\.(0?[1-9]|1[0-2])\.(0?[1-9]|[12][0-9]|3[01])"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_CALVER_PRERELEASE_HINT_RE = re.compile(r"^(?:alpha|beta|rc|pre|dev|nightly|canary|preview|next)\b", re.I)
_HEX_BUILD_TAG_RE = re.compile(r"^[0-9a-f]{6,40}$", re.I)

CALVER_TOOLS = frozenset({"cursor", "cursor_cli", "cursor-agent"})
CALVER_TRIBES = CALVER_TOOLS  # compatibility alias


class SemVerError(ValueError):
    """Raised when a version or constraint violates strict SemVer/CalVer syntax."""

    def __init__(self, reason_code: str, raw: str, detail: str) -> None:
        self.reason_code = reason_code
        self.raw = raw
        self.detail = detail
        super().__init__(f"{reason_code}: {detail} ({raw!r})")


@dataclass(frozen=True)
class SemVer:
    """Parsed SemVer 2.0.0 (or CalVer) version."""

    major: int
    minor: int
    patch: int
    prerelease: tuple[int | str, ...] = ()
    build: tuple[str, ...] = ()
    raw: str = ""

    @property
    def core(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    @property
    def is_prerelease(self) -> bool:
        return len(self.prerelease) > 0

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            base += "-" + ".".join(str(p) for p in self.prerelease)
        if self.build:
            base += "+" + ".".join(self.build)
        return base


def parse_semver(raw: str) -> SemVer:
    """Strictly parse a SemVer 2.0.0 string without stripping prefixes or suffixes."""
    if not isinstance(raw, str) or not raw:
        raise SemVerError("malformed_version", str(raw), "version must be a non-empty string")
    if raw != raw.strip():
        raise SemVerError("malformed_version", raw, "leading or trailing whitespace is not allowed")
    m = _SEMVER_RE.match(raw)
    if not m:
        raise SemVerError("malformed_version", raw, "does not match strict SemVer 2.0.0 grammar")
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))

    pre_parts: list[int | str] = []
    if m.group(4) is not None:
        for ident in m.group(4).split("."):
            if _DIGITS_RE.match(ident):
                if len(ident) > 1 and ident.startswith("0"):
                    raise SemVerError(
                        "malformed_version",
                        raw,
                        f"numeric prerelease identifier {ident!r} must not have leading zeros",
                    )
                pre_parts.append(int(ident))
            else:
                pre_parts.append(ident)

    build_parts: tuple[str, ...] = tuple(m.group(5).split(".")) if m.group(5) is not None else ()
    return SemVer(
        major=major,
        minor=minor,
        patch=patch,
        prerelease=tuple(pre_parts),
        build=build_parts,
        raw=raw,
    )


def parse_calver(raw: str) -> SemVer:
    """Strictly parse a calendar version (``YYYY.MM.DD``) such as Cursor CLI releases.

    Validates real calendar dates (rejecting e.g. ``2026.02.30``). A pure hex
    commit suffix like ``2026.10.08-1a2b3c4`` is treated as build metadata rather
    than a prerelease, while tags like ``-rc.1`` or ``-beta.2`` are treated as
    prerelease identifiers.
    """
    if not isinstance(raw, str) or not raw:
        raise SemVerError("malformed_version", str(raw), "version must be a non-empty string")
    if raw != raw.strip():
        raise SemVerError("malformed_version", raw, "leading or trailing whitespace is not allowed")
    m = _CALVER_RE.match(raw)
    if not m:
        raise SemVerError("malformed_version", raw, "does not match strict CalVer YYYY.MM.DD grammar")
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        date(year, month, day)
    except ValueError:
        raise SemVerError("malformed_version", raw, f"invalid calendar date {year:04d}-{month:02d}-{day:02d}") from None

    pre_parts: list[int | str] = []
    build_parts: list[str] = list(m.group(5).split(".")) if m.group(5) is not None else []
    suffix = m.group(4)
    if suffix is not None:
        if _HEX_BUILD_TAG_RE.match(suffix) and not _CALVER_PRERELEASE_HINT_RE.match(suffix):
            build_parts.insert(0, suffix)
        else:
            for ident in suffix.split("."):
                pre_parts.append(int(ident) if _DIGITS_RE.match(ident) else ident)

    return SemVer(
        major=year,
        minor=month,
        patch=day,
        prerelease=tuple(pre_parts),
        build=tuple(build_parts),
        raw=raw,
    )


def compare_semver(a: SemVer, b: SemVer) -> int:
    """Compare two SemVer instances per SemVer 2.0.0 §11 (returns -1, 0, or +1)."""
    if a.core != b.core:
        return -1 if a.core < b.core else 1
    if not a.prerelease and not b.prerelease:
        return 0
    if not a.prerelease:
        return 1
    if not b.prerelease:
        return -1

    for pa, pb in zip(a.prerelease, b.prerelease):
        if pa == pb:
            continue
        a_num, b_num = isinstance(pa, int), isinstance(pb, int)
        if a_num and b_num:
            return -1 if pa < pb else 1
        if a_num != b_num:
            return -1 if a_num else 1
        return -1 if str(pa) < str(pb) else 1

    if len(a.prerelease) != len(b.prerelease):
        return -1 if len(a.prerelease) < len(b.prerelease) else 1
    return 0


@dataclass(frozen=True)
class Comparator:
    """Single parsed constraint comparator such as ``>= 1.0.0``."""

    op: str
    target: SemVer


@dataclass(frozen=True)
class GateDecision:
    """Structured result of evaluating a version against a constraint spec."""

    ok: bool
    reason_code: str
    detail: str
    version_raw: str
    spec_raw: str
    parsed_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "ok": self.ok,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "version_raw": self.version_raw,
            "spec_raw": self.spec_raw,
            "parsed_version": self.parsed_version,
        }


def parse_constraint(spec_raw: str, *, scheme: str = "semver") -> tuple[Comparator, ...]:
    """Parse a comma-separated list of strict SemVer (or CalVer) comparators."""
    if not isinstance(spec_raw, str) or not spec_raw.strip():
        raise SemVerError("malformed_constraint", str(spec_raw), "constraint spec must be non-empty")
    parts = [p.strip() for p in spec_raw.split(",") if p.strip()]
    if not parts:
        raise SemVerError("malformed_constraint", spec_raw, "no comparators found")
    parser = parse_calver if scheme == "calver" else parse_semver
    out: list[Comparator] = []
    for part in parts:
        m = _COMPARATOR_RE.match(part)
        if not m:
            raise SemVerError("malformed_constraint", spec_raw, f"invalid comparator {part!r}")
        op, ver_str = m.group(1), m.group(2)
        try:
            ver = parser(ver_str)
        except SemVerError as exc:
            raise SemVerError("malformed_constraint", spec_raw, f"invalid version in {part!r}: {exc.detail}") from exc
        out.append(Comparator(op=op, target=ver))
    return tuple(out)


def _caret_upper(v: SemVer) -> SemVer:
    if v.major > 0:
        return SemVer(v.major + 1, 0, 0)
    if v.minor > 0:
        return SemVer(0, v.minor + 1, 0)
    return SemVer(0, 0, v.patch + 1)


def _tilde_upper(v: SemVer) -> SemVer:
    return SemVer(v.major, v.minor + 1, 0)


def _satisfies_comparator(v: SemVer, comp: Comparator) -> bool:
    cmp_val = compare_semver(v, comp.target)
    if comp.op == "==":
        return cmp_val == 0
    if comp.op == "!=":
        return cmp_val != 0
    if comp.op == ">=":
        return cmp_val >= 0
    if comp.op == ">":
        return cmp_val > 0
    if comp.op == "<=":
        return cmp_val <= 0
    if comp.op == "<":
        return cmp_val < 0
    if comp.op == "^":
        return cmp_val >= 0 and compare_semver(v, _caret_upper(comp.target)) < 0
    if comp.op == "~":
        return cmp_val >= 0 and compare_semver(v, _tilde_upper(comp.target)) < 0
    return False


def evaluate_constraint(
    version_raw: str,
    spec_raw: str,
    *,
    allow_prerelease: bool = False,
    scheme: str = "semver",
) -> GateDecision:
    """Evaluate ``version_raw`` against ``spec_raw`` under SemVer 2.0.0 (or CalVer) + stable policy."""
    parser = parse_calver if scheme == "calver" else parse_semver
    try:
        ver = parser(version_raw)
    except SemVerError as exc:
        return GateDecision(False, exc.reason_code, exc.detail, str(version_raw), str(spec_raw))

    try:
        comps = parse_constraint(spec_raw, scheme=scheme)
    except SemVerError as exc:
        return GateDecision(False, exc.reason_code, exc.detail, version_raw, str(spec_raw), str(ver))

    for comp in comps:
        if not _satisfies_comparator(ver, comp):
            reason = (
                "prerelease_below_release"
                if (ver.is_prerelease and ver.core == comp.target.core and not comp.target.is_prerelease)
                else "constraint_not_satisfied"
            )
            return GateDecision(
                False,
                reason,
                f"{ver} does not satisfy {comp.op} {comp.target}",
                version_raw,
                spec_raw,
                str(ver),
            )

    if ver.is_prerelease and not allow_prerelease:
        explicitly_opted_in = any(c.target.is_prerelease and c.target.core == ver.core for c in comps)
        if not explicitly_opted_in:
            return GateDecision(
                False,
                "prerelease_disallowed_in_stable_range",
                f"prerelease {ver} is excluded from stable range {spec_raw!r}",
                version_raw,
                spec_raw,
                str(ver),
            )

    return GateDecision(
        True,
        "ok",
        f"{ver} satisfies {spec_raw}",
        version_raw,
        spec_raw,
        str(ver),
    )


@dataclass(frozen=True)
class UpgradeTransition:
    """Classified version transition for CLI harness upgrade checks."""

    status: str
    before_raw: str
    after_raw: str
    before_parsed: str | None
    after_parsed: str | None
    detail: str
    alert: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "status": self.status,
            "before_raw": self.before_raw,
            "after_raw": self.after_raw,
            "before_parsed": self.before_parsed,
            "after_parsed": self.after_parsed,
            "detail": self.detail,
            "alert": self.alert,
        }


def _extract_version_candidate(raw: str) -> str:
    """Extract the version-looking token from a CLI ``--version`` line without mutating it."""
    text = (raw or "").strip()
    if not text:
        return ""
    tokens = text.split()
    if len(tokens) == 1:
        return tokens[0]
    for tok in tokens:
        cleaned = tok.strip("(),")
        if re.match(r"^[vV]?\d+\.\d+", cleaned):
            return cleaned
    return text


def classify_upgrade_transition(
    before_raw: str,
    after_raw: str,
    *,
    floor: str | None = None,
    extract_token: bool = True,
    scheme: str = "auto",
    tool: str | None = None,
    tribe: str | None = None,
) -> UpgradeTransition:
    """Classify a CLI version move under strict SemVer 2.0.0 or CalVer (``YYYY.MM.DD``).

    Uses Calendar Versioning when ``scheme="calver"`` or ``tool``/``tribe`` is in
    ``CALVER_TOOLS`` (e.g. ``cursor``, ``cursor-agent``). Flags prerelease jumps,
    downgrades, floor violations, and malformed version strings.
    """
    tool_hint = tool if tool is not None else tribe
    use_calver = (scheme == "calver") or (
        scheme == "auto" and bool(tool_hint) and str(tool_hint).strip().lower() in CALVER_TOOLS
    )
    eff_scheme = "calver" if use_calver else "semver"
    parser = parse_calver if use_calver else parse_semver

    b_tok = _extract_version_candidate(before_raw) if extract_token else before_raw
    a_tok = _extract_version_candidate(after_raw) if extract_token else after_raw

    try:
        v_before = parser(b_tok)
    except SemVerError as exc:
        return UpgradeTransition("malformed_version", before_raw, after_raw, None, None, f"before: {exc.detail}", True)
    try:
        v_after = parser(a_tok)
    except SemVerError as exc:
        return UpgradeTransition("malformed_version", before_raw, after_raw, str(v_before), None, f"after: {exc.detail}", True)

    if floor is not None:
        gate = evaluate_constraint(a_tok, floor, allow_prerelease=False, scheme=eff_scheme)
        if not gate.ok:
            return UpgradeTransition(
                "floor_violation" if gate.reason_code != "prerelease_disallowed_in_stable_range" else "prerelease_jump",
                before_raw,
                after_raw,
                str(v_before),
                str(v_after),
                gate.detail,
                True,
            )

    cmp_val = compare_semver(v_before, v_after)
    if cmp_val == 0:
        return UpgradeTransition(
            "same",
            before_raw,
            after_raw,
            str(v_before),
            str(v_after),
            "version precedence unchanged",
            False,
        )
    if v_after.is_prerelease:
        return UpgradeTransition(
            "prerelease_jump",
            before_raw,
            after_raw,
            str(v_before),
            str(v_after),
            f"moved onto prerelease tag {v_after}",
            True,
        )
    if cmp_val > 0:
        return UpgradeTransition(
            "downgraded",
            before_raw,
            after_raw,
            str(v_before),
            str(v_after),
            f"version regressed from {v_before} to {v_after}",
            True,
        )
    return UpgradeTransition(
        "upgraded_stable",
        before_raw,
        after_raw,
        str(v_before),
        str(v_after),
        f"upgraded cleanly from {v_before} to {v_after}",
        False,
    )
