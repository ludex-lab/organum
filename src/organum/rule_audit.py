"""Independent invariant rule-audit core ("Verify the Verifier").

Graduated from Ludex (Collective Organs Wave 2). Strictly read-only, Python
standard library only, zero network or LLM calls.

Provides the domain-neutral rule-audit engine (``RuleSpec``, ``RuleViolation``,
``AuditReport``, ``audit_trace``, ``evaluate_cases``) plus two built-in generic
packs (``queue`` S1..S5 self-demo and ``hygiene`` H1..H3 runtime/artifact checks).
Project- or village-specific rule packs belong in each caller's own repository.

정직 경계 — 과대주장 금지 (Honest Boundary):
  - Evaluates numbered invariants directly over a trace dict independently of
    any baseline/reference verifier, flagging ``escape_detected=True`` whenever
    ``legacy_verdict == "PASS"`` disagrees with rule violations.
  - Only detects violations encoded in the supplied ``RuleSpec`` sequence:
    ``PASS`` means "zero violations among the checked rules", NOT "bug-free".
  - Does NOT mutate traces, repair state, or execute untrusted code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Callable, Sequence

SCHEMA = "organum.rule-audit/v1"

DEFAULT_QUOTA_PHRASES: tuple[str, ...] = (
    "hit your limit",
    "weekly limit",
    "rate limit exceeded",
    "quota exceeded",
    "usage limit reached",
)

_LEAK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("abs_user_path", re.compile(r"(?:/Users/|/home/|C:\\Users\\)", re.I)),
    ("email_address", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
)


@dataclass(frozen=True)
class RuleViolation:
    """Single numbered rule violation discovered in an execution trace."""

    rule_id: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "message": self.message,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class RuleSpec:
    """Numbered invariant rule evaluated directly over a trace dict."""

    rule_id: str
    title: str
    description: str
    check: Callable[[dict[str, Any]], list[RuleViolation]]


@dataclass(frozen=True)
class AuditReport:
    """Structured verdict from running an independent rule-audit pack."""

    pack: str
    passed: bool
    rules_checked: tuple[str, ...]
    violations: tuple[RuleViolation, ...]
    legacy_verdict: str | None = None
    escape_detected: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "pack": self.pack,
            "passed": self.passed,
            "verdict": "PASS" if self.passed else "FAIL",
            "rules_checked": list(self.rules_checked),
            "violation_count": len(self.violations),
            "violations": [v.to_dict() for v in self.violations],
            "legacy_verdict": self.legacy_verdict,
            "escape_detected": self.escape_detected,
        }


def audit_trace(
    trace: dict[str, Any],
    rules: Sequence[RuleSpec],
    *,
    pack: str = "custom",
    legacy_verdict: str | None = None,
) -> AuditReport:
    """Run all numbered rules over ``trace`` independently of any baseline model."""
    violations: list[RuleViolation] = []
    rule_ids: list[str] = []
    for rule in rules:
        rule_ids.append(rule.rule_id)
        found = rule.check(trace)
        violations.extend(found)
    passed = len(violations) == 0
    norm_legacy = legacy_verdict.upper() if isinstance(legacy_verdict, str) else None
    escape = (norm_legacy == "PASS") and (not passed)
    return AuditReport(
        pack=pack,
        passed=passed,
        rules_checked=tuple(rule_ids),
        violations=tuple(violations),
        legacy_verdict=norm_legacy,
        escape_detected=escape,
    )


def evaluate_cases(
    cases: Sequence[dict[str, Any]],
    rules: Sequence[RuleSpec],
    *,
    pack: str = "custom",
) -> dict[str, Any]:
    """Evaluate a battery of labeled traces and count verifier escapes caught."""
    results: list[dict[str, Any]] = []
    counts = {
        "total": 0,
        "escapes_caught": 0,
        "true_passes": 0,
        "true_fails": 0,
        "false_alarms": 0,
        "missed_faults": 0,
    }
    for idx, case in enumerate(cases):
        case_id = str(case.get("case_id") or f"case-{idx + 1}")
        legacy = case.get("legacy_verdict")
        expected_pass = bool(case.get("expected_rule_pass", True))
        report = audit_trace(
            case.get("trace") or {},
            rules,
            pack=pack,
            legacy_verdict=legacy,
        )
        counts["total"] += 1
        if report.escape_detected and not expected_pass:
            counts["escapes_caught"] += 1
        if expected_pass and report.passed:
            counts["true_passes"] += 1
        elif (not expected_pass) and (not report.passed):
            counts["true_fails"] += 1
        elif expected_pass and (not report.passed):
            counts["false_alarms"] += 1
        else:
            counts["missed_faults"] += 1
        results.append({
            "case_id": case_id,
            "expected_rule_pass": expected_pass,
            "report": report.to_dict(),
        })
    return {
        "schema": SCHEMA,
        "pack": pack,
        "summary": counts,
        "cases": results,
    }


# ---------------------------------------------------------------------------
# Built-in Pack 1: Bounded FIFO Queue Verification Pack (S1..S5)
# ---------------------------------------------------------------------------

def _check_queue_s1_capacity(trace: dict[str, Any]) -> list[RuleViolation]:
    cap = int(trace.get("capacity", 4))
    steps = trace.get("steps") or []
    violations: list[RuleViolation] = []
    size = 0
    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        op = step.get("op")
        accepted = bool(step.get("accepted"))
        if op == "enqueue":
            if size >= cap and accepted:
                violations.append(RuleViolation(
                    "S1",
                    f"step {idx}: enqueue accepted when queue was already full (capacity={cap})",
                    {"step": idx, "capacity": cap, "size_before": size, "item": step.get("item")},
                ))
            elif accepted:
                size += 1
        elif op == "dequeue":
            if accepted and size > 0:
                size -= 1
    return violations


def _check_queue_s2_fifo_order(trace: dict[str, Any]) -> list[RuleViolation]:
    steps = trace.get("steps") or []
    violations: list[RuleViolation] = []
    expected_q: list[Any] = []
    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        op = step.get("op")
        accepted = bool(step.get("accepted"))
        if op == "enqueue" and accepted:
            expected_q.append(step.get("item"))
        elif op == "dequeue" and accepted:
            actual = step.get("item")
            if not expected_q:
                continue
            oldest = expected_q.pop(0)
            if actual != oldest:
                violations.append(RuleViolation(
                    "S2",
                    f"step {idx}: FIFO order violated (dequeued {actual!r}, expected oldest {oldest!r})",
                    {"step": idx, "actual": actual, "expected": oldest},
                ))
    return violations


def _check_queue_s3_empty_dequeue(trace: dict[str, Any]) -> list[RuleViolation]:
    steps = trace.get("steps") or []
    violations: list[RuleViolation] = []
    size = 0
    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        op = step.get("op")
        accepted = bool(step.get("accepted"))
        if op == "enqueue" and accepted:
            size += 1
        elif op == "dequeue":
            if size == 0 and accepted:
                violations.append(RuleViolation(
                    "S3",
                    f"step {idx}: dequeue accepted on empty queue",
                    {"step": idx, "returned": step.get("item")},
                ))
            elif accepted and size > 0:
                size -= 1
    return violations


def _check_queue_s4_size_conservation(trace: dict[str, Any]) -> list[RuleViolation]:
    cap = int(trace.get("capacity", 4))
    steps = trace.get("steps") or []
    violations: list[RuleViolation] = []
    size = 0
    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        op = step.get("op")
        accepted = bool(step.get("accepted"))
        reported = step.get("size_after")
        if op == "enqueue" and accepted and size < cap:
            size += 1
        elif op == "dequeue" and accepted and size > 0:
            size -= 1
        if reported is not None and reported != size:
            violations.append(RuleViolation(
                "S4",
                f"step {idx}: size_after={reported} disagrees with conserved size={size}",
                {"step": idx, "reported_size": reported, "expected_size": size},
            ))
    return violations


def _check_queue_s5_trace_completeness(trace: dict[str, Any]) -> list[RuleViolation]:
    steps = trace.get("steps")
    if not isinstance(steps, list):
        return [RuleViolation("S5", "trace missing list field 'steps'", {})]
    violations: list[RuleViolation] = []
    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            violations.append(RuleViolation("S5", f"step {idx}: non-dict entry", {"step": idx}))
            continue
        if step.get("op") not in ("enqueue", "dequeue"):
            violations.append(RuleViolation("S5", f"step {idx}: invalid op {step.get('op')!r}", {"step": idx}))
        if "accepted" not in step or "size_after" not in step:
            violations.append(RuleViolation(
                "S5",
                f"step {idx}: missing required keys ('accepted', 'size_after')",
                {"step": idx, "keys": sorted(step.keys())},
            ))
    return violations


QUEUE_RULES: tuple[RuleSpec, ...] = (
    RuleSpec("S1", "Capacity bound", "Reject enqueue when queue is at capacity; never silently overwrite.", _check_queue_s1_capacity),
    RuleSpec("S2", "FIFO order", "Dequeued element must match oldest accepted element.", _check_queue_s2_fifo_order),
    RuleSpec("S3", "Empty dequeue reject", "Dequeue on empty queue must be rejected.", _check_queue_s3_empty_dequeue),
    RuleSpec("S4", "Size conservation", "Reported size_after must match conserved queue length.", _check_queue_s4_size_conservation),
    RuleSpec("S5", "Trace completeness", "Every step must record valid op, accepted, and size_after.", _check_queue_s5_trace_completeness),
)


# ---------------------------------------------------------------------------
# Built-in Pack 2: Domain-Neutral Agent Hygiene Invariants (H1..H3)
# ---------------------------------------------------------------------------

def _check_hygiene_h1_quota_leak(trace: dict[str, Any]) -> list[RuleViolation]:
    phrases = tuple(p.lower() for p in (trace.get("quota_phrases") or DEFAULT_QUOTA_PHRASES))
    rows = trace.get("runs") or trace.get("ring_rows") or []
    violations: list[RuleViolation] = []
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        outcome = str(row.get("outcome") or "").lower()
        exit_code = row.get("exit_code")
        claimed_ok = outcome in ("ok", "pass", "done") or exit_code == 0
        if not claimed_ok:
            continue
        haystack = " ".join(
            str(row.get(k) or "") for k in ("output_preview", "summary", "text", "note")
        ).lower()
        for phrase in phrases:
            if phrase in haystack:
                violations.append(RuleViolation(
                    "H1",
                    f"row {idx}: claimed ok/exit-0 while output contains quota phrase {phrase!r}",
                    {"row": idx, "actor": row.get("actor") or row.get("agent"), "phrase": phrase, "outcome": outcome},
                ))
                break
    return violations


def _check_hygiene_h2_privacy_leak(trace: dict[str, Any]) -> list[RuleViolation]:
    rows = trace.get("artifacts") or trace.get("rows") or []
    forbidden = tuple(str(t) for t in (trace.get("forbidden_tokens") or ()) if t)
    violations: list[RuleViolation] = []
    for idx, row in enumerate(rows):
        if isinstance(row, str):
            items = [("text", row)]
        elif isinstance(row, dict):
            items = [(str(k), str(v or "")) for k, v in row.items()]
        else:
            continue
        for col, val in items:
            for kind, rx in _LEAK_PATTERNS:
                if rx.search(val):
                    violations.append(RuleViolation(
                        "H2",
                        f"row {idx} field {col!r} matched privacy leak pattern {kind}",
                        {"row": idx, "field": col, "kind": kind},
                    ))
                    if len(violations) >= 20:
                        return violations
            for tok in forbidden:
                if tok in val:
                    violations.append(RuleViolation(
                        "H2",
                        f"row {idx} field {col!r} contains forbidden token",
                        {"row": idx, "field": col, "token": tok},
                    ))
                    if len(violations) >= 20:
                        return violations
    return violations


def _check_hygiene_h3_pin_hash_integrity(trace: dict[str, Any]) -> list[RuleViolation]:
    pins = trace.get("pin_checks") or []
    violations: list[RuleViolation] = []
    for entry in pins:
        if not isinstance(entry, dict):
            continue
        expected = entry.get("expected_sha256")
        actual = entry.get("actual_sha256")
        path = entry.get("path") or "<unknown>"
        if expected and actual and expected != actual:
            violations.append(RuleViolation(
                "H3",
                f"pinned file {path} sha256 drift detected",
                {"path": path, "expected_sha256": expected, "actual_sha256": actual},
            ))
    return violations


HYGIENE_RULES: tuple[RuleSpec, ...] = (
    RuleSpec("H1", "Zero-exit quota leak", "Exit-0 / ok run output must not contain quota or rate-limit notices.", _check_hygiene_h1_quota_leak),
    RuleSpec("H2", "Privacy leak scan", "Exported artifacts/rows must not leak local user paths, emails, or forbidden tokens.", _check_hygiene_h2_privacy_leak),
    RuleSpec("H3", "Pinned file integrity", "Pinned file sha256 digest must match expected digest.", _check_hygiene_h3_pin_hash_integrity),
)


def build_queue_self_demo_cases() -> list[dict[str, Any]]:
    """Return 7 synthetic FIFO-queue cases (2 normal, 2 genuine faults, 3 verifier escapes)."""
    return [
        {
            "case_id": "normal-fifo-fill-and-drain",
            "legacy_verdict": "PASS",
            "expected_rule_pass": True,
            "trace": {
                "capacity": 4,
                "steps": [
                    {"op": "enqueue", "item": "a", "accepted": True, "size_after": 1},
                    {"op": "enqueue", "item": "b", "accepted": True, "size_after": 2},
                    {"op": "dequeue", "item": "a", "accepted": True, "size_after": 1},
                    {"op": "dequeue", "item": "b", "accepted": True, "size_after": 0},
                ],
            },
        },
        {
            "case_id": "normal-full-reject-and-empty-reject",
            "legacy_verdict": "PASS",
            "expected_rule_pass": True,
            "trace": {
                "capacity": 2,
                "steps": [
                    {"op": "dequeue", "item": None, "accepted": False, "size_after": 0},
                    {"op": "enqueue", "item": 1, "accepted": True, "size_after": 1},
                    {"op": "enqueue", "item": 2, "accepted": True, "size_after": 2},
                    {"op": "enqueue", "item": 3, "accepted": False, "size_after": 2},
                    {"op": "dequeue", "item": 1, "accepted": True, "size_after": 1},
                ],
            },
        },
        {
            "case_id": "fault-lifo-order",
            "legacy_verdict": "FAIL",
            "expected_rule_pass": False,
            "trace": {
                "capacity": 4,
                "steps": [
                    {"op": "enqueue", "item": "x", "accepted": True, "size_after": 1},
                    {"op": "enqueue", "item": "y", "accepted": True, "size_after": 2},
                    {"op": "dequeue", "item": "y", "accepted": True, "size_after": 1},
                ],
            },
        },
        {
            "case_id": "fault-empty-dequeue-fabricates",
            "legacy_verdict": "FAIL",
            "expected_rule_pass": False,
            "trace": {
                "capacity": 4,
                "steps": [
                    {"op": "dequeue", "item": "ghost", "accepted": True, "size_after": 0},
                ],
            },
        },
        {
            "case_id": "escape-shared-overwrite-bug",
            "legacy_verdict": "PASS",
            "expected_rule_pass": False,
            "trace": {
                "capacity": 2,
                "steps": [
                    {"op": "enqueue", "item": "a", "accepted": True, "size_after": 1},
                    {"op": "enqueue", "item": "b", "accepted": True, "size_after": 2},
                    {"op": "enqueue", "item": "c", "accepted": True, "size_after": 2},
                    {"op": "dequeue", "item": "b", "accepted": True, "size_after": 1},
                ],
            },
        },
        {
            "case_id": "escape-unchecked-size-drift",
            "legacy_verdict": "PASS",
            "expected_rule_pass": False,
            "trace": {
                "capacity": 4,
                "steps": [
                    {"op": "enqueue", "item": 10, "accepted": True, "size_after": 1},
                    {"op": "enqueue", "item": 20, "accepted": True, "size_after": 99},
                ],
            },
        },
        {
            "case_id": "escape-missing-trace-fields",
            "legacy_verdict": "PASS",
            "expected_rule_pass": False,
            "trace": {
                "capacity": 4,
                "steps": [
                    {"op": "enqueue", "item": 10},
                ],
            },
        },
    ]


PACKS: dict[str, tuple[RuleSpec, ...]] = {
    "queue": QUEUE_RULES,
    "hygiene": HYGIENE_RULES,
}
