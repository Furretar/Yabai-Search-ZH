// Worker + Coordinator end to end, with a fake network and fake storage.
// Run: node --test deploy/worker/test/
import test from "node:test";
import assert from "node:assert/strict";
import worker, { resetStateCache } from "../worker.js";
import { Coordinator } from "../coordinator.js";

const SECRET = "origin-secret";
const env0 = {
  HOSTS: "laptop,tablet", ORIGIN_SECRET: SECRET, REPL_SECRET: "r".repeat(20),
  DEAD_MS: "300", HOLD_MS: "3000", PROBE_MS: "100", HEDGE_MS: "50", WRITE_TIMEOUT_MS: "1000",
};

// A fake Durable Object platform: one Coordinator, in-memory storage.
function makeEnv(overrides = {}) {
  resetStateCache(); // each test starts with an empty per-instance /state cache
  const store = new Map();
  let alarm = null;
  const ctx = {
    storage: {
      get: async (k) => store.get(k), put: async (k, v) => store.set(k, v),
      getAlarm: async () => alarm, setAlarm: async (t) => { alarm = t; },
    },
    blockConcurrencyWhile: (fn) => fn(),
  };
  const env = { ...env0, ...overrides };
  const dobj = new Coordinator(ctx, env);
  env.COORDINATOR = { idFromName: () => "id", get: () => ({ fetch: (u, i) => dobj.fetch(new Request(u, i)) }) };
  env.__dobj = dobj; // so a test can call tick() and alarmDelay() directly
  return env;
}

// A fake network. net.down = set of host names that do not answer.
function fakeNet() {
  const net = { down: new Set(), log: [], vectors: { laptop: { laptop: 3 }, tablet: { laptop: 3 } } };
  globalThis.fetch = async (input, init = {}) => {
    const url = new URL(input instanceof Request ? input.url : input);
    const name = url.hostname.split(".")[0];
    const headers = new Headers(init.headers);
    if (net.down.has(name)) throw new TypeError("connection refused");
    if (headers.get("X-Origin-Secret") !== SECRET) return new Response("no", { status: 403 });
    if (url.pathname === "/__ping") return new Response("{}", { status: 200 });
    if (url.pathname === "/__repl/state") return Response.json({ node: name, vector: net.vectors[name] });
    net.log.push({
      name, method: init.method, path: url.pathname,
      writer: headers.get("X-Yabai-Writer"), epoch: headers.get("X-Yabai-Writer-Epoch"),
    });
    if (net.reply) return net.reply(name, url, init);
    return new Response(`from ${name}`, { status: init.method === "POST" ? 302 : 200 });
  };
  return net;
}
const req = (path, init = {}) => new Request("https://yabaisearch.org" + path, init);

test("a signup goes to the writer with its epoch, and sets the read-your-writes cookie", async () => {
  const net = fakeNet();
  const res = await worker.fetch(req("/accounts/signup/", { method: "POST", body: "x=1" }), makeEnv());
  assert.equal(res.status, 302);
  assert.equal(res.headers.get("X-Origin"), "laptop");
  assert.match(res.headers.get("Set-Cookie"), /yabai_rw=1/);
  assert.deepEqual(net.log.at(-1), { name: "laptop", method: "POST", path: "/accounts/signup/", writer: "laptop", epoch: "1" });
});

test("a signup waits while the writer is down, then goes to the new writer", async () => {
  const net = fakeNet();
  const env = makeEnv();
  await worker.fetch(req("/"), env); // warm up: both hosts probed
  net.down.add("laptop");
  const t0 = Date.now();
  const res = await worker.fetch(req("/accounts/signup/", { method: "POST", body: "x=1" }), env);
  const took = Date.now() - t0;
  assert.equal(res.status, 302);
  assert.equal(res.headers.get("X-Origin"), "tablet");
  assert.deepEqual(net.log.at(-1).epoch, "2"); // the epoch went up
  assert.ok(took >= 250 && took < 2500, `took ${took} ms (DEAD_MS is 300)`);
});

test("both hosts down: the write waits, then answers 503", async () => {
  const net = fakeNet();
  const env = makeEnv({ HOLD_MS: "800" });
  net.down.add("laptop").add("tablet");
  const t0 = Date.now();
  const res = await worker.fetch(req("/accounts/signup/", { method: "POST", body: "x=1" }), env);
  assert.equal(res.status, 503);
  assert.ok(Date.now() - t0 >= 700);
});

test("a 421 from the writer (it moved) is asked again", async () => {
  const net = fakeNet();
  let n = 0;
  net.reply = () => (++n === 1 ? new Response("no", { status: 421 }) : new Response("ok", { status: 200 }));
  const res = await worker.fetch(req("/api/account/settings", { method: "PUT", body: "{}" }), makeEnv());
  assert.equal(res.status, 200);
  assert.equal(n, 2);
});

test("reads use the local host cookie, fall back when it fails, and strip forged writer headers", async () => {
  const net = fakeNet();
  const env = makeEnv();
  const cookie = { Cookie: "yabai_local=tablet", "X-Yabai-Writer": "evil", "X-Yabai-Writer-Epoch": "99" };
  let res = await worker.fetch(req("/api/search?q=a", { headers: cookie }), env);
  assert.equal(res.headers.get("X-Origin"), "tablet");
  assert.equal(net.log.at(-1).writer, null);
  net.down.add("tablet");
  res = await worker.fetch(req("/api/search?q=a", { headers: cookie }), env);
  assert.equal(res.headers.get("X-Origin"), "laptop");
});

test("a login POST is a local request, not a write", async () => {
  const net = fakeNet();
  const res = await worker.fetch(
    req("/accounts/login/", { method: "POST", body: "u=1", headers: { Cookie: "yabai_local=tablet" } }), makeEnv());
  assert.equal(res.headers.get("X-Origin"), "tablet");
  assert.equal(net.log.at(-1).writer, null);
});

test("/__repl/ is never reachable on the public domain; /__control/state needs the secret", async () => {
  fakeNet();
  const env = makeEnv();
  assert.equal((await worker.fetch(req("/__repl/snapshot"), env)).status, 404);
  assert.equal((await worker.fetch(req("/__control/state"), env)).status, 403);
  const ok = await worker.fetch(req("/__control/state", { headers: { "X-Origin-Secret": SECRET } }), env);
  assert.deepEqual(await ok.json(), { writer: "laptop", epoch: 1, url: "https://laptop.yabaisearch.org" });
});

test("an offline non-writer host does not shorten the probe delay, but a failing writer does", async () => {
  const net = fakeNet();
  const env = makeEnv({ PROBE_MS: "15000", FAILING_PROBE_MS: "10000" });
  net.down.add("tablet"); // a second host is switched off; the writer is healthy
  await worker.fetch(req("/"), env);
  assert.equal(env.__dobj.alarmDelay(), 15000);
  net.down.add("laptop"); // now the writer itself is failing
  await env.__dobj.tick();
  assert.equal(env.__dobj.alarmDelay(), 10000);
});

test("/__probe times each host and names the writer", async () => {
  const net = fakeNet();
  net.down.add("tablet");
  const body = await (await worker.fetch(req("/__probe"), makeEnv())).json();
  assert.equal(body.t.tablet, null);
  assert.equal(typeof body.t.laptop, "number");
  assert.equal(body.writer, "laptop");
});
