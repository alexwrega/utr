# utr-recruited-at

What UTR were a conference's current roster players *recruited at*, and how does a recruit's UTR today compare?

Comparing a recruit against a roster's current UTRs flatters the roster: those players have had one to four years of college development. This tool looks up each roster player's singles UTR in the September one year before they enrolled, and compares against that instead.

| Class on a 2026-27 roster | Enrolled | Benchmark |
| --- | --- | --- |
| Freshman | Fall 2026 | Sept 2025 |
| Sophomore | Fall 2025 | Sept 2024 |
| Junior | Fall 2024 | Sept 2023 |
| Senior | Fall 2023 | Sept 2022 |

Years come from the roster's `rosterYear`, so nothing is hardcoded to a season. Only freshmen and sophomores are included by default, because older classes reflect recruiting standards that have drifted.

> **Caveat.** This uses UTR's internal app API, which is undocumented and unsanctioned. That's fine for personal analysis, but don't build a product on it. For anything user-facing, the sanctioned path is the UTR Engage API over OAuth2.

## Install

```bash
uv venv --python 3.11 && uv pip install -e ".[dev]"
```

## Authentication

Everything useful needs a signed-in session. Without one, UTR masks ratings (`12.xx`) and returns no history.

1. Sign in at app.utrsports.net.
2. Open DevTools → Application → Cookies → `https://app.utrsports.net` and copy the value of the `jwt` cookie.
3. Either `export UTR_SESSION_COOKIE='<value>'` (a full `Cookie:` header string also works), or pass `--cookie-file` with a Netscape `cookies.txt`, a JSON cookie export, or a file holding the header.

If UTR answers 401 or 403, the tool tells you the session expired and stops; it doesn't retry. Don't commit the cookie: `.gitignore` excludes the usual cookie-file names.

## Usage

```bash
utr-recruited-at --conference 12 --gender M --target-utr 11.4
utr-recruited-at --college 1071 1039 --classes fr,so,jr --format csv --out caa.csv
utr-recruited-at --conference 12 --target-name "Jane Doe" --target-location "Raleigh, NC"
utr-recruited-at --conference 12 --all-classes --format json

# or paste links straight from the UTR site (a profile link makes that player the target)
utr-recruited-at --url "https://app.utrsports.net/search?type=colleges&conference=12&gender=M"
utr-recruited-at --url "https://app.utrsports.net/college/1071?tab=roster" "https://app.utrsports.net/profiles/970607"

# every NCAA D1 men's program, from a College Corner link (or --division 1); strongest Power 6 first
utr-recruited-at --url "https://app.utrsports.net/college-corner/teams?division=1&sort=power6" --max-teams 25 --format csv --out d1-top25.csv
```

All of D1 is 278 men's programs and roughly 1,200 freshman/sophomore profiles, which is about **two hours** cold at the required 5s spacing. Try `--max-teams` first. A roster that won't load is skipped with a note instead of stopping the run, and reruns within 24h reuse the cache. College Corner's location and UTR-fit filters aren't reproduced; the tool says so if your link sets them.

| Flag | Default | Notes |
| --- | --- | --- |
| `--conference ID` / `--college CLUB_ID…` | required | Club IDs are the numbers in `app.utrsports.net/college/<id>` (gender-specific: UNCW men 1071) |
| `--gender M\|W` | `M` | |
| `--target-utr` / `--target-name` | none | Without either, only the bands are reported. `--target-utr 11.0 12.0` takes a range and labels each school by its median recruited-at: **reach** (above 12.0), **target** (inside the range) or **safety** (below 11.0) |
| `--classes` | `fr,so` | Also `jr`, `sr`, `gr`, `all`, or `--all-classes` |
| `--format table\|csv\|json` | `table` | CSV/JSON carry every field, rounded to 2dp at most |
| `--cache-dir` | `~/.utr-cache` | 24h TTL; `--no-cache` bypasses it |

Progress goes to stderr and results to stdout, so `--format json > out.json` works.

## Browser mode (no cookie export)

If you'd rather not copy cookies, the fetching can run inside a browser tab where you're already signed in to UTR:

1. Open app.utrsports.net and sign in.
2. Paste [browser/runner.js](browser/runner.js) into that tab's DevTools console (or have Claude inject it), then start a job with `__utr.start([1071, 1039], { classes: ['fr', 'so'] })`.
3. Check progress with `await __utr.status(0)`, or stop it with `__utr.stop()`.
4. When it's done, save the output of `JSON.stringify(__utr.capture())` to a file and run `utr-recruited-at --from-capture capture.json --target-utr 11.4`.

The runner uses the same pacing as the CLI, and only the benchmark and newest entries per player leave the page. The Python side still applies every data-quality rule. Leave the tab open while it runs, and keep runs to a handful of schools.

## Output

**Per player:** school, name, class, recruited-at UTR, the actual snapshot date used, gap from the target date, current UTR, delta, years elapsed, annualised delta, flags, and an exclusion reason.

**Per school:**
- the recruited-at band (min–max), median and `n`
- mean annualised development delta, which shows whether players improve or stagnate at that program
- with a target: how many current players were recruited above the target, and the target's percentile within the band (ties count half)

The table view ends with schools ranked by how favourably the target compares.

## How it works: endpoints

| Purpose | Endpoint | Auth |
| --- | --- | --- |
| Profile + weekly rating history | `GET api.utrsports.net/v1/player/{id}/profile` → `extendedRatingProfile.history` | bearer or `jwt` cookie |
| Roster with class years | `GET prod-new-app.utrsports.net/api/web/clubs/{clubId}/school` → `rosterYear`, `roster[].classYearStatus` | `jwt` cookie |
| Conference → colleges | `GET api.utrsports.net/v2/search/colleges?top=100&skip=N`, filtered locally on `conferenceId` / `mensClubId` | none |
| Player search | `GET api.utrsports.net/v2/search/players?query=…` | none |

I found the roster endpoint by reading what the roster tab requests when you're signed in, so no browser scraper is needed. The college search ignores every conference filter I tried (`conference`, `conferenceId`, `conferenceIds`…). The tool therefore pages through the whole index once (~14 requests) and caches it for 24h.

## Rating rules

- Only `WeeklyAverage_Singles` history entries are used.
- The benchmark target is 8 September of the relevant year. Weekly snapshots land on Mondays, so the nearest entry is the first full week of September (2025-09-08, 2024-09-09). There's no interpolation. The date actually used is recorded, and a nearest entry more than 60 days away marks the row unreliable and excludes it.
- **Sanity check:** the newest weekly entry is compared with the profile's displayed UTR.
  - A gap over **0.10** flags that player, e.g. "profile shows 13.58, latest weekly 13.79" (a real case: Connor Van Schalkwyk). The player stays in, and the recruited-at value isn't affected.
  - A gap over **1.0** aborts the run with `ScaleDriftError`, since only a change in what the field means would produce that.
  - The spec's 0.01 was too tight: weekly averages routinely trail the live rating (Alejandro Peña: 11.90 weekly against 11.92 displayed).
- Players with fewer than 100 weekly singles entries are excluded, with the reason given. These are usually internationals who joined UTR recently.
- Recruited-at values below 8.0 are flagged as likely walk-ons but not excluded.
- Schools with `n` of 1 or 2 carry a note saying so.
- Missing data is `null` plus a reason, never a guess.

## Rate limiting

The API returns HTTP 429 after roughly 8–10 rapid requests, and a throttled profile can come back as a 200 with no history. So the client:
- fetches strictly sequentially, at least 5s between profile requests
- backs off exponentially from 7s on 429 and 5xx (5 attempts)
- treats a history-less 200 as a failure to retry
- caches every good profile, roster and college index for 24h

A nine-school conference is roughly 30 profiles, or about three minutes cold. Ctrl-C is safe; rerun to resume from the cache.

## Tests

```bash
.venv/bin/pytest                                   # offline: rules, retries, auth, CLI end to end
UTR_SESSION_COOKIE=... .venv/bin/pytest -m live    # real UNCW regression (~30s)
```

The live test asserts the hand-verified UNC Wilmington recruited-at figures exactly (Jacquemin 11.83, Castellanos Mogollon 11.31, Coello 11.30, Peña 10.74, Marin 11.49, Zeiger 10.47). Current UTRs are only checked within a tolerance, since they change weekly.

Other CAA men's club IDs: Elon 1039, Charleston 1240, William & Mary 1101, Drexel 1262, Monmouth 1276, Campbell 1079, Hampton 1416, Hofstra 1284.
