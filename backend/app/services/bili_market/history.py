from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from app.models import BiliPriceHistory

HISTORY_RANGES_HOURS = {
    "1h": 1,
    "6h": 6,
    "24h": 24,
    "7d": 24 * 7,
    "30d": 24 * 30,
    "90d": 24 * 90,
}
DEFAULT_HISTORY_RANGE = "24h"
DEFAULT_MAX_HISTORY_POINTS = 600
MIN_HISTORY_POINTS = 2
MAX_HISTORY_POINTS = 1000


@dataclass(frozen=True)
class HistoryPointRecord:
    """Common raw/rollup shape consumed by the deterministic sampler."""

    observed_at: datetime
    period_end: datetime
    available: bool
    current_price: Optional[Decimal]
    reference_price: Optional[Decimal]
    resolution_seconds: Optional[int] = None
    sample_count: int = 1
    min_price: Optional[Decimal] = None
    max_price: Optional[Decimal] = None

    @property
    def sort_key(self) -> tuple[datetime, datetime, int]:
        return (self.observed_at, self.period_end, self.resolution_seconds or 0)


def _price_delta(previous: Decimal | None, current: Decimal | None) -> Decimal:
    if previous is None or current is None:
        return Decimal("0")
    return abs(current - previous)


def _range_importance(previous: Any, current: Any) -> Decimal:
    """Score rollup-only interval extrema that are absent from last_price."""
    extrema = [getattr(current, "min_price", None), getattr(current, "max_price", None)]
    baselines = [
        getattr(previous, "current_price", None),
        getattr(previous, "min_price", None),
        getattr(previous, "max_price", None),
    ]
    score = Decimal("0")
    for extreme in extrema:
        if extreme is None:
            continue
        deltas = [_price_delta(extreme, baseline) for baseline in baselines if baseline is not None]
        if deltas:
            delta = max(deltas)
            baseline = max(
                [abs(extreme), *[abs(value) for value in baselines if value is not None], Decimal("1")]
            )
            if delta:
                score = max(score, Decimal("1500") + (delta / baseline) * Decimal("1500"))
    minimum = getattr(current, "min_price", None)
    maximum = getattr(current, "max_price", None)
    if minimum is not None and maximum is not None and maximum != minimum:
        baseline = max(abs(minimum), abs(maximum), Decimal("1"))
        score = max(score, Decimal("500") + (_price_delta(minimum, maximum) / baseline) * Decimal("1000"))
    return score


def _point_importance(rows: Sequence[BiliPriceHistory], index: int) -> Decimal:
    """Score a point for deterministic bucket selection.

    State changes receive priority. Price changes are scored by absolute and
    relative movement, and a local peak/trough receives a small bonus so a
    short-lived price dip is not routinely discarded by downsampling.
    """
    row = rows[index]
    previous = rows[index - 1]
    score = Decimal("0")
    if row.available != previous.available:
        score += Decimal("1000000")
    delta = _price_delta(previous.current_price, row.current_price)
    if delta:
        baseline = max(abs(previous.current_price or 0), abs(row.current_price or 0), Decimal("1"))
        score += Decimal("1000") + (delta / baseline) * Decimal("1000")
    if 0 < index < len(rows) - 1 and row.current_price is not None:
        before = rows[index - 1].current_price
        after = rows[index + 1].current_price
        if before is not None and after is not None:
            midpoint = (before + after) / Decimal("2")
            score += abs(row.current_price - midpoint) / max(abs(midpoint), Decimal("1"))
    score += _range_importance(previous, row)
    return score


def _transition_importance(previous: BiliPriceHistory, current: BiliPriceHistory) -> Decimal:
    score = Decimal("1000000") if current.available != previous.available else Decimal("0")
    delta = _price_delta(previous.current_price, current.current_price)
    if delta:
        baseline = max(abs(previous.current_price or Decimal("0")), abs(current.current_price or Decimal("0")), Decimal("1"))
        score += Decimal("1000") + (delta / baseline) * Decimal("1000")
    score += _range_importance(previous, current)
    return score


def _evenly_spaced(indices: Sequence[int], count: int) -> list[int]:
    if count <= 0 or not indices:
        return []
    if len(indices) <= count:
        return list(indices)
    if count == 1:
        return [indices[len(indices) // 2]]
    last = len(indices) - 1
    positions = {(index * last) // (count - 1) for index in range(count)}
    return [indices[position] for position in sorted(positions)]


def _bucket_candidates(rows: Sequence[BiliPriceHistory], bucket_count: int) -> list[int]:
    if bucket_count <= 0 or len(rows) <= 2:
        return []
    internal_count = len(rows) - 2
    candidates: list[int] = []
    for bucket in range(bucket_count):
        start = 1 + (bucket * internal_count) // bucket_count
        end = 1 + ((bucket + 1) * internal_count) // bucket_count
        if end <= start:
            continue
        bucket_indices = list(range(start, end))
        candidates.append(max(bucket_indices, key=lambda index: _point_importance(rows, index)))
    return candidates


def downsample_history(rows: Sequence[BiliPriceHistory], max_points: int) -> list[BiliPriceHistory]:
    """Return chronological points with endpoints and meaningful changes kept.

    The raw rows are expected in ascending ``observed_at`` order. We reserve
    capacity for endpoints, prioritize availability transitions and price
    changes, then fill remaining capacity from evenly distributed time/index
    buckets. If there are more changes than the requested budget, changes are
    selected evenly so the result still represents the whole range.
    """
    if len(rows) <= max_points:
        return list(rows)
    if max_points < MIN_HISTORY_POINTS:
        raise ValueError(f"max_points must be at least {MIN_HISTORY_POINTS}")

    internal_budget = max_points - 2
    change_indices = [
        index for index in range(1, len(rows))
        if _transition_importance(rows[index - 1], rows[index]) > 0
    ]
    state_indices = [
        index for index in change_indices
        if rows[index].available != rows[index - 1].available
    ]

    # Prefer every meaningful change when it fits. For long, noisy histories,
    # preserve state changes first and distribute the remaining change budget
    # across the timeline rather than clustering at one end.
    selected: set[int] = set()
    if len(change_indices) <= internal_budget:
        selected.update(change_indices)
    else:
        state_budget = min(len(state_indices), internal_budget)
        selected.update(_evenly_spaced(state_indices, state_budget))
        remaining = internal_budget - len(selected)
        price_indices = [index for index in change_indices if index not in selected]
        if remaining > 0:
            ranked = sorted(price_indices, key=lambda index: _point_importance(rows, index), reverse=True)
            selected.update(_evenly_spaced(sorted(ranked[:remaining]), remaining))

    remaining = max_points - 2 - len(selected)
    if remaining > 0:
        bucket_candidates = [index for index in _bucket_candidates(rows, remaining * 2) if index not in selected]
        selected.update(_evenly_spaced(bucket_candidates, remaining))

    indices = [0, *sorted(selected), len(rows) - 1]
    # Defensive cap: duplicate bucket candidates or an overfull change set
    # must never violate the API's max_points contract.
    indices = sorted(set(indices))
    if len(indices) > max_points:
        internal = [index for index in indices if index not in {0, len(rows) - 1}]
        indices = [0, *_evenly_spaced(internal, max_points - 2), len(rows) - 1]
    return [rows[index] for index in sorted(set(indices))]


def downsample_history_stream(rows: Iterable[BiliPriceHistory], total_points: int, max_points: int) -> list[BiliPriceHistory]:
    """Downsample an ordered result without materializing a long history.

    The API counts first, then streams only the raw rows needed to choose one
    representative per time/index bucket. At most ``max_points`` ORM objects
    are retained, which keeps 90-day histories bounded in application memory.
    """
    if total_points <= max_points:
        return list(rows)
    if max_points < MIN_HISTORY_POINTS:
        raise ValueError(f"max_points must be at least {MIN_HISTORY_POINTS}")

    iterator = iter(rows)
    if max_points == MIN_HISTORY_POINTS:
        first = next(iterator, None)
        if first is None:
            return []
        last = first
        for row in iterator:
            last = row
        return [first] if last is first else [first, last]

    bucket_count = max_points - 2
    internal_count = total_points - 2
    first: BiliPriceHistory | None = None
    last: BiliPriceHistory | None = None
    previous: BiliPriceHistory | None = None
    state_rows: list[tuple[int, BiliPriceHistory]] = []
    state_buckets: dict[int, tuple[int, BiliPriceHistory]] | None = None
    bucket_rows: dict[int, list[tuple[Decimal, int, BiliPriceHistory]]] = {}

    def bucket_for(index: int) -> int:
        return min(bucket_count - 1, ((index - 1) * bucket_count) // internal_count)

    def add_bucket_row(bucket: int, score: Decimal, index: int, row: BiliPriceHistory) -> None:
        candidates = bucket_rows.setdefault(bucket, [])
        if any(candidate[1] == index for candidate in candidates):
            return
        if score == 0 and candidates:
            # One ordinary representative is enough; changed-price rows are
            # allowed to add a second candidate for short-lived fluctuations.
            return
        candidates.append((score, index, row))
        candidates.sort(key=lambda candidate: (candidate[0], -candidate[1]), reverse=True)
        del candidates[2:]

    for index, row in enumerate(iterator):
        if first is None:
            first = row
        last = row
        if previous is not None and index < total_points - 1 and bucket_count > 0:
            bucket = bucket_for(index)
            score = _transition_importance(previous, row)
            is_state_change = row.available != previous.available
            if is_state_change:
                if state_buckets is None:
                    state_rows.append((index, row))
                    if len(state_rows) > max_points - 2:
                        # We now know that all state changes cannot fit. Keep
                        # one state point per time bucket from the already-seen
                        # rows, then continue with the same bounded structure.
                        state_buckets = {}
                        for state_index, state_row in state_rows:
                            state_buckets.setdefault(bucket_for(state_index), (state_index, state_row))
                        state_rows.clear()
                else:
                    state_buckets.setdefault(bucket, (index, row))
            else:
                add_bucket_row(bucket, score, index, row)
        previous = row

    if first is None or last is None:
        return []

    state_candidates = state_rows if state_buckets is None else list(state_buckets.values())
    selected_indices = {index for index, _ in state_candidates}
    candidate_rows = {index: row for index, row in state_candidates}
    internal_budget = max_points - 2
    remaining = internal_budget - len(selected_indices)
    candidates = [candidate for bucket in sorted(bucket_rows) for candidate in bucket_rows[bucket] if candidate[1] not in selected_indices]
    candidate_rows.update({index: row for _, index, row in candidates})
    price_candidates = [candidate for candidate in candidates if candidate[0] > 0]
    if remaining > 0:
        if len(price_candidates) <= remaining:
            chosen_prices = price_candidates
        else:
            ranked = sorted(price_candidates, key=lambda candidate: (candidate[0], -candidate[1]), reverse=True)
            chosen_prices = [ranked[0]]
            if remaining > 1:
                positions = _evenly_spaced(list(range(1, len(ranked))), remaining - 1)
                chosen_prices.extend(ranked[index] for index in positions)
        for _, index, row in chosen_prices:
            selected_indices.add(index)
            candidate_rows[index] = row
        remaining = internal_budget - len(selected_indices)

    if remaining > 0:
        fill_candidates = [candidate[1] for candidate in candidates if candidate[1] not in selected_indices]
        for index in _evenly_spaced(fill_candidates, remaining):
            selected_indices.add(index)

    return [first, *(candidate_rows[index] for index in sorted(selected_indices)), last]
