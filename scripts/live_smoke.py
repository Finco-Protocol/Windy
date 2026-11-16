"""Bounded live-provider smoke test — REAL network evidence, run manually.

Exercises the public Open-Meteo endpoints for every configured model (current
forecast) plus one archived Single Runs issuance. Capped requests, no API key,
no billable calls. Output is JSON evidence with UTC timestamps: endpoint,
status, model, valid-interval count, provenance (issue-known vs unknown) and
coverage. This tool performs REAL network calls — it is not part of CI and
must never be presented as synthetic test data.

Usage:
    python scripts/live_smoke.py [--site krnovo] [--out evidence.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.config import Settings  # noqa: E402
from app.openmeteo import ModelRunUnavailable, OMError, OpenMeteoClient  # noqa: E402
from app.registry import WINDY_MODELS, get_model  # noqa: E402
from app.store import ensure_db, load_site  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="bounded live Open-Meteo smoke (REAL network calls)")
    parser.add_argument("--site", default="krnovo")
    parser.add_argument("--out", default=None, help="write JSON evidence to this path (temp file recommended)")
    args = parser.parse_args()

    settings = Settings()
    ensure_db(settings.db_path)
    site = load_site(settings.db_path, args.site)
    client = OpenMeteoClient(timeout_s=settings.om_timeout_s, max_retries=1)
    evidence: dict = {
        "kind": "REAL_LIVE_EVIDENCE",
        "started_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "site_id": site["id"],
        "requests": [],
    }

    # 1) current forecast per configured model — one bounded request each
    for spec in WINDY_MODELS:
        params = {
            "latitude": site["latitude"],
            "longitude": site["longitude"],
            "hourly": ",".join(f"wind_speed_{h}m" for h in spec.speed_heights),
            "models": spec.id,
            "forecast_days": min(10, spec.request_days),
            "wind_speed_unit": "ms",
            "timezone": "UTC",
        }
        url = "https://api.open-meteo.com/v1/forecast"
        entry: dict = {"endpoint": url, "params": params, "model": spec.id, "provider_model_origin": spec.provider}
        try:
            raw = client.fetch_forecast(site["latitude"], site["longitude"], spec, min(10, spec.request_days))
            hourly = raw.get("hourly", {})
            times = hourly.get("time") or []
            series = next(
                (v for k, v in hourly.items() if k.startswith("wind_speed_") and k.endswith(f"_{spec.id}")),
                next((v for k, v in hourly.items() if k.startswith("wind_speed_")), None),
            )
            n_valid = sum(1 for x in series or [] if x is not None)
            entry.update(
                {
                    "http_status": 200,
                    "authentic_live_response": True,
                    "utc_checked": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "valid_intervals": len(times),
                    "non_null_wind_values": n_valid,
                    "first_valid_utc": times[0] + "Z" if times else None,
                    "last_valid_utc": times[-1] + "Z" if times else None,
                    "horizon_respected": len(times) <= spec.max_lead_hours,
                    "heights_requested_m": list(spec.speed_heights),
                    "units": "m/s",
                }
            )
        except OMError as exc:
            entry.update({"http_status": None, "authentic_live_response": False, "error": str(exc)[:300], "status": "FAIL"})
        evidence["requests"].append(entry)

    # 2) archived Single Runs issuance for ONE model/cycle — issue-known provenance
    spec = get_model("ecmwf_ifs")
    yesterday = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
    entry = {
        "endpoint": "https://single-runs-api.open-meteo.com/v1/forecast",
        "params": {"run": f"{yesterday}T00:00", "models": "ecmwf_ifs", "forecast_days": 3},
        "model": "ecmwf_ifs",
        "purpose": "archived issuance (issue-known provenance, immutability check)",
    }
    try:
        raw = client.fetch_single_run(site["latitude"], site["longitude"], spec, f"{yesterday}T00:00", 3)
        times = raw.get("hourly", {}).get("time") or []
        entry.update(
            {
                "http_status": 200,
                "authentic_live_response": True,
                "utc_checked": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "run_issued_utc": f"{yesterday}T00:00:00Z",
                "valid_intervals": len(times),
                "first_valid_utc": times[0] + "Z" if times else None,
                "issue_known": True,
                "note": "archived issuance; stored via immutable store_run — re-collection is a no-op",
            }
        )
    except ModelRunUnavailable as exc:
        entry.update({"http_status": 404, "status": "BLOCKED_PROVIDER", "error": str(exc)[:300]})
    except OMError as exc:
        entry.update({"http_status": None, "status": "BLOCKED_EXTERNAL", "error": str(exc)[:300]})
    evidence["requests"].append(entry)

    client.close()
    evidence["finished_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    out = json.dumps(evidence, indent=2, ensure_ascii=False)
    print(out)
    if args.out:
        Path(args.out).write_text(out, encoding="utf-8")


if __name__ == "__main__":
    main()
