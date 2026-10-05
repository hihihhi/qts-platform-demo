"""Gates as code: each invariant is a check, and this exits 0 only if every one holds. Part of
minilake, an independent demonstration system written only from the platform's public write-up; not
the platform's code.

    python3 -m minilake.gates          (from the top of the repository) builds a fresh SYNTHETIC lake and gates it

A check that looks for a defect proves nothing by staying quiet, so each one also runs a positive
control in the same run: a planted defect it must catch, or the synthetic ground truth it must match.
"""
import os
import shutil
import sys
import tempfile

from . import cleanse, contend, pipeline, query, raw, synth, table


def _flip_byte(path, at=40):
    os.chmod(path, 0o644)
    with open(path, "r+b") as fh:
        fh.seek(at)
        b = fh.read(1)
        fh.seek(at)
        fh.write(bytes([b[0] ^ 1]))


def _copy(lake, scratch, name):
    dest = os.path.join(scratch, name)
    shutil.copytree(lake, dest)
    return dest


def _meta(lake, kind):
    tdir = pipeline.table_dir(lake, kind + "_clean")
    return table.manifest(tdir, table.latest(tdir))["meta"]


def gate_checksums(lake, scratch):
    clean = raw.verify(lake)
    copy = _copy(lake, scratch, "tampered")
    _flip_byte(raw.path_of(copy, sorted(raw.load_manifest(copy))[0]))
    caught_raw = raw.verify(copy)
    tdir = pipeline.table_dir(copy, "trades_clean")
    victim = next(e for e in table.manifest(tdir, 1)["files"] if e["partition"] == "2024-03-05")
    _flip_byte(os.path.join(tdir, victim["file"]))
    try:
        query.fetch(copy, "trades_clean", ["SYM_A"], "2024-03-05", "2024-03-05")
        caught_read = False
    except table.TornRead:
        caught_read = True
    ok = not clean and len(caught_raw) == 1 and caught_read
    return ok, (f"{len(raw.load_manifest(lake))} raw files verified, {len(clean)} problems; controls: altered "
                f"raw byte caught: {len(caught_raw) == 1}; altered table byte refused by fetch: {caught_read}")


def gate_layer_diff(lake):
    problems, days = [], 0
    for kind in pipeline.SCHEMAS:
        meta = _meta(lake, kind)
        for day, hits in meta["rule_counts"].items():
            t, _ = table.read(pipeline.table_dir(lake, kind), 1, partitions={day})
            c, _ = table.read(pipeline.table_dir(lake, kind + "_clean"), 1, partitions={day})
            problems += [f"{kind} {day}: {p}" for p in cleanse.layer_diff(t, c, hits, meta["corrections"][day])]
            days += 1
    day = "2024-03-07"
    meta, t = _meta(lake, "trades"), table.read(pipeline.table_dir(lake, "trades"), 1, partitions={day})[0]

    def caught(change):
        c = table.read(pipeline.table_dir(lake, "trades_clean"), 1, partitions={day})[0]
        change(c)
        return bool(cleanse.layer_diff(t, c, meta["rule_counts"][day], meta["corrections"][day]))

    controls = {"unnamed value change": caught(lambda c: c["price"].__setitem__(10, c["price"][10] + 0.01)),
                "unlogged time change": caught(lambda c: c["ts"].__setitem__(10, c["ts"][10][:11] + "12:34:56.789")),
                "wrong session label": caught(lambda c: c["session"].__setitem__(10, "closing_auction"))}
    ok = not problems and days > 0 and all(controls.values())
    return ok, (f"{days} table-days diffed, {len(problems)} unexplained changes; controls caught: "
                f"{', '.join(k for k, v in controls.items() if v) or 'NONE'}"
                + "".join(f"\n      {p}" for p in problems[:5]))


def gate_rule_counts(lake):
    found = dict.fromkeys(synth.PLANTED, 0)
    for kind in pipeline.SCHEMAS:
        for hits in _meta(lake, kind)["rule_counts"].values():
            for rule in found.keys() & hits.keys():
                found[rule] += hits[rule]
    r = query.fetch(lake, "trades_clean", ["SYM_A"], synth.CALENDAR[0], synth.CALENDAR[-1], on_missing="skip")
    found["missing_day"] = len(r.skipped_days)
    text = ", ".join(f"{k} {found[k]}/{v}" for k, v in synth.PLANTED.items())
    return found == synth.PLANTED, f"found/planted: {text}"


def gate_auction_labels(lake):
    """The headline invariant against the synthetic ground truth: every closing-auction trade, and
    only those, is labelled closing_auction, kept, and unflagged."""
    c, _ = table.read(pipeline.table_dir(lake, "trades_clean"), 1)
    truth = {i for i, t in enumerate(c["trade_id"]) if t.endswith("-auction")}
    labelled = {i for i, s in enumerate(c["session"]) if s == "closing_auction"}
    flagged = [i for i in truth if c["flags"][i]]
    ok = len(truth) == synth.PLANTED["closing_auction"] and labelled == truth and not flagged
    return ok, (f"{len(labelled)} labelled closing_auction; ground truth {len(truth)} kept of "
                f"{synth.PLANTED['closing_auction']} delivered; same rows: {labelled == truth}; flagged: {len(flagged)}")


def gate_as_of(lake):
    to = "2024-03-05T10:00"
    r = query.fetch(lake, "trades_clean", ["SYM_A", "SYM_B"], "2024-03-04", to)
    within = bool(r.rows) and max(x["ts"] for x in r.rows) < to and max(r.partitions_read) <= to[:10]
    b = query.fetch(lake, "trades_clean", ["SYM_A"], "2024-03-05", "2024-03-05T09:47", freq="5m")
    open_bar = bool(b.rows) and max(x["start"] for x in b.rows) <= "2024-03-05T09:40"  # 09:45 still open
    f = query.fetch(lake, "trades_clean", ["SYM_A", "SYM_C"], "2024-03-05", to, freq="1m", gaps="carry_back")
    filled = [x for x in f.rows if x["filled_from"]]
    real = {(x["symbol"], x["start"]): x for x in f.rows if not x["filled_from"]}

    def recomputed(x):  # the filled value rebuilt from the bar it names, which must be earlier
        src = real.get((x["symbol"], x["filled_from"]))
        return (src is not None and x["filled_from"] < x["start"]
                and all(x[k] == src["close"] for k in ("open", "high", "low", "close")))

    backward = bool(filled) and all(recomputed(x) for x in filled)
    try:
        query.fetch(lake, "trades_clean", ["SYM_A"], "2024-03-05", to, freq="1m", gaps="closest")
        refused = False
    except query.LookAhead:
        refused = True
    ok = within and r.withheld > 0 and open_bar and backward and refused
    return ok, (f"{len(r.rows)} rows returned, last {max(x['ts'] for x in r.rows)} < {to}; control: the read "
                f"path handed over {r.withheld} later rows and the guard withheld them; open bar withheld: "
                f"{open_bar}; {len(filled)} carry_back fills, each equal to the earlier bar it names: {backward}; "
                f"gaps='closest' without permit_future refused: {refused}")


def gate_pinned(lake, scratch):
    copy = _copy(lake, scratch, "pinned")
    before = query.fetch(copy, "trades_clean", ["SYM_A"], "2024-03-04", "2024-03-04", snapshot=1).rows
    tdir = pipeline.table_dir(copy, "trades_clean")
    cols, _ = table.read(tdir, 1, partitions={"2024-03-04"})
    v = table.replace(tdir, table.manifest(tdir, 1)["schema"],
                      {"2024-03-04": {k: vals[:100] for k, vals in cols.items()}}, base=1)
    again = query.fetch(copy, "trades_clean", ["SYM_A"], "2024-03-04", "2024-03-04", snapshot=1).rows
    latest = query.fetch(copy, "trades_clean", ["SYM_A"], "2024-03-04", "2024-03-04").rows
    ok = v == 2 and again == before and latest != before
    return ok, (f"v{v} published over the pinned v1; fetch(snapshot=1) unchanged: {again == before} "
                f"({len(again)} rows); control: latest differs: {latest != before} ({len(latest)} rows)")


def gate_concurrency(scratch):
    s, c = contend.run(scratch), contend.controls(scratch)
    ok = (s["committed"] == s["expected"] and s["lost"] == 0 and s["torn"] == 0 and s["reads"] > 0
          and s["rewrites"] > 0 and s["crashed"] == 0 and s["hung"] == 0 and not s["errors"] and s["pinned_unchanged"]
          and all(c.values()))
    return ok, (f"{s['readers']} readers, {s['writers']} writers, 1 rewriter: commits {s['committed']}/"
                f"{s['expected']} acknowledged, lost {s['lost']}; rewrites {s['rewrites']} (conflicts "
                f"retried {s['conflicts']}); reads {s['reads']}, torn {s['torn']}; crashed {s['crashed']}, "
                f"hung {s['hung']}; pinned version unchanged: {s['pinned_unchanged']}; controls: "
                + ", ".join(f"{k}: {v}" for k, v in c.items())
                + (f"; errors: {s['errors'][:2]}" if s["errors"] else ""))


def run(lake, scratch):
    gates = [("checksums", lambda: gate_checksums(lake, scratch)), ("layer diff", lambda: gate_layer_diff(lake)),
             ("rule counts", lambda: gate_rule_counts(lake)), ("auction labels", lambda: gate_auction_labels(lake)),
             ("as-of guard", lambda: gate_as_of(lake)), ("pinned snapshot", lambda: gate_pinned(lake, scratch)),
             ("concurrency", lambda: gate_concurrency(scratch))]
    failed = 0
    for name, gate in gates:
        try:
            ok, detail = gate()
        except Exception as e:  # a gate that crashes has failed, and says why
            ok, detail = False, f"crashed: {type(e).__name__}: {e}"
        failed += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<16} {detail}")
    print(f"  gates: {len(gates) - failed}/{len(gates)} pass")
    return failed


def main():
    base = tempfile.mkdtemp(prefix="minilake-")
    try:
        lake = os.path.join(base, "lake")
        pipeline.build(lake, os.path.join(base, "incoming"))
        os.mkdir(os.path.join(base, "scratch"))
        return 1 if run(lake, os.path.join(base, "scratch")) else 0
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
