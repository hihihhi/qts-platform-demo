"""Versioned, typed, column-oriented tables. Part of minilake, an independent demonstration system
written only from the platform's public write-up; not the platform's code.

Storage is a JSON file per partition holding one list per column: standard library only, so it runs
anywhere. It stands in for a columnar file format, and deliberately does not use the platform's stack.

A table is a folder of versions v000001, v000002, ... Each version folder holds the partition files
its publish wrote, plus a manifest listing every file the version contains (files first written by
earlier versions included), each with its partition, row count, symbols and SHA-256. Files are
never rewritten or deleted, so a version, once visible, never changes; a reader pins one by number.

Publishing is atomic, with optimistic concurrency in the style of the public Delta Lake and Apache
Iceberg table formats. A version is assembled in a staging folder and renamed into place in one
step, so a reader sees all of it or none of it. Two writers racing for the same number cannot both
win the rename. The loser re-checks against every version committed since its base:
  - an append commutes with anything, so it rebases onto the winner's manifest and takes the next
    number;
  - a replace was computed from its base. If a version since then wrote to a partition it replaces,
    rebasing would silently discard that commit, so it raises Conflict and the caller re-reads and
    retries. Otherwise it rebases like an append.
A replace cannot be issued without naming its base: replace() takes it as a required argument.
"""
import hashlib
import json
import os
import re
import shutil
import uuid

VERSION = re.compile(r"^v(\d{6})$")
TYPES = {"str": str, "ts": str, "int": int, "float": float}


class TornRead(Exception):
    """A version that is visible but not whole: what atomic publishing exists to prevent."""


class NoSuchSnapshot(LookupError):
    pass


class Conflict(Exception):
    """A replace whose partitions were written by a version committed after its base."""


def versions(table_dir):
    names = os.listdir(table_dir) if os.path.isdir(table_dir) else []
    return sorted(int(m.group(1)) for m in map(VERSION.match, names) if m)


def latest(table_dir):
    found = versions(table_dir)
    return found[-1] if found else 0


def manifest(table_dir, version):
    folder = os.path.join(table_dir, f"v{version:06d}")
    if not os.path.isdir(folder):
        raise NoSuchSnapshot(f"{os.path.basename(table_dir)} has no version {version}")
    try:
        with open(os.path.join(folder, "manifest.json")) as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        raise TornRead(f"v{version:06d} is visible but its manifest is missing or half-written")


def check_types(schema, cols):
    if set(cols) != set(schema):
        raise TypeError(f"columns {sorted(cols)} do not match the schema {sorted(schema)}")
    lengths = {len(v) for v in cols.values()}
    if len(lengths) > 1:
        raise TypeError(f"columns of unequal length {sorted(lengths)}")
    for name, kind in schema.items():
        bad = [v for v in cols[name] if not isinstance(v, TYPES[kind])]
        if bad:
            raise TypeError(f"column {name}: {bad[0]!r} is not {kind}")


def written_since(table_dir, base, head, partitions):
    """Versions after `base`, up to `head`, that wrote a file into any of `partitions`."""
    return [v for v in range(base + 1, head + 1)
            if any(e["partition"] in partitions and e["file"].startswith(f"v{v:06d}/")
                   for e in manifest(table_dir, v)["files"])]


def publish(table_dir, schema, parts, meta=None):
    """Append: add the files to their partitions as one new version; return its number.

    parts maps a partition key (a day) to columns {name: [values]}."""
    return _commit(table_dir, schema, parts, meta, replace_base=None)


def replace(table_dir, schema, parts, base, meta=None):
    """Make these files the only files of their partitions, as one new version; return its number.

    `base` is required: the version the replacement was computed from. If any version after it
    wrote to these partitions, raise Conflict rather than discard that write."""
    return _commit(table_dir, schema, parts, meta, replace_base=base)


def _commit(table_dir, schema, parts, meta, replace_base):
    for cols in parts.values():
        check_types(schema, cols)
    os.makedirs(table_dir, exist_ok=True)
    stage = os.path.join(table_dir, f".staging-{uuid.uuid4().hex}")  # invisible: not a version name
    os.mkdir(stage)
    new = []
    for key, cols in sorted(parts.items()):
        data = json.dumps({"columns": cols}, sort_keys=True, separators=(",", ":")).encode()
        name = f"{key}-{uuid.uuid4().hex[:8]}.json"
        with open(os.path.join(stage, name), "wb") as fh:
            fh.write(data)
        new.append({"partition": key, "file": name, "rows": len(next(iter(cols.values()), [])),
                    "symbols": sorted(set(cols.get("symbol", []))), "sha256": hashlib.sha256(data).hexdigest()})
    while True:
        head = latest(table_dir)  # read before the check: a later commit makes the rename fail
        if replace_base is not None and written_since(table_dir, replace_base, head, parts):
            shutil.rmtree(stage)
            raise Conflict(f"partitions {sorted(parts)} were written after v{replace_base:06d}; re-read and retry")
        files = manifest(table_dir, head)["files"] if head else []
        if replace_base is not None:
            files = [f for f in files if f["partition"] not in parts]
        target = f"v{head + 1:06d}"
        body = {"version": head + 1, "parent": head, "schema": schema, "meta": meta or {},
                "files": files + [dict(e, file=f"{target}/{e['file']}") for e in new]}
        with open(os.path.join(stage, "manifest.json"), "w") as fh:
            json.dump(body, fh, indent=1)
        try:
            os.rename(stage, os.path.join(table_dir, target))  # the commit point
            return head + 1
        except OSError:
            if not os.path.isdir(os.path.join(table_dir, target)):
                raise
            # another writer took this number first: rebase onto its manifest and try the next


def read(table_dir, version, partitions=None, symbols=None):
    """(columns, partitions opened) of one pinned version, opening only the files a request needs.

    Every file is checked against the manifest's checksum before use."""
    m = manifest(table_dir, version)
    out, opened = {c: [] for c in m["schema"]}, set()
    for e in m["files"]:
        if partitions is not None and e["partition"] not in partitions:
            continue  # partition pruning
        if symbols is not None and not set(symbols) & set(e["symbols"]):
            continue  # file skipped on its symbol list
        try:
            with open(os.path.join(table_dir, e["file"]), "rb") as fh:
                data = fh.read()
        except FileNotFoundError:
            raise TornRead(f"v{version:06d} lists {e['file']}, which is not there")
        if hashlib.sha256(data).hexdigest() != e["sha256"]:
            raise TornRead(f"{e['file']} does not match the checksum v{version:06d} recorded")
        cols = json.loads(data)["columns"]
        for c in out:
            out[c] += cols[c]
        opened.add(e["partition"])
    return out, sorted(opened)


def rows(cols):
    names = list(cols)
    return [dict(zip(names, values)) for values in zip(*(cols[n] for n in names))]


def columns(rowlist, names):
    return {n: [r[n] for r in rowlist] for n in names}
