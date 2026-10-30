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
  +--> laptop.yabaisearch.org --> tunnel "yabai-laptop" --> laptop  ~/yabai-laptop  :8000
  +--> tablet.yabaisearch.org --> tunnel "yabai-tablet" --> tablet  ~/yabai-search  :8000

you
  +--> dev.yabaisearch.org    --> tunnel "yabai-dev"    --> laptop  ~/yabai-dev     :8001
```

Three tunnels, each its own cloudflared process, so one failing never takes
down another. A "tunnel" is a connection your machine opens out to
Cloudflare so the internet can reach it without opening ports at home.

Three copies of the code:

| Copy | Machine | Folder | Port | Purpose |
|---|---|---|---|---|
| dev | laptop | `~/yabai-dev` | 8001 | where you and Claude make changes |
| laptop | laptop | `~/yabai-laptop` | 8000 | serves the public site |
| tablet | tablet | `~/yabai-search` | 8000 | serves as backup |

The serving copies are never edited. They only receive code from GitHub.

## 2. What is in this kit (`deploy/`)

| File | Job |
|---|---|
| `worker/worker.js`, `wrangler.toml` | Cloudflare Worker: failover and hedging |
| `guard.py` | makes the apps refuse requests that skipped the Worker |
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
the Worker, `~/yabai-laptop/deploy/env.local`, and the tablet's
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

### 4.2 Laptop: three folders
```
git clone <repo> ~/yabai-dev
git clone <repo> ~/yabai-laptop
```
Then in each: `uv sync`. Copy `data/` into both (it is gitignored), for
example `cp -r ~/yabai-dev/data ~/yabai-laptop/data`.
Add the `deploy/` folder from this kit to the repo, commit and push it from
dev, then `git pull` in `~/yabai-laptop` and on the tablet.

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
```
pkg install rsync python   # NOTE: Termux has no cloudflared package; see the tablet fallback
cloudflared tunnel login
cloudflared tunnel create yabai-tablet
cloudflared tunnel route dns --overwrite-dns yabai-tablet tablet.yabaisearch.org
```
Fill the ID into `deploy/cloudflared/tablet.yml`. First, delete the old
`tablet.yabaisearch.org` record if the old laptop tunnel still owns it
(`--overwrite-dns` replaces it, but check the dashboard).

Risk: the Termux cloudflared is untested on this tablet. The earlier proot
attempt failed because of Android's seccomp filter, and the Termux build
avoids that, but verify with `cloudflared tunnel info yabai-tablet` (it must
list a connector). If it still hangs, run with `--loglevel debug` and keep
that log. Fallback: a small always-on box (Pi or free VPS) forwarding to the
tablet over Tailscale.

### 4.5 Start things
Laptop:
```
mkdir -p ~/.config/systemd/user
cp ~/yabai-laptop/deploy/systemd/*.service ~/.config/systemd/user/
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
and the same for `tablet.` and `new.` (new. needs no secret). Each should
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

### 4.7 Protect new.
`dev.yabaisearch.org` runs your unfinished work and has no secret. In
Cloudflare Zero Trust (free) add an Access application for that hostname,
allow only your email. Without it anyone can see work in progress.

## 5. Daily workflow

1. Work in `~/yabai-dev`; preview at `https://dev.yabaisearch.org` or
   `http://127.0.0.1:8001`.
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
- Three copies: ~/yabai-dev (edit here, :8001, dev.yabaisearch.org),
  ~/yabai-laptop (:8000) and the tablet (:8000). Serving copies are never edited.
- Publish with deploy/release.sh [--data] after committing. See deploy/PLAN.md.
```
Note: AGENTS.md says the tablet is reached via the laptop tunnel and that the
public URLs run through "the Cloudflare tunnel on this PC"; update those
lines once the separate tablet tunnel is live.

## 10. Open items

- Termux `cloudflared` result (section 4.4) decides whether the tablet can
  join at all.
- Whether the tablet's pinned older fastapi needs any change for
  `deploy/guard.py`: it uses only plain ASGI, so it should not.
- The 800 ms hedge is a guess; tune `HEDGE_MS` in `wrangler.toml` after
  watching `X-Origin` for a few days.
