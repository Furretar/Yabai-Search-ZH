# Django port

The FastAPI app (`yabai_search/main.py`) is now a Django project. The search code
is unchanged; it moved to `yabai_search/services.py` with the FastAPI parts removed.

## First run

    uv sync
    export DJANGO_SECRET_KEY=dev DJANGO_DEBUG=1
    uv run python manage.py migrate
    uv run python manage.py createsuperuser      # becomes a staff user: can open /reports
    uv run python manage.py runserver

Production (laptop and tablet): `deploy/run.sh laptop|tablet` now starts gunicorn
(1 process, `YABAI_THREADS` threads, default 8). Put the values from
`deploy/env.local.example` in `deploy/env.local`, then run `manage.py migrate` once. With `YABAI_REPL_PEER_URL` set, `run.sh` runs
`migrate` itself and replicates the accounts database to the other machine
(`deploy/PLAN.md` section 11).

## What changed

| Before | Now |
|---|---|
| `yabai_search/main.py` routes | `dictionary/views.py`, `dictionary/urls.py` |
| search logic in `main.py` | `yabai_search/services.py` (same function names) |
| Jinja2 templates | same files, loaded by Django's Jinja2 backend |
| `deploy/guard.py` | `OriginSecretMiddleware` in `yabai_site/middleware.py` |
| debug/cache/inflight middleware | `RequestStampMiddleware`, `CacheHeadersMiddleware` |
| "only this device" IP checks | staff login (`/reports`, `/reports/delete`, `/api/search-log`, `/__debug/status`) |
| none | `accounts/`: signup, login, logout, `UserSettings`, `/api/account/settings` |
| uvicorn | gunicorn (gthread) |

## Behaviour changes to know about

- `/api/search-log` is staff-only. It used to show every visitor's last searches to anyone.
- `/reports` and the delete button need a staff login, and the delete form carries a CSRF token.
- `/api/annotate` rejects text over 100,000 characters (it had no limit).
- `/api/report` allows 10 reports per address per 10 minutes.
- `/api/annotate`, `/api/convert-batch`, `/api/report` are exempt from CSRF checks on purpose:
  they use no cookie authority, and the existing front end sends no token. Account
  endpoints are protected.
- The event-loop lag sampler and `/__debug/lag` are gone (no event loop under gunicorn).
- Passwords: Argon2. There is no minimum length and no common-password block; the
  only remaining checks are similarity to the username and all-numeric rejection.
  There is no password reset (it needs email).

## Not done

- `app.js` does not call `/api/account/settings` yet, so nothing syncs. The server side is ready.
- `tests/test_furigana_selection.py` (headless-browser harness) is skipped until its wrapper is ported.
- No Content-Security-Policy: `index.html` has inline scripts, so a strict policy needs a nonce pass first.
