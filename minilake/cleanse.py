"""Layer 3 rules: label, flag or correct; remove only exact duplicates. Part of minilake, an
independent demonstration system written only from the platform's public write-up; not the platform's
code.

Every rule's hits are counted per day. A correction never overwrites silently: each corrected cell
is entered in the day's correction log with the vendor's value. layer_diff() then holds cleansing to
what the rules name: undo every logged correction, and the cleansed rows must equal the typed rows
minus exact duplicates; every label, flag and correction must match its rule and its count.
"""
from datetime import datetime

from .synth import EXCHANGE_UTC_OFFSET, SESSIONS

RULES = {
    "exact_duplicate": "drop: a byte-identical repeat (the only rule that removes rows)",
    "utc_stamp": "correct: a UTC stamp to exchange time; the vendor's value is logged",
    "closing_auction": "label: session = closing_auction (the second after 15:00:00)",
    "out_of_hours": "flag: printed outside every session",
    "off_band_price": "flag: over 20% from the symbol's previous unflagged trade",
    "crossed_quote": "flag: bid above ask",
}
FLAGS = ("out_of_hours", "off_band_price", "crossed_quote")
ADDED = ("session", "flags")  # columns cleansing adds to the typed ones
BAND = 0.20


def to_exchange_time(ts):
    """A UTC stamp (+00:00) as exchange time; any other stamp is returned unchanged."""
    if not ts.endswith("+00:00"):
        return ts
    return (datetime.fromisoformat(ts[:-6]) + EXCHANGE_UTC_OFFSET).isoformat(timespec="milliseconds")


CORRECTIONS = {"utc_stamp": ("ts", to_exchange_time)}  # rule -> (column, the declared correction)


def session_of(ts):
    t = ts[11:]
    if any(start <= t < end for start, end in SESSIONS):
        return "continuous"
    return "closing_auction" if "15:00" <= t < "15:00:01" else "out_of_hours"


def cleanse_day(kind, cols):
    """(cleansed columns, {rule: hits}, correction log) for one day of one table.

    A log entry is [row in the cleansed partition, column, vendor value, corrected value, rule]."""
    names = list(cols) + list(ADDED)
    hits, seen, kept, log, last = dict.fromkeys(RULES, 0), set(), [], [], {}
    for values in zip(*cols.values()):
        if values in seen:
            hits["exact_duplicate"] += 1
            continue
        seen.add(values)
        r = dict(zip(cols, values))
        for rule, (col, fix) in CORRECTIONS.items():
            if fix(r[col]) != r[col]:
                log.append([len(kept), col, r[col], fix(r[col]), rule])
                r[col] = fix(r[col])
                hits[rule] += 1
        r["session"] = session_of(r["ts"])
        hits["closing_auction"] += r["session"] == "closing_auction"
        ref = last.get(r["symbol"])
        tests = {"out_of_hours": r["session"] == "out_of_hours",
                 "off_band_price": kind == "trades" and ref is not None and abs(r["price"] / ref - 1) > BAND,
                 "crossed_quote": kind == "quotes" and r["bid"] > r["ask"]}
        r["flags"] = "|".join(rule for rule in FLAGS if tests[rule])
        for rule in FLAGS:
            hits[rule] += tests[rule]
        if kind == "trades" and not r["flags"]:
            last[r["symbol"]] = r["price"]
        kept.append(r)
    return {n: [r[n] for r in kept] for n in names}, hits, log


def layer_diff(typed, clean, hits, log):
    """Problems if cleansing changed anything its rules do not name; [] means it did not."""
    if sorted(clean) != sorted(list(typed) + list(ADDED)):
        return [f"cleansed columns {sorted(clean)} are not the typed columns plus {list(ADDED)}"]
    problems, names = [], list(typed)
    undone = {c: list(clean[c]) for c in names}
    for i, col, was, now, rule in log:
        if rule not in CORRECTIONS or CORRECTIONS[rule][0] != col or CORRECTIONS[rule][1](was) != now:
            problems.append(f"row {i}: {col} {was!r} -> {now!r} is not the declared correction {rule}")
        elif undone[col][i] != now:
            problems.append(f"row {i}: the log says {col} is {now!r}, the table holds {undone[col][i]!r}")
        undone[col][i] = was
    want = list(dict.fromkeys(zip(*(typed[c] for c in names))))  # typed rows, exact duplicates removed
    got = list(zip(*(undone[c] for c in names)))  # cleansed rows, logged corrections undone
    dropped = len(typed[names[0]]) - len(got)
    if dropped != hits["exact_duplicate"]:
        problems.append(f"{dropped} rows removed, but the counts name {hits['exact_duplicate']} exact duplicates")
    if got != want:
        i = next((i for i, (a, b) in enumerate(zip(got, want)) if a != b), min(len(got), len(want)))
        problems.append(f"row {i} differs from the typed layer in a way no rule names")
    for rule in CORRECTIONS:
        if sum(e[4] == rule for e in log) != hits[rule]:
            problems.append(f"the log holds a different number of {rule} corrections than the counts")
    wrong = sum(s != session_of(t) for s, t in zip(clean["session"], clean["ts"]))
    if wrong or clean["session"].count("closing_auction") != hits["closing_auction"]:
        problems.append(f"{wrong} session labels disagree with the session rule, or their count does")
    for rule in FLAGS:
        n = sum(rule in f.split("|") for f in clean["flags"])
        if n != hits[rule]:
            problems.append(f"{n} rows flagged {rule}, but the counts name {hits[rule]}")
    unknown = {x for f in clean["flags"] for x in f.split("|") if x} - set(FLAGS)
    if unknown:
        problems.append(f"flags no rule declares: {sorted(unknown)}")
    return problems
