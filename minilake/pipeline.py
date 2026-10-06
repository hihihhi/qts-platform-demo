"""Build the three layers from a SYNTHETIC delivery. Part of minilake, an independent demonstration
system written only from the platform's public write-up; not the platform's code.

    raw        the delivered files, byte for byte (raw.py)
    trades     typed: the same rows with proper types; nothing corrected, nothing dropped
    quotes
    trades_clean  cleansed: typed rows plus labels, flags and declared corrections; each day's
    quotes_clean  per-rule counts and correction log are kept in the version's manifest
"""
import csv
import json
import os
from datetime import datetime

from . import cleanse, raw, synth, table

SCHEMAS = {
    "trades": {"ts": "ts", "symbol": "str", "price": "float", "size": "int", "trade_id": "str"},
    "quotes": {"ts": "ts", "symbol": "str", "bid": "float", "ask": "float", "bid_size": "int", "ask_size": "int"},
}


def _ts(text):
    datetime.fromisoformat(text)  # raises on a malformed time; kept as ISO text, which sorts as time
    return text


CAST = {"str": str, "int": int, "float": float, "ts": _ts}


def table_dir(lake, name):
    return os.path.join(lake, "tables", name)


def calendar(lake):
    with open(os.path.join(lake, "calendar.json")) as fh:
        return json.load(fh)


def typed_columns(kind, path):
    """One raw file as typed columns: every row kept, every value cast by the schema."""
    schema = SCHEMAS[kind]
    with open(path, newline="") as fh:
        rowlist = list(csv.DictReader(fh))
    return {name: [CAST[t](r[name]) for r in rowlist] for name, t in schema.items()}


def _publish(tdir, schema, parts, meta=None):
    """Make `parts` the only files of their days. A rebuild of unchanged raw files therefore adds a
    version with the same rows, where an append doubled every day. Raises Conflict if another writer
    touched these days since the head read here, rather than discard that write."""
    return table.replace(tdir, schema, parts, base=table.latest(tdir), meta=meta)


def build(lake, incoming):
    """Deliver, ingest, convert and cleanse. Returns what each layer did, for printing."""
    delivered = synth.generate(incoming)
    added = raw.ingest(lake, delivered)
    problems = raw.verify(lake)
    if problems:
        raise RuntimeError(f"raw layer failed verification: {problems}")
    raw.write_json_atomic(os.path.join(lake, "calendar.json"), list(synth.CALENDAR))
    report = {"raw": added, "typed": {}, "cleansed": {}}
    for kind in SCHEMAS:
        parts = {name[len(kind) + 1:-4]: typed_columns(kind, raw.path_of(lake, name))
                 for name in sorted(raw.load_manifest(lake)) if name.startswith(kind + "_")}
        v = _publish(table_dir(lake, kind), SCHEMAS[kind], parts)
        clean_parts, counts, logs = {}, {}, {}
        for day, cols in parts.items():
            clean_parts[day], counts[day], logs[day] = cleanse.cleanse_day(kind, cols)
        schema = dict(SCHEMAS[kind], session="str", flags="str")
        cv = _publish(table_dir(lake, kind + "_clean"), schema, clean_parts,
                      meta={"rule_counts": counts, "corrections": logs})
        report["typed"][kind] = (v, {d: len(c["ts"]) for d, c in parts.items()})
        report["cleansed"][kind] = (cv, {d: len(c["ts"]) for d, c in clean_parts.items()}, counts, logs)
    return report
