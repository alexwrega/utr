// Runs inside a signed-in app.utrsports.net tab, using that tab's own session.
//
//   __utr.start([1071, 1039], { classes: ['fr', 'so'] })   // kicks off a paced background job
//   await __utr.status(cursor, 25)                         // progress + players captured since `cursor`
//   __utr.stop()
//   __utr.capture()                                        // everything, in --from-capture format
//
// Pacing is strictly sequential: `delay` seconds (min 5, plus up to 25% jitter) between profile
// fetches, `teamPause` between schools, and a `cooldown` pause every `cooldownEvery` requests.
// With `stopOn429` (the default since UTR rate-limited a run on 2026-09-13), the first HTTP 429
// ends the job with state 'rate_limited' and nothing is retried. With it off, 429s back off
// exponentially from 7s, with one 5-minute cooldown. A history-less 200 is retried, not trusted.
// `skip` lists player IDs already captured, so re-running a school only fetches what's missing.
// Only the nearest-to-benchmark and newest weekly singles entries leave the page; the Python side
// (`utr-recruited-at --from-capture`) applies every data-quality rule.
(() => {
  if (window.__utr && window.__utr.version === 2) return 'already loaded';
  if (window.__utr && window.__utr.version !== 2) {
    const prev = window.__utr;
    if (prev.stop) prev.stop();
  }

  const API = 'https://api.utrsports.net';
  const WEB = 'https://prod-new-app.utrsports.net/api/web';
  const OFFSETS = { fr: 0, so: 1, jr: 2, sr: 3, gr: 4 };
  const ALIASES = {
    freshman: 'fr', 'redshirt freshman': 'fr', sophomore: 'so', junior: 'jr', senior: 'sr',
    graduate: 'gr', 'graduate student': 'gr', 'fifth year': 'gr',
  };
  const SINGLES = 'WeeklyAverage_Singles';
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const normClass = (raw) => {
    const k = (raw || '').trim().toLowerCase();
    return k in OFFSETS ? k : ALIASES[k] || null;
  };

  const job = {
    state: 'idle', schools: [], rows: [], log: [], requests: 0, done: 0, total: 0,
    current: null, error: null, stop: false, last: 0, lastCooldownAt: 0, opts: {},
  };
  const note = (m) => {
    job.log.push(`${new Date().toISOString().slice(11, 19)} ${m}`);
    if (job.log.length > 50) job.log.shift();
  };
  const save = () => {
    try { localStorage.setItem('utrCapture', JSON.stringify({ schools: job.schools })); } catch { /* optional */ }
  };
  // Sleep in short slices so stop() takes effect within a couple of seconds, even mid-cooldown.
  async function pause(ms) {
    const end = Date.now() + ms;
    while (Date.now() < end) {
      if (job.stop) throw Object.assign(new Error('stopped'), { stop: true });
      await sleep(Math.min(2000, end - Date.now()));
    }
  }

  async function get(url, { profile = false, valid = null } = {}) {
    let problem = '';
    for (let round = 1; round <= 2; round++) {
      let backoff = 7;
      for (let attempt = 1; attempt <= 5; attempt++) {
        if (job.stop) throw Object.assign(new Error('stopped'), { stop: true });
        const o = job.opts;
        if (o.cooldownEvery && job.requests && job.requests % o.cooldownEvery === 0 && job.lastCooldownAt !== job.requests) {
          job.lastCooldownAt = job.requests;
          note(`cooldown ${o.cooldown}s after ${job.requests} requests`);
          await pause(o.cooldown * 1000);
        }
        const interval = (profile ? o.delay : 2) * (1 + Math.random() * 0.25) * 1000;
        const wait = interval - (Date.now() - job.last);
        if (wait > 0) await pause(wait);
        job.last = Date.now();
        job.requests++;

        let r = null;
        try { r = await fetch(url, { credentials: 'include' }); } catch { problem = 'network error'; }
        if (r) {
          if (r.status === 401 || r.status === 403) {
            throw Object.assign(new Error(`HTTP ${r.status}: signed out or session expired`), { auth: true });
          }
          if (r.status === 429 && o.stopOn429) {
            throw Object.assign(new Error('HTTP 429: UTR is rate limiting; stopped without retrying'), { rateLimited: true });
          }
          if (r.status === 200) {
            const j = await r.json().catch(() => null);
            if (j && (!valid || valid(j))) return j;
            problem = 'incomplete payload';
          } else if (r.status === 429 || r.status >= 500) {
            problem = `HTTP ${r.status}`;
          } else {
            throw new Error(`HTTP ${r.status}`);
          }
        }
        if (attempt < 5) {
          note(`${problem}; retry ${attempt}/4 in ${backoff}s`);
          await pause(backoff * 1000);
          backoff *= 2;
        }
      }
      if (round === 1 && problem.includes('429')) {
        note('still rate limited; cooling down 300s');
        await pause(300000);
        continue;
      }
      break;
    }
    throw new Error(`${problem} after retries`);
  }

  function summarise(profile, cls, season) {
    const singles = (profile.extendedRatingProfile?.history || []).filter((h) => h.type === SINGLES);
    const day = (h) => h.date.slice(0, 10);
    const target = Date.UTC(parseInt(season, 10) - OFFSETS[cls] - 1, 8, 8); // 8 September
    let nearest = null, gap = Infinity, newest = null;
    for (const h of singles) {
      if (h.rating == null) continue;
      const g = Math.abs(Date.parse(`${day(h)}T00:00:00Z`) - target);
      if (g < gap) { gap = g; nearest = h; }
      if (!newest || day(h) > day(newest)) newest = h;
    }
    return {
      count: singles.length,
      nearest: nearest && [day(nearest), nearest.rating],
      newest: newest && [day(newest), newest.rating],
    };
  }

  async function run(clubIds) {
    job.state = 'running';
    const skip = new Set(job.opts.skip || []);
    try {
      for (const [i, club] of clubIds.entries()) {
        job.current = `club ${club}`;
        if (i > 0 && job.opts.teamPause) await pause(job.opts.teamPause * 1000);
        let school = null, skipReason = null;
        try {
          school = await get(`${WEB}/clubs/${club}/school`, { valid: (j) => j && 'roster' in j });
          if (!school.rosterYear) skipReason = 'no active roster season';
        } catch (e) {
          if (e.auth || e.stop || e.rateLimited) throw e;
          skipReason = e.message;
        }
        if (skipReason) {
          job.schools.push({ club_id: club, school: String(club), season: null, error: skipReason, players: [] });
          note(`club ${club} skipped: ${skipReason}`);
          job.done++;
          continue;
        }
        const entry = {
          club_id: club,
          school: school.shortName || school.displayName || school.name || String(club),
          season: school.rosterYear,
          players: [],
        };
        job.schools.push(entry);
        const classOf = (p) => p.classYearStatus || p.playerCollegeDetails?.gradClassName;
        const wanted = (school.roster || []).filter(
          (p) => job.opts.classes.includes(normClass(classOf(p))) && !skip.has(p.id),
        );
        for (const [k, p] of wanted.entries()) {
          const raw = classOf(p);
          job.current = `${entry.school}: ${p.displayName} (${k + 1}/${wanted.length})`;
          const row = { id: p.id, name: p.displayName, class: raw, redshirt: !!p.redshirt };
          try {
            const prof = await get(`${API}/v1/player/${p.id}/profile`, {
              profile: true,
              valid: (j) => (j.extendedRatingProfile?.history || []).some((h) => h.type === SINGLES),
            });
            if (String(prof.singlesUtrDisplay || '').toLowerCase().includes('x')) {
              throw Object.assign(new Error('ratings are masked: this tab is not signed in'), { auth: true });
            }
            Object.assign(row, summarise(prof, normClass(raw), entry.season), {
              singles_utr: prof.singlesUtr,
              display: prof.singlesUtrDisplay,
            });
          } catch (e) {
            if (e.auth || e.stop || e.rateLimited) throw e;
            row.error = `profile fetch failed: ${e.message}`;
          }
          entry.players.push(row);
          job.rows.push({ club_id: entry.club_id, ...row });
          save();
        }
        job.done++;
        save();
      }
      job.state = 'done';
    } catch (e) {
      job.state = e.stop ? 'stopped' : e.auth ? 'auth' : e.rateLimited ? 'rate_limited' : 'error';
      job.error = e.message;
      note(e.message);
    }
    job.current = null;
    save();
  }

  window.__utr = {
    version: 2,
    start(clubIds, opts = {}) {
      if (job.state === 'running') return 'already running';
      Object.assign(job, {
        state: 'starting', schools: [], rows: [], log: [], requests: 0, done: 0,
        total: clubIds.length, error: null, stop: false, lastCooldownAt: 0,
      });
      job.opts = {
        classes: ['fr', 'so'], delay: 6, teamPause: 5, cooldownEvery: 50, cooldown: 60,
        stopOn429: true, skip: [], ...opts,
      };
      if (job.opts.delay < 5) return 'delay must be at least 5 seconds';
      run(clubIds);
      return `started ${clubIds.length} club(s)`;
    },
    async status(cursor = 0, waitSeconds = 0) {
      const end = Date.now() + waitSeconds * 1000;
      while (Date.now() < end && (job.state === 'running' || job.state === 'starting')) await sleep(1000);
      return {
        state: job.state, done: job.done, total: job.total, requests: job.requests,
        current: job.current, error: job.error, cursor: job.rows.length,
        schools: job.schools.map((s) => ({ club_id: s.club_id, school: s.school, season: s.season, error: s.error || null })),
        new_rows: job.rows.slice(cursor),
        log: job.log.slice(-3),
      };
    },
    stop() { job.stop = true; return 'stopping within a couple of seconds'; },
    capture() { return { captured_at: new Date().toISOString(), schools: job.schools }; },
  };
  return 'loaded';
})();
