"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from rich.console import Console

from . import __version__
from .analysis import PlayerRow, SchoolSummary, build_player_row, rank_for_target, summarise_school
from .benchmark import CLASS_OFFSETS
from .cache import DiskCache
from .capture import analyse_capture, load_capture
from .client import (
    ProfileFetchError,
    ScaleDriftError,
    SessionExpiredError,
    UTRClient,
    UTRError,
    load_session_token,
)
from .output import render_csv, render_json, render_table, write_out

SUPPORTED_URLS = (
    "app.utrsports.net/college/<id>, "
    "app.utrsports.net/search?type=colleges&conference=<id>&gender=M, "
    "app.utrsports.net/college-corner/teams?division=1[&conference=<id>], "
    "app.utrsports.net/profiles/<playerId>"
)
# College Corner filters this tool can't reproduce; they're reported, not silently applied.
IGNORED_CORNER_FILTERS = ("location", "lat", "lng", "locationStateName", "locationCountryName", "utrFitRating")


@dataclass
class UrlScope:
    conference: int | None = None
    division: int | None = None
    gender: str | None = None
    colleges: list[int] = field(default_factory=list)
    target_player: int | None = None
    notes: list[str] = field(default_factory=list)


def _first(query: dict[str, list[str]], key: str) -> str:
    return (query.get(key) or [""])[0].strip()


def parse_utr_urls(urls: list[str]) -> UrlScope:
    """Read college, conference, College Corner and player-profile links copied from the UTR site."""
    scope = UrlScope()
    for raw in urls:
        u = urlparse(raw if "://" in raw else f"https://{raw}")
        if not (u.hostname or "").endswith("utrsports.net"):
            raise UTRError(f"Not a UTR URL: {raw}")
        parts = [p for p in u.path.split("/") if p]
        query = parse_qs(u.query, keep_blank_values=True)
        gender = _first(query, "gender").upper()
        if gender:
            scope.gender = "W" if gender in ("W", "F") else "M"

        if len(parts) >= 2 and parts[0] in ("college", "club", "clubs") and parts[1].isdigit():
            scope.colleges.append(int(parts[1]))
        elif len(parts) >= 2 and parts[0] in ("profiles", "profile", "player") and parts[1].isdigit():
            if scope.target_player is not None:
                raise UTRError("Only one player profile URL can be the target.")
            scope.target_player = int(parts[1])
        elif parts[:1] == ["search"] and _first(query, "conference").isdigit():
            scope.conference = int(_first(query, "conference"))
        elif parts[:2] == ["college-corner", "teams"]:
            if _first(query, "conference").isdigit():
                scope.conference = int(_first(query, "conference"))
            if _first(query, "division").isdigit():
                scope.division = int(_first(query, "division"))
            if scope.conference is None and scope.division is None:
                raise UTRError(f"{raw} has no division or conference filter; that would be every college in UTR.")
            ignored = [k for k in IGNORED_CORNER_FILTERS if _first(query, k)]
            if ignored:
                scope.notes.append(f"ignoring College Corner filters this tool doesn't support: {', '.join(ignored)}")
        else:
            raise UTRError(f"Don't know how to read {raw}. Supported: {SUPPORTED_URLS}")

    group = scope.conference is not None or scope.division is not None
    if group and scope.colleges:
        raise UTRError("Give either a conference/division URL or college URLs, not both.")
    if not group and not scope.colleges:
        raise UTRError(f"Need at least one college, conference or division URL. Supported: {SUPPORTED_URLS}")
    return scope


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
        prog="utr-recruited-at",
        description=(
            "For each roster player, find their singles UTR in the September one year before "
            "they enrolled, and compare a target recruit's UTR today against those bands."
        ),
    )
    scope = p.add_mutually_exclusive_group(required=True)
    scope.add_argument("--conference", type=int, help="UTR conference ID (CAA is 12)")
    scope.add_argument("--division", type=int, help="UTR division ID (NCAA D1 is 1)")
    scope.add_argument("--college", type=int, nargs="+", metavar="CLUB_ID",
                       help="explicit college club IDs (as in app.utrsports.net/college/<id>); skips conference lookup")
    scope.add_argument("--url", nargs="+", metavar="URL",
                       help=f"links copied from the UTR site: {SUPPORTED_URLS} (a profile link sets the target)")
    scope.add_argument("--from-capture", type=Path, metavar="JSON",
                       help="analyse results captured in a signed-in browser by browser/runner.js (no cookie needed)")
    p.add_argument("--gender", choices=["M", "W", "m", "w"], default=None, help="default M, or taken from --url")
    p.add_argument("--max-teams", type=int, metavar="N",
                   help="only the top N programs by Power 6 (conference/division runs)")
    target = p.add_mutually_exclusive_group()
    target.add_argument("--target-utr", type=float, nargs="+", metavar="UTR",
                        help="recruit's current singles UTR, or a LOW HIGH range such as 11.0 12.0")
    target.add_argument("--target-name", help="resolve the recruit's UTR by name via player search")
    p.add_argument("--target-location", help="disambiguate --target-name, e.g. 'Raleigh, NC'")
    p.add_argument("--classes", type=_classes, default=["fr", "so"],
                   help="comma list of fr,so,jr,sr,gr or 'all' (default fr,so)")
    p.add_argument("--all-classes", action="store_true", help="shorthand for --classes all")
    p.add_argument("--format", choices=["table", "csv", "json"], default="table")
    p.add_argument("--out", type=Path, help="write csv/json output to this file")
    pace = p.add_argument_group("pacing (to avoid UTR rate limits on long runs)")
    pace.add_argument("--delay", type=float, default=6.0,
                      help="seconds between profile requests, min 5; up to +25%% random jitter is added (default 6)")
    pace.add_argument("--team-pause", type=float, default=5.0,
                      help="extra seconds between teams that needed live requests (default 5)")
    pace.add_argument("--cooldown-every", type=int, default=50,
                      help="pause after every N live requests; 0 disables (default 50)")
    pace.add_argument("--cooldown", type=float, default=60.0, help="length of that pause in seconds (default 60)")
    p.add_argument("--cache-dir", type=Path, default=Path("~/.utr-cache"))
    p.add_argument("--no-cache", action="store_true", help="bypass the 24h disk cache")
    p.add_argument("--cookie-file", type=Path,
                   help="cookie export containing the `jwt` cookie (else UTR_SESSION_COOKIE)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def resolve_target(client: UTRClient, name: str, location: str | None, log: Console) -> float:
    hits = client.search_players(name)
    wanted = name.lower().split()
    hits = [h for h in hits if all(w in (h.get("displayName") or "").lower() for w in wanted)]
    if location:
        hits = [h for h in hits if location.lower() in ((h.get("location") or {}).get("display") or "").lower()]
    if not hits:
        raise UTRError(f"No player matching {name!r}" + (f" in {location!r}" if location else ""))
    if len(hits) > 1:
        options = "; ".join(
            f"{h.get('displayName')} ({(h.get('location') or {}).get('display')}, UTR {h.get('singlesUtr')}, id {h.get('id')})"
            for h in hits[:10]
        )
        raise UTRError(f"{len(hits)} players match {name!r}. Add --target-location or use --target-utr. Candidates: {options}")
    hit = hits[0]
    utr = hit.get("singlesUtr")
    if not utr:
        raise UTRError(f"{hit.get('displayName')} has no singles UTR")
    log.print(f"Target: {hit.get('displayName')} ({(hit.get('location') or {}).get('display')}), UTR {utr:.2f}")
    return float(utr)


def resolve_target_profile(client: UTRClient, player_id: int, log: Console) -> float:
    profile = client.get_player_profile(player_id)
    utr = profile.get("singlesUtr")
    if not utr:
        raise UTRError(f"Player {player_id} has no singles UTR")
    location = (profile.get("location") or {}).get("display")
    log.print(f"Target: {profile.get('displayName')} ({location}), UTR {float(utr):.2f}")
    return float(utr)


def _target_range(values: list[float] | float | None) -> tuple[float | None, float | None]:
    if values is None or isinstance(values, (int, float)):
        return values, None
    if len(values) > 2:
        raise UTRError("--target-utr takes one UTR or a LOW HIGH range, e.g. --target-utr 11.0 12.0")
    if len(values) == 1:
        return values[0], None
    low, high = sorted(values)
    return low, high


def run(args: argparse.Namespace) -> int:
    err = Console(stderr=True)
    classes = list(CLASS_OFFSETS) if args.all_classes else args.classes
    args.target_utr, args.target_high = _target_range(args.target_utr)
    if args.from_capture:
        return run_capture(args, classes, err)

    conference: int | None = args.conference
    division: int | None = args.division
    college_ids: list[int] | None = args.college
    target_player: int | None = None
    url_gender: str | None = None
    if args.url:
        scope = parse_utr_urls(args.url)
        conference, division, college_ids = scope.conference, scope.division, scope.colleges or None
        url_gender, target_player = scope.gender, scope.target_player
        for note in scope.notes:
            err.print(f"[yellow]Note: {note}[/yellow]")
        if target_player is not None and (args.target_utr is not None or args.target_name):
            raise UTRError("A profile URL already sets the target; drop --target-utr/--target-name.")
    gender = (args.gender or url_gender or "M").upper()

    token = load_session_token(args.cookie_file)
    if not token:
        if args.cookie_file and args.cookie_file.expanduser().exists():
            content = args.cookie_file.expanduser().read_text().strip()
            looks = "a web address" if "://" in content else "no `jwt` cookie value"
            err.print(f"[red]{args.cookie_file} contains {looks}.[/red] In DevTools → Application → Cookies → "
                      "app.utrsports.net, copy the Value of the `jwt` row (it starts with eyJ) and save that.")
        else:
            err.print("[red]No session token.[/red] Set UTR_SESSION_COOKIE to your `jwt` cookie, or pass --cookie-file.")
        return 2

    cache = DiskCache(None if args.no_cache else args.cache_dir)
    players: list[PlayerRow] = []
    summaries: list[SchoolSummary] = []

    if args.delay < 5:
        raise UTRError("--delay below 5 seconds reliably triggers UTR's rate limiting; use 5 or more.")

    with UTRClient(
        token, cache=cache, log=lambda m: err.print(f"[dim]{m}[/dim]"),
        profile_interval=args.delay, cooldown_every=args.cooldown_every, cooldown_s=args.cooldown,
    ) as client:
        target_utr = args.target_utr
        if args.target_name:
            target_utr = resolve_target(client, args.target_name, args.target_location, err)
        elif target_player is not None:
            target_utr = resolve_target_profile(client, target_player, err)

        if conference is not None or division is not None:
            label = " ".join(filter(None, [
                f"conference {conference}" if conference is not None else None,
                f"division {division}" if division is not None else None,
            ]))
            err.print(f"Looking up {label} ({gender})…")
            colleges = client.get_colleges(conference_id=conference, division_id=division, gender=gender)
            if not colleges:
                err.print(f"[red]No {gender} programs found for {label}.[/red]")
                return 1
            if args.max_teams:
                colleges = colleges[: args.max_teams]
            club_ids = [c.club_id for c in colleges]
            names = ", ".join(c.short_name for c in colleges[:12]) + (" …" if len(colleges) > 12 else "")
            err.print(f"  {len(colleges)} programs (strongest Power 6 first): {names}")
            if len(colleges) > 12:
                per_team = 2 + args.team_pause + 4.5 * args.delay * 1.125
                hours = len(colleges) * per_team / 3600
                if args.cooldown_every:
                    hours += len(colleges) * 5.5 / args.cooldown_every * args.cooldown / 3600
                err.print(f"  [dim]Rough cold-run estimate: {hours:.1f} h (less for anything already cached). "
                          f"Ctrl-C is safe; reruns within 24h resume from the cache.[/dim]")
        else:
            club_ids = college_ids or []

        live_before_team = client.live_requests

        for n, club_id in enumerate(club_ids, 1):
            if n > 1 and args.team_pause > 0 and client.live_requests > live_before_team:
                client.pause(args.team_pause)
            live_before_team = client.live_requests
            try:
                roster = client.get_roster(club_id)
            except (SessionExpiredError, ScaleDriftError):
                raise
            except UTRError as exc:
                err.print(f"\n[yellow]Club {club_id}: skipped, {exc}[/yellow]")
                summaries.append(SchoolSummary(school=str(club_id), college_id=club_id, n=0, band_min=None,
                                               band_max=None, median=None, mean_annualised_delta=None,
                                               note=f"roster unavailable: {exc}"))
                continue
            wanted = [p for p in roster.players if p.class_code in classes]
            unknown = [p for p in roster.players if p.class_code is None]
            err.print(f"\n[bold]{roster.school}[/bold] ({roster.season}, team {n}/{len(club_ids)}): "
                      f"{len(wanted)} of {len(roster.players)} players in {','.join(classes)}")
            for p in unknown:
                err.print(f"  [yellow]skipping {p.name}: unrecognised class {p.class_raw!r}[/yellow]")

            school_rows: list[PlayerRow] = []
            for i, p in enumerate(wanted, 1):
                err.print(f"  [{i}/{len(wanted)}] {p.name} ({p.class_raw})…", end=" ")
                profile, problem = None, None
                try:
                    profile = client.get_player_profile(p.player_id)
                except ProfileFetchError as exc:
                    problem = f"profile fetch failed: {exc}"
                row = build_player_row(
                    school=roster.school, college_id=club_id, player_id=p.player_id, name=p.name,
                    class_code=p.class_code or "", season_name=roster.season, profile=profile, fetch_error=problem,
                )
                if p.redshirt:
                    row.flags.append("redshirt")
                school_rows.append(row)
                err.print(
                    f"{row.recruited_at_utr:.2f} on {row.recruited_at_date}" if row.recruited_at_utr is not None
                    else f"[yellow]excluded: {row.reason}[/yellow]"
                )
            players.extend(school_rows)
            summaries.append(summarise_school(roster.school, club_id, school_rows, target_utr, args.target_high))

    return emit(args, players, summaries, target_utr, err, {
        "conference": conference, "division": division, "colleges": club_ids, "gender": gender,
        "classes": classes, "target_utr": target_utr, "target_utr_high": args.target_high,
        "target_player_id": target_player,
    })


def run_capture(args: argparse.Namespace, classes: list[str], err: Console) -> int:
    if args.target_name:
        raise UTRError("--target-name needs live API access; with --from-capture use --target-utr.")
    capture = load_capture(args.from_capture)
    players, summaries = analyse_capture(capture, classes, args.target_utr, args.target_high)
    err.print(f"Loaded {len(capture['schools'])} school(s) captured {capture.get('captured_at', 'at an unknown time')}")
    return emit(args, players, summaries, args.target_utr, err, {
        "source": "browser capture", "captured_at": capture.get("captured_at"),
        "colleges": [s.get("club_id") for s in capture["schools"]], "classes": classes,
        "target_utr": args.target_utr, "target_utr_high": args.target_high,
    })


def emit(
    args: argparse.Namespace,
    players: list[PlayerRow],
    summaries: list[SchoolSummary],
    target_utr: float | None,
    err: Console,
    meta: dict[str, object],
) -> int:
    ranking = rank_for_target(summaries) if target_utr is not None else []
    if args.format == "table":
        render_table(players, summaries, ranking, target_utr, Console(), target_high=getattr(args, "target_high", None))
        return 0

    if args.format == "csv":
        text = render_csv(players, summaries)
    else:
        text = render_json(players, summaries, ranking, {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **meta,
        })
    if args.out:
        args.out.write_text(text)
        err.print(f"\nWrote {args.out}")
    else:
        write_out(text, sys.stdout)
    return 0


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    err = Console(stderr=True)
    try:
        code = run(args)
    except SessionExpiredError as exc:
        err.print(f"[red]Session problem:[/red] {exc}")
        code = 3
    except UTRError as exc:
        err.print(f"[red]Error:[/red] {exc}")
        code = 1
    except KeyboardInterrupt:
        err.print("\nInterrupted. Profiles fetched so far are cached; rerun to resume.")
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()
