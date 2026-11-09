// Pure decision rules for the global writer. No network and no clock access in
// here (the caller passes `now`), so the rules can be tested in plain Node.
//
// Jargon: the "writer" is the one host allowed to change accounts. The "epoch" is
// a counter that goes up every time the writer changes, so an old writer can be
// told apart from the current one. A "version vector" is the per-machine write
// counters kept in each accounts database (see accounts/replication.py); two
// hosts hold the same data exactly when their vectors are equal.

const num = (v, d) => (v === undefined || v === "" || !Number.isFinite(Number(v)) ? d : Number(v));

// All numbers come from [vars] in wrangler.toml; these are the defaults.
export function config(env = {}) {
  return {
    hosts: String(env.HOSTS || "laptop,tablet").split(",").map((s) => s.trim()).filter(Boolean),
    domain: env.HOST_DOMAIN || "yabaisearch.org",
    writePrefixes: String(env.WRITE_PREFIXES || "/accounts/signup/,/api/account/,/admin/")
      .split(",").map((s) => s.trim()).filter(Boolean),
    lockMs: num(env.LOCK_MS, 30 * 60 * 1000), // no planned move this soon after a move
    leadMs: num(env.LEAD_MS, 5 * 60 * 1000), // a challenger must stay faster this long
    slowMs: num(env.SLOW_MS, 3000), // writer median above this ignores the lock
    deadMs: num(env.DEAD_MS, 10000), // no answer for this long means dead
    margin: num(env.MARGIN, 0.2), // "faster" means at least this much faster
    holdMs: num(env.HOLD_MS, 25000), // a write waits this long for a writer
    probeMs: num(env.PROBE_MS, 15000), // ms between probe rounds while active
    failingProbeMs: num(env.FAILING_PROBE_MS, 10000), // ... while the writer is failing
    hedgeMs: num(env.HEDGE_MS, 800), // read hedging delay
    writeTimeoutMs: num(env.WRITE_TIMEOUT_MS, 15000),
  };
}

export const hostUrl = (cfg, name) => `https://${name}.${cfg.domain}`;

// A write is an unsafe method on an account path. Everything else (search,
// convert, login, logout) works on any host, because logins use signed cookies.
export function isWrite(method, path, prefixes) {
  if (["GET", "HEAD", "OPTIONS"].includes(String(method).toUpperCase())) return false;
  return prefixes.some((p) => path.startsWith(p));
}

export function median(nums) {
  if (!nums.length) return null;
  const a = [...nums].sort((x, y) => x - y);
  const mid = a.length >> 1;
  return a.length % 2 ? a[mid] : (a[mid - 1] + a[mid]) / 2;
}

// --- health of one host ---------------------------------------------------------

export const newHealth = () => ({ samples: [], failSince: null });

// Record one probe. `startedAt` is when the probe began: the 10 second "dead"
// clock starts at the FIRST failed probe, and a probe that hangs counts from its start.
export function recordProbe(h, ok, ms, startedAt) {
  if (ok) {
    h.samples.push(ms);
    if (h.samples.length > 5) h.samples.shift(); // median of the last 5
    h.failSince = null;
  } else if (h.failSince === null) {
    h.failSince = startedAt;
  }
}

export const up = (h) => !!h && h.failSince === null && h.samples.length > 0;
export const speed = (h) => (h ? median(h.samples) : null);

// --- version vectors --------------------------------------------------------------

export function sameVector(a, b) {
  if (!a || !b) return false;
  for (const k of new Set([...Object.keys(a), ...Object.keys(b)])) {
    if ((a[k] || 0) !== (b[k] || 0)) return false;
  }
  return true;
}
const total = (v) => (v ? Object.values(v).reduce((s, n) => s + n, 0) : -1);

// --- the writer state ---------------------------------------------------------------

export const initialState = (cfg) => ({ writer: cfg.hosts[0], epoch: 1, movedAt: 0, lead: null });

export function applyMove(state, to, now) {
  state.writer = to;
  state.epoch += 1;
  state.movedAt = now; // any move, including a failover, restarts the lock
  state.lead = null;
}

// Decide what to do now. `known` maps host -> its version vector (null when the
// host did not answer, undefined when not fetched yet). Returns one of:
//   { action: "stay" }
//   { action: "need-vectors", hosts: [...] }   fetch these, then call again
//   { action: "move", to, reason }             reason: dead | slow | faster
// It updates state.lead (who has been clearly faster, and since when).
export function decide(state, health, now, cfg, known = {}) {
  const w = health[state.writer];
  const others = cfg.hosts.filter((n) => n !== state.writer);
  const responsive = others.filter((n) => up(health[n]));

  // 1. Dead writer: no answer for deadMs. Move at once, ignoring every other rule,
  // to the responsive host with the most recent data (speed breaks ties).
  if (w && w.failSince !== null && now - w.failSince >= cfg.deadMs) {
    if (!responsive.length) return { action: "stay" }; // everything is down
    const missing = responsive.filter((n) => known[n] === undefined);
    if (missing.length) return { action: "need-vectors", hosts: missing };
    const ranked = [...responsive].sort(
      (a, b) =>
        total(known[b]) - total(known[a]) || speed(health[a]) - speed(health[b]) || (a < b ? -1 : 1),
    );
    return { action: "move", to: ranked[0], reason: "dead" };
  }

  // 2. Who has been at least `margin` faster than the writer?
  const wm = speed(w);
  let best = null;
  if (wm !== null) {
    for (const n of responsive) {
      const m = speed(health[n]);
      if (m <= wm * (1 - cfg.margin) && (best === null || m < speed(health[best]))) best = n;
    }
  }
  if (!best) state.lead = null;
  else if (!state.lead || state.lead.host !== best) state.lead = { host: best, since: now };
  if (!best) return { action: "stay" };

  // 3. Planned move (lock expired and lead long enough) or slow writer (lock ignored;
  // the median of 5 probes is the debounce, so one slow answer cannot trigger it).
  const planned = now - state.lead.since >= cfg.leadMs && now - state.movedAt >= cfg.lockMs;
  const slow = wm > cfg.slowMs;
  if (!planned && !slow) return { action: "stay" };

  // Either way the new host must be fully caught up first. If the old writer is too
  // slow to answer the check, wait: the dead rule will handle it.
  const missing = [state.writer, best].filter((n) => known[n] === undefined);
  if (missing.length) return { action: "need-vectors", hosts: missing };
  if (!sameVector(known[best], known[state.writer])) return { action: "stay" };
  return { action: "move", to: best, reason: planned ? "faster" : "slow" };
}
