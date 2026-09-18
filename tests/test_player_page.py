"""Offline tests for the player-page generator: extraction, masking, rendering, CLI."""

from __future__ import annotations

import functools
from typing import Any

import httpx
import pytest

from utr_recruited_at import page_cli
from utr_recruited_at.client import ProfileFetchError, SessionExpiredError, UTRClient, UTRError
from utr_recruited_at.cache import DiskCache
from utr_recruited_at.player_page import extract, locked_fields, render_html

from .test_offline import weekly_history


def masked_payload(**over: Any) -> dict[str, Any]:
    """The shape UTR serves an unauthenticated caller: rating masked, history withheld."""
    payload = {
        "id": 1475065,
        "profileId": 950094,
        "displayName": "Joachim Szamborski",
        "nationality": "POL",
        "ageRange": "14-18",
        "claimed": True,
        "collegeRecruiting": True,
        "isCollegeRecruitingAge": True,
        "location": {"display": "Warsaw, Poland"},
        "singlesUtr": 11.0,
        "singlesUtrDisplay": "11.xx",
        "doublesUtr": 11.0,
        "doublesUtrDisplay": "11.xx",
        "ratingStatusSingles": "Rated",
        "ratingProgressSingles": 100.0,
        "ratingStatusDoubles": "Rated",
        "gender": "Male",
        "heightInches": 74,
        "dominantHand": "Right-Handed",
        "backhand": "Two-Handed",
        "birthPlace": "Warsaw",
        "displayCollegeProspectBadge": True,
        "ratingChoice": "UTRRating",
        "myUtrSingles": 11.0,
        "myUtrSinglesDisplay": "11.xx",
        "myUtrStatusSingles": "Rated",
        "myUtrProgressSingles": 100.0,
        "myUtrDoubles": 11.0,
        "myUtrDoublesDisplay": "11.xx",
        "myUtrStatusDoubles": "Rated",
        "threeMonthRating": 11.26,
        "threeMonthRatingChangeDetails": {"ratingDifference": 0.13, "changeDirection": "down"},
        "historicRatings": {"historicSinglesRating": None},
        "totalResults": 426,
        "resultCountsSingles": [{"key": 2025, "value": 59}, {"key": 2026, "value": 28}],
        "resultCountsDoubles": [{"key": 2025, "value": 20}, {"key": 2026, "value": 4}],
        "extendedRatingProfile": {"history": None, "eligibleResults": {"singles": [1] * 30, "doubles": [1] * 8}},
    }
    payload.update(over)
    return payload


def full_payload(**over: Any) -> dict[str, Any]:
    """What the same endpoint returns with a session: real decimals and weekly history."""
    payload = {
        "id": 970607,
        "displayName": "Noah Zeiger",
        "singlesUtr": 12.0,
        "singlesUtrDisplay": "12.00",
        "doublesUtr": 11.4,
        "doublesUtrDisplay": "11.40",
        "ratingStatusSingles": "Rated",
        "ratingProgressSingles": 100.0,
        "threeMonthRating": 12.68,
        "threeMonthRatingChangeDetails": {"ratingDifference": -0.10, "changeDirection": "up"},
        "historicRatings": {
            "historicSinglesRating": 8.0,
            "historicSinglesRatingReliability": 10.0,
            "historicSinglesRatingDate": "2023-09-10T00:00:00",
        },
        "totalResults": 12,
        "resultCountsSingles": [{"key": 2025, "value": 7}],
        "resultCountsDoubles": [{"key": 2025, "value": 5}],
        "playerCollege": {
            "name": "UNC Wilmington",
            "displayName": "UNC Wilmington",
            "gender": {"male": True, "female": False},
            "power6Men": 75.0,
            "conference": {"shortName": "CAA", "division": {"shortName": "D1"}},
            "top6UtrMale": [
                {"position": 1, "rating": 12.8}, {"position": 2, "rating": 12.5},
                {"position": 3, "rating": 12.1}, {"position": 4, "rating": 11.9},
                {"position": 5, "rating": 11.6}, {"position": 6, "rating": 11.2},
            ],
        },
        "playerCollegeDetails": {"gradClassName": "Sophomore"},
        "extendedRatingProfile": {
            "history": weekly_history("2024-09-09", 10.47, 12.0),
            "eligibleResults": {"singles": [1] * 20, "doubles": []},
        },
    }
    payload.update(over)
    return payload


# ------------------------------------------------------------------ extraction


def test_masked_payload_extracts_what_is_public_and_flags_the_rest():
    page = extract(masked_payload(), fetched_at="18 September 2026")
    assert page.authenticated is False
    assert page.singles.masked and page.singles.display == "11.xx"
    assert page.singles.known is None
    assert page.three_month == 11.26
    # The masked rating is unusable, so the 3-month figure stands in as the estimate.
    assert page.rating_estimate == 11.26
    assert (page.singles_results, page.doubles_results) == (87, 24)
    assert (page.eligible_singles, page.eligible_doubles) == (30, 8)
    assert page.history == []
    assert page.college is None
    assert [i["what"] for i in locked_fields(page)] == [
        "Weekly UTR trajectory",
        "Unmasked singles & doubles rating",
        "Match-by-match results",
        "Historic rating anchors",
    ]


@pytest.mark.parametrize(
    "difference,direction,expected",
    [
        # UTR's ratingDifference carries the sign opposite to changeDirection, so the
        # direction wins and the difference is read as a magnitude.
        (0.13, "down", -0.13),
        (-0.10, "up", 0.10),
        (0.0, "n/a", None),
        (0.4, "", None),
    ],
)
def test_three_month_direction_beats_the_difference_sign(difference, direction, expected):
    page = extract(masked_payload(
        threeMonthRatingChangeDetails={"ratingDifference": difference, "changeDirection": direction}))
    assert page.three_month_change == expected


def test_full_payload_leaves_nothing_locked():
    page = extract(full_payload())
    assert page.authenticated is True
    assert page.singles.known == 12.0
    assert page.historic_singles == 8.0 and page.historic_singles_date == "2023-09-10"
    assert len(page.history) > 100
    assert page.history == sorted(page.history)  # oldest first, for the chart's x axis
    assert locked_fields(page) == []


def test_lineup_projection_counts_the_players_rated_above():
    page = extract(full_payload())
    assert page.college is not None
    assert page.college.name == "UNC Wilmington"
    assert page.college.conference == "CAA" and page.college.division == "D1"
    # 12.80, 12.50 and 12.10 are above 12.00; the next slot is 4.
    assert page.college.projected_position == 4
    assert page.college.projected_basis == "current UTR"


def test_projection_falls_back_to_the_three_month_rating_when_masked():
    payload = full_payload(singlesUtr=11.0, singlesUtrDisplay="11.xx", threeMonthRating=11.7)
    page = extract(payload)
    # 11.70 sits behind 12.80, 12.50, 12.10 and 11.90 — five deep.
    assert page.college.projected_position == 5
    assert page.college.projected_basis == "three-month rating"


def test_history_ignores_doubles_and_undated_entries():
    history = weekly_history("2024-09-09", 10.47, 12.0)
    history.append({"type": "WeeklyAverage_Doubles", "date": "2026-09-07T00:00:00", "rating": 99.0})
    history.append({"type": "WeeklyAverage_Singles", "date": "2026-09-07T00:00:00", "rating": None})
    page = extract(full_payload(extendedRatingProfile={"history": history, "eligibleResults": {}}))
    assert all(rating < 20 for _date, rating in page.history)


def test_attributes_and_myutr_are_extracted():
    page = extract(masked_payload())
    assert (page.height_inches, page.dominant_hand, page.backhand) == (74, "Right-Handed", "Two-Handed")
    assert page.birth_place == "Warsaw" and page.gender == "Male"
    assert page.prospect_badge is True
    assert page.rating_choice == "UTRRating"
    # MyUTR uses its own field names and is masked alongside the headline rating.
    assert page.my_singles.display == "11.xx" and page.my_singles.masked
    assert page.my_singles.status == "Rated" and page.my_singles.progress == 100.0
    assert page.my_doubles.progress is None  # absent in the payload, not invented


def test_attributes_render_with_both_unit_systems():
    html = render_html(extract(masked_payload()))
    assert "6ft 2in (188 cm)" in html
    assert "Right-Handed" in html and "Two-Handed" in html
    assert "MyUTR singles" in html
    assert "College prospect badge" in html
    assert "display the UTR rating" in html


def test_the_section_is_dropped_when_nothing_populates_it():
    bare = masked_payload()
    for k in ("heightInches", "dominantHand", "backhand", "birthPlace", "ratingChoice",
              "myUtrSingles", "myUtrSinglesDisplay", "myUtrStatusSingles", "myUtrProgressSingles",
              "myUtrDoubles", "myUtrDoublesDisplay", "myUtrStatusDoubles",
              "singlesUtr", "singlesUtrDisplay", "doublesUtr", "doublesUtrDisplay"):
        bare.pop(k, None)
    assert "Ratings and attributes" not in render_html(extract(bare))


def test_height_alone_is_enough_to_show_the_section():
    page = extract(masked_payload(heightInches=68, dominantHand=None, backhand=None, birthPlace=None))
    html = render_html(page)
    assert "5ft 8in (173 cm)" in html
    assert "Backhand" not in html  # absent means absent, never a dash


# -------------------------------------------------------------------- rendering


def test_masked_page_shows_a_redaction_and_the_possible_range():
    html = render_html(extract(masked_payload()))
    assert '<span class="hidden-digits">xx</span>' in html
    assert "11.00–11.99" in html
    assert "down 0.13 over three months" in html
    assert "What a session would unlock" in html
    assert "Rating trajectory" not in html


def test_full_page_charts_the_trajectory_and_drops_the_locked_panel():
    html = render_html(extract(full_payload()))
    assert "Rating trajectory" in html
    assert "What a session would unlock" not in html
    assert "position 4" in html


def test_reconciliation_states_whether_the_counts_agree():
    assert "matching the payload's own" in render_html(extract(masked_payload(totalResults=111)))
    assert "against the payload's own" in render_html(extract(masked_payload(totalResults=999)))


def test_player_name_and_college_are_escaped():
    html = render_html(extract(masked_payload(displayName='<script>alert("x")</script>')))
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def test_fragment_omits_the_document_skeleton():
    page = extract(masked_payload())
    assert render_html(page, standalone=True).startswith("<!doctype html>")
    assert "<!doctype html>" not in render_html(page, standalone=False)


def test_script_data_cannot_close_the_script_tag():
    html = render_html(extract(masked_payload(displayName="</script><b>")))
    # The name reaches the JSON block via the chart payload only if it is escaped there too.
    assert "</script><b>" not in html


# ----------------------------------------------------------------------- client


def _client(handler, tmp_path=None) -> UTRClient:
    return UTRClient(
        None,
        cache=DiskCache(tmp_path) if tmp_path else None,
        sleep=lambda _s: None,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_lenient_fetch_accepts_a_masked_profile():
    page = extract(_client(lambda r: httpx.Response(200, json=masked_payload())).get_player_profile(1, strict=False))
    assert page.singles.masked


def test_strict_fetch_still_refuses_a_masked_profile():
    # With history present the payload passes the completeness check, so it is the masked
    # rating itself that trips the strict path.
    masked_with_history = masked_payload(
        extendedRatingProfile={"history": weekly_history("2024-09-09", 10.47, 12.0), "eligibleResults": {}})
    with pytest.raises(SessionExpiredError):
        _client(lambda r: httpx.Response(200, json=masked_with_history)).get_player_profile(1)


def test_strict_fetch_rejects_a_history_less_profile_as_incomplete():
    with pytest.raises(ProfileFetchError):
        _client(lambda r: httpx.Response(200, json=masked_payload())).get_player_profile(1)


def test_a_partial_profile_never_satisfies_a_strict_read(tmp_path):
    """A signed-out page run must not leave the benchmark reading a masked profile as real."""
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        return httpx.Response(200, json=masked_payload())

    client = _client(handler, tmp_path)
    client.get_player_profile(1475065, strict=False)
    client.get_player_profile(1475065, strict=False)
    assert calls["n"] == 1  # the partial payload is cached for lenient callers
    with pytest.raises(UTRError):
        client.get_player_profile(1475065)
    assert calls["n"] > 1  # but a strict caller refetches rather than trusting it


def test_a_complete_profile_serves_both_callers(tmp_path):
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        return httpx.Response(200, json=full_payload())

    client = _client(handler, tmp_path)
    client.get_player_profile(970607, strict=False)
    client.get_player_profile(970607)
    assert calls["n"] == 1


# -------------------------------------------------------------------------- CLI


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1475065", 1475065),
        ("https://app.utrsports.net/profiles/1475065", 1475065),
        ("app.utrsports.net/profiles/1475065?tab=results", 1475065),
    ],
)
def test_parse_player_ref(raw, expected):
    assert page_cli.parse_player_ref(raw) == expected


@pytest.mark.parametrize("raw", ["https://example.com/profiles/1", "app.utrsports.net/college/1071", "Joachim"])
def test_parse_player_ref_rejects_anything_else(raw):
    with pytest.raises(UTRError):
        page_cli.parse_player_ref(raw)


def test_cli_writes_a_page_without_a_token(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("UTR_SESSION_COOKIE", raising=False)
    monkeypatch.setattr(
        page_cli, "UTRClient",
        functools.partial(UTRClient, sleep=lambda _s: None,
                          http=httpx.Client(transport=httpx.MockTransport(
                              lambda r: httpx.Response(200, json=masked_payload())))),
    )
    out = tmp_path / "page.html"
    args = page_cli.build_parser().parse_args(["1475065", "--out", str(out), "--no-cache"])
    assert page_cli.run(args) == 0
    html = out.read_text()
    assert "Joachim Szamborski" in html
    assert "What a session would unlock" in html
    assert "No session token" in capsys.readouterr().err
