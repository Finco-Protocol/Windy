#!/usr/bin/env python3
"""PR#8 Correction A §3 — import the owner's 26 GPS/altitude records into a
Windy DB as ONE append-only location revision.

Run BY THE OWNER against the production DB after the merge (this PR never
writes any production database):

    python scripts/import_krnovo_locations.py            # default db, krnovo
    python scripts/import_krnovo_locations.py --db /srv/windy/data/windy.db

Data source: the committed package backend/app/data/krnovo_turbine_locations.json
converted verbatim from the owner workbook 'Geographical coordinates & altitude.xlsx'
(DMS -> decimal WGS84). unit_id mapping WTG-001..026 is PROVISIONAL by owner row
order. Previous location revisions remain queryable; a re-import creates the
next revision (never rewrites history).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.siteconfig import import_owner_locations  # noqa: E402
from app.store import ensure_db  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(BACKEND / "data" / "windy.db"), help="Windy SQLite path")
    parser.add_argument("--site", default="krnovo", help="site id")
    args = parser.parse_args()

    ensure_db(args.db)
    out = import_owner_locations(args.db, args.site)
    print(f"site={out['site_id']} revision={out['revision']} records={out['n_locations']} source={out['source_file']}")
    for loc in out["locations"][:5]:
        print(f"  {loc['unit_id']}: {loc['latitude']:.9f}, {loc['longitude']:.9f}, {loc['elevation_m']} m ASL ({loc['source']})")
    if out["n_locations"] > 5:
        print(f"  … +{out['n_locations'] - 5} more")
    print("OK — previous revisions remain queryable; run again only to create a new revision.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
