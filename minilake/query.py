"""The query interface. Part of minilake, an independent demonstration system written only from the platform's
public write-up; not the platform's code.

fetch(lake, table, universe, from_, to, freq=None, snapshot=None, gaps=None, permit_future=False)
  - pins a snapshot (the latest version when none is named) and returns its number, so the same
    call can be repeated later on the same rows;
  - opens only the days in range (partition pruning), and only files that list a requested symbol;
  - raises rather than return an empty or silently partial result: an unknown symbol, a calendar
    day the snapshot does not hold (unless on_missing="skip", which names the days skipped), or an
    unknown snapshot;
  - never returns anything from at or after `to`. The read path hands over whole day partitions,
    later rows included, and as_of_guard() is the one place those rows are withheld. Gap handling
    that looks only backwards (gaps="carry_back") is allowed; gaps="closest" may look forwards and
    needs permit_future=True. Every filled bar says it was filled, and from which bar.
`to` is exclusive when it is a time, and covers the whole day when it is a date.
"""
import re
from collections import namedtuple

from . import pipeline, table
from .cleanse import session_of, to_exchange_time

Result = namedtuple("Result", "rows snapshot partitions_read skipped_days withheld")


class NoSuchInstrument(LookupError):
    pass


class MissingDay(LookupError):
    pass


class LookAhead(Exception):
    """A request that would use information from after the time it asks about."""


def bounds(from_, to):
    return (from_ if "T" in from_ else f"{from_}T00:00",
            to if "T" in to else f"{to}T24:00")  # ISO text sorts as time; T24:00 ends the day


def as_of_guard(rows, hi):
    """(rows before `hi`, how many were withheld): the only place the as-of bound is enforced.

    Compared as exchange-time instants: the typed tables still hold the vendor's UTC stamps, which as
    text sort before every exchange-time stamp of the day and would slip past a text comparison."""
    kept = [r for r in rows if to_exchange_time(r["ts"]) < hi]
    return kept, len(rows) - len(kept)


def fetch(lake, name, universe, from_, to, freq=None, snapshot=None, gaps=None, permit_future=False,
          on_missing="raise"):
    if not universe:
        raise NoSuchInstrument("an empty universe asks for nothing; name at least one symbol")
    if freq is not None and not re.fullmatch(r"[1-9][0-9]*m|1d", freq):
        raise ValueError(f"freq {freq!r}: use 'Nm' (minutes) or '1d'")
    tdir = pipeline.table_dir(lake, name)
    snapshot = table.latest(tdir) if snapshot is None else snapshot
    m = table.manifest(tdir, snapshot)
    lo, hi = bounds(from_, to)
    days = [d for d in pipeline.calendar(lake) if lo[:10] <= d and f"{d}T00:00" < hi]  # `to` is exclusive: a midnight `to` reads nothing of its own day
    skipped = sorted(set(days) - {e["partition"] for e in m["files"]})
    if skipped and on_missing == "raise":
        raise MissingDay(f"{', '.join(skipped)}: on the calendar, not in {name} snapshot {snapshot}")
    unknown = sorted(set(universe) - {s for e in m["files"] for s in e["symbols"]})
    if unknown:
        raise NoSuchInstrument(f"{', '.join(unknown)}: not in {name} snapshot {snapshot}")
    cols, opened = table.read(tdir, snapshot, partitions=set(days), symbols=universe)
    rows = [r for r in table.rows(cols) if r["symbol"] in universe and to_exchange_time(r["ts"]) >= lo]  # no upper bound here
    rows, withheld = as_of_guard(rows, hi)
    if freq:
        rows = bars(rows, freq, hi, gaps, permit_future)
    return Result(rows, snapshot, opened, skipped, withheld)


def _stamp(day, minute):
    return f"{day}T{minute // 60:02d}:{minute % 60:02d}"


def bars(rows, freq, hi, gaps=None, permit_future=False):
    """OHLCV bars ("Nm" minutes or "1d") from unflagged trades; only bars complete before `hi`.

    gaps="carry_back" fills an empty bar with the last earlier close; gaps="closest" with the
    nearest bar either side (an earlier one on a tie), which can read later data and so needs
    permit_future=True. A filled bar has volume 0 and names its source bar in filled_from."""
    if gaps == "closest" and not permit_future:
        raise LookAhead("gaps='closest' can copy a later bar into an earlier gap; pass permit_future=True")
    width = 1440 if freq == "1d" else int(freq[:-1])
    buckets = {}
    for r in sorted(rows, key=lambda r: r["ts"]):
        if not r["flags"]:  # a flagged print never makes a bar
            minute = int(r["ts"][11:13]) * 60 + int(r["ts"][14:16])
            buckets.setdefault((r["symbol"], r["ts"][:10]), {}).setdefault(minute // width * width, []).append(r)
    out = []
    for (symbol, day), by_start in sorted(buckets.items()):
        starts = sorted(by_start)
        real = {s: {"symbol": symbol, "start": _stamp(day, s), "open": b[0]["price"],
                    "high": max(x["price"] for x in b), "low": min(x["price"] for x in b),
                    "close": b[-1]["price"], "volume": sum(x["size"] for x in b), "filled_from": None}
                for s, b in by_start.items()}
        for s in range(starts[0], starts[-1] + 1, width):
            bar = real.get(s)
            if bar is None and gaps and session_of(_stamp(day, s)) != "out_of_hours":
                src = max(x for x in starts if x < s)
                if gaps == "closest":
                    src = min(starts, key=lambda x: (abs(x - s), x))
                value = real[src]["close"] if src < s else real[src]["open"]
                bar = {"symbol": symbol, "start": _stamp(day, s), "open": value, "high": value, "low": value,
                       "close": value, "volume": 0, "filled_from": _stamp(day, src)}
            if bar and _stamp(day, s + width) <= hi:
                out.append(bar)
    return out
