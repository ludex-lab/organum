"""Zero-Reference Equivalence Check (arXiv 2611.27186 §2.2, cleanroom stdlib).

When checking whether a simulated or candidate effect ``c`` "matches" a human
or reference interval ``[lo, hi]`` within equivalence margin ``m > 0``:
  - Inclusive equivalence: ``hi - m <= c <= lo + m``  (iff ``max(|c - lo|, |c - hi|) <= m``)
  - Strict equivalence (§2.2): ``hi - m < c < lo + m`` (iff ``max(|c - lo|, |c - hi|) < m``)

A matching PASS is **uninformative** whenever the zero-effect dummy candidate
``c = 0`` ALSO passes the exact same test — i.e., when the zero-reference
threshold ``T = max(|lo|, |hi|)`` satisfies ``T <= m`` (inclusive) or ``T < m``
(strict). Always reporting ``(T, zero_pass_strict, zero_pass_inclusive)``
alongside the candidate verdict prevents vacuous "match" claims.

Uses exact decimal arithmetic (``decimal.Decimal``) internally so boundary cases
like ``lo = -0.20, hi = 0.20, m = 0.20`` never suffer IEEE-754 drift.
Strictly read-only, Python standard library only, zero LLM or network calls.

정직 경계 — 과대주장 금지 (Honest Boundary):
  - Flags whether a reference interval ``[lo, hi]`` is distinguishable from zero
    at margin ``m`` and classifies each item as ``informative_match``,
    ``uninformative_pass``, ``worse_than_zero``, or ``mismatch``.
  - Does NOT compute the reference confidence interval itself, choose ``m``, or
    prove causal validity — it only audits whether an equivalence claim is
    vacuously satisfied by a null ``c = 0`` baseline.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
import math
from pathlib import Path
from typing import Any, Sequence

SCHEMA = "organum.zero-ref/v1"


class ZeroRefError(ValueError):
    """Raised on malformed numeric inputs or invalid interval/margin parameters."""


def _to_decimal(val: Any, name: str) -> Decimal:
    if isinstance(val, bool) or val is None:
        raise ZeroRefError(f"{name} must be a finite number, got {val!r}")
    if isinstance(val, Decimal):
        if not val.is_finite():
            raise ZeroRefError(f"{name} must be finite, got {val!r}")
        return val
    if isinstance(val, (int, float)):
        if not math.isfinite(val):
            raise ZeroRefError(f"{name} must be finite, got {val!r}")
        return Decimal(str(val))
    if isinstance(val, str):
        s = val.strip()
        if not s:
            raise ZeroRefError(f"{name} must be a finite number, got {val!r}")
        try:
            d = Decimal(s)
        except InvalidOperation:
            raise ZeroRefError(f"{name} is not a valid number: {val!r}") from None
        if not d.is_finite():
            raise ZeroRefError(f"{name} must be finite, got {val!r}")
        return d
    raise ZeroRefError(f"{name} must be a finite number, got {val!r}")


def _classify(candidate_pass: bool, zero_pass: bool) -> str:
    if candidate_pass and not zero_pass:
        return "informative_match"
    if candidate_pass and zero_pass:
        return "uninformative_pass"
    if (not candidate_pass) and zero_pass:
        return "worse_than_zero"
    return "mismatch"


@dataclass(frozen=True)
class ZeroRefResult:
    """Exact zero-reference check result for one equivalence claim."""

    lo: float
    hi: float
    candidate: float
    margin: float
    t_zero: float
    t_candidate: float
    interval_width: float
    feasible_inclusive: bool
    feasible_strict: bool
    candidate_pass_inclusive: bool
    zero_pass_inclusive: bool
    classification_inclusive: str
    candidate_pass_strict: bool
    zero_pass_strict: bool
    classification_strict: str
    zero_at_boundary: bool
    candidate_at_boundary: bool
    strict: bool = True
    item_id: str | None = None

    @property
    def candidate_pass(self) -> bool:
        return self.candidate_pass_strict if self.strict else self.candidate_pass_inclusive

    @property
    def zero_pass(self) -> bool:
        return self.zero_pass_strict if self.strict else self.zero_pass_inclusive

    @property
    def classification(self) -> str:
        return self.classification_strict if self.strict else self.classification_inclusive

    @property
    def informative(self) -> bool:
        return self.classification == "informative_match"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema": SCHEMA,
            "mode": "strict" if self.strict else "inclusive",
            "lo": self.lo,
            "hi": self.hi,
            "candidate": self.candidate,
            "margin": self.margin,
            "t_zero": self.t_zero,
            "t_candidate": self.t_candidate,
            "interval_width": self.interval_width,
            "feasible_inclusive": self.feasible_inclusive,
            "feasible_strict": self.feasible_strict,
            "candidate_pass": self.candidate_pass,
            "zero_pass": self.zero_pass,
            "classification": self.classification,
            "informative": self.informative,
            "inclusive": {
                "candidate_pass": self.candidate_pass_inclusive,
                "zero_pass": self.zero_pass_inclusive,
                "classification": self.classification_inclusive,
            },
            "strict": {
                "candidate_pass": self.candidate_pass_strict,
                "zero_pass": self.zero_pass_strict,
                "classification": self.classification_strict,
            },
            "zero_at_boundary": self.zero_at_boundary,
            "candidate_at_boundary": self.candidate_at_boundary,
        }
        if self.item_id is not None:
            out["item_id"] = self.item_id
        return out


def evaluate_zero_ref(
    lo: float | int | str | Decimal,
    hi: float | int | str | Decimal,
    candidate: float | int | str | Decimal,
    margin: float | int | str | Decimal,
    *,
    strict: bool = True,
    item_id: str | None = None,
) -> ZeroRefResult:
    """Evaluate a candidate effect against reference interval ``[lo, hi]`` and null ``c = 0``.

    Defaults to ``strict=True`` (arXiv 2611.27186 §2.2 strict ``<``), while
    always computing and returning both ``strict`` and ``inclusive`` (``<=``)
    verdicts plus boundary flags.
    """
    d_lo = _to_decimal(lo, "lo")
    d_hi = _to_decimal(hi, "hi")
    d_c = _to_decimal(candidate, "candidate")
    d_m = _to_decimal(margin, "margin")

    if d_lo > d_hi:
        raise ZeroRefError(f"interval lo ({d_lo}) must be <= hi ({d_hi})")
    if d_m <= 0:
        raise ZeroRefError(f"equivalence margin must be > 0, got {d_m}")

    d_t_zero = max(abs(d_lo), abs(d_hi))
    d_t_cand = max(abs(d_c - d_lo), abs(d_c - d_hi))
    d_width = d_hi - d_lo
    d_two_m = d_m * 2

    feas_inc = d_width <= d_two_m
    feas_str = d_width < d_two_m

    cand_inc = d_t_cand <= d_m
    zero_inc = d_t_zero <= d_m
    cls_inc = _classify(cand_inc, zero_inc)

    cand_str = d_t_cand < d_m
    zero_str = d_t_zero < d_m
    cls_str = _classify(cand_str, zero_str)

    return ZeroRefResult(
        lo=float(d_lo),
        hi=float(d_hi),
        candidate=float(d_c),
        margin=float(d_m),
        t_zero=float(d_t_zero),
        t_candidate=float(d_t_cand),
        interval_width=float(d_width),
        feasible_inclusive=feas_inc,
        feasible_strict=feas_str,
        candidate_pass_inclusive=cand_inc,
        zero_pass_inclusive=zero_inc,
        classification_inclusive=cls_inc,
        candidate_pass_strict=cand_str,
        zero_pass_strict=zero_str,
        classification_strict=cls_str,
        zero_at_boundary=(d_t_zero == d_m),
        candidate_at_boundary=(d_t_cand == d_m),
        strict=strict,
        item_id=item_id,
    )


def evaluate_zero_ref_batch(
    records: Sequence[dict[str, Any]],
    *,
    strict: bool = True,
) -> dict[str, Any]:
    """Evaluate a sequence of ``{id?, lo, hi, candidate, margin}`` records."""
    counts = {
        "total": 0,
        "informative_match": 0,
        "uninformative_pass": 0,
        "worse_than_zero": 0,
        "mismatch": 0,
        "zero_at_boundary": 0,
        "infeasible": 0,
    }
    results: list[dict[str, Any]] = []
    for idx, rec in enumerate(records):
        if not isinstance(rec, dict):
            raise ZeroRefError(f"record {idx} must be a dict")
        for req in ("lo", "hi", "candidate", "margin"):
            if req not in rec:
                raise ZeroRefError(f"record {idx} missing required key {req!r}")
        iid = str(rec["id"]) if rec.get("id") is not None else f"item-{idx + 1}"
        res = evaluate_zero_ref(
            rec["lo"],
            rec["hi"],
            rec["candidate"],
            rec["margin"],
            strict=strict,
            item_id=iid,
        )
        counts["total"] += 1
        counts[res.classification] += 1
        if res.zero_at_boundary:
            counts["zero_at_boundary"] += 1
        if not (res.feasible_strict if strict else res.feasible_inclusive):
            counts["infeasible"] += 1
        results.append(res.to_dict())

    return {
        "schema": SCHEMA,
        "mode": "strict" if strict else "inclusive",
        "summary": counts,
        "results": results,
    }


def load_zero_ref_records(path: Path) -> list[dict[str, Any]]:
    """Read a JSON array or JSONL file of zero-ref records (strictly read-only)."""
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"zero-ref input file not found: {target}")
    raw = target.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    if raw.startswith("["):
        data = json.loads(raw)
        if not isinstance(data, list):
            raise ZeroRefError("JSON root must be an array of record objects")
        return data
    if raw.startswith("{") and "\n" not in raw:
        return [json.loads(raw)]
    records: list[dict[str, Any]] = []
    for lineno, line in enumerate(raw.splitlines(), start=1):
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except json.JSONDecodeError as exc:
            raise ZeroRefError(f"invalid JSON on line {lineno}: {exc}") from exc
        if not isinstance(obj, dict):
            raise ZeroRefError(f"line {lineno} must be a JSON object")
        records.append(obj)
    return records
