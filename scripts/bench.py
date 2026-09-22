#!/usr/bin/env python3
"""Measure this node instead of guessing. Run on each box, compare, assign roles.

    python scripts/bench.py                 # all benchmarks
    python scripts/bench.py --embed-only
    python scripts/bench.py --json

Reports embedding throughput (the only part that really cares about CPU age),
search latency, and SQLite write throughput.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SAMPLE = (
    "QGIS crashes when opening a large GeoPackage stored on a network share. "
    "The layer loads but panning triggers a segfault in GDAL. Bat call sequences "
    "from Anabat Insight were exported to Kaleidoscope and the auto classifier "
    "mislabelled harmonic noise as Barbastella barbastellus at 32 kHz. "
)


def node_info() -> dict:
    info = {"host": platform.node(), "os": f"{platform.system()} {platform.release()}",
            "machine": platform.machine(), "python": platform.python_version(),
            "cpu_count": os.cpu_count()}
    try:
        info["ram_gb"] = round(
            os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9, 1)
    except (ValueError, OSError):
        pass
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        for line in cpuinfo.read_text().splitlines():
            if "model name" in line:
                info["cpu"] = line.split(":", 1)[1].strip()
                break
        flags = next((l for l in cpuinfo.read_text().splitlines()
                      if l.startswith("flags")), "")
        info["simd"] = ("avx512" if "avx512" in flags else
                        "avx2" if " avx2 " in flags else
                        "avx" if " avx " in flags else "none")
    return info


def bench_embed(kind: str, n: int = 128) -> dict:
    from brain.embed import get_embedder
    texts = [f"{SAMPLE} document {i}" for i in range(n)]
    emb = get_embedder(kind)
    emb.encode(texts[:4])                       # warm up: model load, not throughput
    t0 = time.perf_counter()
    vecs = emb.encode(texts)
    elapsed = time.perf_counter() - t0
    return {"embedder": emb.name, "dim": emb.dim, "chunks": n,
            "seconds": round(elapsed, 2),
            "chunks_per_sec": round(n / elapsed, 1),
            "est_minutes_per_10k": round((10_000 / (n / elapsed)) / 60, 1),
            "sane": len(vecs) == n}


def bench_search(reps: int = 25) -> dict | None:
    from brain.store import Store
    from brain.search import search
    db = os.environ.get("BRAIN_DB", "data/brain.db")
    if not Path(db).exists():
        return None
    st = Store(db)
    if not sum(s["n"] for s in st.sources()):
        st.close()
        return None
    queries = ["geopackage lock network share", "bat call classifier confidence",
               "pyqgis processing algorithm crash", "arcgis pro projection mismatch",
               "sql express connection timeout"]
    lat = []
    for i in range(reps):
        t0 = time.perf_counter()
        search(st, queries[i % len(queries)], limit=10)
        lat.append((time.perf_counter() - t0) * 1000)
    docs = sum(s["n"] for s in st.sources())
    st.close()
    return {"documents": docs, "queries": reps,
            "p50_ms": round(statistics.median(lat), 1),
            "p95_ms": round(sorted(lat)[int(len(lat) * 0.95) - 1], 1)}


def bench_write(n: int = 300) -> dict:
    from brain.store import Store, Document
    tmp = tempfile.mkdtemp()
    st = Store(os.path.join(tmp, "bench.db"))
    docs = [Document(source="bench", id=str(i), url=f"http://x/{i}",
                     title=f"doc {i}", text=SAMPLE * 3) for i in range(n)]
    t0 = time.perf_counter()
    for d in docs:
        st.upsert(d)
    elapsed = time.perf_counter() - t0
    st.close()
    return {"documents": n, "seconds": round(elapsed, 2),
            "docs_per_sec": round(n / elapsed, 1)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--embedder", default=os.environ.get("BRAIN_EMBEDDER", "hash"))
    ap.add_argument("--embed-only", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--chunks", type=int, default=128)
    args = ap.parse_args()

    out: dict = {"node": node_info()}
    try:
        out["embed"] = bench_embed(args.embedder, args.chunks)
    except Exception as exc:
        out["embed"] = {"error": f"{exc.__class__.__name__}: {exc}"}
    if not args.embed_only:
        try:
            out["search"] = bench_search() or {"skipped": "store empty"}
        except Exception as exc:
            out["search"] = {"error": str(exc)}
        try:
            out["write"] = bench_write()
        except Exception as exc:
            out["write"] = {"error": str(exc)}

    if args.json:
        print(json.dumps(out, indent=2))
        return 0

    n = out["node"]
    print(f"\n{n['host']}  {n.get('cpu', n['machine'])}")
    print(f"  {n['os']}, {n['cpu_count']} cpu, {n.get('ram_gb', '?')} GB ram, "
          f"simd={n.get('simd', '?')}")
    e = out["embed"]
    if "error" in e:
        print(f"\nembed    FAILED: {e['error']}")
    else:
        print(f"\nembed    {e['embedder']} ({e['dim']}d): "
              f"{e['chunks_per_sec']} chunks/sec  "
              f"-> ~{e['est_minutes_per_10k']} min per 10k chunks")
    if s := out.get("search"):
        if "p50_ms" in s:
            print(f"search   {s['documents']} docs: p50 {s['p50_ms']} ms, "
                  f"p95 {s['p95_ms']} ms")
        else:
            print(f"search   {s.get('skipped') or s.get('error')}")
    if w := out.get("write"):
        if "docs_per_sec" in w:
            print(f"write    {w['docs_per_sec']} docs/sec ingested+chunked")
    print("\nRun this on both nodes. Put bulk embedding where chunks/sec is highest;\n"
          "the API and store can live anywhere, they are not CPU bound.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
