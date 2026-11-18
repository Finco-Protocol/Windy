#!/usr/bin/env bash
# Windy restore — copies a backup into a DISPOSABLE directory and verifies it
# before it can replace anything live. Never restores over the live DB
# in-place; the operator swaps files deliberately after verification.
#
#   ./restore.sh /srv/windy/backups/windy-20261116T120000Z.db /tmp/windy-restore-check
set -euo pipefail

BACKUP="${1:?usage: restore.sh <backup.db> <verify-dir> [live-db-dir]}"
VERIFY_DIR="${2:?usage: restore.sh <backup.db> <verify-dir>}"
LIVE_DIR="${3:-}"

mkdir -p "$VERIFY_DIR"
cp "$BACKUP" "$VERIFY_DIR/windy.db"

# integrity check on the restored copy
python3 - "$VERIFY_DIR/windy.db" << 'EOF'
import sys
import sqlite3

conn = sqlite3.connect(sys.argv[1])
ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
assets = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
runs = conn.execute("SELECT COUNT(*) FROM forecast_runs").fetchone()[0]
print(f"integrity: {ok} | schema_version: {version} | assets: {assets} | forecast_runs: {runs}")
if ok != "ok":
    sys.exit("RESTORED COPY FAILED INTEGRITY CHECK")
print("verify OK")
EOF

echo "verified copy: $VERIFY_DIR/windy.db"
if [ -n "$LIVE_DIR" ]; then
  echo "TO PROMOTE (manual, stops the app first):"
  echo "  1. docker compose -f compose.prod.yml stop windy"
  echo "  2. cp $VERIFY_DIR/windy.db $LIVE_DIR/windy.db && rm -f $LIVE_DIR/windy.db-wal $LIVE_DIR/windy.db-shm"
  echo "  3. docker compose -f compose.prod.yml start windy"
fi
