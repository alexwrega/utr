"""`utr-scrape-roster`: walk a roster in a real signed-in browser, slowly.

You sign in once in the browser window it opens; the profile directory keeps the session
for later runs. Every player lands in the output file as it is scraped, so Ctrl-C costs
you nothing and a rerun resumes.
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path
from urllib.parse import urlparse

from rich.console import Console

from . import __version__
from .benchmark import CLASS_OFFSETS
from .cache import DiskCache
from .client import UTRError
from .roster_scrape import (
    MAX_DELAY_S,
    MIN_DELAY_S,
    BrowserSession,
    NotSignedIn,
    RateLimited,
    scrape_rosters,
    warm_cache,
)

DEFAULT_PROFILE_DIR = Path("~/.utr-browser")
DEFAULT_OUT = Path("captures/roster.jsonl")


def parse_club_ref(raw: str) -> int:
    """A club ID, or a college URL copied from the UTR site."""
    text = raw.strip()
    if text.isdigit():
        return int(text)
    u = urlparse(text if "://" in text else f"https://{text}")
    if not (u.hostname or "").endswith("utrsports.net"):
        raise UTRError(f"Not a club ID or a UTR college URL: {raw}")
    parts = [p for p in u.path.split("/") if p]
    if len(parts) >= 2 and parts[0] in ("college", "club", "clubs") and parts[1].isdigit():
        return int(parts[1])
    raise UTRError(f"No club ID in {raw}. Expected app.utrsports.net/college/<id>.")


def _classes(value: str) -> list[str]:
    if value.strip().lower() == "all":
        return list(CLASS_OFFSETS)
    codes = [c.strip().lower() for c in value.split(",") if c.strip()]
    bad = [c for c in codes if c not in CLASS_OFFSETS]
    if bad:
        raise argparse.ArgumentTypeError(f"unknown class code(s) {bad}; use {','.join(CLASS_OFFSETS)} or 'all'")
    return codes


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="utr-scrape-roster",
        description="Open each roster player's profile page in a signed-in browser and keep what it "
                    "loaded, waiting a random 60–300s between players.",
        epilog="Needs the scraper extra: uv pip install -e \".[scraper]\" && playwright install chromium",
    )
    p.add_argument("club", nargs="+", help="club ID(s), or app.utrsports.net/college/<id> link(s)")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT,
                   help=f"JSONL output, appended and resumed (default {DEFAULT_OUT})")
    p.add_argument("--classes", type=_classes, default=None,
                   help=f"limit to {','.join(CLASS_OFFSETS)} or 'all' (default: the whole roster)")
    p.add_argument("--min-delay", type=float, default=MIN_DELAY_S, metavar="SEC",
                   help=f"shortest wait between players (default {MIN_DELAY_S:.0f})")
    p.add_argument("--max-delay", type=float, default=MAX_DELAY_S, metavar="SEC",
                   help=f"longest wait between players (default {MAX_DELAY_S:.0f})")
    p.add_argument("--max-players", type=int, metavar="N", help="stop after N players")
    p.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR,
                   help=f"Chromium profile holding your UTR session (default {DEFAULT_PROFILE_DIR})")
    p.add_argument("--browser", type=Path, metavar="PATH",
                   help="use this Chrome/Chromium binary instead of Playwright's bundled one")
    p.add_argument("--headless", action="store_true",
                   help="run without a visible window; only works once the profile is signed in")
    p.add_argument("--login-timeout", type=float, default=300.0, metavar="SEC",
                   help="how long to wait for you to sign in by hand (default 300)")
    p.add_argument("--seed", type=int, help="seed the delay randomness, for a reproducible dry run")
    p.add_argument("--cache-dir", type=Path, default=Path("~/.utr-cache"),
                   help="also write complete profiles here, for utr-recruited-at and utr-player-page")
    p.add_argument("--no-cache", action="store_true", help="don't write scraped profiles to the cache")
    p.add_argument("--warm-cache-only", action="store_true",
                   help="don't scrape; just load an existing --out file into the cache")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def run(args: argparse.Namespace) -> int:
    err = Console(stderr=True)
    cache = None if args.no_cache else DiskCache(args.cache_dir)

    if args.warm_cache_only:
        if not args.out.exists():
            raise UTRError(f"{args.out} does not exist.")
        n = warm_cache(args.out, cache or DiskCache(args.cache_dir))
        err.print(f"Loaded {n} complete profile(s) from {args.out} into the cache.")
        return 0

    club_ids = [parse_club_ref(c) for c in args.club]
    rng = random.Random(args.seed) if args.seed is not None else None

    err.print(f"[dim]Browser profile: {args.profile_dir}  ·  output: {args.out}[/dim]")
    with BrowserSession(args.profile_dir, headless=args.headless, executable_path=args.browser,
                        log=lambda m: err.print(f"[dim]{m}[/dim]")) as session:
        session.wait_for_login(args.login_timeout)
        progress = scrape_rosters(
            session, club_ids, args.out,
            classes=args.classes, min_delay=args.min_delay, max_delay=args.max_delay,
            max_players=args.max_players, cache=cache, rng=rng,
            log=lambda m: err.print(m),
        )

    err.print(f"\n[bold]Captured {progress.captured}[/bold], skipped {progress.skipped} already held, "
              f"{progress.failed} failed. Written to {args.out}")
    if progress.delays:
        err.print(f"[dim]Waits ranged {min(progress.delays):.0f}–{max(progress.delays):.0f}s, "
                  f"{sum(progress.delays) / 60:.0f} min of waiting in total.[/dim]")
    if cache and progress.captured:
        err.print("[dim]Complete profiles are in the cache: utr-player-page <id> and "
                  "utr-recruited-at will now use them without refetching.[/dim]")
    return 0


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    err = Console(stderr=True)
    try:
        code = run(args)
    except RateLimited as exc:
        err.print(f"[red]Rate limited:[/red] {exc}")
        code = 4
    except NotSignedIn as exc:
        err.print(f"[red]Not signed in:[/red] {exc}")
        code = 3
    except UTRError as exc:
        err.print(f"[red]Error:[/red] {exc}")
        code = 1
    except KeyboardInterrupt:
        err.print("\nStopped. Everything scraped so far is in the output file; rerun to resume.")
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()
