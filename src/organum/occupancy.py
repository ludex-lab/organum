"""organum occupancy — 결정적 시간 버킷 점유율 제어 (Collective Memory Hygiene).

발단: Ludex/나루 마을의 집단 기억 실측에서 단일 세션/하루 폭주("불꽃놀이")가
제한된 기억 슬롯을 100% 점유해 이전 시간대의 중요한 사실을 밀어내는 붕괴를
방지하기 위해 검증·졸업한 기관이다 (참고: KeyMem, arXiv:2611.25649).

동작:
- 타임스탬프 스트림을 고정 폭 시간 버킷(``int(t // bucket_seconds)``)으로 나눈다.
- 버킷당 최대 점유 상한(``bucket_cap``)을 강제하고, 전체 용량(``capacity``) 초과 시
  가장 붐비는 버킷의 최저 순위 항목부터 결정적 순위 ``(score, t, id)``로 축출한다.
- 랜덤 샘플러·LLM 호출·외부 의존성 0 (Python 3.10+ 표준 라이브러리만).

정직 경계 (과대주장 금지):
- 시간 분포 편중을 진단하고 결정적 버킷 상한으로 균형을 맞출 뿐, 기억 내용의
  의미적 진위·오염 여부를 판정하지 않는다(저장 경계 차단은 ``organum.guard`` 몫).
- 읽기 전용 진단·선택기이며 ``.organum/``의 ``format-v0`` 파일을 수정하지 않는다.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable, Sequence

SCHEMA = "organum.temporal-occupancy/v1"

_CONFIDENCE_SCORE = {
    "well-supported": 0.95,
    "confirmed": 0.80,
    "tentative": 0.50,
}


class OccupancyError(ValueError):
    """Fail-fast validation error for invalid occupancy parameters or items."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class MemoryItem:
    """Normalized timestamped memory record with deterministic rank key."""

    item_id: str
    t: float
    score: float
    payload: Any = None

    @property
    def rank_key(self) -> tuple[float, float, str]:
        return (self.score, self.t, self.item_id)


def _validate_params(
    capacity: int,
    bucket_seconds: float | None = None,
    bucket_cap: int | None = None,
) -> None:
    if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity <= 0:
        raise OccupancyError("invalid_capacity", f"capacity must be a positive int, got {capacity!r}")
    if bucket_seconds is not None:
        if isinstance(bucket_seconds, bool) or not isinstance(bucket_seconds, (int, float)) or bucket_seconds <= 0:
            raise OccupancyError("invalid_bucket_seconds", f"bucket_seconds must be > 0, got {bucket_seconds!r}")
    if bucket_cap is not None:
        if not isinstance(bucket_cap, int) or isinstance(bucket_cap, bool) or bucket_cap <= 0:
            raise OccupancyError("invalid_bucket_cap", f"bucket_cap must be a positive int, got {bucket_cap!r}")


def _parse_iso_epoch(raw: Any) -> float | None:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    if not isinstance(raw, str) or not raw.strip():
        return None
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _normalize_items(
    items: Sequence[Any],
    *,
    score_key: Callable[[Any], float] | None = None,
    time_key: Callable[[Any], float] | None = None,
    id_key: Callable[[Any], str] | None = None,
) -> list[MemoryItem]:
    out: list[MemoryItem] = []
    for idx, raw in enumerate(items):
        if isinstance(raw, MemoryItem):
            out.append(raw)
            continue
        if score_key is None or time_key is None or id_key is None:
            if not isinstance(raw, dict):
                raise OccupancyError("bad_item", f"item {idx} must be MemoryItem or dict")
            iid = str(raw.get("id") or "")
            if not iid:
                raise OccupancyError("missing_id", f"item {idx} missing non-empty 'id'")
            t_val = raw.get("t")
            s_val = raw.get("score")
            if isinstance(t_val, bool) or not isinstance(t_val, (int, float)):
                raise OccupancyError("bad_time", f"item {iid!r} has non-numeric 't'")
            if isinstance(s_val, bool) or not isinstance(s_val, (int, float)):
                raise OccupancyError("bad_score", f"item {iid!r} has non-numeric 'score'")
            out.append(MemoryItem(item_id=iid, t=float(t_val), score=float(s_val), payload=raw))
        else:
            iid = str(id_key(raw))
            if not iid:
                raise OccupancyError("missing_id", f"item {idx} resolved empty id")
            out.append(MemoryItem(item_id=iid, t=float(time_key(raw)), score=float(score_key(raw)), payload=raw))
    return out


def select_score_only(
    items: Sequence[Any],
    *,
    capacity: int,
    score_key: Callable[[Any], float] | None = None,
    time_key: Callable[[Any], float] | None = None,
    id_key: Callable[[Any], str] | None = None,
) -> list[MemoryItem]:
    """Weak baseline: keep highest-ranked items globally with no temporal structure."""
    _validate_params(capacity)
    stream = _normalize_items(items, score_key=score_key, time_key=time_key, id_key=id_key)
    kept: list[MemoryItem] = []
    for item in stream:
        kept.append(item)
        if len(kept) > capacity:
            victim = min(kept, key=lambda x: x.rank_key)
            kept.remove(victim)
    return sorted(kept, key=lambda x: x.rank_key, reverse=True)


def select_recency_fifo(
    items: Sequence[Any],
    *,
    capacity: int,
    score_key: Callable[[Any], float] | None = None,
    time_key: Callable[[Any], float] | None = None,
    id_key: Callable[[Any], str] | None = None,
) -> list[MemoryItem]:
    """Weak baseline: plain FIFO recency eviction (oldest arrival leaves first)."""
    _validate_params(capacity)
    stream = _normalize_items(items, score_key=score_key, time_key=time_key, id_key=id_key)
    kept: list[MemoryItem] = []
    for item in stream:
        kept.append(item)
        if len(kept) > capacity:
            kept.pop(0)
    return kept


def select_with_bucket_cap(
    items: Sequence[Any],
    *,
    capacity: int,
    bucket_seconds: float,
    bucket_cap: int,
    score_key: Callable[[Any], float] | None = None,
    time_key: Callable[[Any], float] | None = None,
    id_key: Callable[[Any], str] | None = None,
) -> dict[str, Any]:
    """Strict arm: enforce a hard per-time-bucket occupancy cap + crowded-bucket overflow."""
    _validate_params(capacity, bucket_seconds=bucket_seconds, bucket_cap=bucket_cap)
    stream = _normalize_items(items, score_key=score_key, time_key=time_key, id_key=id_key)

    buckets: dict[int, list[MemoryItem]] = defaultdict(list)
    total = 0
    evicted_bucket_cap = 0
    evicted_global_cap = 0

    for item in stream:
        b_idx = int(item.t // bucket_seconds)
        buckets[b_idx].append(item)
        total += 1

        if len(buckets[b_idx]) > bucket_cap:
            victim = min(buckets[b_idx], key=lambda x: x.rank_key)
            buckets[b_idx].remove(victim)
            total -= 1
            evicted_bucket_cap += 1

        if total > capacity:
            non_empty = [(b, arr) for b, arr in buckets.items() if arr]
            max_len = max(len(arr) for _, arr in non_empty)
            most_crowded = [(b, arr) for b, arr in non_empty if len(arr) == max_len]
            victim_bucket, victim_item = min(
                ((b, min(arr, key=lambda x: x.rank_key)) for b, arr in most_crowded),
                key=lambda pair: pair[1].rank_key,
            )
            buckets[victim_bucket].remove(victim_item)
            if not buckets[victim_bucket]:
                del buckets[victim_bucket]
            total -= 1
            evicted_global_cap += 1

    survivors = [item for arr in buckets.values() for item in arr]
    survivors.sort(key=lambda x: x.rank_key, reverse=True)
    bucket_counts = {str(b): len(arr) for b, arr in sorted(buckets.items()) if arr}
    max_occ = max((len(arr) for arr in buckets.values() if arr), default=0)

    return {
        "schema": SCHEMA,
        "survivors": survivors,
        "capacity": capacity,
        "bucket_seconds": bucket_seconds,
        "bucket_cap": bucket_cap,
        "kept_count": len(survivors),
        "distinct_buckets": len(bucket_counts),
        "max_bucket_occupancy": max_occ,
        "bucket_counts": bucket_counts,
        "evicted_bucket_cap": evicted_bucket_cap,
        "evicted_global_cap": evicted_global_cap,
    }


def load_jsonl_items(path: Path) -> tuple[list[MemoryItem], int]:
    """Read timestamped records from ``.organum/memory/memories.jsonl`` or any JSONL ledger.

    Returns ``(items, skipped_rows)``. Strictly read-only.
    """
    if not path.is_file():
        return [], 0
    items: list[MemoryItem] = []
    skipped = 0
    for idx, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if not isinstance(row, dict):
            skipped += 1
            continue
        ts = _parse_iso_epoch(row.get("ts") if row.get("ts") is not None else (row.get("at") if row.get("at") is not None else row.get("t")))
        if ts is None:
            skipped += 1
            continue
        iid = str(row.get("id") or f"row-{idx}")
        raw_score = row.get("score")
        if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool):
            score = float(raw_score)
        else:
            score = _CONFIDENCE_SCORE.get(str(row.get("confidence") or "").lower(), 0.50)
        items.append(MemoryItem(item_id=iid, t=ts, score=score, payload=row))
    return items, skipped


def inspect_jsonl_occupancy(
    path: Path,
    *,
    capacity: int = 24,
    bucket_seconds: float = 86400.0,
    bucket_cap: int = 6,
) -> dict[str, Any]:
    """Read-only occupancy diagnostic on a JSONL memory ledger (before vs after bucket cap)."""
    _validate_params(capacity, bucket_seconds=bucket_seconds, bucket_cap=bucket_cap)
    items, skipped = load_jsonl_items(path)
    raw_buckets: dict[int, int] = defaultdict(int)
    for it in items:
        raw_buckets[int(it.t // bucket_seconds)] += 1
    raw_max = max(raw_buckets.values(), default=0)
    raw_top_share = round(raw_max / len(items), 4) if items else 0.0

    capped = select_with_bucket_cap(
        items,
        capacity=capacity,
        bucket_seconds=bucket_seconds,
        bucket_cap=bucket_cap,
    )
    return {
        "schema": SCHEMA,
        "source": str(path),
        "total_items": len(items),
        "skipped_rows": skipped,
        "raw_distinct_buckets": len(raw_buckets),
        "raw_max_bucket_count": raw_max,
        "raw_top_bucket_share": raw_top_share,
        "capacity": capacity,
        "bucket_seconds": bucket_seconds,
        "bucket_cap": bucket_cap,
        "kept_count": capped["kept_count"],
        "distinct_buckets": capped["distinct_buckets"],
        "max_bucket_occupancy": capped["max_bucket_occupancy"],
        "evicted_bucket_cap": capped["evicted_bucket_cap"],
        "evicted_global_cap": capped["evicted_global_cap"],
        "survivor_ids": [m.item_id for m in capped["survivors"]],
    }
