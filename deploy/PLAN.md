# Yabai Search: two-machine hosting plan

Goal: yabaisearch.org is served by the laptop, with the tablet as an automatic
backup. You edit in a separate dev copy that has its own address, and one
command publishes finished work to both serving copies.

## 1. Architecture

```
visitor
  |
  v
Cloudflare Worker  (yabaisearch.org, www.yabaisearch.org)
  |  laptop first; tablet if laptop fails or is slow (GET/HEAD)
  +--> laptop.yabaisearch.org --> tunnel "yabai-laptop" --> laptop  ~/Desktop/ai/yabai-laptop  :8000
  +--> tablet.yabaisearch.org --> tunnel "yabai-tablet" --> tablet  ~/yabai-search  :8000

you
  +--> dev.yabaisearch.org    --> tunnel "yabai-dev"    --> laptop  ~/Documents/Yabai-Search  :8001
```

Three tunnels, each its own cloudflared process, so one failing never takes
down another. A "tunnel" is a connection your machine opens out to
Cloudflare so the internet can reach it without opening ports at home.

Three copies of the code:

| Copy | Machine | Folder | Port | Purpose |
|---|---|---|---|---|
| dev | laptop | `~/Documents/Yabai-Search` | 8001 | where you and Claude make changes |
| laptop | laptop | `~/Desktop/ai/yabai-laptop` | 8000 | serves the public site |
| tablet | tablet | `~/yabai-search` | 8000 | serves as backup |

The serving copies are never edited. They only receive code from GitHub.

## 2. What is in this kit (`deploy/`)

| File | Job |
|---|---|
| `worker/worker.js`, `wrangler.toml` | Cloudflare Worker: failover and hedging |
| `yabai_site/middleware.py` (OriginSecretMiddleware) | makes the apps refuse requests that skipped the Worker |
| `run.sh <role>` | keeps the app running, restarts it if it exits |
| `tunnel.sh <role>` | keeps cloudflared running |
| `update.sh <role>` | pull, restart, health check, roll back on failure |
| `release.sh [--data]` | tests, push, update laptop then tablet |
| `check.sh` | shows which machine answers on each hostname |
| `cloudflared/*.yml` | one tunnel config per role |
| `systemd/*.service` | start everything at boot on the laptop |

Nothing in `yabai_search/` is changed.

## 3. Behaviour of the Worker

- Laptop first. Tablet on a laptop error, a 5xx, or a dead tunnel.
- GET and HEAD only: if the laptop has not answered in 800 ms (`HEDGE_MS`),
  the tablet is also asked and the first good answer wins. The slower request
  is cancelled. This is a timer, not a latency measurement. The laptop is far
  faster than the tablet, so it nearly always wins.
- POST and others are never duplicated. They fail over only on Cloudflare
  502/530 (the request never reached the app).
- Both down: a 503 with `Retry-After`.
- Every answer has `X-Origin: laptop|tablet` (Worker) and `X-Served-By`
  (the app already sends this from `YABAI_SERVED_BY`).
- The real visitor IP is forwarded in `X-Forwarded-For`, which the app
  already reads.
- Costs: free plan allows 100k Worker requests per day. Each hedge adds a
  subrequest but that is within limits.

Known tradeoffs: about 20-50 ms extra per request from the Worker hop; the
two machines must hold the same data or results differ by who answered
(section 6); Cloudflare itself remains the single point of failure.

## 4. One-time setup

Do these in order. Each step has a check.

### 4.1 Secret
Pick a long random string (`openssl rand -hex 32`). It goes in three places:
the Worker, `~/Desktop/ai/yabai-laptop/deploy/env.local`, and the tablet's
`~/yabai-search/deploy/env.local`:

```
YABAI_ORIGIN_SECRET=<the string>
```

`deploy/env.local` is per machine and must not go in git. Add to `.gitignore`:

```
deploy/env.local
deploy/release.env
run/
```

### 4.2 Laptop: two folders
The working copy is the git checkout itself, kept in
`~/Documents/Yabai-Search`; the dev unit serves it on :8001. Only the public
site needs a separate clone:
```
git clone <repo> ~/Desktop/ai/yabai-laptop
```
Then `uv sync` in both. Copy `data/` (it is gitignored), for example
`cp -r ~/Documents/Yabai-Search/data ~/Desktop/ai/yabai-laptop/data`.
Add the `deploy/` folder from this kit to the repo, commit and push it from the
working copy, then `git pull` in `~/Desktop/ai/yabai-laptop` and on the tablet.

### 4.3 Laptop tunnels
```
cloudflared tunnel login
cloudflared tunnel create yabai-laptop
cloudflared tunnel route dns yabai-laptop laptop.yabaisearch.org
cloudflared tunnel create yabai-dev
cloudflared tunnel route dns yabai-dev dev.yabaisearch.org
```
Put each printed tunnel ID and your username into
`deploy/cloudflared/laptop.yml` and `dev.yml` in the matching clone.

### 4.4 Tablet (Termux)
The Termux main repo has no `cloudflared`, but the Termux User Repository
(TUR) does. Install TUR first:
```
pkg install tur-repo
pkg update
pkg install cloudflared rsync python
```
Then create the tunnel on the laptop (it owns the Cloudflare login):
```
cloudflared tunnel create yabai-tablet
cloudflared tunnel route dns --overwrite-dns yabai-tablet tablet.yabaisearch.org
```
Copy the printed credentials JSON to the tablet's
`~/.cloudflared/<id>.json`, fill the ID into `deploy/cloudflared/tablet.yml`,
and add `CLOUDFLARED_EDGE_IP_VERSION=4` to `deploy/env.local` on the tablet.
Android on this tablet cannot reach one of Cloudflare's edge regions over
IPv6, so cloudflared must be told to use IPv4; without that flag its precheck
fails with `hard_fail=true`. The flag is read by `deploy/tunnel.sh`.

Verify with `cloudflared tunnel info yabai-tablet`: it must list an
`android_arm64` connector. If it does not, run
`cloudflared tunnel --edge-ip-version 4 --loglevel debug ... run` and keep
the log.

### 4.5 Start things
Laptop:
```
mkdir -p ~/.config/systemd/user
cp ~/Desktop/ai/yabai-laptop/deploy/systemd/*.service ~/.config/systemd/user/
# The dev instance is pointed at the working copy by these drop-ins.
cp -r ~/Desktop/ai/yabai-laptop/deploy/systemd/yabai-app@dev.service.d \
      ~/Desktop/ai/yabai-laptop/deploy/systemd/yabai-tunnel@dev.service.d \
      ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now yabai-app@laptop yabai-app@dev yabai-tunnel@laptop yabai-tunnel@dev
loginctl enable-linger $USER
```
On NixOS, check `cloudflared` and `uv` are on the user service PATH (add
`Environment=PATH=...` to the units if not).

Tablet (from the existing SSH session):
```
termux-wake-lock
nohup deploy/run.sh tablet > run.log 2>&1 &
nohup deploy/tunnel.sh tablet > tunnel.log 2>&1 &
```
For boot start, install Termux:Boot and put those two lines in
`~/.termux/boot/yabai`.

Check: from the laptop, `curl -H "X-Origin-Secret: <s>" https://laptop.yabaisearch.org/__ping`
and the same for `tablet.` and `dev.` (dev. needs no secret). Each should
return JSON.

### 4.6 Worker
```
cd deploy/worker
npx wrangler login
npx wrangler secret put ORIGIN_SECRET      # paste the same string
```
In the Cloudflare dashboard, delete the existing DNS records for
`yabaisearch.org` and `www` that point at the old tunnel (a Custom Domain
cannot be created while they exist). Then `npx wrangler deploy`.

Brief downtime is possible between deleting the records and deploy finishing,
so do it at a quiet hour and run the deploy immediately after.

### 4.7 Protect dev.
`dev.yabaisearch.org` runs your unfinished work and has no secret. In
Cloudflare Zero Trust (free) add an Access application for that hostname,
allow only your email. Without it anyone can see work in progress.

## 5. Daily workflow

1. Work in `~/Documents/Yabai-Search`; preview at `https://dev.yabaisearch.org`
   or `http://127.0.0.1:8001`. The dev unit serves this folder directly, so a
   restart (`systemctl --user restart yabai-app@dev`) picks up edits.
2. Commit when happy (release.sh does not commit for you).
3. `deploy/release.sh` (add `--data` if you rebuilt `data/*.sqlite`).

release.sh refuses with uncommitted changes, runs `pytest`, pushes, updates the
laptop copy, then the tablet. It stops at the first failure, so the other
machine keeps serving. `update.sh` rolls back to the previous commit if the
new one fails `/__ping` within 40 seconds.

Local settings (tablet address, key path) can be overridden in
`deploy/release.env`. Defaults come from AGENTS.md.

Check anytime: `deploy/check.sh`.

## 6. Data

`data/` is gitignored, so `git pull` never updates the dictionaries. Rebuild
them on dev, then `deploy/release.sh --data` copies `data/*.sqlite` to both
machines (atomically, via rsync temp files). Code and data that depend on each
other should be released together. If you change the schema, release with
`--data` in the same run.

## 7. Failure drills (do these once after setup)

| Test | Expected |
|---|---|
| `systemctl --user stop yabai-tunnel@laptop` | site still loads; `X-Origin: tablet` |
| start it again | `X-Origin: laptop` returns |
| stop the tablet tunnel | no visible change |
| both stopped | 503 page |
| `curl https://laptop.yabaisearch.org/` without the secret | 403 |
| push a deliberately broken commit | `update.sh` rolls back, release stops |

## 8. Rollback and recovery

- Bad release already live on both: `git revert` on dev, commit, release.
- Worker problem: in the dashboard, remove the Custom Domains, then re-add
  DNS records for `yabaisearch.org` pointing at the `yabai-laptop` tunnel
  (`cloudflared tunnel route dns --overwrite-dns yabai-laptop yabaisearch.org`).
  That restores the old single-machine setup in minutes.
- A serving clone that has diverged: `git fetch && git reset --hard origin/main`.

## 9. Add to AGENTS.md (it asks to be kept current)

```
## Hosting
- Three copies: ~/Documents/Yabai-Search (edit here, :8001, dev.yabaisearch.org),
  ~/Desktop/ai/yabai-laptop (:8000) and the tablet (:8000). Only ~/Desktop/ai/yabai-laptop and the
  tablet pull from GitHub; the working copy is the checkout itself.
- Publish with deploy/release.sh [--data] after committing. See deploy/PLAN.md.
```
Note: AGENTS.md says the tablet is reached via the laptop tunnel and that the
public URLs run through "the Cloudflare tunnel on this PC"; update those
lines once the separate tablet tunnel is live.

## 10. Open items

- Termux `cloudflared` result (section 4.4) decides whether the tablet can
  join at all.
- Whether the tablet's older dependencies need any change: the shared-secret
  guard is now Django middleware, so they should not.
- The 800 ms hedge is a guess; tune `HEDGE_MS` in `wrangler.toml` after
  watching `X-Origin` for a few days.

## 11. Accounts database replication (laptop <-> tablet)

> Superseded: the single global writer, the Durable Object that picks it, and
> replication through Cloudflare instead of Tailscale are described in
> `deploy/GLOBAL-WRITER.md`. Sections 3 and 11 describe the older laptop-first design.

Accounts, sessions and settings live in `data/accounts.sqlite3` on each machine.
`accounts/replication.py` (standard library only) keeps them in step. Only one
machine takes writes at a time: the Worker sends them to the laptop and uses the
tablet only when the laptop is unreachable. There is nothing to merge.

### How it behaves
- Every write bumps a counter that belongs to the machine that made it (SQLite
  triggers, so admin edits and raw SQL count). The pair of counters is a version
  vector. Clocks are never compared.
- Each machine runs a small server (port 8765, secret + private-address check) and
  a pull loop (every 10 s). It installs the peer's copy only when the peer has
  every write it has plus more. A returning machine with an older copy therefore
  can never overwrite newer data, and never serves before catching up
  (`run.sh` runs `catch-up` before gunicorn, on every restart).
- Installs happen in one exclusive transaction, so the running app is never
  handed a half-copied database.
- If both machines took writes (a tunnel dropped while the process ran, or a crash
  before replication), the side with more writes wins and the other's database is
  saved in `data/replication-archive/`. Recover its accounts with:
  `python -m accounts.replication salvage data/replication-archive/<file> [--dry-run]`.
  Taken usernames are skipped and listed; sessions are not copied.
- A machine whose peer is unreachable at boot waits up to `YABAI_REPL_WAIT`
  seconds, then serves its own copy.
- Schemas: a machine will not install a peer database that is newer than its own
  code. Release with `release.sh` so both update within a minute; `run.sh` runs
  `migrate` after catch-up on every start.
- A signup POST that the Worker delivers twice logs the person in instead of
  showing "username taken" (same username and password within 2 minutes).

### Setup
1. In `deploy/env.local` on both machines set the `YABAI_REPL_*` values from
   `env.local.example` (PEER_URL points at the other machine's Tailscale address;
   the secret is identical). Keep `DJANGO_SECRET_KEY` identical too.
2. Make sure each machine can reach the other's port 8765 over Tailscale.
3. Restart `run.sh`. The first start of the first machine creates the database
   (`migrate`); the other one downloads it.

### Check and drills
- `python -m accounts.replication status` prints both vectors and the verdict
  (`noop`, `keep`, `install`, `install_conflict`).
- Stop the laptop tunnel, sign up on the tablet, start the laptop: the laptop log
  should say `installed` and the account exists on both.
- Stop the laptop's pull process only (keep it serving), sign up on the tablet,
  then restart: no data is overwritten.
- Known limits: a crash within one pull interval of a write can lose it (it is
  recoverable only if the crashed machine's file survives and is archived by the
  next conflict). The snapshot travels over Tailscale; it contains session keys.
