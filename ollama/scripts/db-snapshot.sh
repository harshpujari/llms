#!/usr/bin/env bash
# Copies the library database out of Docker so it can be browsed.
#
#   ./scripts/db-snapshot.sh     write snapshots/library.db and open it in VS Code
#
# The live database lives in the library-data volume, not on this disk (WAL
# locking over a macOS bind mount is unreliable -- see DockerCompse.yml), so a
# viewer can't open it in place. This takes a consistent copy with SQLite's
# backup API, safe while the workers are writing. It is a snapshot: rerun it to
# see newer data.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

COMPOSE=(docker compose -f DockerCompse.yml)
OUT=snapshots/library.db
TMP=/tmp/library-snapshot.db

mkdir -p snapshots
# Snapshots are copies of your documents' text: never commit them.
[[ -f snapshots/.gitignore ]] || echo '*' > snapshots/.gitignore

"${COMPOSE[@]}" exec -T api python -c "
import os, sqlite3
src = sqlite3.connect(os.getenv('LIBRARY_DB', '/app/data/library.db'))
dst = sqlite3.connect('$TMP')
src.backup(dst)
dst.close()
src.close()
"
"${COMPOSE[@]}" cp "api:$TMP" "$OUT" >/dev/null
"${COMPOSE[@]}" exec -T api rm -f "$TMP"
echo "snapshot written: $OUT"

if command -v code >/dev/null; then
  code "$OUT"
fi
