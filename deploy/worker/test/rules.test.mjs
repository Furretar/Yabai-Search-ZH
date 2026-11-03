// Run: node --test deploy/worker/test/
import test from "node:test";
import assert from "node:assert/strict";
import {
  applyMove, config, decide, initialState, isWrite, median, newHealth, recordProbe,
} from "../rules.js";

const cfg = config({ HOSTS: "laptop,tablet,pc" });
const MIN = 60 * 1000;

// Build health records: ms = list of timings, fail = failSince.
function health(spec) {
  const out = {};
  for (const [name, { ms = [], fail = null }] of Object.entries(spec)) {
    out[name] = { samples: ms, failSince: fail };
  }
  return out;
}
const same = { laptop: 5 };

test("median and probe recording", () => {
  assert.equal(median([3, 1, 2]), 2);
  assert.equal(median([1, 2, 3, 4]), 2.5);
  const h = newHealth();
  for (let i = 1; i <= 7; i++) recordProbe(h, true, i * 10, 0);
  assert.deepEqual(h.samples, [30, 40, 50, 60, 70]); // last 5 only
  recordProbe(h, false, 0, 1000);
  recordProbe(h, false, 0, 2000);
  assert.equal(h.failSince, 1000); // clock starts at the FIRST failure
  recordProbe(h, true, 10, 3000);
  assert.equal(h.failSince, null);
});

test("isWrite: only unsafe methods on account paths", () => {
  const p = cfg.writePrefixes;
  assert.ok(isWrite("POST", "/accounts/signup/", p));
  assert.ok(isWrite("PUT", "/api/account/settings", p));
  assert.ok(isWrite("POST", "/admin/login/", p));
  assert.ok(!isWrite("GET", "/accounts/signup/", p));
  assert.ok(!isWrite("POST", "/accounts/login/", p)); // logins work on any host
  assert.ok(!isWrite("POST", "/api/convert", p));
});

test("normal: the writer stays put", () => {
  const s = initialState(cfg);
  const h = health({ laptop: { ms: [50, 50, 50] }, tablet: { ms: [60, 60, 60] }, pc: { ms: [55] } });
  assert.deepEqual(decide(s, h, 1000, cfg), { action: "stay" });
  assert.equal(s.lead, null);
});

test("dead writer: moves after 10 s to the host with the most recent data", () => {
  const s = initialState(cfg);
  const h = health({ laptop: { ms: [50], fail: 0 }, tablet: { ms: [60] }, pc: { ms: [200] } });
  assert.deepEqual(decide(s, h, 9999, cfg), { action: "stay" }); // not yet 10 s
  const need = decide(s, h, 10000, cfg);
  assert.equal(need.action, "need-vectors");
  assert.deepEqual(need.hosts.sort(), ["pc", "tablet"]);
  // pc is slower but has more data: it wins. Speed only breaks ties.
  let d = decide(s, h, 10000, cfg, { tablet: { laptop: 4 }, pc: { laptop: 5 } });
  assert.deepEqual(d, { action: "move", to: "pc", reason: "dead" });
  d = decide(s, h, 10000, cfg, { tablet: { laptop: 5 }, pc: { laptop: 5 } });
  assert.equal(d.to, "tablet");
  // A host that did not answer the vector request ranks last.
  d = decide(s, h, 10000, cfg, { tablet: null, pc: { laptop: 1 } });
  assert.equal(d.to, "pc");
});

test("dead writer with every host down: stay (the write hold ends in a 503)", () => {
  const s = initialState(cfg);
  const h = health({ laptop: { fail: 0 }, tablet: { fail: 0 }, pc: { fail: 0 } });
  assert.deepEqual(decide(s, h, 60000, cfg), { action: "stay" });
});

test("dead rule ignores the lock", () => {
  const s = initialState(cfg);
  applyMove(s, "laptop", 1000); // just moved
  const h = health({ laptop: { ms: [50], fail: 1000 }, tablet: { ms: [60] }, pc: {} });
  const d = decide(s, h, 11000, cfg, { tablet: same });
  assert.equal(d.action, "move");
});

test("planned move needs 20% faster for 5 min, the lock, and a synced host", () => {
  const s = initialState(cfg); // movedAt 0: lock already over
  const h = health({ laptop: { ms: [100] }, tablet: { ms: [79] }, pc: {} });
  assert.equal(decide(s, h, 40 * MIN, cfg).action, "stay"); // lead starts now
  assert.equal(s.lead.host, "tablet");
  assert.equal(decide(s, h, 44 * MIN, cfg).action, "stay"); // 4 min: too early
  let d = decide(s, h, 45 * MIN, cfg);
  assert.equal(d.action, "need-vectors");
  d = decide(s, h, 45 * MIN, cfg, { laptop: { laptop: 5 }, tablet: { laptop: 4 } });
  assert.equal(d.action, "stay"); // not caught up
  d = decide(s, h, 45 * MIN, cfg, { laptop: { laptop: 5 }, tablet: { laptop: 5, tablet: 0 } });
  assert.deepEqual(d, { action: "move", to: "tablet", reason: "faster" });
});

test("19% faster is not enough, and a lead that breaks restarts the clock", () => {
  const s = initialState(cfg);
  let h = health({ laptop: { ms: [100] }, tablet: { ms: [81] }, pc: {} });
  decide(s, h, 40 * MIN, cfg);
  assert.equal(s.lead, null);
  h = health({ laptop: { ms: [100] }, tablet: { ms: [70] }, pc: {} });
  decide(s, h, 41 * MIN, cfg);
  assert.equal(s.lead.since, 41 * MIN);
  h = health({ laptop: { ms: [100] }, tablet: { ms: [95] }, pc: {} });
  decide(s, h, 43 * MIN, cfg);
  assert.equal(s.lead, null);
});

test("lock: a returning host cannot take the role back for 30 minutes", () => {
  const s = initialState(cfg);
  applyMove(s, "tablet", 100 * MIN); // laptop died, tablet took over
  assert.equal(s.epoch, 2);
  const h = health({ laptop: { ms: [10] }, tablet: { ms: [100] }, pc: {} });
  const v = { laptop: { laptop: 5 }, tablet: { laptop: 5 } };
  decide(s, h, 101 * MIN, cfg, v);
  assert.equal(decide(s, h, 120 * MIN, cfg, v).action, "stay"); // lead long enough, lock not over
  assert.equal(decide(s, h, 131 * MIN, cfg, v).to, "laptop");
});

test("slow writer ignores the lock but still needs a synced host", () => {
  const s = initialState(cfg);
  applyMove(s, "tablet", 100 * MIN);
  const h = health({ laptop: { ms: [200] }, tablet: { ms: [3500, 3500, 3500] }, pc: {} });
  const synced = { laptop: { laptop: 5 }, tablet: { laptop: 5 } };
  const d = decide(s, h, 101 * MIN, cfg, synced);
  assert.deepEqual(d, { action: "move", to: "laptop", reason: "slow" });
  // The slow writer cannot answer the check: wait for the dead rule instead.
  assert.equal(decide(s, h, 101 * MIN, cfg, { laptop: { laptop: 5 }, tablet: null }).action, "stay");
});

test("slow but nobody is 20% faster: stay", () => {
  const s = initialState(cfg);
  const h = health({ laptop: { ms: [3500] }, tablet: { ms: [3400] }, pc: {} });
  assert.equal(decide(s, h, 99 * MIN, cfg).action, "stay");
});
