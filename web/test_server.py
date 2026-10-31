#!/usr/bin/env python3
"""Tests for the cpcpub results hub.

    python3 web/test_server.py

Runs a real server on an ephemeral port against a temporary database, so the
HTTP layer, the validation and the SQL are all exercised together.
"""

import hashlib
import http.client
import http.cookiejar
import itertools
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

import downloads
import server as srv

# A fixture has to be a document the benchmark could have written. The hub
# recomputes `score`, `ilp`, `filp` and `mlp` from the numbers they are made of
# and refuses an upload that disagrees with itself, so a record carrying a score
# typed in independently of its own measurements is no longer a valid input --
# it is the thing the check exists to catch.
#
# A test that wants a row to rank at a chosen score therefore still asks for it,
# and the throughput components are scaled until their geometric mean lands
# there. The test keeps saying what it meant and the document stays honest.

# The four score components a fixture scales. The other two -- the memory term
# and the dispatch span -- are left alone: one is a latency that would have to
# move the wrong way and the other cannot go negative, and four are enough.
SCORE_INPUTS = ("int_thr_mops", "fp_thr_mflops", "mul_thr_mmul_s",
                "disp_thr_mcall_s")


def _cols(rec):
    """The record under the hub's column names, which its helpers read."""
    return {col: rec.get(jkey) for jkey, col, _ in srv.CORE_FIELDS}


def _score_of(rec, threads=1.0):
    return srv._geomean(srv.score_terms(_cols(rec), threads))


def _tie(rec, asked, ratio, top, bottom, move):
    """Keep `ratio` equal to top/bottom.

    A test that pinned the ratio keeps it and `move` shifts to match -- always
    the field of the three that is not one of the score's own inputs, so tying
    a ratio cannot quietly move the score. Otherwise the ratio follows the two
    measurements it is the ratio of.
    """
    if asked.get(ratio) is not None:
        r = float(asked[ratio])
        other = rec.get(bottom if move == top else top)
        if not isinstance(other, (int, float)) or not r:
            rec[move] = None
        else:
            rec[move] = r * other if move == top else other / r
        return
    t, b = rec.get(top), rec.get(bottom)
    rec[ratio] = (t / b if isinstance(t, (int, float)) and isinstance(b, (int, float))
                  and b else None)


def core(scope="cpu", cpu: int | None = 0, *, threads=1.0, **over):
    rec: dict[str, object] = {
        "scope": scope, "cpu": cpu, "mhz": 4000.0, "mhz_src": "measured",
        "int_lat_mops": 4000.0, "int_thr_mops": 20000.0, "ilp": None,
        "mul_thr_mmul_s": 9000.0, "fp_lat_mflops": 1500.0,
        "fp_thr_mflops": 11000.0, "filp": None, "mem_gbps": 30.0,
        "mem_lat_ns": 100.0, "mem_lat8_ns": 12.0, "mlp": None,
        "disp_thr_mcall_s": 1000.0, "disp_cap_calls": 500.0,
        "disp_prediction": "measured", "disp_gain": 5.0, "disp_span": 7.0,
        "score": None, "fp32_lat_mflops": 1500.0, "fp32_thr_mflops": 11000.0,
    }
    rec.update(over)

    # Scale to the score the caller asked for, if it asked for one. Four of the
    # six terms move, so a geometric mean over six of them shifts by m^(4/6) --
    # which is the exponent below, undoing it.
    want = over.get("score")
    if want is not None:
        now = _score_of(rec, threads)
        if now > 0.0:
            m = (float(want) / now) ** 1.5
            for key in SCORE_INPUTS:
                if isinstance(rec.get(key), (int, float)):
                    rec[key] *= m

    _tie(rec, over, "ilp", "int_thr_mops", "int_lat_mops", "int_lat_mops")
    _tie(rec, over, "filp", "fp_thr_mflops", "fp_lat_mflops", "fp_lat_mflops")
    _tie(rec, over, "mlp", "mem_lat_ns", "mem_lat8_ns", "mem_lat_ns")
    rec["score"] = _score_of(rec, threads) or None
    return rec


# Every fixture is a document of its own. The hub refuses a document it
# already holds, and a real run never repeats one -- its timings differ every
# time -- so tests that upload "another run" get one, by a key the hub ignores.
_fixtures = itertools.count(1)


def document(mode="per-core", **over):
    doc = {
        "schema": "cpu-bench/1",
        "fixture": next(_fixtures),
        "mode": mode,
        "build": {"compiler": "gcc 15.2.0", "compiler_version": "15.2.0",
                  "cc": "cc", "flags": "-O3 -std=c2x -fno-tree-vectorize",
                  "target": "x86_64", "vectorize": False, "fma": False},
        "system": {"sysname": "Linux", "release": "7.0.0", "machine": "x86_64",
                   "cpus": 8, "cpu_models": "Test CPU 9000"},
        "config": {"threads": 1, "seconds_per_phase": 0.5, "reps": 3,
                   "warmup_seconds": 0.15, "mem_bytes_per_thread": 33554432,
                   "pin": True, "clock": "raw", "seed": 1},
    }
    if mode in ("per-core", "full"):
        doc["cores"] = [core(cpu=0), core(cpu=1, score=15000.0)]
    if mode in ("threads", "full"):
        doc["threads"] = [core("thread", 0), core("thread", 1)]
        # The whole-machine score counts one set of pointer chases per thread,
        # so it is built knowing how many the document says there were.
        doc["total"] = core("total", None, threads=doc["config"]["threads"])
        doc["checksum"] = "0xdeadbeefdeadbeef"
    doc.update(over)
    return doc


class MergeListTest(unittest.TestCase):
    """CPU lines an operator has said are another CPU."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "m.sqlite3")
        self.aliases = Path(self.tmp.name) / "aliases.json"

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, store, cpu_models, scores):
        doc = document()
        doc["system"].update(cpu_models=cpu_models, cpus=len(scores))
        doc["cores"] = [core(cpu=i, score=s) for i, s in enumerate(scores)]
        run, cores = srv.validate(doc)
        return store.insert(run, cores, json.dumps(doc), None, None)[0]

    def groups(self, store):
        rows, _ = store.cpu_groups(sort="score", desc=True, norm="abs", limit=50,
                                   offset=0, scope="cpu", search="Mrg")
        return sorted((g["cpu_models"], g["cpu_layout"], g["runs"], tuple(g["boards"]))
                      for g in rows)

    def test_an_alias_joins_its_cpu(self):
        big_little = [8000.0] * 2 + [20000.0] * 2
        self.aliases.write_text(json.dumps([
            {"line": "Mrg Vendor Chip", "cpu": "Mrg A + Mrg B",
             "layout": "2x Mrg A + 2x Mrg B"}]))
        store = srv.Store(self.db, self.aliases)
        self.put(store, "Mrg A + Mrg B (Board One)", big_little)
        self.put(store, "Mrg Vendor Chip (Board Two)", big_little)
        self.assertEqual(self.groups(store),
                         [("Mrg A + Mrg B", "2x Mrg A + 2x Mrg B", 2, ("Board One", "Board Two"))])

    def test_the_list_is_read_again_at_start(self):
        store = srv.Store(self.db, self.aliases)
        self.put(store, "Mrg A + Mrg B (Board One)", [8000.0] * 2 + [20000.0] * 2)
        self.put(store, "Mrg Vendor Chip (Board Two)", [8000.0] * 2 + [20000.0] * 2)
        self.assertEqual(len(self.groups(store)), 2)
        self.aliases.write_text(json.dumps([
            {"line": "Mrg Vendor Chip", "cpu": "Mrg A + Mrg B",
             "layout": "2x Mrg A + 2x Mrg B"}]))
        self.assertEqual(len(self.groups(srv.Store(self.db, self.aliases))), 1)

    def test_rank_is_against_other_runs_not_their_cores(self):
        # Three runs of four cores each: the middle run's best core beats one
        # run and loses to one, however many cores each of them has.
        store = srv.Store(self.db, self.aliases)
        ids = [self.put(store, "Rnk CPU", [s, s / 2, s / 3, s / 4])
               for s in (10000.0, 20000.0, 30000.0)]
        pct = store.rank(ids[1])["metrics"]["score"]
        self.assertEqual(pct["population"], 2)
        self.assertEqual(pct["percentile"], 50.0)
        # The best beats both.
        self.assertEqual(store.rank(ids[2])["metrics"]["score"]["percentile"], 100.0)

    def test_a_device_tree_id_beside_a_name_is_dropped(self):
        self.assertEqual(srv.tidy_line("Spacemit(R) X60 + spacemit,x60 (LPi3A Board)"),
                         "Spacemit(R) X60 (LPi3A Board)")
        self.assertEqual(srv.tidy_line("Ky(R) X1 + ky,x60"), "Ky(R) X1")
        # The only name a line has stays, and so does a line of plain names.
        self.assertEqual(srv.tidy_line("thead,c906 (Milk-V Duo S)"), "thead,c906 (Milk-V Duo S)")
        self.assertEqual(srv.tidy_line("Cortex-A55 + Cortex-A76"), "Cortex-A55 + Cortex-A76")

    def test_stored_lines_are_tidied_at_start(self):
        store = srv.Store(self.db, self.aliases)
        run_id = self.put(store, "Dt X60 (Dt Board)", [9000.0] * 4)
        with store.connect() as db:
            db.execute("UPDATE runs SET cpu_models = ? WHERE id = ?",
                       ("Dt X60 + dt,x60 (Dt Board)", run_id))
        store = srv.Store(self.db, self.aliases)
        with store.connect() as db:
            row = db.execute("SELECT cpu_models, cpu_key FROM runs WHERE id = ?",
                             (run_id,)).fetchone()
        self.assertEqual((row["cpu_models"], row["cpu_key"]),
                         ("Dt X60 (Dt Board)", "Dt X60 (Dt Board)"))

    def test_the_shipped_list_joins_ky_x1_to_spacemit_x60(self):
        store = srv.Store(self.db, srv.ALIASES_PATH)
        eight = [9000.0] * 8
        self.put(store, "Ky(R) X1 + ky,x60 (ky x1 orangepi-rv2 board)", eight)
        self.put(store, "Spacemit(R) X60 + spacemit,x60 (SiPEED LPi3A Board)", eight)
        rows, _ = store.cpu_groups(sort="score", desc=True, norm="abs", limit=50,
                                   offset=0, scope="cpu", search="X")
        self.assertEqual([(g["cpu_models"], g["runs"]) for g in rows],
                         [("Spacemit(R) X60", 2)])

    def test_a_bad_list_stops_the_hub(self):
        self.aliases.write_text(json.dumps([{"line": "X"}]))
        with self.assertRaises(ValueError):
            srv.Store(self.db, self.aliases)


class HubTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.server = srv.build_server(str(Path(cls.tmp.name) / "t.sqlite3"),
                                      "127.0.0.1", 0, rate_limit=0)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    # -- helpers ----------------------------------------------------------
    def req(self, path, method="GET", body=None, headers=None, raw=False):
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
        r = urllib.request.Request(self.base + path, data=data, method=method,
                                   headers=headers or {})
        if data is not None:
            r.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(r) as resp:
            payload = resp.read()
            return resp.status, (payload if raw else json.loads(payload))

    def expect_error(self, path, method="GET", body=None, headers=None):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.req(path, method, body, headers)
        return cm.exception.code, json.loads(cm.exception.read())

    def upload(self, doc=None, query=""):
        status, out = self.req("/api/runs" + query, "POST", doc or document())
        self.assertEqual(status, 201)
        return out

    # -- tests ------------------------------------------------------------
    def test_upload_and_fetch(self):
        out = self.upload()
        self.assertEqual(out["records"], 2)
        status, run = self.req(f"/api/runs/{out['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(run["cpu_models"], "Test CPU 9000")
        self.assertEqual(run["target"], "x86_64")
        self.assertEqual(run["vectorize"], 0)
        self.assertEqual(len(run["cores"]), 2)
        self.assertAlmostEqual(run["cores"][0]["int_thr"], 20000.0)

    def test_threads_mode_keeps_total_row(self):
        out = self.upload(document("threads"))
        _, run = self.req(f"/api/runs/{out['id']}")
        scopes = sorted(c["scope"] for c in run["cores"])
        self.assertEqual(scopes, ["thread", "thread", "total"])
        self.assertEqual(run["checksum"], "0xdeadbeefdeadbeef")

    def test_full_mode_keeps_both_phases(self):
        out = self.upload(document("full"))
        self.assertEqual(out["records"], 5)     # 2 cores + 2 threads + total
        _, run = self.req(f"/api/runs/{out['id']}")
        self.assertEqual(run["mode"], "full")
        counts = {}
        for c in run["cores"]:
            counts[c["scope"]] = counts.get(c["scope"], 0) + 1
        self.assertEqual(counts, {"cpu": 2, "thread": 2, "total": 1})
        # A full run is ranked against the per-core population, not per-thread.
        _, rank = self.req(f"/api/runs/{out['id']}/rank")
        self.assertEqual(rank["scope"], "cpu")

    def test_build_string_is_recorded(self):
        out = self.upload()
        _, run = self.req(f"/api/runs/{out['id']}")
        self.assertEqual(run["compiler"], "gcc 15.2.0")
        self.assertEqual(run["compiler_version"], "15.2.0")
        self.assertEqual(run["cc"], "cc")
        self.assertEqual(run["build_flags"], "-O3 -std=c2x -fno-tree-vectorize")

    def test_full_run_needs_at_least_one_result_array(self):
        doc = document("full")
        del doc["cores"]
        del doc["threads"]
        del doc["total"]
        code, err = self.expect_error("/api/runs", "POST", doc)
        self.assertEqual(code, 400)
        self.assertIn("result array", err["error"])

    def test_raw_document_is_preserved(self):
        doc = document()
        out = self.upload(doc)
        _, body = self.req(f"/api/runs/{out['id']}/raw", raw=True)
        self.assertEqual(json.loads(body), doc)

    def test_leaderboard_sorts_and_filters(self):
        self.upload()
        _, out = self.req("/api/cores?scope=cpu&sort=score&order=desc")
        scores = [c["score"] for c in out["cores"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        _, out = self.req("/api/cores?scope=cpu&target=riscv64")
        self.assertEqual(out["cores"], [])
        _, out = self.req("/api/cores?scope=cpu&q=Test+CPU")
        self.assertTrue(out["cores"])

    def test_board_rows_carry_the_operating_system(self):
        """The board has an OS column, so the listing has to answer with one."""
        self.upload()
        _, out = self.req("/api/cores?scope=cpu&limit=1")
        self.assertEqual(out["cores"][0]["sysname"], "Linux")

    def test_the_os_filter_narrows_to_one_system(self):
        """Matched case-insensitively: a hand-written link is not a typo."""
        self.upload()
        self.upload(document(system={"sysname": "Windows", "release": "10",
                                     "machine": "x86_64", "cpus": 8,
                                     "cpu_models": "Test CPU 9000"}))
        _, out = self.req("/api/cores?scope=cpu&os=Windows")
        self.assertTrue(out["cores"])
        self.assertEqual({c["sysname"] for c in out["cores"]}, {"Windows"})
        _, lower = self.req("/api/cores?scope=cpu&os=windows")
        self.assertEqual(len(lower["cores"]), len(out["cores"]))
        _, none = self.req("/api/cores?scope=cpu&os=Plan9")
        self.assertEqual(none["cores"], [])

    def test_stats_counts_the_systems_it_holds(self):
        """What the OS picker is drawn from -- there is no fixed list of them."""
        _, before = self.req("/api/stats")
        was = {row["sysname"]: row["n"] for row in before["systems"]}
        self.upload()
        _, after = self.req("/api/stats")
        now = {row["sysname"]: row["n"] for row in after["systems"]}
        self.assertEqual(now.get("Linux", 0), was.get("Linux", 0) + 1)

    def test_latency_metric_sorts_ascending(self):
        self.upload()
        _, out = self.req("/api/cores?scope=cpu&sort=mem_lat_ns&order=asc&limit=5")
        lats = [c["mem_lat_ns"] for c in out["cores"]]
        self.assertEqual(lats, sorted(lats))

    def test_fp32_is_a_metric_marked_outside_the_score(self):
        """Listed for the page, with the reason it is set apart; not a default column."""
        _, out = self.req("/api/metrics")
        fp32 = {m["key"]: m for m in out["metrics"] if m["key"].startswith("fp32")}
        self.assertEqual(set(fp32), {"fp32_lat", "fp32_thr"})
        for m in fp32.values():
            self.assertIn("not in the score", m["aside"])
            self.assertFalse(m.get("headline"))
        # Nothing the score is made of is marked aside.
        self.assertFalse(any(m.get("aside") for m in out["metrics"]
                             if not m["key"].startswith("fp32")))

    def test_the_board_sorts_on_fp32_with_older_runs_last(self):
        """A run from before FP32 has none, and ranks below every run that does."""
        old = self.upload(document(cores=[core(cpu=0, fp32_lat_mflops=None,
                                               fp32_thr_mflops=None)]))
        low = self.upload(document(cores=[core(cpu=0, fp32_thr_mflops=500.0)]))
        high = self.upload(document(cores=[core(cpu=0, fp32_thr_mflops=90000.0)]))
        _, out = self.req("/api/cores?scope=cpu&sort=fp32_thr&order=desc&limit=200")
        ids = [c["run_id"] for c in out["cores"]]
        self.assertLess(ids.index(high["id"]), ids.index(low["id"]))
        self.assertLess(ids.index(low["id"]), ids.index(old["id"]))
        self.assertIsNone(out["cores"][ids.index(old["id"])]["fp32_thr"])

    # -- one upload, one row ----------------------------------------------
    def test_upload_takes_one_leaderboard_row(self):
        """A machine with 8 cores is one result, not eight."""
        cores = [core(cpu=i, score=1000.0 + i) for i in range(8)]
        out = self.upload(document("per-core", cores=cores))
        self.assertEqual(out["records"], 8)
        _, board = self.req("/api/cores?scope=cpu&sort=score&order=desc&limit=200")
        mine = [c for c in board["cores"] if c["run_id"] == out["id"]]
        self.assertEqual(len(mine), 1)
        # The row stands for the whole upload and says how many it covers.
        self.assertEqual(mine[0]["records"], 8)

    def test_the_row_is_the_best_record_at_the_sorted_metric(self):
        cores = [core(cpu=0, score=1000.0, mem_lat_ns=300.0),
                 core(cpu=1, score=9000.0, mem_lat_ns=200.0),
                 core(cpu=2, score=5000.0, mem_lat_ns=100.0)]
        out = self.upload(document("per-core", cores=cores))

        def row(sort, order):
            _, b = self.req(f"/api/cores?scope=cpu&sort={sort}&order={order}&limit=200")
            return next(c for c in b["cores"] if c["run_id"] == out["id"])

        best = row("score", "desc")
        self.assertEqual(best["cpu"], 1)
        # Every column comes from that one core, so the row is a real
        # measurement rather than a per-metric best of several.
        self.assertEqual(best["mem_lat_ns"], 200.0)
        quickest = row("mem_lat_ns", "asc")
        self.assertEqual(quickest["cpu"], 2)
        # Not an exact equality: a fixture reaches a chosen score by scaling the
        # components the score is the geometric mean of, so it arrives within a
        # float rounding of it rather than on it.
        self.assertAlmostEqual(quickest["score"], 5000.0, places=6)

    # -- per GHz ------------------------------------------------------------
    def test_per_ghz_ranks_by_the_per_ghz_figure(self):
        """The board is ordered by the values it is printing.

        Per GHz is a reading of the same numbers, not a second set of them, so
        the ranking has to follow it: a column of figures the eye can see is
        out of order is the bug, whatever the underlying values do.
        """
        fast = self.upload(document(label="perghz-fast", cores=[
            core(cpu=0, mhz=5000.0, score=20000.0)]))       # 4000 per GHz
        frugal = self.upload(document(label="perghz-frugal", cores=[
            core(cpu=0, mhz=2000.0, score=12000.0)]))       # 6000 per GHz

        def order(norm):
            _, b = self.req(f"/api/cores?scope=cpu&q=perghz&sort=score"
                            f"&order=desc&norm={norm}&limit=10")
            return [c["run_id"] for c in b["cores"]]

        self.assertEqual(order("abs"), [fast["id"], frugal["id"]])
        self.assertEqual(order("ghz"), [frugal["id"], fast["id"]])

    def test_per_ghz_turns_a_latency_into_cycles(self):
        """A `time` metric multiplies by the clock rather than dividing."""
        slow = self.upload(document(label="cycles-slow", cores=[
            core(cpu=0, mhz=2000.0, mem_lat_ns=100.0)]))     # 200 cycles
        quick = self.upload(document(label="cycles-quick", cores=[
            core(cpu=0, mhz=5000.0, mem_lat_ns=60.0)]))      # 300 cycles

        def order(norm):
            _, b = self.req(f"/api/cores?scope=cpu&q=cycles-&sort=mem_lat_ns"
                            f"&order=asc&norm={norm}&limit=10")
            return [c["run_id"] for c in b["cores"]]

        self.assertEqual(order("abs"), [quick["id"], slow["id"]])
        self.assertEqual(order("ghz"), [slow["id"], quick["id"]])

    def test_a_clock_independent_metric_does_not_move(self):
        """`fixed` and `ratio` metrics rank the same either way.

        MEM is the memory controller's figure, not the core's, so dividing it
        by a core clock would only flatter whichever core was clocked lower
        while measuring the same DRAM. The page does not do it and neither
        does the ordering.
        """
        wide = self.upload(document(label="fixed-wide", cores=[
            core(cpu=0, mhz=5000.0, mem_gbps=30.0, ilp=6.0)]))
        narrow = self.upload(document(label="fixed-narrow", cores=[
            core(cpu=0, mhz=1000.0, mem_gbps=20.0, ilp=4.0)]))

        for sort in ("mem_gbps", "ilp"):
            for norm in ("abs", "ghz"):
                with self.subTest(sort=sort, norm=norm):
                    _, b = self.req(f"/api/cores?scope=cpu&q=fixed-&sort={sort}"
                                    f"&order=desc&norm={norm}&limit=10")
                    self.assertEqual([c["run_id"] for c in b["cores"]],
                                     [wide["id"], narrow["id"]])

    def test_per_ghz_stands_a_run_up_by_its_most_efficient_record(self):
        """Which record represents an upload follows the reading too.

        Ranked per GHz, a machine is on the board for its most efficient core,
        not its fastest -- otherwise the row shown is one the ranking did not
        pick, and its printed value can sit out of order among the rest.
        """
        out = self.upload(document(label="pick-perghz", cores=[
            core(cpu=0, mhz=5000.0, score=20000.0),          # 4000 per GHz
            core(cpu=1, mhz=2000.0, score=12000.0)]))        # 6000 per GHz

        def row(norm):
            _, b = self.req(f"/api/cores?scope=cpu&q=pick-perghz&sort=score"
                            f"&order=desc&norm={norm}&limit=10")
            return next(c for c in b["cores"] if c["run_id"] == out["id"])

        self.assertEqual(row("abs")["cpu"], 0)
        self.assertEqual(row("ghz")["cpu"], 1)

    def test_a_record_with_no_clock_sorts_on_what_it_shows(self):
        """No clock, nothing to divide by, so the raw figure is the reading.

        Runs from binaries that could not read a clock are permanently in that
        state. The page prints their absolute value in the per-GHz view, so
        that is what they have to be placed by; anything else puts a row in a
        position its own number does not explain.
        """
        clocked = self.upload(document(label="noclock-clocked", cores=[
            core(cpu=0, mhz=5000.0, score=20000.0)]))        # 4000 per GHz
        clockless = self.upload(document(label="noclock-bare", cores=[
            core(cpu=0, mhz=None, score=5000.0)]))           # shown as 5000

        _, b = self.req("/api/cores?scope=cpu&q=noclock-&sort=score"
                        "&order=desc&norm=ghz&limit=10")
        self.assertEqual([c["run_id"] for c in b["cores"]],
                         [clockless["id"], clocked["id"]])

    def test_rejects_an_unknown_norm(self):
        code, _ = self.expect_error("/api/cores?norm=cycles")
        self.assertEqual(code, 400)

    def test_records_of_one_run_are_addressable(self):
        """What the expanded view of a board row asks for."""
        cores = [core(cpu=i, score=1000.0 + i) for i in range(4)]
        out = self.upload(document("per-core", cores=cores))
        _, all_of = self.req(
            f"/api/cores?run={out['id']}&scope=cpu&sort=score&order=desc")
        self.assertEqual([c["cpu"] for c in all_of["cores"]], [3, 2, 1, 0])
        self.assertTrue(all(c["run_id"] == out["id"] for c in all_of["cores"]))

    def test_ids_address_records_not_runs(self):
        """A shared comparison link names individual cores of the same run."""
        out = self.upload(document("per-core",
                                   cores=[core(cpu=0), core(cpu=1, score=1.0)]))
        _, run = self.req(f"/api/runs/{out['id']}")
        ids = ",".join(str(c["id"]) for c in run["cores"])
        _, picked = self.req(f"/api/cores?ids={ids}")
        self.assertEqual(len(picked["cores"]), 2)

    def test_group_none_returns_every_record(self):
        cores = [core(cpu=i) for i in range(3)]
        out = self.upload(document("per-core", cores=cores))
        _, flat = self.req("/api/cores?scope=cpu&group=none&limit=500")
        self.assertEqual(len([c for c in flat["cores"] if c["run_id"] == out["id"]]), 3)

    def test_rejects_bad_group(self):
        code, _ = self.expect_error("/api/cores?group=everything")
        self.assertEqual(code, 400)

    def test_rank_reports_percentile(self):
        self.upload()           # something to stand against
        out = self.upload()
        _, rank = self.req(f"/api/runs/{out['id']}/rank")
        self.assertEqual(rank["scope"], "cpu")
        self.assertIn("int_thr", rank["metrics"])
        pct = rank["metrics"]["int_thr"]
        self.assertGreater(pct["population"], 0)
        self.assertTrue(0 <= pct["percentile"] <= 100)

    def test_label_and_notes_from_query(self):
        out = self.upload(query="?label=my+box&notes=quiet+room")
        _, run = self.req(f"/api/runs/{out['id']}")
        self.assertEqual(run["label"], "my box")
        self.assertEqual(run["notes"], "quiet room")

    def test_delete_requires_matching_token(self):
        out = self.upload()
        code, _ = self.expect_error(f"/api/runs/{out['id']}", "DELETE",
                                    headers={"X-Delete-Token": "wrong"})
        self.assertEqual(code, 403)
        status, _ = self.req(f"/api/runs/{out['id']}", "DELETE",
                             headers={"X-Delete-Token": out["delete_token"]})
        self.assertEqual(status, 200)
        code, _ = self.expect_error(f"/api/runs/{out['id']}")
        self.assertEqual(code, 404)

    def test_delete_removes_core_rows(self):
        out = self.upload(document("per-core", cores=[core(cpu=7, score=1.0)]))
        self.req(f"/api/runs/{out['id']}", "DELETE",
                 headers={"X-Delete-Token": out["delete_token"]})
        _, board = self.req("/api/cores?scope=cpu&sort=score&order=asc&limit=200")
        self.assertFalse([c for c in board["cores"] if c["run_id"] == out["id"]])

    # -- page size --------------------------------------------------------
    def test_a_listing_shows_the_page_size_it_was_asked_for(self):
        for _ in range(3):
            self.upload()
        _, one = self.req("/api/runs?limit=1")
        self.assertEqual(len(one["runs"]), 1)
        _, two = self.req("/api/cores?scope=cpu&limit=2")
        self.assertEqual(len(two["cores"]), 2)

    def test_a_listing_can_be_walked_page_by_page(self):
        """Every row is reachable, once, by stepping the offset.

        The size of a page decides how much is on screen, never how much of the
        board can be read: what is past the first page is what `offset` is for,
        and `total` is how a page knows there is another one.
        """
        for i in range(7):
            self.upload(document(label=f"page me {i}"))
        seen, offset = [], 0
        while True:
            _, page = self.req(f"/api/cores?scope=cpu&limit=3&offset={offset}")
            self.assertEqual(page["offset"], offset)
            self.assertEqual(page["limit"], 3)
            seen += [c["id"] for c in page["cores"]]
            offset += 3
            if offset >= page["total"]:
                break
        self.assertEqual(len(seen), len(set(seen)))       # no row twice
        self.assertEqual(len(seen), page["total"])        # and none missed
        # Past the end is an empty page rather than an error or a wrap-around.
        _, past = self.req(f"/api/cores?scope=cpu&limit=3&offset={page['total'] + 90}")
        self.assertEqual(past["cores"], [])
        self.assertEqual(past["total"], page["total"])

    def test_runs_are_paged_the_same_way(self):
        for i in range(4):
            self.upload(document(label=f"run page {i}"))
        _, first = self.req("/api/runs?limit=2&offset=0")
        _, second = self.req("/api/runs?limit=2&offset=2")
        self.assertEqual(len(first["runs"]), 2)
        self.assertEqual(len(second["runs"]), 2)
        self.assertGreaterEqual(first["total"], 4)
        self.assertFalse({r["id"] for r in first["runs"]} &
                         {r["id"] for r in second["runs"]})

    def test_a_count_counts_what_the_filters_left(self):
        """The total is the filtered listing's, not the whole hub's.

        A pager built from a count that ignored the filters would offer pages
        that are not there.
        """
        self.upload(document(label="countme-alpha"))
        _, page = self.req("/api/cores?scope=cpu&q=countme-alpha&limit=1")
        self.assertEqual(page["total"], 1)
        self.assertEqual(len(page["cores"]), 1)
        # One row per upload on the board, however many records it holds --
        # counted the same way, or the last page would be short of rows that
        # were never going to be shown separately.
        out = self.upload(document("per-core", label="countme-beta",
                                   cores=[core(cpu=i, score=float(i + 1))
                                          for i in range(5)]))
        _, grouped = self.req("/api/cores?scope=cpu&q=countme-beta&limit=10")
        self.assertEqual(grouped["total"], 1)
        _, flat = self.req("/api/cores?scope=cpu&q=countme-beta&group=none&limit=10")
        self.assertEqual(flat["total"], 5)
        self.assertEqual(flat["cores"][0]["run_id"], out["id"])

    def test_both_listings_stop_at_the_same_cap(self):
        """An over-cap limit is clamped rather than refused.

        The two endpoints used to cap at different numbers, which made the
        page's own picker wrong for one of them. Nothing here uploads MAX_PAGE
        runs to see the cut -- what matters is that neither listing errors and
        neither hands back more than the cap the picker offers.
        """
        self.upload()
        for path in (f"/api/runs?limit={srv.MAX_PAGE * 10}",
                     f"/api/cores?scope=cpu&limit={srv.MAX_PAGE * 10}"):
            status, out = self.req(path)
            self.assertEqual(status, 200)
            rows = out.get("runs", out.get("cores"))
            self.assertLessEqual(len(rows), srv.MAX_PAGE)
        # A limit that is not a number at all falls back rather than failing.
        self.assertEqual(self.req("/api/runs?limit=lots")[0], 200)

    # -- rejected input ---------------------------------------------------
    def test_rejects_wrong_schema(self):
        code, err = self.expect_error("/api/runs", "POST",
                                      document(schema="something/2"))
        self.assertEqual(code, 400)
        self.assertIn("schema", err["error"])

    def test_rejects_disp_sweep_dump(self):
        code, err = self.expect_error("/api/runs", "POST", document("disp-sweep"))
        self.assertEqual(code, 400)
        self.assertIn("mode", err["error"])

    def test_rejects_nan_and_infinity(self):
        for literal in (b"NaN", b"Infinity", b"-Infinity"):
            body = json.dumps(document()).encode().replace(b"20000.0", literal, 1)
            code, _ = self.expect_error("/api/runs", "POST", body)
            self.assertEqual(code, 400, literal)

    def test_rejects_absurd_values(self):
        code, _ = self.expect_error("/api/runs", "POST",
                                    document("per-core", cores=[core(int_thr_mops=1e30)]))
        self.assertEqual(code, 400)

    def test_rejects_malformed_json(self):
        code, err = self.expect_error("/api/runs", "POST", b"{nope")
        self.assertEqual(code, 400)
        self.assertIn("invalid JSON", err["error"])

    def test_rejects_empty_core_list(self):
        code, _ = self.expect_error("/api/runs", "POST", document("per-core", cores=[]))
        self.assertEqual(code, 400)

    def test_rejects_oversized_body(self):
        big = document("per-core", cores=[core(cpu=i) for i in range(4000)])
        body = json.dumps(big).encode()
        self.assertGreater(len(body), srv.MAX_BODY)
        code, _ = self.expect_error("/api/runs", "POST", body)
        self.assertEqual(code, 413)
        # The server must still be serving afterwards.
        self.assertEqual(self.req("/api/stats")[0], 200)

    def test_rejects_too_many_records(self):
        # Under the byte cap, over the record cap.
        many = document("per-core",
                        cores=[{"scope": "cpu", "cpu": i, "score": 1.0}
                               for i in range(srv.MAX_CORES + 1)])
        code, err = self.expect_error("/api/runs", "POST", many)
        self.assertEqual(code, 400)
        self.assertIn("too many", err["error"])

    def test_rejects_bad_sort_key(self):
        code, _ = self.expect_error("/api/cores?sort=score;DROP+TABLE+runs")
        self.assertEqual(code, 400)

    def test_control_characters_are_stripped_from_text(self):
        out = self.upload(document(label="ok\x07\x00label"))
        _, run = self.req(f"/api/runs/{out['id']}")
        self.assertEqual(run["label"], "oklabel")

    def test_unknown_endpoint_is_404(self):
        code, _ = self.expect_error("/api/nope")
        self.assertEqual(code, 404)

    def test_static_files_are_served_and_confined(self):
        status, body = self.req("/", raw=True)
        self.assertEqual(status, 200)
        self.assertIn(b"cpcpub results", body)
        code, _ = self.expect_error("/../server.py")
        self.assertEqual(code, 404)

    def test_rate_limit_blocks_floods(self):
        limiter = srv.RateLimiter(2, 60.0)
        self.assertTrue(limiter.allow("a"))
        self.assertTrue(limiter.allow("a"))
        self.assertFalse(limiter.allow("a"))
        self.assertTrue(limiter.allow("b"))

    # -- re-uploads --------------------------------------------------------
    def test_an_exact_reupload_is_refused(self):
        doc = document()
        first = self.upload(doc)
        code, err = self.expect_error("/api/runs", "POST", doc)
        self.assertEqual(code, 409)
        self.assertEqual(err["id"], first["id"])
        self.assertIn(f"run {first['id']}", err["error"])
        # Whoever sends a copy -- and the raw document of every run is public
        # -- must not get the means to withdraw it.
        self.assertNotIn("delete_token", err)
        self.assertNotIn(first["delete_token"], json.dumps(err))

    def test_a_new_label_or_layout_is_still_the_same_document(self):
        doc = document()
        first = self.upload(doc)
        respaced = json.dumps(dict(doc, label="in the document"), indent=2).encode()
        code, err = self.expect_error("/api/runs?label=another&notes=more", "POST",
                                      respaced)
        self.assertEqual((code, err["id"]), (409, first["id"]))

    def test_a_browsers_copy_is_still_the_same_document(self):
        """What the Submit page sends: JSON.parse then JSON.stringify, which
        writes 20000.0 as 20000 and a seed past 2^53 as the nearest double."""
        doc = document()
        doc["config"]["seed"] = (1 << 62) + 1
        first = self.upload(doc)

        def js(v):
            if isinstance(v, dict):
                return {k: js(x) for k, x in v.items()}
            if isinstance(v, list):
                return [js(x) for x in v]
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                f = float(v)
                return int(f) if f.is_integer() else f
            return v
        copy = json.dumps(js(doc)).encode()
        self.assertIn(b"4611686018427387904", copy)
        self.assertNotIn(b"20000.0", copy)
        code, err = self.expect_error("/api/runs", "POST", copy)
        self.assertEqual((code, err["id"]), (409, first["id"]))

    def test_a_withdrawn_document_may_be_sent_again(self):
        doc = document()
        first = self.upload(doc)
        self.req(f"/api/runs/{first['id']}", "DELETE",
                 headers={"X-Delete-Token": first["delete_token"]})
        self.upload(doc)                # 201, or upload() fails the test

    # -- hiding ------------------------------------------------------------
    def hide(self, out, hidden=True):
        return self.req(f"/api/runs/{out['id']}", "PATCH", {"hidden": hidden},
                        headers={"X-Delete-Token": out["delete_token"]})[1]

    def on_board(self, cpu):
        _, board = self.req("/api/cores?scope=cpu&q=" + urllib.parse.quote(cpu))
        return [r["run_id"] for r in board["cores"]]

    def test_a_hidden_run_is_off_the_board_but_still_there(self):
        doc = document()
        doc["system"]["cpu_models"] = "Hidden CPU 1"
        doc["build"]["target"] = "s390x"
        doc["system"]["machine"] = "s390x"
        out = self.upload(doc)
        self.assertEqual(self.on_board("Hidden CPU 1"), [out["id"]])

        changed = self.hide(out)
        self.assertTrue(changed["hidden"])
        self.assertEqual(self.on_board("Hidden CPU 1"), [])
        _, grouped = self.req("/api/cores?scope=cpu&group=cpu&q=Hidden+CPU+1")
        self.assertEqual(grouped["groups"], [])
        _, recent = self.req("/api/runs?limit=500")
        self.assertNotIn(out["id"], [r["id"] for r in recent["runs"]])
        _, stats = self.req("/api/stats")
        self.assertNotIn("s390x", [t["target"] for t in stats["targets"]])
        self.assertNotIn("Hidden CPU 1", [c["cpu_key"] for c in stats["cpus"]])
        # Its own link, and a link naming its records, still answer.
        status, run = self.req(f"/api/runs/{out['id']}")
        self.assertEqual(status, 200)
        self.assertTrue(run["hidden_at"])
        _, records = self.req(f"/api/cores?run={out['id']}")
        self.assertEqual(len(records["cores"]), 2)

        self.assertFalse(self.hide(out, False)["hidden"])
        self.assertEqual(self.on_board("Hidden CPU 1"), [out["id"]])
        self.assertIsNone(self.req(f"/api/runs/{out['id']}")[1]["hidden_at"])

    def test_hiding_again_keeps_when_it_was_hidden(self):
        out = self.upload()
        self.hide(out)
        at = self.req(f"/api/runs/{out['id']}")[1]["hidden_at"]
        with mock.patch.object(srv, "iso_now", return_value="2099-01-01T00:00:00Z"):
            self.hide(out)
        self.assertEqual(self.req(f"/api/runs/{out['id']}")[1]["hidden_at"], at)

    def test_a_hidden_run_is_no_ones_peer(self):
        def run(score):
            doc = document()
            doc["system"]["cpu_models"] = "Hidden Peer CPU"
            doc["cores"] = [core(cpu=0, score=score)]
            return self.upload(doc)
        a, b = run(10000.0), run(12000.0)
        self.assertEqual(self.req(f"/api/runs/{a['id']}/rank")[1]["cpu"]["scopes"]["cpu"]["runs"], 2)
        self.hide(b)
        self.assertEqual(self.req(f"/api/runs/{a['id']}/rank")[1]["cpu"]["scopes"], {})
        self.assertIsNone(self.req(f"/api/runs/{b['id']}/rank")[1]["cpu"])
        _, rank = self.req(f"/api/runs/{a['id']}/rank")
        before = rank["metrics"]["score"]["population"]
        self.hide(b, False)
        _, rank = self.req(f"/api/runs/{a['id']}/rank")
        self.assertEqual(rank["metrics"]["score"]["population"], before + 1)

    def test_hiding_needs_a_claim_and_a_bool(self):
        out = self.upload()
        code, _ = self.expect_error(f"/api/runs/{out['id']}", "PATCH", {"hidden": True})
        self.assertEqual(code, 401)
        code, _ = self.expect_error(f"/api/runs/{out['id']}", "PATCH", {"hidden": True},
                                    headers={"X-Delete-Token": "not-it"})
        self.assertEqual(code, 403)
        code, _ = self.expect_error(f"/api/runs/{out['id']}", "PATCH", {"hidden": 1},
                                    headers={"X-Delete-Token": out["delete_token"]})
        self.assertEqual(code, 400)
        self.assertIsNone(self.req(f"/api/runs/{out['id']}")[1]["hidden_at"])

    # -- kinds of core and the same CPU -----------------------------------
    def hybrid(self, cpu_models, scores, mode="per-core", threads_pinned=True):
        """A run whose CPU i scored scores[i], under this CPU line."""
        doc = document(mode)
        doc["system"]["cpu_models"] = cpu_models
        doc["system"]["cpus"] = len(scores)
        doc["cores"] = [core(cpu=i, score=s) for i, s in enumerate(scores)]
        if mode == "full":
            n = len(scores)
            doc["config"]["threads"] = n
            doc["threads"] = [core("thread", i if threads_pinned else -1, score=5000.0)
                              for i in range(n)]
            doc["total"] = core("total", None, threads=n)
        return self.upload(doc)

    def test_a_run_says_which_kind_each_core_is(self):
        out = self.hybrid("Hybrid Kinds 1", [8000, 8100, 7900, 8050,
                                             10500, 10600, 10400, 10550], mode="full")
        _, run = self.req(f"/api/runs/{out['id']}")
        kinds = [(t["scope"], t["name"], t["cpus"]) for t in run["types"]]
        self.assertEqual(kinds, [("cpu", "large", [4, 5, 6, 7]),
                                 ("cpu", "small", [0, 1, 2, 3]),
                                 ("thread", "large", [4, 5, 6, 7]),
                                 ("thread", "small", [0, 1, 2, 3])])
        by_cpu = {(c["scope"], c["cpu"]): c["type"] for c in run["cores"]}
        self.assertEqual(by_cpu[("cpu", 0)], "small")
        self.assertEqual(by_cpu[("thread", 7)], "large")
        self.assertIsNone(by_cpu[("total", None)])

    def test_the_expanded_row_carries_the_kinds_too(self):
        out = self.hybrid("Hybrid Kinds 2", [8000, 8000, 10500, 10500], mode="full")
        _, page = self.req(f"/api/cores?run={out['id']}&scope=thread")
        self.assertEqual({c["cpu"]: c["type"] for c in page["cores"]},
                         {0: "small", 1: "small", 2: "large", 3: "large"})

    def test_a_fit_says_how_a_run_stands_on_its_cpu(self):
        cpu = "Fit CPU 1"
        ids = [self.hybrid(cpu, [s])["id"] for s in (10000, 11000, 12000)]
        lone = self.hybrid("Fit CPU 2", [9000])["id"]
        _, page = self.req("/api/cores?scope=cpu&q=Fit+CPU&limit=50")
        fit = {r["run_id"]: r.get("cpu_fit") for r in page["cores"]}
        self.assertIsNone(fit[lone])
        self.assertEqual([fit[i]["rank"] for i in ids], [3, 2, 1])
        self.assertEqual({fit[i]["of"] for i in ids}, {3})
        self.assertAlmostEqual(fit[ids[0]]["median"], 11000, delta=1)
        # A latency ranks the other way round.
        _, page = self.req("/api/cores?scope=cpu&q=Fit+CPU+1&sort=mem_lat_ns&order=asc")
        self.assertEqual({r["cpu_fit"]["rank"] for r in page["cores"]}, {1})

    def test_peers_must_share_the_build(self):
        cpu = "Fit CPU 3"
        self.hybrid(cpu, [10000])
        doc = document()
        doc["system"]["cpu_models"] = cpu
        doc["build"]["vectorize"] = True
        self.upload(doc)
        _, page = self.req("/api/cores?scope=cpu&vectorize=&fma=&q=Fit+CPU+3")
        self.assertEqual(len(page["cores"]), 2)
        self.assertTrue(all("cpu_fit" not in r for r in page["cores"]))

    def test_the_board_groups_by_cpu(self):
        for s in (10000, 11000, 12000):
            self.hybrid("Group CPU A", [s])
        self.hybrid("Group CPU B", [20000])
        _, page = self.req("/api/cores?scope=cpu&group=cpu&q=Group+CPU")
        rows = [(g["cpu_models"], g["runs"]) for g in page["groups"]]
        self.assertEqual(rows, [("Group CPU B", 1), ("Group CPU A", 3)])
        self.assertEqual(page["total"], 2)
        a = page["groups"][1]
        self.assertAlmostEqual(a["score"], 11000, delta=1)
        self.assertAlmostEqual(a["low"], 10000, delta=1)
        self.assertAlmostEqual(a["high"], 12000, delta=1)
        # Unfolding one: its runs, by the exact CPU line.
        _, runs = self.req("/api/cores?scope=cpu&cpu=Group+CPU+A")
        self.assertEqual(len(runs["cores"]), 3)

    def test_boards_on_one_soc_are_one_cpu(self):
        for s, board in ((10000, "Board One"), (11000, "Board Two")):
            self.hybrid(f"Soc CPU X + Soc CPU Y ({board})", [s])
        self.hybrid("Soc CPU Z (Board Three)", [5000])
        _, page = self.req("/api/cores?scope=cpu&group=cpu&q=Soc+CPU")
        rows = {g["cpu_models"]: g for g in page["groups"]}
        self.assertEqual(rows["Soc CPU X + Soc CPU Y"]["runs"], 2)
        self.assertEqual(rows["Soc CPU X + Soc CPU Y"]["boards"], ["Board One", "Board Two"])
        self.assertEqual(rows["Soc CPU Z (Board Three)"]["runs"], 1)
        _, runs = self.req("/api/cores?scope=cpu&cpu=Soc+CPU+X+%2B+Soc+CPU+Y")
        self.assertEqual(len(runs["cores"]), 2)

    def test_a_chip_name_keeps_its_board(self):
        # A line naming no core design: the board is all that tells chips apart.
        ids = [self.hybrid(f"One Chip ({b})", [s])["id"]
               for b, s in (("Board P", 10000), ("Board Q", 12000))]
        _, page = self.req("/api/cores?scope=cpu&q=One+Chip&limit=50")
        self.assertTrue(all(r.get("cpu_fit") is None for r in page["cores"]
                            if r["run_id"] in ids))
        _, page = self.req("/api/cores?scope=cpu&group=cpu&q=One+Chip")
        self.assertEqual(len(page["groups"]), 2)

    def test_one_arm_design_is_one_cpu_across_boards(self):
        for b in ("Arm Board P", "Arm Board Q"):
            doc = document()
            doc["system"].update(cpu_models=f"Cortex-A35 ({b})", cpus=4)
            self.upload(doc)
        doc["system"].update(cpu_models="Cortex-A35 (Arm Board R)", cpus=2)
        self.upload(doc)
        _, page = self.req("/api/cores?scope=cpu&group=cpu&q=Arm+Board")
        self.assertEqual(sorted((g["cpu_models"], g["cpu_layout"], g["runs"])
                                for g in page["groups"]),
                         [("Cortex-A35", "4x Cortex-A35", 2),
                          ("Cortex-A35 (Arm Board R)", "2x Cortex-A35", 1)])

    def test_firmware_keeps_its_chip(self):
        # Firmware names its chip in parentheses and as the machine: the chip is
        # the CPU there, so one core design in two chips is two CPUs.
        for chip in ("nrf52840", "stm32f401xe", "stm32f401xe"):
            doc = document()
            doc["system"].update(cpu_models=f"Cortex-M4F ({chip})", machine=chip,
                                 cpus=1, sysname="zephyr")
            self.upload(doc)
        _, page = self.req("/api/cores?scope=cpu&group=cpu&q=Cortex-M4F")
        self.assertEqual(sorted((g["cpu_models"], g["runs"]) for g in page["groups"]),
                         [("Cortex-M4F (nrf52840)", 1), ("Cortex-M4F (stm32f401xe)", 2)])

    def test_an_arm11_is_an_arm_design(self):
        # The first Raspberry Pi's core, named from its MIDR like any Cortex.
        for b in ("Pi Model B Rev 2", "Pi Zero W"):
            doc = document()
            doc["system"].update(cpu_models=f"ARM1176 (Raspberry {b})", cpus=1)
            self.upload(doc)
        _, page = self.req("/api/cores?scope=cpu&group=cpu&q=ARM1176")
        self.assertEqual([(g["cpu_models"], g["cpu_layout"], g["runs"])
                          for g in page["groups"]], [("ARM1176", "1x ARM1176", 2)])

    def test_a_run_carries_its_guessed_layout(self):
        doc = document()
        doc["system"]["cpu_models"] = "Shw A + Shw B (Shw Board)"
        doc["cores"] = [core(cpu=i, score=20000.0 if i < 2 else 8000.0) for i in range(8)]
        rid = self.upload(doc)["id"]
        _, run = self.req(f"/api/runs/{rid}")
        self.assertEqual(run["cpu_layout"], "2x Shw A + 6x Shw B")
        _, page = self.req("/api/cores?scope=cpu&q=Shw+A")
        self.assertEqual(page["cores"][0]["cpu_layout"], "2x Shw A + 6x Shw B")

    def test_one_arm_design_is_counted(self):
        doc = document()
        doc["system"].update(cpu_models="Cortex-A55 (Cnt Board)", cpus=4)
        rid = self.upload(doc)["id"]
        _, run = self.req(f"/api/runs/{rid}")
        self.assertEqual(run["cpu_layout"], "4x Cortex-A55")
        # A brand name is a chip, not a core: nothing to count it as.
        doc["system"].update(cpu_models="Cnt Brand(R) N9", cpus=4)
        rid = self.upload(doc)["id"]
        _, run = self.req(f"/api/runs/{rid}")
        self.assertIsNone(run["cpu_layout"])

    def test_peers_are_named_for_the_page(self):
        for s in (10000, 11000):
            self.hybrid("Peer CPU", [s])
        self.hybrid("Peer CPU 2", [9000])
        _, page = self.req("/api/cores?scope=cpu&q=Peer+CPU&limit=50")
        peers = {r["cpu_models"]: r.get("peers") for r in page["cores"]}
        self.assertIsNotNone(peers["Peer CPU"])
        self.assertIsNone(peers["Peer CPU 2"])

    def test_stats_list_each_cpu_once_with_its_counts(self):
        for layout in ("2x Pck A + 6x Pck B", "4x Pck A + 4x Pck B", "4x Pck A + 4x Pck B"):
            doc = document()
            doc["system"].update(cpu_models="Pck A + Pck B (Pck Board)", cpu_layout=layout)
            self.upload(doc)
        _, stats = self.req("/api/stats")
        mine = sorted((c["cpu_layout"], c["n"]) for c in stats["cpus"]
                      if c["cpu_key"] == "Pck A + Pck B")
        self.assertEqual(mine, [("2x Pck A + 6x Pck B", 1), ("4x Pck A + 4x Pck B", 2)])
        # Picking one is the cpu= and layout= the page sends.
        _, page = self.req("/api/cores?scope=cpu&cpu=Pck+A+%2B+Pck+B"
                           "&layout=2x+Pck+A+%2B+6x+Pck+B")
        self.assertEqual(len(page["cores"]), 1)

    def test_core_counts_split_a_soc(self):
        for layout, s in (("2x Lay A + 6x Lay B", 10000), ("4x Lay A + 4x Lay B", 11000),
                          ("4x Lay A + 4x Lay B", 12000)):
            doc = document()
            doc["system"]["cpu_models"] = "Lay A + Lay B (Some Board)"
            doc["system"]["cpu_layout"] = layout
            self.upload(doc)
        _, page = self.req("/api/cores?scope=cpu&group=cpu&q=Lay+A")
        self.assertEqual(sorted((g["cpu_layout"], g["runs"]) for g in page["groups"]),
                         [("2x Lay A + 6x Lay B", 1), ("4x Lay A + 4x Lay B", 2)])
        _, runs = self.req("/api/cores?scope=cpu&cpu=Lay+A+%2B+Lay+B"
                           "&layout=4x+Lay+A+%2B+4x+Lay+B")
        self.assertEqual(len(runs["cores"]), 2)

    def test_layout_is_guessed_when_not_reported(self):
        # Three fast cores and five slow ones, by score, under a two-design line.
        def run(layout):
            doc = document(mode="per-core")
            doc["system"]["cpu_models"] = "Gue A + Gue B (Gue Board)"
            if layout:
                doc["system"]["cpu_layout"] = layout
            fast, slow = 3, 5
            doc["cores"] = [core(cpu=i, score=20000.0 if i < fast else 8000.0)
                           for i in range(fast + slow)]
            return self.upload(doc)
        run("3x Gue A + 5x Gue B")
        run(None)
        _, page = self.req("/api/cores?scope=cpu&group=cpu&q=Gue+A")
        self.assertEqual([(g["cpu_layout"], g["runs"]) for g in page["groups"]],
                         [("3x Gue A + 5x Gue B", 2)])

    def test_stats_list_guessed_layouts_apart(self):
        # Neither run reports its layout, so both are the hub's guess: one CPU
        # line, two core mixes, two entries.
        for fast in (2, 4):
            doc = document(mode="per-core")
            doc["system"]["cpu_models"] = "Gst A + Gst B (Gst Board)"
            doc["cores"] = [core(cpu=i, score=20000.0 if i < fast else 8000.0)
                            for i in range(8)]
            self.upload(doc)
        _, stats = self.req("/api/stats")
        self.assertEqual(sorted((c["cpu_layout"], c["n"]) for c in stats["cpus"]
                                if c["cpu_key"] == "Gst A + Gst B"),
                         [("2x Gst A + 6x Gst B", 1), ("4x Gst A + 4x Gst B", 1)])

    def test_runs_with_cpus_offline_are_not_compared(self):
        ids = [self.hybrid("Off CPU", [s])["id"] for s in (10000, 11000)]
        doc = document()
        doc["system"]["cpu_models"] = "Off CPU"
        doc["system"]["cpus_offline"] = 4
        off = self.upload(doc)["id"]
        _, page = self.req("/api/cores?scope=cpu&q=Off+CPU&limit=50")
        fit = {r["run_id"]: r.get("cpu_fit") for r in page["cores"]}
        self.assertIsNone(fit[off])
        self.assertEqual({fit[i]["of"] for i in ids}, {2})
        _, page = self.req("/api/cores?scope=cpu&group=cpu&q=Off+CPU")
        self.assertEqual(sorted((g["runs"], g.get("cpus_offline")) for g in page["groups"]),
                         [(1, 4), (2, None)])
        _, runs = self.req("/api/cores?scope=cpu&cpu=Off+CPU&layout=")
        self.assertEqual(sorted(r["run_id"] for r in runs["cores"]), sorted(ids))
        _, rank = self.req(f"/api/runs/{off}/rank")
        self.assertIsNone(rank["cpu"])

    def test_grouping_by_cpu_needs_a_scope(self):
        code, _ = self.expect_error("/api/cores?group=cpu&ids=1")
        self.assertEqual(code, 400)

    def test_the_run_page_has_the_same_cpu_standing(self):
        cpu = "Fit CPU 4"
        self.hybrid(cpu, [10000, 10000], mode="full")
        out = self.hybrid(cpu, [12000, 12000], mode="full")
        _, rank = self.req(f"/api/runs/{out['id']}/rank")
        self.assertEqual(rank["cpu"]["cpu_models"], cpu)
        best = rank["cpu"]["scopes"]["cpu"]
        self.assertEqual(best["runs"], 2)
        self.assertEqual(best["metrics"]["score"]["rank"], 1)
        self.assertIn("total", rank["cpu"]["scopes"])


class CoreTypesTest(unittest.TestCase):
    @staticmethod
    def kinds(scores, cpu_models="", threads=()):
        recs = [{"id": i, "scope": "cpu", "cpu": i, "score": s}
                for i, s in enumerate(scores)]
        recs += [{"id": 100 + i, "scope": "thread", "cpu": c, "score": 1.0}
                 for i, c in enumerate(threads)]
        return [(t["scope"], t["name"], t["cpus"])
                for t in srv.core_types(recs, cpu_models)]

    def test_a_phone_names_its_designs(self):
        self.assertEqual(
            self.kinds([2000, 2010, 1990, 2005, 6000, 6100, 5950, 7600],
                       "Cortex-A55 + Cortex-A78 + Cortex-X1 (Qualcomm SM8350)"),
            [("cpu", "Cortex-X1", [7]), ("cpu", "Cortex-A78", [4, 5, 6]),
             ("cpu", "Cortex-A55", [0, 1, 2, 3])])

    def test_one_design_at_two_clocks_is_one_kind_when_the_line_says_so(self):
        # The prime core is a faster-clocked X4: three steps, two names.
        self.assertEqual(
            self.kinds([4000, 4000, 4000, 4000, 6500, 6500, 6500, 7600],
                       "Cortex-A720 + Cortex-X4"),
            [("cpu", "Cortex-X4", [4, 5, 6, 7]),
             ("cpu", "Cortex-A720", [0, 1, 2, 3])])

    def test_a_brand_string_gives_sizes(self):
        self.assertEqual(self.kinds([8000, 8000, 10500, 10500, 3000],
                                    "Intel(R) Core(TM) Ultra 7 155H"),
                         [("cpu", "large", [2, 3]), ("cpu", "medium", [0, 1]),
                          ("cpu", "small", [4])])

    def test_alike_cores_are_one_kind(self):
        self.assertEqual(self.kinds([10000, 10400, 9800, 10200]),
                         [("cpu", "all cores", [0, 1, 2, 3])])

    def test_a_single_core_is_not_grouped(self):
        self.assertEqual(self.kinds([10000]), [])

    def test_unpinned_threads_are_one_group(self):
        self.assertEqual(self.kinds([8000, 10500], threads=(-1, -1)),
                         [("cpu", "large", [1]), ("cpu", "small", [0]),
                          ("thread", "all threads", [])])

    @staticmethod
    def thread_kinds(scores, cpus=None, cpu_models="Qualcomm Oryon"):
        recs = [{"id": i, "scope": "thread", "cpu": c, "score": s}
                for i, (s, c) in enumerate(zip(scores, cpus or range(len(scores))))]
        return [(t["scope"], t["name"], t["cpus"])
                for t in srv.core_types(recs, cpu_models)]

    def test_threads_alone_are_grouped_by_their_own_scores(self):
        # A Snapdragon 8 Elite measured multi-threaded only: one design, the
        # prime pair a step above the six.
        self.assertEqual(
            self.thread_kinds([9767, 9974, 9942, 9847, 9843, 9770, 14238, 14755]),
            [("thread", "large", [6, 7]), ("thread", "small", [0, 1, 2, 3, 4, 5])])

    def test_alike_threads_alone_are_one_group(self):
        self.assertEqual(self.thread_kinds([9800, 10100, 9900, 10000]),
                         [("thread", "all threads", [0, 1, 2, 3])])

    def test_unpinned_threads_alone_are_one_group(self):
        self.assertEqual(self.thread_kinds([9800, 14500], cpus=(-1, -1)),
                         [("thread", "all threads", [])])


class Browser:
    """One client with its own cookie jar, as a browser has.

    Sends the CSRF header the page would send, since that is what the server
    requires of a cookie-authenticated write; `csrf=False` reproduces the
    request another site could make, which is the one that must be refused.
    """

    def __init__(self, base: str):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def cookie(self, name: str) -> str | None:
        for c in self.jar:
            if c.name == name:
                return c.value
        return None

    def call(self, path, method="GET", body=None, headers=None, csrf=True):
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers=dict(headers or {}))
        if data is not None:
            req.add_header("Content-Type", "application/json")
        token = self.cookie(srv.CSRF_COOKIE)
        if csrf and token:
            req.add_header(srv.CSRF_HEADER, token)
        try:
            with self.opener.open(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def manifest(release: Any = "v1.0.0", digests: Sequence[str] = (DIGEST_A,),
             **over: Any) -> dict:
    """A builds manifest, valid unless a test asks for otherwise.

    Typed loose on purpose: half the tests below hand it a `release` of None or
    a `builds` of "no" to watch the manifest be refused, and a signature that
    forbade that would be describing the wrong function.
    """
    doc: dict[str, Any] = {
        "schema": "cpu-bench-builds/1",
        "release": release,
        "url": f"https://github.com/example/cpcpub/releases/tag/{release}",
        "builds": [{"filename": f"cpcpub-linux-x86_64-{i}", "sha256": d,
                    "target": "x86_64", "march": "x86-64"}
                   for i, d in enumerate(digests)],
    }
    doc.update(over)
    return doc


class VerifiedTest(unittest.TestCase):
    """Which runs count as produced by a published build, and which do not."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = str(Path(cls.tmp.name) / "v.sqlite3")
        cls.server = srv.build_server(cls.db, "127.0.0.1", 0, rate_limit=0)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.store = srv.Store(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        with self.store.connect() as db:
            db.execute("DELETE FROM verified_builds")

    def upload(self, digest=None, **over):
        build = dict(document()["build"])
        if digest is not None:
            build["binary_sha256"] = digest
        b = Browser(self.base)
        status, out = b.call("/api/runs", "POST", document(build=build, **over))
        self.assertEqual(status, 201, out)
        return b, out

    # -- the digest itself --------------------------------------------------
    def test_the_digest_round_trips(self):
        _, out = self.upload(DIGEST_A)
        b = Browser(self.base)
        _, run = b.call(f"/api/runs/{out['id']}")
        self.assertEqual(run["binary_sha256"], DIGEST_A)

    def test_a_malformed_digest_is_refused(self):
        for bad in ("nonsense", "A" * 63, "g" * 64, 12345, "  "):
            with self.subTest(bad):
                build = dict(document()["build"], binary_sha256=bad)
                code, _ = Browser(self.base).call(
                    "/api/runs", "POST", document(build=build))
                self.assertEqual(code, 400)

    def test_an_uppercase_digest_is_normalised(self):
        """sha256sum(1) and the benchmark agree on lowercase; be kind anyway."""
        build = dict(document()["build"], binary_sha256=DIGEST_A.upper())
        b = Browser(self.base)
        status, out = b.call("/api/runs", "POST", document(build=build))
        self.assertEqual(status, 201)
        _, run = b.call(f"/api/runs/{out['id']}")
        self.assertEqual(run["binary_sha256"], DIGEST_A)

    def test_a_run_without_a_digest_is_still_accepted(self):
        _, out = self.upload(None)
        _, run = Browser(self.base).call(f"/api/runs/{out['id']}")
        self.assertIsNone(run["binary_sha256"])
        self.assertIsNone(run["release_build"])

    # -- matching -----------------------------------------------------------
    def test_a_published_digest_marks_the_run_as_a_release_build(self):
        self.store.add_verified_builds(manifest("v1.2.3", [DIGEST_A]))
        _, out = self.upload(DIGEST_A)
        b = Browser(self.base)
        _, run = b.call(f"/api/runs/{out['id']}")
        self.assertEqual(run["release_build"], "v1.2.3")
        _, board = b.call("/api/cores?scope=cpu&verified=release")
        self.assertIn(out["id"], [c["run_id"] for c in board["cores"]])

    def test_an_unpublished_digest_does_not(self):
        self.store.add_verified_builds(manifest("v1.2.3", [DIGEST_A]))
        _, out = self.upload(DIGEST_B)
        b = Browser(self.base)
        self.assertIsNone(b.call(f"/api/runs/{out['id']}")[1]["release_build"])
        _, board = b.call("/api/cores?scope=cpu&verified=release&limit=200")
        self.assertNotIn(out["id"], [c["run_id"] for c in board["cores"]])

    def test_the_old_filter_spellings_still_answer(self):
        """`verified=ci|attested|1` were the GitHub-signature tiers this hub
        used to keep. The marks are gone; a saved link that names one must
        still answer as the release filter rather than break."""
        self.store.add_verified_builds(manifest("v1.2.3", [DIGEST_A]))
        _, out = self.upload(DIGEST_A)
        b = Browser(self.base)
        for spelling in ("release", "ci", "attested", "1", "true"):
            code, board = b.call(f"/api/cores?scope=cpu&verified={spelling}")
            self.assertEqual(code, 200, spelling)
            self.assertIn(out["id"], [c["run_id"] for c in board["cores"]], spelling)

    def test_a_bad_filter_value_is_refused(self):
        code, _ = Browser(self.base).call("/api/cores?verified=sort-of")
        self.assertEqual(code, 400)

    def test_loading_a_manifest_marks_runs_already_stored(self):
        """The reason the match is a join and not a stamp at upload time."""
        _, out = self.upload(DIGEST_A)
        b = Browser(self.base)
        self.assertIsNone(b.call(f"/api/runs/{out['id']}")[1]["release_build"])
        self.store.add_verified_builds(manifest("v2.0.0", [DIGEST_A]))
        self.assertEqual(b.call(f"/api/runs/{out['id']}")[1]["release_build"], "v2.0.0")

    def test_forgetting_a_release_unmarks_its_runs(self):
        self.store.add_verified_builds(manifest("v1.2.3", [DIGEST_A]))
        _, out = self.upload(DIGEST_A)
        b = Browser(self.base)
        self.assertEqual(b.call(f"/api/runs/{out['id']}")[1]["release_build"], "v1.2.3")
        self.store.forget_release("v1.2.3")
        self.assertIsNone(b.call(f"/api/runs/{out['id']}")[1]["release_build"])

    def test_reloading_a_release_replaces_what_it_published(self):
        self.store.add_verified_builds(manifest("v1.2.3", [DIGEST_A, DIGEST_B]))
        self.store.add_verified_builds(manifest("v1.2.3", [DIGEST_A]))
        digests = {b["sha256"] for b in self.store.verified_builds()}
        self.assertEqual(digests, {DIGEST_A})

    def test_other_releases_survive_a_reload(self):
        self.store.add_verified_builds(manifest("v1.0.0", [DIGEST_A]))
        self.store.add_verified_builds(manifest("v2.0.0", [DIGEST_B]))
        self.store.add_verified_builds(manifest("v2.0.0", [DIGEST_B]))
        releases = {b["release"] for b in self.store.verified_builds()}
        self.assertEqual(releases, {"v1.0.0", "v2.0.0"})

    # -- the manifest -------------------------------------------------------
    def test_manifest_must_carry_the_right_schema(self):
        with self.assertRaises(srv.Invalid):
            self.store.add_verified_builds(manifest(schema="something/9"))

    def test_manifest_needs_a_release_and_builds(self):
        broken: tuple[dict[str, Any], ...] = (
            {"release": None}, {"builds": []}, {"builds": "no"})
        for bad in broken:
            with self.subTest(bad), self.assertRaises(srv.Invalid):
                self.store.add_verified_builds(manifest(**bad))

    def test_manifest_digests_are_checked(self):
        with self.assertRaises(srv.Invalid):
            self.store.add_verified_builds(
                manifest(builds=[{"filename": "x", "sha256": "not-a-digest"}]))

    def test_a_manifest_url_must_be_http(self):
        """It becomes an href in the page, so `javascript:` is not a URL here."""
        with self.assertRaises(srv.Invalid):
            self.store.add_verified_builds(
                manifest(url="javascript:alert(document.domain)"))

    def test_the_hub_publishes_what_it_trusts(self):
        self.store.add_verified_builds(manifest("v3.0.0", [DIGEST_A]))
        _, out = Browser(self.base).call("/api/builds")
        self.assertEqual([b["sha256"] for b in out["builds"]], [DIGEST_A])
        self.assertEqual(out["builds"][0]["release"], "v3.0.0")
        _, stats = Browser(self.base).call("/api/stats")
        self.assertEqual(stats["release"]["release"], "v3.0.0")

    # -- one binary, several releases ---------------------------------------
    #
    # The ordinary case, not a corner one: a release rebuilds every target and
    # only some of them come out different, so most digests are published again
    # unchanged. What a run claims is then the newest release carrying its
    # bytes -- the version they are current in -- and never an accident of the
    # order an operator loaded manifests in.
    def test_a_digest_in_two_releases_reports_the_newer_one(self):
        self.store.add_verified_builds(manifest("v1.0.0", [DIGEST_A]))
        self.store.add_verified_builds(manifest("v1.1.0", [DIGEST_A]))
        _, out = self.upload(DIGEST_A)
        run = Browser(self.base).call(f"/api/runs/{out['id']}")[1]
        self.assertEqual(run["release_build"], "v1.1.0")

    def test_the_order_manifests_are_loaded_in_does_not_matter(self):
        self.store.add_verified_builds(manifest("v1.1.0", [DIGEST_A]))
        self.store.add_verified_builds(manifest("v1.0.0", [DIGEST_A]))
        _, out = self.upload(DIGEST_A)
        run = Browser(self.base).call(f"/api/runs/{out['id']}")[1]
        self.assertEqual(run["release_build"], "v1.1.0")

    def test_newest_is_by_version_and_not_by_string(self):
        """v0.10.0 is newer than v0.9.0, which no string comparison agrees."""
        self.store.add_verified_builds(manifest("v0.9.0", [DIGEST_A]))
        self.store.add_verified_builds(manifest("v0.10.0", [DIGEST_A]))
        _, out = self.upload(DIGEST_A)
        run = Browser(self.base).call(f"/api/runs/{out['id']}")[1]
        self.assertEqual(run["release_build"], "v0.10.0")
        _, stats = Browser(self.base).call("/api/stats")
        self.assertEqual(stats["release"]["release"], "v0.10.0")

    def test_forgetting_the_newest_falls_back_to_the_one_before(self):
        """The older release still published these bytes; it did not stop."""
        self.store.add_verified_builds(manifest("v1.0.0", [DIGEST_A]))
        self.store.add_verified_builds(manifest("v1.1.0", [DIGEST_A]))
        _, out = self.upload(DIGEST_A)
        b = Browser(self.base)
        self.store.forget_release("v1.1.0")
        self.assertEqual(b.call(f"/api/runs/{out['id']}")[1]["release_build"],
                         "v1.0.0")

    def test_a_release_that_drops_a_binary_stops_claiming_it(self):
        """Re-loading a manifest speaks for that release only, as it always did."""
        self.store.add_verified_builds(manifest("v1.0.0", [DIGEST_A]))
        self.store.add_verified_builds(manifest("v1.1.0", [DIGEST_A, DIGEST_B]))
        self.store.add_verified_builds(manifest("v1.1.0", [DIGEST_B]))
        _, out = self.upload(DIGEST_A)
        run = Browser(self.base).call(f"/api/runs/{out['id']}")[1]
        self.assertEqual(run["release_build"], "v1.0.0")


class ReleaseRankTest(unittest.TestCase):
    """The order the board calls "newest", tag by tag."""

    def newest(self, *tags: str) -> str:
        return max(tags, key=srv.release_rank)

    def test_numbers_compare_as_numbers(self):
        self.assertEqual(self.newest("v0.9.0", "v0.10.0"), "v0.10.0")
        self.assertEqual(self.newest("v1.9.9", "v1.10.0"), "v1.10.0")
        self.assertEqual(self.newest("v2.0.0", "v10.0.0"), "v10.0.0")

    def test_a_leading_v_is_not_part_of_the_version(self):
        self.assertEqual(self.newest("v0.3.0", "0.4.0"), "0.4.0")

    def test_a_prerelease_ranks_below_the_release_it_leads_to(self):
        self.assertEqual(self.newest("v1.0.0-rc1", "v1.0.0"), "v1.0.0")
        self.assertEqual(self.newest("v1.0.0-rc1", "v1.0.0-rc2"), "v1.0.0-rc2")
        self.assertEqual(self.newest("v1.0.0", "v1.0.1-rc1"), "v1.0.1-rc1")

    def test_an_absurd_component_does_not_scramble_its_neighbours(self):
        """A tag with no version in it must not outrank every real one."""
        self.assertEqual(self.newest("v2.0.0", "v" + "9" * 40 + ".0.0"),
                         "v" + "9" * 40 + ".0.0")
        self.assertEqual(self.newest("v1.0.0", "v1.0." + "9" * 40),
                         "v1.0." + "9" * 40)

    def test_tags_that_are_not_versions_still_rank_somewhere(self):
        """Nothing here may raise: the tag is whatever a release was called."""
        for tag in ("", "-", "release", "2026-08-18", "v1.0.0+build.7"):
            with self.subTest(tag):
                self.assertIsInstance(srv.release_rank(tag), str)


class VerifiedBuildsMigrationTest(unittest.TestCase):
    """A database written before one binary could belong to two releases."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "old.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def old_shape(self) -> None:
        """The table as the previous schema declared it: keyed on the digest."""
        srv.Store(self.db)          # everything else, at the current schema
        with sqlite3.connect(self.db) as db:
            db.execute("DROP TABLE verified_builds")
            db.execute("""
                CREATE TABLE verified_builds (
                    sha256      TEXT PRIMARY KEY,
                    release     TEXT NOT NULL,
                    release_url TEXT,
                    filename    TEXT,
                    target      TEXT,
                    march       TEXT,
                    added_at    TEXT NOT NULL)""")
            db.execute(
                "INSERT INTO verified_builds "
                "(sha256, release, release_url, filename, target, march, added_at) "
                "VALUES (?, 'v1.0.0', 'https://example/v1.0.0', 'cpcpub-x', "
                "        'x86_64', 'x86-64', '2026-01-01T00:00:00Z')",
                (DIGEST_A,))

    def test_what_the_old_table_held_survives(self):
        self.old_shape()
        builds = srv.Store(self.db).verified_builds()
        self.assertEqual([(b["sha256"], b["release"], b["filename"])
                          for b in builds],
                         [(DIGEST_A, "v1.0.0", "cpcpub-x")])

    def test_the_migrated_table_takes_a_second_release_for_one_binary(self):
        self.old_shape()
        store = srv.Store(self.db)
        store.add_verified_builds(manifest("v1.1.0", [DIGEST_A]))
        self.assertEqual({(b["sha256"], b["release"])
                          for b in store.verified_builds()},
                         {(DIGEST_A, "v1.0.0"), (DIGEST_A, "v1.1.0")})

    def test_migrating_twice_is_a_no_op(self):
        self.old_shape()
        srv.Store(self.db)
        before = srv.Store(self.db).verified_builds()
        self.assertEqual(before, srv.Store(self.db).verified_builds())


class CoresMigrationTest(unittest.TestCase):
    """A database whose `cores` table predates the FP32 columns."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "old.sqlite3")
        srv.Store(self.db)
        # Named here rather than read from ADDED_CORE_COLUMNS, so emptying
        # that list fails this test instead of quietly making it vacuous.
        with sqlite3.connect(self.db) as db:
            for col in ("fp32_lat", "fp32_thr"):
                db.execute(f"ALTER TABLE cores DROP COLUMN {col}")

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_old_database_takes_an_upload_and_keeps_the_new_fields(self):
        store = srv.Store(self.db)
        doc = document()
        run, cores = srv.validate(doc)
        run_id = store.insert(run, cores, json.dumps(doc), None, None)[0]
        with sqlite3.connect(self.db) as db:
            got = db.execute("SELECT fp32_lat, fp32_thr FROM cores "
                             "WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
        self.assertEqual(got, [(1500.0, 11000.0)] * len(doc["cores"]))

    def test_migrating_twice_is_a_no_op(self):
        srv.Store(self.db)
        srv.Store(self.db)
        with sqlite3.connect(self.db) as db:
            have = {r[1] for r in db.execute("PRAGMA table_info(cores)")}
        self.assertLessEqual(set(srv.CORE_COLUMNS), have)


class ReuploadMigrationTest(unittest.TestCase):
    """A database from before re-uploads were refused."""

    def test_old_copies_stay_and_new_ones_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "old.sqlite3")
            store = srv.Store(path)
            doc = document()
            run, cores = srv.validate(doc)
            first = store.insert(run, cores, json.dumps(doc), None, None)[0]
            # Two copies of one document, as a hub that took them both left it,
            # and no digest on either.
            with store.connect() as db:
                db.execute("UPDATE runs SET doc_sha256 = NULL")
                row = dict(db.execute("SELECT * FROM runs WHERE id = ?",
                                      (first,)).fetchone())
                row.pop("id")
                cols = list(row)
                db.execute(f"INSERT INTO runs ({','.join(cols)}) "
                           f"VALUES ({','.join('?' * len(cols))})",
                           [row[c] for c in cols])
            store = srv.Store(path)
            with store.connect() as db:
                digests = [r["doc_sha256"] for r in db.execute("SELECT doc_sha256 FROM runs")]
            self.assertEqual(len(digests), 2)
            self.assertEqual(len(set(digests)), 1)
            self.assertIsNotNone(digests[0])
            with self.assertRaises(srv.Duplicate) as cm:
                store.insert(run, cores, json.dumps(doc), None, None)
            self.assertEqual(cm.exception.run_id, first)


class BatchTest(unittest.TestCase):
    """Many machines' results in one file, for an account."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        # Single uploads held to two an hour (eight for an account), batches
        # to forty results: the two allowances are what is being told apart.
        cls.server = srv.build_server(str(Path(cls.tmp.name) / "b.sqlite3"),
                                      "127.0.0.1", 0, rate_limit=2, auth_limit=0,
                                      register_pow=0, register_limit=0,
                                      batch_limit=40)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.seq = 0

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def account(self):
        type(self).seq += 1
        b = Browser(self.base)
        status, out = b.call("/api/auth/register", "POST",
                             {"name": f"batcher{self.seq}", "password": "a good long password"})
        self.assertEqual(status, 201, out)
        return b, out["user"]["name"], out["user"]["api_token"]

    VARIANTS = ((False, False), (True, False), (False, True), (True, True))

    @staticmethod
    def machine(cpu, which=VARIANTS, **over):
        """One machine's results, one per build, as --variants writes them."""
        docs = []
        for vec, fma in which:
            d = document(**over)
            d["build"] = dict(d["build"], vectorize=vec, fma=fma)
            d["system"] = dict(d["system"], cpu_models=cpu)
            docs.append(d)
        return docs

    def send(self, text, token, query=""):
        req = urllib.request.Request(f"{self.base}/api/batch{query}",
                                     data=text.encode(), method="POST",
                                     headers={"Authorization": f"Bearer {token}",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    # -- reading the file ------------------------------------------------
    def test_a_catted_file_splits_into_its_documents(self):
        a, b = document(), document()
        text = "\ufeff" + json.dumps(a, indent=2) + "\n" + json.dumps(b) + "  \n"
        docs = srv.split_documents(text)
        self.assertEqual([d[2] for d in docs], [a, b])
        # Each is stored as its own text, cut from the file.
        self.assertEqual(json.loads(docs[0][1]), a)
        self.assertEqual(docs[0][1], json.dumps(a, indent=2))

    def test_arrays_are_parts_of_their_own(self):
        one, two = self.machine("P1"), self.machine("P2")
        loose = document()
        text = json.dumps(one) + json.dumps(loose) + json.dumps(two, indent=1) + "[]"
        parts = [d[0] for d in srv.split_documents(text)]
        self.assertEqual(len(parts), 9)
        self.assertEqual(len(set(parts[:4])), 1)
        self.assertNotIn(parts[4], parts[:4] + parts[5:])
        self.assertEqual(len(set(parts[5:])), 1)

    def test_broken_json_says_where(self):
        with self.assertRaises(srv.Invalid) as cm:
            srv.split_documents(json.dumps(document()) + '\n{"schema": ')
        self.assertIn("line 2", str(cm.exception))
        with self.assertRaises(srv.Invalid) as cm:
            srv.split_documents("[" + json.dumps(document()) + " x]")
        self.assertIn("expected ',' or ']'", str(cm.exception))

    def test_a_batch_has_a_ceiling(self):
        with mock.patch.object(srv, "MAX_BATCH", 3):
            with self.assertRaises(srv.Invalid):
                srv.split_documents("{}" * 4)

    # -- grouping --------------------------------------------------------
    def groups(self, text):
        return [[r["run"]["cpu_models"] + ":" + srv.variant_name(r["run"])
                 for r in up["runs"]] for up in srv.plan_batch(text)]

    def test_each_machines_variants_are_one_upload(self):
        loose = "\n".join(json.dumps(d) for d in self.machine("G1") + self.machine("G2"))
        self.assertEqual([len(g) for g in self.groups(loose)], [4, 4])
        arrays = json.dumps(self.machine("G1")) + json.dumps(self.machine("G2"))
        self.assertEqual([len(g) for g in self.groups(arrays)], [4, 4])

    def test_the_same_machine_run_twice_is_two_uploads(self):
        docs = self.machine("Twice", self.VARIANTS[:2]) * 1
        docs += self.machine("Twice", self.VARIANTS[:2])
        self.assertEqual([len(g) for g in self.groups("".join(json.dumps(d) for d in docs))],
                         [2, 2])

    def test_other_settings_are_another_upload(self):
        a = self.machine("Same", self.VARIANTS[:1])
        b = self.machine("Same", self.VARIANTS[1:2])
        b[0]["config"] = dict(b[0]["config"], threads=4)
        self.assertEqual([len(g) for g in self.groups(json.dumps(a[0]) + json.dumps(b[0]))],
                         [1, 1])

    def test_an_array_does_not_join_its_neighbours(self):
        loose = self.machine("Near", self.VARIANTS[:1])[0]
        arr = self.machine("Near", self.VARIANTS[1:])
        self.assertEqual([len(g) for g in self.groups(json.dumps(loose) + json.dumps(arr))],
                         [1, 3])

    def test_one_bad_result_names_itself(self):
        docs = self.machine("Bad", self.VARIANTS[:2])
        docs[1]["schema"] = "nope"
        with self.assertRaises(srv.Invalid) as cm:
            srv.plan_batch("".join(json.dumps(d) for d in docs))
        self.assertIn("result 2 of 2", str(cm.exception))

    # -- the endpoint ----------------------------------------------------
    def test_a_batch_needs_an_account(self):
        text = json.dumps(self.machine("Anon"))
        status, _ = Browser(self.base).call("/api/batch", "POST", text.encode())
        self.assertEqual(status, 401)
        b, _, _ = self.account()
        self.assertEqual(b.call("/api/batch", "POST", text.encode(), csrf=False)[0], 403)
        self.assertEqual(self.send(text, "no-such-token")[0], 401)

    def test_a_dry_run_stores_nothing(self):
        b, _, token = self.account()
        text = "".join(json.dumps(d) for d in self.machine("Dry 1") + self.machine("Dry 2", self.VARIANTS[:1]))
        status, plan = self.send(text, token, "?dry=1")
        self.assertEqual(status, 200, plan)
        self.assertEqual(plan["results"], 5)
        self.assertEqual([(u["cpu_models"], len(u["runs"])) for u in plan["uploads"]],
                         [("Dry 1", 4), ("Dry 2", 1)])
        self.assertEqual(plan["uploads"][0]["runs"][3]["variant"], "vector-fma")
        self.assertEqual(b.call("/api/uploads?user=me")[1]["runs"], 0)

    def test_a_batch_stores_each_machine_as_one_upload(self):
        b, name, token = self.account()
        text = "".join(json.dumps(d) for d in self.machine("Store 1") + self.machine("Store 2", self.VARIANTS[:1]))
        status, out = self.send(text, token, "?label=rack+a&label=&notes=lab")
        self.assertEqual(status, 201, out)
        self.assertEqual((out["stored"], out["skipped"], out["user"]), (5, 0, name))
        first, second = out["uploads"]
        self.assertEqual([r["label"] for r in first["runs"]],
                         ["rack a (scalar-nofma)", "rack a (vector-nofma)",
                          "rack a (scalar-fma)", "rack a (vector-fma)"])
        # A machine with one build keeps its label bare, and a blank one is none.
        self.assertIsNone(second["runs"][0]["label"])
        _, listing = b.call("/api/uploads?user=me")
        shape = sorted((c["cpu_models"], [len(u["runs"]) for u in c["uploads"]])
                       for c in listing["cpus"])
        self.assertEqual(shape, [("Store 1", [4]), ("Store 2", [1])])
        _, run = b.call(f"/api/runs/{first['runs'][0]['id']}")
        self.assertEqual((run["notes"], run["user"]), ("lab", name))

    def test_the_page_sends_labels_in_the_body(self):
        b, _, _ = self.account()
        text = json.dumps(self.machine("Form 1", self.VARIANTS[:2])) + \
            json.dumps(self.machine("Form 2", self.VARIANTS[:1]))
        status, out = b.call("/api/batch", "POST",
                             {"batch": text, "labels": ["one", "two"], "notes": "n"})
        self.assertEqual(status, 201, out)
        self.assertEqual([r["label"] for up in out["uploads"] for r in up["runs"]],
                         ["one (scalar-nofma)", "one (vector-nofma)", "two"])
        status, _ = b.call("/api/batch", "POST", {"batch": text, "labels": ["a", "b", "c"]})
        self.assertEqual(status, 400)

    def test_results_already_held_are_skipped_not_refused(self):
        b, _, token = self.account()
        docs = self.machine("Again", self.VARIANTS[:2])
        _, out = self.send(json.dumps(docs[0]), token)
        held = out["uploads"][0]["runs"][0]["id"]
        # The one already stored, the new one, and the new one once more.
        text = "".join(json.dumps(d) for d in (docs[0], docs[1])) + json.dumps([docs[1]])
        _, plan = self.send(text, token, "?dry=1")
        self.assertEqual(plan["held"], 1)
        status, out = self.send(text, token)
        self.assertEqual(status, 201, out)
        self.assertEqual((out["stored"], out["skipped"]), (1, 2))
        self.assertEqual(out["uploads"][0]["runs"][0]["duplicate_of"], held)
        new = out["uploads"][0]["runs"][1]["id"]
        self.assertEqual(out["uploads"][1]["runs"][0]["duplicate_of"], new)
        status, out = self.send(text, token)
        self.assertEqual((status, out["stored"]), (200, 0))

    def test_a_refused_batch_stores_nothing(self):
        b, _, token = self.account()
        docs = self.machine("Atomic")
        docs[3]["cores"] = []
        status, err = self.send("".join(json.dumps(d) for d in docs), token)
        self.assertEqual(status, 400)
        self.assertIn("result 4 of 4", err["error"])
        self.assertEqual(b.call("/api/uploads?user=me")[1]["runs"], 0)

    def test_batches_have_their_own_allowance(self):
        _, _, token = self.account()
        # Single uploads: two an hour here, an account four times that, and
        # then no more.
        for expect in [201] * 2 * srv.ACCOUNT_RATE_FACTOR + [429]:
            status, _ = self.send_single(token)
            self.assertEqual(status, expect)
        # A batch is still welcome, up to forty results; past that, refused
        # whole, with nothing counted.
        text = "".join(json.dumps(d) for i in range(9) for d in self.machine(f"Many {i}"))
        status, out = self.send(text, token)
        self.assertEqual((status, out["stored"]), (201, 36))
        status, err = self.send("".join(json.dumps(d) for d in self.machine("Over 1") + self.machine("Over 2")), token)
        self.assertEqual(status, 429)
        self.assertIn("8 results", err["error"])
        status, out = self.send("".join(json.dumps(d) for d in self.machine("Fits")), token)
        self.assertEqual((status, out["stored"]), (201, 4))

    def send_single(self, token):
        req = urllib.request.Request(f"{self.base}/api/runs", data=json.dumps(document()).encode(),
                                     method="POST", headers={"Authorization": f"Bearer {token}",
                                                             "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_the_hub_says_what_a_batch_may_hold(self):
        _, stats = Browser(self.base).call("/api/stats")
        self.assertEqual(stats["batch"], {"max_results": srv.MAX_BATCH, "per_hour": 40})


class ConsistencyTest(unittest.TestCase):
    """A document has to agree with itself.

    `score` is a geometric mean of six numbers in the same record and `ilp`,
    `filp` and `mlp` are ratios of two each, so a result edited after the
    benchmark wrote it contradicts itself. That is the only check a hub can
    make on its own, and these tests fix both what it catches and what it
    openly does not.
    """

    SAMPLE = Path(__file__).resolve().parent.parent / "testdata" / "full-run.json"

    def sample(self):
        """A real document, straight from the benchmark."""
        if not self.SAMPLE.exists():          # a checkout without the sample
            self.skipTest(f"{self.SAMPLE} is not here")
        return json.loads(self.SAMPLE.read_text())

    def test_a_real_document_passes(self):
        """The formulas here are the benchmark's, and this is what says so.

        Every record of a genuine --full run -- eight cores, eight threads and
        the machine total -- has to recompute. If it stops doing so, the hub
        has started refusing every honest upload, and that is far worse than
        anything it was meant to catch.
        """
        _, rows = srv.validate(self.sample())
        self.assertEqual(len(rows), 17)

    def test_an_edited_score_is_refused(self):
        doc = self.sample()
        doc["cores"][0]["score"] = 99999.0
        with self.assertRaises(srv.Invalid) as cm:
            srv.validate(doc)
        self.assertIn("score", str(cm.exception))

    def test_an_edited_total_is_refused(self):
        doc = self.sample()
        doc["total"]["score"] = 99999.0
        with self.assertRaises(srv.Invalid):
            srv.validate(doc)

    def test_a_nudged_score_is_refused(self):
        """One per cent is far too small to be worth taking, and is taken."""
        doc = self.sample()
        doc["cores"][0]["score"] *= 1.01
        with self.assertRaises(srv.Invalid):
            srv.validate(doc)

    def test_editing_an_input_breaks_a_ratio(self):
        """Raising a component instead of the score is caught by `ilp`, which
        is that component over the latency beside it."""
        doc = self.sample()
        doc["cores"][0]["int_thr_mops"] *= 3.0
        with self.assertRaises(srv.Invalid) as cm:
            srv.validate(doc)
        self.assertIn("ilp", str(cm.exception))

    def test_a_shaved_latency_is_refused(self):
        doc = self.sample()
        doc["cores"][0]["mem_lat8_ns"] /= 5.0
        with self.assertRaises(srv.Invalid) as cm:
            srv.validate(doc)
        self.assertIn("mlp", str(cm.exception))

    def test_a_run_with_no_dispatch_measurement_passes(self):
        """`disp_span` is written as null both when there was no measurement
        and when the span came out at zero, and the score treats those two
        differently. An honest record of the first kind must not be refused."""
        doc = document("per-core", cores=[
            core(cpu=0, disp_prediction="unknown", disp_span=None,
                 disp_cap_calls=None)])
        _, rows = srv.validate(doc)
        self.assertIsNone(rows[0]["disp_span"])

    def test_a_consistent_forgery_passes(self):
        """What this check is not.

        Rescale every input and recompute the numbers derived from them and the
        document agrees with itself perfectly, because it is arithmetic and not
        evidence. Nothing running on a machine its owner controls can do
        better, and the hub says so rather than implying otherwise -- this test
        is here so that stays a decision and not an oversight.
        """
        doc = self.sample()
        for rec in doc["cores"] + doc["threads"] + [doc["total"]]:
            for key in ("int_thr_mops", "fp_thr_mflops", "mul_thr_mmul_s",
                        "disp_thr_mcall_s", "int_lat_mops", "fp_lat_mflops"):
                rec[key] *= 1.2
            n = doc["config"]["threads"] if rec is doc["total"] else 1.0
            rec["score"] = _score_of(rec, n)
        _, rows = srv.validate(doc)
        self.assertEqual(len(rows), 17)


class AccountTest(unittest.TestCase):
    """Accounts: signing in, what an account may do, and what it may not."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        # Both limiters off: these tests sign in far more often in a second
        # than any person does in fifteen minutes. The registration puzzle is
        # off too -- what it costs to get an account is RegistrationTest's
        # subject, and solving one per account here would only slow this down.
        cls.server = srv.build_server(str(Path(cls.tmp.name) / "a.sqlite3"),
                                      "127.0.0.1", 0, rate_limit=0, auth_limit=0,
                                      register_pow=0, register_limit=0)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.seq = 0

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def account(self, password="a good long password"):
        """A fresh account, signed in, as (browser, name, token)."""
        type(self).seq += 1
        name = f"tester{self.seq}"
        b = Browser(self.base)
        status, out = b.call("/api/auth/register", "POST",
                             {"name": name, "password": password})
        self.assertEqual(status, 201, out)
        return b, name, out["user"]["api_token"]

    def anon(self):
        return Browser(self.base)

    # -- registering and signing in ---------------------------------------
    def test_register_signs_the_browser_in(self):
        b, name, _ = self.account()
        status, out = b.call("/api/auth/me")
        self.assertEqual(status, 200)
        self.assertEqual(out["user"]["name"], name)
        self.assertEqual(out["user"]["runs"], 0)

    def test_the_two_cookies_carry_the_right_flags(self):
        """The session cookie is the page's to send, not to read.

        The CSRF cookie is the opposite: the page has to read it to echo it
        back, which is the whole mechanism, so it alone is not HttpOnly.
        """
        req = urllib.request.Request(
            self.base + "/api/auth/register", method="POST",
            data=json.dumps({"name": "cookieflags",
                             "password": "a good long password"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            set_cookies = resp.headers.get_all("Set-Cookie") or []
        session = [c for c in set_cookies if c.startswith(srv.SESSION_COOKIE + "=")]
        csrf = [c for c in set_cookies if c.startswith(srv.CSRF_COOKIE + "=")]
        self.assertEqual(len(session), 1, set_cookies)
        self.assertEqual(len(csrf), 1, set_cookies)
        self.assertIn("HttpOnly", session[0])
        self.assertIn("SameSite=Lax", session[0])
        self.assertNotIn("HttpOnly", csrf[0])
        # Plain http in the tests, so neither may claim Secure -- a Secure
        # cookie over http is one the browser drops, i.e. a session that
        # silently never works.
        self.assertNotIn("Secure", session[0])

    def test_bad_names_and_passwords_are_refused(self):
        b = self.anon()
        for name in ("ab", "no spaces", "a" * 33, "-leading"):
            with self.subTest(name):
                code, _ = b.call("/api/auth/register", "POST",
                                 {"name": name, "password": "a good long password"})
                self.assertEqual(code, 400)
        code, err = b.call("/api/auth/register", "POST",
                           {"name": "shorty", "password": "short"})
        self.assertEqual(code, 400)
        self.assertIn("8 characters", err["error"])

    def test_names_are_taken_case_insensitively(self):
        _, name, _ = self.account()
        code, err = self.anon().call("/api/auth/register", "POST",
                                     {"name": name.upper(), "password": "another password"})
        self.assertEqual(code, 409)
        self.assertIn("taken", err["error"])

    def test_login_and_logout(self):
        b, name, _ = self.account("a good long password")
        self.assertEqual(b.call("/api/auth/logout", "POST", {})[0], 200)
        self.assertIsNone(b.call("/api/auth/me")[1]["user"])

        code, err = b.call("/api/auth/login", "POST",
                           {"name": name, "password": "wrong password"})
        self.assertEqual(code, 401)
        # The message must not say which half was wrong.
        self.assertIn("wrong name or password", err["error"])

        status, out = b.call("/api/auth/login", "POST",
                             {"name": name.upper(), "password": "a good long password"})
        self.assertEqual(status, 200)
        self.assertEqual(out["user"]["name"], name)

    def test_password_change_ends_other_sessions(self):
        b, name, _ = self.account("a good long password")
        other = Browser(self.base)
        self.assertEqual(other.call("/api/auth/login", "POST",
                                    {"name": name, "password": "a good long password"})[0], 200)
        self.assertEqual(b.call("/api/auth/password", "POST",
                                {"current": "a good long password",
                                 "password": "an even better password"})[0], 200)
        self.assertIsNone(other.call("/api/auth/me")[1]["user"])
        # The browser that changed it stays in.
        self.assertIsNotNone(b.call("/api/auth/me")[1]["user"])
        self.assertEqual(b.call("/api/auth/login", "POST",
                                {"name": name, "password": "a good long password"})[0], 401)

    def test_password_change_needs_the_current_one(self):
        b, _, _ = self.account()
        code, _ = b.call("/api/auth/password", "POST",
                         {"current": "not it", "password": "a new long password"})
        self.assertEqual(code, 401)

    # -- changing the name -------------------------------------------------
    def rename(self, b, name, password="a good long password"):
        return b.call("/api/auth/rename", "POST", {"name": name, "password": password})

    def test_rename_keeps_the_runs_and_the_session(self):
        b, name, token = self.account()
        _, up = self.anon().call("/api/runs", "POST", document(),
                                 headers={"Authorization": f"Bearer {token}"})
        new = name + "-new"
        status, out = self.rename(b, new)
        self.assertEqual(status, 200, out)
        self.assertEqual(out["user"]["name"], new)
        self.assertEqual(b.call("/api/auth/me")[1]["user"]["name"], new)
        self.assertEqual(b.call(f"/api/runs/{up['id']}")[1]["user"], new)
        self.assertEqual(self.anon().call(f"/api/users/{new}")[0], 200)
        self.assertEqual(self.anon().call(f"/api/users/{name}")[0], 404)
        # The old name is free to log in with no longer, the new one is not.
        self.assertEqual(self.anon().call("/api/auth/login", "POST",
                         {"name": new, "password": "a good long password"})[0], 200)

    def test_rename_is_allowed_once_a_week(self):
        b, name, _ = self.account()
        self.assertEqual(self.rename(b, name + "-one")[0], 200)
        code, err = self.rename(b, name + "-two")
        self.assertEqual(code, 429)
        self.assertIn("7 days", err["error"])
        self.assertIsNotNone(b.call("/api/auth/me")[1]["user"]["rename_after"])
        # Eight days on, it is allowed again.
        old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(
            timespec="seconds").replace("+00:00", "Z")
        with self.server.store.connect() as db:
            db.execute("UPDATE users SET renamed_at = ? WHERE name = ?",
                       (old, name + "-one"))
        self.assertEqual(self.rename(b, name + "-two")[0], 200)

    def test_rename_refuses_a_name_another_account_has(self):
        b, name, _ = self.account()
        _, other, _ = self.account()
        for taken in (other, other.upper()):
            with self.subTest(taken):
                code, err = self.rename(b, taken)
                self.assertEqual(code, 409)
                self.assertIn("taken", err["error"])
        # A refused rename does not use up the week.
        self.assertEqual(self.rename(b, name + "-free")[0], 200)

    def test_rename_may_change_only_the_case(self):
        b, name, _ = self.account()
        code, out = self.rename(b, name.upper())
        self.assertEqual(code, 200, out)
        self.assertEqual(out["user"]["name"], name.upper())

    def test_rename_needs_the_password_and_a_usable_name(self):
        b, name, _ = self.account()
        self.assertEqual(self.rename(b, name + "x", "not the password")[0], 401)
        self.assertEqual(self.rename(b, "no spaces")[0], 400)
        self.assertEqual(self.rename(b, name)[0], 400)
        self.assertEqual(self.anon().call("/api/auth/rename", "POST",
                         {"name": "whoever1", "password": "x"})[0], 401)

    # -- uploads: variants grouped, labels and notes edited ----------------
    VARIANTS = ((False, False), (True, False), (False, True), (True, True))

    def post(self, token=None, label=None, notes=None, vec=False, fma=False,
             cpu="Test CPU 9000"):
        """Upload one run as cpcpub would, as (status, reply)."""
        doc = document()
        doc["build"] = dict(doc["build"], vectorize=vec, fma=fma)
        doc["system"] = dict(doc["system"], cpu_models=cpu)
        q = urllib.parse.urlencode({k: v for k, v in
                                    (("label", label), ("notes", notes)) if v})
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.anon().call("/api/runs" + ("?" + q if q else ""), "POST", doc,
                                headers=headers)

    def variants(self, token, base, notes=None, cpu="Test CPU 9000", which=VARIANTS):
        """A --variants upload: one run per build, each label ending in it."""
        ids = []
        for vec, fma in which:
            tag = f"({'vector' if vec else 'scalar'}-{'fma' if fma else 'nofma'})"
            status, out = self.post(token, f"{base} {tag}" if base else tag, notes,
                                    vec, fma, cpu)
            self.assertEqual(status, 201, out)
            ids.append(out["id"])
        return ids

    def test_variants_of_one_upload_are_listed_together(self):
        b, _, token = self.account()
        box = self.variants(token, "box", "quiet fan")
        solo = self.post(token, "solo")[1]["id"]
        other = self.post(token, "elsewhere", cpu="Other CPU 1")[1]["id"]
        status, out = b.call("/api/uploads?user=me")
        self.assertEqual(status, 200, out)
        self.assertEqual((out["total"], out["uploads"], out["runs"]), (2, 3, 6))
        # CPUs by their newest upload; on each, its uploads newest first.
        self.assertEqual([c["cpu_models"] for c in out["cpus"]],
                         ["Other CPU 1", "Test CPU 9000"])
        self.assertEqual(out["cpus"][0]["uploads"][0]["runs"][0]["id"], other)
        here = out["cpus"][1]
        self.assertEqual(here["runs"], 5)
        self.assertEqual([u["id"] for u in here["uploads"]], [solo, box[0]])
        grouped = here["uploads"][1]
        self.assertEqual(grouped["label"], "box")
        self.assertEqual(grouped["notes"], "quiet fan")
        self.assertEqual([r["id"] for r in grouped["runs"]], box)
        self.assertEqual([r["variant"] for r in grouped["runs"]],
                         ["scalar-nofma", "vector-nofma", "scalar-fma", "vector-fma"])
        self.assertTrue(all(r["tagged"] for r in grouped["runs"]))
        self.assertFalse(here["uploads"][0]["runs"][0]["tagged"])
        # Every run says which upload it came in.
        self.assertEqual(b.call(f"/api/runs/{box[3]}")[1]["upload_id"], box[0])
        # Paged by CPU, so a CPU's uploads are never split across pages.
        page = b.call("/api/uploads?user=me&limit=1&offset=1")[1]
        self.assertEqual(len(page["cpus"]), 1)
        self.assertEqual(page["cpus"][0]["runs"], 5)

    def test_what_starts_a_new_upload(self):
        _, name, token = self.account()

        def uploads():
            out = self.anon().call(f"/api/uploads?user={name}")[1]
            return [[r["id"] for r in u["runs"]] for c in out["cpus"] for u in c["uploads"]]

        # A build the upload already has: the same command, run again.
        first = self.variants(token, "box", which=self.VARIANTS[:2])
        again = self.variants(token, "box", which=self.VARIANTS[:1])
        self.assertEqual(uploads(), [again, first])
        # A different label, or none of the variant tags.
        relabelled = self.variants(token, "other box", which=self.VARIANTS[1:2])
        plain = [self.post(token, "box", vec=True, fma=True)[1]["id"]]
        self.assertEqual(uploads(), [plain, relabelled, again, first])
        # Different notes.
        a = self.variants(token, "", notes="one", which=self.VARIANTS[:1])
        b = self.variants(token, "", notes="two", which=self.VARIANTS[1:2])
        self.assertEqual(uploads()[:2], [b, a])
        # Too long after the run before it.
        c = self.variants(token, "late", which=self.VARIANTS[:1])
        late = (datetime.now(timezone.utc) - srv.UPLOAD_GAP - timedelta(minutes=1))
        with self.server.store.connect() as db:
            db.execute("UPDATE runs SET created_at = ? WHERE id = ?",
                       (late.isoformat(timespec="seconds").replace("+00:00", "Z"), c[0]))
        d = self.variants(token, "late", which=self.VARIANTS[1:2])
        self.assertEqual(uploads()[:2], [d, c])
        # An unlabelled --variants upload is still one upload.
        e = self.variants(token, "")
        self.assertEqual(uploads()[0], e)

    def test_only_the_same_account_continues_an_upload(self):
        _, _, mine = self.account()
        _, _, theirs = self.account()
        first = self.variants(mine, "box", which=self.VARIANTS[:1])[0]
        stranger = self.variants(theirs, "box", which=self.VARIANTS[1:2])[0]
        anon = self.post(None, "box (scalar-fma)", fma=True)[1]["id"]
        second = self.variants(mine, "box", which=self.VARIANTS[1:2])[0]
        get = lambda i: self.anon().call(f"/api/runs/{i}")[1]["upload_id"]
        self.assertEqual(get(stranger), stranger)
        self.assertEqual(get(anon), anon)
        # Another account's runs in between do not break this one's upload.
        self.assertEqual(get(second), first)

    def test_uploads_are_grouped_for_runs_stored_before_grouping(self):
        _, _, token = self.account()
        ids = self.variants(token, "older")
        with self.server.store.connect() as db:
            db.execute("UPDATE runs SET upload_id = NULL")
        store = srv.Store(self.server.store.path)
        with store.connect() as db:
            got = [db.execute("SELECT upload_id FROM runs WHERE id = ?", (i,)).fetchone()[0]
                   for i in ids]
        self.assertEqual(got, [ids[0]] * 4)

    def test_uploads_listing_needs_an_account_named(self):
        anon = self.anon()
        self.assertEqual(anon.call("/api/uploads")[0], 400)
        self.assertEqual(anon.call("/api/uploads?user=me")[0], 401)
        status, out = anon.call("/api/uploads?user=nobody-at-all")
        self.assertEqual(status, 200)
        self.assertEqual((out["cpus"], out["total"]), ([], 0))

    def test_owner_edits_label_and_notes(self):
        b, _, token = self.account()
        run = self.post(token, "old label", "old notes")[1]["id"]
        status, out = b.call(f"/api/runs/{run}", "PATCH", {"label": "  new label "})
        self.assertEqual(status, 200, out)
        self.assertEqual((out["label"], out["notes"]), ("new label", "old notes"))
        status, out = b.call(f"/api/runs/{run}", "PATCH", {"notes": ""})
        self.assertEqual((status, out["notes"]), (200, None))
        stored = self.anon().call(f"/api/runs/{run}")[1]
        self.assertEqual((stored["label"], stored["notes"]), ("new label", None))
        # The upload token is the account too.
        status, _ = self.anon().call(f"/api/runs/{run}", "PATCH", {"label": "by token"},
                                     headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(status, 200)
        self.assertEqual(b.call(f"/api/runs/{run}", "PATCH", {})[0], 400)
        self.assertEqual(b.call(f"/api/runs/{run}", "PATCH", {"label": 5})[0], 400)

    def test_only_the_owner_or_the_token_holder_may_edit(self):
        owner, _, token = self.account()
        run = self.post(token, "mine")[1]["id"]
        stranger, _, _ = self.account()
        self.assertEqual(stranger.call(f"/api/runs/{run}", "PATCH", {"label": "x"})[0], 403)
        self.assertEqual(self.anon().call(f"/api/runs/{run}", "PATCH", {"label": "x"})[0], 401)
        self.assertEqual(self.anon().call(f"/api/runs/{run}", "PATCH", {"label": "x"},
                         headers={"X-Delete-Token": "not-the-token"})[0], 403)
        # The page's own request, without the CSRF header another site lacks.
        self.assertEqual(owner.call(f"/api/runs/{run}", "PATCH", {"label": "x"},
                                    csrf=False)[0], 403)
        self.assertEqual(self.anon().call(f"/api/runs/{run}")[1]["label"], "mine")
        # An anonymous run, by its delete token.
        status, up = self.post(None, "anonymous")
        self.assertEqual(status, 201)
        self.assertEqual(stranger.call(f"/api/runs/{up['id']}", "PATCH", {"label": "x"})[0], 403)
        status, out = self.anon().call(f"/api/runs/{up['id']}", "PATCH", {"label": "renamed"},
                                       headers={"X-Delete-Token": up["delete_token"]})
        self.assertEqual((status, out["label"]), (200, "renamed"))
        self.assertEqual(self.anon().call("/api/runs/999999", "PATCH", {"label": "x"},
                         headers={"X-Delete-Token": "whatever"})[0], 403)

    # -- adopting anonymous runs -------------------------------------------
    def adopt(self, b, *tokens, csrf=True):
        return b.call("/api/auth/adopt", "POST", {"tokens": list(tokens)}, csrf=csrf)

    def test_an_account_adopts_runs_by_their_delete_tokens(self):
        b, name, _ = self.account()
        _, one = self.post(None, "anon one")
        _, two = self.post(None, "anon two")
        status, out = self.adopt(b, one["delete_token"], two["delete_token"],
                                 one["delete_token"])
        self.assertEqual(status, 200, out)
        self.assertEqual(out, {"adopted": sorted([one["id"], two["id"]]),
                               "already": [], "refused": 0})
        self.assertEqual(self.anon().call(f"/api/runs/{one['id']}")[1]["user"], name)
        self.assertEqual(b.call("/api/auth/me")[1]["user"]["runs"], 2)
        # Once more is a no-op, and the token still edits the run.
        self.assertEqual(self.adopt(b, one["delete_token"])[1]["already"], [one["id"]])
        status, _ = self.anon().call(f"/api/runs/{one['id']}", "PATCH", {"label": "x"},
                                     headers={"X-Delete-Token": one["delete_token"]})
        self.assertEqual(status, 200)

    def test_another_accounts_run_is_not_adopted(self):
        _, owner, token = self.account()
        thief, _, _ = self.account()
        _, theirs = self.post(token, "theirs")
        _, free = self.post(None, "free")
        status, out = self.adopt(thief, theirs["delete_token"], "no-such-token",
                                 free["delete_token"])
        self.assertEqual(status, 200)
        self.assertEqual(out, {"adopted": [free["id"]], "already": [], "refused": 2})
        self.assertEqual(self.anon().call(f"/api/runs/{theirs['id']}")[1]["user"], owner)

    def test_adopting_needs_a_signed_in_page(self):
        _, up = self.post(None, "anon")
        self.assertEqual(self.adopt(self.anon(), up["delete_token"])[0], 401)
        b, _, _ = self.account()
        self.assertEqual(self.adopt(b, up["delete_token"], csrf=False)[0], 403)
        self.assertEqual(self.adopt(b)[0], 400)
        self.assertEqual(b.call("/api/auth/adopt", "POST", {"tokens": [5]})[0], 400)
        self.assertEqual(self.adopt(b, *["t"] * (srv.MAX_ADOPT + 1))[0], 400)
        self.assertIsNone(self.anon().call(f"/api/runs/{up['id']}")[1]["user"])

    def test_adopted_variants_become_one_upload(self):
        b, _, _ = self.account()
        tokens = []
        for vec, fma in self.VARIANTS[:2]:
            tag = f"({'vector' if vec else 'scalar'}-{'fma' if fma else 'nofma'})"
            status, out = self.post(None, f"adopted box {tag}", None, vec, fma,
                                    "Adopted CPU")
            self.assertEqual(status, 201, out)
            tokens.append(out["delete_token"])
        self.adopt(b, *tokens)
        _, listing = b.call("/api/uploads?user=me")
        cpu = next(c for c in listing["cpus"] if c["cpu_models"] == "Adopted CPU")
        self.assertEqual(len(cpu["uploads"]), 1)
        self.assertEqual(len(cpu["uploads"][0]["runs"]), 2)

    def test_a_hidden_run_is_listed_to_its_owner_alone(self):
        b, name, token = self.account()
        _, shown = self.post(token, "shown")
        _, hidden = self.post(token, "hidden")
        status, out = b.call(f"/api/runs/{hidden['id']}", "PATCH", {"hidden": True})
        self.assertEqual((status, out["hidden"]), (200, True))

        def ids(listing):
            return {r["id"]: r["hidden"] for c in listing["cpus"]
                    for u in c["uploads"] for r in u["runs"]}
        self.assertEqual(ids(b.call("/api/uploads?user=me")[1]),
                         {shown["id"]: False, hidden["id"]: True})
        self.assertEqual(ids(b.call(f"/api/uploads?user={name}")[1]),
                         {shown["id"]: False, hidden["id"]: True})
        self.assertEqual(ids(self.anon().call(f"/api/uploads?user={name}")[1]),
                         {shown["id"]: False})
        _, mine = b.call("/api/runs?user=me")
        self.assertEqual((mine["total"], len(mine["runs"])), (2, 2))
        _, public = self.anon().call(f"/api/runs?user={name}")
        self.assertEqual([r["id"] for r in public["runs"]], [shown["id"]])
        self.assertEqual(self.anon().call(f"/api/users/{name}")[1]["user"]["runs"], 1)
        self.assertEqual(b.call("/api/auth/me")[1]["user"]["runs"], 2)
        _, board = self.anon().call(f"/api/cores?scope=cpu&user={name}")
        self.assertEqual([c["run_id"] for c in board["cores"]], [shown["id"]])

    # -- uploading as an account -------------------------------------------
    def test_upload_token_attributes_the_run(self):
        b, name, token = self.account()
        status, out = self.anon().call(
            "/api/runs", "POST", document(),
            headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(status, 201)
        self.assertEqual(out["user"], name)
        _, run = b.call(f"/api/runs/{out['id']}")
        self.assertEqual(run["user"], name)
        # ...and the board says who it belongs to.
        _, board = b.call(f"/api/cores?scope=cpu&user={name}")
        self.assertEqual([c["user"] for c in board["cores"]], [name])

    def test_unknown_upload_token_is_refused_not_anonymous(self):
        """A stale token must fail loudly rather than upload to nobody."""
        code, err = self.anon().call(
            "/api/runs", "POST", document(),
            headers={"Authorization": "Bearer no-such-token"})
        self.assertEqual(code, 401)
        self.assertIn("unknown upload token", err["error"])

    def test_rotating_the_token_retires_the_old_one(self):
        b, _, old = self.account()
        status, out = b.call("/api/auth/token", "POST", {})
        self.assertEqual(status, 200)
        self.assertNotEqual(out["api_token"], old)
        self.assertEqual(self.anon().call(
            "/api/runs", "POST", document(),
            headers={"Authorization": f"Bearer {old}"})[0], 401)
        self.assertEqual(self.anon().call(
            "/api/runs", "POST", document(),
            headers={"Authorization": f"Bearer {out['api_token']}"})[0], 201)

    def test_cookie_upload_requires_the_csrf_token(self):
        """What stops another site posting for a signed-in visitor."""
        b, _, _ = self.account()
        self.assertEqual(b.call("/api/runs", "POST", document(), csrf=False)[0], 403)
        self.assertEqual(b.call("/api/runs", "POST", document())[0], 201)

    def test_a_refused_write_leaves_the_connection_usable(self):
        """A refusal decided before the body was read must still eat it.

        Otherwise the unread document sits in front of the next request on the
        same keep-alive connection and is parsed as one.
        """
        b, _, _ = self.account()
        conn = http.client.HTTPConnection(self.base.removeprefix("http://"))
        body = json.dumps({"current": "x", "password": "a good long password"})
        # No CSRF header: refused before change_password reads anything.
        conn.request("POST", "/api/auth/password", body, {
            "Content-Type": "application/json",
            "Cookie": f"{srv.SESSION_COOKIE}={b.cookie(srv.SESSION_COOKIE)}",
        })
        refused = conn.getresponse()
        self.assertEqual(refused.status, 403)
        refused.read()
        # The next request on the same connection must be understood.
        conn.request("GET", "/api/stats")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        json.loads(resp.read())
        conn.close()

    def test_anonymous_upload_still_needs_nothing(self):
        status, out = self.anon().call("/api/runs", "POST", document())
        self.assertEqual(status, 201)
        self.assertIsNone(out["user"])
        self.assertTrue(out["delete_token"])

    # -- who may withdraw a run --------------------------------------------
    def test_owner_withdraws_without_a_token(self):
        b, _, _ = self.account()
        _, out = b.call("/api/runs", "POST", document())
        self.assertEqual(b.call(f"/api/runs/{out['id']}", "DELETE")[0], 200)
        self.assertEqual(b.call(f"/api/runs/{out['id']}")[0], 404)

    def test_another_account_cannot_withdraw_it(self):
        mine, _, _ = self.account()
        theirs, _, _ = self.account()
        _, out = mine.call("/api/runs", "POST", document())
        code, _ = theirs.call(f"/api/runs/{out['id']}", "DELETE")
        self.assertEqual(code, 403)
        self.assertEqual(mine.call(f"/api/runs/{out['id']}")[0], 200)

    def test_the_delete_token_still_works_on_an_owned_run(self):
        """Signing in must not strand a run uploaded from a shell."""
        _, _, token = self.account()
        _, out = self.anon().call("/api/runs", "POST", document(),
                                  headers={"Authorization": f"Bearer {token}"})
        status, _ = self.anon().call(
            f"/api/runs/{out['id']}", "DELETE",
            headers={"X-Delete-Token": out["delete_token"]})
        self.assertEqual(status, 200)

    def test_withdrawing_needs_one_claim_or_the_other(self):
        _, out = self.anon().call("/api/runs", "POST", document())
        code, err = self.anon().call(f"/api/runs/{out['id']}", "DELETE")
        self.assertEqual(code, 401)
        self.assertIn("delete token", err["error"])

    # -- listing and profiles ----------------------------------------------
    def test_user_filter_narrows_to_one_account(self):
        b, name, _ = self.account()
        b.call("/api/runs", "POST", document())
        self.anon().call("/api/runs", "POST", document())
        _, mine = b.call("/api/runs?user=me")
        self.assertTrue(mine["runs"])
        self.assertTrue(all(r["user"] == name for r in mine["runs"]))
        _, by_name = self.anon().call(f"/api/runs?user={name}")
        self.assertEqual([r["id"] for r in by_name["runs"]],
                         [r["id"] for r in mine["runs"]])

    def test_user_me_needs_a_session(self):
        code, _ = self.anon().call("/api/runs?user=me")
        self.assertEqual(code, 401)

    def test_an_unknown_name_matches_nothing(self):
        self.anon().call("/api/runs", "POST", document())
        _, out = self.anon().call("/api/runs?user=nobody-at-all")
        self.assertEqual(out["runs"], [])

    def test_profile_leaks_neither_token_nor_hash(self):
        _, name, token = self.account()   # token must appear nowhere below
        status, out = self.anon().call(f"/api/users/{name}")
        self.assertEqual(status, 200)
        self.assertEqual(out["user"]["name"], name)
        self.assertNotIn("api_token", out["user"])
        self.assertNotIn("password_hash", out["user"])
        self.assertNotIn(token, json.dumps(out))
        self.assertEqual(self.anon().call("/api/users/nobody-here")[0], 404)

    def test_own_token_is_only_shown_to_a_session(self):
        b, _, token = self.account()
        _, mine = b.call("/api/auth/me")
        self.assertEqual(mine["user"]["api_token"], token)

    # -- closing an account -------------------------------------------------
    def test_closing_deletes_the_runs_and_frees_the_name(self):
        b, name, _ = self.account("a good long password")
        _, run = b.call("/api/runs", "POST", document())
        code, _ = b.call("/api/auth/close", "POST", {"password": "wrong"})
        self.assertEqual(code, 401)
        status, out = b.call("/api/auth/close", "POST",
                             {"password": "a good long password"})
        self.assertEqual(status, 200)
        self.assertEqual(out["runs_deleted"], 1)
        self.assertEqual(b.call(f"/api/runs/{run['id']}")[0], 404)
        self.assertIsNone(b.call("/api/auth/me")[1]["user"])
        # The name is free again, and the new account inherits nothing.
        fresh = Browser(self.base)
        status, out = fresh.call("/api/auth/register", "POST",
                                 {"name": name, "password": "a different password"})
        self.assertEqual(status, 201)
        self.assertEqual(out["user"]["runs"], 0)

    def test_a_run_survives_its_uploader_being_forgotten(self):
        """user_id may name an account that is gone; the run still reads back."""
        _, out = self.anon().call("/api/runs", "POST", document())
        store = srv.Store(str(Path(self.tmp.name) / "a.sqlite3"))
        with store.connect() as db:
            db.execute("UPDATE runs SET user_id = 424242 WHERE id = ?", (out["id"],))
        status, run = self.anon().call(f"/api/runs/{out['id']}")
        self.assertEqual(status, 200)
        self.assertIsNone(run["user"])


class RegistrationTest(unittest.TestCase):
    """What it costs to get an account, and what it costs to script one.

    Each hub here is built with the policy under test, because the policy is
    what is being tested -- there is no way to change it on a running server,
    deliberately.
    """

    def serve(self, **policy) -> str:
        """A hub with this registration policy, torn down with the test."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        server = srv.build_server(str(Path(tmp.name) / "r.sqlite3"),
                                  "127.0.0.1", 0, rate_limit=0, auth_limit=0,
                                  **policy)
        # Kept so a test can look at the challenge store, which is the only
        # state here that is not in the database.
        self.hub = server
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def solved(self, base: str, name: str, **over) -> tuple[int, dict]:
        """Register `name`, doing whatever work the hub asks for."""
        b = Browser(base)
        _, policy = b.call("/api/auth/challenge")
        body: dict[str, Any] = {"name": name, "password": "a good long password"}
        if policy.get("bits"):
            nonce = 0
            while not srv.pow_solved(policy["challenge"], str(nonce), policy["bits"]):
                nonce += 1
            body.update(challenge=policy["challenge"], nonce=str(nonce))
        body.update(over)
        return b.call("/api/auth/register", "POST", body)

    # -- the proof of work -------------------------------------------------
    def test_the_hub_says_what_it_asks_for(self):
        base = self.serve()
        status, policy = Browser(base).call("/api/auth/challenge")
        self.assertEqual(status, 200)
        self.assertTrue(policy["open"])
        self.assertFalse(policy["invite_required"])
        self.assertEqual(policy["bits"], srv.REGISTER_POW_BITS)
        self.assertTrue(policy["challenge"])

    def test_the_policy_costs_nothing_to_read(self):
        """The page reads this on every draw, so it must not issue a puzzle.

        A challenge per page view would fill the store with challenges nobody
        solves, and spend the rate limit that registering needs on people who
        only came to read the board.
        """
        base = self.serve()
        status, policy = Browser(base).call("/api/auth/policy")
        self.assertEqual(status, 200)
        self.assertTrue(policy["open"])
        self.assertEqual(policy["bits"], srv.REGISTER_POW_BITS)
        self.assertNotIn("challenge", policy)
        self.assertEqual(len(self.hub.challenges.live), 0)
        # And the puzzle endpoint still does issue one.
        Browser(base).call("/api/auth/challenge")
        self.assertEqual(len(self.hub.challenges.live), 1)

    def test_the_page_can_finish_the_hardest_puzzle_a_hub_may_ask_for(self):
        """The ceiling is what the browser can search, not a round number.

        app.js gives up after 2^26 nonces and the challenge expires besides, so
        a maximum that needs more work than either allows is a door that looks
        open and is not.
        """
        searchable = 1 << 26                       # the limit in solveChallenge
        expected = 1 << srv.MAX_POW_BITS
        self.assertLessEqual(expected * 8, searchable,
                             "the search would fail more often than rarely")

    def test_a_solved_challenge_registers(self):
        base = self.serve(register_pow=12)
        status, out = self.solved(base, "worker")
        self.assertEqual(status, 201, out)
        self.assertEqual(out["user"]["name"], "worker")

    def test_registering_without_one_is_refused(self):
        base = self.serve(register_pow=12)
        status, err = Browser(base).call(
            "/api/auth/register", "POST",
            {"name": "scripted", "password": "a good long password"})
        self.assertEqual(status, 400)
        self.assertIn("challenge", err["error"])

    def test_a_wrong_answer_is_refused(self):
        base = self.serve(register_pow=12)
        b = Browser(base)
        _, policy = b.call("/api/auth/challenge")
        status, err = b.call("/api/auth/register", "POST",
                             {"name": "scripted", "password": "a good long password",
                              "challenge": policy["challenge"], "nonce": "0"})
        # 1 in 4096 that nonce 0 happens to solve a 12-bit challenge, which
        # would be a pass reported as a failure -- so the assertion is on
        # either outcome being the honest one for what was sent.
        if status == 201:
            self.skipTest("nonce 0 solved it by luck")
        self.assertEqual(status, 403)
        self.assertIn("not solved", err["error"])

    def test_a_challenge_is_spent_once(self):
        base = self.serve(register_pow=12)
        b = Browser(base)
        _, policy = b.call("/api/auth/challenge")
        nonce = 0
        while not srv.pow_solved(policy["challenge"], str(nonce), policy["bits"]):
            nonce += 1
        body = {"password": "a good long password",
                "challenge": policy["challenge"], "nonce": str(nonce)}
        first, _ = b.call("/api/auth/register", "POST", dict(body, name="first"))
        second, err = b.call("/api/auth/register", "POST", dict(body, name="second"))
        self.assertEqual(first, 201)
        self.assertEqual(second, 403)
        self.assertIn("expired", err["error"])

    def test_an_invented_challenge_is_refused(self):
        base = self.serve(register_pow=0)      # even where none is required
        status, _ = Browser(base).call(
            "/api/auth/register", "POST",
            {"name": "nopuzzle", "password": "a good long password"})
        self.assertEqual(status, 201)

    def test_challenges_expire(self):
        challenges = srv.Challenges(ttl=-1.0)
        token = challenges.issue()
        self.assertFalse(challenges.spend(token))

    def test_the_challenge_store_is_bounded(self):
        challenges = srv.Challenges(cap=8)
        issued = [challenges.issue() for _ in range(64)]
        self.assertLessEqual(len(challenges.live), 8)
        self.assertFalse(challenges.spend(issued[0]))

    def test_pow_difficulty_is_capped(self):
        # A hub cannot ask for work no browser will finish, however it is
        # started.
        base = self.serve(register_pow=1 << 20)
        _, policy = Browser(base).call("/api/auth/challenge")
        self.assertEqual(policy["bits"], srv.MAX_POW_BITS)

    # -- the other two doors ----------------------------------------------
    def test_a_closed_hub_takes_no_accounts(self):
        base = self.serve(register_open=False, register_pow=0)
        _, policy = Browser(base).call("/api/auth/challenge")
        self.assertFalse(policy["open"])
        self.assertNotIn("challenge", policy)
        status, err = self.solved(base, "hopeful")
        self.assertEqual(status, 403)
        self.assertIn("not taking new accounts", err["error"])

    def test_a_closed_hub_still_takes_uploads(self):
        base = self.serve(register_open=False)
        status, _ = Browser(base).call("/api/runs", "POST", document())
        self.assertEqual(status, 201)

    def test_an_invite_code_is_required_when_set(self):
        base = self.serve(register_pow=0, signup_token="the-code")
        _, policy = Browser(base).call("/api/auth/challenge")
        self.assertTrue(policy["invite_required"])
        refused, err = self.solved(base, "outsider")
        self.assertEqual(refused, 403)
        self.assertIn("invite", err["error"])
        wrong, _ = self.solved(base, "outsider", invite="not-the-code")
        self.assertEqual(wrong, 403)
        ok, out = self.solved(base, "insider", invite="the-code")
        self.assertEqual(ok, 201, out)

    def test_one_address_cannot_take_every_name(self):
        base = self.serve(register_pow=0, register_limit=2)
        for i in range(2):
            status, out = self.solved(base, f"early{i}")
            self.assertEqual(status, 201, out)
        status, err = self.solved(base, "late")
        self.assertEqual(status, 429)
        self.assertIn("too many accounts", err["error"])

    def test_an_unsolved_attempt_does_not_use_up_the_limit(self):
        """The limit counts accounts made, not attempts refused.

        Otherwise a script that never solves anything could still exhaust the
        quota of everyone behind the same address.
        """
        base = self.serve(register_pow=12, register_limit=1)
        for i in range(5):
            status, _ = Browser(base).call(
                "/api/auth/register", "POST",
                {"name": f"noise{i}", "password": "a good long password"})
            self.assertEqual(status, 400)
        status, out = self.solved(base, "genuine")
        self.assertEqual(status, 201, out)

    def test_a_bad_name_is_refused_before_the_work_is_checked(self):
        """A typo should not cost the puzzle -- it is not an attempt at abuse."""
        base = self.serve(register_pow=12)
        status, _ = Browser(base).call("/api/auth/register", "POST",
                                       {"name": "x", "password": "short"})
        self.assertEqual(status, 400)

    def test_a_taken_name_does_not_cost_the_puzzle(self):
        """Nor does a name someone else already has -- also not abuse.

        Proved by re-using the very challenge the refused attempt carried: if it
        had been spent, the second attempt could not register with it.
        """
        base = self.serve(register_pow=12)
        b = Browser(base)
        self.assertEqual(self.solved(base, "taken")[0], 201)

        _, policy = b.call("/api/auth/challenge")
        nonce = 0
        while not srv.pow_solved(policy["challenge"], str(nonce), policy["bits"]):
            nonce += 1
        body = {"password": "a good long password",
                "challenge": policy["challenge"], "nonce": str(nonce)}
        again, err = b.call("/api/auth/register", "POST", dict(body, name="TAKEN"))
        self.assertEqual(again, 409)
        self.assertIn("taken", err["error"])
        spare, out = b.call("/api/auth/register", "POST", dict(body, name="free"))
        self.assertEqual(spare, 201, out)


class ProofOfWorkTest(unittest.TestCase):
    def test_a_solution_has_the_leading_zero_bits(self):
        nonce = 0
        while not srv.pow_solved("abc", str(nonce), 12):
            nonce += 1
        digest = hashlib.sha256(f"abc:{nonce}".encode()).digest()
        self.assertEqual(int.from_bytes(digest, "big") >> (256 - 12), 0)

    def test_zero_bits_accepts_anything(self):
        self.assertTrue(srv.pow_solved("abc", "0", 0))

    def test_the_challenge_is_part_of_what_is_hashed(self):
        nonce = 0
        while not srv.pow_solved("abc", str(nonce), 12):
            nonce += 1
        # The same nonce against another challenge is worth nothing, which is
        # what stops one solution being spent on a second registration.
        self.assertFalse(srv.pow_solved("abd", str(nonce), 12))


class PasswordTest(unittest.TestCase):
    def test_hash_round_trip(self):
        stored = srv.hash_password("a good long password")
        self.assertTrue(srv.password_matches("a good long password", stored))
        self.assertFalse(srv.password_matches("a good long passwore", stored))

    def test_each_hash_is_salted(self):
        self.assertNotEqual(srv.hash_password("same password"),
                            srv.hash_password("same password"))

    def test_a_damaged_hash_matches_nothing(self):
        for stored in ("", "nonsense", "scrypt$x$8$1$aa$bb", "md5$1$1$1$aa$bb"):
            with self.subTest(stored):
                self.assertFalse(srv.password_matches("anything", stored))

    def test_account_limit_is_separate_from_the_address_limit(self):
        limiter = srv.RateLimiter(2, 60.0)
        self.assertTrue(limiter.allow("user:1", 2 * srv.ACCOUNT_RATE_FACTOR))
        for _ in range(2 * srv.ACCOUNT_RATE_FACTOR - 1):
            self.assertTrue(limiter.allow("user:1", 2 * srv.ACCOUNT_RATE_FACTOR))
        self.assertFalse(limiter.allow("user:1", 2 * srv.ACCOUNT_RATE_FACTOR))
        # ...and the address it came from still has its own budget.
        self.assertTrue(limiter.allow("10.0.0.1"))

    def test_an_address_is_forgotten_after_its_window(self):
        # What PRIVACY.md says: an address is held no longer than the
        # limit needs it, whether or not it ever comes back.
        limiter = srv.RateLimiter(2, 60.0)
        clock = [1000.0]
        with mock.patch.object(srv.time, "monotonic", lambda: clock[0]):
            limiter.swept = clock[0]
            self.assertTrue(limiter.allow("10.0.0.1"))
            clock[0] += 30
            self.assertTrue(limiter.allow("10.0.0.2"))
            clock[0] += 31          # .1's hit has left the window, .2's has not
            self.assertTrue(limiter.allow("10.0.0.3"))
            self.assertEqual(sorted(limiter.hits), ["10.0.0.2", "10.0.0.3"])


class ValidateTest(unittest.TestCase):
    def test_an_unnamed_target_is_read_off_the_machine(self):
        def target(reported, machine, release="6.12.0"):
            doc = document()
            doc["build"] = dict(doc["build"], target=reported)
            doc["system"] = dict(doc["system"], machine=machine, release=release)
            return srv.validate(doc)[0]["target"]

        self.assertEqual(target("unknown", "armv6l"), "armv6")
        self.assertEqual(target("unknown", "armv7l"), "armv7")
        self.assertEqual(target(None, "armv7l"), "armv7")
        self.assertEqual(target("unknown", "armv8l"), "arm")
        # Raspberry Pi OS builds ARMv6 code on every Pi.
        self.assertEqual(target("unknown", "armv7l", "6.12.62+rpt-rpi-v7"), "armv6")
        self.assertEqual(target("unknown", "armv7l", "5.10.103-v7l+"), "armv6")
        # A 64-bit machine may be running a 32-bit build: not guessed at.
        self.assertEqual(target("unknown", "aarch64"), "unknown")
        self.assertEqual(target("unknown", "x86_64"), "unknown")
        # What a build says of itself always stands.
        self.assertEqual(target("armv6", "armv7l"), "armv6")
        self.assertEqual(target("x86_64", "armv7l"), "x86_64")

    def test_stored_unknown_targets_are_named_on_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "t.sqlite3")
            store = srv.Store(path)
            doc = document()
            doc["system"] = dict(doc["system"], machine="armv6l")
            run, cores = srv.validate(doc)
            run_id, _ = store.insert(dict(run, target="unknown"), cores, "{}", None, None)
            srv.Store(path)
            with store.connect() as db:
                got = db.execute("SELECT target FROM runs WHERE id = ?", (run_id,)).fetchone()[0]
            self.assertEqual(got, "armv6")

    @staticmethod
    def rated_doc(mhz):
        """A full run whose every record carries `mhz` as a rated clock, as
        Windows' `~MHz` did before 0.5.1 -- under an INT-lat of 4000."""
        doc = document("full")
        for key in ("cores", "threads"):
            doc[key] = [dict(r, mhz=mhz, mhz_src="rated") for r in doc[key]]
        doc["total"] = dict(doc["total"], mhz=mhz, mhz_src="rated")
        return doc

    def test_a_rated_base_clock_gives_way_to_int_lat(self):
        _, rows = srv.validate(self.rated_doc(806.0))
        self.assertEqual({(r["scope"], r["mhz"], r["mhz_src"]) for r in rows},
                         {("cpu", 4000.0, "estimated"),
                          ("thread", 4000.0, "estimated"),
                          ("total", 4000.0, "estimated")})

    def test_a_rated_clock_int_lat_allows_stands(self):
        # 4000 is within 10% of 3700: a clock named a little low, not turbo.
        _, rows = srv.validate(self.rated_doc(3700.0))
        self.assertEqual({(r["mhz"], r["mhz_src"]) for r in rows},
                         {(3700.0, "rated")})

    def test_the_total_keeps_the_weakest_source_of_its_threads(self):
        doc = document("full")
        doc["threads"] = [dict(doc["threads"][0], mhz=806.0, mhz_src="rated"),
                          dict(doc["threads"][1], mhz=3000.0, mhz_src="measured")]
        doc["total"] = dict(doc["total"], mhz=1903.0, mhz_src="rated")
        _, rows = srv.validate(doc)
        total = next(r for r in rows if r["scope"] == "total")
        self.assertEqual((total["mhz"], total["mhz_src"]), (3500.0, "estimated"))

    def test_stored_rated_base_clocks_are_fixed_on_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "t.sqlite3")
            store = srv.Store(path)
            run, cores = srv.validate(document("full"))
            run_id, _ = store.insert(run, cores, "{}", None, None)
            # As a hub before 0.5.1 stored a Windows run.
            with store.connect() as db:
                db.execute("UPDATE cores SET mhz = 806, mhz_src = 'rated'")
            srv.Store(path)
            with store.connect() as db:
                got = db.execute("SELECT DISTINCT mhz, mhz_src FROM cores "
                                 "WHERE run_id = ?", (run_id,)).fetchall()
            self.assertEqual([tuple(r) for r in got], [(4000.0, "estimated")])

    def test_scope_defaults_per_mode(self):
        doc = document("per-core")
        for c in doc["cores"]:
            del c["scope"]
        _, rows = srv.validate(doc)
        self.assertEqual({r["scope"] for r in rows}, {"cpu"})

    def test_unknown_scope_rejected(self):
        with self.assertRaises(srv.Invalid):
            srv.validate(document("per-core", cores=[core(scope="weird")]))

    def test_missing_measurements_rejected(self):
        empty: dict[str, object] = {k: None for k in core()}
        empty["scope"] = "cpu"
        with self.assertRaises(srv.Invalid):
            srv.validate(document("per-core", cores=[empty]))

    def test_nulls_survive(self):
        """A run with no memory phases: the whole memory group is absent."""
        _, rows = srv.validate(document("per-core", cores=[core(
            mem_gbps=None, mem_lat_ns=None, mem_lat8_ns=None, mlp=None)]))
        self.assertIsNone(rows[0]["mem_gbps"])
        self.assertIsNone(rows[0]["mlp"])
        self.assertEqual(rows[0]["int_thr"], 20000.0)


class HardeningTest(unittest.TestCase):
    """What a hostile client might try, behind a proxy as the public hub is."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.server = srv.build_server(str(Path(cls.tmp.name) / "h.sqlite3"),
                                      "127.0.0.1", 0, rate_limit=0, auth_limit=2,
                                      register_pow=0, register_limit=0,
                                      trust_proxy=True)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def send(self, path, method="GET", body=None, headers=None,
             ctype="application/json"):
        """(status, reply, headers), with nothing added that was not asked for."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        data = body if body is None or isinstance(body, bytes) \
            else json.dumps(body).encode()
        h = dict(headers or {})
        if data is not None and ctype:
            h["Content-Type"] = ctype
        conn.request(method, path, body=data, headers=h)
        resp = conn.getresponse()
        out = resp.read()
        conn.close()
        try:
            reply = json.loads(out)
        except ValueError:
            reply = out
        return resp.status, reply, resp.headers

    def login(self, via):
        return self.send("/api/auth/login", "POST",
                         {"name": "nobody", "password": "not the password"},
                         {"X-Forwarded-For": via})[0]

    def test_a_forged_forwarded_for_does_not_reset_the_limit(self):
        # The proxy appends the address it saw; what comes before is the
        # client's to write, and must not be a fresh address per request.
        codes = [self.login(f"10.9.9.{i}, 198.51.100.7") for i in range(3)]
        self.assertEqual(codes, [401, 401, 429])

    def test_one_ipv6_network_is_one_address(self):
        codes = [self.login(f"2001:db8:7:7::{i}") for i in range(1, 4)]
        self.assertEqual(codes, [401, 401, 429])
        self.assertEqual(self.login("2001:db8:7:8::1"), 401)

    def test_cloudflare_names_the_visitor_and_no_one_else_does(self):
        # Through an edge: the visitor's address counts, not the edge's.
        codes = [self.send("/api/auth/login", "POST",
                           {"name": "nobody", "password": "not the password"},
                           {"X-Forwarded-For": f"2606:4700::{i}",
                            "CF-Connecting-IP": "198.51.100.33"})[0]
                 for i in range(1, 4)]
        self.assertEqual(codes, [401, 401, 429])
        # From anywhere else the header is the client's own, and ignored.
        codes = [self.send("/api/auth/login", "POST",
                           {"name": "nobody", "password": "not the password"},
                           {"X-Forwarded-For": "198.51.100.44",
                            "CF-Connecting-IP": f"198.51.100.{50 + i}"})[0]
                 for i in range(3)]
        self.assertEqual(codes, [401, 401, 429])

    def test_the_page_links_its_script_by_content(self):
        status, page, _ = self.send("/")
        self.assertEqual(status, 200)
        def digest(name):
            return hashlib.sha256((srv.STATIC_DIR / name).read_bytes()).hexdigest()[:12]
        self.assertIn(f'src="/app.js?v={digest("app.js")}"'.encode(), page)
        self.assertIn(f'href="/style.css?v={digest("style.css")}"'.encode(), page)
        self.assertEqual(self.send(f"/app.js?v={digest('app.js')}")[0], 200)

    def test_address_groups(self):
        self.assertEqual(srv.address_group("2001:db8::1"), "2001:db8::/64")
        self.assertEqual(srv.address_group("::ffff:192.0.2.1"), "192.0.2.1")
        self.assertEqual(srv.address_group("192.0.2.1"), "192.0.2.1")
        self.assertEqual(srv.address_group("not an address"), "not an address")

    def test_a_form_on_another_site_cannot_sign_anyone_up(self):
        # text/plain is what an HTML form can send anywhere without asking.
        body = {"name": "formfilled", "password": "a good long password"}
        status, _, _ = self.send("/api/auth/register", "POST", body,
                                 {"X-Forwarded-For": "203.0.113.50"},
                                 ctype="text/plain")
        self.assertEqual(status, 415)
        self.assertIsNone(self.server.store.user_by_name("formfilled"))

    def test_https_cookies_are_host_only(self):
        via = {"X-Forwarded-For": "203.0.113.60", "X-Forwarded-Proto": "https"}
        status, _, headers = self.send(
            "/api/auth/register", "POST",
            {"name": "hostonly", "password": "a good long password"}, via)
        self.assertEqual(status, 201)
        cookies = headers.get_all("Set-Cookie")
        self.assertTrue(all(c.startswith("__Host-") and "Secure" in c
                            for c in cookies), cookies)
        session = next(c for c in cookies
                       if c.startswith(f"__Host-{srv.SESSION_COOKIE}="))
        value = session.split(";")[0].split("=", 1)[1]
        # The same session under the bare name -- which a sibling host could
        # have set for the parent domain -- is not believed over https.
        _, me, _ = self.send("/api/auth/me", headers=dict(
            via, Cookie=f"{srv.SESSION_COOKIE}={value}"))
        self.assertIsNone(me["user"])
        _, me, _ = self.send("/api/auth/me", headers=dict(
            via, Cookie=f"__Host-{srv.SESSION_COOKIE}={value}"))
        self.assertEqual(me["user"]["name"], "hostonly")

    def test_a_token_that_is_not_ascii_is_refused_not_crashed_on(self):
        status, out, _ = self.send("/api/runs", "POST", document())
        self.assertEqual(status, 201)
        for method in ("DELETE", "PATCH"):
            status, _, _ = self.send(
                f"/api/runs/{out['id']}", method,
                {"label": "x"} if method == "PATCH" else None,
                {"X-Delete-Token": "été".encode().decode("latin-1")})
            self.assertEqual(status, 403, method)

    def test_a_seed_past_a_signed_integer_still_uploads(self):
        doc = document()
        doc["config"]["seed"] = (1 << 64) - 1
        status, out, _ = self.send("/api/runs", "POST", doc)
        self.assertEqual(status, 201, out)
        _, run, _ = self.send(f"/api/runs/{out['id']}")
        self.assertIsNone(run["seed"])
        _, raw, _ = self.send(f"/api/runs/{out['id']}/raw")
        self.assertEqual(raw["config"]["seed"], (1 << 64) - 1)

    def test_ids_too_large_for_a_column_are_refused(self):
        self.assertEqual(self.send("/api/runs/" + "9" * 30)[0], 404)
        self.assertEqual(self.send("/api/cores?run=" + "9" * 30)[0], 400)
        self.assertEqual(self.send("/api/cores?ids=" + "9" * 30)[0], 400)

    def test_a_batch_of_one_long_array_stops_at_the_limit(self):
        text = "[" + ",".join(["{}"] * (srv.MAX_BATCH + 5)) + "]"
        with self.assertRaises(srv.Invalid) as cm:
            srv.split_documents(text)
        self.assertIn("at most", str(cm.exception))

    def test_log_lines_hide_tokens_and_control_characters(self):
        line = srv.TOKEN_IN_QUERY_RE.sub(
            r"\1[redacted]", '"DELETE /api/runs/1?token=s3cret&x=1 HTTP/1.1"')
        self.assertNotIn("s3cret", line)
        self.assertEqual(srv.CONTROL_RE.sub("?", "a\x1b[31mb"), "a?[31mb")


# The v0.5.0 release's assets, as GitHub lists them.
RELEASE_050 = """
SHA256SUMS verified-builds.json cpcpub-0.5.0-android.aab cpcpub-0.5.0-android.apk
cpcpub-0.5.0-windows-setup.exe cpcpub-gui_0.5.0-1_all.deb cpcpub-gui-0.5.0-1.noarch.rpm
cpcpub-gui-0.5.0-1-any.pkg.tar.zst cpcpub_0.5.0-1_amd64.deb cpcpub_0.5.0-1_arm64.deb
cpcpub_0.5.0-1_armhf.deb cpcpub_0.5.0-1_riscv64.deb cpcpub_0.5.0-1_loong64.deb
cpcpub_0.5.0-1_ppc64el.deb cpcpub_0.5.0-1_s390x.deb cpcpub-0.5.0-1.x86_64.rpm
cpcpub-0.5.0-1.aarch64.rpm cpcpub-0.5.0-1.armv7hl.rpm cpcpub-0.5.0-1.riscv64.rpm
cpcpub-0.5.0-1.loongarch64.rpm cpcpub-0.5.0-1.ppc64le.rpm cpcpub-0.5.0-1.s390x.rpm
cpcpub-0.5.0-1-x86_64.pkg.tar.zst cpcpub-0.5.0-1-aarch64.pkg.tar.zst
cpcpub-0.5.0-1-armv7h.pkg.tar.zst cpcpub-0.5.0-1-riscv64.pkg.tar.zst
cpcpub-0.5.0-1-loong64.pkg.tar.zst cpcpub-0.5.0-1-ppc64le.pkg.tar.zst
cpcpub-termux_0.5.0-1_aarch64.deb cpcpub-termux_0.5.0-1_arm.deb
cpcpub-linux-x86_64 cpcpub-linux-x86_64-v3 cpcpub-linux-aarch64 cpcpub-linux-armv7
cpcpub-linux-armv7-neon cpcpub-linux-armv6 cpcpub-linux-riscv64 cpcpub-linux-riscv64-rva23
cpcpub-linux-loongarch64 cpcpub-linux-ppc64le cpcpub-linux-s390x cpcpub-macos-x86_64
cpcpub-macos-arm64 cpcpub-windows-x86_64.exe cpcpub-windows-x86_64-v3.exe
cpcpub-windows-arm64.exe cpcpub-android-arm64 cpcpub-android-armv7 cpcpub-rp2040.uf2
cpcpub-rp2350-arm.uf2 cpcpub-rp2350-riscv.uf2 cpcpub-esp32.bin cpcpub-esp32s2.bin
cpcpub-esp32s3.bin cpcpub-esp32c3.bin cpcpub-esp32c6.bin cpcpub-esp32p4.bin
""".split()


class FakeGitHub:
    """Just enough of GitHub for the mirror: the latest release, its files."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.release: dict = {}
        self.asked_etag: list[str | None] = []
        fake = self

        class Handler(srv.BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                if self.path == "/latest":
                    fake.asked_etag.append(self.headers.get("If-None-Match"))
                    body = json.dumps(fake.release).encode()
                    etag = '"' + hashlib.sha256(body).hexdigest() + '"'
                    if self.headers.get("If-None-Match") == etag:
                        self.send_response(304)
                        self.end_headers()
                        return
                    self.send_response(200)
                    self.send_header("ETag", etag)
                else:
                    body = fake.files.get(self.path.rsplit("/", 1)[-1])
                    if body is None:
                        self.send_response(404)
                        self.end_headers()
                        return
                    self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = srv.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.api_url = base + "/latest"
        self.asset_url = base + "/{repo}/{tag}/{name}"

    def publish(self, tag, names, *, sums_for=None, missing=()):
        """A release `tag` holding `names`, each file's bytes its own name."""
        version = tag.removeprefix("v")
        self.files = {n.replace("VER", version): f"{tag} {n}".encode() * 50
                      for n in names}
        listed = sums_for if sums_for is not None else self.files
        self.files["SHA256SUMS"] = "".join(
            f"{hashlib.sha256(self.files[n]).hexdigest()}  {n}\n"
            for n in listed).encode()
        self.release = {
            "tag_name": tag, "html_url": f"https://github.com/o/r/releases/tag/{tag}",
            "published_at": "2026-10-30T14:29:58Z",
            "assets": [{"name": n, "size": len(b), "state": "uploaded"}
                       for n, b in self.files.items() if n not in missing],
        }

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


SOME = ["cpcpub_VER-1_amd64.deb", "cpcpub-linux-x86_64", "cpcpub-VER-android.apk",
        "cpcpub-VER-android.aab", "verified-builds.json", "cpcpub-rp2040.uf2"]


class DownloadsTest(unittest.TestCase):
    """The release mirror, and the hub serving what it holds."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "downloads"
        self.gh = FakeGitHub()
        self.server = srv.build_server(str(Path(self.tmp.name) / "d.sqlite3"),
                                       "127.0.0.1", 0, rate_limit=0)
        self.mirror = srv.Mirror(self.dir, "o/r", api_url=self.gh.api_url,
                                 asset_url=self.gh.asset_url)
        self.server.downloads = self.mirror
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.gh.close()
        self.tmp.cleanup()

    def get(self, path, method="GET", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, headers=headers or {})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, body, resp

    def test_only_what_people_run_is_taken(self):
        left = sorted(n for n in RELEASE_050 if not downloads.place("0.5.0", n))
        self.assertEqual(left, ["SHA256SUMS", "cpcpub-0.5.0-android.aab",
                                "verified-builds.json"])
        # A Windows binary without .exe, or anything else with one, is no file
        # the release makes.
        self.assertIsNone(downloads.place("0.5.0", "cpcpub-windows-x86_64"))
        self.assertIsNone(downloads.place("0.5.0", "cpcpub-linux-x86_64.exe"))
        self.assertIsNone(downloads.place("0.5.0", "cpcpub_0.4.0-1_amd64.deb"))

    def test_the_layout_groups_as_the_release_notes_do(self):
        files = [{"name": n, "size": 1, "sha256": "0" * 64} for n in RELEASE_050
                 if downloads.place("0.5.0", n)]
        lay = downloads.layout({"tag": "v0.5.0", "files": files})
        self.assertEqual([p["id"] for p in lay["packages"]],
                         ["debian", "fedora", "arch", "windows", "android", "termux"])
        debian = lay["packages"][0]
        self.assertEqual([b["arch"] for b in debian["bench"]], downloads.ARCHES)
        self.assertEqual(debian["gui"]["name"], "cpcpub-gui_0.5.0-1_all.deb")
        self.assertEqual(lay["packages"][3]["file"]["href"],
                         "/downloads/v0.5.0/cpcpub-0.5.0-windows-setup.exe")
        self.assertEqual([o["id"] for o in lay["binaries"]["oses"]],
                         ["linux", "macos", "windows", "android"])
        x86 = lay["binaries"]["rows"][0]
        self.assertEqual([f["variant"] for f in x86["cells"]["windows"]],
                         ["x86_64", "x86_64-v3"])
        self.assertEqual([c["chip"] for f in lay["firmware"] for c in f["chips"]],
                         ["RP2040", "RP2350, Arm", "RP2350, RISC-V", "ESP32",
                          "ESP32-S2", "ESP32-S3", "ESP32-C3", "ESP32-C6", "ESP32-P4"])
        self.assertEqual(len(lay["files"]), len(files))

    def test_a_hub_with_nothing_mirrored_says_so(self):
        status, body, _ = self.get("/api/downloads")
        self.assertEqual((status, json.loads(body)), (200, {"release": None}))
        self.assertEqual(self.get("/downloads/v0.5.0/cpcpub-linux-x86_64")[0], 404)

    def test_a_release_is_mirrored_and_served(self):
        self.gh.publish("v0.5.0", SOME)
        self.assertEqual(self.mirror.sync(), "now serving v0.5.0: 4 files")
        status, body, _ = self.get("/api/downloads")
        lay = json.loads(body)
        self.assertEqual(lay["release"], "v0.5.0")
        self.assertEqual(sorted(f["name"] for f in lay["files"]),
                         ["cpcpub-0.5.0-android.apk", "cpcpub-linux-x86_64",
                          "cpcpub-rp2040.uf2", "cpcpub_0.5.0-1_amd64.deb"])

        want = self.gh.files["cpcpub-0.5.0-android.apk"]
        status, body, resp = self.get("/downloads/v0.5.0/cpcpub-0.5.0-android.apk")
        self.assertEqual((status, body), (200, want))
        self.assertEqual(resp.getheader("Content-Type"),
                         "application/vnd.android.package-archive")
        self.assertIn("attachment", resp.getheader("Content-Disposition", ""))
        self.assertEqual(resp.getheader("ETag"),
                         '"' + hashlib.sha256(want).hexdigest() + '"')

        # HEAD says the size and sends nothing; a range sends that part.
        status, body, resp = self.get("/downloads/v0.5.0/cpcpub-linux-x86_64", "HEAD")
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(int(resp.getheader("Content-Length", "-1")),
                         len(self.gh.files["cpcpub-linux-x86_64"]))
        status, body, resp = self.get("/downloads/v0.5.0/cpcpub-linux-x86_64",
                                      headers={"Range": "bytes=3-9"})
        self.assertEqual((status, body), (206, self.gh.files["cpcpub-linux-x86_64"][3:10]))
        size = len(self.gh.files["cpcpub-linux-x86_64"])
        self.assertEqual(resp.getheader("Content-Range"), f"bytes 3-9/{size}")
        status, _, resp = self.get("/downloads/v0.5.0/cpcpub-linux-x86_64",
                                   headers={"Range": f"bytes={size}-"})
        self.assertEqual(status, 416)

        # What is not a served file of the served release is not found.
        for path in ("/downloads/v0.5.0/cpcpub-0.5.0-android.aab",
                     "/downloads/v0.5.0/verified-builds.json",
                     "/downloads/v0.5.0/SHA256SUMS",
                     "/downloads/v0.5.0/manifest.json",
                     "/downloads/v0.4.5/cpcpub-linux-x86_64",
                     "/downloads/v0.5.0/../../d.sqlite3",
                     "/downloads/v0.5.0/.hidden",
                     "/downloads/CURRENT"):
            self.assertEqual(self.get(path)[0], 404, path)

        status, _, resp = self.get("/downloads/latest/cpcpub-linux-x86_64")
        self.assertEqual((status, resp.getheader("Location")),
                         (302, "/downloads/v0.5.0/cpcpub-linux-x86_64"))
        status, _, resp = self.get("/download")
        self.assertEqual((status, resp.getheader("Location")), (302, "/#download"))

    def test_every_file_the_release_lists_is_served(self):
        # Names of up to 34 characters, as Arch, Termux and the GUI packages
        # have: the route once checked them against the account-name pattern,
        # which stops at 32, and served those as not found.
        self.gh.publish("v0.5.0", RELEASE_050)
        self.mirror.sync()
        files = json.loads(self.get("/api/downloads")[1])["files"]
        self.assertTrue(any(len(f["name"]) > 32 for f in files))
        for f in files:
            self.assertEqual(self.get(f"/downloads/v0.5.0/{f['name']}", "HEAD")[0],
                             200, f["name"])

    def test_downloads_are_counted_as_a_number_and_nothing_else(self):
        self.gh.publish("v0.5.0", SOME)
        self.mirror.sync()
        apk, binary = "cpcpub-0.5.0-android.apk", "cpcpub-linux-x86_64"

        def counts():
            return json.loads(self.get("/api/downloads")[1])["downloads"]

        self.assertEqual(counts(), {"total": 0, "files": {}})
        self.get(f"/downloads/v0.5.0/{apk}")
        self.get(f"/downloads/v0.5.0/{apk}")
        self.get(f"/downloads/v0.5.0/{binary}")
        self.assertEqual(counts(), {"total": 3, "files": {apk: 2, binary: 1}})

        # Not downloads: a HEAD, the rest of a file already begun, a refusal,
        # and a name that is not served.
        self.get(f"/downloads/v0.5.0/{binary}", "HEAD")
        self.get(f"/downloads/v0.5.0/{binary}", headers={"Range": "bytes=3-9"})
        self.get(f"/downloads/v0.5.0/{binary}", headers={"Range": "bytes=999999-"})
        self.get("/downloads/v0.5.0/SHA256SUMS")
        self.assertEqual(counts()["total"], 3)
        # The first part of one is the start of a download.
        self.get(f"/downloads/v0.5.0/{binary}", headers={"Range": "bytes=0-4"})
        self.assertEqual(counts()["files"][binary], 2)

        # All that is stored is the count.
        with self.server.store.connect() as db:
            cols = [r["name"] for r in db.execute("PRAGMA table_info(download_counts)")]
        self.assertEqual(cols, ["release", "name", "count"])

    def test_an_unchanged_release_is_asked_about_cheaply(self):
        self.gh.publish("v0.5.0", SOME)
        self.mirror.sync()
        self.assertIsNone(self.mirror.sync())
        self.assertIsNotNone(self.gh.asked_etag[-1])

    def test_a_file_that_does_not_match_its_checksum_is_refused(self):
        self.gh.publish("v0.5.0", SOME)
        self.mirror.sync()
        self.gh.publish("v0.5.1", SOME)
        self.gh.files["cpcpub-linux-x86_64"] = b"x" * len(self.gh.files["cpcpub-linux-x86_64"])
        with self.assertRaises(downloads.MirrorError):
            self.mirror.sync()
        # The release served before stays served, and nothing half-made is left.
        self.assertEqual((self.mirror.manifest() or {}).get("tag"), "v0.5.0")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["CURRENT", "v0.5.0"])

    def test_a_release_still_uploading_waits(self):
        self.gh.publish("v0.5.0", SOME, missing={"cpcpub-linux-x86_64"})
        with self.assertRaises(downloads.MirrorError) as cm:
            self.mirror.sync()
        self.assertIn("not fully uploaded", str(cm.exception))
        self.gh.publish("v0.5.0", SOME, missing={"SHA256SUMS"})
        with self.assertRaises(downloads.MirrorError):
            self.mirror.sync()
        self.assertIsNone(self.mirror.manifest())
        # Asked in full each time, since nothing was taken.
        self.assertEqual(self.gh.asked_etag, [None, None])

    def test_a_new_release_replaces_the_old_and_leaves_the_rest(self):
        self.gh.publish("v0.5.0", SOME)
        self.mirror.sync()
        stranger = self.dir / "keep-me"
        stranger.mkdir()
        self.gh.publish("v0.5.1", SOME)
        self.assertEqual(self.mirror.sync(), "now serving v0.5.1: 4 files")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         ["CURRENT", "keep-me", "v0.5.1"])
        self.assertEqual(self.get("/downloads/v0.5.0/cpcpub-linux-x86_64")[0], 404)
        status, body, _ = self.get("/downloads/v0.5.1/cpcpub-linux-x86_64")
        self.assertEqual((status, body), (200, self.gh.files["cpcpub-linux-x86_64"]))

    def test_a_release_rebuilt_under_its_tag_is_taken_again(self):
        self.gh.publish("v0.5.0", SOME)
        self.mirror.sync()
        self.gh.publish("v0.5.0", SOME)
        self.gh.files["cpcpub-linux-x86_64"] = b"rebuilt"
        self.gh.files["SHA256SUMS"] = "".join(
            f"{hashlib.sha256(b).hexdigest()}  {n}\n"
            for n, b in self.gh.files.items() if n != "SHA256SUMS").encode()
        for a in self.gh.release["assets"]:
            a["size"] = len(self.gh.files[a["name"]])
        self.assertEqual(self.mirror.sync(), "now serving v0.5.0: 4 files")
        self.assertEqual(self.get("/downloads/v0.5.0/cpcpub-linux-x86_64")[1], b"rebuilt")

    def test_a_sync_by_the_script_shows_without_a_restart(self):
        other = srv.Mirror(self.dir, "o/r", api_url=self.gh.api_url,
                           asset_url=self.gh.asset_url)
        self.assertIsNone(json.loads(self.get("/api/downloads")[1])["release"])
        self.gh.publish("v0.5.0", SOME)
        other.sync()
        self.assertEqual(json.loads(self.get("/api/downloads")[1])["release"], "v0.5.0")

    def test_byte_ranges(self):
        self.assertEqual(srv.byte_range("bytes=0-0", 10), (0, 0))
        self.assertEqual(srv.byte_range("bytes=5-", 10), (5, 9))
        self.assertEqual(srv.byte_range("bytes=-3", 10), (7, 9))
        self.assertEqual(srv.byte_range("bytes=-30", 10), (0, 9))
        self.assertEqual(srv.byte_range("bytes=8-100", 10), (8, 9))
        self.assertEqual(srv.byte_range("bytes=10-", 10), "unsatisfiable")
        self.assertEqual(srv.byte_range("bytes=-0", 10), "unsatisfiable")
        for whole in (None, "", "bytes=5-2", "bytes=0-1,4-5", "items=0-1", "bytes=-"):
            self.assertIsNone(srv.byte_range(whole, 10), whole)

    def test_checksum_lists_are_read_strictly(self):
        a, b = "a" * 64, "B" * 64
        got = downloads.parse_sums(f"{a}  one\n{b} *two\nshort  three\n{a}  ../up\n")
        self.assertEqual(got, {"one": a, "two": b.lower()})

    def test_a_bad_repository_is_refused(self):
        with self.assertRaises(ValueError):
            srv.Mirror(self.dir, "not a repo")


if __name__ == "__main__":
    unittest.main(verbosity=2)
