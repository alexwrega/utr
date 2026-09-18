"""`utr-player-page`: render any UTR player ID as a standalone HTML page.

Unlike `utr-recruited-at`, this runs without a session. Signed out UTR masks the
rating and withholds the history, and the page says so per field instead of
guessing; a `jwt` cookie fills those panels in.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from rich.console import Console

from . import __version__
from .cache import DiskCache
from .client import UTRClient, UTRError, load_session_token
from .player_page import extract, locked_fields, render_html

DEFAULT_NAME = "utr-player-{id}.html"


def parse_player_ref(raw: str) -> int:
    """A bare ID, or a profile URL copied from the UTR site."""
    text = raw.strip()
    if text.isdigit():
        return int(text)
    u = urlparse(text if "://" in text else f"https://{text}")
    if not (u.hostname or "").endswith("utrsports.net"):
        raise UTRError(f"Not a player ID or a UTR profile URL: {raw}")
    parts = [p for p in u.path.split("/") if p]
    if len(parts) >= 2 and parts[0] in ("profiles", "profile", "player") and parts[1].isdigit():
        return int(parts[1])
    raise UTRError(f"No player ID in {raw}. Expected app.utrsports.net/profiles/<id>.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="utr-player-page",
        description="Render a UTR player's profile as a standalone HTML page. Works without a session; "
                    "a session fills in the rating history and unmasks the rating.",
    )
    p.add_argument("player", help="player ID, or an app.utrsports.net/profiles/<id> link")
    p.add_argument("--out", type=Path, metavar="FILE",
                   help=f"write here instead of ./{DEFAULT_NAME.format(id='<id>')}; '-' writes to stdout")
    p.add_argument("--fragment", action="store_true",
                   help="omit the <html>/<head>/<body> skeleton, for embedding or publishing as an Artifact")
    p.add_argument("--cache-dir", type=Path, default=Path("~/.utr-cache"))
    p.add_argument("--no-cache", action="store_true", help="bypass the 24h disk cache")
    p.add_argument("--cookie-file", type=Path,
                   help="cookie export containing the `jwt` cookie (else UTR_SESSION_COOKIE)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def run(args: argparse.Namespace) -> int:
    err = Console(stderr=True)
    player_id = parse_player_ref(args.player)

    token = load_session_token(args.cookie_file)
    if not token:
        err.print("[yellow]No session token.[/yellow] The page will be built from public fields only: "
                  "the rating stays masked and there is no history. Set UTR_SESSION_COOKIE or pass "
                  "--cookie-file for the full page.")

    cache = DiskCache(None if args.no_cache else args.cache_dir)
    with UTRClient(token, cache=cache, log=lambda m: err.print(f"[dim]{m}[/dim]")) as client:
        err.print(f"Fetching profile {player_id}…")
        profile = client.get_player_profile(player_id, strict=False)

    page = extract(profile, fetched_at=datetime.now(timezone.utc).strftime("%d %B %Y"))
    err.print(f"[bold]{page.name}[/bold] — {page.singles.display or 'no rating'}"
              f"{' (masked)' if page.singles.masked else ''}, "
              f"{len(page.history)} weekly snapshots, {len(page.year_counts)} active years")
    for item in locked_fields(page):
        err.print(f"  [yellow]withheld:[/yellow] {item['what']} ({item['state']})")

    html = render_html(page, standalone=not args.fragment)

    if args.out and str(args.out) == "-":
        sys.stdout.write(html)
        return 0
    out = args.out or Path(DEFAULT_NAME.format(id=player_id))
    out.write_text(html, encoding="utf-8")
    err.print(f"\nWrote {out}")
    return 0


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    err = Console(stderr=True)
    try:
        code = run(args)
    except UTRError as exc:
        err.print(f"[red]Error:[/red] {exc}")
        code = 1
    except KeyboardInterrupt:
        err.print("\nInterrupted.")
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()
