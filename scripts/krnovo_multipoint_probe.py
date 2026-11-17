#!/usr/bin/env python3
"""PR#8 Correction A §3 — bounded multi-point pilot for the 26 owner GPS
coordinates against the FREE public Open-Meteo endpoint.

Answers two questions with ONE (or two) no-cost API request(s):
1. Do the 26 coordinates produce genuinely DISTINCT hub-relevant wind values
   within one model, or is the farm flat (single-point query justified)?
2. What is the actual extra API call volume for per-turbine querying
   (multi-location batching: N coordinates in ONE request)?

Run:  python scripts/krnovo_multipoint_probe.py [--days 1]
No API key, no paid tier, no production writes. Read-only evidence for the
owner report; results are printed, never persisted into the archive.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import urllib.request
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.siteconfig import load_owner_locations_package  # noqa: E402


def probe(models: str, hourly: str, days: int, lat_csv: str, lon_csv: str, timeout_s: int = 45) -> dict:
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat_csv}&longitude={lon_csv}&hourly={hourly}"
        f"&models={models}&wind_speed_unit=ms&forecast_days={days}"
    )
    with urllib.request.urlopen(url, timeout=timeout_s) as res:
        return json.loads(res.read().decode("utf-8"))


def summarize(model: str, payload: dict, elevs: list[float]) -> dict:
    items = payload if isinstance(payload, list) else [payload]
    per_loc_mean: list[float] = []
    for loc in items:
        ws = (loc.get("hourly") or {}).get("wind_speed_100m") or (loc.get("hourly") or {}).get("wind_speed_80m") or []
        vals = [v for v in ws if v is not None]
        if not vals:
            return {"model": model, "error": "no wind values returned"}
        per_loc_mean.append(statistics.fmean(vals))
    spread = max(per_loc_mean) - min(per_loc_mean)
    distinct = len({round(v, 2) for v in per_loc_mean})
    return {
        "model": model,
        "locations": len(items),
        "mean_wind_ms_range": [round(min(per_loc_mean), 2), round(max(per_loc_mean), 2)],
        "farm_spread_ms": round(spread, 2),
        "distinct_values_rounded": distinct,
        "correlation_with_elevation_note": "hub wind vs turbine ground elevation",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=1)
    args = parser.parse_args()

    pkg = load_owner_locations_package()
    recs = pkg["records"]
    lat_csv = ",".join(f"{r['latitude']:.9f}" for r in recs)
    lon_csv = ",".join(f"{r['longitude']:.9f}" for r in recs)
    elevs = [r["elevation_m"] for r in recs]
    print(f"coordinates: {len(recs)} (owner workbook, WTG mapping provisional)")

    results = []
    # ONE request covers all 26 locations (multi-location batching)
    r1 = probe("gfs_seamless", "wind_speed_80m,wind_speed_100m", args.days, lat_csv, lon_csv)
    results.append(summarize("gfs_seamless", r1, elevs))
    r2 = probe("ecmwf_ifs", "wind_speed_100m", args.days, lat_csv, lon_csv)
    results.append(summarize("ecmwf_ifs", r2, elevs))

    print(json.dumps(results, indent=1))
    print(
        "\nAPI volume: 2 requests total for 26 coordinates x "
        f"{args.days} day(s) (multi-location batching) — 26 single-point queries would cost 26 requests per model."
    )
    print("Interpretation: wind_speed differences across the ~5 km site are model-resolution dependent;")
    print("the production forecast still uses the SINGLE farm-centroid point until per-turbine wind is")
    print("implemented and numerically validated — declared PARTIAL in the owner report.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
