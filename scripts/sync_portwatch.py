"""
Pull IMF PortWatch data from the public ArcGIS FeatureServers and upsert into
Supabase (project: Liner Services / jeuqpnhajmatekbzlfne, tables prefixed imf_).

No local storage of the source data — this streams ArcGIS query pages
directly into Postgres via the Supabase REST API (PostgREST upsert).

Env vars required:
  SUPABASE_URL              e.g. https://jeuqpnhajmatekbzlfne.supabase.co
  SUPABASE_SERVICE_ROLE_KEY service role key (server-side only, never expose client-side)

Optional:
  SYNC_SINCE_DAYS  how many days of fact-table history to (re)pull each run
                    (default 14 -- covers the weekly refresh plus a safety margin
                    for late-arriving/revised days). Reference tables (ports,
                    chokepoints) are always pulled in full since they're small.
"""
import os
import sys
import time
import datetime
import requests

ARCGIS_BASE = "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services"
PAGE_SIZE = 1000  # matches the FeatureServer's maxRecordCount

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
SYNC_SINCE_DAYS = int(os.environ.get("SYNC_SINCE_DAYS", "14"))

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates",
}


def arcgis_query_all(layer_url, where="1=1", out_fields="*"):
    """Page through an ArcGIS FeatureServer layer, yielding attribute dicts."""
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
        resp = requests.get(f"{layer_url}/query", params=params, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"ArcGIS error: {data['error']}")
        features = data.get("features", [])
        if not features:
            break
        for f in features:
            yield f["attributes"]
        if len(features) < PAGE_SIZE:
            break
        offset += PAGE_SIZE


def to_date_str(value):
    """Daily_Ports_Data / Daily_Chokepoints_Data expose `date` as esriFieldTypeDateOnly,
    which ArcGIS serializes as a "YYYY-MM-DD" string, not epoch millis."""
    if value is None:
        return None
    if isinstance(value, str):
        return value[:10]
    return datetime.datetime.utcfromtimestamp(value / 1000).strftime("%Y-%m-%d")


def supabase_upsert(table, rows, batch_size=500):
    if not rows:
        return 0
    total = 0
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        resp = requests.post(url, headers=SB_HEADERS, json=batch, timeout=60)
        if resp.status_code >= 300:
            raise RuntimeError(f"Supabase upsert failed for {table}: {resp.status_code} {resp.text[:500]}")
        total += len(batch)
        time.sleep(0.1)  # be polite to PostgREST
    return total


def log_run(dataset, row_count, min_date, max_date, status="ok", notes=None):
    supabase_upsert(
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


def sync_ports():
    layer = f"{ARCGIS_BASE}/PortWatch_ports_database/FeatureServer/0"
    rows = []
    for a in arcgis_query_all(layer):
        rows.append({
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
        })
    n = supabase_upsert("imf_ports?on_conflict=portid", rows)
    print(f"imf_ports: upserted {n} rows")
    log_run("imf_ports", n, None, None)


def sync_chokepoints():
    layer = f"{ARCGIS_BASE}/PortWatch_chokepoints_database/FeatureServer/0"
    rows = []
    for a in arcgis_query_all(layer):
        rows.append({
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
        })
    n = supabase_upsert("imf_chokepoints?on_conflict=portid", rows)
    print(f"imf_chokepoints: upserted {n} rows")
    log_run("imf_chokepoints", n, None, None)


def sync_port_activity():
    layer = f"{ARCGIS_BASE}/Daily_Ports_Data/FeatureServer/0"
    since = (datetime.date.today() - datetime.timedelta(days=SYNC_SINCE_DAYS)).isoformat()
    where = f"date >= DATE '{since}'"
    rows = []
    for a in arcgis_query_all(layer, where=where):
        rows.append({
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
        })
    n = supabase_upsert("imf_port_activity?on_conflict=portid,date", rows)
    dates = [r["date"] for r in rows if r["date"]]
    print(f"imf_port_activity: upserted {n} rows ({min(dates, default='-')}..{max(dates, default='-')})")
    log_run("imf_port_activity", n, min(dates, default=None), max(dates, default=None))


def sync_chokepoint_activity():
    layer = f"{ARCGIS_BASE}/Daily_Chokepoints_Data/FeatureServer/0"
    since = (datetime.date.today() - datetime.timedelta(days=SYNC_SINCE_DAYS)).isoformat()
    where = f"date >= DATE '{since}'"
    rows = []
    for a in arcgis_query_all(layer, where=where):
        rows.append({
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
        })
    n = supabase_upsert("imf_chokepoint_activity?on_conflict=portid,date", rows)
    dates = [r["date"] for r in rows if r["date"]]
    print(f"imf_chokepoint_activity: upserted {n} rows ({min(dates, default='-')}..{max(dates, default='-')})")
    log_run("imf_chokepoint_activity", n, min(dates, default=None), max(dates, default=None))


def refresh_matviews():
    for mv in ("imf_mv_global_daily", "imf_mv_port_weekly", "imf_mv_chokepoint_daily"):
        resp = requests.post(
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
    # Reference tables first (fact tables FK into them)
    sync_ports()
    sync_chokepoints()
    sync_port_activity()
    sync_chokepoint_activity()
    refresh_matviews()
