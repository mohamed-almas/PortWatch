# IMF PortWatch Executive Dashboard

Pulls public data from the [IMF PortWatch](https://portwatch.imf.org) ArcGIS
FeatureServers straight into Supabase (no local downloads), then serves a
static executive dashboard from GitHub Pages that reads Supabase directly.

```
ArcGIS FeatureServer (public, no auth)
        │  GitHub Actions cron (weekly + midweek safety net)
        ▼
scripts/sync_portwatch.py  ──upsert/reload──▶  Supabase Postgres (project: Liner Services)
                                          tables: imf_ports, imf_chokepoints,
                                                  imf_port_activity, imf_chokepoint_activity,
                                                  imf_disruptions,
                                                  imf_spillover_port_impact,
                                                  imf_spillover_country_trade_impact,
                                                  imf_spillover_supplychain_impact,
                                                  imf_climate_port_risk,
                                                  imf_climate_trade_risk
                                          matviews: imf_mv_global_daily,
                                                    imf_mv_port_weekly,
                                                    imf_mv_chokepoint_daily
        ▼
dashboard/index.html  ──PostgREST (anon key, read-only via RLS)──▶  GitHub Pages
```

## Datasets covered

| Table | Source layer | Refresh pattern |
|---|---|---|
| `imf_ports`, `imf_chokepoints` | Ports/Chokepoints reference | full reload every run (small) |
| `imf_port_activity`, `imf_chokepoint_activity` | Daily Port/Chokepoint Activity | rolling window (`SYNC_SINCE_DAYS`), upserted on `(portid, date)` |
| `imf_disruptions` | GDACS-sourced Disruptions | full reload every run (~130 rows, ongoing events), upserted on `eventid` |
| `imf_spillover_port_impact` | Spillover Simulator — port-to-port capacity at risk | **opt-in** (`LOAD_STATIC_DATASETS=true`), wholesale reload |
| `imf_spillover_country_trade_impact` | Spillover Simulator — country import/export value at risk | opt-in, wholesale reload |
| `imf_spillover_supplychain_impact` | Spillover Simulator — industry output/consumption at risk | opt-in, wholesale reload |
| `imf_climate_port_risk` | Climate Scenarios — port-level climate risk | opt-in, wholesale reload |
| `imf_climate_trade_risk` | Climate Scenarios — country trade risk | opt-in, wholesale reload |

The 5 Spillover/Climate tables are IMF-published **static snapshots**
("Last Updated April 3, 2024" on the source site, not touched by PortWatch's
weekly refresh) with no natural unique key, so they're reloaded wholesale
(delete-all + reinsert) rather than upserted, and are gated behind
`LOAD_STATIC_DATASETS` so the default weekly run doesn't re-pull ~6.95M rows
for data that isn't changing.

## Why this shape

- **No local/manual download step.** PortWatch datasets are hosted as public
  Esri FeatureServers — queryable directly over REST. The sync script pages
  through them and upserts straight into Postgres, one page (1,000 rows) at
  a time, so it never holds a full dataset in memory even for the 3.4M-row
  supply-chain table.
- **Supabase as source of truth**, not flat files in the repo: the daily
  port-activity table alone is ~2,000 ports × daily rows since ~2019
  (~5.7M rows), too large to usefully commit/refetch as JSON. Materialized
  views pre-aggregate the cuts the dashboard needs so page load stays fast.
- **Source refreshes weekly** (Tuesdays 9AM ET), so the sync job runs weekly
  plus a Friday safety-net run to catch late revisions, rather than polling
  constantly.
- **Dashboard is a static file** — GitHub Pages, zero backend — talking to
  Supabase with the public anon key, which RLS policies restrict to
  `SELECT`-only on the `imf_*` tables.

## One-time setup

1. **GitHub repo secrets** (Settings → Secrets and variables → Actions):
   - `SUPABASE_URL` = `https://jeuqpnhajmatekbzlfne.supabase.co` — **exactly
     this, no `/rest/v1` suffix** (the script appends that path itself; a
     doubled path causes a `PGRST125` failure).
   - `SUPABASE_SERVICE_ROLE_KEY` = the project's service_role key (Settings →
     API in the Supabase dashboard — **never** put this in the dashboard
     HTML or commit it; it's Actions-only, server-side).

2. **Enable GitHub Pages** on this repo, serving from the `dashboard/`
   folder (or copy `dashboard/index.html` to the repo root / `docs/` per
   your Pages config).

3. **Backfill runs** — trigger manually from Actions → "Sync IMF PortWatch
   data" → Run workflow, which now takes two inputs:
   - `sync_since_days` — days of port/chokepoint daily-activity history to
     pull. Default `30` (the normal weekly cadence, with margin for late
     revisions). For a full historical
     backfill (data goes back to ~2019), set a large value, e.g. `3000`.
   - `load_static_datasets` — set to `true` to (re)load the 5 Spillover/
     Climate tables (~6.95M rows combined).

   **Run these as separate triggers, not combined in one run.** A full
   `sync_since_days=3000` backfill (~5.7M rows) is roughly 2–3 hours by
   itself; adding `load_static_datasets=true` on top risks the 340-minute
   job timeout. Recommended order:
   1. First run: `sync_since_days=3000`, `load_static_datasets=false` — backfills port/chokepoint history.
   2. Second run: `sync_since_days=30` (default), `load_static_datasets=true` — loads the static datasets once.
   3. After that, the scheduled weekly runs use the defaults (`30`, `false`) and stay fast/cheap.

   If ArcGIS rate-limits a very large single run, narrow the backfill by
   running `sync_since_days` at smaller values covering different windows
   (the upsert on `(portid, date)` makes repeat/overlapping runs safe).

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
- `imf_disruptions` — active/recent disruption events (map pins, alert feed)
- `imf_spillover_port_impact` / `imf_spillover_country_trade_impact` /
  `imf_spillover_supplychain_impact` — "what's at risk if port X goes down"
  panels (network/sankey visuals)
- `imf_climate_port_risk` / `imf_climate_trade_risk` — climate scenario risk
  scoring by port/country
- `imf_refresh_log` — sync run history / data-freshness panel (per dataset)

The current `dashboard/index.html` only visualizes the port/chokepoint
activity tables — the disruptions/spillover/climate tables are synced and
queryable but not yet wired into the dashboard UI. That's a follow-up, not
done in this pass.

## Note on the reused Supabase project

`jeuqpnhajmatekbzlfne` (Liner Services) already had 10 tables
(`eesea_*`, `ref_tradelane_classification`) with RLS disabled and several
`SECURITY DEFINER` functions callable by `anon`/`authenticated` — pre-existing,
unrelated to this project. Both were hardened (RLS read-only policies added;
functions switched to `SECURITY INVOKER` where the underlying data was
already anon-readable) as part of this build, at the user's request.
