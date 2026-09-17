"""Turn rosters + profiles into per-player rows and per-school summaries."""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from .benchmark import (
    MAX_GAP_DAYS,
    benchmark_date,
    nearest_entry,
    parse_utr_date,
    singles_history,
)

MIN_HISTORY_ENTRIES = 100
WALK_ON_THRESHOLD = 8.0
# Flag (don't drop) players whose displayed UTR and newest weekly entry disagree by more than this.
DISPLAY_MISMATCH = 0.10


@dataclass
class PlayerRow:
    school: str
    college_id: int
    player_id: int
    name: str
    class_code: str
    benchmark_target: str
    recruited_at_utr: float | None = None
    recruited_at_date: str | None = None
    benchmark_gap_days: int | None = None
    current_utr: float | None = None
    current_date: str | None = None
    delta: float | None = None
    years_elapsed: float | None = None
    annualised_delta: float | None = None
    history_entries: int = 0
    included: bool = True
    flags: list[str] = field(default_factory=list)
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SchoolSummary:
    school: str
    college_id: int
    n: int
    band_min: float | None
    band_max: float | None
    median: float | None
    mean_annualised_delta: float | None
    note: str | None = None
    target_utr: float | None = None
    recruited_above_target: int | None = None
    target_percentile: float | None = None
    # Set only when a target range (low..high) is given; target_utr is then the low end.
    target_utr_high: float | None = None
    recruited_in_range: int | None = None
    recruited_above_high: int | None = None
    target_percentile_high: float | None = None
    fit: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _r2(x: float | None) -> float | None:
    return None if x is None else round(x, 2)


def build_player_row(
    *,
    school: str,
    college_id: int,
    player_id: int,
    name: str,
    class_code: str,
    season_name: str,
    profile: dict[str, Any] | None,
    fetch_error: str | None = None,
    history_count: int | None = None,
) -> PlayerRow:
    """`history_count` overrides the entry count when `profile` carries a trimmed history."""
    target = benchmark_date(season_name, class_code)
    row = PlayerRow(
        school=school,
        college_id=college_id,
        player_id=player_id,
        name=name,
        class_code=class_code,
        benchmark_target=target.isoformat(),
    )
    if profile is None:
        row.included = False
        row.reason = fetch_error or "profile unavailable"
        return row

    history = singles_history(profile)
    row.history_entries = history_count if history_count is not None else len(history)
    if history:
        newest = max(history, key=lambda h: h["date"])
        row.current_utr = _r2(newest["rating"])
        row.current_date = parse_utr_date(newest["date"]).isoformat()
        shown = profile.get("singlesUtr")
        if shown and row.current_utr is not None and abs(float(shown) - row.current_utr) > DISPLAY_MISMATCH:
            row.flags.append(f"profile shows {float(shown):.2f}, latest weekly {row.current_utr:.2f}")

    if row.history_entries < MIN_HISTORY_ENTRIES:
        row.included = False
        row.reason = f"only {row.history_entries} weekly singles entries (< {MIN_HISTORY_ENTRIES})"
        return row

    hit = nearest_entry(history, target)
    if hit is None:
        row.included = False
        row.reason = "no rated singles entries"
        return row
    entry, gap = hit
    row.benchmark_gap_days = gap
    if gap > MAX_GAP_DAYS:
        row.included = False
        row.reason = f"nearest entry is {gap} days from {target.isoformat()} (> {MAX_GAP_DAYS})"
        row.flags.append("unreliable")
        return row

    landed = parse_utr_date(entry["date"])
    row.recruited_at_utr = _r2(entry["rating"])
    row.recruited_at_date = landed.isoformat()
    if row.current_utr is not None and row.current_date is not None:
        row.delta = _r2(row.current_utr - row.recruited_at_utr)
        years = (date.fromisoformat(row.current_date) - landed).days / 365.25
        row.years_elapsed = _r2(years)
        if years > 0:
            row.annualised_delta = _r2((row.current_utr - row.recruited_at_utr) / years)
    if row.recruited_at_utr < WALK_ON_THRESHOLD:
        row.flags.append("below-8.0-likely-walk-on")
    return row


def percentile_of(value: float, population: list[float]) -> float:
    """Share of the population strictly below `value`, with ties counted half."""
    below = sum(1 for p in population if p < value)
    equal = sum(1 for p in population if p == value)
    return 100.0 * (below + 0.5 * equal) / len(population)


def summarise_school(
    school: str,
    college_id: int,
    rows: list[PlayerRow],
    target_utr: float | None,
    target_high: float | None = None,
) -> SchoolSummary:
    usable = [r for r in rows if r.included and r.recruited_at_utr is not None]
    values = [r.recruited_at_utr for r in usable]  # type: ignore[misc]
    deltas = [r.annualised_delta for r in usable if r.annualised_delta is not None]
    n = len(values)
    summary = SchoolSummary(
        school=school,
        college_id=college_id,
        n=n,
        band_min=min(values) if values else None,
        band_max=max(values) if values else None,
        median=_r2(statistics.median(values)) if values else None,
        mean_annualised_delta=_r2(statistics.fmean(deltas)) if deltas else None,
    )
    if n == 0:
        summary.note = "no usable players"
    elif n == 1:
        summary.note = "single player: a data point, not a band"
    elif n == 2:
        summary.note = "only two players: treat the band with caution"
    if target_utr is not None and values:
        summary.target_utr = target_utr
        summary.recruited_above_target = sum(1 for v in values if v > target_utr)
        summary.target_percentile = _r2(percentile_of(target_utr, values))
        if target_high is not None:
            summary.target_utr_high = target_high
            summary.recruited_in_range = sum(1 for v in values if target_utr <= v <= target_high)
            summary.recruited_above_high = sum(1 for v in values if v > target_high)
            summary.target_percentile_high = _r2(percentile_of(target_high, values))
            median = statistics.median(values)
            summary.fit = "reach" if median > target_high else "safety" if median < target_utr else "target"
    return summary


def _favourability(s: SchoolSummary) -> float:
    if s.target_percentile_high is not None:
        return ((s.target_percentile or 0) + s.target_percentile_high) / 2
    return s.target_percentile or 0


def rank_for_target(summaries: list[SchoolSummary]) -> list[SchoolSummary]:
    """Most favourable first: highest target percentile (range midpoint), then fewest recruited above."""
    scored = [s for s in summaries if s.target_percentile is not None]
    return sorted(
        scored,
        key=lambda s: (
            -_favourability(s),
            s.recruited_above_high if s.recruited_above_high is not None else s.recruited_above_target or 0,
            -s.n,
        ),
    )
