// The Durable Object (a small stateful Cloudflare service, one instance for the
// whole site) that decides which host is the global writer. Nothing else may
// decide this, so two writers at once cannot happen.
//
// It probes every host, applies the rules in rules.js, and answers the Worker:
//   GET  /state    who the writer is, plus each host's health
//   POST /writer   who to send a write to; holds the answer while the writer is
//                  failing, up to hold_ms, until a writer is usable
// It re-arms an alarm (a timer that wakes it) only while the site is in use, so an
// idle site costs nothing on the free plan.

import {
  applyMove, config, decide, hostUrl, initialState, newHealth, recordProbe, speed, up,
} from "./rules.js";

const ACTIVE_MS = 5 * 60 * 1000; // stop probing after this long without a request
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const json = (v) => new Response(JSON.stringify(v), { headers: { "Content-Type": "application/json" } });

export class Coordinator {
  constructor(ctx, env) {
    this.ctx = ctx;
    this.env = env;
    this.cfg = config(env);
    this.s = initialState(this.cfg);
    this.health = {};
    this.lastProbe = 0;
    this.lastTouch = 0;
    this.busy = null;
    // Load what was saved before this instance was (re)started.
    ctx.blockConcurrencyWhile(async () => {
      const saved = await ctx.storage.get("state");
      if (saved && saved.s && this.cfg.hosts.includes(saved.s.writer)) {
        this.s = saved.s;
        this.health = saved.health || {};
      }
    });
  }

  save() {
    return this.ctx.storage.put("state", { s: this.s, health: this.health });
  }

  originHeaders() {
    return this.env.ORIGIN_SECRET ? { "X-Origin-Secret": this.env.ORIGIN_SECRET } : {};
  }

  // --- probing ------------------------------------------------------------------

  async probe(name) {
    const h = (this.health[name] ||= newHealth());
    const start = Date.now();
    try {
      const res = await fetch(hostUrl(this.cfg, name) + "/__ping", {
        headers: this.originHeaders(),
        redirect: "manual",
        signal: AbortSignal.timeout(this.cfg.deadMs), // a hung host counts as failing
      });
      if (res.body) res.body.cancel().catch(() => {});
      recordProbe(h, res.ok, Date.now() - start, start);
    } catch {
      recordProbe(h, false, 0, start);
    }
  }

  // The host's replication state (its version vector), or null if it does not answer.
  async fetchVector(name) {
    try {
      const res = await fetch(hostUrl(this.cfg, name) + "/__repl/state", {
        headers: { ...this.originHeaders(), "X-Replication-Secret": this.env.REPL_SECRET || "" },
        redirect: "manual",
        signal: AbortSignal.timeout(3000),
      });
      if (!res.ok) return null;
      const data = await res.json();
      return data && typeof data.vector === "object" ? data.vector : null;
    } catch {
      return null;
    }
  }

  // One probing round. Concurrent callers share the same round.
  tick() {
    if (!this.busy) {
      this.busy = this.round().finally(() => { this.busy = null; });
    }
    return this.busy;
  }

  async round() {
    await Promise.all(this.cfg.hosts.map((n) => this.probe(n)));
    this.lastProbe = Date.now();
    const known = {};
    for (let i = 0; i < 3; i++) {
      const d = decide(this.s, this.health, Date.now(), this.cfg, known);
      if (d.action === "need-vectors") {
        await Promise.all(d.hosts.map(async (n) => { known[n] = await this.fetchVector(n); }));
        continue;
      }
      if (d.action === "move") {
        console.log(`writer ${this.s.writer} -> ${d.to} (${d.reason}), epoch ${this.s.epoch + 1}`);
        applyMove(this.s, d.to, Date.now());
      }
      break;
    }
    await this.save();
  }

  // --- answers --------------------------------------------------------------------

  view() {
    const hosts = {};
    for (const n of this.cfg.hosts) {
      hosts[n] = { up: up(this.health[n]), ms: speed(this.health[n]) };
    }
    return { writer: this.s.writer, epoch: this.s.epoch, movedAt: this.s.movedAt, hosts };
  }

  // The writer to send a write to. While the writer is failing, wait for the
  // 10 second rule to pick a new one (or for the old one to answer again).
  async writerFor(body) {
    const holdMs = Math.min(this.cfg.holdMs, Number(body.hold_ms) || this.cfg.holdMs);
    const end = Date.now() + holdMs;
    // The Worker just failed to reach this writer: start its failure clock now.
    if (Number(body.suspect_epoch) === this.s.epoch) {
      const h = (this.health[this.s.writer] ||= newHealth());
      if (h.failSince === null) h.failSince = Date.now();
    } else if (Date.now() - this.lastProbe > this.cfg.probeMs * 3) {
      await this.tick(); // idle for a while: the first write checks the hosts
    }
    for (;;) {
      const h = this.health[this.s.writer];
      if (!h || h.failSince === null) return { writer: this.s.writer, epoch: this.s.epoch };
      if (Date.now() >= end) return { writer: null, epoch: this.s.epoch };
      await this.tick();
      if (this.health[this.s.writer]?.failSince !== null) await sleep(Math.min(1000, this.cfg.probeMs));
    }
  }

  async fetch(request) {
    this.lastTouch = Date.now();
    if (!(await this.ctx.storage.getAlarm())) {
      await this.ctx.storage.setAlarm(Date.now() + this.cfg.probeMs);
    }
    const path = new URL(request.url).pathname;
    if (path === "/state") {
      if (Date.now() - this.lastProbe > this.cfg.probeMs * 3) await this.tick();
      return json(this.view());
    }
    if (path === "/writer" && request.method === "POST") {
      const body = await request.json().catch(() => ({}));
      return json(await this.writerFor(body));
    }
    return new Response("Not found", { status: 404 });
  }

  // How long to wait before the next probe round. One round is one billed
  // Durable Object request, and the free plan allows 100,000 a day, so it is
  // deliberately unhurried: a round already probes every host, so a host that
  // is merely switched off (a PC or tablet) costs nothing extra beyond being
  // in that round. Only a failing WRITER earns faster probing, because that is
  // the one failure that has to fail over within DEAD_MS.
  alarmDelay() {
    const w = this.health[this.s.writer];
    return w && w.failSince !== null ? this.cfg.failingProbeMs : this.cfg.probeMs;
  }

  // Keep probing while the site is in use; faster while the writer is failing.
  async alarm() {
    await this.tick();
    if (Date.now() - this.lastTouch > ACTIVE_MS) return; // idle: the next request re-arms
    await this.ctx.storage.setAlarm(Date.now() + this.alarmDelay());
  }
}
