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

Needs Python 3.11 or newer (`python3 --version`; on macOS, `brew install python@3.12` if yours is older).

```bash
git clone https://github.com/alexwrega/utr.git
cd utr

python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Or with [uv](https://docs.astral.sh/uv/), which the rest of this README assumes:

```bash
uv venv --python 3.11 && uv pip install -e ".[dev]"
```

The commands (`utr-recruited-at`, `utr-player-page`, `utr-scrape-roster`) are on your PATH once the
venv is active. Without activating, call them by path: `.venv/bin/utr-player-page 1475065`.

Add `.[scraper]` instead of `.[dev]` for the browser scraper, which also needs
`playwright install chromium`.

## Authentication

Everything useful needs a signed-in session. Without one, UTR masks ratings (`12.xx`) and returns no history. The exception is [`utr-player-page`](#player-pages), which runs signed out and reports what was withheld instead of failing.

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

## Player pages

`utr-player-page` renders any single player as a standalone HTML page — the profile as a scouting card rather than a row in a table.

```bash
utr-player-page 1475065
utr-player-page "https://app.utrsports.net/profiles/1475065" --out joachim.html
utr-player-page 970607 --fragment --out -        # no <html> skeleton, for embedding
```

Unlike `utr-recruited-at`, **this runs without a session.** That matters because it changes what the page can say, so the page says it per field:

| | Signed out | Signed in |
| --- | --- | --- |
| Singles/doubles UTR | masked `11.xx`, rendered as a redaction with the 11.00–11.99 range stated | the real figure |
| Rating trajectory | absent — listed under "what a session would unlock" | weekly line chart over the whole history |
| Three-month rating | present (UTR leaves this one unmasked) | present |
| Match volume by year | present | present |
| Historic anchors, match results | listed as withheld, with the field and endpoint that holds them | filled in |

Everything else — rating status and reliability, career result counts, how many matches feed the current rating, college, conference, Power 6 — comes through either way. So do both rating systems (UTR and MyUTR, singles and doubles, each with its own status and reliability) and the physical profile UTR carries: height in both unit systems, dominant hand, backhand, birthplace. Masking applies to MyUTR exactly as it does to the headline rating. Where the player is on a college roster, the page also reads UTR's published top six and shows which lineup position they project into, using the real rating when it has one and the three-month figure when the rating is masked.

Nothing is estimated in place of a withheld field. A masked rating is shown as masked, and the page names the endpoint that would resolve it.

Two details the page encodes that aren't obvious from the payload:

- **The three-month direction.** UTR reports `changeDirection: "down"` alongside a *positive* `ratingDifference`; the sign runs opposite to the stated direction on every profile sampled. The direction field is treated as authoritative and the difference as a magnitude.
- **Partial profiles are cached apart from complete ones.** A signed-out page run caches under `profiles_partial`, so it can never leave `utr-recruited-at` reading a masked profile as though it were real.

## Browser scraping, slowly

`utr-scrape-roster` walks a roster in a real Chromium window signed in as you, opening each player's profile page one at a time.

```bash
uv pip install -e ".[scraper]" && playwright install chromium

utr-scrape-roster 1071                                    # sign in when the window opens
utr-scrape-roster 1071 1039 --classes fr,so --out caa.jsonl
utr-scrape-roster "https://app.utrsports.net/college/1071" --max-players 5   # try a few first
```

The loop is: roster page → player page → keep what loaded → wait a uniform random **60–300s** → next player. You sign in by hand once; `--profile-dir` (default `~/.utr-browser`) keeps the session for later runs, so subsequent runs can use `--headless`.

**What it keeps per player**, in order of trust:

1. The `/v1/player/{id}/profile` response *the page itself requested*. Structured, complete and unmasked, because it is the signed-in app making the call. This is the record every figure comes from.
2. The page's visible text, stored whole. Scraping UTR's DOM field-by-field would break on their next deploy, so nothing is parsed out of it — it is there for eyeballing, and for confirming the page rendered a real rating rather than a masked one.

**Pacing.** `utr-recruited-at` pages the API at 5s and UTR rate-limited a run on 2026-09-13. This waits 15–60x longer between players, and `--min-delay` refuses anything under 30s. A single HTTP 429 anywhere ends the run; nothing is retried. Sleeps happen in one-second slices, so Ctrl-C lands immediately.

**Nothing is lost.** Output is JSONL, appended and flushed per player. A run that dies, or that you stop, keeps everything up to that point, and rerunning skips what the file already holds. A player whose fetch failed is recorded as an error row and retried next run.

**It feeds the rest of the tool.** Complete profiles are written into `~/.utr-cache` as they land, so `utr-player-page <id>` and `utr-recruited-at` use them without refetching. `--warm-cache-only` replays an existing capture file into the cache without opening a browser.

| Flag | Default | Notes |
| --- | --- | --- |
| `--min-delay` / `--max-delay` | 60 / 300 | seconds between players, uniform random; under 30s is refused |
| `--classes` | whole roster | `fr,so,jr,sr,gr` or `all` |
| `--max-players N` | none | stop after N — use it for a first run |
| `--profile-dir` | `~/.utr-browser` | Chromium profile holding your session |
| `--browser PATH` | bundled | point at your own Chrome instead |
| `--headless` | off | only works once the profile is signed in |
| `--seed N` | none | reproducible delays, for a dry run |

A roster of 10 players at 60–300s averages about 15 minutes. Budget accordingly, and keep runs to schools you actually need.

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
