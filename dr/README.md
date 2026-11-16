# Disaster Recovery (DR) — Aurora PostgreSQL Parquet Backups & Local Restore

This directory provides automated Disaster Recovery (DR) backups for all active Aurora PostgreSQL tables in **Reality Check**, exported as compact, column-oriented **Apache Parquet** files.

If AWS resources are accidentally terminated or inaccessible, the entire database state can be restored in seconds to any PostgreSQL instance (including a local Docker Compose PostgreSQL 16 container with `pgvector`).

---

## 1. Directory Structure

```
dr/
├── data/
│   ├── daily_cost.parquet          # 29 daily cost snapshots (compressed ZSTD)
│   ├── resource_inventory.parquet  # 72 active cloud resources
│   ├── __cdc_metadata.parquet      # CDC LSN & watermark replication metadata
│   └── manifest.json               # Export timestamps, row counts, checksums
├── docker-compose.yml              # Local PostgreSQL 16 + pgvector container (port 5433)
├── export_aurora_to_parquet.py     # Live extractor from Aurora / DuckDB reader -> Parquet
├── restore_parquet_to_postgres.py  # Importer from Parquet -> PostgreSQL
└── verify_dr.sh                    # End-to-end automated verification script
```

---

## 2. Quickstart: Restore to Local Docker PostgreSQL (Disaster Recovery)

To restore all data locally in a fresh PostgreSQL instance:

```bash
# 1. Start the local PostgreSQL 16 container with pgvector:
docker compose -f dr/docker-compose.yml up -d --wait

# 2. Restore all tables and verify row counts:
python3 dr/restore_parquet_to_postgres.py --port 5433

# 3. Connect and query locally:
psql -h localhost -p 5433 -U postgres -d postgres -c "SELECT count(*), sum(gross_usd) FROM daily_cost;"
```

To run the complete automated test (start container -> import -> verify counts -> teardown):
```bash
./dr/verify_dr.sh
```

---

## 3. Restoring to a Remote or New AWS Aurora Instance

To restore the Parquet files to a new PostgreSQL server (e.g., a replacement Aurora cluster):

```bash
python3 dr/restore_parquet_to_postgres.py \
  --host <NEW_AURORA_ENDPOINT> \
  --port 5432 \
  --dbname postgres \
  --user postgres \
  --password <PASSWORD_OR_IAM_TOKEN>
```

---

## 4. Re-Exporting Latest Data from Aurora

To refresh the Parquet backups from the live serverless query endpoint:

```bash
python3 dr/export_aurora_to_parquet.py
```
This updates `dr/data/*.parquet` and `dr/data/manifest.json`. Commit the updated Parquet files to GitHub to maintain version-controlled database history.
