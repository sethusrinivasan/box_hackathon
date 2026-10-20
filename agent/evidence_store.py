"""
evidence_store.py — pgvector-backed semantic retrieval of review evidence.

Replaces "stuff every evidence doc into the prompt" with "embed the corpus once,
then retrieve only the top-k most relevant passages per review". Keeps Bedrock
input tokens (a tracked cost) bounded and lets the evidence corpus grow past the
context window.

Design: docs/DESIGN_pgvector_evidence_retrieval.md

Everything here is OPTIONAL and best-effort:
  * enabled only when EVIDENCE_STORE=on AND the DB env vars are set,
  * any failure (no DB, no pgvector, Bedrock error) degrades to "retrieval
    unavailable" so the caller transparently falls back to the full corpus.

Connectivity mirrors the cost Lambdas: native PostgreSQL via psycopg, IAM auth
token as the password (no stored secret), TLS verify-full against certifi.
Embeddings: Amazon Titan Text Embeddings v2 (1024-dim, normalized).
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

EMBED_MODEL = os.environ.get("EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0")
EMBED_DIMS = int(os.environ.get("EMBED_DIMS", "1024"))
REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_REGION_NAME", "us-east-1")

DB_HOST = os.environ.get("EVIDENCE_DB_HOST") or os.environ.get("DB_HOST")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_NAME = os.environ.get("DB_NAME", "postgres")
# Review path uses a read-only db user; ingest uses a writer. Default to the
# reader so the public review Lambda never needs write rights.
DB_USER = os.environ.get("EVIDENCE_DB_USER", "evidence_reader")

CHUNK_CHARS = int(os.environ.get("EVIDENCE_CHUNK_CHARS", "1200"))
CHUNK_OVERLAP = int(os.environ.get("EVIDENCE_CHUNK_OVERLAP", "200"))
TOP_K = int(os.environ.get("EVIDENCE_TOP_K", "6"))


def enabled() -> bool:
    """True only when explicitly switched on and a DB host is configured."""
    return (os.environ.get("EVIDENCE_STORE", "").strip().lower() in ("1", "true", "on", "yes")
            and bool(DB_HOST))


# --------------------------------------------------------------------------- #
# Chunking (pure, unit-testable, no deps)
# --------------------------------------------------------------------------- #
def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> List[str]:
    """Split text into overlapping chunks on paragraph/sentence boundaries where
    possible, falling back to a hard character window. Overlap preserves context
    that straddles a boundary so a claim's evidence isn't split away."""
    text = re.sub(r"[ \t]+", " ", (text or "").strip())
    if not text:
        return []
    if len(text) <= size:
        return [text]
    chunks: List[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            # prefer to break at a paragraph, then sentence, then space
            window = text[start:end]
            for sep in ("\n\n", ". ", "\n", " "):
                idx = window.rfind(sep)
                if idx > size * 0.5:          # don't break too early
                    end = start + idx + len(sep)
                    break
        chunks.append(text[start:end].strip())
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]


# --------------------------------------------------------------------------- #
# Lazy AWS / DB clients (importable without boto3/psycopg for unit tests)
# --------------------------------------------------------------------------- #
_clients: dict = {}


def _bedrock():
    if "bedrock" not in _clients:
        import boto3
        _clients["bedrock"] = boto3.client("bedrock-runtime", region_name=REGION)
    return _clients["bedrock"]


def _rds():
    if "rds" not in _clients:
        import boto3
        _clients["rds"] = boto3.client("rds", region_name=REGION)
    return _clients["rds"]


def embed(text: str) -> List[float]:
    """One Titan embedding (1024-dim, normalized) for a piece of text."""
    body = json.dumps({"inputText": text[:50000], "dimensions": EMBED_DIMS,
                       "normalize": True})
    resp = _bedrock().invoke_model(modelId=EMBED_MODEL, body=body)
    return json.loads(resp["body"].read())["embedding"]


def _connect(user: Optional[str] = None):
    """Native-PG connection with a fresh IAM auth token (no stored secret).
    Retries once to ride through an Aurora Serverless v2 cold resume."""
    import certifi
    import psycopg
    user = user or DB_USER
    last_exc: Optional[Exception] = None
    for _ in range(2):
        token = _rds().generate_db_auth_token(
            DBHostname=DB_HOST, Port=DB_PORT, DBUsername=user, Region=REGION)
        try:
            return psycopg.connect(
                host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=user,
                password=token, sslmode="verify-full", sslrootcert=certifi.where(),
                connect_timeout=20)
        except psycopg.OperationalError as exc:
            last_exc = exc  # likely resuming; retry once
    raise last_exc  # type: ignore[misc]


def _vec_literal(vec: List[float]) -> str:
    """pgvector text literal: '[0.1,0.2,...]'."""
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


# --------------------------------------------------------------------------- #
# Retrieval (used by the review path; read-only)
# --------------------------------------------------------------------------- #
def retrieve(query_text: str, k: int = TOP_K,
             min_similarity: float = 0.0) -> Optional[List[Dict[str, Any]]]:
    """Top-k evidence chunks most similar to query_text, grouped back into
    source documents. Returns None (not []) when retrieval is unavailable, so
    the caller can distinguish "nothing relevant" from "store not usable" and
    fall back to the full corpus.
    """
    if not enabled():
        return None
    try:
        qvec = _vec_literal(embed(query_text))
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT source_ref, source_name, passage, last_updated, "
                    "       1 - (embedding <=> %s::vector) AS similarity "
                    "FROM evidence_chunk "
                    "ORDER BY embedding <=> %s::vector "
                    "LIMIT %s;",
                    (qvec, qvec, k),
                )
                rows = cur.fetchall()
    except Exception:
        return None  # best-effort: any failure → caller falls back
    hits = [
        {"source_ref": r[0], "source_name": r[1], "passage": r[2],
         "last_updated": r[3].isoformat() if hasattr(r[3], "isoformat") else r[3],
         "similarity": round(float(r[4]), 4)}
        for r in rows if float(r[4]) >= min_similarity
    ]
    return hits or None


def retrieve_sources(query_text: str, k: int = TOP_K) -> Optional[List[Dict[str, Any]]]:
    """Retrieve top-k chunks and regroup them into per-source blocks shaped like
    the review flow's `sources` (ref/title/text/last_updated), so the prompt
    builder is unchanged. Concatenates a source's hit passages in chunk order."""
    hits = retrieve(query_text, k)
    if not hits:
        return hits  # None or []
    by_ref: Dict[str, Dict[str, Any]] = {}
    for h in hits:
        ref = h["source_ref"]
        slot = by_ref.setdefault(ref, {
            "ref": ref, "name": h["source_name"], "title": h["source_name"],
            "last_updated": h["last_updated"], "passages": [], "top_similarity": 0.0,
        })
        slot["passages"].append(h["passage"])
        slot["top_similarity"] = max(slot["top_similarity"], h["similarity"])
    sources = []
    for ref, slot in sorted(by_ref.items(),
                            key=lambda kv: kv[1]["top_similarity"], reverse=True):
        sources.append({
            "ref": ref, "name": slot["name"], "title": slot["title"],
            "last_updated": slot["last_updated"],
            "text": "\n…\n".join(slot["passages"]),
        })
    return sources


# --------------------------------------------------------------------------- #
# Ingestion (used by the one-time ingest path; writer role)
# --------------------------------------------------------------------------- #
def upsert_source(source_ref: str, source_name: str, text: str,
                  last_updated: Optional[str], *, writer_user: str) -> int:
    """Chunk + embed a source document and replace its rows in evidence_chunk.
    Returns the number of chunks written. Requires a writer db user."""
    chunks = chunk_text(text)
    rows = [(source_ref, source_name, i, c, last_updated, _vec_literal(embed(c)))
            for i, c in enumerate(chunks)]
    with _connect(writer_user) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM evidence_chunk WHERE source_ref = %s;", (source_ref,))
            cur.executemany(
                "INSERT INTO evidence_chunk "
                "(source_ref, source_name, chunk_no, passage, last_updated, embedding) "
                "VALUES (%s, %s, %s, %s, %s, %s::vector);",
                rows,
            )
        conn.commit()
    return len(chunks)
