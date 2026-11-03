// Cloudflare Worker in front of yabaisearch.org.
//
// Reads (search, convert, login, ...) go to the visitor's "local host": the
// fastest host for them, remembered in the yabai_local cookie. If it fails or is
// slow, the next hosts are tried (hedging: a backup request is sent early instead
// of waiting for a failure; GET and HEAD only, so nothing runs twice).
//
// Writes (signup, settings, admin) go ONLY to the global writer chosen by the
// Coordinator (coordinator.js). They carry X-Yabai-Writer and X-Yabai-Writer-Epoch
// so a host can refuse a write meant for a different writer or an older epoch.
// While the writer is failing, a write waits (up to HOLD_MS) for the new writer.
//
// Extra routes:
//   /__probe          how long each host takes from the edge nearest the visitor
//   /__control/state  who the writer is (needs X-Origin-Secret; hosts use it)
//   /__repl/*         always 404 here: replication talks to hosts directly
//
// Every answer carries X-Origin: <host>.

import { config, hostUrl, isWrite } from "./rules.js";
export { Coordinator } from "./coordinator.js";

// Statuses that mean "the request never reached the app" (tunnel is gone).
const NEVER_REACHED = new Set([502, 530]);
const WRITER_HEADERS = ["X-Yabai-Writer", "X-Yabai-Writer-Epoch"];
const RW_COOKIE = "yabai_rw=1; Max-Age=30; Path=/; Secure; SameSite=Lax"; // read-your-writes

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const json = (v, extra = {}) =>
  new Response(JSON.stringify(v), {
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store", ...extra },
  });

// --- talking to the Coordinator -----------------------------------------------------

async function coordinator(env, path, body) {
  const stub = env.COORDINATOR.get(env.COORDINATOR.idFromName("global"));
  const res = await stub.fetch(
    "https://coordinator" + path,
    body ? { method: "POST", body: JSON.stringify(body) } : undefined,
  );
  if (!res.ok) throw new Error(`coordinator ${res.status}`);
  return res.json();
}

// Reads may use a copy up to 10 seconds old (kept per Worker instance).
let cached = null;
async function readState(env) {
  if (cached && Date.now() - cached.at < 10000) return cached.value;
  try {
    const value = await coordinator(env, "/state");
    cached = { at: Date.now(), value };
    return value;
  } catch {
    return cached ? cached.value : null;
  }
}

function parseCookies(header) {
  const out = {};
  for (const part of (header || "").split(";")) {
    const i = part.indexOf("=");
    if (i > 0) out[part.slice(0, i).trim()] = part.slice(i + 1).trim();
  }
  return out;
}

// Compare two secrets without stopping at the first difference.
function safeEqual(a, b) {
  if (!a || !b || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

// --- sending one request to one host ---------------------------------------------------

function sendTo(request, env, cfg, name, body, extra, signal) {
  const url = new URL(request.url);
  const target = new URL(url.pathname + url.search, hostUrl(cfg, name));
  const headers = new Headers(request.headers);
  for (const h of WRITER_HEADERS) headers.delete(h); // a visitor must not forge these
  if (env.ORIGIN_SECRET) headers.set("X-Origin-Secret", env.ORIGIN_SECRET);
  const ip = request.headers.get("CF-Connecting-IP");
  if (ip) headers.set("X-Forwarded-For", ip); // the app's logs and rate limits see the visitor
  for (const [k, v] of Object.entries(extra || {})) headers.set(k, v);
  return fetch(target, { method: request.method, headers, body, redirect: "manual", signal });
}

function stamp(res, name, extraCookie) {
  const out = new Response(res.body, res);
  out.headers.set("X-Origin", name);
  if (extraCookie) out.headers.append("Set-Cookie", extraCookie);
  return out;
}

function unavailable(text, retry = "10") {
  return new Response(text, {
    status: 503,
    headers: { "Retry-After": retry, "Cache-Control": "no-store" },
  });
}

// --- reads ---------------------------------------------------------------------------------

// The order to try hosts: the visitor's local host, then the rest (up first, fastest first).
function readOrder(request, cfg, state) {
  const cookies = parseCookies(request.headers.get("Cookie"));
  const info = (n) => (state && state.hosts && state.hosts[n]) || {};
  const rest = (first) =>
    cfg.hosts
      .filter((n) => n !== first)
      .sort((a, b) => (info(b).up ? 1 : 0) - (info(a).up ? 1 : 0) || (info(a).ms ?? 1e9) - (info(b).ms ?? 1e9));
  let first = cfg.hosts.includes(cookies.yabai_local) ? cookies.yabai_local : null;
  if (cookies.yabai_rw && state && state.writer) first = state.writer; // just wrote: read from the writer
  if (!first) first = rest(null).find((n) => info(n).up) || (state && state.writer) || cfg.hosts[0];
  return [first, ...rest(first)];
}

async function read(request, env, cfg, body) {
  const safe = request.method === "GET" || request.method === "HEAD";
  const order = readOrder(request, cfg, await readState(env));
  const ctrls = Object.fromEntries(order.map((n) => [n, new AbortController()]));

  // Resolves with {name, res} on a usable answer, rejects otherwise.
  async function attempt(name) {
    let res;
    try {
      res = await sendTo(request, env, cfg, name, body, null, ctrls[name].signal);
    } catch (err) {
      throw new Error(`${name}: ${err}`);
    }
    const bad = safe ? res.status >= 500 : NEVER_REACHED.has(res.status);
    if (bad) {
      if (res.body) res.body.cancel().catch(() => {});
      throw new Error(`${name}: status ${res.status}`);
    }
    return { name, res };
  }

  let winner;
  if (!safe) {
    // No hedging for POST and friends: the next host only if this one was unreachable.
    for (const name of order) {
      try { winner = await attempt(name); break; } catch { /* try the next host */ }
    }
    if (!winner) return unavailable("No server is reachable. Try again shortly.");
  } else {
    // Host i starts when its timer fires (hedgeMs * i) or when host i-1 fails.
    const timers = [];
    const starts = [];
    const promises = order.map(
      (name, i) =>
        new Promise((resolve, reject) => {
          let started = false;
          starts[i] = () => {
            if (started) return;
            started = true;
            attempt(name).then(resolve, reject);
          };
        }),
    );
    promises.forEach((p, i) => {
      p.catch(() => starts[i + 1] && starts[i + 1]());
      if (i > 0) timers.push(setTimeout(starts[i], cfg.hedgeMs * i));
    });
    starts[0]();
    try {
      winner = await Promise.any(promises);
    } catch {
      return unavailable("No server is reachable. Try again shortly.");
    } finally {
      timers.forEach(clearTimeout);
    }
    for (const n of order) if (n !== winner.name) ctrls[n].abort(); // cancel the losers
  }
  return stamp(winner.res, winner.name);
}

// --- writes ----------------------------------------------------------------------------------

async function write(request, env, cfg, body) {
  const deadline = Date.now() + cfg.holdMs;
  let suspect = null; // epoch of a writer we just failed to reach
  for (let tries = 0; tries < 8 && Date.now() < deadline; tries++) {
    let w;
    try {
      w = await coordinator(env, "/writer", { suspect_epoch: suspect, hold_ms: deadline - Date.now() });
    } catch {
      break;
    }
    if (!w.writer) break; // no writer became usable in time
    suspect = null;
    let res = null;
    try {
      res = await sendTo(
        request, env, cfg, w.writer, body,
        { "X-Yabai-Writer": w.writer, "X-Yabai-Writer-Epoch": String(w.epoch) },
        AbortSignal.timeout(cfg.writeTimeoutMs),
      );
    } catch { /* unreachable: handled below */ }
    if (res && !NEVER_REACHED.has(res.status) && res.status !== 421) {
      // A good answer: remember for 30 seconds that this visitor just wrote.
      return stamp(res, w.writer, res.status < 400 ? RW_COOKIE : null);
    }
    if (res && res.body) res.body.cancel().catch(() => {});
    if (res && res.status === 421) await sleep(300); // the writer moved while we sent: ask again
    else suspect = w.epoch; // never reached: tell the Coordinator
  }
  return unavailable("The account server is switching. Try again in a few seconds.", "5");
}

// --- extra routes ------------------------------------------------------------------------------

async function probeAll(env, cfg) {
  const t = {};
  await Promise.all(
    cfg.hosts.map(async (name) => {
      const start = Date.now();
      try {
        const res = await fetch(hostUrl(cfg, name) + "/__ping", {
          headers: env.ORIGIN_SECRET ? { "X-Origin-Secret": env.ORIGIN_SECRET } : {},
          redirect: "manual",
          signal: AbortSignal.timeout(3000),
        });
        if (res.body) res.body.cancel().catch(() => {});
        t[name] = res.ok ? Date.now() - start : null;
      } catch {
        t[name] = null;
      }
    }),
  );
  const state = await readState(env);
  return json({ t, writer: state ? state.writer : null, epoch: state ? state.epoch : null });
}

async function controlState(request, env, cfg) {
  if (!safeEqual(request.headers.get("X-Origin-Secret"), env.ORIGIN_SECRET)) {
    return new Response("Forbidden", { status: 403 });
  }
  try {
    const s = await coordinator(env, "/state");
    return json({ writer: s.writer, epoch: s.epoch, url: hostUrl(cfg, s.writer) });
  } catch {
    return unavailable("coordinator unavailable", "5");
  }
}

export default {
  async fetch(request, env) {
    const cfg = config(env);
    const url = new URL(request.url);
    if (url.pathname.startsWith("/__repl/")) return new Response("Not found", { status: 404 });
    if (url.pathname === "/__control/state") return controlState(request, env, cfg);
    if (url.pathname === "/__probe") return probeAll(env, cfg);
    const safe = request.method === "GET" || request.method === "HEAD";
    // Buffer the body once so it can be sent again to another host.
    const body = safe ? null : await request.arrayBuffer();
    return isWrite(request.method, url.pathname, cfg.writePrefixes)
      ? write(request, env, cfg, body)
      : read(request, env, cfg, body);
  },
};
