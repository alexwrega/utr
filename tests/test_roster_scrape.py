"""Offline tests for the browser scraper's logic, with the browser itself faked.

Playwright and a real UTR session are out of scope here: `BrowserSession` is deliberately
thin so everything that decides behaviour — pacing, resume, filtering, what counts as a
failure — is testable with a stand-in.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest

from utr_recruited_at import scrape_cli
from utr_recruited_at.cache import DiskCache
from utr_recruited_at.client import UTRError
from utr_recruited_at.roster_scrape import (
    DELAY_FLOOR_S,
    RateLimited,
    Target,
    already_captured,
    build_row,
    roster_targets,
    sample_delay,
    scrape_rosters,
    sleep_in_slices,
    warm_cache,
)

from .test_player_page import full_payload, masked_payload


def roster_payload(*players: dict[str, Any]) -> dict[str, Any]:
    return {"rosterYear": "2026-27", "shortName": "UNC Wilmington", "roster": list(players)}


def player(pid: int, name: str, cls: str = "Freshman", **over: Any) -> dict[str, Any]:
    return {"id": pid, "displayName": name, "classYearStatus": cls, **over}


class FakeSession:
    """Stands in for BrowserSession: same three methods the loop calls."""

    def __init__(self, roster: dict[str, Any], profiles: dict[int, Any] | None = None) -> None:
        self._roster = roster
        self._profiles = profiles or {}
        self.visited: list[int] = []

    def fetch_roster(self, club_id: int) -> dict[str, Any]:
        return self._roster

    def fetch_player(self, player_id: int) -> tuple[dict[str, Any], str]:
        self.visited.append(player_id)
        result = self._profiles.get(player_id, full_payload(id=player_id))
        if isinstance(result, Exception):
            raise result
        return result, f"visible text for {player_id}"


# ------------------------------------------------------------------------- pacing


def test_delays_land_inside_the_requested_window():
    rng = random.Random(7)
    draws = [sample_delay(60, 300, rng) for _ in range(500)]
    assert all(60 <= d <= 300 for d in draws)
    # Uniform, not a constant or a clustered value.
    assert max(draws) - min(draws) > 200
    assert len(set(round(d) for d in draws)) > 100


def test_a_delay_under_the_floor_is_refused():
    with pytest.raises(UTRError, match="below the 30s floor"):
        sample_delay(5, 300)


def test_an_inverted_window_is_refused():
    with pytest.raises(UTRError, match="above --max-delay"):
        sample_delay(300, 60)


def test_waiting_is_sliced_so_ctrl_c_lands_quickly():
    slices: list[float] = []
    sleep_in_slices(5.0, slices.append)
    assert sum(slices) == pytest.approx(5.0)
    assert max(slices) <= 1.0  # never one long uninterruptible sleep


def test_the_loop_waits_between_players_but_not_after_the_last():
    waits: list[float] = []
    session = FakeSession(roster_payload(player(1, "A"), player(2, "B"), player(3, "C")))
    progress = scrape_rosters(
        session, [1071], Path("/dev/null"), min_delay=60, max_delay=300,
        rng=random.Random(1), sleep=waits.append, cache=None,
    )
    assert progress.captured == 3
    assert len(progress.delays) == 2  # three players, two gaps
    assert all(60 <= d <= 300 for d in progress.delays)
    assert sum(waits) == pytest.approx(sum(progress.delays), abs=1.0)


# ------------------------------------------------------------------- roster reading


def test_roster_targets_read_names_classes_and_redshirts():
    payload = roster_payload(
        player(1, "A", "Freshman"),
        player(2, "B", "Sophomore", redshirt=True),
        {"id": 3, "firstName": "C", "lastName": "D", "classYearStatus": "Junior"},
        {"displayName": "no id"},
    )
    targets = roster_targets(payload)
    assert [t.player_id for t in targets] == [1, 2, 3]
    assert [t.class_code for t in targets] == ["fr", "so", "jr"]
    assert targets[1].redshirt is True
    assert targets[2].name == "C D"


def test_roster_targets_filter_by_class():
    payload = roster_payload(player(1, "A", "Freshman"), player(2, "B", "Senior"))
    assert [t.player_id for t in roster_targets(payload, ["fr", "so"])] == [1]


# ------------------------------------------------------------------ resume & output


def test_rows_are_written_one_per_player_as_they_land(tmp_path):
    out = tmp_path / "roster.jsonl"
    session = FakeSession(roster_payload(player(1, "A"), player(2, "B")))
    scrape_rosters(session, [1071], out, min_delay=60, max_delay=300,
                   rng=random.Random(2), sleep=lambda _s: None)
    lines = out.read_text().strip().splitlines()
    assert len(lines) == 2
    row = json.loads(lines[0])
    assert row["player_id"] == 1 and row["club_id"] == 1071
    assert row["name"] == "A" and row["class_code"] == "fr"
    assert row["profile"]["id"] == 1
    assert row["visible_text"].startswith("visible text")
    assert row["masked"] is False


def test_a_rerun_skips_players_already_in_the_file(tmp_path):
    out = tmp_path / "roster.jsonl"
    roster = roster_payload(player(1, "A"), player(2, "B"), player(3, "C"))
    first = FakeSession(roster)
    scrape_rosters(first, [1071], out, max_players=2, min_delay=60, max_delay=300,
                   rng=random.Random(3), sleep=lambda _s: None)
    assert first.visited == [1, 2]

    second = FakeSession(roster)
    progress = scrape_rosters(second, [1071], out, min_delay=60, max_delay=300,
                              rng=random.Random(3), sleep=lambda _s: None)
    assert second.visited == [3]  # only the one that was missing
    assert progress.skipped == 2


def test_a_half_written_final_line_does_not_break_resume(tmp_path):
    out = tmp_path / "roster.jsonl"
    out.write_text(json.dumps({"player_id": 1}) + "\n" + '{"player_id": 2, "prof')
    assert already_captured(out) == {1}


def test_a_failed_player_is_recorded_and_retried_next_run(tmp_path):
    out = tmp_path / "roster.jsonl"
    session = FakeSession(roster_payload(player(1, "A"), player(2, "B")),
                          {1: UTRError("profile 1 returned nothing usable")})
    progress = scrape_rosters(session, [1071], out, min_delay=60, max_delay=300,
                              rng=random.Random(4), sleep=lambda _s: None)
    assert (progress.captured, progress.failed) == (1, 1)
    rows = [json.loads(line) for line in out.read_text().strip().splitlines()]
    assert rows[0]["error"].startswith("profile 1")
    assert "profile" not in rows[0]
    # An error row is not a capture, so the next run tries that player again.
    assert already_captured(out) == {2}


def test_rate_limiting_stops_the_whole_run(tmp_path):
    out = tmp_path / "roster.jsonl"
    session = FakeSession(roster_payload(player(1, "A"), player(2, "B"), player(3, "C")),
                          {2: RateLimited("UTR returned HTTP 429")})
    with pytest.raises(RateLimited):
        scrape_rosters(session, [1071], out, min_delay=60, max_delay=300,
                       rng=random.Random(5), sleep=lambda _s: None)
    assert session.visited == [1, 2]  # never reached the third
    assert len(out.read_text().strip().splitlines()) == 1  # the first player is kept


# ------------------------------------------------------------------------- caching


def test_only_unmasked_profiles_reach_the_shared_cache(tmp_path):
    out, cache_dir = tmp_path / "roster.jsonl", tmp_path / "cache"
    cache = DiskCache(cache_dir)
    session = FakeSession(roster_payload(player(1, "A"), player(2, "B")),
                          {2: masked_payload(id=2)})
    scrape_rosters(session, [1071], out, min_delay=60, max_delay=300,
                   rng=random.Random(6), sleep=lambda _s: None, cache=cache)
    assert cache.get("profiles", "1") is not None
    # A masked profile is not a real one; the benchmark must never read it as such.
    assert cache.get("profiles", "2") is None


def test_warm_cache_replays_a_capture_file(tmp_path):
    out, cache = tmp_path / "roster.jsonl", DiskCache(tmp_path / "cache")
    session = FakeSession(roster_payload(player(1, "A"), player(2, "B")))
    scrape_rosters(session, [1071], out, min_delay=60, max_delay=300,
                   rng=random.Random(8), sleep=lambda _s: None, cache=None)
    assert warm_cache(out, cache) == 2
    assert cache.get("profiles", "1")["id"] == 1


def test_a_player_on_two_rosters_is_scraped_once(tmp_path):
    out = tmp_path / "roster.jsonl"
    session = FakeSession(roster_payload(player(1, "A"), player(2, "B")))
    progress = scrape_rosters(session, [1071, 1071], out, min_delay=60, max_delay=300,
                              rng=random.Random(9), sleep=lambda _s: None)
    assert session.visited == [1, 2]
    assert progress.captured == 2


def test_masked_rows_are_flagged():
    row = build_row(1071, Target(9, "X"), masked_payload(id=9), "text")
    assert row["masked"] is True


# ----------------------------------------------------------------------------- CLI


@pytest.mark.parametrize(
    "raw,expected",
    [("1071", 1071),
     ("https://app.utrsports.net/college/1071", 1071),
     ("app.utrsports.net/college/1071?tab=roster", 1071)],
)
def test_parse_club_ref(raw, expected):
    assert scrape_cli.parse_club_ref(raw) == expected


@pytest.mark.parametrize("raw", ["https://example.com/college/1", "app.utrsports.net/profiles/970607", "UNCW"])
def test_parse_club_ref_rejects_anything_else(raw):
    with pytest.raises(UTRError):
        scrape_cli.parse_club_ref(raw)


def test_cli_defaults_are_the_requested_window():
    args = scrape_cli.build_parser().parse_args(["1071"])
    assert (args.min_delay, args.max_delay) == (60.0, 300.0)
    assert args.headless is False  # you need to see the window to sign in
    assert sample_delay(args.min_delay, args.max_delay, random.Random(0)) >= DELAY_FLOOR_S


def test_cli_warm_cache_only_needs_no_browser(tmp_path, capsys):
    out = tmp_path / "roster.jsonl"
    out.write_text(json.dumps({"player_id": 1, "masked": False, "profile": full_payload(id=1)}) + "\n")
    args = scrape_cli.build_parser().parse_args(
        ["1071", "--out", str(out), "--warm-cache-only", "--cache-dir", str(tmp_path / "c")])
    assert scrape_cli.run(args) == 0
    assert "1 complete profile" in capsys.readouterr().err
