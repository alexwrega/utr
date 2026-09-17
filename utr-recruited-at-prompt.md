# Prompt: build a `utr-recruited-at` CLI tool

Paste everything below the line into Claude Code.

---

Build a Python CLI tool called `utr-recruited-at`.

## The question it answers

For a given college conference, what UTR were the current roster players *actually recruited at* — measured one year before they enrolled — and how does a target recruit's UTR today compare against that benchmark?

## Why this framing (do not skip this — it's the whole point)

Comparing a recruit's current UTR against a team's *current* roster UTRs is misleading. Every player on that roster has had one to four extra years of college development, so the recruit looks weaker than he is. The honest comparison is recruit-now versus roster-player-at-the-same-point-in-their-own-timeline.

So for every roster player, look up what their UTR was in the September one year before they enrolled:

| Class on the 2026-27 roster | Enrolled | Benchmark date |
| :---- | :---- | :---- |
| Freshman | Fall 2026 | Sept 2025 |
| Sophomore | Fall 2025 | Sept 2024 |
| Junior | Fall 2024 | Sept 2023 |
| Senior | Fall 2023 | Sept 2022 |

Derive this from the roster season rather than hardcoding years, so the tool still works next season. Default to freshmen and sophomores only — older classes reflect recruiting standards that have drifted — with a flag to include all classes.

## CLI shape

utr-recruited-at \--conference 12 \--gender M \--target-utr 11.4

                 \[--college 1071 1039 ...\]     \# explicit college IDs, skip conference lookup

                 \[--classes fr,so\]              \# default fr,so

                 \[--format table|csv|json\]      \# default table

                 \[--out results.csv\]

                 \[--cache-dir \~/.utr-cache\]     \# default on, 24h TTL

`--target-utr` is optional; without it the tool just reports the recruited-at bands.

## Data sources

### Player profile \+ rating history (verified working)

GET https://api.utrsports.net/v1/player/{playerId}/profile

Equivalent aliases that return the same payload: `https://api.utrsports.net/v2/player/{playerId}` and `https://app.utrsports.net/api/v1/player/{playerId}/profile`.

The field you need is `extendedRatingProfile.history` — an array of weekly snapshots:

{"id": 599077594, "ratingStatus": 3, "rating": 12.44, "ratingDisplay": "12.44",

 "type": "WeeklyAverage\_Singles", "date": "2026-09-07T00:00:00"}

Filter to `type === "WeeklyAverage_Singles"` (there are doubles entries too). Entries are weekly, newest first, and go back roughly six years for an established player. The newest entry matches the profile's displayed UTR to within 0.01, which is your scale sanity check — assert this on every fetch and fail loudly if it drifts, because it means the field semantics changed.

To resolve a benchmark date, pick the history entry whose date is *nearest* the target, and record the actual date you landed on alongside the value. Do not interpolate. If the nearest entry is more than 60 days from the target, mark the row as unreliable rather than silently using a distant value.

Other useful profile fields: `singlesUtrDisplay`, `singlesUtr`, `displayName`, `gradYearCollege`, `playerCollegeDetails`, `nationality`, `location`.

### Player search (verified working)

GET https://api.utrsports.net/v2/search/players?query={q}\&top=25\&skip=0

Returns `hits[].source` with `id`, `displayName`, `location.display`, `singlesUtr`, `gender`. Use this to resolve the target player by name if `--target-name` is given instead of `--target-utr`. Note it matches surnames as well as forenames, so disambiguate on location and rating.

### Rosters — no API found, you'll need to solve this

I probed and got 404 on all of these: `/v1/college/{id}`, `/v2/college/{id}`, `/v1/college/{id}/roster`, `/v2/college/{id}/roster`, `/v1/team/{id}/roster`. Before writing a browser scraper, spend a few minutes probing for the real endpoint — load `https://app.utrsports.net/college/1071?tab=roster` in a browser with devtools open and read the XHR the roster table actually fires. If you find it, use it and delete the scraper path.

Fallback that is known to work: Playwright against `https://app.utrsports.net/college/{collegeId}?tab=roster`. The roster renders client-side, so wait for the table (2–3 seconds, or better, wait for the selector). Then extract with this heuristic, which is tested:

const YEARS \= \['Freshman','Sophomore','Junior','Senior','Graduate'\];

const links \= \[...document.querySelectorAll('a\[href\*="/profiles/"\]')\];

const seen \= new Set(); const out \= \[\];

for (const a of links) {

  const id \= a.getAttribute('href').split('/profiles/')\[1\];

  if (seen.has(id)) continue;

  seen.add(id);

  let el \= a, found \= null;

  for (let i \= 0; i \< 8 && el; i++) {

    el \= el.parentElement;

    if (\!el) break;

    const t \= el.innerText || '';

    const y \= YEARS.find(y \=\> t.includes(y));

    if (y && t.length \< 400\) { found \= { year: y, text: t }; break; }

  }

  out.push({ id, name: (a.innerText || '').trim(),

             year: found ? found.year : null,

             utr: found ? (found.text.match(/\\d+\\.\\d\\d/) || \[\])\[0\] : null });

}

Each roster row links to the player twice (name and "paid hit"), hence the dedupe. The first decimal match in the row text is the singles UTR; the second is doubles.

### Conference → college list

`https://app.utrsports.net/search?type=colleges&conference={id}&gender=M` lists the conference's programs with their Power 6 score, division and location. I did not find a JSON endpoint behind it — probe for one the same way, else scrape it. CAA is conference 12\. The page needs a moment to hydrate before the rows exist.

## Auth

The API uses cookie-based session auth. From a browser already signed in to UTR, `fetch(url, {credentials: 'include'})` works. For a CLI, expose `--cookie-file` pointing at an exported cookie jar, or read `UTR_SESSION_COOKIE` from the environment. Do not put credentials in source, in URLs, or in the cache filenames. If a request comes back 401/403, say plainly that the session expired rather than retrying.

## Rate limiting — this will bite you

The API starts returning **HTTP 429 after roughly 8–10 rapid requests**. Parallel fetching fails almost immediately, and a failed profile fetch is silent in the sense that `extendedRatingProfile` simply comes back absent — so a naive implementation produces a table full of blanks rather than an error.

Requirements:

- Fetch sequentially, never with `asyncio.gather` fan-out or a thread pool.  
- Minimum 5 seconds between profile requests.  
- On 429, exponential backoff starting at 7 seconds, at least 4 attempts.  
- Treat a 200 whose payload lacks `extendedRatingProfile.history` as a failure to retry, not as a player with no data.  
- Cache every successful profile response to disk keyed by player ID with a 24-hour TTL, so reruns and crashes don't re-hammer the API. This matters a lot — a nine-school conference is \~30 profile fetches and about three minutes of wall clock.  
- Print progress per player so a long run doesn't look hung.

## Data quality rules

- Exclude any player whose singles history has **fewer than 100 weekly entries**. These are typically internationals who only entered the UTR system recently, and their benchmark date falls outside their history entirely. Real example: College of Charleston's Edoardo Santoni has 48 entries starting Oct 2025 and no usable one-year-out figure, while teammates have 300–530.  
- Flag but don't exclude players whose recruited-at value sits below 8.0 — usually walk-ons rather than recruits, and they drag the band misleadingly wide.  
- Report `n` per school. A single-freshman school is a data point, not a band; say so in the output.  
- Never fabricate a value when history is missing. Emit `null` and a reason.

## Output

Per player: school, name, class, recruited-at UTR, the actual date that value came from, current UTR, delta, and years elapsed.

Per school: the recruited-at band (min–max), median, n, and mean annualised development delta — that last one is the interesting signal, since it separates programs where players improve from programs where they stagnate.

If `--target-utr` is supplied, add per school: how many current players were recruited above the target, and the target's percentile within that school's band. Then print a summary ranking schools by how favourably the target compares.

Table output should be readable in a terminal. CSV and JSON should be complete — every field, no rounding beyond two decimals.

## Validation

Build a regression test against these values, which I verified by hand. All are UNC Wilmington (college ID 1071), benchmark date 2025-09-08 for freshmen and 2024-09-09 for sophomores:

| Player | Player ID | Class | Recruited at | Current |
| :---- | :---- | :---- | :---- | :---- |
| Gabriel Jacquemin | 5260045 | Fr | 11.83 | 12.43 |
| David Castellanos Mogollon | 4232316 | Fr | 11.31 | 11.47 |
| Jose Pablo Coello | 2554674 | Fr | 11.30 | 11.79 |
| Alejandro Peña | 1917583 | Fr | 10.74 | 11.92 |
| Tomas Marin | 1963017 | So | 11.49 | 12.32 |
| Noah Zeiger | 970607 | So | 10.47 | 12.58 |

Current UTRs will drift week to week; assert the recruited-at figures exactly and the current ones within a tolerance.

Other CAA college IDs for wider testing: Elon 1039, Charleston 1240, William & Mary 1101, Drexel 1262, Monmouth 1276, Campbell 1079, Hampton 1416, Hofstra 1284\.

## Engineering notes

Python 3.11+, `httpx` for requests, `playwright` only if the roster scraper is actually needed, `rich` for table output. Keep the UTR client in its own module with a clean interface (`get_player_profile`, `get_rating_at_date`, `search_players`, `get_roster`, `get_conference_colleges`) so it can be lifted into another project later. Type hints throughout. No secrets in the repo.

One caveat to carry in the README: this uses UTR's internal app API, which is undocumented and unsanctioned, and is fine for personal analysis but not something to build a product on. The sanctioned path for anything user-facing is the UTR Engage API over OAuth2.  
