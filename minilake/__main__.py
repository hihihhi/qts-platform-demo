"""Walk through every layer on a fresh SYNTHETIC lake, then run the gates; exit 0 only if all pass.
Part of minilake, an independent demonstration system written only from the platform's public
write-up; not the platform's code.

    python3 -m minilake          (from the top of the repository)
"""
import os
import shutil
import sys
import tempfile

from . import cleanse, gates, pipeline, query, raw, synth


def show(label, text=""):
    print(f"{label:<10} {text}")


def walkthrough(lake, incoming):
    report = pipeline.build(lake, incoming)
    held = sorted(raw.load_manifest(lake))
    show("delivery", f"{len(held)} files for {len(synth.CALENDAR) - 1} of {len(synth.CALENDAR)} calendar days; "
         f"{synth.MISSING_DAY} never delivered")
    show("", "planted: " + ", ".join(f"{k} {v}" for k, v in synth.PLANTED.items()))

    show("raw", f"{len(report['raw'])} files held read-only under a SHA-256 manifest; "
         f"verify: {len(raw.verify(lake))} problems")
    again = raw.ingest(lake, [os.path.join(incoming, held[0])])
    with open(os.path.join(incoming, held[0]), "a") as fh:
        fh.write("tampered\n")
    try:
        raw.ingest(lake, [os.path.join(incoming, held[0])])
        refused = "ACCEPTED (wrong)"
    except ValueError:
        refused = "refused"
    show("", f"re-delivering the same bytes adds {len(again)} files; different bytes under a held name: {refused}")

    for kind, (v, counts) in report["typed"].items():
        show("typed" if kind == "trades" else "", f"{kind} v{v}: {len(counts)} day partitions, "
             f"{sum(counts.values()):,} rows, typed; nothing corrected, nothing dropped")

    days = sorted(report["cleansed"]["trades"][2])
    show("cleansed", "hits per rule per day (trades and quotes), each rule's effect:")
    print(f"{'':<12}{'rule':<17}" + "".join(f"{d[5:]:>7}" for d in days))
    for rule, effect in cleanse.RULES.items():
        per_day = [sum(report["cleansed"][k][2][d][rule] for k in report["cleansed"]) for d in days]
        print(f"{'':<12}{rule:<17}" + "".join(f"{n:>7}" for n in per_day) + f"   {effect}")
    removed = sum(sum(report["typed"][k][1].values()) - sum(report["cleansed"][k][1].values())
                  for k in report["typed"])
    auction = sum(h["closing_auction"] for h in report["cleansed"]["trades"][2].values())
    show("", f"rows removed: {removed}, all exact duplicates. {auction} closing-auction trades are labelled;")
    show("", "a 09:30-15:00:00 time-window filter would have dropped every one of them.")
    log = [e for k in report["cleansed"] for d in days for e in report["cleansed"][k][3][d]]
    show("", f"correction log: {len(log)} cells, each with the vendor's value, e.g. row {log[0][0]} "
         f"{log[0][1]} {log[0][2]} -> {log[0][3]}")

    r = query.fetch(lake, "trades_clean", ["SYM_A"], "2024-03-04", "2024-03-04")
    show("query", f"fetch SYM_A, 2024-03-04: {len(r.rows)} rows from pinned snapshot {r.snapshot}; "
         f"partitions read {r.partitions_read} of {len(days)}")
    b = query.fetch(lake, "trades_clean", ["SYM_A"], "2024-03-04", "2024-03-04T09:47", freq="5m", snapshot=r.snapshot)
    for bar in b.rows:
        show("", f"  5m bar {bar['start'][11:]}  open {bar['open']:6.2f}  high {bar['high']:6.2f}  "
             f"low {bar['low']:6.2f}  close {bar['close']:6.2f}  volume {bar['volume']:>6}")
    show("", f"  (as of 09:47 the 09:45 bar is still open, so it is withheld; the as-of guard withheld "
         f"{b.withheld} rows the read path handed over)")
    attempts = [
        ("the whole week", dict(universe=["SYM_A"], from_="2024-03-04", to="2024-03-08")),
        ("an unknown symbol", dict(universe=["SYM_Z"], from_="2024-03-04", to="2024-03-04")),
        ("gaps='closest' without permit_future",
         dict(universe=["SYM_A"], from_="2024-03-04", to="2024-03-04", freq="1m", gaps="closest")),
    ]
    for what, kw in attempts:
        try:
            query.fetch(lake, "trades_clean", **kw)
            show("", f"{what}: returned (wrong)")
        except (query.MissingDay, query.NoSuchInstrument, query.LookAhead) as e:
            show("", f"{what}: {type(e).__name__}: {e}")
    s = query.fetch(lake, "trades_clean", ["SYM_A"], "2024-03-04", "2024-03-08", on_missing="skip")
    show("", f"the whole week, on_missing='skip': {len(s.rows)} rows; skipped days named: {s.skipped_days}")


def main():
    print("minilake: an independent demonstration system written only from the platform's public write-up;\n"
          "not the platform's code. All data is SYNTHETIC.\n")
    base = tempfile.mkdtemp(prefix="minilake-")
    try:
        lake = os.path.join(base, "lake")
        walkthrough(lake, os.path.join(base, "incoming"))
        print()
        show("gates", "each runs a positive control: a planted defect to catch, or ground truth to match")
        os.mkdir(os.path.join(base, "scratch"))
        return 1 if gates.run(lake, os.path.join(base, "scratch")) else 0
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
