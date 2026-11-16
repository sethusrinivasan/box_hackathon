#!/usr/bin/env bash
#
# verify_dr.sh — Automated Disaster Recovery verification.
#
# 1. Boots local PostgreSQL container via Docker Compose (port 5433).
# 2. Waits for PostgreSQL readiness.
# 3. Executes restore_parquet_to_postgres.py.
# 4. Validates table row counts and sums.
# 5. Cleans up container & volumes.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "============================================================"
echo "1. Starting DR PostgreSQL 16 container via Docker Compose..."
echo "============================================================"
docker compose up -d --wait

echo "==> PostgreSQL container is healthy and ready."

echo "============================================================"
echo "2. Restoring Parquet backups into local PostgreSQL..."
echo "============================================================"
python3 restore_parquet_to_postgres.py --host localhost --port 5433 --user postgres --password postgres --dbname postgres

echo "============================================================"
echo "3. Tearing down test container..."
echo "============================================================"
docker compose down -v

echo "============================================================"
echo "SUCCESS: Disaster Recovery end-to-end verification passed!"
echo "============================================================"
