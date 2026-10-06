"""Tests for minilake, an independent demonstration system written only from the platform's public
write-up; not the platform's code. One test class per invariant.
Run from the top of the repository:  python3 -m unittest discover -s tests
"""
import os
import shutil
import tempfile
import time
import unittest
from concurrent import futures
from unittest import mock

from minilake import cleanse, contend, gates, pipeline, query, raw, synth, table

TMP = LAKE = None


def setUpModule():
    global TMP, LAKE
    TMP = tempfile.mkdtemp(prefix="minilake-test-")
    LAKE = os.path.join(TMP, "lake")
    pipeline.build(LAKE, os.path.join(TMP, "incoming"))


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def scratch():
    return tempfile.mkdtemp(prefix="minilake-case-", dir=TMP)


class RawManifest(unittest.TestCase):
    def test_untouched_raw_layer_verifies(self):
        self.assertEqual(raw.verify(LAKE), [])

    def test_one_altered_byte_is_reported(self):
        lake = os.path.join(scratch(), "lake")
        shutil.copytree(os.path.join(LAKE, "raw"), os.path.join(lake, "raw"))
        name = sorted(raw.load_manifest(lake))[-1]
        os.chmod(raw.path_of(lake, name), 0o644)
        with open(raw.path_of(lake, name), "r+b") as fh:
            fh.seek(40)
            b = fh.read(1)
            fh.seek(40)
            fh.write(bytes([b[0] ^ 1]))
        self.assertEqual(raw.verify(lake), [f"{name}: bytes differ from the manifest"])

    def test_held_files_are_read_only_and_never_rewritten(self):
        name = sorted(raw.load_manifest(LAKE))[0]
        self.assertFalse(os.stat(raw.path_of(LAKE, name)).st_mode & 0o222)
        other = os.path.join(scratch(), name)
        with open(other, "w") as fh:
            fh.write("different bytes\n")
        with self.assertRaises(ValueError):
            raw.ingest(LAKE, [other])
        self.assertEqual(raw.ingest(LAKE, [raw.path_of(LAKE, name)]), [])  # same bytes: a no-op

    def test_two_overlapping_ingests_both_land_in_the_manifest(self):
        """Review 2026-10-06: with no lock the last manifest rename won and dropped the other's file."""
        base = scratch()
        srcs = []
        for n in ("p.csv", "q.csv"):
            srcs.append(os.path.join(base, n))
            with open(srcs[-1], "w") as fh:
                fh.write(f"bytes of {n}\n")
        lake = os.path.join(base, "lake")
        real = shutil.copyfile

        def slow(*a):
            time.sleep(0.2)  # widens the window in which both ingests have read the old manifest
            return real(*a)

        with mock.patch.object(raw.shutil, "copyfile", slow):
            with futures.ThreadPoolExecutor(2) as pool:
                for f in [pool.submit(raw.ingest, lake, [s]) for s in srcs]:
                    f.result()
        self.assertEqual(sorted(raw.load_manifest(lake)), ["p.csv", "q.csv"])
        self.assertEqual(raw.verify(lake), [])

    def test_an_empty_raw_layer_is_not_a_pass(self):
        lake = scratch()
        os.makedirs(os.path.join(lake, "raw"))
        self.assertTrue(raw.verify(lake))


class VersionedTable(unittest.TestCase):
    SCHEMA = {"symbol": "str", "n": "int"}

    def part(self, *values):
        return {"2024-03-04": {"symbol": ["SYM_A"] * len(values), "n": list(values)}}

    def test_a_publish_that_dies_before_its_rename_exposes_nothing(self):
        tdir = os.path.join(scratch(), "t")
        table.publish(tdir, self.SCHEMA, self.part(1))
        with mock.patch.object(table.os, "rename", side_effect=RuntimeError("killed")):
            with self.assertRaises(RuntimeError):
                table.publish(tdir, self.SCHEMA, self.part(2))
        self.assertEqual(table.versions(tdir), [1])
        self.assertEqual(table.read(tdir, 1)[0]["n"], [1])

    def test_a_visible_version_without_its_manifest_is_a_torn_read(self):
        tdir = os.path.join(scratch(), "t")
        table.publish(tdir, self.SCHEMA, self.part(1))
        os.mkdir(os.path.join(tdir, "v000002"))
        with self.assertRaises(table.TornRead):
            table.read(tdir, 2)

    def test_a_writer_that_loses_the_race_rebases_and_loses_nothing(self):
        tdir = os.path.join(scratch(), "t")
        table.publish(tdir, self.SCHEMA, self.part(1))
        real, calls = table.latest, []

        def stale_once(d):  # the second writer first sees the table before the first commit
            calls.append(d)
            return 0 if len(calls) == 1 else real(d)

        with mock.patch.object(table, "latest", stale_once):
            v = table.publish(tdir, self.SCHEMA, self.part(2))
        self.assertEqual((v, len(calls)), (2, 2))
        self.assertEqual(sorted(table.read(tdir, 2)[0]["n"]), [1, 2])

    def test_a_replace_computed_before_a_concurrent_append_conflicts(self):
        tdir = os.path.join(scratch(), "t")
        table.publish(tdir, self.SCHEMA, self.part(1))  # v1 holds the partition
        base = table.latest(tdir)  # writer A computes its replacement from v1 ...
        replacement = self.part(10)
        table.publish(tdir, self.SCHEMA, self.part(2))  # ... writer B appends, acknowledged as v2
        with self.assertRaises(table.Conflict):
            table.replace(tdir, self.SCHEMA, replacement, base)
        self.assertEqual(table.versions(tdir), [1, 2])
        self.assertEqual(sorted(table.read(tdir, 2)[0]["n"]), [1, 2])  # B's commit survives
        self.assertFalse([n for n in os.listdir(tdir) if n.startswith(".staging-")])

    def test_a_replace_cannot_be_issued_without_its_base(self):
        tdir = os.path.join(scratch(), "t")
        table.publish(tdir, self.SCHEMA, self.part(1))
        with self.assertRaises(TypeError):
            table.replace(tdir, self.SCHEMA, self.part(10))  # no base: refused before anything is written
        with self.assertRaises(TypeError):
            table.publish(tdir, self.SCHEMA, self.part(10), mode="replace")  # no replace through append
        self.assertEqual(table.versions(tdir), [1])
        self.assertFalse([n for n in os.listdir(tdir) if n.startswith(".staging-")])

    def test_a_replace_rebases_over_commits_to_other_partitions(self):
        tdir = os.path.join(scratch(), "t")
        table.publish(tdir, self.SCHEMA, self.part(1))
        table.publish(tdir, self.SCHEMA, {"2024-03-05": {"symbol": ["SYM_A"], "n": [7]}})
        v = table.replace(tdir, self.SCHEMA, self.part(10), base=1)
        self.assertEqual(sorted(table.read(tdir, v)[0]["n"]), [7, 10])

    def test_a_corrupted_file_is_refused_on_read(self):
        tdir = os.path.join(scratch(), "t")
        table.publish(tdir, self.SCHEMA, self.part(1, 2, 3))
        path = os.path.join(tdir, table.manifest(tdir, 1)["files"][0]["file"])
        with open(path, "rb") as fh:
            data = bytearray(fh.read())
        data[-5] ^= 1
        with open(path, "wb") as fh:
            fh.write(bytes(data))
        with self.assertRaises(table.TornRead):
            table.read(tdir, 1)

    def test_wrong_types_are_refused(self):
        with self.assertRaises(TypeError):
            table.publish(os.path.join(scratch(), "t"), self.SCHEMA, {"d": {"symbol": ["SYM_A"], "n": ["1"]}})

    def test_a_pinned_version_reads_the_same_after_new_publishes(self):
        tdir = os.path.join(scratch(), "t")
        table.publish(tdir, self.SCHEMA, self.part(1))
        before = table.read(tdir, 1)
        table.publish(tdir, self.SCHEMA, self.part(2))
        table.replace(tdir, self.SCHEMA, self.part(3), base=2)
        self.assertEqual(table.read(tdir, 1), before)
        self.assertEqual(table.read(tdir, 3)[0]["n"], [3])



def _day(kind, day, clean=False):
    name = kind + ("_clean" if clean else "")
    return table.read(pipeline.table_dir(LAKE, name), 1, partitions={day})[0]


def _meta(kind):
    return table.manifest(pipeline.table_dir(LAKE, kind + "_clean"), 1)["meta"]


class Cleansing(unittest.TestCase):
    def test_every_planted_problem_is_counted_exactly_as_planted(self):
        totals = dict.fromkeys(cleanse.RULES, 0)
        for kind in pipeline.SCHEMAS:
            for hits in _meta(kind)["rule_counts"].values():
                for rule, n in hits.items():
                    totals[rule] += n
        self.assertEqual({k: totals[k] for k in synth.PLANTED if k != "missing_day"},
                         {k: v for k, v in synth.PLANTED.items() if k != "missing_day"})

    def test_only_exact_duplicates_are_removed(self):
        for kind in pipeline.SCHEMAS:
            for day, hits in _meta(kind)["rule_counts"].items():
                removed = len(_day(kind, day)["ts"]) - len(_day(kind, day, clean=True)["ts"])
                self.assertEqual(removed, hits["exact_duplicate"], (kind, day))

    def test_closing_auction_prints_are_labelled_not_flagged(self):
        c = _day("trades", "2024-03-04", clean=True)
        auction = [i for i, s in enumerate(c["session"]) if s == "closing_auction"]
        self.assertEqual(sorted(c["trade_id"][i] for i in auction), [f"{s}-2024-03-04-auction" for s in synth.SYMBOLS])
        self.assertTrue(all(c["flags"][i] == "" for i in auction))

    def test_a_correction_is_logged_with_the_vendor_value(self):
        c, log = _day("trades", "2024-03-07", clean=True), _meta("trades")["corrections"]["2024-03-07"]
        self.assertEqual(len(log), synth.PLANTED["utc_stamp"])
        for i, col, was, now, rule in log:
            self.assertEqual((col, rule, c["ts"][i]), ("ts", "utc_stamp", now))
            self.assertEqual(synth.to_utc_text(now), was)  # the vendor's UTC stamp, the same instant
            self.assertEqual(c["session"][i], "continuous")  # corrected before it was labelled

    def test_the_off_band_print_is_flagged_and_kept(self):
        c = _day("trades", "2024-03-07", clean=True)
        self.assertEqual(sum("off_band_price" in f for f in c["flags"]), 1)

    def test_layer_diff_passes_on_the_real_output(self):
        for kind in pipeline.SCHEMAS:
            m = _meta(kind)
            for day, hits in m["rule_counts"].items():
                self.assertEqual(cleanse.layer_diff(_day(kind, day), _day(kind, day, clean=True), hits,
                                                    m["corrections"][day]), [])

    def test_layer_diff_catches_changes_no_rule_names(self):
        day, m = "2024-03-07", _meta("trades")
        t, hits, log = _day("trades", day), m["rule_counts"][day], m["corrections"][day]
        for column, change in (("price", lambda v: v + 0.01), ("ts", lambda v: v[:11] + "12:34:56.789"),
                               ("session", lambda v: "closing_auction"), ("flags", lambda v: "made_up")):
            c = _day("trades", day, clean=True)
            c[column][20] = change(c[column][20])
            self.assertTrue(cleanse.layer_diff(t, c, hits, log), column)
        c = _day("trades", day, clean=True)
        for col in c:
            del c[col][20]  # a silent drop of a row that is not a duplicate
        self.assertTrue(cleanse.layer_diff(t, c, hits, log))
        qm = _meta("quotes")
        qday = next(d for d, h in qm["rule_counts"].items() if h["crossed_quote"])
        q = _day("quotes", qday, clean=True)
        i = q["flags"].index("crossed_quote")
        q["flags"][i], q["flags"][i + 1] = q["flags"][i + 1], q["flags"][i]  # a flag moved to the next row
        self.assertTrue(cleanse.layer_diff(_day("quotes", qday), q, qm["rule_counts"][qday], qm["corrections"][qday]))
        wrong = [list(e) for e in log]
        wrong[0][2] = wrong[0][2].replace("+00:00", "+01:00")  # a logged correction that is not the rule
        self.assertTrue(cleanse.layer_diff(t, _day("trades", day, clean=True), hits, wrong))


class Query(unittest.TestCase):
    def test_partition_pruning_opens_only_the_days_asked_for(self):
        r = query.fetch(LAKE, "trades_clean", ["SYM_A"], "2024-03-05", "2024-03-05")
        self.assertEqual(r.partitions_read, ["2024-03-05"])
        self.assertEqual({x["ts"][:10] for x in r.rows}, {"2024-03-05"})

    def test_nothing_at_or_after_the_as_of_time_is_returned(self):
        to = "2024-03-05T10:00"
        r = query.fetch(LAKE, "trades_clean", ["SYM_A"], "2024-03-04", to)
        self.assertTrue(r.rows)
        self.assertLess(max(x["ts"] for x in r.rows), to)
        self.assertGreater(r.withheld, 0)  # the read path handed over later rows; the guard held them
        self.assertEqual(r.partitions_read, ["2024-03-04", "2024-03-05"])
        b = query.fetch(LAKE, "trades_clean", ["SYM_A"], "2024-03-05", "2024-03-05T09:47", freq="5m")
        self.assertEqual(max(x["start"] for x in b.rows), "2024-03-05T09:40")  # 09:45 is still open

    def test_utc_stamps_in_the_typed_table_are_bounded_as_instants(self):
        """Review 2026-10-06: compared as text, the vendor's +00:00 stamps (01:50 UTC = 09:50 exchange time)
        slipped past a 09:40 as-of bound and dropped out of the window they belong to."""
        early = query.fetch(LAKE, "trades", ["SYM_B"], "2024-03-07", "2024-03-07T09:40").rows
        self.assertFalse([r for r in early if cleanse.to_exchange_time(r["ts"]) >= "2024-03-07T09:40"])
        typed = query.fetch(LAKE, "trades", ["SYM_B"], "2024-03-07T09:45", "2024-03-07T10:30").rows
        clean = query.fetch(LAKE, "trades_clean", ["SYM_B"], "2024-03-07T09:45", "2024-03-07T10:30").rows
        self.assertTrue([r for r in typed if r["ts"].endswith("+00:00")])  # the case is really exercised
        self.assertEqual(len(typed), len(clean))

    def test_the_guard_is_what_holds_the_bound(self):
        with mock.patch.object(query, "as_of_guard", lambda rows, hi: (rows, 0)):
            r = query.fetch(LAKE, "trades_clean", ["SYM_A"], "2024-03-05", "2024-03-05T10:00")
        self.assertTrue([x for x in r.rows if x["ts"] >= "2024-03-05T10:00"])

    def test_carry_back_fills_only_from_earlier_bars(self):
        bars = query.fetch(LAKE, "trades_clean", ["SYM_A", "SYM_C"], "2024-03-05", "2024-03-05",
                           freq="1m", gaps="carry_back").rows
        filled = [b for b in bars if b["filled_from"]]
        self.assertTrue(filled)
        for b in filled:
            self.assertLess(b["filled_from"], b["start"])
            src = next(x for x in bars if x["symbol"] == b["symbol"] and x["start"] == b["filled_from"])
            self.assertIsNone(src["filled_from"])
            for k in ("open", "high", "low", "close"):
                self.assertEqual(b[k], src["close"])

    def test_closest_needs_an_opt_in_and_says_it_filled(self):
        kw = dict(universe=["SYM_A"], from_="2024-03-04", to="2024-03-04", freq="1m", gaps="closest")
        with self.assertRaises(query.LookAhead):
            query.fetch(LAKE, "trades_clean", **kw)
        bars = query.fetch(LAKE, "trades_clean", permit_future=True, **kw).rows
        filled = [b for b in bars if b["filled_from"]]
        self.assertTrue(any(b["filled_from"] > b["start"] for b in filled))  # it did look forwards
        self.assertTrue(all(b["volume"] == 0 for b in filled))

    def test_flagged_prints_never_make_a_bar(self):
        bars = query.fetch(LAKE, "trades_clean", ["SYM_A"], "2024-03-04", "2024-03-04", freq="1m").rows
        self.assertFalse([b for b in bars if b["start"] >= "2024-03-04T15:01"])  # the evening print

    def test_missing_data_raises_instead_of_returning_less(self):
        with self.assertRaises(query.MissingDay):
            query.fetch(LAKE, "trades_clean", ["SYM_A"], "2024-03-04", "2024-03-08")
        with self.assertRaises(query.NoSuchInstrument):
            query.fetch(LAKE, "trades_clean", ["SYM_Z"], "2024-03-04", "2024-03-04")
        with self.assertRaises(table.NoSuchSnapshot):
            query.fetch(LAKE, "trades_clean", ["SYM_A"], "2024-03-04", "2024-03-04", snapshot=9)
        with self.assertRaises(table.NoSuchSnapshot):  # 0 is a version number, not "latest"
            query.fetch(LAKE, "trades_clean", ["SYM_A"], "2024-03-04", "2024-03-04", snapshot=0)
        with self.assertRaises(query.NoSuchInstrument):
            query.fetch(LAKE, "trades_clean", [], "2024-03-04", "2024-03-04")
        for freq in ("2d", "1h", "m", "0m"):
            with self.assertRaises(ValueError, msg=freq):
                query.fetch(LAKE, "trades_clean", ["SYM_A"], "2024-03-04", "2024-03-04", freq=freq)
        r = query.fetch(LAKE, "trades_clean", ["SYM_A"], "2024-03-04", "2024-03-08", on_missing="skip")
        self.assertEqual(r.skipped_days, [synth.MISSING_DAY])


class Concurrency(unittest.TestCase):
    def test_no_lost_commit_and_no_torn_read_with_a_rewriter(self):
        s = contend.run(scratch(), readers=3, writers=3, commits=8, batch=20)
        self.assertEqual((s["committed"], s["lost"], s["torn"], s["crashed"], s["hung"]), (24, 0, 0, 0, 0))
        self.assertGreater(s["reads"], 0)
        self.assertGreater(s["rewrites"], 0)
        self.assertTrue(s["pinned_unchanged"])

    def test_a_crashed_writer_fails_the_run_instead_of_stalling_it(self):
        s = contend.run(scratch(), readers=2, writers=2, commits=5, batch=10, timeout=30, fault="crash")
        self.assertEqual((s["crashed"], s["hung"]), (1, 0))
        self.assertEqual(s["errors"], ["RuntimeError: injected writer crash"])
        self.assertEqual(s["committed"], 7)  # writer 0 acknowledged 2 commits before it crashed

    def test_a_hung_writer_is_stopped_at_the_deadline_and_fails_the_run(self):
        s = contend.run(scratch(), readers=1, writers=1, commits=5, batch=10, timeout=3, fault="hang")
        self.assertEqual(s["hung"], 1)

    def test_the_controls_catch_their_defects(self):
        self.assertEqual(contend.controls(scratch()), {"torn read caught": True, "stale replace refused": True})


class Gates(unittest.TestCase):
    def test_every_gate_passes_on_a_fresh_lake(self):
        with mock.patch("builtins.print"):
            self.assertEqual(gates.main(), 0)
