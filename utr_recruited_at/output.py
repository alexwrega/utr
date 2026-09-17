"""Render results as a terminal table, CSV or JSON."""

from __future__ import annotations

import csv
import io
import json
from typing import Any, TextIO

from rich.console import Console
from rich.table import Table

from .analysis import PlayerRow, SchoolSummary

CLASS_LABELS = {"fr": "Fr", "so": "So", "jr": "Jr", "sr": "Sr", "gr": "Gr"}


def _fmt(x: float | None, digits: int = 2) -> str:
    return "—" if x is None else f"{x:.{digits}f}"


def _ordinal(x: float | None) -> str:
    if x is None:
        return "—"
    n = int(round(x))
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def render_table(
    players: list[PlayerRow],
    summaries: list[SchoolSummary],
    ranking: list[SchoolSummary],
    target_utr: float | None,
    console: Console,
    target_high: float | None = None,
) -> None:
    pt = Table(title="Players: UTR one year before enrolment", header_style="bold")
    for col, justify in [
        ("School", "left"), ("Player", "left"), ("Class", "center"),
        ("Recruited at", "right"), ("From date", "left"), ("Current", "right"),
        ("Δ", "right"), ("Yrs", "right"), ("Δ/yr", "right"), ("Notes", "left"),
    ]:
        pt.add_column(col, justify=justify)  # type: ignore[arg-type]
    for r in players:
        notes = ", ".join(r.flags + ([r.reason] if r.reason else []))
        style = "dim" if not r.included else ("yellow" if r.flags else None)
        pt.add_row(
            r.school, r.name, CLASS_LABELS.get(r.class_code, r.class_code),
            _fmt(r.recruited_at_utr), r.recruited_at_date or "—", _fmt(r.current_utr),
            _fmt(r.delta), _fmt(r.years_elapsed, 1), _fmt(r.annualised_delta), notes,
            style=style,
        )
    console.print(pt)

    st = Table(title="Schools: recruited-at bands", header_style="bold")
    st.add_column("School")
    st.add_column("n", justify="right")
    st.add_column("Band", justify="center")
    st.add_column("Median", justify="right")
    st.add_column("Mean Δ/yr", justify="right")
    ranged = target_utr is not None and target_high is not None
    if ranged:
        st.add_column(f"In {target_utr:.2f}–{target_high:.2f}", justify="right")
        st.add_column(f"Above {target_high:.2f}", justify="right")
        st.add_column(f"Pctile @{target_utr:.2f}", justify="right")
        st.add_column(f"Pctile @{target_high:.2f}", justify="right")
        st.add_column("Fit")
    elif target_utr is not None:
        st.add_column(f"Above {target_utr:.2f}", justify="right")
        st.add_column("Target pctile", justify="right")
    st.add_column("Note")
    for s in summaries:
        band = "—" if s.band_min is None else f"{s.band_min:.2f}–{s.band_max:.2f}"
        cells = [s.school, str(s.n), band, _fmt(s.median), _fmt(s.mean_annualised_delta)]
        if ranged:
            cells += [
                "—" if s.recruited_in_range is None else str(s.recruited_in_range),
                "—" if s.recruited_above_high is None else str(s.recruited_above_high),
                _fmt(s.target_percentile, 0), _fmt(s.target_percentile_high, 0), s.fit or "—",
            ]
        elif target_utr is not None:
            cells += [
                "—" if s.recruited_above_target is None else str(s.recruited_above_target),
                _fmt(s.target_percentile, 0),
            ]
        cells.append(s.note or "")
        st.add_row(*cells)
    console.print(st)

    if ranged and ranking:
        console.print(
            f"\n[bold]Where an {target_utr:.2f}–{target_high:.2f} recruit fits[/bold] "
            "[dim](ranked by percentile at the middle of the range)[/dim]"
        )
        for fit, label in [("target", "Target"), ("safety", "Safety"), ("reach", "Reach")]:
            group = [s for s in ranking if s.fit == fit]
            if not group:
                continue
            console.print(f"\n  [bold]{label}[/bold] ({len(group)})")
            for i, s in enumerate(group, 1):
                caveat = f"  [dim]({s.note})[/dim]" if s.note else ""
                console.print(
                    f"  {i:>3}. {s.school}: median {s.median:.2f}, "
                    f"{_ordinal(s.target_percentile)}–{_ordinal(s.target_percentile_high)} percentile, "
                    f"{s.recruited_in_range} of {s.n} recruited in range, {s.recruited_above_high} above{caveat}"
                )
    elif target_utr is not None and ranking:
        console.print(f"\n[bold]Where a target of {target_utr:.2f} compares most favourably[/bold]")
        for i, s in enumerate(ranking, 1):
            caveat = f"  [dim]({s.note})[/dim]" if s.note else ""
            console.print(
                f"  {i:>2}. {s.school}: {_ordinal(s.target_percentile)} percentile, "
                f"{s.recruited_above_target} of {s.n} recruited above{caveat}"
            )


def _csv_value(v: Any) -> Any:
    if isinstance(v, list):
        return ";".join(str(x) for x in v)
    return "" if v is None else v


def render_csv(players: list[PlayerRow], summaries: list[SchoolSummary]) -> str:
    buf = io.StringIO()
    for title, rows in [("players", [p.to_dict() for p in players]),
                        ("schools", [s.to_dict() for s in summaries])]:
        if not rows:
            continue
        buf.write(f"# {title}\n")
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _csv_value(v) for k, v in row.items()})
        buf.write("\n")
    return buf.getvalue()


def render_json(
    players: list[PlayerRow],
    summaries: list[SchoolSummary],
    ranking: list[SchoolSummary],
    meta: dict[str, Any],
) -> str:
    return json.dumps(
        {
            "meta": meta,
            "players": [p.to_dict() for p in players],
            "schools": [s.to_dict() for s in summaries],
            "ranking": [s.college_id for s in ranking],
        },
        indent=2,
        ensure_ascii=False,
    )


def write_out(text: str, stream: TextIO) -> None:
    stream.write(text)
    if not text.endswith("\n"):
        stream.write("\n")
