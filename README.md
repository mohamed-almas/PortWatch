# IMF PortWatch Executive Dashboard

Pulls public data from the [IMF PortWatch](https://portwatch.imf.org) ArcGIS
FeatureServers straight into Supabase (no local downloads), then serves a
static executive dashboard from GitHub Pages that reads Supabase directly.

```
ArcGIS FeatureServer (public, no auth)
        │  GitHub Actions cron (weekly + midweek safety net)
        ▼
scripts/sync_portwatch.py  ──upsert──▶  Supabase Postgres (project: Liner Services)
                                          tables: imf_ports, imf_chokepoints,
                                                  imf_port_activity, imf_chokepoint_activity
                                          matviews: imf_mv_global_daily,
                                                    imf_mv_port_weekly,
                                                    imf_mv_chokepoint_daily
        ▼
dashboard/index.html  ──PostgREST (anon key, read-only via RLS)──▶  GitHub Pages
```

## Why this shape

- **No local/manual download step.** PortWatch datasets are hosted as public
  Esri FeatureServers — queryable directly over REST. The sync script pages
  through them and upserts straight into Postgres.
- **Supabase as source of truth**, not flat files in the repo: the daily
  port-activity table is ~2,000 ports × daily rows since ~2019, too large to
  usefully commit/refetch as JSON. Materialized views pre-aggregate the cuts
  the dashboard needs so page load stays fast.
- **Source refreshes weekly** (Tuesdays 9AM ET), so the sync job runs weekly
  plus a Friday safety-net run to catch late revisions, rather than polling
  constantly.
- **Dashboard is a static file** — GitHub Pages, zero backend — talking to
  Supabase with the public anon key, which RLS policies restrict to
  `SELECT`-only on the `imf_*` tables.

## One-time setup

1. **GitHub repo secrets** (Settings → Secrets and variables → Actions):
   - `SUPABASE_URL` = `https://jeuqpnhajmatekbzlfne.supabase.co`
   - `SUPABASE_SERVICE_ROLE_KEY` = the project's service_role key (Settings →
     API in the Supabase dashboard — **never** put this in the dashboard
     HTML or commit it; it's Actions-only, server-side).

2. **Enable GitHub Pages** on this repo, serving from the `dashboard/`
   folder (or copy `dashboard/index.html` to the repo root / `docs/` per
   your Pages config).

3. **First sync**: trigger the workflow manually once
   (Actions → "Sync IMF PortWatch data" → Run workflow) to backfill data
   before relying on the schedule. The default `SYNC_SINCE_DAYS=14` pulls a
   rolling recent window; for a full historical backfill (data goes back to
   ~2019), run once locally with a much larger value, e.g.:

   ```bash
   pip install -r requirements.txt
   SUPABASE_URL=https://jeuqpnhajmatekbzlfne.supabase.co \
   SUPABASE_SERVICE_ROLE_KEY=<service_role_key> \
   SYNC_SINCE_DAYS=3000 \
   python scripts/sync_portwatch.py
   ```

   This will be a large pull (~2,000 ports × ~6 years daily). Consider
   narrowing `where` in `sync_port_activity()`/`sync_chokepoint_activity()`
   to a specific year range and running it a few times if the ArcGIS server
   rate-limits large single queries.

## What's already provisioned

- Supabase project: **Liner Services** (`jeuqpnhajmatekbzlfne`) — chosen
  because it held the least data among existing projects. Tables/matviews
  are prefixed `imf_` to stay clearly separated from that project's
  existing `eesea_*` / `ml_liners_*` tables.
- Schema + RLS read-only policies + `imf_refresh_matview()` helper function:
  already applied via migration.
- Dashboard anon key is already wired into `dashboard/index.html` (safe to
  expose — RLS limits it to read-only on `imf_*`).

## Extending the dashboard

- `imf_mv_global_daily` — global daily trade nowcast (headline KPIs, trend line)
- `imf_mv_port_weekly` — per-port weekly rollup (add port drill-down views)
- `imf_mv_chokepoint_daily` — chokepoint transit trend + capacity utilization
- `imf_refresh_log` — sync run history / data-freshness panel

Add a Disruptions layer (GDACS-sourced) the same way if needed — PortWatch
exposes it as another FeatureServer; follow the same
query → transform → upsert pattern in `sync_portwatch.py`.

## Note on the reused Supabase project

`jeuqpnhajmatekbzlfne` (Liner Services) already has 10 tables
(`eesea_*`, `ref_tradelane_classification`) with **RLS disabled** —
unrelated to this project, pre-existing, not modified here. Anyone with the
anon key can currently read/write those tables. Flagging for awareness; not
auto-fixed since it's out of scope for this build.
