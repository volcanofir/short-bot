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

The first successful owner login automatically registers a **SHA-256 hash** of the existing
Render `TELEGRAM_BOT_TOKEN` in `dashboard_ingest_auth`. The raw token is never stored in
Supabase or GitHub.

After that bootstrap, Render pushes changed watchlist/alert snapshots directly to the
`ingest_short_bot` RPC about once per minute and immediately after guarded intraday scans.
The RPC accepts writes only when the request's existing Telegram token hashes to the registered
owner value.

Persisted history lives in:

- `public.dashboard_candidates`
- `public.dashboard_alerts`

The browser still upserts the current live feed as an immediate fallback, so the first login
does not have to wait for the next background cycle. No Supabase service-role key is stored on
Render or in GitHub.

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
- `dashboard_ingest_auth` — one owner-only SHA-256 token fingerprint used for secure Bot ingest
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


## Smart Entry Lab

Every new V2.9 intraday alert now creates a frozen `SMART_V1` research record.

The goal is to separate two questions:

1. **Was the original V2.9 signal useful?**
2. **Did waiting for a better entry improve or hurt the result?**

For each alert the Bot stores both paths:

- **V2.9 baseline:** assumes the original alert entry.
- **SMART_V1:** acceptable zone from the alert price through two ticks above it, with the
  expected limit set one tick above the alert price. The expected entry may not cross or sit
  beyond the original structural stop.
- **Entry TTL:** 20 minutes. If the expected limit is not reached, SMART_V1 is marked expired.
- **Tracking window:** 60 minutes after the relevant starting point, capped at the 11:30
  monitoring-session end.
- **Recorded diagnostics:** 5/15/30/60-minute sampled prices, MFE, MAE, 1R, 2R, stop-first,
  fill rate and sample count.

The original signal path keeps tracking even if SMART_V1 never fills. This is deliberate:
otherwise a good V2.9 signal that moved down immediately could be misclassified as a bad
strategy simply because the smarter waiting rule missed the trade.

The price path is sampled at the Bot/runtime cadence (roughly 30–120 seconds depending on
session phase), not reconstructed from tick-by-tick trades. Therefore the lab is designed for
long-run strategy comparison rather than exact exchange-order simulation.

The Dashboard's **Smart Entry** page compares baseline 2R rate, SMART_V1 effective 2R rate,
expected fill rate, stop-first counts and average MFE/MAE over 7/30/90-day windows.

Smart Entry is Shadow-only. It does not place orders and does not change V2.9 entry logic.


## Runtime heartbeat

Render now writes a one-row `dashboard_runtime` heartbeat through the authenticated Bot ingest
path. The Dashboard can therefore distinguish a fresh Bot process from stale historical data
without relying only on the browser's direct Render request. A heartbeat newer than roughly
three minutes is shown as connected.
