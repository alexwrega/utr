"""Merge a batch of rows pulled from the browser runner into a --from-capture file.

Usage: python scripts/merge_capture.py captures/d1.json < batch.json
where batch.json is {"schools": [...status().schools...], "rows": [...status().new_rows...]}.
Rows are de-duplicated on (club_id, id), so re-merging an overlapping batch is harmless.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHOOL_KEYS = ["club_id", "school", "season", "error"]
ROW_KEYS = ["club_id", "id", "name", "class", "redshirt", "count", "nearest", "newest", "singles_utr", "display", "error"]

def main() -> None:
    target = Path(sys.argv[1])
    batch = json.load(sys.stdin)
    capture = json.loads(target.read_text()) if target.exists() else {"schools": []}
    by_club = {s["club_id"]: s for s in capture["schools"]}

    for meta in batch.get("schools", []):
        if isinstance(meta, list):  # compact [club_id, school, season, error]
            meta = dict(zip(SCHOOL_KEYS, meta))
        school = by_club.setdefault(meta["club_id"], {"club_id": meta["club_id"], "players": []})
        school.update({k: meta.get(k) for k in ("school", "season", "error")})
    added = 0
    for row in batch.get("rows", []):
        if isinstance(row, list):  # compact form pulled from the runner
            row = {k: v for k, v in zip(ROW_KEYS, row) if v is not None}
            row["redshirt"] = bool(row.get("redshirt"))
        club = row.pop("club_id")
        school = by_club.setdefault(club, {"club_id": club, "school": str(club), "season": None, "players": []})
        if all(p["id"] != row["id"] for p in school["players"]):
            school["players"].append(row)
            added += 1

    capture["schools"] = list(by_club.values())
    capture["captured_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    target.write_text(json.dumps(capture, ensure_ascii=False))
    players = sum(len(s["players"]) for s in capture["schools"])
    print(f"+{added} rows; {len(capture['schools'])} schools, {players} players in {target}")


if __name__ == "__main__":
    main()
