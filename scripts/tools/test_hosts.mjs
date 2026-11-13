// Run: node scripts/tools/test_hosts.mjs   (tests the local-host rule in static/hosts.js)
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

new Function(readFileSync(new URL("../../yabai_search/static/hosts.js", import.meta.url), "utf8"))();
const pick = globalThis.__pickLocal;
const S = 1000;

// First visit: the fastest host.
let st = pick({ local: null, challenger: null }, { laptop: 50, tablet: 90 }, 0);
assert.equal(st.local, "laptop");
// 19% faster is not enough.
st = pick(st, { laptop: 100, tablet: 81 }, 1 * S);
assert.equal(st.challenger, null);
// 20% faster must last 30 s.
st = pick(st, { laptop: 100, tablet: 80 }, 10 * S);
assert.equal(st.local, "laptop");
assert.equal(st.challenger.since, 10 * S);
st = pick(st, { laptop: 100, tablet: 80 }, 39 * S);
assert.equal(st.local, "laptop");
st = pick(st, { laptop: 100, tablet: 80 }, 40 * S);
assert.equal(st.local, "tablet");
// A broken streak restarts the clock.
st = pick(st, { laptop: 50, tablet: 80 }, 50 * S);
st = pick(st, { laptop: 90, tablet: 80 }, 60 * S);
assert.equal(st.challenger, null);
// The local host stops answering: switch at once.
st = pick(st, { laptop: 50 }, 61 * S);
assert.equal(st.local, "laptop");
console.log("hosts.js rules ok");
