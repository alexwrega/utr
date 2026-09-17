"""Client for UTR's internal app API.

Undocumented and unsanctioned: fine for personal analysis, not for a product.
Everything here is sequential and throttled on purpose; see README "Rate limiting".
"""

from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

import httpx

from .benchmark import nearest_entry, normalize_class, parse_utr_date, singles_history
from .cache import DiskCache

API_BASE = "https://api.utrsports.net"
WEB_BASE = "https://prod-new-app.utrsports.net/api/web"

PROFILE_INTERVAL_S = 5.0
OTHER_INTERVAL_S = 2.0
JITTER_FRACTION = 0.25
COOLDOWN_EVERY = 50
COOLDOWN_S = 60.0
RATE_LIMIT_COOLDOWN_S = 300.0
BACKOFF_START_S = 7.0
MAX_ATTEMPTS = 5
# The newest weekly average can trail or differ from the displayed rating: Alejandro Peña showed
# 11.90 weekly vs 11.92 displayed, Connor Van Schalkwyk 13.79 vs 13.58. Gaps like that are flagged
# per player by the analysis; only a gap beyond SCALE_HARD_LIMIT means the field semantics changed.
SCALE_TOLERANCE = 0.10
SCALE_HARD_LIMIT = 1.0
COLLEGE_PAGE_SIZE = 100

Log = Callable[[str], None]


class UTRError(RuntimeError):
    pass


class SessionExpiredError(UTRError):
    pass


class ScaleDriftError(UTRError):
    pass


class ProfileFetchError(UTRError):
    pass


@dataclass
class College:
    school_id: int
    name: str
    short_name: str
    club_id: int
    conference_id: int | None
    conference: str | None
    division: str | None
    location: str | None
    power6: float | None
    division_id: int | None = None


@dataclass
class RosterPlayer:
    player_id: int
    name: str
    class_raw: str | None
    class_code: str | None
    redshirt: bool
    singles_utr: float | None


@dataclass
class Roster:
    club_id: int
    school: str
    season: str
    roster_id: int | None
    players: list[RosterPlayer]


@dataclass
class RatingAtDate:
    target: date
    rating: float | None
    landed_on: date | None
    gap_days: int | None
    history_entries: int


# --------------------------------------------------------------------------- auth


def _token_from_cookie_header(text: str) -> str | None:
    for part in text.split(";"):
        name, _, value = part.strip().partition("=")
        if name == "jwt" and value:
            return value
    return None


def load_session_token(cookie_file: Path | None = None) -> str | None:
    """Find the `jwt` session cookie.

    Accepts a Netscape cookies.txt, a JSON cookie export (list of {name, value}),
    a raw `Cookie:` header line, or a bare token. `UTR_SESSION_COOKIE` may hold
    a bare token or a cookie header string.
    """
    if cookie_file:
        text = cookie_file.expanduser().read_text().strip()
        if text.startswith("[") or text.startswith("{"):
            data = json.loads(text)
            items = data if isinstance(data, list) else data.get("cookies", [])
            for c in items:
                if c.get("name") == "jwt":
                    return c.get("value")
        for line in text.splitlines():
            fields = line.split("\t")
            if len(fields) >= 7 and fields[5] == "jwt":
                return fields[6].strip()
        token = _token_from_cookie_header(text.removeprefix("Cookie:").strip())
        if token:
            return token
        if "\n" not in text and "=" not in text and " " not in text:
            return text
        return None

    env = os.environ.get("UTR_SESSION_COOKIE", "").strip()
    if not env:
        return None
    return _token_from_cookie_header(env) or (env if "=" not in env else None)


# ------------------------------------------------------------------------- client


class UTRClient:
    def __init__(
        self,
        token: str | None,
        cache: DiskCache | None = None,
        log: Log | None = None,
        sleep: Callable[[float], None] = time.sleep,
        http: httpx.Client | None = None,
        profile_interval: float = PROFILE_INTERVAL_S,
        jitter: float = JITTER_FRACTION,
        cooldown_every: int = COOLDOWN_EVERY,
        cooldown_s: float = COOLDOWN_S,
        rate_limit_cooldown_s: float = RATE_LIMIT_COOLDOWN_S,
    ) -> None:
        self._token = token
        self._cache = cache or DiskCache(None)
        self._log = log or (lambda _msg: None)
        self._sleep = sleep
        self._last_request: dict[str, float] = {}
        self._profile_interval = max(PROFILE_INTERVAL_S, profile_interval)
        self._jitter = jitter
        self._cooldown_every = cooldown_every
        self._cooldown_s = cooldown_s
        self._rate_limit_cooldown_s = rate_limit_cooldown_s
        self.live_requests = 0
        headers = {"User-Agent": "utr-recruited-at/0.1 (personal analysis)", "Accept": "application/json"}
        self._http = http or httpx.Client(timeout=30.0, headers=headers, follow_redirects=True)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "UTRClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- plumbing

    def _auth_headers(self) -> dict[str, str]:
        if not self._token:
            return {}
        # The web API only honours the cookie; the v1/v2 API also takes a bearer header.
        return {"Cookie": f"jwt={self._token}", "Authorization": f"Bearer {self._token}"}

    def pause(self, seconds: float, reason: str = "pausing between teams") -> None:
        if seconds > 0:
            self._log(f"  {reason}: {seconds:.0f}s")
            self._sleep(seconds)

    def _throttle(self, kind: str, interval: float) -> None:
        if self._cooldown_every and self.live_requests and self.live_requests % self._cooldown_every == 0:
            self.pause(self._cooldown_s, f"cooldown after {self.live_requests} requests")
        # Random jitter so a long run doesn't hit the API on a fixed rhythm.
        interval *= 1 + random.uniform(0, self._jitter)
        last = self._last_request.get(kind)
        if last is not None:
            wait = interval - (time.monotonic() - last)
            if wait > 0:
                self._sleep(wait)
        self._last_request[kind] = time.monotonic()
        self.live_requests += 1

    def _get_json(
        self,
        url: str,
        *,
        kind: str,
        interval: float,
        params: dict[str, Any] | None = None,
        valid: Callable[[Any], bool] | None = None,
        what: str = "",
    ) -> Any:
        for round_no in (1, 2):
            try:
                return self._get_json_round(url, kind=kind, interval=interval, params=params, valid=valid, what=what)
            except ProfileFetchError as exc:
                if round_no == 2 or "429" not in str(exc):
                    raise
                self.pause(self._rate_limit_cooldown_s, f"still rate limited on {what or url}; cooling down")
        raise AssertionError("unreachable")

    def _get_json_round(
        self,
        url: str,
        *,
        kind: str,
        interval: float,
        params: dict[str, Any] | None,
        valid: Callable[[Any], bool] | None,
        what: str,
    ) -> Any:
        delay = BACKOFF_START_S
        last_problem = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._throttle(kind, interval)
            try:
                resp = self._http.get(url, params=params, headers=self._auth_headers())
            except httpx.TransportError as exc:
                last_problem = f"network error: {exc.__class__.__name__}"
            else:
                if resp.status_code in (401, 403):
                    raise SessionExpiredError(
                        f"UTR returned {resp.status_code} for {what or url}. The session cookie is "
                        "missing or expired: sign in to app.utrsports.net again and re-export the "
                        "`jwt` cookie (UTR_SESSION_COOKIE or --cookie-file)."
                    )
                if resp.status_code == 400 and "Token is missing" in resp.text:
                    raise SessionExpiredError(
                        f"UTR says the token is missing for {what or url}. Set UTR_SESSION_COOKIE "
                        "or pass --cookie-file."
                    )
                if resp.status_code == 429:
                    last_problem = "HTTP 429 rate limited"
                elif resp.status_code >= 500:
                    last_problem = f"HTTP {resp.status_code}"
                elif resp.status_code != 200:
                    raise UTRError(f"HTTP {resp.status_code} for {what or url}: {resp.text[:200]}")
                else:
                    payload = resp.json()
                    if valid is None or valid(payload):
                        return payload
                    last_problem = "incomplete payload"
            if attempt < MAX_ATTEMPTS:
                self._log(f"    {what or url}: {last_problem}; retry {attempt}/{MAX_ATTEMPTS - 1} in {delay:.0f}s")
                self._sleep(delay)
                delay *= 2
        raise ProfileFetchError(f"{what or url}: {last_problem} after {MAX_ATTEMPTS} attempts")

    # -- public interface

    def get_player_profile(self, player_id: int) -> dict[str, Any]:
        """Profile with rating history. Cached 24h; retries 429s and history-less 200s."""
        cached = self._cache.get("profiles", str(player_id))
        if cached is not None:
            return cached

        profile = self._get_json(
            f"{API_BASE}/v1/player/{player_id}/profile",
            kind="profile",
            interval=PROFILE_INTERVAL_S,
            valid=lambda p: isinstance(p, dict) and bool(singles_history(p)),
            what=f"profile {player_id}",
        )
        self._check_scale(player_id, profile)
        self._cache.set("profiles", str(player_id), profile)
        return profile

    @staticmethod
    def _check_scale(player_id: int, profile: dict[str, Any]) -> None:
        display = str(profile.get("singlesUtrDisplay") or "")
        if "x" in display.lower():
            raise SessionExpiredError(
                f"Profile {player_id} came back with a masked rating ({display!r}), which means "
                "the request was not authenticated. Refresh the session cookie."
            )
        shown = profile.get("singlesUtr")
        history = singles_history(profile)
        if not shown or not history:
            return
        newest = max(history, key=lambda h: h["date"])
        if abs(float(newest["rating"]) - float(shown)) > SCALE_HARD_LIMIT:
            raise ScaleDriftError(
                f"Profile {player_id}: newest weekly singles entry {newest['rating']} "
                f"({newest['date'][:10]}) differs from the displayed UTR {shown} by more than "
                f"{SCALE_HARD_LIMIT}. The history field semantics may have changed; refusing to continue."
            )

    def get_rating_at_date(self, player_id: int, target: date) -> RatingAtDate:
        history = singles_history(self.get_player_profile(player_id))
        hit = nearest_entry(history, target)
        if hit is None:
            return RatingAtDate(target, None, None, None, len(history))
        entry, gap = hit
        return RatingAtDate(target, float(entry["rating"]), parse_utr_date(entry["date"]), gap, len(history))

    def search_players(self, query: str, top: int = 25) -> list[dict[str, Any]]:
        payload = self._get_json(
            f"{API_BASE}/v2/search/players",
            kind="search",
            interval=OTHER_INTERVAL_S,
            params={"query": query, "top": top, "skip": 0},
            what=f"player search {query!r}",
        )
        return [h["source"] for h in payload.get("hits", []) if "source" in h]

    def get_roster(self, club_id: int) -> Roster:
        cached = self._cache.get("rosters", str(club_id))
        payload = cached
        if payload is None:
            payload = self._get_json(
                f"{WEB_BASE}/clubs/{club_id}/school",
                kind="web",
                interval=OTHER_INTERVAL_S,
                valid=lambda p: isinstance(p, dict) and "roster" in p,
                what=f"roster for club {club_id}",
            )
            self._cache.set("rosters", str(club_id), payload)
        season = payload.get("rosterYear")
        if not season:
            raise UTRError(f"Club {club_id} has no active roster season (rosterYear missing).")
        players = []
        for p in payload.get("roster") or []:
            raw = p.get("classYearStatus") or (p.get("playerCollegeDetails") or {}).get("gradClassName")
            players.append(
                RosterPlayer(
                    player_id=int(p["id"]),
                    name=p.get("displayName") or f"{p.get('firstName', '')} {p.get('lastName', '')}".strip(),
                    class_raw=raw,
                    class_code=normalize_class(raw),
                    redshirt=bool(p.get("redshirt")),
                    singles_utr=p.get("singlesUtr"),
                )
            )
        return Roster(
            club_id=club_id,
            school=payload.get("shortName") or payload.get("displayName") or payload.get("name") or str(club_id),
            season=season,
            roster_id=payload.get("rosterId"),
            players=players,
        )

    def _all_colleges(self) -> list[dict[str, Any]]:
        cached = self._cache.get("colleges", "all")
        if cached is not None:
            return cached
        seen: dict[int, dict[str, Any]] = {}
        skip = 0
        total = None
        while total is None or skip < total:
            self._log(f"  college index: fetching {skip}–{skip + COLLEGE_PAGE_SIZE}" + (f" of {total}" if total else ""))
            payload = self._get_json(
                f"{API_BASE}/v2/search/colleges",
                kind="search",
                interval=OTHER_INTERVAL_S,
                params={"top": COLLEGE_PAGE_SIZE, "skip": skip},
                what="college search",
            )
            hits = payload.get("hits") or []
            total = int(payload.get("total") or 0)
            for h in hits:
                src = h.get("source") or {}
                if "id" in src:
                    seen[int(src["id"])] = src
            if not hits:
                break
            skip += COLLEGE_PAGE_SIZE
        colleges = list(seen.values())
        self._cache.set("colleges", "all", colleges)
        return colleges

    def get_conference_colleges(self, conference_id: int, gender: str = "M") -> list[College]:
        return self.get_colleges(conference_id=conference_id, gender=gender)

    def get_colleges(
        self, *, conference_id: int | None = None, division_id: int | None = None, gender: str = "M"
    ) -> list[College]:
        """Programs for a gender, filtered by conference and/or division; strongest Power 6 first."""
        club_key = "mensClubId" if gender.upper() == "M" else "womensClubId"
        out = []
        for src in self._all_colleges():
            conf = src.get("conference") or {}
            if not src.get(club_key):
                continue
            if conference_id is not None and src.get("conferenceId") != conference_id:
                continue
            if division_id is not None and (conf.get("division") or {}).get("id") != division_id:
                continue
            power6 = next(
                ((r.get("power6") or {}).get("power6Rating") for r in src.get("activeRosters") or []
                 if (r.get("club") or {}).get("id") == src[club_key]),
                None,
            )
            out.append(
                College(
                    school_id=int(src["id"]),
                    name=src.get("name") or "",
                    short_name=src.get("shortName") or src.get("name") or "",
                    club_id=int(src[club_key]),
                    conference_id=src.get("conferenceId"),
                    conference=conf.get("shortName") or conf.get("conferenceName"),
                    division=(conf.get("division") or {}).get("shortName"),
                    location=(src.get("location") or {}).get("display"),
                    power6=power6,
                    division_id=(conf.get("division") or {}).get("id"),
                )
            )
        return sorted(out, key=lambda c: (-(c.power6 or 0), c.short_name))
