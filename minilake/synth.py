"""A SYNTHETIC tick delivery with planted problems. Part of minilake, an independent demonstration
system written only from the platform's public write-up; not the platform's code. Nothing here is
market data.

Planted, and each must be found by the later layers exactly as often as it was planted:
  exact_duplicate  trade rows delivered twice, byte for byte, on the second day
  out_of_hours     one trade printed in the evening of the first day
  off_band_price   one trade at half the price of the trade before it
  crossed_quote    one quote whose bid is above its ask
  utc_stamp        a few trades stamped in UTC (with a +00:00 offset) instead of exchange time,
                   a value cleansing corrects
  missing_day      one calendar day with no delivery at all
Closing-auction trades, stamped a fraction after 15:00:00, are planted as well (one per symbol per
day, their trade_id ending "-auction"). They are not a problem: they must be labelled, never dropped.
"""
import csv
import os
import random
from datetime import datetime, timedelta

SYMBOLS = ("SYM_A", "SYM_B", "SYM_C")
CALENDAR = ("2024-03-04", "2024-03-05", "2024-03-06", "2024-03-07", "2024-03-08")
MISSING_DAY = "2024-03-06"
EXCHANGE_UTC_OFFSET = timedelta(hours=8)  # exchange time = UTC + 8 hours
SESSIONS = (("09:30", "11:30"), ("13:00", "15:00"))  # continuous trading, start inclusive
FIELDS = {"trades": ("ts", "symbol", "price", "size", "trade_id"),
          "quotes": ("ts", "symbol", "bid", "ask", "bid_size", "ask_size")}
PLANTED = {"exact_duplicate": 3, "out_of_hours": 1, "off_band_price": 1, "crossed_quote": 1,
           "utc_stamp": 4, "closing_auction": len(SYMBOLS) * (len(CALENDAR) - 1), "missing_day": 1}
PER_SYMBOL_DAY = 120


def _clock(rng):
    """A random time inside a continuous session, as HH:MM:SS.mmm."""
    start, end = rng.choice(SESSIONS)
    lo = (int(start[:2]) * 60 + int(start[3:])) * 60_000
    hi = (int(end[:2]) * 60 + int(end[3:])) * 60_000
    ms = rng.randrange(lo, hi)
    return f"{ms // 3_600_000:02d}:{ms // 60_000 % 60:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}"


def to_utc_text(local_ts):
    """Exchange time as the same instant in UTC, with an explicit offset."""
    utc = datetime.fromisoformat(local_ts) - EXCHANGE_UTC_OFFSET
    return utc.isoformat(timespec="milliseconds") + "+00:00"


def _day(rng, d, day):
    trades, quotes = [], []
    for s, sym in enumerate(SYMBOLS):
        mid = 10.0 + 5 * s + d / 10
        for i, t in enumerate(sorted(_clock(rng) for _ in range(PER_SYMBOL_DAY))):
            mid = max(1.0, mid + rng.choice((-0.01, 0.0, 0.01)))
            trades.append([f"{day}T{t}", sym, f"{mid:.2f}", rng.randrange(1, 50) * 100, f"{sym}-{day}-{i}"])
        auction = f"{day}T15:00:00.{rng.randrange(10, 990):03d}"
        trades.append([auction, sym, f"{mid:.2f}", rng.randrange(10, 200) * 100, f"{sym}-{day}-auction"])
        for t in sorted(_clock(rng) for _ in range(PER_SYMBOL_DAY)):
            bid = round(mid - rng.choice((0.01, 0.02)), 2)
            quotes.append([f"{day}T{t}", sym, f"{bid:.2f}", f"{bid + 0.01 * rng.randrange(1, 4):.2f}",
                           rng.randrange(1, 90) * 100, rng.randrange(1, 90) * 100])
    return trades, quotes


def generate(out_dir, seed=7):
    """Write one trades file and one quotes file per delivered day; return their paths."""
    rng = random.Random(seed)
    os.makedirs(out_dir, exist_ok=True)
    days = [day for day in CALENDAR if day != MISSING_DAY]
    paths = []
    for d, day in enumerate(days):
        trades, quotes = _day(rng, d, day)
        if d == 0:
            trades.append([f"{day}T19:42:10.000", "SYM_A", trades[0][2], 100, f"SYM_A-{day}-late"])
        if d == 1:
            trades += [list(row) for row in rng.sample(trades, PLANTED["exact_duplicate"])]
        if d == 2:
            trades[5][2] = f"{float(trades[5][2]) / 2:.2f}"
            for row in [r for r in trades if r[1] == "SYM_B"][10:10 + PLANTED["utc_stamp"]]:
                row[0] = to_utc_text(row[0])
        if d == 3:
            quotes[7][2], quotes[7][3] = quotes[7][3], quotes[7][2]  # ask > bid, so now strictly crossed
        for kind, rows in (("trades", trades), ("quotes", quotes)):
            path = os.path.join(out_dir, f"{kind}_{day}.csv")
            with open(path, "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(FIELDS[kind])
                w.writerows(sorted(rows, key=lambda r: (r[0], r[1])))
            paths.append(path)
    return paths
