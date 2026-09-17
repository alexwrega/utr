"""Short target/safety report for a browser capture, marking schools not shown in earlier reports.

Usage: python scripts/fit_report.py captures/d1-2026-09-13.json 11.0 12.0
Remembers what it has already reported in <capture>.fit-seen.json next to the capture.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from utr_recruited_at.analysis import rank_for_target
from utr_recruited_at.capture import analyse_capture, load_capture


def ordinal(x: float) -> str:
    n = int(round(x))
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def main() -> None:
    path, low, high = Path(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3])
    seen_path = path.with_name(path.stem + ".fit-seen.json")
    seen: set[str] = set(json.loads(seen_path.read_text())) if seen_path.exists() else set()

    _players, summaries = analyse_capture(load_capture(path), ["fr", "so"], low, high)
    ranking = rank_for_target(summaries)
    counts = {f: sum(1 for s in ranking if s.fit == f) for f in ("target", "safety", "reach")}
    print(f"{len(summaries)} schools analysed: {counts['target']} target, {counts['safety']} safety, "
          f"{counts['reach']} reach, {len(summaries) - len(ranking)} with no usable players")

    new_keys = []
    for fit in ("target", "safety"):
        group = [s for s in ranking if s.fit == fit]
        if not group:
            continue
        print(f"\n{fit.upper()} ({len(group)})")
        for s in group:
            key = f"{s.college_id}:{fit}"
            mark = "NEW " if key not in seen else "    "
            if key not in seen:
                new_keys.append(key)
            caveat = f"  [{s.note}]" if s.note else ""
            print(f"  {mark}{s.school}: median {s.median:.2f}, band {s.band_min:.2f}-{s.band_max:.2f}, "
                  f"{s.recruited_in_range}/{s.n} in range, {s.recruited_above_high} above {high:.1f}, "
                  f"{ordinal(s.target_percentile)}-{ordinal(s.target_percentile_high)} pctile{caveat}")

    seen.update(new_keys)
    seen_path.write_text(json.dumps(sorted(seen)))
    print(f"\nnew target/safety schools this report: {len(new_keys)}")


if __name__ == "__main__":
    main()
