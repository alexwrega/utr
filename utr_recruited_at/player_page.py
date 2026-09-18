"""Render one player's profile payload as a standalone HTML page.

Everything here is pure: `extract` turns a profile payload into `PlayerPage`,
`render_html` turns that into a page. Nothing fetches, so both are testable
against a fixture and neither depends on being signed in.

Signed out, UTR masks ratings to "11.xx" and returns no rating history. The page
reports that state rather than papering over it: a masked rating renders as a
redaction, and every field the session withheld is listed with the endpoint it
came from. Signed in, the same code fills those panels in.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .benchmark import SINGLES_TYPE, parse_utr_date

# UTR renders a withheld rating as "11.xx"; the numeric field is rounded to a flat 11.0 alongside it.
MASK_MARKER = "x"
# Lineup positions UTR publishes per college team (its "top 6").
LINEUP_SIZE = 6


@dataclass
class Rating:
    display: str | None = None
    value: float | None = None
    masked: bool = False
    status: str | None = None
    progress: float | None = None

    @property
    def known(self) -> float | None:
        """The rating only when it is a real figure, not a masked placeholder."""
        return None if self.masked else self.value


@dataclass
class YearCount:
    year: int
    singles: int = 0
    doubles: int = 0

    @property
    def total(self) -> int:
        return self.singles + self.doubles


@dataclass
class LineupSlot:
    position: int
    rating: float


@dataclass
class CollegeContext:
    name: str
    conference: str | None = None
    division: str | None = None
    power6: float | None = None
    class_name: str | None = None
    lineup: list[LineupSlot] = field(default_factory=list)
    projected_position: int | None = None
    projected_basis: str | None = None


@dataclass
class PlayerPage:
    player_id: int
    name: str
    profile_id: int | None = None
    location: str | None = None
    nationality: str | None = None
    age_range: str | None = None
    claimed: bool = False
    is_pro: bool = False
    college_recruiting: bool = False
    recruiting_age: bool = False
    singles: Rating = field(default_factory=Rating)
    doubles: Rating = field(default_factory=Rating)
    three_month: float | None = None
    three_month_change: float | None = None
    historic_singles: float | None = None
    historic_singles_date: str | None = None
    historic_singles_reliability: float | None = None
    total_results: int | None = None
    year_counts: list[YearCount] = field(default_factory=list)
    eligible_singles: int | None = None
    eligible_doubles: int | None = None
    history: list[tuple[str, float]] = field(default_factory=list)
    college: CollegeContext | None = None
    third_party_rankings: list[str] = field(default_factory=list)
    fetched_at: str = ""

    @property
    def authenticated(self) -> bool:
        """A session is what unmasks the rating, so the rating is the tell."""
        return not self.singles.masked

    @property
    def rating_estimate(self) -> float | None:
        """Best available figure for the current rating: the real one, else the 3-month."""
        return self.singles.known if self.singles.known is not None else self.three_month

    @property
    def singles_results(self) -> int:
        return sum(y.singles for y in self.year_counts)

    @property
    def doubles_results(self) -> int:
        return sum(y.doubles for y in self.year_counts)


# --------------------------------------------------------------------------- extract


def _rating(profile: dict[str, Any], kind: str) -> Rating:
    display = profile.get(f"{kind}UtrDisplay")
    display = str(display) if display is not None else None
    raw = profile.get(f"{kind}Utr")
    suffix = kind.capitalize()
    return Rating(
        display=display,
        value=float(raw) if isinstance(raw, (int, float)) else None,
        masked=bool(display and MASK_MARKER in display.lower()),
        status=profile.get(f"ratingStatus{suffix}"),
        progress=profile.get(f"ratingProgress{suffix}"),
    )


def _three_month_change(profile: dict[str, Any]) -> float | None:
    """Signed 3-month change, negative for a drop.

    UTR reports `changeDirection` next to a `ratingDifference` whose sign runs the
    other way (a "down" profile carries a positive difference), so the direction
    field is treated as authoritative and the difference as a magnitude.
    """
    details = profile.get("threeMonthRatingChangeDetails") or {}
    raw = details.get("ratingDifference")
    direction = str(details.get("changeDirection") or "").lower()
    if not isinstance(raw, (int, float)) or direction not in ("up", "down"):
        return None
    magnitude = abs(float(raw))
    if not magnitude:
        return None
    return -magnitude if direction == "down" else magnitude


def _year_counts(profile: dict[str, Any]) -> list[YearCount]:
    by_year: dict[int, YearCount] = {}
    for key, attr in (("resultCountsSingles", "singles"), ("resultCountsDoubles", "doubles")):
        for item in profile.get(key) or []:
            year, value = item.get("key"), item.get("value")
            if not isinstance(year, int) or not isinstance(value, int):
                continue
            setattr(by_year.setdefault(year, YearCount(year)), attr, value)
    return [by_year[y] for y in sorted(by_year)]


def _singles_history(profile: dict[str, Any]) -> list[tuple[str, float]]:
    erp = profile.get("extendedRatingProfile") or {}
    points = []
    for entry in erp.get("history") or []:
        if entry.get("type") != SINGLES_TYPE or entry.get("rating") is None:
            continue
        try:
            points.append((parse_utr_date(entry["date"]).isoformat(), float(entry["rating"])))
        except (ValueError, KeyError, TypeError):
            continue
    return sorted(points)


def _college(profile: dict[str, Any], estimate: float | None) -> CollegeContext | None:
    raw = profile.get("playerCollege") or {}
    if not raw.get("name"):
        return None
    conference = raw.get("conference") or {}
    gender_male = bool((raw.get("gender") or {}).get("male"))
    key = "top6UtrMale" if gender_male else "top6UtrFemale"
    lineup = [
        LineupSlot(position=int(s["position"]), rating=round(float(s["rating"]), 2))
        for s in (raw.get(key) or [])
        if isinstance(s, dict) and s.get("position") is not None and s.get("rating") is not None
    ]
    lineup.sort(key=lambda s: s.position)

    college = CollegeContext(
        name=raw.get("displayName") or raw.get("name") or "",
        conference=conference.get("shortName") or conference.get("conferenceName"),
        division=(conference.get("division") or {}).get("shortName"),
        power6=raw.get("power6Men") if gender_male else raw.get("power6Women"),
        class_name=(profile.get("playerCollegeDetails") or {}).get("gradClassName"),
        lineup=lineup,
    )
    if lineup and estimate is not None:
        ahead = sum(1 for s in lineup if s.rating > estimate)
        college.projected_position = min(ahead + 1, LINEUP_SIZE)
        college.projected_basis = "current UTR" if estimate == profile.get("singlesUtr") else "three-month rating"
    return college


def _third_party(profile: dict[str, Any]) -> list[str]:
    out = []
    for r in profile.get("thirdPartyRankings") or []:
        source, kind, rank = r.get("source"), r.get("type"), r.get("rank")
        if source and rank:
            out.append(f"{source} {kind or ''} #{rank}".replace("  ", " ").strip())
    return out


def extract(profile: dict[str, Any], fetched_at: str | None = None) -> PlayerPage:
    """Pull every field the page can show out of a `/v1/player/{id}/profile` payload."""
    historic = profile.get("historicRatings") or {}
    singles = _rating(profile, "singles")
    three_month = profile.get("threeMonthRating")
    three_month = float(three_month) if isinstance(three_month, (int, float)) and three_month else None
    eligible = (profile.get("extendedRatingProfile") or {}).get("eligibleResults") or {}

    page = PlayerPage(
        player_id=int(profile.get("id") or 0),
        name=profile.get("displayName")
        or " ".join(filter(None, [profile.get("firstName"), profile.get("lastName")]))
        or "Unknown player",
        profile_id=profile.get("profileId"),
        location=(profile.get("location") or {}).get("display"),
        nationality=profile.get("nationality"),
        age_range=profile.get("ageRange"),
        claimed=bool(profile.get("claimed")),
        is_pro=bool(profile.get("isPro")),
        college_recruiting=bool(profile.get("collegeRecruiting")),
        recruiting_age=bool(profile.get("isCollegeRecruitingAge")),
        singles=singles,
        doubles=_rating(profile, "doubles"),
        three_month=three_month,
        three_month_change=_three_month_change(profile),
        historic_singles=historic.get("historicSinglesRating"),
        historic_singles_date=(historic.get("historicSinglesRatingDate") or "")[:10] or None,
        historic_singles_reliability=historic.get("historicSinglesRatingReliability"),
        total_results=profile.get("totalResults"),
        year_counts=_year_counts(profile),
        eligible_singles=len(eligible.get("singles") or []) or None,
        eligible_doubles=len(eligible.get("doubles") or []) or None,
        history=_singles_history(profile),
        third_party_rankings=_third_party(profile),
        fetched_at=fetched_at or datetime.now(timezone.utc).strftime("%d %B %Y"),
    )
    page.college = _college(profile, page.rating_estimate)
    return page


def locked_fields(page: PlayerPage) -> list[dict[str, str]]:
    """What this session could not see, with the field or endpoint that holds it."""
    out = []
    if not page.history:
        out.append({
            "what": "Weekly UTR trajectory",
            "state": "returns null",
            "field": "extendedRatingProfile.history — the whole development curve, "
                     "and the field the recruited-at benchmark runs on",
        })
    if page.singles.masked:
        out.append({
            "what": "Unmasked singles & doubles rating",
            "state": f"masked {page.singles.display}",
            "field": "singlesUtr / doublesUtr — resolves the two hidden decimals",
        })
        out.append({
            "what": "Match-by-match results",
            "state": "http 400",
            "field": f"/v1/player/{page.player_id}/results — opponent, opponent rating, score, "
                     "event; quality-of-win lives here",
        })
    if page.historic_singles is None:
        out.append({
            "what": "Historic rating anchors",
            "state": "all null",
            "field": "historicRatings.historicSinglesRating + reliability + date",
        })
    return out


# --------------------------------------------------------------------------- render

_STYLE = """
  :root {
    --ground: #f3f4f0; --surface: #fbfbf9; --surface-2: #eceee7;
    --ink: #14160f; --ink-2: #545748; --ink-3: #7e8271;
    --line: #dde0d5; --line-strong: #c7cbbd;
    --accent: #2a78d6; --s1: #2a78d6; --s2: #eb6834;
    --display: "Archivo", "Helvetica Neue", Arial, sans-serif;
    --body: "Newsreader", Georgia, "Times New Roman", serif;
    --mono: "IBM Plex Mono", ui-monospace, "SF Mono", Menlo, monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --ground: #111310; --surface: #191b17; --surface-2: #22251f;
      --ink: #f1f3ec; --ink-2: #b7bbac; --ink-3: #888d7c;
      --line: #2b2e26; --line-strong: #3c4034;
      --accent: #3987e5; --s1: #3987e5; --s2: #d95926;
    }
  }
  :root[data-theme="dark"] {
    --ground: #111310; --surface: #191b17; --surface-2: #22251f;
    --ink: #f1f3ec; --ink-2: #b7bbac; --ink-3: #888d7c;
    --line: #2b2e26; --line-strong: #3c4034;
    --accent: #3987e5; --s1: #3987e5; --s2: #d95926;
  }

  body { background: var(--ground); color: var(--ink); font-family: var(--display);
         margin: 0; -webkit-font-smoothing: antialiased; }
  .wrap { max-width: 880px; margin: 0 auto; padding-inline: 20px; padding-block: 32px 56px; }
  h1, h2 { margin: 0; text-wrap: balance; }
  .eyebrow { font-family: var(--mono); font-size: 11px; letter-spacing: 0.12em;
             text-transform: uppercase; color: var(--ink-3); }

  header.card { border-bottom: 1px solid var(--line-strong); padding-bottom: 22px; }
  h1 { font-size: clamp(30px, 6vw, 46px); font-weight: 700; letter-spacing: -0.02em;
       line-height: 1.05; margin-top: 8px; }
  .meta { display: flex; flex-wrap: wrap; gap: 6px 14px; margin-top: 12px;
          font-family: var(--mono); font-size: 12.5px; color: var(--ink-2); }
  .pills { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 16px; }
  .pill { font-size: 11.5px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase;
          padding: 5px 10px; border-radius: 3px; background: var(--surface-2);
          color: var(--ink-2); border: 1px solid var(--line); }
  .pill.on { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 40%, var(--line)); }

  .rating { display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr);
            gap: 28px; margin-top: 32px; align-items: start; }
  @media (max-width: 620px) { .rating { grid-template-columns: 1fr; gap: 24px; } }
  .figure { font-size: clamp(64px, 15vw, 104px); font-weight: 700; letter-spacing: -0.04em;
            line-height: 0.92; font-variant-numeric: tabular-nums; display: flex; align-items: baseline; }
  .figure .hidden-digits { color: var(--ink-3); background: var(--surface-2);
                           border-radius: 4px; padding-inline: 0.05em; }
  .note { font-family: var(--body); font-size: 15px; line-height: 1.5; color: var(--ink-2);
          margin-top: 14px; max-width: 46ch; }
  .note strong { color: var(--ink); font-weight: 500; }
  .side { background: var(--surface); border: 1px solid var(--line);
          border-left: 3px solid var(--accent); padding: 18px 20px; }
  .side .value { font-weight: 700; font-size: 40px; letter-spacing: -0.02em;
                 font-variant-numeric: tabular-nums; margin-top: 6px; line-height: 1; }
  .change { font-family: var(--mono); font-size: 13px; color: var(--ink-2);
            margin-top: 10px; display: flex; align-items: center; gap: 7px; }

  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(172px, 1fr)); gap: 1px;
          background: var(--line); border: 1px solid var(--line); margin-top: 34px; }
  .kpi { background: var(--surface); padding: 16px 18px; }
  .kpi .v { font-weight: 600; font-size: 26px; letter-spacing: -0.01em;
            font-variant-numeric: tabular-nums; margin-top: 7px; line-height: 1.1; }
  .kpi .sub { font-family: var(--mono); font-size: 11.5px; color: var(--ink-3); margin-top: 6px; }

  section { margin-top: 48px; }
  h2 { font-size: 19px; font-weight: 600; letter-spacing: -0.01em;
       padding-bottom: 10px; border-bottom: 1px solid var(--line-strong); }
  .lede { font-family: var(--body); font-size: 15.5px; line-height: 1.6; color: var(--ink-2);
          margin: 14px 0 0; max-width: 62ch; }

  .chart-head { display: flex; flex-wrap: wrap; gap: 14px; align-items: center;
                justify-content: space-between; margin-top: 20px; }
  .legend { display: flex; gap: 16px; font-family: var(--mono); font-size: 12px; color: var(--ink-2); }
  .legend i { width: 11px; height: 11px; border-radius: 2px; display: inline-block;
              margin-right: 6px; vertical-align: -1px; }
  .toggle { font-size: 12px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase;
            background: var(--surface); color: var(--ink-2); border: 1px solid var(--line-strong);
            border-radius: 3px; padding: 6px 12px; cursor: pointer; font-family: var(--display); }
  .toggle:hover { color: var(--ink); border-color: var(--ink-3); }
  .toggle:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

  .chart-box { position: relative; margin-top: 16px; }
  svg { display: block; width: 100%; height: auto; overflow: visible; }
  .tip { position: absolute; pointer-events: none; background: var(--ink); color: var(--ground);
         font-family: var(--mono); font-size: 12px; line-height: 1.45; padding: 7px 10px;
         border-radius: 3px; transform: translate(-50%, -100%); white-space: nowrap;
         opacity: 0; transition: opacity .12s; z-index: 5; }
  .tip.show { opacity: 1; }

  table { border-collapse: collapse; width: 100%; font-family: var(--mono);
          font-size: 13px; margin-top: 16px; }
  caption { text-align: left; font-family: var(--mono); font-size: 11.5px;
            color: var(--ink-3); padding-bottom: 8px; }
  th, td { text-align: right; padding: 7px 10px; border-bottom: 1px solid var(--line);
           font-variant-numeric: tabular-nums; }
  th:first-child, td:first-child { text-align: left; }
  thead th { color: var(--ink-3); font-weight: 500; border-bottom-color: var(--line-strong); }
  .table-scroll { overflow-x: auto; }

  .rows { display: grid; gap: 1px; background: var(--line); border: 1px solid var(--line); margin-top: 20px; }
  .row { background: var(--surface-2); padding: 15px 18px; display: grid;
         grid-template-columns: 1fr auto; gap: 4px 18px; align-items: baseline; }
  .row .what { font-size: 14.5px; font-weight: 600; }
  .row .field { font-family: var(--mono); font-size: 11.5px; color: var(--ink-3);
                grid-column: 1 / -1; word-break: break-word; }
  .row .state { font-family: var(--mono); font-size: 11px; letter-spacing: 0.08em;
                text-transform: uppercase; color: var(--ink-3); border: 1px dashed var(--line-strong);
                padding: 3px 8px; border-radius: 2px; white-space: nowrap; }
  .row.here { background: var(--surface); }
  .row.here .state { border-style: solid; border-color: color-mix(in srgb, var(--accent) 45%, var(--line));
                     color: var(--accent); }

  footer { margin-top: 52px; padding-top: 20px; border-top: 1px solid var(--line-strong);
           font-family: var(--mono); font-size: 11.5px; line-height: 1.7; color: var(--ink-3); }
  footer code { color: var(--ink-2); }
  @media (prefers-reduced-motion: reduce) { .tip { transition: none; } }
"""


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _json_for_script(data: Any) -> str:
    """JSON safe to drop inside a <script> block."""
    return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")


def _figure(page: PlayerPage) -> str:
    """The rating as UTR served it: a real number, or a redaction where the decimals were."""
    if page.singles.masked:
        display = page.singles.display or ""
        whole, _, hidden = display.partition(".")
        return (
            f'<div class="figure" aria-label="{_e(display)}, masked">'
            f'<span>{_e(whole)}.</span><span class="hidden-digits">{_e(hidden or "xx")}</span></div>'
        )
    shown = page.singles.display or (f"{page.singles.value:.2f}" if page.singles.value else "—")
    return f'<div class="figure">{_e(shown)}</div>'


def _hero_note(page: PlayerPage) -> str:
    if page.singles.masked:
        value = page.singles.value
        low = f"{value:.2f}" if value is not None else "?"
        high = f"{value + 0.99:.2f}" if value is not None else "?"
        return (
            f"UTR withholds the decimals from unauthenticated callers. The API returns "
            f"<strong>{_e(page.singles.display)}</strong> for display and rounds the numeric field to a flat "
            f"<strong>{_e(low)}</strong> — so the true rating sits somewhere in <strong>{_e(low)}–{_e(high)}</strong>, "
            f"and nothing on this page can narrow it further. Doubles is masked the same way."
        )
    parts = []
    if page.singles.status:
        progress = f"{page.singles.progress:.0f}%" if page.singles.progress is not None else "—"
        parts.append(f"UTR calls this <strong>{_e(page.singles.status)}</strong> at {_e(progress)} reliability.")
    if page.doubles.value is not None:
        parts.append(f"Doubles sits at <strong>{page.doubles.value:.2f}</strong>.")
    if page.eligible_singles:
        parts.append(f"It rests on <strong>{page.eligible_singles}</strong> rating-eligible singles matches.")
    return " ".join(parts) or "Read straight from the profile payload."


def _side_card(page: PlayerPage) -> str:
    if page.three_month is None:
        return ""
    change = ""
    if page.three_month_change is not None:
        arrow = "↓" if page.three_month_change < 0 else "↑"
        word = "down" if page.three_month_change < 0 else "up"
        change = (
            f'<div class="change"><span aria-hidden="true">{arrow}</span>'
            f"<span>{word} {abs(page.three_month_change):.2f} over three months</span></div>"
        )
    note = (
        "This field slips through unmasked, and it is the only real decimal figure available "
        "without a session. Treat it as the best current estimate of the hidden rating."
        if page.singles.masked
        else "UTR's rolling three-month average, next to the headline rating."
    )
    return (
        '<div class="side"><div class="eyebrow">Three-month rating'
        f'{" — not masked" if page.singles.masked else ""}</div>'
        f'<div class="value">{page.three_month:.2f}</div>{change}'
        f'<p class="note" style="font-size:14px; margin-top:14px;">{note}</p></div>'
    )


def _kpis(page: PlayerPage) -> str:
    cells: list[tuple[str, str, str]] = []

    if page.singles.status:
        progress = f"{page.singles.progress:.0f}% progress" if page.singles.progress is not None else "reliability n/a"
        both = page.doubles.status == page.singles.status
        cells.append(("Rating status", page.singles.status, f"{progress}, {'both formats' if both else 'singles'}"))
    if page.total_results:
        cells.append(("Career results", f"{page.total_results:,}",
                      f"{page.singles_results:,} singles · {page.doubles_results:,} doubles"))
    if page.eligible_singles:
        doubles = f", plus {page.eligible_doubles} doubles" if page.eligible_doubles else ""
        cells.append(("Feeding the rating", str(page.eligible_singles), f"eligible singles{doubles}"))
    if page.year_counts:
        years = len(page.year_counts)
        cells.append(("Recorded since", str(page.year_counts[0].year), f"{years} active year{'s' if years > 1 else ''}"))

    if not cells:
        return ""
    body = "".join(
        f'<div class="kpi"><div class="eyebrow">{_e(label)}</div>'
        f'<div class="v">{_e(value)}</div><div class="sub">{_e(sub)}</div></div>'
        for label, value, sub in cells
    )
    return f'<div class="kpis">{body}</div>'


def _header(page: PlayerPage) -> str:
    ids = f"UTR player {page.player_id}"
    if page.profile_id:
        ids += f" · profile {page.profile_id}"
    meta = [m for m in (page.location, page.nationality,
                        f"Age band {page.age_range}" if page.age_range else None,
                        "Claimed profile" if page.claimed else "Unclaimed profile") if m]
    pills = []
    if page.college_recruiting:
        pills.append(("College recruiting", True))
    if page.recruiting_age:
        pills.append(("Recruiting age", True))
    if page.is_pro:
        pills.append(("Pro", True))
    if page.college:
        label = page.college.name
        if page.college.class_name:
            label = f"{page.college.class_name} — {label}"
        pills.append((label, False))
    else:
        pills.append(("Unattached — no college", False))
    for ranking in page.third_party_rankings:
        pills.append((ranking, False))
    if not page.third_party_rankings and not page.is_pro:
        pills.append(("No ATP/WTA ranking", False))

    meta_html = "".join(f"<span>{_e(m)}</span>" for m in meta)
    pill_html = "".join(
        '<span class="pill{}">{}</span>'.format(" on" if on else "", _e(label)) for label, on in pills
    )
    return (
        f'<header class="card"><div class="eyebrow">{_e(ids)}</div><h1>{_e(page.name)}</h1>'
        f'<div class="meta">{meta_html}</div>'
        f'<div class="pills">{pill_html}</div>'
        "</header>"
    )


def _trajectory_section(page: PlayerPage) -> str:
    if len(page.history) < 2:
        return ""
    first, last = page.history[0], page.history[-1]
    span = f"{first[0][:4]}–{last[0][:4]}"
    move = last[1] - first[1]
    direction = "up" if move > 0 else "down" if move < 0 else "flat"
    return f"""
  <section>
    <h2>Rating trajectory</h2>
    <p class="lede">Every weekly singles snapshot UTR holds, {_e(span)} — {len(page.history)} of them.
    Across that window the rating is {direction} {abs(move):.2f}. This is the series the recruited-at
    benchmark reads to answer what a player was rated before they enrolled.</p>
    <div class="chart-head">
      <div class="legend"><span><i style="background:var(--s1)"></i>Weekly singles UTR</span></div>
    </div>
    <div class="chart-box">
      <svg id="trajectory" viewBox="0 0 720 300" role="img"
           aria-label="Line chart of weekly singles UTR from {_e(first[0])} at {first[1]:.2f} to {_e(last[0])} at {last[1]:.2f}."></svg>
      <div class="tip" id="tipTraj" role="status"></div>
    </div>
  </section>"""


def _volume_section(page: PlayerPage) -> str:
    if not page.year_counts:
        return ""
    busiest = max(page.year_counts, key=lambda y: y.singles)
    return f"""
  <section>
    <h2>Match volume by year</h2>
    <p class="lede">How much play the rating rests on, year by year. The busiest singles season is
    {busiest.year} at {busiest.singles}; the current year is partial until it closes.</p>
    <div class="chart-head">
      <div class="legend">
        <span><i style="background:var(--s1)"></i>Singles</span>
        <span><i style="background:var(--s2)"></i>Doubles</span>
      </div>
      <button class="toggle" id="tableToggle" type="button" aria-expanded="false" aria-controls="dataTable">Show table</button>
    </div>
    <div class="chart-box">
      <svg id="volume" viewBox="0 0 720 310" role="img" aria-label="Grouped bar chart of singles and doubles match counts per year."></svg>
      <div class="tip" id="tipVol" role="status"></div>
    </div>
    <div class="table-scroll" id="dataTable" hidden>
      <table>
        <caption>Match counts by year and format, read from resultCountsSingles and resultCountsDoubles.</caption>
        <thead><tr><th scope="col">Year</th><th scope="col">Singles</th><th scope="col">Doubles</th><th scope="col">Total</th></tr></thead>
        <tbody id="tableBody"></tbody>
      </table>
    </div>
  </section>"""


def _college_section(page: PlayerPage) -> str:
    college = page.college
    if not college:
        return ""
    bits = [b for b in (college.conference, college.division,
                        f"Power 6 {college.power6:.0f}" if college.power6 else None) if b]
    rows = ""
    if college.lineup:
        estimate = page.rating_estimate
        for slot in college.lineup:
            here = college.projected_position == slot.position
            state = "would slot here" if here else f"{slot.rating:.2f}"
            rows += (
                f'<div class="row{" here" if here else ""}"><div class="what">Position {slot.position}</div>'
                f'<div class="state">{_e(state)}</div>'
                f'<div class="field">roster UTR {slot.rating:.2f}'
                + (f" · this player at {estimate:.2f}" if here and estimate is not None else "")
                + "</div></div>"
            )
        rows = f'<div class="rows">{rows}</div>'
        basis = f" on the {college.projected_basis}" if college.projected_basis else ""
        lede = (
            f"UTR publishes this program's top six by rating. Against that lineup"
            f"{basis}, this player projects at "
            f"<strong>position {college.projected_position}</strong>."
            if college.projected_position
            else "UTR publishes this program's top six by rating."
        )
    else:
        lede = "UTR has no published lineup ratings for this program."

    return f"""
  <section>
    <h2>{_e(college.name)}{" · " + _e(" · ".join(bits)) if bits else ""}</h2>
    <p class="lede">{lede}</p>
    {rows}
  </section>"""


def _locked_section(page: PlayerPage) -> str:
    items = locked_fields(page)
    if not items:
        return ""
    rows = "".join(
        f'<div class="row"><div class="what">{_e(i["what"])}</div>'
        f'<div class="state">{_e(i["state"])}</div>'
        f'<div class="field">{_e(i["field"])}</div></div>'
        for i in items
    )
    return f"""
  <section>
    <h2>What a session would unlock</h2>
    <p class="lede">These fields are present in the payload shape but empty or masked for this
    caller. A <code>jwt</code> cookie fills them in; nothing here is estimated in their place.</p>
    <div class="rows">{rows}</div>
  </section>"""


def _footer(page: PlayerPage) -> str:
    lines = [
        f"Built from <code>GET api.utrsports.net/v1/player/{page.player_id}/profile</code>, fetched "
        f"{'with a session' if page.authenticated else 'unauthenticated'} on {_e(page.fetched_at)}. "
        "Every figure is read from that payload, not modelled."
    ]
    if page.total_results and page.year_counts:
        counted = page.singles_results + page.doubles_results
        agrees = "matching" if counted == page.total_results else "against"
        lines.append(
            f"Reconciliation: {page.singles_results:,} singles + {page.doubles_results:,} doubles = "
            f"{counted:,}, {agrees} the payload's own <code>totalResults</code> of {page.total_results:,}."
        )
    if page.three_month_change is not None:
        lines.append(
            "The three-month direction follows <code>changeDirection</code>; UTR's "
            "<code>ratingDifference</code> carries the opposite sign, so it is read here as a magnitude."
        )
    if page.historic_singles is not None:
        reliability = (f", reliability {page.historic_singles_reliability:.0f}"
                       if page.historic_singles_reliability is not None else "")
        lines.append(
            f"Historic singles anchor: {page.historic_singles:.2f} on "
            f"{_e(page.historic_singles_date or 'an unstated date')}{reliability}."
        )
    return f"<footer>{'<br>'.join(lines)}</footer>"


_SCRIPT = """
(function () {
  var D = JSON.parse(document.getElementById("page-data").textContent);
  var NS = "http://www.w3.org/2000/svg";
  function el(n, a) { var e = document.createElementNS(NS, n); for (var k in a) e.setAttribute(k, a[k]); return e; }
  function text(svg, x, y, s, opts) {
    var t = el("text", Object.assign({ x: x, y: y, "font-size": "11", "font-family": "var(--mono)",
      fill: "var(--ink-3)" }, opts || {}));
    t.textContent = s; svg.appendChild(t); return t;
  }
  // Smallest step that covers the peak in at most four gridlines, so a quiet year
  // doesn't get a chart scaled for a busy one.
  function niceMax(max) {
    var steps = [1, 2, 5, 10, 20, 25, 50, 100, 200, 500];
    for (var i = 0; i < steps.length; i++) {
      var n = Math.ceil(max / steps[i]);
      if (n <= 4) return { step: steps[i], max: n * steps[i] };
    }
    return { step: 1000, max: Math.ceil(max / 1000) * 1000 };
  }
  function niceRatingStep(range) {
    var steps = [0.25, 0.5, 1, 2, 5];
    for (var i = 0; i < steps.length; i++) if (range / steps[i] <= 6) return steps[i];
    return 10;
  }
  function tipAt(tip, box, W, x, y, label) {
    tip.textContent = label; tip.classList.add("show");
    var scale = box.getBoundingClientRect().width / W;
    tip.style.left = (x * scale) + "px";
    tip.style.top = (y * scale - 8) + "px";
  }

  // ---- weekly trajectory ----
  var traj = document.getElementById("trajectory");
  if (traj && D.history && D.history.length > 1) {
    var W = 720, H = 300, padL = 40, padR = 12, padT = 18, padB = 40;
    var plotW = W - padL - padR, plotH = H - padT - padB;
    var vals = D.history.map(function (p) { return p[1]; });
    var lo = Math.floor(Math.min.apply(null, vals) * 2) / 2 - 0.25;
    var hi = Math.ceil(Math.max.apply(null, vals) * 2) / 2 + 0.25;
    var t0 = Date.parse(D.history[0][0]), t1 = Date.parse(D.history[D.history.length - 1][0]);
    var span = Math.max(t1 - t0, 1);
    function X(d) { return padL + (Date.parse(d) - t0) / span * plotW; }
    function Y(v) { return padT + plotH - (v - lo) / (hi - lo) * plotH; }

    var tick = niceRatingStep(hi - lo);
    for (var v = Math.ceil(lo / tick) * tick; v <= hi + 1e-9; v += tick) {
      traj.appendChild(el("line", { x1: padL, x2: padL + plotW, y1: Y(v), y2: Y(v),
        stroke: "var(--line)", "stroke-width": 1, "shape-rendering": "crispEdges" }));
      text(traj, padL - 9, Y(v) + 4, v.toFixed(2), { "text-anchor": "end" });
    }
    var seenYear = {};
    D.history.forEach(function (p) {
      var yr = p[0].slice(0, 4);
      if (seenYear[yr]) return;
      seenYear[yr] = true;
      text(traj, X(p[0]), H - padB + 20, yr, { "text-anchor": "middle", fill: "var(--ink-2)", "font-size": "12" });
    });

    var line = D.history.map(function (p, i) { return (i ? "L" : "M") + X(p[0]).toFixed(1) + "," + Y(p[1]).toFixed(1); }).join("");
    traj.appendChild(el("path", { d: line + "L" + X(D.history[D.history.length - 1][0]).toFixed(1) + "," + Y(lo) +
      "L" + X(D.history[0][0]).toFixed(1) + "," + Y(lo) + "Z", fill: "var(--s1)", opacity: "0.10" }));
    traj.appendChild(el("path", { d: line, fill: "none", stroke: "var(--s1)", "stroke-width": "2",
      "stroke-linejoin": "round", "stroke-linecap": "round" }));

    var last = D.history[D.history.length - 1];
    traj.appendChild(el("circle", { cx: X(last[0]), cy: Y(last[1]), r: "4.5", fill: "var(--s1)",
      stroke: "var(--ground)", "stroke-width": "2" }));
    text(traj, X(last[0]) - 8, Y(last[1]) - 10, last[1].toFixed(2),
      { "text-anchor": "end", fill: "var(--ink)", "font-size": "12", "font-weight": "600",
        "font-family": "var(--display)" });

    var tipT = document.getElementById("tipTraj"), boxT = traj.parentNode;
    var cursor = el("line", { y1: padT, y2: padT + plotH, stroke: "var(--ink-3)", "stroke-width": 1,
      opacity: "0", "pointer-events": "none" });
    traj.appendChild(cursor);
    traj.addEventListener("mousemove", function (ev) {
      var r = boxT.getBoundingClientRect();
      var px = (ev.clientX - r.left) / r.width * W;
      var best = null, bestD = Infinity;
      D.history.forEach(function (p) { var d = Math.abs(X(p[0]) - px); if (d < bestD) { bestD = d; best = p; } });
      if (!best) return;
      cursor.setAttribute("x1", X(best[0])); cursor.setAttribute("x2", X(best[0]));
      cursor.setAttribute("opacity", "0.45");
      tipAt(tipT, boxT, W, X(best[0]), Y(best[1]), best[0] + " · " + best[1].toFixed(2));
    });
    traj.addEventListener("mouseleave", function () {
      cursor.setAttribute("opacity", "0"); tipT.classList.remove("show");
    });
  }

  // ---- match volume ----
  var vol = document.getElementById("volume");
  if (vol && D.years && D.years.length) {
    var W2 = 720, H2 = 310, padL2 = 34, padR2 = 8, padT2 = 18, padB2 = 46;
    var plotW2 = W2 - padL2 - padR2, plotH2 = H2 - padT2 - padB2;
    var peak = 0;
    D.years.forEach(function (y) { peak = Math.max(peak, y.singles, y.doubles); });
    var scale = niceMax(Math.max(peak, 1));
    var groupW = Math.min(plotW2 / D.years.length, 110);
    var originX = padL2 + (plotW2 - groupW * D.years.length) / 2;
    var barW = Math.min(26, Math.max(6, (groupW - 12) / 2));
    function Y2(v) { return padT2 + plotH2 - (v / scale.max) * plotH2; }
    function bar(x, top, w, bottom) {
      var r = Math.min(4, (bottom - top) / 2, w / 2);
      return "M" + x + "," + bottom + "V" + (top + r) + "a" + r + "," + r + " 0 0 1 " + r + ",-" + r +
        "h" + (w - 2 * r) + "a" + r + "," + r + " 0 0 1 " + r + "," + r + "V" + bottom + "Z";
    }
    for (var g = 0; g <= scale.max + 1e-9; g += scale.step) {
      vol.appendChild(el("line", { x1: padL2, x2: padL2 + plotW2, y1: Y2(g), y2: Y2(g),
        stroke: "var(--line)", "stroke-width": g === 0 ? 1.5 : 1, "shape-rendering": "crispEdges" }));
      text(vol, padL2 - 9, Y2(g) + 4, g, { "text-anchor": "end" });
    }
    var tipV = document.getElementById("tipVol"), boxV = vol.parentNode;
    D.years.forEach(function (d, i) {
      var gx = originX + i * groupW, pairW = barW * 2 + 2, x0 = gx + (groupW - pairW) / 2;
      [["singles", "Singles", "var(--s1)", x0], ["doubles", "Doubles", "var(--s2)", x0 + barW + 2]]
        .forEach(function (s) {
          var v = d[s[0]];
          if (!v) return;
          var p = el("path", { d: bar(s[3], Y2(v), barW, Y2(0)), fill: s[2], tabindex: "0", role: "img",
            "aria-label": d.year + " " + s[1] + ": " + v + " matches" });
          function show() {
            tipAt(tipV, boxV, W2, s[3] + barW / 2, Y2(v),
              d.year + (d.partial ? " (partial)" : "") + " · " + s[1] + " · " + v + " matches");
          }
          function hide() { tipV.classList.remove("show"); }
          p.addEventListener("mouseenter", show); p.addEventListener("focus", show);
          p.addEventListener("mouseleave", hide); p.addEventListener("blur", hide);
          vol.appendChild(p);
          if (d.labelled && s[0] === "singles") {
            text(vol, s[3] + barW / 2, Y2(v) - 8, v, { "text-anchor": "middle", fill: "var(--ink)",
              "font-size": "12", "font-weight": "600", "font-family": "var(--display)" });
          }
        });
      text(vol, gx + groupW / 2, H2 - padB2 + 20, d.year,
        { "text-anchor": "middle", fill: d.partial ? "var(--ink-3)" : "var(--ink-2)", "font-size": "12" });
      if (d.partial) text(vol, gx + groupW / 2, H2 - padB2 + 35, "partial",
        { "text-anchor": "middle", "font-size": "10" });
    });

    var tbody = document.getElementById("tableBody");
    if (tbody) {
      var ts = 0, td = 0;
      D.years.forEach(function (d) {
        ts += d.singles; td += d.doubles;
        var tr = document.createElement("tr");
        tr.innerHTML = "<th scope='row' style='text-align:left;font-weight:400'>" + d.year +
          (d.partial ? " *" : "") + "</th><td>" + d.singles + "</td><td>" + d.doubles +
          "</td><td>" + (d.singles + d.doubles) + "</td>";
        tbody.appendChild(tr);
      });
      var tot = document.createElement("tr");
      tot.innerHTML = "<th scope='row' style='text-align:left'>Total</th><td>" + ts + "</td><td>" +
        td + "</td><td>" + (ts + td) + "</td>";
      tbody.appendChild(tot);
      var btn = document.getElementById("tableToggle"), tbl = document.getElementById("dataTable");
      btn.addEventListener("click", function () {
        var open = !tbl.hidden;
        tbl.hidden = open;
        btn.setAttribute("aria-expanded", String(!open));
        btn.textContent = open ? "Show table" : "Hide table";
      });
    }
  }
})();
"""


def render_html(page: PlayerPage, standalone: bool = True) -> str:
    """The page. `standalone=False` drops the document skeleton, for publishing as an Artifact."""
    current_year = datetime.now(timezone.utc).year
    busiest = max((y.singles for y in page.year_counts), default=0)
    years = [
        {
            "year": y.year,
            "singles": y.singles,
            "doubles": y.doubles,
            "partial": y.year >= current_year,
            # Direct-label only the extremes and the open year; the rest are on hover and in the table.
            "labelled": y.singles in (busiest, min((c.singles for c in page.year_counts), default=0))
            or y.year >= current_year,
        }
        for y in page.year_counts
    ]
    data = _json_for_script({"history": page.history, "years": years})

    body = f"""<div class="wrap">
  {_header(page)}
  <div class="rating">
    <div>
      <div class="eyebrow">Singles UTR{" — as served to this session" if page.singles.masked else ""}</div>
      {_figure(page)}
      <p class="note">{_hero_note(page)}</p>
    </div>
    {_side_card(page)}
  </div>
  {_kpis(page)}
  {_trajectory_section(page)}
  {_volume_section(page)}
  {_college_section(page)}
  {_locked_section(page)}
  {_footer(page)}
</div>

<script type="application/json" id="page-data">{data}</script>
<script>{_SCRIPT}</script>"""

    head = (
        f"<title>{_e(page.name)}</title>\n"
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
        "family=Archivo:wght@500;600;700&family=Newsreader:opsz,wght@6..72,400;6..72,500&"
        'family=IBM+Plex+Mono:wght@400;500&display=swap">\n'
        f"<style>{_STYLE}</style>\n"
    )
    if not standalone:
        return head + body
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        f"{head}</head>\n<body>\n{body}\n</body>\n</html>\n"
    )
