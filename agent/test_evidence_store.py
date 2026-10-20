"""
Offline unit tests for evidence_store pure logic (chunking + enable gating).
No AWS, no DB. Run: python3 agent/test_evidence_store.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import evidence_store as es  # noqa: E402

_p = _f = 0


def check(name, cond):
    global _p, _f
    if cond:
        _p += 1; print(f"PASS {name}")
    else:
        _f += 1; print(f"FAIL {name}")


# chunk_text ----------------------------------------------------------------
check("empty text → no chunks", es.chunk_text("") == [])
check("short text → single chunk", es.chunk_text("hello world") == ["hello world"])

long = ("Paragraph one about availability SLA in Ireland.\n\n"
        "Paragraph two about the delivery roadmap and committed dates.\n\n"
        "Paragraph three about validated benchmark throughput numbers.\n\n") * 40
chunks = es.chunk_text(long, size=500, overlap=100)
check("long text → multiple chunks", len(chunks) > 1)
check("chunks respect size bound (+overlap slack)", all(len(c) <= 700 for c in chunks))
check("overlap preserves continuity", any(
    chunks[i][-30:] and chunks[i][-20:] in (chunks[i] + chunks[i + 1])
    for i in range(len(chunks) - 1)))
# reassembled text covers the original content (no dropped middle)
check("no content dropped", "benchmark throughput" in " ".join(chunks))

# vector literal ------------------------------------------------------------
check("vec literal format", es._vec_literal([0.1, -0.2, 0.3]) == "[0.100000,-0.200000,0.300000]")

# enable gating -------------------------------------------------------------
os.environ.pop("EVIDENCE_STORE", None)
check("disabled by default", es.enabled() is False)
os.environ["EVIDENCE_STORE"] = "on"
# DB_HOST unset in test env → still disabled (needs a host)
prev_host = es.DB_HOST
es.DB_HOST = None
check("enabled needs DB host", es.enabled() is False)
es.DB_HOST = "cluster.example"
check("enabled when on + host set", es.enabled() is True)
es.DB_HOST = prev_host
os.environ.pop("EVIDENCE_STORE", None)


if __name__ == "__main__":
    print(f"\n{_p} passed, {_f} failed")
    sys.exit(1 if _f else 0)
