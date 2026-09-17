"""Offline tests: date logic, analysis rules, client retry/auth behaviour, CLI end to end."""

from __future__ import annotations

import functools
import json
from datetime import date, timedelta
from typing import Any

import httpx
import pytest

from utr_recruited_at import cli
from utr_recruited_at.analysis import build_player_row, percentile_of, rank_for_target, summarise_school
from utr_recruited_at.benchmark import benchmark_date, normalize_class
from utr_recruited_at.cache import DiskCache
from utr_recruited_at.client import (
    ProfileFetchError,
    ScaleDriftError,
    SessionExpiredError,
    UTRClient,
    load_session_token,
)

# Hand-verified UNC Wilmington values (2026-27 roster).
UNCW = [
    # id, name, class, recruited-at, benchmark date, current
    (5260045, "Gabriel Jacquemin", "Freshman", 11.83, "2025-09-08", 12.43),
    (4232316, "David Castellanos Mogollon", "Freshman", 11.31, "2025-09-08", 11.47),
    (2554674, "Jose Pablo Coello", "Freshman", 11.30, "2025-09-08", 11.79),
    (1917583, "Alejandro Peña", "Freshman", 10.74, "2025-09-08", 11.92),
    (1963017, "Tomas Marin", "Sophomore", 11.49, "2024-09-09", 12.32),
    (970607, "Noah Zeiger", "Sophomore", 10.47, "2024-09-09", 12.58),
]
NEWEST = date(2026, 9, 7)


def weekly_history(
    anchor: str, anchor_rating: float, current: float, start: date = date(2019, 1, 7), end: date = NEWEST
) -> list[dict[str, Any]]:
    """Monday snapshots, newest first, passing exactly through the anchor and the current value."""
    anchor_d = date.fromisoformat(anchor)
    out = []
    d = start
    while d <= end:
        if d <= anchor_d:
            r = anchor_rating - 0.002 * (anchor_d - d).days / 7
        else:
            r = anchor_rating + (current - anchor_rating) * (d - anchor_d).days / (end - anchor_d).days
        if d == anchor_d:
            r = anchor_rating
        out.append({"rating": round(r, 2), "type": "WeeklyAverage_Singles", "date": f"{d.isoformat()}T00:00:00"})
        out.append({"rating": 9.0, "type": "WeeklyAverage_Doubles", "date": f"{d.isoformat()}T00:00:00"})
        d += timedelta(days=7)
    return list(reversed(out))


def profile(pid: int, name: str, history: list[dict[str, Any]], shown: float | None = None) -> dict[str, Any]:
    singles = [h for h in history if h["type"] == "WeeklyAverage_Singles"]
    newest = singles[0]["rating"] if singles else 0.0
    shown = newest if shown is None else shown
    return {
        "id": pid, "displayName": name, "singlesUtr": shown, "singlesUtrDisplay": f"{shown:.2f}",
        "extendedRatingProfile": {"history": history},
    }


# ---------------------------------------------------------------- benchmark dates


def test_benchmark_dates_derive_from_season():
    assert benchmark_date("2026-27", "fr") == date(2025, 9, 8)
    assert benchmark_date("2026-27", "so") == date(2024, 9, 8)
    assert benchmark_date("2026-27", "jr") == date(2023, 9, 8)
    assert benchmark_date("2026-27", "sr") == date(2022, 9, 8)
    assert benchmark_date("2027-28", "fr") == date(2026, 9, 8)


def test_class_normalisation():
    assert normalize_class("Freshman") == "fr"
    assert normalize_class("Graduate Student") == "gr"
    assert normalize_class("Wizard") is None


# ------------------------------------------------------------ UNCW regression rows


@pytest.mark.parametrize("pid,name,cls,recruited,bdate,current", UNCW)
def test_uncw_regression_offline(pid, name, cls, recruited, bdate, current):
    hist = weekly_history(bdate, recruited, current)
    row = build_player_row(
        school="UNC Wilmington", college_id=1071, player_id=pid, name=name,
        class_code=normalize_class(cls), season_name="2026-27", profile=profile(pid, name, hist),
    )
    assert row.included, row.reason
    assert row.recruited_at_utr == recruited
    assert row.recruited_at_date == bdate
    assert row.current_utr == pytest.approx(current, abs=0.01)
    assert row.delta == pytest.approx(current - recruited, abs=0.011)
    assert row.years_elapsed == pytest.approx((NEWEST - date.fromisoformat(bdate)).days / 365.25, abs=0.01)


def test_short_history_excluded_with_reason():
    # Edoardo Santoni pattern: 48 entries starting Oct 2025.
    hist = weekly_history("2025-10-06", 10.9, 11.4, start=date(2025, 10, 6))
    assert len([h for h in hist if h["type"] == "WeeklyAverage_Singles"]) < 100
    row = build_player_row(school="Charleston", college_id=1240, player_id=1, name="Short History",
                           class_code="fr", season_name="2026-27", profile=profile(1, "x", hist))
    assert not row.included
    assert row.recruited_at_utr is None
    assert "weekly singles entries" in (row.reason or "")


def test_distant_nearest_entry_marked_unreliable():
    hist = weekly_history("2025-09-08", 11.0, 11.5)
    singles = [h for h in hist if h["type"] == "WeeklyAverage_Singles"]
    gappy = [h for h in singles if not ("2025-06-01" <= h["date"][:10] <= "2025-12-01")]
    row = build_player_row(school="S", college_id=1, player_id=2, name="Gap", class_code="fr",
                           season_name="2026-27", profile=profile(2, "Gap", gappy))
    assert not row.included
    assert "unreliable" in row.flags
    assert row.recruited_at_utr is None


def test_walk_on_flagged_not_excluded():
    hist = weekly_history("2025-09-08", 7.4, 8.9)
    row = build_player_row(school="S", college_id=1, player_id=3, name="Walk On", class_code="fr",
                           season_name="2026-27", profile=profile(3, "Walk On", hist))
    assert row.included
    assert "below-8.0-likely-walk-on" in row.flags


def test_missing_profile_is_null_with_reason():
    row = build_player_row(school="S", college_id=1, player_id=4, name="Nope", class_code="so",
                           season_name="2026-27", profile=None, fetch_error="profile fetch failed: HTTP 429")
    assert not row.included and row.recruited_at_utr is None and "429" in (row.reason or "")


def test_school_summary_and_target():
    rows = [
        build_player_row(school="UNCW", college_id=1071, player_id=pid, name=n, class_code=normalize_class(c),
                         season_name="2026-27", profile=profile(pid, n, weekly_history(bd, r, cur)))
        for pid, n, c, r, bd, cur in UNCW
    ]
    s = summarise_school("UNCW", 1071, rows, target_utr=11.4)
    assert (s.n, s.band_min, s.band_max) == (6, 10.47, 11.83)
    assert s.median == pytest.approx((11.30 + 11.31) / 2, abs=0.005)
    assert s.recruited_above_target == 2
    assert s.target_percentile == pytest.approx(100 * 4 / 6, abs=0.01)
    assert s.mean_annualised_delta is not None and s.mean_annualised_delta > 0

    single = summarise_school("Tiny", 9, rows[:1], target_utr=11.4)
    assert single.n == 1 and "data point" in (single.note or "")
    assert [x.college_id for x in rank_for_target([single, s])] == [1071, 9]


def test_school_summary_with_target_range():
    rows = [
        build_player_row(school="UNCW", college_id=1071, player_id=pid, name=n, class_code=normalize_class(c),
                         season_name="2026-27", profile=profile(pid, n, weekly_history(bd, r, cur)))
        for pid, n, c, r, bd, cur in UNCW
    ]
    s = summarise_school("UNCW", 1071, rows, target_utr=11.0, target_high=12.0)
    # recruited-at values: 10.47, 10.74, 11.30, 11.31, 11.49, 11.83
    assert (s.recruited_in_range, s.recruited_above_high, s.recruited_above_target) == (4, 0, 4)
    assert s.target_percentile == pytest.approx(100 * 2 / 6, abs=0.01)
    assert s.target_percentile_high == 100.0
    assert s.fit == "target"
    # A 9–10 recruit is below where these players were recruited (reach); a 12–13 recruit is above it (safety).
    assert summarise_school("x", 1, rows, target_utr=9.0, target_high=10.0).fit == "reach"
    assert summarise_school("x", 1, rows, target_utr=12.0, target_high=13.0).fit == "safety"


def test_cli_target_range_from_capture(tmp_path, capsys):
    path = tmp_path / "capture.json"
    path.write_text(json.dumps(uncw_capture()))
    args = cli.build_parser().parse_args(
        ["--from-capture", str(path), "--target-utr", "12.0", "11.0", "--format", "json"])
    assert cli.run(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert (data["meta"]["target_utr"], data["meta"]["target_utr_high"]) == (11.0, 12.0)
    assert data["schools"][0]["fit"] == "target"
    with pytest.raises(cli.UTRError):
        cli.run(cli.build_parser().parse_args(["--from-capture", str(path), "--target-utr", "1", "2", "3"]))


def test_percentile_ties_count_half():
    assert percentile_of(11.0, [10.0, 11.0, 12.0]) == 50.0


# ------------------------------------------------------------------ auth loading


def test_load_session_token_formats(tmp_path, monkeypatch):
    monkeypatch.delenv("UTR_SESSION_COOKIE", raising=False)
    netscape = tmp_path / "cookies.txt"
    netscape.write_text("# Netscape HTTP Cookie File\n.utrsports.net\tTRUE\t/\tTRUE\t0\tjwt\tabc.def.ghi\n")
    assert load_session_token(netscape) == "abc.def.ghi"
    js = tmp_path / "c.json"
    js.write_text(json.dumps([{"name": "_ga", "value": "1"}, {"name": "jwt", "value": "tok"}]))
    assert load_session_token(js) == "tok"
    header = tmp_path / "h.txt"
    header.write_text("Cookie: _ga=1; jwt=hdr")
    assert load_session_token(header) == "hdr"
    monkeypatch.setenv("UTR_SESSION_COOKIE", "a=b; jwt=envtok")
    assert load_session_token() == "envtok"
    monkeypatch.setenv("UTR_SESSION_COOKIE", "baretoken")
    assert load_session_token() == "baretoken"


# ------------------------------------------------------------ client behaviour


def make_client(handler, tmp_path=None, sleeps: list[float] | None = None) -> UTRClient:
    record = sleeps if sleeps is not None else []
    return UTRClient(
        "tok",
        cache=DiskCache(tmp_path) if tmp_path else None,
        sleep=record.append,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def good_profile(pid=970607):
    return profile(pid, "Noah Zeiger", weekly_history("2024-09-09", 10.47, 12.58))


def test_429_backs_off_exponentially_then_succeeds():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        assert req.headers["cookie"] == "jwt=tok"
        if calls["n"] <= 2:
            return httpx.Response(429)
        return httpx.Response(200, json=good_profile())

    sleeps: list[float] = []
    c = make_client(handler, sleeps=sleeps)
    assert c.get_player_profile(970607)["displayName"] == "Noah Zeiger"
    backoffs = [s for s in sleeps if s >= 7]
    assert backoffs[:2] == [7.0, 14.0]


def test_persistent_429_gets_long_cooldown_then_recovers():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(429) if calls["n"] <= 5 else httpx.Response(200, json=good_profile())

    sleeps: list[float] = []
    c = make_client(handler, sleeps=sleeps)
    assert c.get_player_profile(970607)["displayName"] == "Noah Zeiger"
    assert 300.0 in sleeps


def test_periodic_cooldown():
    sleeps: list[float] = []
    c = UTRClient("tok", sleep=sleeps.append, cooldown_every=3, cooldown_s=42.0,
                  http=httpx.Client(transport=httpx.MockTransport(
                      lambda req: httpx.Response(200, json=good_profile(int(req.url.path.split("/")[3]))))))
    for pid in range(1, 8):
        c.get_player_profile(pid)
    assert sleeps.count(42.0) == 2  # after requests 3 and 6


def test_200_without_history_is_retried_then_fails():
    def handler(req):
        return httpx.Response(200, json={"id": 1, "singlesUtr": 11.0, "singlesUtrDisplay": "11.00"})

    c = make_client(handler)
    with pytest.raises(ProfileFetchError, match="incomplete payload"):
        c.get_player_profile(1)


def test_401_says_session_expired_without_retry():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(401)

    with pytest.raises(SessionExpiredError):
        make_client(handler).get_player_profile(1)
    assert calls["n"] == 1


def test_masked_rating_means_unauthenticated():
    p = good_profile()
    p["singlesUtrDisplay"] = "12.xx"
    with pytest.raises(SessionExpiredError, match="masked"):
        make_client(lambda req: httpx.Response(200, json=p)).get_player_profile(970607)


def test_scale_drift_fails_loudly():
    p = good_profile()
    p["singlesUtr"] = 6.29
    p["singlesUtrDisplay"] = "6.29"
    with pytest.raises(ScaleDriftError):
        make_client(lambda req: httpx.Response(200, json=p)).get_player_profile(970607)


def test_moderate_display_mismatch_is_flagged_not_fatal():
    # Real case: Connor Van Schalkwyk showed 13.58 on his profile with a newest weekly entry of 13.79.
    p = good_profile()
    p["singlesUtr"] = 12.30
    p["singlesUtrDisplay"] = "12.30"
    got = make_client(lambda req: httpx.Response(200, json=p)).get_player_profile(970607)
    row = build_player_row(school="S", college_id=1, player_id=970607, name="Noah Zeiger", class_code="so",
                           season_name="2026-27", profile=got)
    assert row.included and row.recruited_at_utr == 10.47
    assert "profile shows 12.30, latest weekly 12.58" in row.flags


def test_profiles_are_cached_and_throttled(tmp_path):
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, json=good_profile(int(req.url.path.split("/")[3])))

    sleeps: list[float] = []
    c = make_client(handler, tmp_path, sleeps)
    c.get_player_profile(970607)
    c.get_player_profile(1963017)
    c.get_player_profile(970607)
    assert calls["n"] == 2
    assert any(4.9 < s <= 6.3 for s in sleeps), sleeps  # 5s spacing plus up to 25% jitter
    assert c.live_requests == 2
    r = c.get_rating_at_date(970607, date(2024, 9, 8))
    assert (r.rating, r.landed_on, r.gap_days) == (10.47, date(2024, 9, 9), 1)


# ----------------------------------------------------------------- CLI end to end


def fake_api(req: httpx.Request) -> httpx.Response:
    path = req.url.path
    if path == "/v2/search/colleges":
        hits = [
            {"source": {"id": 90, "name": "UNC Wilmington", "shortName": "UNC Wilmington", "conferenceId": 12,
                        "mensClubId": 1071, "womensClubId": 1072,
                        "conference": {"shortName": "CAA", "division": {"shortName": "D1"}}}},
            {"source": {"id": 1, "name": "University of Tennessee", "shortName": "Tennessee", "conferenceId": 26,
                        "mensClubId": 911}},
        ]
        return httpx.Response(200, json={"total": len(hits), "hits": hits})
    if path == "/api/web/clubs/1071/school":
        roster = [{"id": pid, "displayName": n, "classYearStatus": c, "redshirt": False} for pid, n, c, *_ in UNCW]
        roster.append({"id": 4086019, "displayName": "Jordi Domenech Castillo", "classYearStatus": "Senior"})
        return httpx.Response(200, json={"shortName": "UNC Wilmington", "rosterYear": "2026-27",
                                         "rosterId": 15970, "roster": roster})
    if path.startswith("/v1/player/"):
        pid = int(path.split("/")[3])
        for p, n, _c, r, bd, cur in UNCW:
            if p == pid:
                return httpx.Response(200, json=profile(p, n, weekly_history(bd, r, cur)))
        return httpx.Response(404)
    return httpx.Response(404)


def test_cli_conference_json(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("UTR_SESSION_COOKIE", "jwt=tok")
    monkeypatch.setattr(cli, "UTRClient", functools.partial(
        UTRClient, sleep=lambda s: None, http=httpx.Client(transport=httpx.MockTransport(fake_api))))
    args = cli.build_parser().parse_args(
        ["--conference", "12", "--gender", "M", "--target-utr", "11.4", "--format", "json",
         "--cache-dir", str(tmp_path)])
    assert cli.run(args) == 0
    data = json.loads(capsys.readouterr().out)
    got = {p["player_id"]: (p["recruited_at_utr"], p["recruited_at_date"]) for p in data["players"]}
    assert got == {pid: (r, bd) for pid, _n, _c, r, bd, _cur in UNCW}
    assert 4086019 not in got  # senior excluded by default
    (school,) = data["schools"]
    assert school["n"] == 6 and school["recruited_above_target"] == 2
    assert data["ranking"] == [1071]


def test_parse_utr_urls():
    s = cli.parse_utr_urls(["https://app.utrsports.net/search?type=colleges&conference=12&gender=M"])
    assert (s.conference, s.gender, s.colleges) == (12, "M", [])
    s = cli.parse_utr_urls([
        "https://app.utrsports.net/college/1071?tab=roster",
        "app.utrsports.net/college/1039",
        "https://app.utrsports.net/profiles/970607",
    ])
    assert (s.conference, s.colleges, s.target_player) == (None, [1071, 1039], 970607)
    assert cli.parse_utr_urls(["https://app.utrsports.net/search?type=colleges&conference=12&gender=F"]).gender == "W"
    for bad in (["https://example.com/college/1071"], ["https://app.utrsports.net/events/5"],
                ["https://app.utrsports.net/profiles/1"]):
        with pytest.raises(cli.UTRError):
            cli.parse_utr_urls(bad)


CORNER_D1 = (
    "https://app.utrsports.net/college-corner/teams?conference&distance=50mi&division=1&lat&limit=10&lng"
    "&location=&locationCountryName=&locationInputValue=&locationStateName=&order=desc&page=1&sort=power6"
    "&utrFitPosition=6&utrFitRating=&utrReliability="
)


def test_parse_college_corner_division_url():
    s = cli.parse_utr_urls([CORNER_D1])
    assert (s.division, s.conference, s.colleges, s.notes) == (1, None, [], [])
    s = cli.parse_utr_urls([CORNER_D1.replace("conference&", "conference=12&").replace("&lat&", "&lat=35.1&")])
    assert (s.division, s.conference) == (1, 12) and "lat" in s.notes[0]
    with pytest.raises(cli.UTRError, match="every college"):
        cli.parse_utr_urls(["https://app.utrsports.net/college-corner/teams?division=&conference="])


def test_cli_division_url_skips_broken_roster_and_caps_teams(monkeypatch, tmp_path, capsys):
    def api(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v2/search/colleges":
            d1 = {"shortName": "D1", "id": 1}
            hits = [
                {"source": {"id": 90, "shortName": "UNC Wilmington", "conferenceId": 12, "mensClubId": 1071,
                            "conference": {"shortName": "CAA", "division": d1},
                            "activeRosters": [{"club": {"id": 1071}, "power6": {"power6Rating": 74.5}}]}},
                {"source": {"id": 2, "shortName": "Ghost U", "conferenceId": 12, "mensClubId": 5555,
                            "conference": {"shortName": "CAA", "division": d1},
                            "activeRosters": [{"club": {"id": 5555}, "power6": {"power6Rating": 80.0}}]}},
                {"source": {"id": 3, "shortName": "Weak U", "conferenceId": 12, "mensClubId": 6666,
                            "conference": {"shortName": "CAA", "division": d1}}},
                {"source": {"id": 4, "shortName": "D3 College", "conferenceId": 99, "mensClubId": 7777,
                            "conference": {"shortName": "X", "division": {"shortName": "D3", "id": 3}}}},
            ]
            return httpx.Response(200, json={"total": len(hits), "hits": hits})
        if req.url.path == "/api/web/clubs/5555/school":
            return httpx.Response(200, json={"shortName": "Ghost U", "roster": []})  # no rosterYear
        return fake_api(req)

    monkeypatch.setenv("UTR_SESSION_COOKIE", "jwt=tok")
    monkeypatch.setattr(cli, "UTRClient", functools.partial(
        UTRClient, sleep=lambda s: None, http=httpx.Client(transport=httpx.MockTransport(api))))
    args = cli.build_parser().parse_args(
        ["--url", CORNER_D1, "--max-teams", "2", "--format", "json", "--cache-dir", str(tmp_path)])
    assert cli.run(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["meta"]["colleges"] == [5555, 1071]  # strongest Power 6 first, capped at 2
    ghost, uncw = data["schools"]
    assert ghost["n"] == 0 and "roster unavailable" in ghost["note"]
    assert uncw["n"] == 6


def test_cli_url_with_profile_target(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("UTR_SESSION_COOKIE", "jwt=tok")
    monkeypatch.setattr(cli, "UTRClient", functools.partial(
        UTRClient, sleep=lambda s: None, http=httpx.Client(transport=httpx.MockTransport(fake_api))))
    args = cli.build_parser().parse_args(
        ["--url", "https://app.utrsports.net/college/1071?tab=roster", "https://app.utrsports.net/profiles/970607",
         "--format", "json", "--cache-dir", str(tmp_path)])
    assert cli.run(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["meta"]["target_player_id"] == 970607
    assert data["meta"]["target_utr"] == pytest.approx(12.58, abs=0.01)
    assert data["schools"][0]["recruited_above_target"] == 0


def uncw_capture() -> dict[str, Any]:
    players = [
        {"id": pid, "name": n, "class": c, "redshirt": False, "count": 300,
         "nearest": [bd, r], "newest": ["2026-09-07", cur], "singles_utr": cur, "display": f"{cur:.2f}"}
        for pid, n, c, r, bd, cur in UNCW
    ]
    players += [
        {"id": 1, "name": "Short History", "class": "Freshman", "count": 48,
         "nearest": ["2025-10-06", 10.9], "newest": ["2026-09-07", 11.4], "singles_utr": 11.4, "display": "11.40"},
        {"id": 2, "name": "Failed Fetch", "class": "Sophomore", "error": "profile fetch failed: HTTP 429 after retries"},
        {"id": 3, "name": "Far Gap", "class": "Freshman", "count": 150,
         "nearest": ["2025-12-29", 11.0], "newest": ["2026-09-07", 11.5], "singles_utr": 11.5, "display": "11.50"},
        {"id": 4, "name": "A Senior", "class": "Senior", "count": 400,
         "nearest": ["2022-09-05", 11.0], "newest": ["2026-09-07", 12.0], "singles_utr": 12.0, "display": "12.00"},
    ]
    return {"captured_at": "2026-09-13T12:00:00Z", "schools": [
        {"club_id": 1071, "school": "UNC Wilmington", "season": "2026-27", "players": players},
        {"club_id": 5555, "school": "5555", "season": None, "error": "no active roster season", "players": []},
    ]}


def test_analyse_capture_applies_the_same_rules():
    from utr_recruited_at.capture import analyse_capture

    players, summaries = analyse_capture(uncw_capture(), ["fr", "so"], target_utr=11.4)
    by_id = {p.player_id: p for p in players}
    for pid, _n, _c, r, bd, cur in UNCW:
        assert (by_id[pid].recruited_at_utr, by_id[pid].recruited_at_date, by_id[pid].current_utr) == (r, bd, cur)
    assert not by_id[1].included and "48" in by_id[1].reason
    assert not by_id[2].included and "429" in by_id[2].reason
    assert not by_id[3].included and "unreliable" in by_id[3].flags
    assert 4 not in by_id  # seniors filtered out
    uncw, missing = summaries
    assert (uncw.n, uncw.band_min, uncw.band_max, uncw.recruited_above_target) == (6, 10.47, 11.83, 2)
    assert missing.n == 0 and "roster unavailable" in missing.note


def test_capture_masked_ratings_rejected():
    from utr_recruited_at.capture import analyse_capture

    cap = uncw_capture()
    cap["schools"][0]["players"][0]["display"] = "12.xx"
    with pytest.raises(SessionExpiredError):
        analyse_capture(cap, ["fr", "so"], None)


def test_cli_from_capture_needs_no_token(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("UTR_SESSION_COOKIE", raising=False)
    path = tmp_path / "capture.json"
    path.write_text(json.dumps(uncw_capture()))
    args = cli.build_parser().parse_args(["--from-capture", str(path), "--target-utr", "11.4", "--format", "json"])
    assert cli.run(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["meta"]["source"] == "browser capture"
    assert data["ranking"] == [1071]


def test_cli_without_token_exits_cleanly(monkeypatch, tmp_path):
    monkeypatch.delenv("UTR_SESSION_COOKIE", raising=False)
    args = cli.build_parser().parse_args(["--college", "1071", "--cache-dir", str(tmp_path)])
    assert cli.run(args) == 2
