"""Windy CLI — collection, backfill and DB utilities.

    python -m app.cli init-db
    python -m app.cli collect [--days 10]
    python -m app.cli backfill --model-id gfs_seamless --run-date 2026-11-14 --run-hour 0
    python -m app.cli fetch-curve
    python -m app.cli schedule        # blocking twice-daily scheduler
"""

from __future__ import annotations

import argparse
import json


def main() -> None:
    parser = argparse.ArgumentParser(prog="windy", description="Windy wind-farm forecast collector")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="create schema and seed the default asset/curve")
    p_collect = sub.add_parser("collect", help="collect the newest run of every model now")
    p_collect.add_argument("--days", type=int, default=None, help="forecast days (1-16)")
    p_backfill = sub.add_parser("backfill", help="backfill ONE archived run from the Single Runs API")
    p_backfill.add_argument("--model-id", required=True)
    p_backfill.add_argument("--run-date", required=True, help="YYYY-MM-DD (UTC)")
    p_backfill.add_argument("--run-hour", type=int, default=0, choices=[0, 6, 12, 18])
    p_backfill.add_argument("--days", type=int, default=10)
    sub.add_parser("fetch-curve", help="print the configured power-curve provenance")
    sub.add_parser("schedule", help="run the blocking twice-daily scheduler")

    args = parser.parse_args()
    from .config import Settings
    from .openmeteo import OMError, OpenMeteoClient
    from .store import ensure_db, load_site_and_curve

    settings = Settings()

    if args.cmd == "init-db":
        ensure_db(settings.db_path)
        print(f"db ready: {settings.db_path}")
    elif args.cmd == "collect":
        ensure_db(settings.db_path)
        from .collect import collect_site

        client = OpenMeteoClient(timeout_s=settings.om_timeout_s, max_retries=settings.om_max_retries)
        result = collect_site(settings.db_path, client, "krnovo", days=args.days)
        print(json.dumps(result, indent=2))
        client.close()
    elif args.cmd == "backfill":
        ensure_db(settings.db_path)
        from .backfill import ModelRunUnavailable, run_backfill

        try:
            print(json.dumps(run_backfill(settings, args.model_id, args.run_date, args.run_hour, args.days), indent=2))
        except ModelRunUnavailable as exc:
            print(f"UNAVAILABLE: {exc}")
            raise SystemExit(2)
        except OMError as exc:
            print(f"ERROR: {exc}")
            raise SystemExit(2)
    elif args.cmd == "fetch-curve":
        ensure_db(settings.db_path)
        site, curve = load_site_and_curve(settings.db_path, "krnovo")
        print(json.dumps({
            "id": curve.id,
            "label": curve.label,
            "status": curve.status,
            "source": curve.source,
            "provenance": curve.provenance,
            "rated_power_mw": curve.rated_power_mw,
            "cut_in_mps": curve.cut_in_mps,
            "rated_mps": curve.rated_mps,
            "cut_out_mps": curve.cut_out_mps,
            "n_points": len(curve.points),
        }, indent=2))
    elif args.cmd == "schedule":
        ensure_db(settings.db_path)
        from .scheduler import run_blocking

        run_blocking(settings)


if __name__ == "__main__":
    main()
