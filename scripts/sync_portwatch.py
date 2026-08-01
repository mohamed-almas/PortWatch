"""
Pull IMF PortWatch data from the public ArcGIS FeatureServers and upsert into
Supabase (project: Liner Services / jeuqpnhajmatekbzlfne, tables prefixed imf_).

No local storage of the source data -- this streams ArcGIS query pages
directly into Postgres via the Supabase REST API, one page at a time
(never accumulates a whole dataset in memory -- some of these tables are
multiple millions of rows).

Env vars required:
  SUPABASE_URL              e.g. https://jeuqpnhajmatekbzlfne.supabase.co
  SUPABASE_SERVICE_ROLE_KEY service role key (server-side only, never expose client-side)

Optional:
  SYNC_SINCE_DAYS      how many days of daily-activity history to (re)pull each run
                        (default 30 -- the weekly refresh only needs ~7-14 days,
                        but a wider rolling window cheaply absorbs late-arriving/
                        revised days without needing a special-case backfill).
                        Pass a large value (e.g. 3000) for a one-time full
                        historical backfill.
  LOAD_STATIC_DATASETS  "true" to also (re)load the Spillover Simulator and
                        Climate Scenarios tables. These are IMF-published static
                        snapshots (not updated weekly), ~7M rows combined, so
                        they're opt-in rather than part of the default weekly run.
  SYNC_TARGET           Run only one named sync instead of the full set. Use this
                        for static datasets so each fits comfortably inside a
                        single job -- e.g. SYNC_TARGET=spillover_supplychain.
                        One of: ports, chokepoints, port_activity,
                        chokepoint_activity, disruptions, spillover_port,
                        spillover_country_trade, spillover_supplychain,
                        climate_port_risk, climate_trade_risk.
"""
import os
import sys
import time
import datetime
import requests

ARCGIS_BASE = "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services"
PAGE_SIZE = 1000  # safe across all layers used here (lowest maxRecordCount is 1000)
MAX_RETRIES = 6
RETRY_BACKOFF_BASE = 2  # seconds: 2, 4, 8, 16, 32, 64

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
SYNC_SINCE_DAYS = int(os.environ.get("SYNC_SINCE_DAYS", "30"))
LOAD_STATIC_DATASETS = os.environ.get("LOAD_STATIC_DATASETS", "false").lower() == "true"
SYNC_TARGET = os.environ.get("SYNC_TARGET", "").strip()

SESSION = requests.Session()
SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}


def request_with_retry(method, url, **kwargs):
    """requests call with exponential-backoff retry on transient failures
    (connection errors, timeouts, 5xx). ArcGIS's public endpoint intermittently
    502s under load -- a single blip shouldn't kill a multi-hour backfill."""
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = SESSION.request(method, url, **kwargs)
            if resp.status_code >= 500:
                raise requests.exceptions.HTTPError(f"{resp.status_code} server error", response=resp)
            return resp
        except (requests.exceptions.RequestException,) as exc:
            last_exc = exc
            if attempt == MAX_RETRIES:
                break
            wait = RETRY_BACKOFF_BASE ** attempt
            print(f"  request failed ({exc}), retrying in {wait}s (attempt {attempt}/{MAX_RETRIES})...", flush=True)
            time.sleep(wait)
    raise last_exc


def arcgis_pages(layer_url, where="1=1", out_fields="*"):
    """Page through an ArcGIS FeatureServer layer, yielding one page (list of
    attribute dicts) at a time."""
    offset = 0
    while True:
        params = {
            "where": where,
            "outFields": out_fields,
            "f": "json",
            "resultOffset": offset,
            "resultRecordCount": PAGE_SIZE,
            "orderByFields": "ObjectId",
        }
        data = None
        for attempt in range(1, MAX_RETRIES + 1):
            resp = request_with_retry("GET", f"{layer_url}/query", params=params, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            if "error" not in data:
                break
            # ArcGIS sometimes returns an in-body error (HTTP 200, JSON has "error")
            # on transient server-side hiccups -- confirmed the identical request
            # succeeds moments later, so this is retryable, not a real bad request.
            if attempt == MAX_RETRIES:
                raise RuntimeError(f"ArcGIS error at offset {offset} after {MAX_RETRIES} attempts: {data['error']}")
            wait = RETRY_BACKOFF_BASE ** attempt
            print(f"  ArcGIS returned error at offset {offset} ({data['error']}), retrying in {wait}s "
                  f"(attempt {attempt}/{MAX_RETRIES})...", flush=True)
            time.sleep(wait)
        features = data.get("features", [])
        if not features:
            return
        yield [f["attributes"] for f in features]
        if len(features) < PAGE_SIZE:
            return
        offset += PAGE_SIZE


def to_date_str(value):
    """esriFieldTypeDateOnly is serialized as a "YYYY-MM-DD" string by ArcGIS;
    esriFieldTypeDate (regular date/timestamp) is serialized as epoch millis."""
    if value is None:
        return None
    if isinstance(value, str):
        return value[:10]
    return datetime.datetime.utcfromtimestamp(value / 1000).strftime("%Y-%m-%d")


def to_timestamp_str(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return datetime.datetime.utcfromtimestamp(value / 1000).isoformat()


def supabase_upsert_batch(table, rows, on_conflict=None):
    """POST one batch. Table upserts (has a natural key) pass on_conflict;
    wholesale-reload tables (no natural key) omit it and just insert."""
    if not rows:
        return
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = dict(SB_HEADERS)
    if on_conflict:
        url += f"?on_conflict={on_conflict}"
        headers["Prefer"] = "resolution=merge-duplicates"
    resp = request_with_retry("POST", url, headers=headers, json=rows, timeout=120)
    if resp.status_code >= 300:
        raise RuntimeError(f"Supabase write failed for {table}: {resp.status_code} {resp.text[:500]}")


def supabase_delete_all(table):
    """Delete-all for wholesale-reload tables. Uses the surrogate `id` column
    every such table has, since PostgREST requires a filter on delete."""
    url = f"{SUPABASE_URL}/rest/v1/{table}?id=gte.0"
    resp = request_with_retry("DELETE", url, headers=SB_HEADERS, timeout=120)
    if resp.status_code >= 300:
        raise RuntimeError(f"Supabase delete-all failed for {table}: {resp.status_code} {resp.text[:500]}")


def log_run(dataset, row_count, min_date=None, max_date=None, status="ok", notes=None):
    supabase_upsert_batch(
        "imf_refresh_log",
        [{
            "dataset": dataset,
            "row_count": row_count,
            "min_date": min_date,
            "max_date": max_date,
            "status": status,
            "notes": notes,
        }],
    )


def sync_stream(label, layer_url, transform, table, on_conflict=None, where="1=1", wholesale=False):
    """Generic paged sync: fetch page -> transform -> upsert page, immediately,
    without ever holding the full result set in memory."""
    if wholesale:
        supabase_delete_all(table)
    total = 0
    dates = []
    for page in arcgis_pages(layer_url, where=where):
        rows = [transform(a) for a in page]
        supabase_upsert_batch(table, rows, on_conflict=on_conflict)
        total += len(rows)
        for r in rows:
            d = r.get("date") or r.get("fromdate")
            if d:
                dates.append(d)
        print(f"  {label}: {total} rows so far...", flush=True)
    min_d, max_d = (min(dates), max(dates)) if dates else (None, None)
    print(f"{label}: wrote {total} rows" + (f" ({min_d}..{max_d})" if dates else ""))
    log_run(label, total, min_d, max_d)
    return total


# ---------------------------------------------------------------------------
# Reference tables (small, always fetched in full)
# ---------------------------------------------------------------------------

def sync_ports():
    def transform(a):
        return {
            "portid": a.get("portid"),
            "portname": a.get("portname"),
            "fullname": a.get("fullname"),
            "country": a.get("country"),
            "iso3": a.get("ISO3"),
            "continent": a.get("continent"),
            "lat": a.get("lat"),
            "lon": a.get("lon"),
            "locode": a.get("LOCODE"),
            "pageid": a.get("pageid"),
            "vessel_count_total": a.get("vessel_count_total"),
            "vessel_count_container": a.get("vessel_count_container"),
            "vessel_count_dry_bulk": a.get("vessel_count_dry_bulk"),
            "vessel_count_general_cargo": a.get("vessel_count_general_cargo"),
            "vessel_count_roro": a.get("vessel_count_RoRo"),
            "vessel_count_tanker": a.get("vessel_count_tanker"),
            "industry_top1": a.get("industry_top1"),
            "industry_top2": a.get("industry_top2"),
            "industry_top3": a.get("industry_top3"),
            "share_country_maritime_import": a.get("share_country_maritime_import"),
            "share_country_maritime_export": a.get("share_country_maritime_export"),
        }
    sync_stream("imf_ports", f"{ARCGIS_BASE}/PortWatch_ports_database/FeatureServer/0",
                transform, "imf_ports", on_conflict="portid")


def sync_chokepoints():
    def transform(a):
        return {
            "portid": a.get("portid"),
            "portname": a.get("portname"),
            "fullname": a.get("fullname"),
            "country": a.get("country"),
            "iso3": a.get("ISO3"),
            "continent": a.get("continent"),
            "lat": a.get("lat"),
            "lon": a.get("lon"),
            "vessel_count_total": a.get("vessel_count_total"),
            "vessel_count_container": a.get("vessel_count_container"),
            "vessel_count_dry_bulk": a.get("vessel_count_dry_bulk"),
            "vessel_count_general_cargo": a.get("vessel_count_general_cargo"),
            "vessel_count_roro": a.get("vessel_count_RoRo"),
            "vessel_count_tanker": a.get("vessel_count_tanker"),
            "industry_top1": a.get("industry_top1"),
            "industry_top2": a.get("industry_top2"),
            "industry_top3": a.get("industry_top3"),
        }
    sync_stream("imf_chokepoints", f"{ARCGIS_BASE}/PortWatch_chokepoints_database/FeatureServer/0",
                transform, "imf_chokepoints", on_conflict="portid")


# ---------------------------------------------------------------------------
# Daily fact tables (rolling window by default; SYNC_SINCE_DAYS controls depth)
# ---------------------------------------------------------------------------

def sync_port_activity():
    since = (datetime.date.today() - datetime.timedelta(days=SYNC_SINCE_DAYS)).isoformat()
    def transform(a):
        return {
            "portid": a.get("portid"),
            "date": to_date_str(a.get("date")),
            "portcalls": a.get("portcalls"),
            "portcalls_container": a.get("portcalls_container"),
            "portcalls_dry_bulk": a.get("portcalls_dry_bulk"),
            "portcalls_general_cargo": a.get("portcalls_general_cargo"),
            "portcalls_roro": a.get("portcalls_roro"),
            "portcalls_tanker": a.get("portcalls_tanker"),
            "portcalls_cargo": a.get("portcalls_cargo"),
            "import": a.get("import"),
            "import_container": a.get("import_container"),
            "import_dry_bulk": a.get("import_dry_bulk"),
            "import_general_cargo": a.get("import_general_cargo"),
            "import_roro": a.get("import_roro"),
            "import_tanker": a.get("import_tanker"),
            "import_cargo": a.get("import_cargo"),
            "export": a.get("export"),
            "export_container": a.get("export_container"),
            "export_dry_bulk": a.get("export_dry_bulk"),
            "export_general_cargo": a.get("export_general_cargo"),
            "export_roro": a.get("export_roro"),
            "export_tanker": a.get("export_tanker"),
            "export_cargo": a.get("export_cargo"),
        }
    sync_stream("imf_port_activity", f"{ARCGIS_BASE}/Daily_Ports_Data/FeatureServer/0",
                transform, "imf_port_activity", on_conflict="portid,date",
                where=f"date >= DATE '{since}'")


def sync_chokepoint_activity():
    since = (datetime.date.today() - datetime.timedelta(days=SYNC_SINCE_DAYS)).isoformat()
    def transform(a):
        return {
            "portid": a.get("portid"),
            "date": to_date_str(a.get("date")),
            "n_total": a.get("n_total"),
            "n_container": a.get("n_container"),
            "n_dry_bulk": a.get("n_dry_bulk"),
            "n_general_cargo": a.get("n_general_cargo"),
            "n_roro": a.get("n_roro"),
            "n_tanker": a.get("n_tanker"),
            "n_cargo": a.get("n_cargo"),
            "capacity": a.get("capacity"),
            "capacity_container": a.get("capacity_container"),
            "capacity_dry_bulk": a.get("capacity_dry_bulk"),
            "capacity_general_cargo": a.get("capacity_general_cargo"),
            "capacity_roro": a.get("capacity_roro"),
            "capacity_tanker": a.get("capacity_tanker"),
            "capacity_cargo": a.get("capacity_cargo"),
        }
    sync_stream("imf_chokepoint_activity", f"{ARCGIS_BASE}/Daily_Chokepoints_Data/FeatureServer/0",
                transform, "imf_chokepoint_activity", on_conflict="portid,date",
                where=f"date >= DATE '{since}'")


# ---------------------------------------------------------------------------
# Disruptions (small, ~130 rows, ongoing events -- upserted every run)
# ---------------------------------------------------------------------------

def sync_disruptions():
    def transform(a):
        return {
            "eventid": a.get("eventid"),
            "eventtype": a.get("eventtype"),
            "eventname": a.get("eventname"),
            "htmlname": a.get("htmlname"),
            "htmldescription": a.get("htmldescription"),
            "alertlevel": a.get("alertlevel"),
            "country": a.get("country"),
            "fromdate": to_date_str(a.get("fromdate")),
            "todate": to_date_str(a.get("todate")),
            "year": a.get("year"),
            "severitytext": a.get("severitytext"),
            "lat": a.get("lat"),
            "long": a.get("long"),
            "editdate": to_timestamp_str(a.get("editdate")),
            "affectedports": a.get("affectedports"),
            "n_affectedports": a.get("n_affectedports"),
            "affectedpopulation": a.get("affectedpopulation"),
            "pageid": a.get("pageid"),
        }
    sync_stream("imf_disruptions", f"{ARCGIS_BASE}/portwatch_disruptions_database/FeatureServer/0",
                transform, "imf_disruptions", on_conflict="eventid")


# ---------------------------------------------------------------------------
# Spillover Simulator + Climate Scenarios: static IMF snapshots (LOAD_STATIC_DATASETS=true only)
# ---------------------------------------------------------------------------

def sync_spillover_port_impact():
    def transform(a):
        return {
            "from_portid": a.get("from_portid"), "from_portname": a.get("from_portname"),
            "from_country": a.get("from_country"), "from_iso3": a.get("from_iso3"),
            "from_lat": a.get("from_lat"), "from_lon": a.get("from_lon"),
            "to_portid": a.get("to_portid"), "to_portname": a.get("to_portname"),
            "to_country": a.get("to_country"), "to_iso3": a.get("to_iso3"),
            "to_lat": a.get("to_lat"), "to_lon": a.get("to_lon"),
            "average_transit_days": a.get("average_transit_days"),
            "daily_capacity_at_risk": a.get("daily_capacity_at_risk"),
            "relative_capacity_at_risk": a.get("relative_capacity_at_risk"),
        }
    sync_stream("imf_spillover_port_impact", f"{ARCGIS_BASE}/spillovers_port_level_impact/FeatureServer/0",
                transform, "imf_spillover_port_impact", wholesale=True)


def sync_spillover_country_trade_impact():
    def transform(a):
        return {
            "from_portid": a.get("from_portid"), "from_portname": a.get("from_portname"),
            "from_country": a.get("from_country"), "from_iso3": a.get("from_iso3"),
            "from_lat": a.get("from_lat"), "from_lon": a.get("from_lon"),
            "to_country": a.get("to_country"), "to_iso3": a.get("to_iso3"),
            "to_lat": a.get("to_lat"), "to_lon": a.get("to_lon"),
            "industry": a.get("industry"), "hs_section": a.get("hs_section"),
            "unit": a.get("unit"), "scale": a.get("scale"),
            "daily_export_value_at_risk": a.get("daily_export_value_at_risk"),
            "daily_import_value_at_risk": a.get("daily_import_value_at_risk"),
        }
    sync_stream("imf_spillover_country_trade_impact", f"{ARCGIS_BASE}/spillovers_trade/FeatureServer/0",
                transform, "imf_spillover_country_trade_impact", wholesale=True)


def sync_spillover_supplychain_impact():
    def transform(a):
        return {
            "from_portid": a.get("from_portid"), "from_portname": a.get("from_portname"),
            "from_country": a.get("from_country"), "from_iso3": a.get("from_iso3"),
            "from_lat": a.get("from_lat"), "from_lon": a.get("from_lon"),
            "to_country": a.get("to_country"), "to_iso3": a.get("to_iso3"),
            "to_lat": a.get("to_lat"), "to_lon": a.get("to_lon"),
            "industry": a.get("industry"), "hs_section": a.get("hs_section"),
            "unit": a.get("unit"), "scale": a.get("scale"),
            "daily_consumption_at_risk": a.get("daily_consumption_at_risk"),
            "daily_industryoutput_at_risk": a.get("daily_industryoutput_at_risk"),
        }
    sync_stream("imf_spillover_supplychain_impact", f"{ARCGIS_BASE}/spillovers_supplychain/FeatureServer/0",
                transform, "imf_spillover_supplychain_impact", wholesale=True)


def sync_climate_port_risk():
    def transform(a):
        return {
            "portid": a.get("portid"), "portname": a.get("portname"),
            "country": a.get("country"), "iso3": a.get("ISO3"),
            "lat": a.get("lat"), "lon": a.get("lon"),
            "scenario": a.get("scenario"), "unit": a.get("unit"),
            "measure": a.get("measure"), "value": a.get("value"),
            "hazard": a.get("hazard"),
        }
    sync_stream("imf_climate_port_risk", f"{ARCGIS_BASE}/climate_scenarios_climate_risk/FeatureServer/0",
                transform, "imf_climate_port_risk", wholesale=True)


def sync_climate_trade_risk():
    def transform(a):
        return {
            "from_country": a.get("from_country"), "from_iso3": a.get("from_ISO3"),
            "to_portid": a.get("to_portid"), "to_portname": a.get("to_portname"),
            "to_country": a.get("to_country"), "to_iso3": a.get("to_ISO3"),
            "to_lat": a.get("to_lat"), "to_lon": a.get("to_lon"),
            "scenario": a.get("scenario"), "flow": a.get("flow"),
            "industry": a.get("industry"), "hs_section": a.get("hs_section"),
            "rank": a.get("rank"), "days_downtime_at_port": a.get("days_downtime_at_port"),
            "trade_value_at_risk": a.get("trade_value_at_risk"), "unit": a.get("unit"),
        }
    sync_stream("imf_climate_trade_risk", f"{ARCGIS_BASE}/scenarios_trade/FeatureServer/0",
                transform, "imf_climate_trade_risk", wholesale=True)


DATASET_SYNCS = {
    "ports": sync_ports,
    "chokepoints": sync_chokepoints,
    "port_activity": sync_port_activity,
    "chokepoint_activity": sync_chokepoint_activity,
    "disruptions": sync_disruptions,
    "spillover_port": sync_spillover_port_impact,
    "spillover_country_trade": sync_spillover_country_trade_impact,
    "spillover_supplychain": sync_spillover_supplychain_impact,
    "climate_port_risk": sync_climate_port_risk,
    "climate_trade_risk": sync_climate_trade_risk,
}


def refresh_matviews():
    for mv in (
        "imf_mv_global_daily", "imf_mv_port_weekly", "imf_mv_chokepoint_daily",
        "imf_mv_country_daily", "imf_mv_region_daily",
        "imf_mv_port_recent_summary", "imf_mv_chokepoint_recent_summary",
    ):
        resp = SESSION.post(
            f"{SUPABASE_URL}/rest/v1/rpc/imf_refresh_matview",
            headers=SB_HEADERS,
            json={"mv_name": mv},
            timeout=120,
        )
        if resp.status_code >= 300:
            print(f"WARNING: could not refresh {mv}: {resp.status_code} {resp.text[:300]}", file=sys.stderr)
        else:
            print(f"refreshed {mv}")


if __name__ == "__main__":
    if SYNC_TARGET:
        if SYNC_TARGET not in DATASET_SYNCS:
            raise SystemExit(f"Unknown SYNC_TARGET '{SYNC_TARGET}'. Valid: {', '.join(DATASET_SYNCS)}")
        DATASET_SYNCS[SYNC_TARGET]()
        refresh_matviews()
    else:
        # Default run: reference tables first (fact tables logically depend on them)
        sync_ports()
        sync_chokepoints()
        sync_port_activity()
        sync_chokepoint_activity()
        sync_disruptions()

        if LOAD_STATIC_DATASETS:
            # Each of these is multi-million rows and can take a long time; running
            # all 5 sequentially risks the job timeout (as happened in practice --
            # supplychain got cut off mid-load). Load them one SYNC_TARGET per run instead.
            print(
                "NOTE: LOAD_STATIC_DATASETS=true runs all 5 static datasets sequentially "
                "and may exceed the job timeout on a large table. Prefer running each via "
                "SYNC_TARGET=<name> in its own workflow trigger.",
                file=sys.stderr,
            )
            sync_spillover_port_impact()
            sync_spillover_country_trade_impact()
            sync_spillover_supplychain_impact()
            sync_climate_port_risk()
            sync_climate_trade_risk()

        refresh_matviews()
