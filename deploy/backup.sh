#!/usr/bin/env bash
# Windy SQLite backup — uses the SQLite online backup API (safe while WAL
# writes are in flight). Run ON THE SERVER against the live data directory.
#
#   ./backup.sh /srv/windy/data/windy.db /srv/windy/backups
#
# Never commit or upload the backup file; retain it on private server storage.
set -euo pipefail

DB_PATH="${1:?usage: backup.sh <db-path> <backup-dir>}"
BACKUP_DIR="${2:?usage: backup.sh <db-path> <backup-dir>}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="$BACKUP_DIR/windy-$STAMP.db"

mkdir -p "$BACKUP_DIR"
python3 - "$DB_PATH" "$TARGET" << 'EOF'
import sys
import sqlite3

src, dst = sys.argv[1], sys.argv[2]
src_conn = sqlite3.connect(src)
dst_conn = sqlite3.connect(dst)
src_conn.backup(dst_conn)  # online backup API — consistent under WAL
dst_conn.close()
src_conn.close()
print("backup written:", dst)
EOF

# retain the last 14 backups
ls -1t "$BACKUP_DIR"/windy-*.db | tail -n +15 | xargs -r rm -f
echo "backups retained: $(ls -1 "$BACKUP_DIR"/windy-*.db | wc -l)"
