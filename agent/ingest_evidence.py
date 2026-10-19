#!/usr/bin/env python3
"""
ingest_evidence.py — embed the review evidence corpus into pgvector (one-time /
on change). Reads the same evidence documents the review agent uses (Box when
configured, else local fixtures), chunks + Titan-embeds each, and upserts rows
into the `evidence_chunk` table.

Usage:
  # point at the Aurora cluster and a WRITER db user, then:
  EVIDENCE_STORE=on \
  DB_HOST=<cluster-endpoint> DB_NAME=postgres \
  EVIDENCE_DB_WRITER=evidence_writer AWS_REGION=us-east-2 \
  python3 agent/ingest_evidence.py

Design: docs/DESIGN_pgvector_evidence_retrieval.md
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "agent"))

import evidence_store  # noqa: E402
from local_agent import get_docs, pdf_text, last_updated  # noqa: E402

WRITER_USER = os.environ.get("EVIDENCE_DB_WRITER", "evidence_writer")


def main() -> int:
    if not os.environ.get("DB_HOST"):
        print("DB_HOST not set — point at the Aurora cluster endpoint.", file=sys.stderr)
        return 2
    docs = get_docs()
    evidence = docs.evidence()
    if not evidence:
        print("No evidence documents found.", file=sys.stderr)
        return 1

    total_chunks = 0
    for i, e in enumerate(evidence, 1):
        text = pdf_text(docs.read(e["id"]))
        title = text.strip().splitlines()[0] if text.strip() else e["name"]
        ref = f"B{i}"
        n = evidence_store.upsert_source(
            source_ref=ref, source_name=e["name"], text=text,
            last_updated=last_updated(text), writer_user=WRITER_USER)
        total_chunks += n
        print(f"{ref}  {e['name']}  → {n} chunks  ({title[:60]})")

    print(f"\nIngested {len(evidence)} sources, {total_chunks} chunks into evidence_chunk.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
