#!/usr/bin/env python3
"""The documents' test: what can break in the README, checked against the code that backs it.

    check_docs.py              run every check on this repository; exit 0 only if all pass
    check_docs.py --self-test  plant one defect per check in a scratch copy; each must go red,
                               and the untouched copy (the control) must stay green

Checks, each with the failure it exists to catch:
  links     a relative link or #anchor in README.md that points nowhere
  fences    an unclosed code fence (it swallows the rest of the page on GitHub), or a mermaid
            block that is empty or does not start with a diagram type (GitHub shows an error box)
  results   the README's gate output differing from a fresh `python3 -m minilake` in anything but
            the concurrency line's rewrite, conflict and read counts, which vary between runs; a
            README without that block fails too
  forbidden a hardware, access or vendor term anywhere in the repository, this file included
            (only its pattern lists are skipped). This check blocks; it never just informs.
            Further patterns, one regex per line, are read from the file named by the environment
            variable FORBIDDEN_TERMS_FILE when it is set; that file is never committed.
Standard library only.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SELF = os.path.relpath(os.path.abspath(__file__), os.path.dirname(HERE))
LINK = re.compile(r"\]\(([^)\s]+)\)")
FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
MERMAID_TYPES = ("flowchart", "graph", "sequenceDiagram", "stateDiagram", "timeline", "xychart-beta")
# The only figures in the gate output that depend on how the processes interleave.
VARYING = re.compile(r"\b(rewrites|conflicts retried|reads) \d+")
GATES_START = "gates      each runs"

# --- pattern lists: the only lines of this file the forbidden-terms scan skips ---
MIT_TEXT = """Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE."""

# Generic words only; anything more specific would describe a real setup if listed here. Units are
# banned outright, because a size or a rate next to a duration encodes the hardware.
GENERIC = [
    r"\bGPUs?\b", r"\bRAM\b", r"\bthreads?\b", r"\bcores?\b", r"\bNAS\b", r"\bSSH", r"\bVPN\b",
    r"\bsudo\b", r"\broot\b", r"passwords?",
    r"\d\s?(?:KB|MB|GB|TB|PB|KiB|MiB|GiB|TiB)\b", r"\b(?:KB|MB|GB|TB)/s\b", r"\brows/s\b", r"\d\s?[GM]bps\b",
]
# Vendor and brand names are caught by shape, so that the names themselves never ship in this
# file: any domain name, and any all-capitals word that is not on the short list of terms this
# repository uses. github.com is allowed for the links to the write-up and its own CI.
DOMAIN = r"\b[a-z0-9-]+\.(?:com|net|org|io|cn|hk|ai|dev|xyz|co)\b"
DOMAIN_OK = {"github.com"}
ACRONYM = re.compile(r"(?<![\w-])[A-Z][A-Z0-9]{2,}(?![\w-])")
ACRONYM_OK = {
    "CUHK", "QTS", "README", "LICENSE", "PASS", "FAIL", "SYNTHETIC", "MIT", "UTC", "JSON",
}
# --- end of pattern lists ---
BLOCK_START, BLOCK_END = "# --- pattern lists:", "# --- end of pattern lists ---"
# Extra patterns from a file outside the repository; unset in CI, where the line printed says so.
PRIVATE = os.environ.get("FORBIDDEN_TERMS_FILE", "")


def slug(text):
    """GitHub's heading anchor: lower-case, drop punctuation, spaces to hyphens."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text).lower().replace("`", "")
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def parse(path):
    """(prose lines outside fences, heading anchors, fence errors, fences) for one markdown file.
    Each fence is (info string, prose heading above it, body lines)."""
    prose, anchors, errors, fences, seen, fence, heading = [], set(), [], [], {}, None, ""
    for n, line in enumerate(open(path, encoding="utf-8").read().splitlines(), 1):
        m = FENCE.match(line)
        if fence is None:
            if m:
                fence = (m.group(1), m.group(2).strip(), n, [])
                continue
            prose.append((n, line))
            h = HEADING.match(line)
            if h:
                heading = h.group(2)
                s = slug(heading)
                k = seen.get(s, 0)
                seen[s] = k + 1
                anchors.add(s if k == 0 else f"{s}-{k}")
            continue
        marker, info, start, body = fence
        if m and m.group(2).strip():
            errors.append(f"{path}:{start}: {info or 'code'} fence still open when another opens at line {n}")
        if m and m.group(1)[0] == marker[0] and len(m.group(1)) >= len(marker) and not m.group(2).strip():
            if info == "mermaid":
                first = next((b.strip() for b in body if b.strip()), "")
                if not first.startswith(MERMAID_TYPES):
                    errors.append(f"{path}:{start}: mermaid block empty or without a diagram type: {first[:40]!r}")
            fences.append((info, heading, body))
            fence = None
        else:
            body.append(line)
    if fence is not None:
        errors.append(f"{path}:{fence[2]}: {fence[1] or 'code'} fence opened here is never closed")
    return prose, anchors, errors, fences


def check_links_and_fences(tree):
    prose, anchors, errors, _ = parse(os.path.join(tree, "README.md"))
    for n, line in prose:
        for target in LINK.findall(line):
            if re.match(r"[a-z][a-z0-9+.-]*:", target):
                continue  # external URL: not checked offline
            file_part, _, anchor = target.partition("#")
            if file_part and not os.path.exists(os.path.join(tree, os.path.normpath(file_part))):
                errors.append(f"README.md:{n}: link to missing file {target}")
            elif anchor and not file_part and anchor not in anchors:
                errors.append(f"README.md:{n}: no heading for anchor {target}")
    return errors


def check_results(tree):
    """The README's text block under ## Results must be the gates section of a fresh run."""
    _, _, _, fences = parse(os.path.join(tree, "README.md"))
    stated = next((body for info, heading, body in fences if heading == "Results" and info == "text"), None)
    if not stated or not stated[0].startswith(GATES_START):
        return ["README.md: no gate output (a text block starting with the gates line) under ## Results"]
    run = subprocess.run([sys.executable, "-m", "minilake"], cwd=tree, capture_output=True, text=True, timeout=300)
    if run.returncode != 0:
        return [f"python3 -m minilake exit {run.returncode}: {(run.stdout + run.stderr)[-500:]}"]
    lines = run.stdout.splitlines()
    actual = next((lines[i:] for i, l in enumerate(lines) if l.startswith(GATES_START)), [])
    mask = lambda block: [VARYING.sub(r"\1 N", l.rstrip()) for l in block]
    if mask(stated) != mask(actual):
        diff = [f"README: {s!r}\n    run:    {a!r}" for s, a in zip(mask(stated), mask(actual)) if s != a]
        return ["README.md: gate output differs from a fresh run"
                + (f" ({len(stated)} lines stated, {len(actual)} printed)" if len(stated) != len(actual) else "")]\
            + diff[:3]
    return []


def check_forbidden(tree, private=PRIVATE):
    patterns = [(p, re.compile(p, re.I)) for p in GENERIC + [DOMAIN]]
    if private and os.path.isfile(private):
        for line in open(private, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.append(("<private pattern>", re.compile(line, re.I)))
    # The standard MIT text uses words the scan forbids elsewhere. Only lines that are exactly
    # the canonical MIT text are exempt, and only in LICENSE, so the file cannot hide anything else.
    mit_lines = {l.strip() for l in MIT_TEXT.splitlines() if l.strip()}
    errors = []
    for d, dirs, files in os.walk(tree):
        dirs[:] = [x for x in dirs if x not in (".git", "__pycache__")]  # never committed (.gitignore)
        for f in files:
            rel = os.path.relpath(os.path.join(d, f), tree)
            try:
                text = open(os.path.join(tree, rel), encoding="utf-8").read()
            except UnicodeDecodeError:
                errors.append(f"{rel}: not UTF-8 text; a binary file cannot be scanned, so it may not ship")
                continue
            in_block = False
            for n, line in enumerate(text.splitlines(), 1):
                if rel == SELF and line.startswith((BLOCK_START, BLOCK_END)):
                    in_block = line.startswith(BLOCK_START)
                    continue
                if in_block:
                    continue  # the pattern lists themselves, and nothing else in this file
                if rel == "LICENSE" and line.strip() in mit_lines:
                    continue
                for label, rx in patterns:
                    for m in rx.finditer(line):
                        if label == DOMAIN and m.group(0).lower() in DOMAIN_OK:
                            continue
                        shown = m.group(0) if label != "<private pattern>" else "<redacted>"
                        errors.append(f"{rel}:{n}: forbidden term {shown!r} ({label})")
                # Code uses capitals for constants, so the shape rule reads prose and config only.
                for m in ([] if rel.endswith(".py") else ACRONYM.finditer(line)):
                    if m.group(0) not in ACRONYM_OK:
                        errors.append(f"{rel}:{n}: unlisted all-capitals word {m.group(0)!r}: a possible vendor or "
                                      "hardware name; add it to ACRONYM_OK only if it is neither")
    return errors


CHECKS = [("links and fences", check_links_and_fences), ("results", check_results),
          ("forbidden terms", check_forbidden)]


def run(tree):
    if PRIVATE and os.path.isfile(PRIVATE):
        n = sum(1 for l in open(PRIVATE, encoding="utf-8") if l.strip() and not l.startswith("#"))
        print(f"INFO  forbidden terms: generic list and shape rules, plus {n} patterns from FORBIDDEN_TERMS_FILE")
    elif PRIVATE:
        print("FAIL  FORBIDDEN_TERMS_FILE is set but is not a file")  # a typo must not read as a clean scan
        return 1
    else:
        print("INFO  forbidden terms: generic list and shape rules only (FORBIDDEN_TERMS_FILE not set)")
    failed = 0
    for name, fn in CHECKS:
        errors = fn(tree)
        for e in errors[:30]:
            print(f"  FAIL {e}")
        print(f"{'FAIL' if errors else 'PASS'}  {name}")
        failed += bool(errors)
    return failed


def self_test():
    """Each mutant plants one defect in a scratch copy and must turn exactly the named check red."""
    def edit(path, old, new):
        def apply(tree):
            p = os.path.join(tree, path)
            text = open(p, encoding="utf-8").read()
            assert old in text, f"self-test anchor {old!r} not in {path}"
            open(p, "w", encoding="utf-8").write(text.replace(old, new, 1))
        return apply

    def append(path, text):
        return lambda tree: open(os.path.join(tree, path), "a", encoding="utf-8").write(text)

    cases = [
        ("control", None, None),
        # the varying counts are masked, so a different run's counts must stay green
        ("other run's concurrency counts", None,
         edit("README.md", "rewrites 60 (conflicts retried 5); reads 915", "rewrites 7 (conflicts retried 0); reads 3")),
        ("broken link", "links and fences", edit("README.md", "(minilake/synth.py)", "(minilake/missing.py)")),
        ("broken anchor", "links and fences", append("README.md", "\nSee [results](#no-such-heading).\n")),
        ("unclosed fence", "links and fences", append("README.md", "\n```text\nnever closed\n")),
        ("empty mermaid", "links and fences", append("README.md", "\n```mermaid\n```\n")),
        ("README figure drift", "results", edit("README.md", "exact_duplicate 3/3", "exact_duplicate 4/4")),
        ("README gate line dropped", "results",
         edit("README.md", "  PASS  auction labels   12 labelled closing_auction; ground truth 12 kept of 12 "
              "delivered; same rows: True; flagged: 0\n", "")),
        ("gate output missing", "results", edit("README.md", "```text\ngates      each runs", "```text\ngate      each runs")),
        # the code changes and the README does not: the planted count moves, the stated one stays
        ("code drift", "results", edit("minilake/gates.py", "same rows: {", "identical rows: {")),
        # Every planted term is made up, so the fixtures describe nothing real. Each is assembled
        # at run time, because this file is scanned too and must not match its own fixtures.
        ("generic word", "forbidden terms", append("minilake/raw.py", "\n# The job asked for 4096 thr" + "eads.\n")),
        ("size unit", "forbidden terms", append("README.md", "\nThe layer takes 999 " + "PB.\n")),
        ("vendor-shaped name", "forbidden terms", append("README.md", "\nData came from ACMEDATA.\n")),
        ("domain name", "forbidden terms", append("README.md", "\nSee acme-data" + ".com for files.\n")),
        ("allowed domain hiding another", "forbidden terms",
         append("README.md", "\nSee github" + ".com and acme-data" + ".com.\n")),
        ("private-list hardware word", "forbidden terms", append("README.md", "\nIt ran on a zzhw" + "term card.\n")),
        ("private-list access word", "forbidden terms",
         append("minilake/table.py", "\n# Researchers connect through zzaccess" + "term.\n")),
    ]
    base = tempfile.mkdtemp(prefix="minilake-docs-selftest-")
    repo = os.path.dirname(HERE)
    # A scratch private list proves the private-file path works whether or not the real one exists.
    private = os.path.join(base, "private-terms.txt")
    open(private, "w", encoding="utf-8").write("# scratch\n\\bzzhw" + "term\\b\n\\bzzaccess" + "term\\b\n")
    bad = 0
    try:
        for label, target, mutate in cases:
            tree = os.path.join(base, label.replace(" ", "-").replace("'", ""))
            shutil.copytree(repo, tree, ignore=shutil.ignore_patterns(".git", "__pycache__"))
            if mutate:
                mutate(tree)
            red = [name for name, fn in CHECKS
                   if (fn(tree, private) if fn is check_forbidden else fn(tree))]
            want = [target] if target else []
            ok = red == want
            bad += not ok
            print(f"  {'ok ' if ok else 'BAD'} {label}: red checks {red or 'none'}"
                  + ("" if ok else f", expected {want or 'none'}"))
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print(f"{'PASS' if not bad else 'FAIL'}  self-test ({len(cases)} cases, {bad} wrong)")
    return bad


def main():
    if sys.argv[1:] == ["--self-test"]:
        return 1 if self_test() else 0
    if sys.argv[1:]:
        print(__doc__.strip())
        return 2
    return 1 if run(os.path.dirname(HERE)) else 0


if __name__ == "__main__":
    sys.exit(main())
