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
-- Columns: net spend (unblended/blended/net), GROSS pre-credit spend
-- (gross_usd/gross_services) which is the real consumption on a credit-covered
-- account, the credit offset, and a daily credit-balance + runway snapshot.
CREATE TABLE IF NOT EXISTS daily_cost (
    usage_date            date PRIMARY KEY,
    unblended_usd         numeric(14,6) NOT NULL,
    blended_usd           numeric(14,6) NOT NULL,
    net_usd               numeric(14,6) NOT NULL,
    currency              text          NOT NULL,
    services              jsonb         NOT NULL,
    gross_usd             numeric(14,6),
    credit_usd            numeric(14,6),
    gross_services        jsonb,
    plan_type             text,
    credits_granted_usd   numeric(14,2),
    credits_remaining_usd numeric(14,2),
    credits_consumed_usd  numeric(14,2),
    credits_expiry        date,
    projection            jsonb,
    collected_at          timestamptz   NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE ON daily_cost TO cost_collector;

-- Active-resource inventory: every taggable resource plus creation time where
-- discoverable. first_seen/last_seen accumulate a running-time history across
-- runs even when AWS does not expose a creation time.
CREATE TABLE IF NOT EXISTS resource_inventory (
    arn            text PRIMARY KEY,
    service        text        NOT NULL,
    resource_type  text        NOT NULL,
    region         text        NOT NULL,
    created_at     timestamptz,
    first_seen     timestamptz NOT NULL DEFAULT now(),
    last_seen      timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE ON resource_inventory TO cost_collector;

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
GRANT SELECT ON resource_inventory TO cost_reader;

-- -----------------------------------------------------------------------------
-- pgvector evidence store for semantic retrieval during contract review.
-- Embeddings: Amazon Titan Text Embeddings v2 → 1024-dim vectors.
-- Design + rationale: docs/DESIGN_pgvector_evidence_retrieval.md
-- (pgvector is enabled as `vector` near the top of this file.)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS evidence_chunk (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_ref    text         NOT NULL,          -- "B1","B2",… maps to the review's evidence_ref
    source_name   text         NOT NULL,          -- original doc filename/title
    chunk_no      int          NOT NULL,          -- ordinal within the doc
    passage       text         NOT NULL,          -- chunk text = the citation shown to the user
    last_updated  date,                           -- doc freshness (already tracked per source)
    embedding     vector(1024) NOT NULL,
    created_at    timestamptz  NOT NULL DEFAULT now(),
    UNIQUE (source_ref, chunk_no)
);

-- Cosine-distance ANN index (embeddings are L2-normalized → cosine is correct).
-- HNSW needs no training step and gives strong recall on a small/medium corpus
-- that grows incrementally. See the design doc for the HNSW-vs-IVFFlat rationale.
CREATE INDEX IF NOT EXISTS evidence_chunk_embedding_hnsw
    ON evidence_chunk USING hnsw (embedding vector_cosine_ops);

-- WRITER role: the one-time ingest path (agent/ingest_evidence.py) embeds the
-- corpus and replaces rows. INSERT/DELETE/SELECT only, IAM-auth only.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'evidence_writer') THEN
        CREATE ROLE evidence_writer WITH LOGIN;
    END IF;
END
$$;
GRANT rds_iam TO evidence_writer;
GRANT CONNECT ON DATABASE postgres TO evidence_writer;
GRANT USAGE ON SCHEMA public TO evidence_writer;
GRANT SELECT, INSERT, DELETE ON evidence_chunk TO evidence_writer;

-- READER role: the review Lambda. SELECT-only on evidence_chunk — it can query
-- for similar passages but never write, and can see nothing else.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'evidence_reader') THEN
        CREATE ROLE evidence_reader WITH LOGIN;
    END IF;
END
$$;
GRANT rds_iam TO evidence_reader;
GRANT CONNECT ON DATABASE postgres TO evidence_reader;
GRANT USAGE ON SCHEMA public TO evidence_reader;
GRANT SELECT ON evidence_chunk TO evidence_reader;
