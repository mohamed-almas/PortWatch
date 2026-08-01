"""
Load a PortWatch CSV export (downloaded by hand from portwatch.imf.org) directly
into Supabase, bypassing the ArcGIS FeatureServer entirely. Useful for the large
one-time backfills where paging through the API hits transient errors.

Usage:
  SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... \
  py scripts/load_csv.py <dataset> <csv_path>

<dataset> is one of: port_activity, chokepoint_activity, spillover_port,
spillover_country_trade, spillover_supplychain, climate_port_risk, climate_trade_risk

Same field-mapping conventions as sync_portwatch.py: fact tables (port_activity,
chokepoint_activity) are upserted on their natural key; the 5 static Spillover/
Climate tables have no natural key and are reloaded wholesale (delete-all, then
bulk insert). Streams the CSV row by row -- never holds the whole file in memory.
"""
import os
import sys
import csv

sys.path.insert(0, os.path.dirname(__file__))
from sync_portwatch import supabase_upsert_batch, supabase_delete_all, log_run  # noqa: E402

BATCH_SIZE = 2000


def to_iso_date(value):
    """PortWatch CSV exports use YYYY/MM/DD; Postgres wants YYYY-MM-DD."""
    if not value:
        return None
    return value.replace("/", "-")


def to_int(value):
    if value is None or value == "":
        return None
    return int(float(value))


def to_float(value):
    if value is None or value == "":
        return None
    return float(value)


TRANSFORMS = {
    "port_activity": {
        "table": "imf_port_activity",
        "on_conflict": "portid,date",
        "fn": lambda r: {
            "portid": r["portid"],
            "date": to_iso_date(r["date"]),
            "portcalls": to_int(r["portcalls"]),
            "portcalls_container": to_int(r["portcalls_container"]),
            "portcalls_dry_bulk": to_int(r["portcalls_dry_bulk"]),
            "portcalls_general_cargo": to_int(r["portcalls_general_cargo"]),
            "portcalls_roro": to_int(r["portcalls_roro"]),
            "portcalls_tanker": to_int(r["portcalls_tanker"]),
            "portcalls_cargo": to_int(r["portcalls_cargo"]),
            "import": to_int(r["import"]),
            "import_container": to_int(r["import_container"]),
            "import_dry_bulk": to_int(r["import_dry_bulk"]),
            "import_general_cargo": to_int(r["import_general_cargo"]),
            "import_roro": to_int(r["import_roro"]),
            "import_tanker": to_int(r["import_tanker"]),
            "import_cargo": to_int(r["import_cargo"]),
            "export": to_int(r["export"]),
            "export_container": to_int(r["export_container"]),
            "export_dry_bulk": to_int(r["export_dry_bulk"]),
            "export_general_cargo": to_int(r["export_general_cargo"]),
            "export_roro": to_int(r["export_roro"]),
            "export_tanker": to_int(r["export_tanker"]),
            "export_cargo": to_int(r["export_cargo"]),
        },
    },
    "chokepoint_activity": {
        "table": "imf_chokepoint_activity",
        "on_conflict": "portid,date",
        "fn": lambda r: {
            "portid": r["portid"],
            "date": to_iso_date(r["date"]),
            "n_total": to_int(r["n_total"]),
            "n_container": to_int(r["n_container"]),
            "n_dry_bulk": to_int(r["n_dry_bulk"]),
            "n_general_cargo": to_int(r["n_general_cargo"]),
            "n_roro": to_int(r["n_roro"]),
            "n_tanker": to_int(r["n_tanker"]),
            "n_cargo": to_int(r["n_cargo"]),
            "capacity": to_int(r["capacity"]),
            "capacity_container": to_int(r["capacity_container"]),
            "capacity_dry_bulk": to_int(r["capacity_dry_bulk"]),
            "capacity_general_cargo": to_int(r["capacity_general_cargo"]),
            "capacity_roro": to_int(r["capacity_roro"]),
            "capacity_tanker": to_int(r["capacity_tanker"]),
            "capacity_cargo": to_int(r["capacity_cargo"]),
        },
    },
    "spillover_port": {
        "table": "imf_spillover_port_impact",
        "wholesale": True,
        "fn": lambda r: {
            "from_portid": r["from_portid"], "from_portname": r["from_portname"],
            "from_country": r["from_country"], "from_iso3": r["from_iso3"],
            "from_lat": to_float(r["from_lat"]), "from_lon": to_float(r["from_lon"]),
            "to_portid": r["to_portid"], "to_portname": r["to_portname"],
            "to_country": r["to_country"], "to_iso3": r["to_iso3"],
            "to_lat": to_float(r["to_lat"]), "to_lon": to_float(r["to_lon"]),
            "average_transit_days": to_float(r["average_transit_days"]),
            "daily_capacity_at_risk": to_float(r["daily_capacity_at_risk"]),
            "relative_capacity_at_risk": to_float(r["relative_capacity_at_risk"]),
        },
    },
    "spillover_country_trade": {
        "table": "imf_spillover_country_trade_impact",
        "wholesale": True,
        "fn": lambda r: {
            "from_portid": r["from_portid"], "from_portname": r["from_portname"],
            "from_country": r["from_country"], "from_iso3": r["from_iso3"],
            "from_lat": to_float(r["from_lat"]), "from_lon": to_float(r["from_lon"]),
            "to_country": r["to_country"], "to_iso3": r["to_iso3"],
            "to_lat": to_float(r["to_lat"]), "to_lon": to_float(r["to_lon"]),
            "industry": r["industry"], "hs_section": r["hs_section"],
            "unit": r["unit"], "scale": r["scale"],
            "daily_export_value_at_risk": to_float(r["daily_export_value_at_risk"]),
            "daily_import_value_at_risk": to_float(r["daily_import_value_at_risk"]),
        },
    },
    "spillover_supplychain": {
        "table": "imf_spillover_supplychain_impact",
        "wholesale": True,
        "fn": lambda r: {
            "from_portid": r["from_portid"], "from_portname": r["from_portname"],
            "from_country": r["from_country"], "from_iso3": r["from_iso3"],
            "from_lat": to_float(r["from_lat"]), "from_lon": to_float(r["from_lon"]),
            "to_country": r["to_country"], "to_iso3": r["to_iso3"],
            "to_lat": to_float(r["to_lat"]), "to_lon": to_float(r["to_lon"]),
            "industry": r["industry"], "hs_section": r["hs_section"],
            "unit": r["unit"], "scale": r["scale"],
            "daily_consumption_at_risk": to_float(r["daily_consumption_at_risk"]),
            "daily_industryoutput_at_risk": to_float(r["daily_industryoutput_at_risk"]),
        },
    },
    "climate_port_risk": {
        "table": "imf_climate_port_risk",
        "wholesale": True,
        "fn": lambda r: {
            "portid": r["portid"], "portname": r["portname"],
            "country": r["country"], "iso3": r["ISO3"],
            "lat": to_float(r["lat"]), "lon": to_float(r["lon"]),
            "scenario": r["scenario"], "unit": r["unit"],
            "measure": r["measure"], "value": to_float(r["value"]),
            "hazard": r["hazard"],
        },
    },
    "climate_trade_risk": {
        "table": "imf_climate_trade_risk",
        "wholesale": True,
        "fn": lambda r: {
            "from_country": r["from_country"], "from_iso3": r["from_ISO3"],
            "to_portid": r["to_portid"], "to_portname": r["to_portname"],
            "to_country": r["to_country"], "to_iso3": r["to_ISO3"],
            "to_lat": to_float(r["to_lat"]), "to_lon": to_float(r["to_lon"]),
            "scenario": r["scenario"], "flow": r["flow"],
            "industry": r["industry"], "hs_section": r["hs_section"],
            "rank": to_float(r["rank"]), "days_downtime_at_port": to_float(r["days_downtime_at_port"]),
            "trade_value_at_risk": to_float(r["trade_value_at_risk"]), "unit": r["unit"],
        },
    },
}


def main(dataset, csv_path):
    if dataset not in TRANSFORMS:
        raise SystemExit(f"Unknown dataset '{dataset}'. Valid: {', '.join(TRANSFORMS)}")
    spec = TRANSFORMS[dataset]
    table = spec["table"]
    fn = spec["fn"]
    on_conflict = spec.get("on_conflict")
    wholesale = spec.get("wholesale", False)

    if wholesale:
        print(f"Deleting existing rows from {table}...")
        supabase_delete_all(table)

    total = 0
    batch = []
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            batch.append(fn(row))
            if len(batch) >= BATCH_SIZE:
                supabase_upsert_batch(table, batch, on_conflict=on_conflict)
                total += len(batch)
                print(f"  {dataset}: {total} rows so far...", flush=True)
                batch = []
        if batch:
            supabase_upsert_batch(table, batch, on_conflict=on_conflict)
            total += len(batch)

    print(f"{dataset}: wrote {total} rows from {csv_path}")
    log_run(dataset, total, status="ok", notes=f"loaded from local CSV: {os.path.basename(csv_path)}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: py scripts/load_csv.py <dataset> <csv_path>")
    main(sys.argv[1], sys.argv[2])
