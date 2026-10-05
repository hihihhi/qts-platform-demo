"""Concurrent readers, writers and a rewriter on one table, each a separate process. Part of
minilake, an independent demonstration system written only from the platform's public write-up; not
the platform's code.

Writers append batches, each publish a new version. A rewriter meanwhile compacts one partition
again and again (a replace computed from the version it read). Readers pin the latest version and
read it whole, again and again.
  torn read     a read that fails its checksums, or sees a batch with rows missing or doubled
  lost commit   a batch whose publish returned but which the final version does not hold exactly
                once; a replace that rebased over a concurrent append would cause one
  crashed/hung  a process that exits with an error, or is still running at the deadline; either
                fails the run instead of stalling it
A version pinned before the processes start must read the same after they finish.
"""
import hashlib
import json
import multiprocessing
import os
import queue
import sys
import time
from collections import Counter

from . import table

SCHEMA = {"batch": "str", "seq": "int"}
DAYS = ("2024-03-04", "2024-03-05", "2024-03-07")
REWRITTEN = DAYS[1]


def _digest(cols):
    return hashlib.sha256(json.dumps(cols, sort_keys=True).encode()).hexdigest()


def _batch(bid, n):
    return {"batch": [bid] * n, "seq": list(range(n))}


def _report(out, kind, work):
    """Run one process's work; send (kind, result, error). A failure exits with code 1 and is
    reported as data, so the caller asserts on it and nothing is printed to stderr."""
    result, error = None, None
    try:
        result = work()
    except Exception as e:
        result, error = getattr(e, "partial", None), f"{type(e).__name__}: {e}"
    out.put((kind, result, error))
    if error:
        sys.exit(1)


def _writer(tdir, w, commits, batch, fault, out):
    def work():
        done = []
        try:
            for c in range(commits):
                if fault and w == 0 and c == 2:  # fault injection, used only by the tests of this module
                    if fault == "crash":
                        raise RuntimeError("injected writer crash")
                    time.sleep(3600)
                table.publish(tdir, SCHEMA, {DAYS[c % len(DAYS)]: _batch(f"w{w}c{c}", batch)})
                done.append(f"w{w}c{c}")  # only after publish returned: the commit is acknowledged
                time.sleep(0.01)  # paced, so the rewriter's compactions interleave with the appends
        except Exception as e:
            e.partial = done  # commits acknowledged before the failure still count
            raise
        return done
    _report(out, "writer", work)


def _rewriter(tdir, stop, out):
    def work():
        made = conflicts = 0
        while not stop.is_set() or made == 0:
            base = table.latest(tdir)
            cols, _ = table.read(tdir, base, partitions={REWRITTEN})
            try:
                table.replace(tdir, SCHEMA, {REWRITTEN: cols}, base)  # compaction
                made += 1
            except table.Conflict:
                conflicts += 1
            time.sleep(0.002)
        return made, conflicts
    _report(out, "rewriter", work)


def _reader(tdir, batch, stop, out):
    def work():
        reads = torn = 0
        while not stop.is_set() or reads == 0:
            try:
                cols, _ = table.read(tdir, table.latest(tdir))
                torn += any(n != batch for n in Counter(cols["batch"]).values())
            except table.TornRead:
                torn += 1
            reads += 1
        return reads, torn
    _report(out, "reader", work)


def run(base, readers=4, writers=2, commits=20, batch=50, timeout=60, fault=None):
    tdir = os.path.join(base, "tables", "contended")
    seed = table.publish(tdir, SCHEMA, {d: _batch(f"seed-{d}", batch) for d in DAYS})
    pinned = _digest(table.read(tdir, seed)[0])
    ctx = multiprocessing.get_context("spawn")
    stop, out = ctx.Event(), ctx.Queue()
    ws = [ctx.Process(target=_writer, args=(tdir, w, commits, batch, fault, out)) for w in range(writers)]
    others = [ctx.Process(target=_reader, args=(tdir, batch, stop, out)) for _ in range(readers)]
    others.append(ctx.Process(target=_rewriter, args=(tdir, stop, out)))
    for p in ws + others:
        p.start()
    deadline = time.monotonic() + timeout
    try:
        for p in ws:
            p.join(max(0.0, deadline - time.monotonic()))
    finally:
        stop.set()
    for p in others:
        p.join(max(1.0, deadline - time.monotonic()))
    hung = [p for p in ws + others if p.is_alive()]
    for p in hung:
        p.terminate()
        p.join()
    crashed = sum(p.exitcode != 0 for p in ws + others if p not in hung)
    msgs = []
    while True:
        try:
            msgs.append(out.get(timeout=0.5))
        except queue.Empty:
            break
    acked = [b for kind, v, _ in msgs if kind == "writer" for b in v or []]
    final = Counter(table.read(tdir, table.latest(tdir))[0]["batch"])
    reads = [v for kind, v, _ in msgs if kind == "reader" and v]
    rewrites = [v for kind, v, _ in msgs if kind == "rewriter" and v] or [(0, 0)]
    return {"expected": writers * commits, "committed": len(acked),
            "lost": sum(final.get(b) != batch for b in acked + [f"seed-{d}" for d in DAYS]),
            "reads": sum(r for r, _ in reads), "torn": sum(t for _, t in reads),
            "rewrites": rewrites[0][0], "conflicts": rewrites[0][1],
            "readers": readers, "writers": writers, "crashed": crashed, "hung": len(hung),
            "errors": sorted(e for _, _, e in msgs if e),
            "pinned_unchanged": _digest(table.read(tdir, seed)[0]) == pinned}


def controls(base):
    """Positive controls for the run above, in the same run: each defect must be caught.

    torn: a version made visible before its data file, as a non-atomic publisher leaves it midway.
    stale replace: a replace computed from v1 after an append committed v2 to the same partition."""
    tdir = os.path.join(base, "tables", "control")
    table.publish(tdir, SCHEMA, {DAYS[0]: _batch("a", 5)})
    m = table.manifest(tdir, 1)
    os.mkdir(os.path.join(tdir, "v000002"))
    missing = {"partition": DAYS[1], "file": "v000002/not-yet-written.json", "rows": 5, "symbols": [],
               "sha256": "0" * 64}
    with open(os.path.join(tdir, "v000002", "manifest.json"), "w") as fh:
        json.dump(dict(m, version=2, parent=1, files=m["files"] + [missing]), fh)
    try:
        table.read(tdir, 2)
        torn = False
    except table.TornRead:
        torn = True
    tdir = os.path.join(base, "tables", "control-replace")
    table.publish(tdir, SCHEMA, {DAYS[0]: _batch("a", 5)})
    table.publish(tdir, SCHEMA, {DAYS[0]: _batch("b", 5)})
    try:
        table.replace(tdir, SCHEMA, {DAYS[0]: _batch("a", 5)}, base=1)
        refused = False
    except table.Conflict:
        refused = True
    return {"torn read caught": torn, "stale replace refused": refused}
