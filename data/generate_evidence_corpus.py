#!/usr/bin/env python3
"""
generate_evidence_corpus.py — synthetic EVIDENCE corpus for the pgvector demo.

Produces a large, varied set of fictional product/evidence documents (solution
guides, roadmaps, delivery reviews, benchmarks, security notes) spanning many
unrelated product areas. Most docs are irrelevant to any one contract, so
semantic top-k retrieval genuinely selects a small relevant subset out of the
whole corpus — which is the point of the pgvector feature.

NOTE: this is *evidence* (what claims are checked against), not contracts. All
content is clearly fictional demo data.

Usage:
  python3 data/generate_evidence_corpus.py --count 100 --out docs/evidence_corpus
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "agent"))

from minipdf import PdfDoc  # noqa: E402

# Varied product areas so embeddings spread across semantic space.
AREAS = [
    ("Archive Storage", "object archival, lifecycle tiering, retrieval SLAs",
     ["Encryption at rest for Standard and Archive tiers in all commercial Regions.",
      "Cross-Region replication is Target, not Committed, for Archive tier.",
      "Bulk retrieval validated to 12 hours for up to 500 TB per batch.",
      "Lifecycle rules move objects Standard→Archive after a configurable window."]),
    ("Streaming Ingest", "real-time event pipelines, throughput, ordering",
     ["Per-shard throughput validated at 1 MB/s write, 2 MB/s read.",
      "Exactly-once delivery is Exploratory; at-least-once is Approved.",
      "Ordering guaranteed within a partition key, not across the stream.",
      "Max retention window is 365 days on the extended tier."]),
    ("Managed Database", "relational engine, HA, backups, replicas",
     ["Automated backups retained up to 35 days; PITR to the second.",
      "Multi-AZ failover validated under 60 seconds in benchmarks.",
      "Read replicas up to 15; cross-Region replicas are Target.",
      "Storage autoscaling to 128 TiB on the current generation."]),
    ("Edge CDN", "caching, TLS, origins, invalidation",
     ["Cache invalidation propagates in under 5 minutes p99.",
      "mTLS to custom origins is Approved; client-cert auth is Exploratory.",
      "Signed URLs and signed cookies supported for private content.",
      "Brotli and gzip compression negotiated per request."]),
    ("Identity & Access", "auth, MFA, federation, tokens",
     ["OIDC auth-code with PKCE is Approved for public clients.",
      "SAML federation validated with the three largest IdPs.",
      "Refresh-token rotation is Committed; token revocation is Approved.",
      "Hardware MFA (FIDO2) is supported; SMS MFA is being deprecated."]),
    ("Observability", "metrics, logs, traces, dashboards",
     ["Custom metrics via EMF with no extra API calls.",
      "Trace sampling configurable 1–100%; tail-based sampling is Target.",
      "Log retention from 1 day to 10 years per log group.",
      "Dashboard sharing by link is Approved; public embed is Exploratory."]),
    ("Compute Autoscaling", "instances, scaling policies, spot",
     ["Target-tracking scaling validated to 500 instances in 4 minutes.",
      "Spot interruption handling with a 2-minute notice is Approved.",
      "Warm pools reduce scale-out latency; cross-AZ rebalance is Target.",
      "Graviton (Arm64) images Approved for the listed runtimes."]),
    ("Networking", "VPC, load balancing, DNS, peering",
     ["Cross-AZ load balancing validated at 1M requests/second.",
      "Latency-based DNS routing is Approved; geoproximity is Exploratory.",
      "Transit peering across up to 50 VPCs on the current limit.",
      "Private connectivity via endpoint services is Committed."]),
]

DOC_KINDS = [
    ("Approved Solution Guide", "Approved for customer use", "Supported capabilities"),
    ("Product Roadmap", "Roadmap — subject to change", "Planned and in-progress items"),
    ("Delivery Review", "Internal delivery review", "Observed delivery outcomes"),
    ("Performance Benchmark", "Validated benchmark results", "Measured results"),
    ("Security Review", "Security posture review", "Controls and findings"),
]

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def _doc(idx: int) -> tuple[str, "PdfDoc"]:
    area, blurb, facts = AREAS[idx % len(AREAS)]
    kind, status, section = DOC_KINDS[(idx // len(AREAS)) % len(DOC_KINDS)]
    ref = f"E{idx:03d}"
    title = f"{area} — {kind}"
    month = MONTHS[idx % 12]
    doc = PdfDoc()
    doc.add_heading(title)
    doc.add_paragraph(
        f"Document ID: {ref} | Owner: {area} Product Team | "
        f"Last updated: {month} {1 + idx % 27}, 2026 | Status: {status}")
    doc.add_paragraph("Fictional demo data. Not actual AWS product documentation or commitments.")
    doc.add_subheading("Scope")
    doc.add_paragraph(f"This document covers {blurb} for the {area} service.")
    doc.add_subheading(section)
    # Rotate the facts so each doc differs while staying on-topic for its area.
    for j, f in enumerate(facts):
        doc.add_paragraph(f"- {f}")
        doc.add_paragraph(
            f"- Note {ref}-{j}: validated in the {month} test cycle; "
            f"figures apply to the listed Regions and configurations only.")
    doc.add_subheading("Caveats")
    doc.add_paragraph(
        "Anything not listed here must be confirmed with the product owner "
        "before it appears in a customer agreement. Burst figures are not "
        "sustained commitments.")
    filename = f"{ref}-{area.lower().replace(' & ', '-').replace(' ', '-')}-{kind.lower().replace(' ', '-')}.pdf"
    return filename, doc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "evidence_corpus"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    for i in range(args.count):
        name, doc = _doc(i)
        doc.save(os.path.join(args.out, name))
    print(f"Wrote {args.count} synthetic evidence PDFs to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
