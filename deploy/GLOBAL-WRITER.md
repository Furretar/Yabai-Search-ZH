# Yabai Search: global writer plan

Goal: any device can host the site. Visitors read from the fastest host for them.
Only ONE host, the global writer, may change accounts. Everything runs through
Cloudflare (no Tailscale).

Jargon used below:
- **Host**: one machine running the app plus a tunnel (laptop, tablet, pc...).
- **Writer / global host**: the one host allowed to write accounts.
- **Local host**: the host that answers a visitor's searches (can differ per visitor).
- **Epoch**: a counter that goes up each time the writer changes.
- **Durable Object (DO)**: a small stateful Cloudflare service. We use one, as the
  single referee for who the writer is.
- **Replica**: a host that is not the writer; it copies the writer's database.

## 1. Who does what

```
visitor ---> Cloudflare Worker (yabaisearch.org)
               |  reads:  local host (cookie), then the others (hedged)
               |  writes: ask the Durable Object -> send ONLY to the writer
               |
               +--> Durable Object "Coordinator"  (one instance, the referee)
               |       probes every host, applies the rules, bumps the epoch
               |
               +--> laptop.yabaisearch.org  --tunnel--> laptop   :8000 app, :8765 replication
               +--> tablet.yabaisearch.org  --tunnel--> tablet   :8000 app, :8765 replication

replicas ---> GET yabaisearch.org/__control/state  (who is the writer?)
         ---> GET <writer>.yabaisearch.org/__repl/*  (copy its database every 10 s)
```

## 2. The rules (all numbers are `[vars]` in `deploy/worker/wrangler.toml`)

Local host (per visitor, in the browser, `static/hosts.js`):

| Rule | Value |
|---|---|
| Probe | every 15 s while the tab is visible, through the Worker's `/__probe` (time from the visitor's nearest Cloudflare edge to each host) |
| Speed | median of the last 5 probes |
| Switch | another host at least 20% faster for 30 s in a row |
| Immediate switch | the current local host stops answering |
| Stored in | cookie `yabai_local`; the Worker routes reads there first |

Global writer (Durable Object, `deploy/worker/rules.js`):

| Situation | Rule |
|---|---|
| Normal | the writer stays put |
| Planned move | 30 min since the last move (`LOCK_MS`) AND another host at least 20% faster for 5 min in a row (`LEAD_MS`, `MARGIN`) AND that host is fully caught up |
| Slow writer | median probe above 3 s (`SLOW_MS`) AND another host at least 20% faster: the 30 min lock is ignored (and so is the 5 min wait: the median of 5 probes is the debounce). The new host must still be fully caught up |
| Dead writer | no answer for 10 s (`DEAD_MS`): move at once to the responsive host with the MOST RECENT data (speed breaks ties), ignoring everything else |
| All hosts down | a signup hangs up to 25 s (`HOLD_MS`), then 503 with Retry-After |

Notes on the clocks:
- "Fully caught up" means the two hosts' version vectors are equal (the write
  counters kept inside each accounts database). If the old writer is too slow to
  answer the check, the move waits and the dead rule decides.
- Any move, including a failover, restarts the 30 min lock, so a laptop that comes
  back cannot take the role back for 30 min unless the new writer turns slow.
- The 10 s dead clock starts at the first failed probe. A signup that fails to
  reach the writer starts the clock itself, so a visitor waits at most about 10 s.
- When nobody has used the site for 5 min the DO stops probing. The first write
  after that probes first (adds at most one probe round).

## 3. How a write flows

1. The Worker sees an unsafe request (POST/PUT/...) on `/accounts/signup/`,
   `/api/account/` or `/admin/`. These are the only writes. Login, logout,
   `/api/convert`, `/api/annotate` and the rest are local.
2. It asks the DO for the writer and epoch. If the writer is failing, the DO holds
   the answer until it has moved the writer or the old one answers again.
3. It sends the request to that host with `X-Yabai-Writer` and
   `X-Yabai-Writer-Epoch`. The host's `WriterGateMiddleware` accepts it only if the
   header names THIS host and the epoch is not older than one it has seen
   (`data/writer-epoch`). Otherwise it answers 421 and the Worker asks again.
4. After a good write the Worker sets cookie `yabai_rw` for 30 s. While it is
   present, that visitor's reads go to the writer, so a new account is visible
   immediately even though replicas lag up to 10 s.
5. If the request never reached the host (tunnel gone, timeout), the Worker tells
   the DO (`suspect_epoch`) and retries on the new writer. A signup that did reach
   the old host is safe to repeat: `accounts/views.py` logs the person in when the
   same username and password were created in the last 2 minutes.

Reads of accounts on replicas work because:
- sessions are signed cookies (default when `YABAI_REPL_NODE` is set), so login
  needs no database write;
- `last_login` is not saved, the password hash is never re-saved on login
  (`accounts/backends.py`), and `GET /api/account/settings` no longer creates a row;
- every non-write request runs with SQLite `query_only` on. A forgotten write fails
  loudly instead of silently making a replica diverge.

Existing database sessions stop working once (everyone logs in again).

## 4. Replication through Cloudflare

- Each host runs the replication server on port 8765. `deploy/cloudflared/*.yml`
  sends `https://<host>.yabaisearch.org/__repl/*` to it and everything else to the
  app. The Worker answers 404 for `/__repl/` on the public domain.
- Every request needs BOTH `X-Origin-Secret` and `X-Replication-Secret`.
- A replica asks `https://yabaisearch.org/__control/state` (needs the origin
  secret) for `{writer, epoch, url}`, then pulls from `url` every 10 s. It always
  converges to the writer's copy. If the replica holds writes the writer never got
  (the old writer crashed right after a write), they are saved in
  `data/replication-archive/` and can be recovered with
  `python -m accounts.replication salvage <file>`.
- A host that boots asks who the writer is before serving (`run.sh`), so a
  returning laptop catches up from the tablet first.

Limits to know about:
- A write that reached the old writer less than 10 s before it died is lost from
  the new writer's copy (archived on the old one, recoverable with `salvage`).
- A write that is in flight at the moment of a planned move can land on the old
  writer after the move; it is archived the same way.
- A LAN or Tailscale visitor can skip the origin secret (existing behaviour of
  `OriginSecretMiddleware`), so such a visitor could forge the writer headers.
- If the DO's storage is ever wiped its epoch restarts at 1 and hosts refuse
  writes. Delete `data/writer-epoch` on every host.
- Free plan budget: 100k Worker requests/day, and the DO has 100k requests/day and
  13,000 GB-s/day (alarms count as requests). Probing every 5 s only while the site
  is in use is about 17k/day at the very most. Each open tab adds one `/__probe`
  per 15 s; raise `EVERY_MS` in `hosts.js` if that matters.
- Reads with the cookie set never wait on the DO (state is cached 10 s).

## 5. Cloudflare setup (once)

Everything below is free-plan compatible. You need the Cloudflare account that
already has `yabaisearch.org`.

1. **Secrets.** Pick two long random strings (`openssl rand -hex 32`):
   `ORIGIN_SECRET` (already exists) and a new `REPL_SECRET`.
   - Every host's `deploy/env.local`: `YABAI_ORIGIN_SECRET`, `YABAI_REPL_SECRET`,
     `YABAI_CONTROL_URL=https://yabaisearch.org/__control/state`, and the same
     `DJANGO_SECRET_KEY`.
2. **One tunnel per host**, named after the host (the role you pass to `run.sh`):
   ```
   cloudflared tunnel create yabai-<name>
   cloudflared tunnel route dns yabai-<name> <name>.yabaisearch.org
   ```
   Put the tunnel ID in `deploy/cloudflared/<name>.yml`. Keep the two ingress
   entries (the `/__repl/` one first). The existing laptop, tablet and pc files
   are already updated. Use the same name in `HOSTS`.
3. **Worker settings.** In `deploy/worker/wrangler.toml` set `HOSTS` (for example
   `"laptop,tablet,pc"`) and check `HOST_DOMAIN`. The first host listed is the
   writer on a brand new install.
4. **Deploy the Worker and the Durable Object together:**
   ```
   cd deploy/worker
   npx wrangler login
   npx wrangler secret put ORIGIN_SECRET
   npx wrangler secret put REPL_SECRET
   npx wrangler deploy
   ```
   The `[[migrations]]` block creates the `Coordinator` Durable Object class as
   SQLite-backed, the only kind the free plan allows. The first `deploy` after
   the old setup needs no DNS change: the Custom Domains already point at the Worker.
5. **Cloudflare dashboard checks** (Workers & Pages, yabai-front):
   - Settings, Bindings: `COORDINATOR` (Durable Object, class `Coordinator`).
   - Settings, Variables and Secrets: the `[vars]` plus the two secrets.
   - Durable Objects tab: one `Coordinator` object appears after the first request.
   - Observability, Logs: look for `writer laptop -> tablet (dead), epoch 2`.
6. **Optional hardening.** Add a WAF custom rule on hostnames
   `laptop.yabaisearch.org`, `tablet.yabaisearch.org`, `pc.yabaisearch.org` that
   blocks requests when the header `X-Origin-Secret` is missing. It is a second
   lock in front of the app's own check. `dev.yabaisearch.org` stays behind
   Cloudflare Access.
7. **Order of rollout:** (a) deploy the code to every host and restart (`release.sh`),
   (b) deploy the Worker, (c) confirm `deploy/check.sh` prints a writer, (d) run the
   drills below.

Adding a host later: create its tunnel and `deploy/cloudflared/<name>.yml`, give it
`deploy/env.local`, run `deploy/run.sh <name>` and `deploy/tunnel.sh <name>`, add the
name to `HOSTS`, `wrangler deploy`. The app accepts any `<name>.yabaisearch.org`.

## 6. Drills (do once after setup)

| Test | Expected |
|---|---|
| `curl https://yabaisearch.org/__probe` | JSON with each host's ms, `writer`, `epoch` |
| Stop the writer's tunnel, then sign up | the signup hangs up to about 10 s, then succeeds; `X-Origin` is the other host; `__probe` shows epoch +1 |
| Start the old writer again | it catches up (log `installed`), is NOT writer for 30 min |
| Stop all tunnels, sign up | about 25 s, then 503 |
| `curl https://yabaisearch.org/__repl/state` | 404 |
| `curl https://tablet.yabaisearch.org/__repl/state` with one secret only | 403 |
| Sign up, then reload on another device | account exists everywhere within about 10 s |
| `python -m accounts.replication status` on a replica | names the writer and says `noop` |

## 7. Files in this change

| File | Change |
|---|---|
| `deploy/worker/rules.js` | new: pure rules (writer moves, write paths), unit tested |
| `deploy/worker/coordinator.js` | new: the Durable Object |
| `deploy/worker/worker.js`, `wrangler.toml` | rewritten: routing, hold and retry, probe and control routes, bindings, settings |
| `deploy/worker/test/*.test.mjs` | new: Node tests |
| `accounts/writer.py` | new: write paths, epoch fence |
| `accounts/backends.py` | new: login that never writes |
| `accounts/replication.py` | follow the writer named by the Worker; both secrets; `/__repl/` |
| `accounts/apps.py`, `accounts/views.py` | no `last_login`, settings GET is read-only |
| `yabai_site/middleware.py`, `settings.py` | `WriterGateMiddleware`, cookie sessions, backends, allowed hosts |
| `yabai_search/static/hosts.js`, `templates/index.html`, `style.css` | local host picker and menu lines |
| `deploy/cloudflared/*.yml`, `run.sh`, `env.local.example`, `check.sh` | replication path, `YABAI_CONTROL_URL` |
| `tests/test_replication.py`, `test_writer.py`, `test_accounts.py` | new tests |
| `AGENTS.md`, `deploy/PLAN.md` | updated |
