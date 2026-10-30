// Cloudflare Worker that sits in front of yabaisearch.org.
//
// Behaviour:
//   1. Every request goes to the laptop first.
//   2. If the laptop is unreachable or answers with a 5xx, the tablet is tried
//      immediately.
//   3. For GET and HEAD only: if the laptop has not answered within HEDGE_MS,
//      the tablet is asked as well and the first good answer wins. ("Hedging"
//      means sending a backup request early instead of waiting for a failure.)
//   4. Other methods (POST etc.) are never hedged, so a write cannot run twice.
//      They only fail over when the request provably never reached the app
//      (Cloudflare 502 / 530 tunnel errors).
//
// The response carries X-Origin: laptop | tablet so you can see who answered.

const ORIGINS = {
  laptop: "https://laptop.yabaisearch.org",
  tablet: "https://tablet.yabaisearch.org",
};

const DEFAULT_HEDGE_MS = 800;

// Statuses that mean "the request never reached the app" (tunnel is gone).
const NEVER_REACHED = new Set([502, 530]);

export default {
  async fetch(request, env) {
    const hedgeMs = Number(env.HEDGE_MS) || DEFAULT_HEDGE_MS;
    const safe = request.method === "GET" || request.method === "HEAD";
    // Buffer the body once so it could be replayed on a second origin.
    const body = safe ? null : await request.arrayBuffer();
    const url = new URL(request.url);
    const clientIp = request.headers.get("CF-Connecting-IP") || "";

    // One AbortController per origin, so the loser can be cancelled.
    const ctrls = {
      laptop: new AbortController(),
      tablet: new AbortController(),
    };

    // Ask one origin. Resolves with {name, res} on a usable answer and
    // rejects otherwise, so Promise.any can pick the first usable one.
    async function attempt(name) {
      const target = new URL(url.pathname + url.search, ORIGINS[name]);
      const headers = new Headers(request.headers);
      // The origin apps refuse requests without this shared secret.
      if (env.ORIGIN_SECRET) headers.set("X-Origin-Secret", env.ORIGIN_SECRET);
      // Keep the real visitor IP visible to the app's logs and rate limits.
      if (clientIp) headers.set("X-Forwarded-For", clientIp);
      let res;
      try {
        res = await fetch(target, {
          method: request.method,
          headers,
          body,
          redirect: "manual", // pass redirects to the browser untouched
          signal: ctrls[name].signal,
        });
      } catch (err) {
        throw new Error(`${name}: ${err}`);
      }
      const bad = safe ? res.status >= 500 : NEVER_REACHED.has(res.status);
      if (bad) {
        // Free the connection before giving up on this answer.
        if (res.body) res.body.cancel().catch(() => {});
        throw new Error(`${name}: status ${res.status}`);
      }
      return { name, res };
    }

    const laptop = attempt("laptop");
    laptop.catch(() => {}); // failures are handled below; avoid a noisy warning

    let winner;
    if (!safe) {
      // No hedging: laptop first, tablet only if the laptop was unreachable.
      try {
        winner = await laptop;
      } catch {
        try {
          winner = await attempt("tablet");
        } catch {
          return unavailable();
        }
      }
    } else {
      // The tablet starts when the timer fires or the laptop fails,
      // whichever comes first. If the laptop answers well first, it never starts.
      const tablet = new Promise((resolve, reject) => {
        let started = false;
        const start = () => {
          if (started) return;
          started = true;
          attempt("tablet").then(resolve, reject);
        };
        const timer = setTimeout(start, hedgeMs);
        laptop.then(
          () => clearTimeout(timer),
          () => {
            clearTimeout(timer);
            start();
          },
        );
      });
      tablet.catch(() => {});
      try {
        winner = await Promise.any([laptop, tablet]);
      } catch {
        return unavailable();
      }
      // Cancel whichever origin lost the race.
      const loser = winner.name === "laptop" ? "tablet" : "laptop";
      ctrls[loser].abort();
    }

    const out = new Response(winner.res.body, winner.res);
    out.headers.set("X-Origin", winner.name);
    return out;
  },
};

// Both machines failed: a plain 503 that browsers and monitors understand.
function unavailable() {
  return new Response("Both servers are unreachable. Try again shortly.", {
    status: 503,
    headers: { "Retry-After": "10", "Cache-Control": "no-store" },
  });
}
