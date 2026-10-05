"""Layer 1, raw: delivered files kept byte for byte under a SHA-256 manifest. Part of minilake, an
independent demonstration system written only from the platform's public write-up; not the platform's
code.

A file enters once and is made read-only. Re-delivering the same bytes is a no-op; different bytes
under a name already held are refused, because the raw layer is never rewritten. verify() reports
any held file whose bytes no longer match the manifest.
"""
import hashlib
import json
import os
import shutil
import uuid


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def write_json_atomic(path, obj):
    """Write to a temporary name, then rename over the target: readers see old or new, never half."""
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _folder(lake):
    return os.path.join(lake, "raw")


def load_manifest(lake):
    try:
        with open(os.path.join(_folder(lake), "manifest.json")) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}


def ingest(lake, paths):
    """Copy delivered files into the raw layer; return the names newly added."""
    os.makedirs(_folder(lake), exist_ok=True)
    manifest, added = load_manifest(lake), []
    for src in paths:
        name, digest = os.path.basename(src), sha256(src)
        if name in manifest:
            if manifest[name]["sha256"] != digest:
                raise ValueError(f"raw/{name} is already held with different bytes; raw is never rewritten")
            continue
        dest = os.path.join(_folder(lake), name)
        shutil.copyfile(src, dest)
        os.chmod(dest, 0o444)
        manifest[name] = {"sha256": digest, "size": os.path.getsize(dest)}
        added.append(name)
    write_json_atomic(os.path.join(_folder(lake), "manifest.json"), manifest)
    return added


def verify(lake):
    """Every problem with the raw layer; an empty list means every byte matches the manifest."""
    manifest, folder = load_manifest(lake), _folder(lake)
    if not manifest:
        return ["manifest is empty or missing: nothing was verified"]  # an empty run is a failure
    problems = []
    for name, entry in sorted(manifest.items()):
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            problems.append(f"{name}: missing")
        elif sha256(path) != entry["sha256"]:
            problems.append(f"{name}: bytes differ from the manifest")
    problems += [f"{n}: not in the manifest" for n in sorted(os.listdir(folder))
                 if n != "manifest.json" and n not in manifest]
    return problems


def path_of(lake, name):
    return os.path.join(_folder(lake), name)
