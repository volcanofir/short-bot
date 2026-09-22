# Short Bot Dashboard

Static, read-only Traditional Chinese dashboard. Pages source: `dashboard` branch, `/docs` folder. The default `main` branch and all Python/Render files remain unchanged. Do not merge this branch into `main` merely to publish the frontend; that can trigger Render auto-deploy.

## Local verification

Requires Node.js 22 or later for built-in tests, with no package install:

```sh
node --test dashboard-tests/data.test.mjs
```

Serve `docs/` with any static HTTP server. All asset paths are relative and navigation uses hash routes, so `/short-bot/#trades` works on GitHub Pages without rewrites. Demo data is synthetic and is regenerated relative to the current Taipei date. Demo is off after every reload. No real trading data is published with this frontend.

## Features

- Today: dated candidates, entry count, realized net P&L, closed-trade win rate, 14-day cumulative P&L.
- Performance: 7/30/90-day period ending on the selected date; closed trades only; profit factor and maximum drawdown in TWD.
- Trades: 1/7/30/90-day date range, code/name search, status filter, detail dialog and UTF-8 CSV export. Demo exports have DEMO in filename; formula-like fields are escaped.
- Shadow Lab: dated experiment snapshots, separate synthetic sample counts, win rate and P&L. No activation, order placement or Bot control endpoints.
- Empty, loading, timeout, invalid-config, malformed-data and stale-snapshot states. Explicit refresh; no polling of Render.

## Supabase contract (reserved, not provisioned)

`docs/config.js` contains only an HTTPS project root URL and `sb_publishable_...` key. Never publish `service_role`, `sb_secret`, Telegram, LINE, Fugle or FinMind credentials. No secrets or actual Supabase project have been provisioned by this change.

The adapter makes one GET to `/rest/v1/dashboard_snapshots?select=payload&order=created_at.desc&limit=1` with an `apikey` header. A future trusted producer must supply a **complete** snapshot for the supported history (at least 90 days for the period selector). The frontend does not infer completeness or query Render. Date filters use entry date; P&L is a cohort summary of trades entered in that interval, not a daily broker settlement ledger. Snapshot producer should document its fee/tax/slippage model.

Expected relation columns: `created_at timestamptz` and `payload jsonb`. The payload schema is validated by `validateSnapshot` in `docs/data.mjs`. Example shape:

```json
{
  "schema_version": 1,
  "generated_at": "2026-09-23T03:00:00Z",
  "candidates": [{"date":"2026-09-23","code":"EXAMPLE","name":"Example","strategy":"V2.9","price":100,"change":-1.2,"setup":"反彈轉弱","chip":"集中","score":7,"status":"觀察中"}],
  "trades": [{"id":"unique-id","date":"2026-09-23","time":"09:25","code":"EXAMPLE","name":"Example","strategy":"V2.9","status":"closed","entry":100,"exit":99,"quantity":1000,"pnl":850,"note":"Synthetic example, not a real trade"}],
  "experiments": [{"id":"SH-001","date":"2026-09-23","strategy":"V2.9","name":"Example","description":"Synthetic example","status":"模擬觀察","samples":10,"win_rate":60,"pnl":100}]
}
```

Dates/times represent Asia/Taipei. `generated_at` must have an ISO timestamp with offset. Closed trades require numeric `exit` and net `pnl`; open trades may have null values. Experiment `win_rate` is 0–100 or null. Empty arrays represent no records; missing connection is not shown as zero performance. Render health is deliberately unverified.

Before enabling a project: enable RLS, grant only SELECT, and configure an explicit policy for approved public/sanitized dashboard snapshots. Supabase now requires explicit Data API grants for new tables. A public anonymous read policy exposes the approved snapshot to everyone; do not place private trading/account data in that snapshot. For private data, add Supabase Auth and per-user RLS before connecting. Do not disable RLS to make the UI work. This change provides no SQL migration or live database changes.

See [Supabase data security](https://supabase.com/docs/guides/database/secure-data) and [GitHub Pages source configuration](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site).

## Deployment / rollback

GitHub Settings → Pages → Deploy from a branch → `dashboard` → `/docs` → Save. GitHub's built-in Pages workflow deploys this static folder; no workflow secret is needed. Expected URL: https://volcanofir.github.io/short-bot/ . Keep Render's existing source branch and settings untouched. Later dashboard updates should only modify frontend files on `dashboard`. To roll back, revert the frontend commit on that branch, or disable Pages; neither changes `main` or the Bot.
