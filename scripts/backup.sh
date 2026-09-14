#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p backups
stamp=$(date +%F_%H-%M-%S)
docker compose exec -T app python -c "import sqlite3; s=sqlite3.connect('/data/app.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d); d.close(); s.close()"
docker compose cp app:/data/backup.db "backups/app_${stamp}.db"
docker compose exec -T app rm -f /data/backup.db
docker compose cp app:/data/uploads "backups/uploads_${stamp}"
tar -C backups -czf "backups/practical-tasks_${stamp}.tar.gz" "app_${stamp}.db" "uploads_${stamp}"
rm -rf "backups/app_${stamp}.db" "backups/uploads_${stamp}"
find backups -name 'practical-tasks_*.tar.gz' -mtime +30 -delete
echo "Created backups/practical-tasks_${stamp}.tar.gz"
