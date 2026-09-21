"""Walk a college roster in a real signed-in browser, one player at a time.

Drives a persistent Chromium profile with Playwright: you sign in once by hand, the
profile keeps the session, and every later run reuses it. For each roster player the
scraper opens that player's profile page and keeps what the page loaded.

What it keeps per player, in order of trust:

1. The `/v1/player/{id}/profile` response the page itself requested. Structured, complete
   and unmasked, because it is the signed-in app making the call. This is the record.
2. The page's visible text. Brittle to scrape field-by-field and not used for any figure,
   so it is stored whole, for eyeballing and for confirming the page rendered a real
   rating rather than a masked one.

Pacing is the point of this module. `utr-recruited-at` pages the API at 5s and UTR
rate-limited a run on 2026-09-13; this waits a uniform random 60–300s between players,
which is 15–60x slower. One HTTP 429 anywhere ends the run — nothing is retried.

Output is JSONL, appended and flushed per player, so a run that dies keeps everything up
to that point and a rerun resumes from it. Complete profiles are also written into the
shared disk cache, so `utr-recruited-at` and `utr-player-page` pick them up for free.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .benchmark import normalize_class
from .cache import DiskCache
from .client import UTRClient, UTRError

APP = "https://app.utrsports.net"
API = "https://api.utrsports.net"
WEB = "https://prod-new-app.utrsports.net/api/web"

MIN_DELAY_S = 60.0
MAX_DELAY_S = 300.0
# Below this the run stops being "a person browsing" and starts being the thing that got
# rate-limited before. The CLI refuses to go under it.
DELAY_FLOOR_S = 30.0
NAV_TIMEOUT_MS = 60_000
RESPONSE_TIMEOUT_MS = 45_000
# Sleep in slices so Ctrl-C lands within a second or two of being pressed.
SLICE_S = 1.0

Log = Callable[[str], None]


def browser_error(exc: Exception, what: str) -> UTRError:
    """Turn a Playwright failure into something a person can act on.

    Without this the CLI hands back a raw stack trace, which says nothing about
    which of the handful of real causes it was.
    """
    text = str(exc)
    first = text.splitlines()[0] if text else exc.__class__.__name__
    if "ERR_CERT" in text or "ERR_SSL" in text:
        return UTRError(
            f"The browser rejected the TLS certificate while {what}. Something on the network is "
            "intercepting HTTPS — usually a corporate proxy or a VPN. Import that proxy's CA "
            "certificate into the browser profile, or run from a network that doesn't intercept. "
            f"({first})"
        )
    if "Executable doesn't exist" in text or "Failed to launch" in text:
        return UTRError(
            "Chromium is not installed for Playwright. Run `playwright install chromium`, or point "
            f"--browser at a Chrome you already have. ({first})"
        )
    if "ERR_NAME_NOT_RESOLVED" in text or "ERR_INTERNET_DISCONNECTED" in text or "ERR_PROXY" in text:
        return UTRError(f"The browser could not reach the network while {what}. ({first})")
    if "Timeout" in text or "timeout" in text:
        return UTRError(f"The browser timed out while {what}. UTR may be slow or the page changed. ({first})")
    return UTRError(f"The browser failed while {what}. ({first})")


class RateLimited(UTRError):
    """UTR answered 429. The run stops; nothing is retried."""


class NotSignedIn(UTRError):
    pass


@dataclass
class Target:
    player_id: int
    name: str
    class_raw: str | None = None
    class_code: str | None = None
    redshirt: bool = False


@dataclass
class Progress:
    captured: int = 0
    skipped: int = 0
    failed: int = 0
    delays: list[float] = field(default_factory=list)


# --------------------------------------------------------------------------- pure logic


def sample_delay(min_s: float, max_s: float, rng: random.Random | None = None) -> float:
    """Uniform random wait between players, in seconds."""
    if min_s > max_s:
        raise UTRError(f"--min-delay {min_s:g} is above --max-delay {max_s:g}.")
    if min_s < DELAY_FLOOR_S:
        raise UTRError(
            f"--min-delay {min_s:g}s is below the {DELAY_FLOOR_S:g}s floor. UTR rate-limited a "
            "run at 5s spacing; this scraper exists to be slower than that, not faster."
        )
    return (rng or random).uniform(min_s, max_s)


def roster_targets(payload: dict[str, Any], classes: list[str] | None = None) -> list[Target]:
    """Roster players from the `clubs/{id}/school` payload, optionally filtered by class."""
    out = []
    for p in payload.get("roster") or []:
        if p.get("id") is None:
            continue
        raw = p.get("classYearStatus") or (p.get("playerCollegeDetails") or {}).get("gradClassName")
        code = normalize_class(raw)
        if classes and code not in classes:
            continue
        out.append(Target(
            player_id=int(p["id"]),
            name=p.get("displayName") or " ".join(
                filter(None, [p.get("firstName"), p.get("lastName")])) or str(p["id"]),
            class_raw=raw,
            class_code=code,
            redshirt=bool(p.get("redshirt")),
        ))
    return out


def already_captured(path: Path) -> set[int]:
    """Player IDs already in the output file, so a rerun resumes instead of refetching."""
    done: set[int] = set()
    if not path.exists():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # a half-written final line from a killed run
        if isinstance(row.get("player_id"), int) and not row.get("error"):
            done.add(row["player_id"])
    return done


def is_masked(profile: dict[str, Any]) -> bool:
    return "x" in str(profile.get("singlesUtrDisplay") or "").lower()


def build_row(
    club_id: int, target: Target, profile: dict[str, Any] | None,
    visible_text: str | None, error: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "club_id": club_id,
        "player_id": target.player_id,
        "name": target.name,
        "class_raw": target.class_raw,
        "class_code": target.class_code,
        "redshirt": target.redshirt,
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if error:
        row["error"] = error
        return row
    row["profile"] = profile
    row["masked"] = is_masked(profile or {})
    if visible_text:
        row["visible_text"] = visible_text
    return row


def append_row(path: Path, row: dict[str, Any]) -> None:
    """One JSON object per line, flushed immediately: a killed run keeps what it got."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        fh.flush()


def sleep_in_slices(seconds: float, sleep: Callable[[float], None] = time.sleep) -> None:
    remaining = seconds
    while remaining > 0:
        sleep(min(SLICE_S, remaining))
        remaining -= SLICE_S


# --------------------------------------------------------------------------- browser


class BrowserSession:
    """A persistent signed-in Chromium profile, and the two page reads the scraper needs."""

    def __init__(self, user_data_dir: Path, headless: bool = False, log: Log | None = None,
                 executable_path: Path | None = None) -> None:
        self._dir = user_data_dir.expanduser()
        self._headless = headless
        self._executable = str(executable_path.expanduser()) if executable_path else None
        self._log = log or (lambda _m: None)
        self._pw = None
        self._ctx = None
        self.page = None
        self.saw_429 = False

    def __enter__(self) -> "BrowserSession":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - depends on the optional extra
            raise UTRError(
                "This needs Playwright. Install it with `uv pip install -e \".[scraper]\"` "
                "and then `playwright install chromium`."
            ) from exc
        self._dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        launch: dict[str, Any] = {"headless": self._headless, "viewport": {"width": 1400, "height": 1000}}
        if self._executable:
            launch["executable_path"] = self._executable
        try:
            self._ctx = self._pw.chromium.launch_persistent_context(str(self._dir), **launch)
        except Exception as exc:
            self._pw.stop()
            raise browser_error(exc, "starting the browser") from exc
        self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self.page.set_default_timeout(NAV_TIMEOUT_MS)
        self.page.on("response", self._watch)
        return self

    def __exit__(self, *exc: object) -> None:
        for closer in (getattr(self._ctx, "close", None), getattr(self._pw, "stop", None)):
            try:
                if closer:
                    closer()
            except Exception:  # pragma: no cover - teardown must not mask the real error
                pass

    def _watch(self, response: Any) -> None:
        if response.status == 429:
            self.saw_429 = True

    def _check_429(self, what: str) -> None:
        if self.saw_429:
            raise RateLimited(
                f"UTR returned HTTP 429 while {what}. Stopping: nothing is retried. Wait a few "
                "hours before rerunning — the output file lets it resume where it left off."
            )

    def has_session(self) -> bool:
        return any(c.get("name") == "jwt" and c.get("value") for c in self._ctx.cookies())

    def wait_for_login(self, timeout_s: float, sleep: Callable[[float], None] = time.sleep) -> None:
        """Open the site and wait for the person to sign in by hand."""
        try:
            self.page.goto(APP, wait_until="domcontentloaded")
        except Exception as exc:
            raise browser_error(exc, f"opening {APP}") from exc
        if self.has_session():
            self._log("Signed in already (the browser profile kept the session).")
            return
        self._log(f"Sign in to UTR in the browser window. Waiting up to {timeout_s / 60:.0f} min…")
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.has_session():
                self._log("Signed in. Starting.")
                return
            sleep(2.0)
        raise NotSignedIn("Timed out waiting for sign-in. Nothing was scraped.")

    def _json_via_page(self, url: str) -> Any:
        """Fetch from inside the tab, so it carries the tab's own session."""
        try:
            return self.page.evaluate(
                """async (u) => {
                    const r = await fetch(u, { credentials: 'include' });
                    if (r.status === 429) return { __status: 429 };
                    if (!r.ok) return { __status: r.status };
                    return await r.json();
                }""",
                url,
            )
        except Exception as exc:
            raise browser_error(exc, f"fetching {url} from the page") from exc

    def _navigate_capturing(self, page_url: str, api_match: str) -> Any:
        """Open a page and keep the API response it requested; fall back to an in-page fetch."""
        from playwright.sync_api import TimeoutError as PWTimeout

        payload = None
        try:
            with self.page.expect_response(
                lambda r: api_match in r.url and r.request.method == "GET",
                timeout=RESPONSE_TIMEOUT_MS,
            ) as info:
                self.page.goto(page_url, wait_until="domcontentloaded")
            response = info.value
            if response.status == 429:
                self.saw_429 = True
            elif response.ok:
                payload = response.json()
        except PWTimeout:
            # The SPA may serve the view from its own cache and never re-request; the
            # in-page fetch below covers that. Anything else is a real failure.
            pass
        except Exception as exc:
            raise browser_error(exc, f"loading {page_url}") from exc
        self._check_429(f"loading {page_url}")
        return payload

    def fetch_roster(self, club_id: int) -> dict[str, Any]:
        url = f"{WEB}/clubs/{club_id}/school"
        payload = self._navigate_capturing(f"{APP}/college/{club_id}?tab=roster", f"/clubs/{club_id}/school")
        if not isinstance(payload, dict) or "roster" not in payload:
            payload = self._json_via_page(url)
        self._check_429(f"loading the roster for club {club_id}")
        if isinstance(payload, dict) and payload.get("__status"):
            raise UTRError(f"Roster for club {club_id} came back HTTP {payload['__status']}.")
        if not isinstance(payload, dict) or "roster" not in payload:
            raise UTRError(
                f"Club {club_id} returned no roster. Check the ID at {APP}/college/{club_id} "
                "and that the roster tab loads while signed in."
            )
        return payload

    def fetch_player(self, player_id: int) -> tuple[dict[str, Any], str]:
        """Open the player's page; return the profile payload and the page's visible text."""
        page_url = f"{APP}/profiles/{player_id}"
        payload = self._navigate_capturing(page_url, f"/player/{player_id}/profile")
        if not isinstance(payload, dict) or payload.get("id") is None:
            payload = self._json_via_page(f"{API}/v1/player/{player_id}/profile")
        self._check_429(f"loading profile {player_id}")
        if isinstance(payload, dict) and payload.get("__status"):
            raise UTRError(f"Profile {player_id} came back HTTP {payload['__status']}.")
        if not isinstance(payload, dict) or payload.get("id") is None:
            raise UTRError(f"Profile {player_id} returned nothing usable.")

        visible = ""
        try:
            self.page.wait_for_selector("main, [class*=profile]", timeout=15_000)
            visible = (self.page.inner_text("body") or "").strip()
        except Exception:
            visible = ""  # the record is the payload; visible text is a bonus
        return payload, visible


# --------------------------------------------------------------------------- the loop


def scrape_rosters(
    session: BrowserSession,
    club_ids: list[int],
    out_path: Path,
    classes: list[str] | None = None,
    min_delay: float = MIN_DELAY_S,
    max_delay: float = MAX_DELAY_S,
    max_players: int | None = None,
    cache: DiskCache | None = None,
    log: Log | None = None,
    rng: random.Random | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Progress:
    """Roster → player → page → row, with a random wait before each next player."""
    note = log or (lambda _m: None)
    progress = Progress()
    done = already_captured(out_path)
    if done:
        note(f"{len(done)} player(s) already in {out_path}; they will be skipped.")

    queue: list[tuple[int, Target]] = []
    queued: set[int] = set()
    for position, club_id in enumerate(club_ids, 1):
        note(f"Roster for club {club_id}…")
        targets = roster_targets(session.fetch_roster(club_id), classes)
        # A player can sit on two rosters in one run; scrape them once.
        wanted = [t for t in targets if t.player_id not in done and t.player_id not in queued]
        progress.skipped += len(targets) - len(wanted)
        note(f"  {len(targets)} player(s) in scope, {len(wanted)} still to scrape")
        queue.extend((club_id, t) for t in wanted)
        queued.update(t.player_id for t in wanted)
        if position < len(club_ids) and wanted:
            _wait(sample_delay(min_delay, max_delay, rng), note, sleep, "next roster")

    if max_players is not None:
        queue = queue[:max_players]
    if not queue:
        note("Nothing to scrape.")
        return progress

    total = len(queue)
    eta = total * (min_delay + max_delay) / 2 / 60
    note(f"\n{total} player(s) to scrape. At {min_delay:.0f}–{max_delay:.0f}s apart that is "
         f"roughly {eta:.0f} min. Ctrl-C is safe; every player is written as it lands.")

    for i, (club_id, target) in enumerate(queue, 1):
        note(f"[{i}/{total}] {target.name} ({target.class_raw or 'class unknown'}, id {target.player_id})…")
        try:
            profile, visible = session.fetch_player(target.player_id)
        except RateLimited:
            raise
        except UTRError as exc:
            append_row(out_path, build_row(club_id, target, None, None, error=str(exc)))
            progress.failed += 1
            note(f"    failed: {exc}")
        else:
            masked = is_masked(profile)
            append_row(out_path, build_row(club_id, target, profile, visible))
            progress.captured += 1
            if masked:
                note("    [warning] rating came back masked — the browser is not signed in")
            else:
                note(f"    {profile.get('singlesUtrDisplay') or '?'} singles, "
                     f"{len(visible)} chars of visible text")
            # Only a complete payload joins the cache the other commands read.
            if cache and not masked:
                cache.set("profiles", str(target.player_id), profile)

        if i < total:
            delay = sample_delay(min_delay, max_delay, rng)
            progress.delays.append(delay)
            _wait(delay, note, sleep, f"next player ({total - i} left)")

    return progress


def _wait(seconds: float, note: Log, sleep: Callable[[float], None], what: str) -> None:
    note(f"    waiting {seconds:.0f}s before {what}")
    sleep_in_slices(seconds, sleep)


def rows(path: Path) -> Iterator[dict[str, Any]]:
    """Read back a capture file, skipping rows that recorded a failure."""
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not row.get("error"):
            yield row


def warm_cache(path: Path, cache: DiskCache) -> int:
    """Load a capture file's complete profiles into the cache the other commands read."""
    n = 0
    for row in rows(path):
        profile = row.get("profile")
        if isinstance(profile, dict) and not row.get("masked") and UTRClient._is_complete(profile):
            cache.set("profiles", str(row["player_id"]), profile)
            n += 1
    return n
