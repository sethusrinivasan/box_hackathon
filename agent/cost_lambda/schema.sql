-- One-time bootstrap for the cost-tracker database.
--
-- Express configuration creates the admin user (default "postgres") with IAM
-- authentication already enabled. Run this ONCE as that admin user, connecting
-- with an IAM auth token — NO password anywhere:
--
--   TOKEN="$(aws rds generate-db-auth-token \
--       --hostname <DbEndpoint> --port 5432 --username postgres --region us-east-2)"
--   PGPASSWORD="$TOKEN" \
--   psql "host=<DbEndpoint> port=5432 dbname=postgres user=postgres sslmode=require" \
--        -f agent/cost_lambda/schema.sql
--
-- This creates a DEDICATED least-privilege role `cost_collector` that the
-- collector Lambda uses at runtime (also via IAM tokens). The admin user is
-- never used by the Lambda.

-- -----------------------------------------------------------------------------
-- Extensions. pgvector ("vector") is enabled so the database can store and
-- query embeddings (vector similarity search) alongside the cost tables.
-- CREATE EXTENSION requires admin privileges, so it runs here as the `postgres`
-- bootstrap user — the runtime Lambda roles do not need (and are not granted)
-- extension-creation rights. Idempotent via IF NOT EXISTS.
-- Intended use case + schema design: docs/DESIGN_pgvector_evidence_retrieval.md
-- (semantic retrieval of contract-review evidence; not used by daily_cost).
-- -----------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS vector;

-- IAM-auth login: no password, authenticates only via IAM tokens.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cost_collector') THEN
        CREATE ROLE cost_collector WITH LOGIN;
    END IF;
END
$$;

-- This is what ties the Postgres role to AWS IAM database authentication.
GRANT rds_iam TO cost_collector;

-- The collector needs to create its table on first run and read/write it.
-- Express configuration's default database is "postgres".
GRANT CONNECT ON DATABASE postgres TO cost_collector;
GRANT USAGE, CREATE ON SCHEMA public TO cost_collector;

-- Table the collector upserts into (it also CREATE TABLE IF NOT EXISTS at
-- runtime, but we create it here so grants are explicit).
CREATE TABLE IF NOT EXISTS daily_cost (
    usage_date     date PRIMARY KEY,
    unblended_usd  numeric(14,6) NOT NULL,
    blended_usd    numeric(14,6) NOT NULL,
    net_usd        numeric(14,6) NOT NULL,
    currency       text          NOT NULL,
    services       jsonb         NOT NULL,
    collected_at   timestamptz   NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE ON daily_cost TO cost_collector;

-- Future tables in public also usable by the collector (defensive).
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE ON TABLES TO cost_collector;

-- -----------------------------------------------------------------------------
-- READ-ONLY role for the public "cloud cost" web view. IAM-auth only, SELECT
-- only on daily_cost — it can never write or see anything else. This backs the
-- public Function URL that renders the rolled-up daily costs.
-- -----------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cost_reader') THEN
        CREATE ROLE cost_reader WITH LOGIN;
    END IF;
END
$$;

GRANT rds_iam TO cost_reader;
GRANT CONNECT ON DATABASE postgres TO cost_reader;
GRANT USAGE ON SCHEMA public TO cost_reader;
GRANT SELECT ON daily_cost TO cost_reader;
