# Design: pgvector-backed evidence retrieval for contract review

Status: **Proposed** (schema + extension provisioned; retrieval not yet wired in)
Owner: Reality Check
Scope: `agent/local_agent.py` review flow + Aurora Serverless v2 (`reality-check-cost-tracker`)

---

## 1. Problem

The review agent (`agent/local_agent.py`) checks each customer-facing **claim** in
a contract against an **evidence corpus** (the `B1, B2, …` source documents:
solution guides, product roadmaps, delivery/benchmark reviews).

Today retrieval is **whole-corpus prompt stuffing**: for every review,
`review_contract()` reads *all* evidence documents and concatenates their full
text into the Bedrock prompt, then relies on the model to locate relevant
passages:

```python
# agent/local_agent.py (current)
for i, e in enumerate(docs.evidence(), 1):
    text = pdf_text(docs.read(e["id"]))
    sources.append({**e, "ref": f"B{i}", "text": text, ...})   # full text, every doc
```

This is fine for the 3 demo documents but does not scale:

- **Context-window ceiling** — a realistic corpus (dozens–hundreds of policy,
  roadmap, and benchmark docs) will not fit in a single prompt.
- **Cost** — every review pays input tokens for the *entire* corpus, regardless
  of relevance. Bedrock input tokens are already a tracked cost driver on the
  observability dashboard.
- **Precision** — the model must find the needle itself inside a large haystack,
  which degrades as the corpus grows.

## 2. Why pgvector, and why here

The database already exists (Aurora Serverless v2 PostgreSQL,
`reality-check-cost-tracker`), the `vector` extension is enabled (pgvector
**0.8.1**, `hnsw` + `ivfflat` index methods available), and the app already
connects over native PG with IAM auth. Adding semantic retrieval needs **no new
infrastructure**.

The matching problem is inherently **semantic**, not lexical: a claim such as
*"99.9% uptime in eu-west-1"* must match an evidence passage about *"availability
SLA, Ireland region"* that shares no keywords. Embedding + vector similarity does
this; `LIKE`/full-text search does not.

Decision: **do not** add vectors to the existing `daily_cost` table (pure numeric
time-series — no semantic retrieval use case). The showcase is the evidence
corpus.

## 3. Design

### 3.1 Data flow

```
INGEST (once per evidence doc, or on change)
  evidence PDF ──► chunk (≈500 tokens, overlap) ──► Titan embed ──► INSERT evidence_chunk(embedding, passage, source_ref, …)

REVIEW (per extracted claim)
  claim text ──► Titan embed ──► SELECT … ORDER BY embedding <=> :q LIMIT k ──► top-k passages ──► Bedrock review prompt
```

Only the top-k passages (k≈5) enter the prompt instead of the whole corpus.

### 3.2 Embedding model

Amazon **Titan Text Embeddings v2** (`amazon.titan-embed-text-v2:0`), available in
`us-east-2`. Output **1024 dims** (the model also supports 512/256 for a
size/quality trade-off). Chosen for the Amazon-native, cost-conscious posture
already used elsewhere in the project; `cohere.embed-v4:0` is a drop-in
alternative if higher recall is needed.

Embeddings are **L2-normalized**, so cosine distance is the correct metric
(`vector_cosine_ops` / the `<=>` operator).

### 3.3 Schema

```sql
CREATE EXTENSION IF NOT EXISTS vector;          -- provisioned via schema.sql

CREATE TABLE IF NOT EXISTS evidence_chunk (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_ref    text         NOT NULL,         -- "B1","B2",… → maps to review's evidence_ref
    source_name   text         NOT NULL,         -- original doc filename/title
    chunk_no      int          NOT NULL,         -- ordinal within the doc
    passage       text         NOT NULL,         -- chunk text = the citation shown to the user
    last_updated  date,                          -- doc freshness (already tracked per source)
    embedding     vector(1024) NOT NULL,
    created_at    timestamptz  NOT NULL DEFAULT now(),
    UNIQUE (source_ref, chunk_no)
);

-- Approximate-nearest-neighbour index (cosine). HNSW: no training step, strong
-- recall on a small/medium corpus that grows incrementally.
CREATE INDEX IF NOT EXISTS evidence_chunk_embedding_hnsw
    ON evidence_chunk USING hnsw (embedding vector_cosine_ops);
```

### 3.4 Retrieval query (per claim)

```sql
SELECT source_ref, source_name, passage, last_updated,
       1 - (embedding <=> %(claim_vec)s) AS similarity   -- <=> = cosine distance
FROM   evidence_chunk
ORDER  BY embedding <=> %(claim_vec)s
LIMIT  %(k)s;                                            -- k ≈ 5
```

The returned `source_ref` / `passage` map directly onto the review's existing
`evidence_ref` / `evidence_passage` output fields, so citations stay grounded and
the downstream report format is unchanged.

### 3.5 Index choice: HNSW vs IVFFlat

Both are available on pgvector 0.8.1. **HNSW** is the default here: it needs no
training/rebuild step and gives strong recall on a corpus that grows
incrementally (new evidence docs appended over time). IVFFlat only pays off at
much larger scale and requires a populated-table training step and periodic
`REINDEX`, which is operational overhead this workload does not justify yet.

### 3.6 Integration point

`review_contract()` changes from "attach every evidence doc" to:
1. embed the claim text (one Titan call, cacheable per claim),
2. run the retrieval query to get top-k passages,
3. build `sources` from those passages only.

Ingestion is a **separate one-time path** (a small script or the collector-style
Lambda) that chunks + embeds evidence docs and upserts rows — mirroring the
collector/reader split already in the cost stack.

## 4. Security / least privilege

Follows the existing IAM-auth, no-stored-secret posture:

| Principal | DB grant | AWS grant |
|-----------|----------|-----------|
| Review Lambda (query) | `SELECT` on `evidence_chunk` | `rds-db:connect` (scoped db user + account/region condition), `bedrock:InvokeModel` on the Titan model |
| Ingest path (write) | `INSERT`/`UPDATE` on `evidence_chunk` | `rds-db:connect` (own db user), `bedrock:InvokeModel` |

`CREATE EXTENSION vector` runs once as the admin bootstrap user
(`agent/cost_lambda/schema.sql`); runtime roles get **no** extension-creation
rights. The `rds-db:connect` grants carry `aws:ResourceAccount` /
`aws:RequestedRegion` conditions (rds-db exposes no tag/service condition keys),
so a connection is only ever allowed to this account/region's cluster.

## 5. Cost & performance notes

- **Token cost drops** from O(whole corpus) to O(top-k) per review; the saving
  grows with corpus size and is visible on the Bedrock-token dashboard widget.
- **Embedding cost** is one Titan call per claim (and once per chunk at ingest).
  Titan v2 is inexpensive; claim embeddings can be cached.
- **Latency** — HNSW ANN lookup on a small/medium corpus is sub-millisecond;
  the dominant cost remains the Bedrock review call itself.
- Aurora Serverless v2 **auto-pause** still applies; the first query after idle
  pays the cold-resume penalty already tracked by the DB-resume dashboard row.

## 6. Status / next steps

- [x] pgvector extension enabled on the live cluster (0.8.1) and in `schema.sql`.
- [ ] Add the `evidence_chunk` table + HNSW index to `schema.sql`.
- [ ] Ingest path: chunk + Titan-embed the evidence corpus → upsert rows.
- [ ] Wire top-k retrieval into `review_contract()`; add `bedrock:InvokeModel`
      (Titan) to the review role.
- [ ] Backfill/measure token reduction vs. the whole-corpus baseline.

*Grounded in live inspection of the cluster and the current
`agent/local_agent.py` review flow.*
