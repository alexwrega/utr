"""Analyse results captured by browser/runner.js inside a signed-in browser tab.

The runner keeps only what the rules need per player: the weekly singles entry nearest the
benchmark date, the newest entry, the total entry count and the displayed UTR. Every rule
(history length, 60-day gap, walk-on flag, scale check) is still applied here, not in the page.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .analysis import PlayerRow, SchoolSummary, build_player_row, summarise_school
from .benchmark import SINGLES_TYPE, normalize_class
from .client import UTRClient, UTRError


def load_capture(path: Path) -> dict[str, Any]:
    data = json.loads(path.expanduser().read_text())
    if not isinstance(data, dict) or not isinstance(data.get("schools"), list):
        raise UTRError(f"{path} isn't a browser capture (expected a top-level 'schools' list).")
    return data


def _entry(pair: list[Any]) -> dict[str, Any]:
    return {"date": f"{pair[0]}T00:00:00", "rating": pair[1], "type": SINGLES_TYPE}


def profile_from_capture(player: dict[str, Any]) -> dict[str, Any] | None:
    if player.get("error"):
        return None
    history = []
    if player.get("newest"):
        history.append(_entry(player["newest"]))
    if player.get("nearest") and player["nearest"] != player.get("newest"):
        history.append(_entry(player["nearest"]))
    return {
        "id": player["id"],
        "displayName": player.get("name"),
        "singlesUtr": player.get("singles_utr"),
        "singlesUtrDisplay": player.get("display") or "",
        "extendedRatingProfile": {"history": history},
    }


def analyse_capture(
    capture: dict[str, Any], classes: list[str], target_utr: float | None, target_high: float | None = None
) -> tuple[list[PlayerRow], list[SchoolSummary]]:
    players: list[PlayerRow] = []
    summaries: list[SchoolSummary] = []
    for school in capture["schools"]:
        club_id = int(school["club_id"])
        name = school.get("school") or str(club_id)
        if school.get("error") or not school.get("season"):
            summaries.append(SchoolSummary(
                school=name, college_id=club_id, n=0, band_min=None, band_max=None, median=None,
                mean_annualised_delta=None, note=f"roster unavailable: {school.get('error') or 'no season'}",
            ))
            continue
        rows: list[PlayerRow] = []
        for p in school.get("players") or []:
            code = normalize_class(p.get("class"))
            if code not in classes:
                continue
            profile = profile_from_capture(p)
            if profile is not None:
                UTRClient._check_scale(int(p["id"]), profile)
            row = build_player_row(
                school=name, college_id=club_id, player_id=int(p["id"]), name=p.get("name") or "",
                class_code=code, season_name=school["season"], profile=profile,
                fetch_error=p.get("error"), history_count=p.get("count"),
            )
            if p.get("redshirt"):
                row.flags.append("redshirt")
            rows.append(row)
        players.extend(rows)
        summaries.append(summarise_school(name, club_id, rows, target_utr, target_high))
    return players, summaries
