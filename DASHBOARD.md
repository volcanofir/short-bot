# Short Bot Dashboard

Private, mobile-friendly Traditional Chinese dashboard for `volcanofir/short-bot`.

## Architecture

- **Bot runtime:** Render, `main` branch, `gunicorn runtime_v2:app`
- **Dashboard frontend:** GitHub Pages, `dashboard` branch, `/docs`
- **Private data:** Supabase PostgreSQL + Auth + RLS
- **Strategy:** V2.9 remains alert/paper-analysis only. The dashboard never places orders.

Expected Pages URL: https://volcanofir.github.io/short-bot/

## Authentication

The frontend signs in through Supabase Auth. The approved owner account is authorized by
`app_metadata.short_bot_role = owner`. Tables keep RLS enabled; anonymous users have no
read/write access to private dashboard data.

`docs/config.js` contains only public client values:

- Supabase project URL
- Supabase `sb_publishable_...` key
- Render public service URL

Never place `service_role`, `sb_secret`, Telegram, LINE, Fugle or FinMind credentials in
the dashboard branch.

## Data sources

### Bot observation / alert stream

The Render runtime exposes `GET /dashboard-live`.

The endpoint requires a valid Supabase bearer token and checks the authenticated user's
`short_bot_role`. It returns a sanitized snapshot of:

- current V2.9 watchlist
- current-day Bot alert records
- runtime scan status

The browser automatically upserts this feed into:

- `public.dashboard_candidates`
- `public.dashboard_alerts`

This gives the dashboard a persisted history whenever the owner dashboard is active while
keeping the Bot itself independent from the Supabase service-role key.

### Actual trades

The owner manually records real fills in `public.dashboard_trades`.

Supported actions:

- create actual entry
- edit entry, quantity, costs and note
- change an open trade to closed and record exit date/time/price
- delete an incorrect record

For short trades the database-generated realized P&L is:

`(entry - exit) * quantity - costs`

Open trades have no realized P&L. Performance, win rate, profit factor and drawdown only use
**closed actual trades**. Bot paper alerts never enter formal performance.

### Shadow Lab

`public.dashboard_experiments` stays separate from actual-trade performance. Shadow samples,
win rate and P&L are displayed as research results only.

## scan_date rule

The live watchlist's `scan_date` means **the session being monitored**, not the date of the
previous closing bar used to construct the candidate.

- Before the weekday 13:40 close scan: current Taiwan trading date
- After the 13:40 close scan: next trading date
- Weekend: next weekday

This fixes the previous mismatch where a 09:xx live watchlist could appear under the previous
day because `get_last_trading_day()` describes the source close, not the monitored session.

Bot alerts always use the current Taiwan session date.

## Supabase tables

- `dashboard_candidates` — persisted Bot observation list
- `dashboard_alerts` — persisted Bot alert records
- `dashboard_trades` — owner's real trades
- `dashboard_experiments` — Shadow results
- `dashboard_snapshots` — retained compatibility snapshot table

All private tables use RLS. Candidate/alert browser sync is allowed only for the authenticated
owner role. Actual trades are additionally isolated by `owner_id = auth.uid()`.

## Local verification

No package install is required for the existing data-unit tests:

```sh
node --test dashboard-tests/data.test.mjs
```

Serve `docs/` with a static HTTP server for UI testing. Hash routing keeps GitHub Pages
compatible without rewrites.

## Deployment / rollback

GitHub Pages should publish from:

- Branch: `dashboard`
- Folder: `/docs`

Render remains on `main`. The dashboard branch must not be merged into `main` merely to
publish the frontend.

To roll back the frontend, revert the relevant dashboard-branch commit. To roll back the Bot
feed, revert the `runtime_v2.py` commit on `main`.
