"""Live regression against the real API. Run with: UTR_SESSION_COOKIE=... pytest -m live

Recruited-at values are asserted exactly; current UTRs drift weekly, so only within a tolerance.
Takes ~30s because profile fetches are throttled to one per 5 seconds.
"""

from __future__ import annotations

import os

import pytest

from utr_recruited_at.analysis import build_player_row
from utr_recruited_at.client import UTRClient, load_session_token

from .test_offline import UNCW

CURRENT_TOLERANCE = 0.75

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("UTR_SESSION_COOKIE"), reason="UTR_SESSION_COOKIE not set"),
]


@pytest.fixture(scope="module")
def uncw():
    with UTRClient(load_session_token()) as client:
        roster = client.get_roster(1071)
        profiles = {pid: client.get_player_profile(pid) for pid, *_ in UNCW}
    return roster, profiles


def test_roster_has_classes(uncw):
    roster, _ = uncw
    by_id = {p.player_id: p for p in roster.players}
    for pid, _name, cls, *_ in UNCW:
        assert by_id[pid].class_raw == cls


@pytest.mark.parametrize("pid,name,cls,recruited,bdate,current", UNCW)
def test_uncw_recruited_at_live(uncw, pid, name, cls, recruited, bdate, current):
    roster, profiles = uncw
    player = next(p for p in roster.players if p.player_id == pid)
    row = build_player_row(school=roster.school, college_id=1071, player_id=pid, name=name,
                           class_code=player.class_code, season_name=roster.season, profile=profiles[pid])
    assert row.included, row.reason
    assert row.recruited_at_date == bdate
    assert row.recruited_at_utr == recruited
    assert abs((row.current_utr or 0) - current) <= CURRENT_TOLERANCE
