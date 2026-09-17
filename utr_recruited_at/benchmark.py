"""Pure date logic: which September a roster player was "recruited at"."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

# Offset from the roster season's start year to the enrolment year.
CLASS_OFFSETS: dict[str, int] = {
    "fr": 0,
    "so": 1,
    "jr": 2,
    "sr": 3,
    "gr": 4,
}

CLASS_ALIASES: dict[str, str] = {
    "freshman": "fr",
    "redshirt freshman": "fr",
    "sophomore": "so",
    "junior": "jr",
    "senior": "sr",
    "graduate": "gr",
    "graduate student": "gr",
    "fifth year": "gr",
}

SINGLES_TYPE = "WeeklyAverage_Singles"
# UTR's weekly snapshots land on Mondays; the 8th puts the target in the first full
# week of September, which is where the hand-verified dates (2025-09-08, 2024-09-09) sit.
BENCHMARK_DAY = 8
MAX_GAP_DAYS = 60


def normalize_class(raw: str | None) -> str | None:
    if not raw:
        return None
    key = raw.strip().lower()
    if key in CLASS_OFFSETS:
        return key
    return CLASS_ALIASES.get(key)


def season_start_year(season_name: str) -> int:
    """'2026-27' -> 2026."""
    m = re.match(r"\s*(\d{4})", season_name)
    if not m:
        raise ValueError(f"Unrecognised roster season name: {season_name!r}")
    return int(m.group(1))


def benchmark_date(season_name: str, class_code: str) -> date:
    enrolled = season_start_year(season_name) - CLASS_OFFSETS[class_code]
    return date(enrolled - 1, 9, BENCHMARK_DAY)


def parse_utr_date(raw: str) -> date:
    return datetime.fromisoformat(raw.replace("Z", "")).date()


def singles_history(profile: dict[str, Any]) -> list[dict[str, Any]]:
    erp = profile.get("extendedRatingProfile") or {}
    return [h for h in (erp.get("history") or []) if h.get("type") == SINGLES_TYPE]


def nearest_entry(history: list[dict[str, Any]], target: date) -> tuple[dict[str, Any], int] | None:
    """Nearest snapshot to `target` and its distance in days. No interpolation."""
    best: tuple[dict[str, Any], int] | None = None
    for entry in history:
        if entry.get("rating") is None:
            continue
        gap = abs((parse_utr_date(entry["date"]) - target).days)
        if best is None or gap < best[1]:
            best = (entry, gap)
    return best
