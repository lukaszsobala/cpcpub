#!/usr/bin/env python3
"""cpcpub results hub: upload `cpcpub --json` output and compare runs.

Standard library only (http.server + sqlite3), matching the benchmark's own
no-dependencies policy.

    python3 web/server.py --db runs.sqlite3 --port 8080

An upload may be signed in or anonymous. An account is a name and a password;
it owns the runs uploaded under it, so they can be withdrawn from any browser
and carry a submitter on the board, and it carries an upload token that makes
`cpcpub --submit URL --token T` put the run on your name. Anonymous uploads
keep working exactly as before, delete token and all -- an account is a
convenience, never a gate. A rate limit, per account where there is one and per
address where there is not, keeps the obvious abuse out; registering costs a
small proof of work as well, which is what keeps a script from taking every
name on the hub overnight (--register-pow, --register-limit, --signup-token,
--no-register).

It can also serve the latest release's downloads, for its Download tab:
--mirror OWNER/REPO keeps a copy of them, fetched from GitHub and checked
against the release's checksums (see downloads.py).

Nothing here claims an uploaded number was produced by the machine it names:
an account says the same person uploaded these runs, not that they are true.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import mimetypes
import os
import re
import secrets
import sqlite3
import statistics
import sys
import threading
import time
import urllib.parse
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

from downloads import NAME_RE, REPO_RE, TAG_RE, Mirror, layout

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"
SCHEMA_PATH = HERE / "schema.sql"

SCHEMA_ID = "cpu-bench/1"
MODES = {"threads", "per-core", "full"}
SCOPES = {"cpu", "thread", "total"}

# What the benchmark writes for build.binary_sha256, and what a release manifest
# publishes for each binary it attached.
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
BUILDS_SCHEMA_ID = "cpu-bench-builds/1"

# Where an upload carries its proof, when it has one. A header rather than a
# field in the document, because the signature is over the document's exact
# bytes and cannot be inside them.

MAX_BODY = 1 << 20          # 1 MiB: a --per-core dump of 256 CPUs is ~120 KiB
MAX_AUTH_BODY = 4096        # a name and a password; anything larger is noise
MAX_DRAIN = 8 << 20         # how much of a refused body to swallow before
                            # hanging up, so the client can read the 413
MAX_CORES = 1024
MAX_PAGE = 500              # the most rows any listing will return at once, so
                            # the page's own picker and a hand-edited limit=
                            # both stop at the same place
MAX_OFFSET = 1 << 30        # how far into a listing paging may go; past the end
                            # is an empty page, not an error
MAX_TEXT = 200
MAX_NOTES = 2000
MAX_EDIT_BODY = 3 * (MAX_TEXT + MAX_NOTES) + 256   # a label and notes, escaped
MAX_ADOPT = 100             # delete tokens one request may adopt: what the page
                            # keeps of a browser's anonymous uploads
MAX_ADOPT_BODY = MAX_ADOPT * 64 + 256
# A batch: many machines' results in one file, for an account. Big enough for
# a hundred machines with all four build variants each; the body cap is that
# many large documents. Its own hourly allowance, per account and counted in
# documents stored, is --batch-limit.
MAX_BATCH = 400
MAX_BATCH_BODY = 16 << 20
BATCH_LIMIT = 1000
# How long apart two variants of one --variants run may land and still be
# called one upload. A variant takes minutes on a laptop and can take most of
# an hour on a machine with a hundred cores to walk; this is the far side of
# that, not a guess at the usual.
UPLOAD_GAP = timedelta(hours=3)
# What `cpcpub --variants --submit` ends each variant's label with.
VARIANT_TAG_RE = re.compile(r"(?:^| )\((?:scalar|vector)-(?:fma|nofma)\)$")
MAX_VALUE = 1e12            # nothing this benchmark reports comes near this
ACCOUNT_RATE_FACTOR = 4     # how much more room a signed-in submitter gets
SQLITE_MAX_INT = (1 << 63) - 1   # the largest integer a column can hold
HANDLER_TIMEOUT = 30.0      # seconds a connection may sit silent, so a client
                            # that opens sockets and sends nothing cannot hold
                            # a thread each for ever
MAX_KEYS = 100_000          # addresses a rate limiter remembers at most

# Accounts -------------------------------------------------------------------
#
# A name has to be safe to print on the board and unambiguous between accounts:
# letters, digits and the three joiners, starting on a letter or digit.
ACCOUNT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$")
MIN_PASSWORD = 8
MAX_PASSWORD = 256          # scrypt costs the same either way; this bounds work
SESSION_DAYS = 30
RENAME_DAYS = 7             # how long an account keeps a name once it has changed it

# Registration -------------------------------------------------------------
#
# There is no email address to confirm and no captcha to farm out, so what
# stands between this hub and a script that registers all night is a small
# proof of work plus a hard per-address limit. Neither stops a determined
# person, and neither is meant to: they turn "free and instant" into "a second
# of CPU each and five an hour", which is what actually deters bulk signup.
#
# 16 bits is ~65k SHA-256 attempts. The page hashes at roughly 130k a second --
# it carries its own SHA-256, because crypto.subtle needs a secure context this
# hub may not have -- so that is about half a second: unnoticed by a person, and
# the difference between a thousand accounts a minute and a few. A hub that is
# being hammered can raise it.
REGISTER_POW_BITS = 16
# Each bit doubles the work, so at that rate 20 bits averages ~8 seconds and 22
# ~30 -- an average over a memoryless search, so a given attempt can take
# several times that and one in ten takes almost none. The ceiling is where the
# page is, not where a server would like it: it searches 2^26 nonces before
# giving up, and the challenge below expires, and 22 bits leaves both a wide
# margin (2^26 is sixteen times the mean, 600s is twenty). Asking for work that
# runs past either would be a door that looks open and is not.
MAX_POW_BITS = 22
CHALLENGE_TTL = 600.0       # seconds; long enough to solve, short enough to forget
MAX_CHALLENGES = 20000      # bound the memory a challenge flood can take
MAX_NONCE = 64
SESSION_COOKIE = "cpb_session"
CSRF_COOKIE = "cpb_csrf"    # readable by the page, unlike the session cookie
CSRF_HEADER = "X-CSRF-Token"

# scrypt parameters: ~16 MiB and a few tens of ms per attempt, which is a wall
# to offline guessing and imperceptible on a login. maxmem has to be given
# because OpenSSL's default is below what n=2^14 asks for on some builds.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 1 << 14, 8, 1
SCRYPT_MAXMEM = 64 << 20
# How many scrypt computations run at once. Each takes 16 MiB, and every
# request is a thread of its own, so without a bound a burst of sign-ins is a
# burst of memory. The rest wait their turn.
SCRYPT_SLOTS = threading.BoundedSemaphore(4)

# The metric table is the single source of truth for the API and the UI.
#   kind  rate  -> per-clock normalisation divides by GHz
#         time  -> per-clock normalisation multiplies by GHz (ns -> cycles)
#         ratio -> already clock-independent
#         fixed -> the core clock is not what sets it; never normalise
#
#   MEM is `fixed` rather than `rate`. It is a DRAM streaming figure -- the
#   buffer is sized past LLC deliberately -- so the memory controller and the
#   DRAM clock set it, not the core's. GB/s per core-GHz would answer no
#   question: it would flatter whichever core happened to be clocked lower
#   while measuring the same memory. MEMlat stays `time` even though it is also
#   a DRAM figure, because latency in core cycles is a real quantity -- it is
#   what the stall costs *this* core, and a faster core does lose more cycles
#   to the same nanoseconds.
#   better  which direction is an improvement; "none" means do not rank it
#   headline  shown by default. Fourteen columns of five significant figures is
#         a data dump, not a result, so the tables carry these three -- the
#         geomean, the compute rate it is mostly made of, and memory bandwidth,
#         which the geomean cannot stand in for -- plus whichever metric the
#         board is being sorted on. The rest are one select away, and every
#         metric is still ranked, filtered and compared on.
#   aside   set on a metric that is not part of the score, saying why; the page
#         sets those columns apart and puts this in their tooltips. Listed last,
#         after everything the score is made of, as the benchmark prints them.
METRICS = [
    {"key": "score",       "label": "score",    "unit": "geomean", "kind": "rate",  "better": "high", "headline": True},
    {"key": "int_thr",     "label": "INT-thr",  "unit": "Mop/s",  "kind": "rate",  "better": "high", "headline": True},
    {"key": "int_lat",     "label": "INT-lat",  "unit": "Mop/s",  "kind": "rate",  "better": "high"},
    {"key": "ilp",         "label": "ILP",      "unit": "x",       "kind": "ratio", "better": "high"},
    {"key": "mul_thr",     "label": "MUL-thr",  "unit": "Mmul/s",  "kind": "rate",  "better": "high"},
    {"key": "fp_thr",      "label": "FP-thr",   "unit": "Mflop/s", "kind": "rate",  "better": "high"},
    {"key": "fp_lat",      "label": "FP-lat",   "unit": "Mflop/s", "kind": "rate",  "better": "high"},
    {"key": "filp",        "label": "fILP",     "unit": "x",       "kind": "ratio", "better": "none"},
    {"key": "mem_gbps",    "label": "MEM",      "unit": "GB/s",    "kind": "fixed", "better": "high", "headline": True},
    {"key": "mem_lat_ns",  "label": "MEMlat",   "unit": "ns",      "kind": "time",  "better": "low"},
    {"key": "mem_lat8_ns", "label": "MEMlat/8", "unit": "ns",      "kind": "time",  "better": "low"},
    {"key": "mlp",         "label": "MLP",      "unit": "x",       "kind": "ratio", "better": "high"},
    {"key": "disp_thr",    "label": "DISP-thr", "unit": "Mcall/s", "kind": "rate",  "better": "high"},
    {"key": "disp_cap",    "label": "DISPcap",  "unit": "calls",   "kind": "ratio", "better": "high"},
    # The FP pair again on floats: what a core with a single-precision FPU and
    # no double-precision one -- most microcontrollers -- does in hardware.
    {"key": "fp32_thr",    "label": "FP32-thr", "unit": "Mflop/s", "kind": "rate",  "better": "high",
     "aside": "single precision, not in the score"},
    {"key": "fp32_lat",    "label": "FP32-lat", "unit": "Mflop/s", "kind": "rate",  "better": "high",
     "aside": "single precision, not in the score"},
]
METRIC_KEYS = [m["key"] for m in METRICS]
# key -> kind, for the one place that has to redo the front end's per-GHz
# arithmetic in SQL. Spelled apart from METRIC_KINDS below, which is the
# vocabulary rather than a lookup.
KIND_OF = {m["key"]: m["kind"] for m in METRICS}
# The front end switches on these strings and falls through to "leave the value
# alone" for anything it does not know, so a typo here is a metric that quietly
# stops being normalised. The contract test pins the vocabulary.
METRIC_KINDS = {"rate", "time", "ratio", "fixed"}

# JSON key -> (column, type) for one core/thread record.
CORE_FIELDS = [
    ("cpu",              "cpu",             "int"),
    ("mhz",              "mhz",             "num"),
    ("mhz_src",          "mhz_src",         "str"),
    ("int_lat_mops",     "int_lat",         "num"),
    ("int_thr_mops",     "int_thr",         "num"),
    ("ilp",              "ilp",             "num"),
    ("mul_thr_mmul_s",   "mul_thr",         "num"),
    ("fp_lat_mflops",    "fp_lat",          "num"),
    ("fp_thr_mflops",    "fp_thr",          "num"),
    ("filp",             "filp",            "num"),
    ("mem_gbps",         "mem_gbps",        "num"),
    ("mem_lat_ns",       "mem_lat_ns",      "num"),
    ("mem_lat8_ns",      "mem_lat8_ns",     "num"),
    ("mlp",              "mlp",             "num"),
    ("disp_thr_mcall_s", "disp_thr",        "num"),
    ("disp_cap_calls",   "disp_cap",        "num"),
    ("disp_prediction",  "disp_prediction", "str"),
    ("disp_gain",        "disp_gain",       "num"),
    ("disp_span",        "disp_span",       "num"),
    ("score",            "score",           "num"),
    # Single precision, outside the score: in METRICS, marked aside.
    ("fp32_lat_mflops",  "fp32_lat",        "num"),
    ("fp32_thr_mflops",  "fp32_thr",        "num"),
]
CORE_COLUMNS = [col for _, col, _ in CORE_FIELDS]

# The submitter's name, resolved wherever a run is read. A correlated subquery
# rather than a join, so a run whose account has since been closed still reads
# back -- with no submitter, which is what it now is.
SUBMITTER = "(SELECT u.name FROM users u WHERE u.id = r.user_id) AS user"

# The release a run's binary *says* it came from, or NULL. Resolved at read
# time rather than stamped on at upload: a hub that loads a release manifest
# afterwards then marks the runs already sitting in it, instead of leaving them
# unmarked for having arrived first.
#
# Deliberately not called "verified". It rests on a checksum the submitter
# wrote into their own document, so it says which build a result claims --
# which is what makes two results comparable, and is not a proof of anything.
# Nothing here can be: see the note on `verified` in the API.
#
# The newest release that published those bytes, where several did. Most
# releases rebuild a target without changing it -- same source, same pinned
# toolchain, same flags -- so one digest is typically published by every release
# from the one that introduced it onwards, and naming the oldest of them would
# label a current binary with a version it has long outlived. Ordered by
# release_rank, because the tags themselves do not sort; see release_rank().
RELEASE_BUILD = ("(SELECT vb.release FROM verified_builds vb "
                 "WHERE vb.sha256 = r.binary_sha256 "
                 "ORDER BY vb.release_rank DESC, vb.added_at DESC "
                 "LIMIT 1) AS release_build")

# RUN_COLUMNS as a select list over `runs r`, the layout read as reported or
# else guessed, which is what every reader of a run means by it.
def run_columns_sql() -> str:
    return ", ".join(f"{LAYOUT_SQL} AS cpu_layout" if c == "cpu_layout" else f"r.{c}"
                     for c in RUN_COLUMNS)


RUN_COLUMNS = [
    "id", "created_at", "label", "notes", "mode", "binary_sha256",
    "compiler",
    "compiler_version", "cc", "build_flags", "target",
    "vectorize", "fma", "sysname", "os_release", "machine", "cpus",
    "cpu_models", "cpu_key", "cpu_layout", "cpus_offline", "threads", "seconds", "reps", "warmup", "mem_bytes", "pin",
    "clock", "seed", "dram_name", "dram_mhz_min", "dram_mhz_max", "checksum",
    "upload_id", "hidden_at",
]


class HttpError(Exception):
    """A refusal with the status it should be reported as."""

    def __init__(self, status: int, message: str, extra: dict | None = None):
        super().__init__(message)
        self.status = status
        # Fields the reply carries beside "error", for a client that can act
        # on more than the words.
        self.extra = extra or {}


class Invalid(HttpError):
    """An upload that will not be stored, with a message the client can show."""

    def __init__(self, message: str):
        super().__init__(HTTPStatus.BAD_REQUEST, message)


class Denied(HttpError):
    """Refused for who is asking rather than for what was sent."""

    def __init__(self, message: str, status: int = HTTPStatus.FORBIDDEN):
        super().__init__(status, message)


class Duplicate(HttpError):
    """An upload of a document the hub already holds.

    The reply names the run it already is and nothing more: the raw document
    of every run is public, so anyone can send one back, and the delete token
    must never be what they get for it.
    """

    def __init__(self, run_id: int):
        super().__init__(
            HTTPStatus.CONFLICT,
            f"this result is already on the hub, as run {run_id}. If it is "
            f"yours, its delete token adds it to your account (My uploads)",
            {"id": run_id, "url": f"/#run={run_id}"})
        self.run_id = run_id


def document_digest(raw: str) -> str | None:
    """SHA-256 of an uploaded document, for telling a re-upload from a run.

    Over the parsed document rather than its bytes, so the page re-serialising
    what was pasted into it, or a file saved with other whitespace, is still
    the same document. The label and notes are left out: they say what the
    uploader calls the run, not what was measured, and the same run sent
    again under a new label is still the same run. None for a body that does
    not parse, which an upload has already been refused for.

    Every number is read as a double, which is all a browser keeps of one:
    the page's JSON.parse and JSON.stringify write the benchmark's 20000.0 as
    20000, and a seed past 2^53 as the nearest double, so the copy the page
    sends hashes the same as the file it was given. (+ 0.0 folds -0.0 into
    0.0, as JSON.stringify does.)
    """
    def num(s: str) -> float:
        return float(s) + 0.0

    try:
        doc = json.loads(raw, parse_float=num, parse_int=num)
    except ValueError:
        return None
    if isinstance(doc, dict):
        doc = {k: v for k, v in doc.items() if k not in ("label", "notes")}
    canon = json.dumps(doc, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
#
# Everything here runs on attacker-controlled input: the payload is whatever was
# POSTed, not necessarily something cpcpub produced.


def _reject_constant(name: str):
    raise Invalid(f"{name} is not a valid value")


def parse_json(raw: bytes) -> object:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise Invalid("body must be UTF-8")
    try:
        # parse_constant rejects the NaN/Infinity literals Python would
        # otherwise accept and which no downstream consumer handles sanely.
        return json.loads(text, parse_constant=_reject_constant)
    except json.JSONDecodeError as e:
        raise Invalid(f"invalid JSON: {e.msg} at line {e.lineno} column {e.colno}")


def _skip_ws(text: str, i: int) -> int:
    """The first index at or after `i` that is not JSON whitespace."""
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    return i


def split_documents(text: str) -> list[tuple[int, str, object]]:
    """The documents in a batch file, as (part, raw, parsed), in file order.

    A batch file is what `cat` makes of result files: JSON values one after
    another. Each is a document, or an array of them as `cpcpub --variants
    --json` prints. `part` says which ran together: every array is a part of
    its own, and so is each stretch of documents between arrays. `raw` is the
    document's own text, cut out of the file, so a batch stores each one as
    exactly as a single upload does.
    """
    dec = json.JSONDecoder(parse_constant=_reject_constant)

    def one(i: int) -> tuple[object, int]:
        try:
            return dec.raw_decode(text, i)
        except json.JSONDecodeError as e:
            raise Invalid(f"invalid JSON: {e.msg} at line {e.lineno} column {e.colno}")

    def where(i: int) -> str:
        return (f"line {text.count(chr(10), 0, i) + 1} "
                f"column {i - text.rfind(chr(10), 0, i)}")

    out: list[tuple[int, str, object]] = []
    part, loose = 0, False
    pos = _skip_ws(text, 1 if text.startswith("\ufeff") else 0)
    while pos < len(text):
        if text[pos] == "[":
            part, loose = part + 1, False
            pos = _skip_ws(text, pos + 1)
            if text.startswith("]", pos):
                pos += 1
            else:
                while True:
                    obj, end = one(pos)
                    out.append((part, text[pos:end], obj))
                    # Inside the array as well as after it: one array of
                    # millions of tiny values would otherwise all be held
                    # before the count was looked at.
                    if len(out) > MAX_BATCH:
                        raise Invalid(f"a batch holds at most {MAX_BATCH} results")
                    pos = _skip_ws(text, end)
                    if text.startswith(",", pos):
                        pos = _skip_ws(text, pos + 1)
                    elif text.startswith("]", pos):
                        pos += 1
                        break
                    else:
                        raise Invalid(f"invalid JSON: expected ',' or ']' at {where(pos)}")
        else:
            if not loose:
                part, loose = part + 1, True
            obj, end = one(pos)
            out.append((part, text[pos:end], obj))
            pos = end
        if len(out) > MAX_BATCH:
            raise Invalid(f"a batch holds at most {MAX_BATCH} results")
        pos = _skip_ws(text, pos)
    return out


# What the variants of one machine's run share, as validate() reads them:
# everything about the machine and the measurement, the build aside.
MACHINE_SAME = ("cpu_models", "machine", "sysname", "os_release",
                "binary_sha256", "mode", "threads", "seconds", "reps",
                "warmup", "mem_bytes", "pin", "seed")


def variant_name(run: dict) -> str:
    """A run's build as cpcpub names it: scalar-nofma ... vector-fma."""
    return (f"{'vector' if run.get('vectorize') else 'scalar'}-"
            f"{'fma' if run.get('fma') else 'nofma'}")


def plan_batch(text: str) -> list[dict]:
    """Validate every document in a batch and group them into uploads.

    An upload is one machine's run: up to one result per build variant, next
    to each other in the file, from one part of it (see split_documents), with
    the same machine, binary and settings. That is what a --variants run
    prints, and what separate --variant runs of one machine catted in a row
    are. A build seen twice starts a new upload: the same machine run again.

    Any document that is not a valid result refuses the whole batch, naming
    it -- a batch is stored all or nothing. Returns the uploads, each
    {"runs": [{index, raw, payload, run, cores}]}, in file order.
    """
    docs = split_documents(text)
    if not docs:
        raise Invalid("the batch holds no results")
    groups: list[dict] = []
    for n, (part, raw, payload) in enumerate(docs, 1):
        try:
            run, cores = validate(payload)
        except Invalid as e:
            raise Invalid(f"result {n} of {len(docs)}: {e}")
        key = tuple(run.get(c) for c in MACHINE_SAME)
        build = (run["vectorize"], run["fma"])
        g = groups[-1] if groups else None
        if (g is None or g["part"] != part or g["key"] != key
                or build in g["builds"] or len(g["runs"]) >= 4):
            g = {"part": part, "key": key, "builds": set(), "runs": []}
            groups.append(g)
        g["builds"].add(build)
        g["runs"].append({"index": n, "raw": raw, "payload": payload,
                          "run": run, "cores": cores})
    return groups


def _text(v, maxlen=MAX_TEXT, field="value"):
    if v is None:
        return None
    if not isinstance(v, str):
        raise Invalid(f"{field} must be a string")
    s = "".join(c for c in v if c == "\n" or c >= " ")
    s = s.strip()[:maxlen].strip()
    return s or None


def _num(v, field, lo=0.0, hi=MAX_VALUE):
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise Invalid(f"{field} must be a number or null")
    x = float(v)
    if not math.isfinite(x):
        raise Invalid(f"{field} must be finite")
    if not (lo <= x <= hi):
        raise Invalid(f"{field} is out of range")
    return x


def _int(v, field, lo=0, hi=1 << 40):
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        else:
            raise Invalid(f"{field} must be an integer or null")
    if not (lo <= v <= hi):
        raise Invalid(f"{field} is out of range")
    return v


def _bool(v, field):
    if v is None:
        return None
    if not isinstance(v, bool):
        raise Invalid(f"{field} must be true or false")
    return int(v)


def _hex64(v, field):
    """A SHA-256 as the benchmark writes it, or None.

    Strict about the shape because it is used as an identity: a value that is
    not a digest can only ever match another value that is not a digest, and
    doing that under the word "verified" is worse than not verifying at all.
    """
    if v is None:
        return None
    if not isinstance(v, str):
        raise Invalid(f"{field} must be a string")
    s = v.strip().lower()
    if not SHA256_RE.match(s):
        raise Invalid(f"{field} must be 64 hexadecimal characters")
    return s


def _obj(payload, key):
    v = payload.get(key)
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise Invalid(f"{key} must be an object")
    return v


# ---------------------------------------------------------------------------
# Internal consistency
# ---------------------------------------------------------------------------
#
# Four numbers in every record are arithmetic on other numbers in the same
# record: `score` is a geometric mean of six of them, and `ilp`, `filp` and
# `mlp` are each a ratio of two. So a document that was opened in an editor and
# improved disagrees with itself, and recomputing them is what notices.
#
# Worth being exact about what this is. It catches a number changed by hand,
# which is how a leaderboard actually gets cheated, and it raises the price of
# a fake result from "edit one field" to "rescale six inputs and recompute the
# derived ones". It is not evidence of anything: someone who pays that price
# passes, and no check running on a machine its owner controls can do better.
# Nothing is shown for passing. A document that fails is refused at upload, and
# that is the whole of it -- there is no badge here and there must not be one.
#
# The formulas are core_score(), machine_score() and summarize() in
# bench/src/measure.c; they are part of the cpu-bench/1 contract, and the real
# sample in testdata/full-run.json is checked against them by the hub's tests,
# so a change to the benchmark's formulas cannot quietly start refusing every
# honest upload.

# The benchmark prints its numbers at six significant figures (`%.6g`), so a
# record nobody has touched still differs from a recomputation somewhere in the
# seventh. This is three orders of magnitude looser than that: an edit small
# enough to hide underneath it is an edit not worth making.
CONSISTENCY_TOLERANCE = 1e-3


def _f(row: dict, key: str) -> float:
    """One measurement as a number, with missing read as zero.

    The benchmark writes null for any value that is not positive, so absent and
    zero are a single state on the wire and are treated as one here.
    """
    v = row.get(key)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def _geomean(values: list[float]) -> float:
    """As geomean() in measure.c: terms that are not positive drop out, rather
    than taking the whole mean to zero."""
    logs = [math.log(v) for v in values if v > 0.0]
    return math.exp(math.fsum(logs) / len(logs)) if logs else 0.0


def score_terms(row: dict, mlp_scale: float = 1.0) -> list[float]:
    """The six components a record's score is the geometric mean of.

    `mlp_scale` is the thread count for a whole-machine row and 1 for every
    other one: n threads chase n sets of pointers at once, and that is the only
    difference between machine_score() and core_score().
    """
    # disp_span is written as null in two different situations -- there was no
    # dispatch measurement at all, or the span itself came out at zero -- and
    # the score treats them differently: the first drops the term, the second
    # contributes (0 + 1) * 2000. disp_prediction is what tells them apart, so
    # an honest record with a zero span is not refused for it.
    span = 0.0 if row.get("disp_prediction") == "unknown" \
        else (_f(row, "disp_span") + 1.0) * 2000.0
    lat8 = _f(row, "mem_lat8_ns")
    return [
        _f(row, "int_thr"),
        _f(row, "fp_thr"),
        _f(row, "mul_thr") * 4.0,
        _f(row, "disp_thr") * 20.0,
        span,
        mlp_scale * (1000.0 / lat8) * 100.0 if lat8 > 0.0 else 0.0,
    ]


def _ratio(row: dict, top: str, bottom: str) -> float:
    below = _f(row, bottom)
    return _f(row, top) / below if below > 0.0 else 0.0


def _agrees(row: dict, key: str, want: float, where: str) -> None:
    got = _f(row, key)
    if got <= 0.0 and want <= 0.0:
        return
    if got > 0.0 and want > 0.0 and abs(got - want) <= CONSISTENCY_TOLERANCE * want:
        return
    raise Invalid(
        f"{where}: {key} is {row.get(key)!r}, but the other numbers in the "
        f"same record make it {want:.6g}. This document does not agree with "
        f"itself, so it is not the one the benchmark wrote -- upload the file "
        f"exactly as produced.")


def check_consistency(rows: list[dict], threads: int | None) -> None:
    """Refuse a document whose derived numbers disagree with their inputs."""
    for row in rows:
        scope = row.get("scope")
        where = f"{scope} record"
        if row.get("cpu") is not None:
            where += f" cpu{row['cpu']}"
        _agrees(row, "ilp", _ratio(row, "int_thr", "int_lat"), where)
        _agrees(row, "filp", _ratio(row, "fp_thr", "fp_lat"), where)
        _agrees(row, "mlp", _ratio(row, "mem_lat_ns", "mem_lat8_ns"), where)
        scale = 1.0
        if scope == "total":
            # The whole-machine score scales its memory term by the number of
            # threads, so it can only be recomputed where the document says how
            # many there were. Missing, the row keeps its ratio checks and
            # skips this one rather than being refused for a field it is
            # allowed to omit.
            if not threads:
                continue
            scale = float(threads)
        _agrees(row, "score", _geomean(score_terms(row, scale)), where)


# INT-lat is a floor under the clock: dependent 1-cycle adds retire at most one
# per cycle on any core. Releases before 0.5.1 reported Windows' `~MHz` as a
# rated clock, and that is the base clock, which turbo leaves behind -- an N100
# says 806 MHz and runs its INT-lat at 3370 -- so every per-GHz number from
# such a run came out four times too good. The benchmark now drops a rated
# clock below this floor for its INT-lat estimate (RATED_SLACK in measure.c,
# the same 10%); the hub does the same for documents that still carry one.
#
# A rated clock is a ceiling, so on a pinned run every INT-lat measured on its
# CPU is a floor under it, the one-thread pass's included -- which matters
# where the thread's own INT-lat is low because it shared the CPU. And on a
# shared CPU INT-lat measured a share of the clock, so it estimates nothing:
# the benchmark reports such a thread's clock as unknown from 0.5.1, and the
# hub does the same for documents from before.
RATED_SLACK = 1.1
# `mhz_src` values from most to least trusted, as measure.h orders them. The
# total keeps the weakest of its threads', as aggregate() does.
MHZ_TRUST = ("measured", "given", "rated", "estimated")


def fix_clocks(rows: list[dict], threads: int | None, cpus: int | None,
               pin: object) -> bool:
    """Bring one run's clocks to what the 0.5.1 benchmark would have written.

    `rows` are the run's records under the hub's column names, changed in
    place; `threads`, `cpus` and `pin` are its config.threads, system.cpus and
    config.pin. A thread shared its CPU where two threads name it, or, with
    nothing pinned, where there were more threads than CPUs. Then:

    - a rated clock below the highest INT-lat on its CPU (on a pinned run; its
      own INT-lat otherwise), less 10%, is dropped;
    - an estimated clock on a shared CPU is dropped;
    - a dropped clock becomes the row's INT-lat as an estimate, or unknown on
      a shared CPU.

    A thread whose clock changes takes the total's with it: the benchmark
    writes the total's `mhz` as the mean of its threads' known clocks and its
    `mhz_src` as the weakest of theirs. A `total` is never tested itself, since
    its INT-lat is the sum over every thread. Returns whether anything changed.
    """
    pinned = bool(pin)
    named = [r.get("cpu") for r in rows if r.get("scope") == "thread"]
    crowded = not pinned and bool(threads and cpus and threads > cpus)

    def shared(row: dict) -> bool:
        if row.get("scope") != "thread":
            return False
        cpu = row.get("cpu")
        return crowded or (cpu is not None and named.count(cpu) > 1)

    floor: dict[object, float] = {}
    if pinned:
        for r in rows:
            if r.get("scope") != "total" and r.get("cpu") is not None:
                floor[r["cpu"]] = max(floor.get(r["cpu"], 0.0), _f(r, "int_lat"))

    changed = threads_changed = False
    for row in rows:
        if row.get("scope") == "total":
            continue
        src, mhz, lat = row.get("mhz_src"), _f(row, "mhz"), _f(row, "int_lat")
        if src == "rated":
            drop = mhz > 0.0 and max(lat, floor.get(row.get("cpu"), 0.0)) > mhz * RATED_SLACK
        else:
            drop = src == "estimated" and shared(row)
        if not drop:
            continue
        if shared(row) or lat <= 0.0:
            row["mhz"], row["mhz_src"] = None, "unknown"
        else:
            row["mhz"], row["mhz_src"] = lat, "estimated"
        changed = True
        threads_changed = threads_changed or row.get("scope") == "thread"
    if threads_changed:
        ts = [r for r in rows if r.get("scope") == "thread"]
        clocks = [_f(r, "mhz") for r in ts if _f(r, "mhz") > 0.0]
        trust = [MHZ_TRUST.index(r["mhz_src"]) for r in ts
                 if _f(r, "mhz") > 0.0 and r.get("mhz_src") in MHZ_TRUST]
        for total in (r for r in rows if r.get("scope") == "total"):
            total["mhz"] = sum(clocks) / len(clocks) if clocks else None
            total["mhz_src"] = MHZ_TRUST[max(trust)] if trust else "unknown"
    return changed


# What a build that could not name its own target was, read off the machine it
# ran on. cpcpub names 32-bit Arm builds now (armv7, armv6, arm); builds from
# before that said "unknown", and a target of "unknown" is a board nobody can
# filter for or compare against. Only machines that leave little doubt are
# here: a kernel that is armv6 runs nothing newer, and an armv7 one is armv7
# userland on every distribution but one (below). A 64-bit machine is left as
# it came, since an unnamed build there may well be a 32-bit one.
TARGET_OF_MACHINE = {"armv6l": "armv6", "armv7l": "armv7", "armv8l": "arm"}
# Raspberry Pi OS ships one 32-bit userland for every Pi, built for the ARMv6
# of the first, so what its own compiler makes is ARMv6 code on an ARMv7 board.
# Its kernels say so in their release: "+rpt-rpi-v7", "-rpi-v7l", "-v7+".
RPI_OS_KERNEL_RE = re.compile(r"rpt-rpi|-rpi-v[78]|-v7l?\+$")


def known_target(target: str | None, machine: str | None,
                 release: str | None) -> str | None:
    """A run's target, as it reported it or else as its machine implies."""
    if target not in (None, "unknown"):
        return target
    guess = TARGET_OF_MACHINE.get(machine or "")
    if guess == "armv7" and RPI_OS_KERNEL_RE.search(release or ""):
        guess = "armv6"
    return guess or target


def validate(payload: object) -> tuple[dict, list[dict]]:
    """Turn an uploaded document into (run row, core rows) or raise Invalid."""
    if not isinstance(payload, dict):
        raise Invalid("body must be a JSON object")
    if payload.get("schema") != SCHEMA_ID:
        raise Invalid(f"unsupported schema {payload.get('schema')!r}; "
                      f"expected {SCHEMA_ID}")

    mode = payload.get("mode")
    if mode not in MODES:
        raise Invalid("mode must be 'full', 'per-core' or 'threads'; a "
                      "--disp-sweep dump carries no summary metrics to compare")

    build = _obj(payload, "build")
    system = _obj(payload, "system")
    config = _obj(payload, "config")
    dram = _obj(payload, "dram")

    run = {
        "mode": mode,
        "compiler": _text(build.get("compiler"), field="build.compiler"),
        "compiler_version": _text(build.get("compiler_version"), 200,
                                  "build.compiler_version"),
        "cc": _text(build.get("cc"), 64, "build.cc"),
        # The exact flags matter for comparability and are long; keep them whole.
        "build_flags": _text(build.get("flags"), 1000, "build.flags"),
        "target": known_target(_text(build.get("target"), 32, "build.target"),
                               _text(system.get("machine"), 64, "system.machine"),
                               _text(system.get("release"), 64, "system.release")),
        "binary_sha256": _hex64(build.get("binary_sha256"), "build.binary_sha256"),
        "vectorize": _bool(build.get("vectorize"), "build.vectorize"),
        "fma": _bool(build.get("fma"), "build.fma"),
        "sysname": _text(system.get("sysname"), 64, "system.sysname"),
        "os_release": _text(system.get("release"), 64, "system.release"),
        "machine": _text(system.get("machine"), 64, "system.machine"),
        "cpus": _int(system.get("cpus"), "system.cpus", 0, 4096),
        "cpu_models": tidy_line(_text(system.get("cpu_models"), 300, "system.cpu_models")),
        "cpu_layout": _text(system.get("cpu_layout"), 300, "system.cpu_layout"),
        "cpus_offline": _int(system.get("cpus_offline"), "system.cpus_offline", 0, 4096),
        "threads": _int(config.get("threads"), "config.threads", 0, 4096),
        "seconds": _num(config.get("seconds_per_phase"), "config.seconds_per_phase", 0, 1e6),
        "reps": _int(config.get("reps"), "config.reps", 0, 1 << 20),
        "warmup": _num(config.get("warmup_seconds"), "config.warmup_seconds", 0, 1e6),
        "mem_bytes": _int(config.get("mem_bytes_per_thread"), "config.mem_bytes_per_thread"),
        "pin": _bool(config.get("pin"), "config.pin"),
        "clock": _text(config.get("clock"), 16, "config.clock"),
        "seed": _int(config.get("seed"), "config.seed", 0, (1 << 64) - 1),
        "dram_name": _text(dram.get("name"), 64, "dram.name"),
        "dram_mhz_min": _num(dram.get("mhz_min"), "dram.mhz_min", 0, 1e7),
        "dram_mhz_max": _num(dram.get("mhz_max"), "dram.mhz_max", 0, 1e7),
        "checksum": _text(payload.get("checksum"), 32, "checksum"),
    }

    records: list[tuple[str, object]] = []
    cores = payload.get("cores")
    threads = payload.get("threads")
    if mode in ("per-core", "full") and cores is not None:
        if not isinstance(cores, list) or not cores:
            raise Invalid("'cores' must be a non-empty array")
        records += [("cpu", c) for c in cores]
    if mode in ("threads", "full") and threads is not None:
        if not isinstance(threads, list) or not threads:
            raise Invalid("'threads' must be a non-empty array")
        records += [("thread", t) for t in threads]
        if payload.get("total") is not None:
            records.append(("total", payload["total"]))
    # `--seed` takes any unsigned 64-bit number, and a column holds a signed
    # one. A seed past that is kept only in the raw document; nothing ranks or
    # shows it, and refusing the upload for it would refuse an honest run.
    if run["seed"] is not None and run["seed"] > SQLITE_MAX_INT:
        run["seed"] = None

    if not records:
        raise Invalid(f"a {mode} run must carry the matching result array "
                      "('cores' and/or 'threads')")

    if len(records) > MAX_CORES:
        raise Invalid(f"too many records (limit {MAX_CORES})")

    rows = []
    for default_scope, rec in records:
        if not isinstance(rec, dict):
            raise Invalid("each result record must be an object")
        scope = _text(rec.get("scope"), 16, "scope") or default_scope
        if scope not in SCOPES:
            raise Invalid(f"unknown scope {scope!r}")
        # Mixed on purpose: a record is a scope name and fourteen numbers, any
        # of which the benchmark may leave out.
        row: dict[str, object] = {"scope": scope}
        for jkey, col, kind in CORE_FIELDS:
            v = rec.get(jkey)
            if kind == "num":
                row[col] = _num(v, jkey)
            elif kind == "int":
                row[col] = _int(v, jkey, -1, 1 << 20)
            else:
                row[col] = _text(v, 32, jkey)
        rows.append(row)

    if not any(r.get("score") or r.get("int_thr") for r in rows):
        raise Invalid("no usable measurements in this upload")
    # Last of all, so an empty or malformed upload says so in its own terms
    # first. This one answers a different question: not "is this a result" but
    # "is this the result the benchmark wrote".
    check_consistency(rows, run.get("threads"))
    # After the check, which is over the document as written; the clock is in
    # none of the numbers it recomputes. The raw document keeps what was sent.
    fix_clocks(rows, run["threads"], run["cpus"], run["pin"])
    return run, rows


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------
#
# Passwords are stored as scrypt digests, salted per account, and compared in
# constant time. Session tokens are stored as SHA-256 -- they are random 256-bit
# strings rather than guessable secrets, so a plain hash with no salt is the
# right shape: it stops a leaked database being a list of live sessions without
# pretending to slow down an attack nobody can mount.


def check_name(v) -> str:
    if not isinstance(v, str):
        raise Invalid("name must be a string")
    # Deliberately not _text(): that truncates to fit, and a name quietly cut
    # to thirty-two characters is not the name anyone asked to register.
    name = "".join(c for c in v if c >= " ").strip()
    if not ACCOUNT_NAME_RE.match(name):
        raise Invalid("a name is 3 to 32 characters of letters, digits, "
                      "'.', '_' or '-', starting with a letter or digit")
    return name


def check_password(v) -> str:
    if not isinstance(v, str):
        raise Invalid("password must be a string")
    if len(v) < MIN_PASSWORD:
        raise Invalid(f"password must be at least {MIN_PASSWORD} characters")
    if len(v) > MAX_PASSWORD:
        raise Invalid(f"password must be at most {MAX_PASSWORD} characters")
    return v


def same_secret(given: str, held: str) -> bool:
    """Whether two secrets match, in constant time.

    As bytes: compare_digest refuses a str holding anything but ASCII with a
    TypeError, and what a client sends can hold anything.
    """
    return secrets.compare_digest(given.encode("utf-8", "surrogatepass"),
                                  held.encode("utf-8", "surrogatepass"))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    with SCRYPT_SLOTS:
        # surrogatepass: JSON can spell a lone surrogate, and a password
        # holding one must hash, not fail.
        dk = hashlib.scrypt(password.encode("utf-8", "surrogatepass"), salt=salt, n=SCRYPT_N,
                            r=SCRYPT_R, p=SCRYPT_P, maxmem=SCRYPT_MAXMEM, dklen=32)
    return "$".join(["scrypt", str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P),
                     salt.hex(), dk.hex()])


def password_matches(password: str, stored: str) -> bool:
    """Whether `password` produces `stored`. Never raises on a malformed row."""
    try:
        algo, n, r, p, salt_hex, want_hex = stored.split("$")
        if algo != "scrypt":
            return False
        want = bytes.fromhex(want_hex)
        with SCRYPT_SLOTS:
            got = hashlib.scrypt(password.encode("utf-8", "surrogatepass"),
                                 salt=bytes.fromhex(salt_hex),
                                 n=int(n), r=int(r), p=int(p),
                                 maxmem=SCRYPT_MAXMEM, dklen=len(want))
    except (ValueError, TypeError):
        return False
    return secrets.compare_digest(got, want)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def pow_solved(challenge: str, nonce: str, bits: int) -> bool:
    """Whether sha256("challenge:nonce") starts with `bits` zero bits.

    The client searches for a nonce; the server does one hash to check it.
    That asymmetry is the whole mechanism, and it needs no state beyond the
    challenge having been issued and not yet spent.
    """
    digest = hashlib.sha256(f"{challenge}:{nonce}".encode()).digest()
    return int.from_bytes(digest, "big") < (1 << (256 - bits))


class Challenges:
    """Registration challenges: issued by us, spent once, forgotten quickly.

    Server-issued rather than derived from the request, so a challenge cannot
    be prepared offline in bulk; single-use, so one solved puzzle is one
    account; expiring, so the set stays small.
    """

    def __init__(self, ttl: float = CHALLENGE_TTL, cap: int = MAX_CHALLENGES):
        self.ttl = ttl
        self.cap = cap
        self.live: dict[str, float] = {}
        self.lock = threading.Lock()

    def issue(self) -> str:
        token = secrets.token_urlsafe(16)
        now = time.monotonic()
        with self.lock:
            if len(self.live) >= self.cap:
                for k in [k for k, exp in self.live.items() if exp <= now]:
                    del self.live[k]
                # Still full: the oldest go, which is the right thing to lose.
                while len(self.live) >= self.cap:
                    del self.live[min(self.live, key=self.live.__getitem__)]
            self.live[token] = now + self.ttl
        return token

    def spend(self, token: str) -> bool:
        """Take `token` if it is live. A second attempt with it fails."""
        with self.lock:
            expires = self.live.pop(token, None)
        return expires is not None and expires > time.monotonic()


def _url(v, field):
    """An http(s) URL, or None.

    The scheme is checked because this value ends up as an href in the page:
    `javascript:` in a link the hub itself renders is a script the CSP was
    supposed to have made impossible.
    """
    s = _text(v, 300, field)
    if s is None:
        return None
    if not s.startswith(("http://", "https://")):
        raise Invalid(f"{field} must be an http:// or https:// URL")
    return s


def release_rank(tag: str) -> str:
    """A sortable key for a release tag, so newest means newest.

    Tags are text, and text does not sort the way versions do: every string
    comparison there is puts "v0.10.0" before "v0.9.0", and it only takes one
    two-digit component for a board to start labelling binaries with a release
    they were superseded in. So each run of digits is padded to a fixed width
    and compared as a number, and what is left between them is compared as
    written.

    Two smaller rules, both of them what a reader of the tag would expect:

      * a leading "v" is dropped, so a repository that tagged "0.4.0" after
        "v0.3.0" is not read as having gone backwards;
      * a pre-release suffix ranks *below* the release it leads to -- v1.0.0-rc1
        precedes v1.0.0 -- which the padding alone would get backwards, a longer
        string being the greater one.

    The key is stored rather than computed per query: SQLite has no version
    collation to ORDER BY, and a Python-side sort would mean reading every row
    of the table to answer one row of the board.
    """
    core, _, pre = tag.strip().lower().partition("-")
    core = core.removeprefix("v")

    def pad(s: str) -> str:
        out = []
        for part in re.findall(r"\d+|\D+", s):
            if part.isdigit():
                n = int(part)
                # A component too large to be a version number is not one; it
                # is clamped rather than allowed to widen the field and so
                # scramble the ordering of every tag around it.
                out.append(f"{n:012d}" if n < 10 ** 12 else "9" * 12)
            else:
                out.append(part)
        return "".join(out)

    # "!" sorts below "~", which is the whole trick: v1.0.0 gets the "~" and so
    # outranks every v1.0.0-<anything>.
    return pad(core) + ("!" + pad(pre) if pre else "~")


def parse_builds_manifest(doc: object) -> tuple[str, str | None, list[dict]]:
    """Validate a release build manifest into (release tag, url, builds).

    The document the release workflow attaches next to the binaries:

        {"schema": "cpu-bench-builds/1", "release": "v0.1.0",
         "url": "https://github.com/…/releases/tag/v0.1.0",
         "builds": [{"filename": …, "sha256": …, "target": …, "march": …}, …]}

    Checked as strictly as an upload is. It arrives from a public URL over a
    network the hub does not control, and everything in it becomes the meaning
    of the word "verified" on the board.
    """
    if not isinstance(doc, dict):
        raise Invalid("a build manifest must be a JSON object")
    if doc.get("schema") != BUILDS_SCHEMA_ID:
        raise Invalid(f"unsupported manifest schema {doc.get('schema')!r}; "
                      f"expected {BUILDS_SCHEMA_ID}")
    release = _text(doc.get("release"), 64, "release")
    if not release:
        raise Invalid("a build manifest must name the release it describes")
    builds = doc.get("builds")
    if not isinstance(builds, list) or not builds:
        raise Invalid("'builds' must be a non-empty array")
    if len(builds) > 100:
        raise Invalid("too many builds in one manifest")

    seen: set[str] = set()
    out = []
    for b in builds:
        if not isinstance(b, dict):
            raise Invalid("each build must be an object")
        digest = _hex64(b.get("sha256"), "builds[].sha256")
        if digest is None:
            raise Invalid("each build must carry a sha256")
        # Two names for one digest is one binary published twice; harmless, but
        # it would make the count say something untrue.
        if digest in seen:
            continue
        seen.add(digest)
        out.append({
            "sha256": digest,
            "filename": _text(b.get("filename"), 128, "builds[].filename"),
            "target": _text(b.get("target"), 32, "builds[].target"),
            "march": _text(b.get("march"), 200, "builds[].march"),
        })
    return release, _url(doc.get("url"), "url"), out


def public_user(user: dict, *, runs: int | None = None,
                token: bool = False) -> dict:
    """An account as the API describes it.

    The password hash never leaves here, and the upload token only when the
    account itself is asking -- `/api/users/<name>` is a public page.
    """
    out = {"name": user["name"], "created_at": user["created_at"]}
    if runs is not None:
        out["runs"] = runs
    if token:
        out["api_token"] = user["api_token"]
        out["rename_after"] = rename_after(user)
    return out


def rename_after(user: dict) -> str | None:
    """When the account may next change its name; None if it may now."""
    last = user.get("renamed_at")
    if not last:
        return None
    due = parse_iso(last) + timedelta(days=RENAME_DAYS)
    if due <= datetime.now(timezone.utc):
        return None
    return due.isoformat(timespec="seconds").replace("+00:00", "Z")


def split_variant(label: str | None) -> tuple[str | None, bool]:
    """A label without the variant tag a --variants upload ends it with, and
    whether it had one. "box (vector-fma)" is ("box", True); "(scalar-fma)"
    alone, an upload with no label of its own, is (None, True)."""
    if not label:
        return None, False
    m = VARIANT_TAG_RE.search(label)
    if not m:
        return label, False
    return label[:m.start()] or None, True


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(s: str) -> datetime:
    """A timestamp as iso_now() writes it, back into an aware datetime."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def sort_expr(sort: str, norm: str) -> str:
    """The quantity a listing is ordered by, in SQL.

    Per-GHz is not a second set of numbers, it is a different way of reading
    the same ones -- so it has to be what the ordering reads too. Ranking by
    the absolute figure while printing the normalised one puts a column on
    screen that is visibly out of order, which is the sort of wrong that makes
    a reader distrust the rows as well as the ordering.

    The arithmetic is the front end's, exactly: rates divide by GHz, latencies
    in ns become cycles, `ratio` and `fixed` metrics do not move, and a record
    whose clock was never reported shows its raw value and therefore sorts on
    it. `mhz` itself is a column, not a metric, and is never normalised.
    """
    op = {"rate": "/", "time": "*"}.get(KIND_OF.get(sort, ""))
    if norm != "ghz" or op is None:
        return f"c.{sort}"
    return (f"CASE WHEN c.mhz IS NULL OR c.mhz = 0 THEN c.{sort} "
            f"ELSE c.{sort} {op} (c.mhz / 1000.0) END")


def normalised(row: dict, key: str, norm: str) -> float | None:
    """One value as the page shows it: sort_expr() in Python."""
    v = row.get(key)
    if v is None or norm != "ghz" or not row.get("mhz"):
        return v
    ghz = row["mhz"] / 1000.0
    kind = KIND_OF.get(key)
    return v / ghz if kind == "rate" else v * ghz if kind == "time" else v


# Kinds of core ----------------------------------------------------------------
#
# A per-core sweep lists every CPU, and on a phone or a hybrid PC that is two or
# three kinds of core over and over: eight or twelve rows where three would say
# it all. The hub groups them so a page can show one averaged row per kind.
#
# They are grouped by what they measured. A record carries no core type, and a
# PC's single brand string names none. Sorted by score, a step of more than
# CORE_TYPE_GAP between neighbours starts a new kind. Cores of one design
# differ by a few percent (a favoured core's turbo bin, a cluster clocked a
# step lower); different designs differ by a quarter or more. Two designs
# closer than the gap are grouped as one, which is what their numbers say
# anyway.
CORE_TYPE_GAP = 1.15


def named_designs(cpu_models: str | None) -> list[str]:
    """The core designs a CPU line names, when it names more than one.

    An Arm machine is named from each CPU's MIDR: distinct names joined with
    " + ", in the order of each design's first CPU, and the board in
    parentheses after, as in "Cortex-A55 + Cortex-A76 (Radxa ROCK 5B)".
    """
    if not cpu_models:
        return []
    line = re.sub(r"\s*\([^()]*\)$", "", cpu_models)
    names = [n.strip() for n in line.split(" + ") if n.strip()]
    return names if len(names) > 1 else []


def _size_names(n: int) -> list[str]:
    if n == 2:
        return ["large", "small"]
    return ["large"] + [f"medium {i}" if n > 3 else "medium"
                        for i in range(1, n - 1)] + ["small"]


def _kinds(recs: list[dict], cpu_models: str | None,
           alike: str) -> tuple[list[str], list[list[dict]]]:
    """Records grouped into kinds of core by score, fastest first, and a name
    for each: a design the CPU line names, a size, or `alike` for one kind."""
    groups: list[list[dict]] = []
    for r in sorted(recs, key=lambda r: -r["score"]):
        if groups and groups[-1][-1]["score"] / r["score"] <= CORE_TYPE_GAP:
            groups[-1].append(r)
        else:
            groups.append([r])
    names = named_designs(cpu_models)
    # Fewer designs named than steps found: two of the groups are one design at
    # two clocks, as a phone's prime core and the rest of its big cluster are.
    # The closest pair is joined until the counts agree.
    while names and len(groups) > len(names):
        i = min(range(len(groups) - 1),
                key=lambda k: groups[k][-1]["score"] / groups[k + 1][0]["score"])
        groups[i:i + 2] = [groups[i] + groups[i + 1]]
    if len(groups) == 1:
        return [alike], groups
    if names and len(names) == len(groups):
        # The line lists designs in the order of their first CPU, so the group
        # holding the lowest-numbered CPU is the first name.
        def first(g):
            return min((c for r in g if isinstance(c := r.get("cpu"), int) and c >= 0),
                       default=1 << 30)
        labels = [""] * len(groups)
        for name, k in zip(names, sorted(range(len(groups)),
                                         key=lambda k: first(groups[k]))):
            labels[k] = name
        return labels, groups
    return _size_names(len(groups)), groups


def core_types(records: list[dict], cpu_models: str | None) -> list[dict]:
    """The kinds of core in one run, fastest first.

    Each is {"scope", "name", "ids", "cpus"}. Per-core records are grouped by
    score. The multi-threaded run's threads follow the kind of the CPU each was
    pinned to. A run without a per-core sweep groups its pinned threads by
    their own scores instead, as a phone's prime pair stands out from the rest
    while all eight run at once. Threads are otherwise one group. A scope with
    fewer than two records is not grouped.
    """
    def cpu_of(r):
        c = r.get("cpu")
        return c if isinstance(c, int) and c >= 0 else None

    cores = [r for r in records if r.get("scope") == "cpu" and r.get("score")]
    threads = [r for r in records if r.get("scope") == "thread"]
    out = []
    if len(cores) > 1:
        labels, groups = _kinds(cores, cpu_models, "all cores")
        for label, g in zip(labels, groups):
            out.append({"scope": "cpu", "name": label, "ids": [r["id"] for r in g],
                        "cpus": sorted(c for c in map(cpu_of, g) if c is not None)})
    elif (len(threads) > 1
          and all(t.get("score") and cpu_of(t) is not None for t in threads)):
        labels, groups = _kinds(threads, cpu_models, "all threads")
    else:
        labels, groups = [], []

    if len(threads) > 1:
        kind = {cpu_of(r): label for label, g in zip(labels, groups) for r in g
                if cpu_of(r) is not None}
        if all(cpu_of(t) in kind for t in threads):
            for label in labels:
                mine = [t for t in threads if kind[cpu_of(t)] == label]
                if mine:
                    out.append({"scope": "thread", "name": label,
                                "ids": [t["id"] for t in mine],
                                "cpus": sorted(c for t in mine
                                               if (c := cpu_of(t)) is not None)})
        else:
            out.append({"scope": "thread", "name": "all threads",
                        "ids": [t["id"] for t in threads],
                        "cpus": sorted(c for c in map(cpu_of, threads) if c is not None)})
    return out


# The same CPU -------------------------------------------------------------
#
# How a result sits among the others from the same processor: a phone measured
# warm, or a desktop on a power-saving plan, shows as one run well under the
# rest. Runs are peers when they name the same CPU line, the same target and
# the same two build switches -- the board's own condition for comparing them
# at all -- and, for the whole-machine and per-thread rows, the same number of
# threads, since a run on half the cores is a different measurement.

def guess_layout(records: list[dict], cpu_models: str | None,
                 cpus: int | None) -> str | None:
    """A run's core counts worked out from what else it reported.

    The fallback for a run that did not report `system.cpu_layout`: the same
    "2x Cortex-A76 + 6x Cortex-A55" it would have said, or None where nothing
    settles it.

    A line of one Arm core design ("Cortex-A55") is that design on every one
    of the machine's `cpus`. Other single names are left alone: "Intel(R)
    N100" names a chip, not a core, and "4x" of it would be wrong.

    A line of several designs is settled by the per-core scores, and only when
    the sweep measured every one of `cpus` -- a sweep of some of them counts
    only those -- and its steps come out as one per design, after
    core_types() joins a design measured at two clocks. Fewer steps than
    names means two designs ran alike, and which is which cannot be told.
    """
    if not cpu_models or not cpus:
        return None
    names = named_designs(cpu_models)
    if not names:
        bare = bare_line(cpu_models)
        return f"{cpus}x {bare}" if ARM_DESIGN_RE.match(bare) else None
    swept = [r for r in records if r.get("scope") == "cpu"]
    if (len(swept) != cpus
            or any(not r.get("score") or not isinstance(r.get("cpu"), int) for r in swept)):
        return None
    count = {t["name"]: len(t["ids"]) for t in core_types(records, cpu_models)
             if t["scope"] == "cpu"}
    if sorted(count) != sorted(names):
        return None
    return " + ".join(f"{count[n]}x {n}" for n in names)


# The core designs Arm itself names, as the benchmark spells them from a MIDR:
# each is one kind of core whatever chip it is in, so a count of them is
# meaningful. Other vendors' names are left to the benchmark to count. ARM11
# is the 32-bit generation before Cortex: "ARM1176" in the first Raspberry Pi.
ARM_DESIGN_RE = re.compile(r"^((Cortex|Neoverse|C1)-[A-Za-z0-9]+|ARM11[0-9A-Za-z-]*)$")

BOARD_RE = re.compile(r"\s*\(([^()]*)\)$")


def bare_line(cpu_models: str) -> str:
    """The CPU line without the board in parentheses at its end, if any."""
    return BOARD_RE.sub("", cpu_models) or cpu_models


# A device-tree id, "vendor,core": what a RISC-V kernel prints as `uarch`,
# beside its "model name". The benchmark took both for names of cores, so a
# line read "Spacemit(R) X60 + spacemit,x60" -- one core, said twice, and
# looking like two kinds. The id is kept only where it is the only name.
DT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*,[a-z0-9][a-z0-9._+-]*$")


def tidy_line(cpu_models: str | None) -> str | None:
    """The CPU line without device-tree ids where it names its CPU otherwise."""
    if not cpu_models:
        return cpu_models
    names = bare_line(cpu_models).split(" + ")
    kept = [n for n in names if not DT_ID_RE.match(n.strip())]
    if not kept or len(kept) == len(names):
        return cpu_models
    board = BOARD_RE.search(cpu_models)
    return " + ".join(kept) + (board.group(0) if board else "")


def board_of(cpu_models: str) -> str | None:
    m = BOARD_RE.search(cpu_models)
    return m.group(1) if m else None


# What a run's core layout is: as reported, or else as the hub has it -- from
# the merge list or worked out by guess_layout().
LAYOUT_SQL = "COALESCE(r.cpu_layout, r.cpu_layout_guess)"


def cpu_family(cpu_models: str, machine: str | None = None) -> str:
    """The CPU line without the board it was measured on, where the rest is cores.

    The benchmark appends the platform's name in parentheses ("Cortex-A55 +
    Cortex-A76 (Radxa ROCK 5B)"), which tells two boards apart in a run's own
    title but must not tell their processors apart. It is set aside where the
    line names core designs -- several, or one of Arm's -- since then the
    core counts, which peer_key() also reads, say the rest: four Cortex-A53s
    are four Cortex-A53s on any board. A line that names a chip or a board
    keeps all of it.

    So does one whose parentheses are the run's `machine`: firmware names its
    chip there and in `system.machine` both ("Cortex-M33 (RP2350)" on an
    RP2350), and on a microcontroller the chip -- its SRAM, its clock -- is
    the processor, where a Linux board's name is only where the processor sat.
    A Cortex-M4F in an nRF52840 and one in an STM32F401 are two CPUs.
    """
    bare = bare_line(cpu_models)
    if machine and board_of(cpu_models) == machine:
        return cpu_models
    return bare if named_designs(cpu_models) or ARM_DESIGN_RE.match(bare) else cpu_models


# The merge list: CPU lines that are another CPU under a different name. A
# vendor kernel can print its own name for a chip ("Rockchip RK3588S CoolPi")
# where the mainline one names the cores, and nothing in the line says the two
# are the same silicon; an operator who knows says so here. Read once, when
# the hub starts.
ALIASES_PATH = HERE / "cpu-aliases.json"


def load_aliases(path: Path | None) -> dict[str, dict]:
    """{bare line: {"cpu", "layout"}} from a merge-list file; {} for no file.

    The file is a JSON list of {"line", "cpu", "layout"?, "why"?}: runs whose
    line, board aside, is `line` are compared as `cpu`, with `layout` as their
    core counts where they did not report their own.
    """
    if path is None or not path.exists():
        return {}
    entries = json.loads(path.read_text())
    if not isinstance(entries, list):
        raise ValueError(f"{path}: a list of entries expected")  # noqa: TRY004 -- a bad file, not a bad call
    out: dict[str, dict] = {}
    for e in entries:
        if not (isinstance(e, dict) and isinstance(e.get("line"), str)
                and isinstance(e.get("cpu"), str)
                and isinstance(e.get("layout", ""), str)):
            raise ValueError(  # noqa: TRY004 -- a bad file, not a bad call
                f"{path}: each entry needs \"line\" and \"cpu\" "
                f"strings, and \"layout\" if any a string: {e!r}")
        out[e["line"]] = {"cpu": e["cpu"], "layout": e.get("layout") or None}
    return out


def classify(cpu_models: str | None, cpus: int | None, cpus_offline: int | None,
             records: list[dict], aliases: dict[str, dict],
             machine: str | None = None) -> tuple[str | None, str | None]:
    """(cpu_key, layout) for one run: what it is compared as, and its core
    counts as the hub has them where the run did not report its own."""
    if not cpu_models:
        return None, None
    alias = aliases.get(bare_line(cpu_models))
    if alias:
        return alias["cpu"], alias["layout"]
    # A sweep of only the CPUs that were up describes a smaller machine than
    # the one named, so nothing is counted from it.
    layout = None if cpus_offline else guess_layout(records, cpu_models, cpus)
    return cpu_family(cpu_models, machine), layout


def design_line(run: dict) -> str | None:
    """The line to name a run's kinds of core from: its own where that names
    designs, or else the CPU the merge list says it is."""
    own = run.get("cpu_models")
    return own if named_designs(own) or not run.get("cpu_key") else run["cpu_key"]


def comparable(row: dict) -> bool:
    """Whether a run can stand among others of its CPU at all.

    Not one that named no CPU, and not one measured with CPUs switched off:
    that machine was less than its CPU line says, by an amount the line does
    not say. Nor one its uploader has hidden.
    """
    return (bool(row.get("cpu_key")) and not row.get("cpus_offline")
            and not row.get("hidden_at"))


# A run its uploader hid is off the board, out of every comparison and out of
# the counts. It is still there: its link works, and its owner still lists it.
VISIBLE_SQL = "r.hidden_at IS NULL"

# The same condition as comparable(), in SQL, for the runs a comparison is
# drawn from.
COMPARABLE_SQL = ("r.cpu_key IS NOT NULL AND COALESCE(r.cpus_offline, 0) = 0 "
                  f"AND {VISIBLE_SQL}")


def peer_key(row: dict, scope: str | None) -> tuple:
    key = (row.get("cpu_key"), row.get("cpu_layout"), row.get("target"),
           row.get("vectorize"), row.get("fma"))
    return key if scope == "cpu" else key + (row.get("threads"),)


def standing(value: float, values: list[float], better: str) -> dict:
    """Where `value` stands among `values`, which include it."""
    beats = (lambda v: v < value) if better == "low" else (lambda v: v > value)
    return {"value": value, "median": statistics.median(values),
            "low": min(values), "high": max(values), "of": len(values),
            "rank": 1 + sum(1 for v in values if better != "none" and beats(v))}


class Store:
    # The attest_* columns are vestigial: the hub once accepted GitHub
    # signatures over a result, and no longer does. They stay on the table so
    # a database written by an older build still opens and still holds what it
    # recorded, but nothing reads or writes them. See schema.sql.
    ADDED_COLUMNS = (("compiler_version", "TEXT"), ("cc", "TEXT"),
                     ("build_flags", "TEXT"), ("user_id", "INTEGER"),
                     ("binary_sha256", "TEXT"), ("attest_tier", "TEXT"),
                     ("attest_repo", "TEXT"), ("attest_workflow", "TEXT"),
                     ("attest_run_url", "TEXT"), ("attest_at", "TEXT"),
                     ("cpu_layout", "TEXT"), ("cpu_layout_guess", "TEXT"), ("cpu_key", "TEXT"),
                     ("cpus_offline", "INTEGER"), ("upload_id", "INTEGER"),
                     ("doc_sha256", "TEXT"), ("hidden_at", "TEXT"))
    # The same for `cores`, which schema.sql cannot widen either: CREATE TABLE
    # IF NOT EXISTS leaves an existing table as it was, so without this a new
    # metric's column never reaches an old database and every upload after it
    # fails on the INSERT.
    ADDED_CORE_COLUMNS = (("fp32_lat", "REAL"), ("fp32_thr", "REAL"))

    def __init__(self, path: str, aliases_path: Path | None = ALIASES_PATH):
        self.path = path
        self.aliases = load_aliases(aliases_path)
        with self.connect() as db:
            db.executescript(SCHEMA_PATH.read_text())
            # Bring a database made by an older build up to date. Names are
            # from the tuple above, never from a request.
            have = {r["name"] for r in db.execute("PRAGMA table_info(runs)")}
            for col, decl in self.ADDED_COLUMNS:
                if col not in have:
                    db.execute(f"ALTER TABLE runs ADD COLUMN {col} {decl}")
            have = {r["name"] for r in db.execute("PRAGMA table_info(cores)")}
            for col, decl in self.ADDED_CORE_COLUMNS:
                if col not in have:
                    db.execute(f"ALTER TABLE cores ADD COLUMN {col} {decl}")
            if "renamed_at" not in {r["name"] for r in db.execute("PRAGMA table_info(users)")}:
                db.execute("ALTER TABLE users ADD COLUMN renamed_at TEXT")
            # After the ALTER, not in schema.sql: an older database has neither
            # column when the script runs, and an index over a column that does
            # not exist yet is an error rather than a no-op.
            db.execute("CREATE INDEX IF NOT EXISTS runs_user ON runs(user_id, id)")
            db.execute("CREATE INDEX IF NOT EXISTS runs_binary "
                       "ON runs(binary_sha256)")
            self._migrate_verified_builds(db)
            db.execute("CREATE INDEX IF NOT EXISTS runs_cpu_key ON runs(cpu_key)")
            db.execute("CREATE INDEX IF NOT EXISTS runs_doc ON runs(doc_sha256)")
            db.execute("CREATE INDEX IF NOT EXISTS runs_delete_token "
                       "ON runs(delete_token)")
            # Runs stored before re-uploads were refused. Not a unique index:
            # a database from then may hold the same document twice already,
            # and both copies stay -- only a new one is turned away.
            for r in db.execute("SELECT id, raw FROM runs "
                                "WHERE doc_sha256 IS NULL").fetchall():
                db.execute("UPDATE runs SET doc_sha256 = ? WHERE id = ?",
                           (document_digest(r["raw"]), r["id"]))
            self._fix_clocks(db)
            # Every run, every start: the merge list may have changed since,
            # or the rules, and a run from before either is classified the way
            # one uploaded now would be.
            for r in db.execute("SELECT id FROM runs").fetchall():
                self._classify(db, r["id"])
            # Runs stored before the hub could tell what an unnamed target was.
            for r in db.execute("SELECT id, target, machine, os_release FROM runs "
                                "WHERE target IS NULL OR target = 'unknown'").fetchall():
                target = known_target(r["target"], r["machine"], r["os_release"])
                if target != r["target"]:
                    db.execute("UPDATE runs SET target = ? WHERE id = ?", (target, r["id"]))
            # Runs stored before uploads were told apart, in the order they
            # arrived, so each is joined to the upload before it exactly as it
            # would have been then.
            for r in db.execute("SELECT id FROM runs WHERE upload_id IS NULL "
                                "ORDER BY id").fetchall():
                self._join_upload(db, r["id"])

    @staticmethod
    def _fix_clocks(db: sqlite3.Connection) -> None:
        """Runs stored with clocks the 0.5.1 benchmark would not have written:
        see fix_clocks(). Every run, every start -- whether a run qualifies
        depends on its other rows, which no one WHERE clause sees -- and a
        no-op once they are fixed."""
        rows_of: dict[int, list[dict]] = defaultdict(list)
        for r in db.execute("SELECT id, run_id, scope, cpu, mhz, mhz_src, "
                            "int_lat FROM cores ORDER BY id"):
            rows_of[r["run_id"]].append(dict(r))
        for run in db.execute("SELECT id, threads, cpus, pin FROM runs").fetchall():
            rows = rows_of.get(run["id"], [])
            before = {r["id"]: (r["mhz"], r["mhz_src"]) for r in rows}
            if not fix_clocks(rows, run["threads"], run["cpus"], run["pin"]):
                continue
            db.executemany(
                "UPDATE cores SET mhz = ?, mhz_src = ? WHERE id = ?",
                [(r["mhz"], r["mhz_src"], r["id"]) for r in rows
                 if (r["mhz"], r["mhz_src"]) != before[r["id"]]])

    @staticmethod
    def _migrate_verified_builds(db: sqlite3.Connection) -> None:
        """Re-key a verified_builds table written before releases could share.

        The old table was keyed on the digest alone, which cannot hold the same
        binary appearing in two releases: the second manifest to be loaded
        overwrote the first, so which release a run was labelled with came down
        to the order an operator happened to run web/verified.py in. The primary
        key is now (sha256, release), which SQLite cannot express as an ALTER --
        hence the copy.

        Nothing is lost and nothing is invented: whatever pairs the old table
        held are carried over as they stand. The releases it dropped along the
        way come back the next time their manifests are loaded, which is a
        re-run of web/verified.py and not a repair job.
        """
        cols = list(db.execute("PRAGMA table_info(verified_builds)"))
        if not cols:                        # a fresh database; schema.sql made it
            return
        key = {c["name"] for c in cols if c["pk"]}
        if key == {"sha256", "release"} and any(c["name"] == "release_rank"
                                                for c in cols):
            return

        db.execute("""
            CREATE TABLE verified_builds_new (
                sha256      TEXT    NOT NULL,
                release     TEXT    NOT NULL,
                release_rank TEXT   NOT NULL,
                release_url TEXT,
                filename    TEXT,
                target      TEXT,
                march       TEXT,
                added_at    TEXT    NOT NULL,
                PRIMARY KEY (sha256, release)
            )""")
        rows = [(r["sha256"], r["release"], release_rank(r["release"]),
                 r["release_url"], r["filename"], r["target"], r["march"],
                 r["added_at"])
                for r in db.execute("SELECT * FROM verified_builds")]
        db.executemany(
            "INSERT OR REPLACE INTO verified_builds_new "
            "(sha256, release, release_rank, release_url, filename, target, "
            " march, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        db.execute("DROP TABLE verified_builds")
        db.execute("ALTER TABLE verified_builds_new RENAME TO verified_builds")
        # The old table's indexes went with it.
        db.execute("CREATE INDEX IF NOT EXISTS verified_builds_sha "
                   "ON verified_builds(sha256)")

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA journal_mode = WAL")
        return db

    def insert(self, run: dict, cores: list[dict], raw: str,
               label: str | None, notes: str | None,
               user_id: int | None = None) -> tuple[int, str]:
        """Store one validated upload: (run id, delete token).

        Raises Duplicate when the hub already holds this document. The check
        and the write share one transaction, so the same document posted twice
        at once -- a retry racing the original -- is stored once.
        """
        digest = document_digest(raw)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            same = self._holding(db, digest)
            if same is not None:            # leaving the block rolls back
                raise Duplicate(same)
            run_id, token = self._store(db, run, cores, raw, label, notes,
                                        user_id, digest)
            self._join_upload(db, run_id)
        return run_id, token

    @staticmethod
    def _holding(db: sqlite3.Connection, digest: str | None) -> int | None:
        """The run that already is this document, if the hub holds one."""
        row = db.execute("SELECT id FROM runs WHERE doc_sha256 = ? "
                         "ORDER BY id LIMIT 1", (digest,)).fetchone()
        return int(row["id"]) if row else None

    def _store(self, db: sqlite3.Connection, run: dict, cores: list[dict],
               raw: str, label: str | None, notes: str | None,
               user_id: int | None, digest: str | None) -> tuple[int, str]:
        """Write one run and its records, inside the caller's transaction."""
        token = secrets.token_urlsafe(24)
        # The delete token is issued whether or not an account owns the run:
        # signing in later must not be the only way back to something uploaded
        # from a shell, and an account holder loses nothing by also having one.
        run = dict(run, created_at=iso_now(), delete_token=token,
                   label=label, notes=notes, raw=raw, user_id=user_id,
                   doc_sha256=digest)
        cols = list(run)
        cur = db.execute(
            f"INSERT INTO runs ({','.join(cols)}) "
            f"VALUES ({','.join('?' * len(cols))})",
            [run[c] for c in cols])
        # sqlite3 sets lastrowid on every successful INSERT into a rowid
        # table; None here would mean the row above did not happen.
        assert cur.lastrowid is not None
        run_id = int(cur.lastrowid)
        ccols = ["run_id", "scope"] + CORE_COLUMNS
        db.executemany(
            f"INSERT INTO cores ({','.join(ccols)}) "
            f"VALUES ({','.join('?' * len(ccols))})",
            [[run_id] + [c.get(col) for col in ccols[1:]] for c in cores])
        self._classify(db, run_id)
        return run_id, token

    def held(self, digests: list[str | None]) -> dict[str, int]:
        """Which of these documents the hub already holds: digest -> run id."""
        want = [d for d in dict.fromkeys(digests) if d]
        out: dict[str, int] = {}
        with self.connect() as db:
            for i in range(0, len(want), 500):      # under SQLite's variable cap
                chunk = want[i:i + 500]
                for r in db.execute(
                        f"SELECT doc_sha256, MIN(id) AS id FROM runs "
                        f"WHERE doc_sha256 IN ({','.join('?' * len(chunk))}) "
                        f"GROUP BY doc_sha256", chunk):
                    out[r["doc_sha256"]] = int(r["id"])
        return out

    def insert_batch(self, uploads: list[dict], user_id: int) -> list[dict]:
        """Store a planned batch (see plan_batch) in one transaction.

        Each entry of `uploads` is a planned upload with a "label" and
        "notes" for its runs set by the caller. A run whose document the hub
        already holds, or that came earlier in this same batch, is skipped
        and named. The runs of each upload are tied together as one upload
        here and now, not by _join_upload's guess: the batch said which they
        are. Returns, per upload, its id and each run's outcome.
        """
        out = []
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            seen: dict[str, int] = {}
            for up in uploads:
                stored: list[int] = []
                runs = []
                for r in up["runs"]:
                    digest = document_digest(r["raw"])
                    same = seen.get(digest or "") or self._holding(db, digest)
                    entry = {"index": r["index"], "variant": variant_name(r["run"])}
                    if same is not None:
                        entry["duplicate_of"] = same
                    else:
                        run_id, token = self._store(db, r["run"], r["cores"], r["raw"],
                                                    r["label"], up["notes"], user_id,
                                                    digest)
                        if digest:
                            seen[digest] = run_id
                        stored.append(run_id)
                        entry.update(id=run_id, label=r["label"], delete_token=token)
                    runs.append(entry)
                if stored:
                    db.execute(f"UPDATE runs SET upload_id = ? WHERE id IN "
                               f"({','.join('?' * len(stored))})", [stored[0], *stored])
                out.append({"id": stored[0] if stored else None,
                            "label": up["label"], "runs": runs})
        return out

    # What two variants of one upload share. Everything the run was told or
    # found about the machine and the measurement; only the build differs.
    SAME_UPLOAD = MACHINE_SAME + ("notes",)

    def _join_upload(self, db: sqlite3.Connection, run_id: int) -> None:
        """Set a run's upload_id: the upload just before it, if it continues
        that one, else a new upload of its own.

        The variants of a --variants run arrive as separate posts with nothing
        to tie them together but their labels, which end in the variant's name.
        A run continues the upload before it only when all of this holds: the
        same account sent both, with no other run of that account between
        them; both labels carry a variant tag and agree apart from it; the
        machine, binary and settings are the same; the build is one that
        upload does not have yet; and it landed within UPLOAD_GAP of the last.
        Anonymous runs have no account to tie them by and are each their own.
        """
        cols = ", ".join(("id", "user_id", "label", "upload_id", "created_at")
                         + self.SAME_UPLOAD)
        run = db.execute(f"SELECT {cols} FROM runs WHERE id = ?", (run_id,)).fetchone()
        upload = run_id
        base, tagged = split_variant(run["label"])
        prev = None
        if run["user_id"] is not None and tagged:
            prev = db.execute(f"SELECT {cols} FROM runs WHERE user_id = ? AND id < ? "
                              f"ORDER BY id DESC LIMIT 1",
                              (run["user_id"], run_id)).fetchone()
        if prev is not None and prev["upload_id"] is not None:
            prev_base, prev_tagged = split_variant(prev["label"])
            gap = parse_iso(run["created_at"]) - parse_iso(prev["created_at"])
            builds = db.execute("SELECT vectorize, fma FROM runs WHERE upload_id = ?",
                                (prev["upload_id"],)).fetchall()
            mine = db.execute("SELECT vectorize, fma FROM runs WHERE id = ?",
                              (run_id,)).fetchone()
            if (prev_tagged and prev_base == base
                    and all(run[c] == prev[c] for c in self.SAME_UPLOAD)
                    and timedelta(0) <= gap <= UPLOAD_GAP
                    and tuple(mine) not in {tuple(b) for b in builds}):
                upload = prev["upload_id"]
        db.execute("UPDATE runs SET upload_id = ? WHERE id = ?", (upload, run_id))

    @staticmethod
    def _may_change(row: sqlite3.Row, token: str | None, user_id: int | None) -> bool:
        """Whether the asker holds one of the two claims to a stored run.

        The delete token proves whoever holds the upload's reply, and the
        account id proves the run was uploaded under it. Either is enough on
        its own; neither is checked against the other.
        """
        owns = user_id is not None and row["user_id"] == user_id
        # compare_digest even though a token mismatch is not timed against
        # anything an attacker controls the length of; it costs nothing.
        holds = token is not None and same_secret(token, row["delete_token"])
        return owns or holds

    def edit_run(self, run_id: int, changes: dict, token: str | None = None,
                 user_id: int | None = None) -> dict | None:
        """Change a run's label and/or notes, or hide it, for whoever could
        withdraw it.

        `changes` holds only the fields to set; None clears one. `hidden` is a
        bool: True takes the run off the board and out of every comparison,
        False puts it back. Hiding a run already hidden keeps the time it was
        first hidden. Returns the run's label, notes and whether it is hidden,
        as they now stand, or None when there is no such run or it is not the
        asker's.
        """
        with self.connect() as db:
            row = db.execute("SELECT delete_token, user_id FROM runs WHERE id = ?",
                             (run_id,)).fetchone()
            if row is None or not self._may_change(row, token, user_id):
                return None
            # Names from the fixed pair below, never from the request.
            sets = [f"{c} = ?" for c in ("label", "notes") if c in changes]
            vals: list[object] = [changes[c] for c in ("label", "notes") if c in changes]
            if "hidden" in changes:
                sets.append("hidden_at = COALESCE(hidden_at, ?)" if changes["hidden"]
                            else "hidden_at = NULL")
                if changes["hidden"]:
                    vals.append(iso_now())
            if sets:
                db.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id = ?",
                           vals + [run_id])
            out = dict(db.execute("SELECT id, label, notes, hidden_at FROM runs "
                                  "WHERE id = ?", (run_id,)).fetchone())
        out["hidden"] = out["hidden_at"] is not None
        return out

    def adopt(self, user_id: int, tokens: list[str]) -> dict:
        """Put the anonymous runs these delete tokens belong to on an account.

        A token proves the run is the asker's exactly as it does for
        withdrawing one, so holding it is enough. A run already on another
        account stays there: the token is still a claim to it, but not one
        that outranks the account it was uploaded under. The delete token
        keeps working afterwards, as it does on any run an account owns.

        Adopted runs are then tied into uploads as if the account had sent
        them, so the variants of one --variants run sent anonymously list as
        one upload. Returns the ids adopted, those already this account's, and
        how many tokens matched nothing this account could take.
        """
        adopted: list[int] = []
        already: list[int] = []
        refused = 0
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for token in dict.fromkeys(tokens):       # each once, in order
                row = db.execute("SELECT id, user_id FROM runs WHERE delete_token = ?",
                                 (token,)).fetchone()
                if row is None or row["user_id"] not in (None, user_id):
                    refused += 1
                elif row["user_id"] == user_id:
                    already.append(int(row["id"]))
                else:
                    adopted.append(int(row["id"]))
            for run_id in adopted:
                db.execute("UPDATE runs SET user_id = ? WHERE id = ?", (user_id, run_id))
            # In the order they arrived, as _join_upload expects to meet them.
            for run_id in sorted(adopted):
                self._join_upload(db, run_id)
        return {"adopted": sorted(adopted), "already": sorted(already),
                "refused": refused}

    def _classify(self, db: sqlite3.Connection, run_id: int) -> None:
        """Store what a run is compared as, and its core counts where it did
        not report them; see classify()."""
        run = dict(db.execute("SELECT cpu_models, cpus, cpus_offline, cpu_layout, cpu_key, "
                              "cpu_layout_guess, machine FROM runs WHERE id = ?",
                              (run_id,)).fetchone())
        # Stored before the hub tidied lines; the raw document keeps the original.
        tidy = tidy_line(run["cpu_models"])
        if tidy != run["cpu_models"]:
            db.execute("UPDATE runs SET cpu_models = ? WHERE id = ?", (tidy, run_id))
            run["cpu_models"] = tidy
        # The records are only read where a count could come from them.
        rows = (db.execute("SELECT id, scope, cpu, score FROM cores WHERE run_id = ?",
                           (run_id,)).fetchall()
                if run["cpu_models"] and not run["cpu_layout"]
                and named_designs(run["cpu_models"]) else [])
        key, layout = classify(run["cpu_models"], run["cpus"], run["cpus_offline"],
                               [dict(r) for r in rows], self.aliases, run["machine"])
        if run["cpu_layout"]:
            layout = None
        if (key, layout) != (run["cpu_key"], run["cpu_layout_guess"]):
            db.execute("UPDATE runs SET cpu_key = ?, cpu_layout_guess = ? WHERE id = ?",
                       (key, layout, run_id))

    def delete(self, run_id: int, token: str | None = None,
               user_id: int | None = None) -> bool:
        """Withdraw a run, for whoever can show one of the two claims to it;
        see _may_change()."""
        with self.connect() as db:
            row = db.execute("SELECT delete_token, user_id FROM runs WHERE id = ?",
                             (run_id,)).fetchone()
            if row is None or not self._may_change(row, token, user_id):
                return False
            db.execute("DELETE FROM cores WHERE run_id = ?", (run_id,))
            db.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        return True

    def run(self, run_id: int) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                f"SELECT {run_columns_sql()}, {SUBMITTER}, {RELEASE_BUILD} "
                f"FROM runs r WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                return None
            cores = db.execute(
                f"SELECT id, scope, {','.join(CORE_COLUMNS)} FROM cores "
                f"WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
        out = dict(row)
        out["cores"] = [dict(c) for c in cores]
        out["types"] = core_types(out["cores"], design_line(out))
        kind = {i: t["name"] for t in out["types"] for i in t["ids"]}
        for c in out["cores"]:
            c["type"] = kind.get(c["id"])
        return out

    def types_of_run(self, run_id: int) -> list[dict]:
        """core_types() for one stored run, whatever a listing narrowed it to."""
        with self.connect() as db:
            run = db.execute("SELECT cpu_models, cpu_key FROM runs WHERE id = ?",
                             (run_id,)).fetchone()
            if run is None:
                return []
            rows = db.execute("SELECT id, scope, cpu, score FROM cores "
                              "WHERE run_id = ?", (run_id,)).fetchall()
        return core_types([dict(r) for r in rows], design_line(dict(run)))

    def run_bests(self, cpu_keys: list[str], scope: str,
                  exprs: list[tuple[str, str, str]]) -> list[dict]:
        """Every comparable run of these CPUs at one scope, one row each.

        A row holds what peer_key() reads plus, for each (name, SQL, MIN|MAX)
        in `exprs`, that aggregate over the run's records -- its best core, or
        for the one-record total scope, its total.
        """
        if not cpu_keys:
            return []
        cols = ", ".join(f"{agg}({sql}) AS {name}" for name, sql, agg in exprs)
        marks = ",".join("?" * len(cpu_keys))
        with self.connect() as db:
            rows = db.execute(
                f"SELECT r.id AS run_id, r.cpu_key, r.cpus_offline, "
                f"       {LAYOUT_SQL} AS cpu_layout, r.target, r.vectorize, "
                f"       r.fma, r.threads, {cols} "
                f"FROM cores c JOIN runs r ON r.id = c.run_id "
                f"WHERE c.scope = ? AND r.cpu_key IN ({marks}) AND {COMPARABLE_SQL} "
                f"GROUP BY r.id", [scope, *cpu_keys]).fetchall()
        return [dict(r) for r in rows]

    def cpu_fit(self, run_id: int) -> dict | None:
        """How this run stands among its peers, per metric: whole machine and best core."""
        with self.connect() as db:
            run = db.execute(f"SELECT r.cpu_models, r.cpu_key, {LAYOUT_SQL} AS cpu_layout, r.target, "
                             f"r.vectorize, r.fma, r.threads, r.cpus_offline, r.hidden_at "
                             f"FROM runs r WHERE r.id = ?", (run_id,)).fetchone()
            scopes = {r["scope"] for r in db.execute(
                "SELECT DISTINCT scope FROM cores WHERE run_id = ?", (run_id,))}
        if run is None or not comparable(dict(run)):
            return None
        ranked = [m for m in METRICS if m["better"] != "none"]
        exprs = [(m["key"], f"c.{m['key']}", "MIN" if m["better"] == "low" else "MAX")
                 for m in ranked]
        out: dict = {"cpu_models": run["cpu_models"], "cpu_key": run["cpu_key"], "scopes": {}}
        # Per-thread rows are left out: each is a share of the whole machine's
        # memory and caches, and the total row already says what that came to.
        for scope in ("total", "cpu"):
            if scope not in scopes:
                continue
            want = peer_key(dict(run), scope)
            peers = [p for p in self.run_bests([run["cpu_key"]], scope, exprs)
                     if peer_key(p, scope) == want]
            mine = next((p for p in peers if p["run_id"] == run_id), None)
            if mine is None or len(peers) < 2:
                continue
            metrics = {}
            for m in ranked:
                vals = [p[m["key"]] for p in peers if p[m["key"]] is not None]
                if mine[m["key"]] is not None and len(vals) > 1:
                    metrics[m["key"]] = standing(mine[m["key"]], vals, m["better"])
            out["scopes"][scope] = {"runs": len(peers), "metrics": metrics}
        return out

    def add_fit(self, rows: list[dict], scope: str, sort: str, desc: bool,
                norm: str) -> None:
        """Mark each board row with where it stands among its CPU's runs.

        At the board's own metric, reading and pick: a run's representative is
        compared with every other run's representative, chosen the same way.
        Rows with no peer are left unmarked.
        """
        models = sorted({r["cpu_key"] for r in rows if comparable(r)})
        if not models or sort == "mhz":
            return
        better = next(m["better"] for m in METRICS if m["key"] == sort)
        peers = self.run_bests(models, scope,
                               [("v", sort_expr(sort, norm), "MAX" if desc else "MIN")])
        by_key: dict[tuple, list[float]] = defaultdict(list)
        for p in peers:
            if p["v"] is not None:
                by_key[peer_key(p, scope)].append(p["v"])
        for r in rows:
            vals = by_key.get(peer_key(r, scope), []) if comparable(r) else []
            if len(vals) > 1 and r.get("sort_value") is not None:
                r["cpu_fit"] = standing(r["sort_value"], vals, better)
                # Which rows share it, for the page to light them up together.
                r["peers"] = json.dumps(peer_key(r, scope))

    def cpu_groups(self, *, sort: str, desc: bool, norm: str, limit: int,
                   offset: int, **filters) -> tuple[list[dict], int]:
        """The board with one row per CPU line rather than per run.

        A row is the median of its runs' representatives, metric by metric,
        in the reading the board is showing (`normalised` says the per-GHz
        arithmetic is done already). Ordered by the median at `sort`. Returns
        one page of rows and how many there are in all.
        """
        scope = filters.get("scope")
        rows = self.cores(group_run=True, sort=sort, desc=desc, norm=norm,
                          limit=-1, offset=0, **filters)
        grouped: dict[tuple, list[dict]] = {}
        for r in rows:
            # A machine that named no CPU is a group of its own: "unknown" is
            # not a processor two runs can share. Nor is one measured with
            # CPUs switched off a sample of its CPU.
            key = peer_key(r, scope) if comparable(r) else ("run", r["run_id"])
            grouped.setdefault(key, []).append(r)

        def median(vals):
            vals = [v for v in vals if v is not None]
            return statistics.median(vals) if vals else None

        out = []
        for members in grouped.values():
            first = members[0]
            g = {k: first.get(k) for k in ("cpu_models", "cpu_key", "cpu_layout",
                                            "cpus_offline", "target", "vectorize", "fma",
                                            "machine", "threads", "scope")}
            g["runs"] = len(members)
            # The CPU the group shares, and the boards it was measured on --
            # each one's own name for itself where it is not that CPU's. A
            # lone run keeps its own title, board and all.
            key = g["cpu_key"]
            boards = {board_of(m["cpu_models"]) or m["cpu_models"] for m in members
                      if m.get("cpu_models") and m["cpu_models"] != key}
            g["boards"] = sorted(boards)
            if len(members) > 1:
                g["cpu_models"] = key
            g["run_id"] = first["run_id"] if len(members) == 1 else None
            g["sysnames"] = sorted({m["sysname"] for m in members if m.get("sysname")})
            g["mhz"] = median(m.get("mhz") for m in members)
            g["normalised"] = norm == "ghz"
            for key in METRIC_KEYS:
                g[key] = median(normalised(m, key, norm) for m in members)
            g["sort_value"] = median(m.get("sort_value") for m in members)
            vals = [m["sort_value"] for m in members if m.get("sort_value") is not None]
            g["low"], g["high"] = (min(vals), max(vals)) if vals else (None, None)
            out.append(g)
        out.sort(key=lambda g: (g["sort_value"] is None,
                                -(g["sort_value"] or 0) if desc else (g["sort_value"] or 0)))
        return out[offset:offset + limit], len(out)

    def raw(self, run_id: int) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT raw FROM runs WHERE id = ?", (run_id,)).fetchone()
        return row["raw"] if row else None

    @staticmethod
    def _runs_where(user_id: int | None, include_hidden: bool) -> tuple[str, list]:
        """The WHERE that runs() and run_total() share, so a page and its
        count agree. Hidden runs are left out unless the listing is their
        owner's own."""
        where, args = [], []
        if user_id is not None:
            where.append("r.user_id = ?")
            args.append(user_id)
        if not include_hidden:
            where.append(VISIBLE_SQL)
        return (f"WHERE {' AND '.join(where)} " if where else ""), args

    def runs(self, limit: int = 50, offset: int = 0,
             user_id: int | None = None, include_hidden: bool = False) -> list[dict]:
        where, args = self._runs_where(user_id, include_hidden)
        with self.connect() as db:
            rows = db.execute(
                f"SELECT {run_columns_sql()}, {SUBMITTER}, {RELEASE_BUILD}, "
                f"       (SELECT COUNT(*) FROM cores c WHERE c.run_id = r.id) AS records, "
                f"       (SELECT MAX(c.score) FROM cores c "
                f"          WHERE c.run_id = r.id AND c.scope != 'total') AS best_score "
                f"FROM runs r {where}ORDER BY r.id DESC LIMIT ? OFFSET ?",
                args + [limit, offset]).fetchall()
        return [dict(r) for r in rows]

    def uploads(self, user_id: int, include_hidden: bool = False) -> list[dict]:
        """One account's runs as its list shows them: by CPU, then by upload.

        A CPU here is the line the run reported, as the list prints it, and not
        a machine: two boxes with one chip are one entry, because nothing an
        upload carries says which box it was and the hub does not guess. Each
        CPU holds its uploads, newest first, and each upload its runs in the
        order they arrived -- a --variants run's builds in the order it ran
        them. CPUs come in the order of their newest upload.

        Hidden runs are listed only with `include_hidden`, which is for the
        account's own list: everyone else's view of it is the board's.
        """
        visible = "" if include_hidden else f" AND {VISIBLE_SQL}"
        with self.connect() as db:
            rows = db.execute(
                f"SELECT r.id, r.created_at, r.label, r.notes, r.mode, r.target, "
                f"       r.vectorize, r.fma, r.threads, r.sysname, r.machine, "
                f"       r.cpu_models, r.cpu_key, {LAYOUT_SQL} AS cpu_layout, "
                f"       r.cpus_offline, r.hidden_at, "
                f"       COALESCE(r.upload_id, r.id) AS upload_id "
                f"FROM runs r WHERE r.user_id = ?{visible} ORDER BY r.id DESC",
                (user_id,)).fetchall()
        uploads: dict[int, list[dict]] = {}
        for r in rows:
            run = dict(r)
            run["hidden"] = run.pop("hidden_at") is not None
            base, tagged = split_variant(run["label"])
            run["variant"] = (f"{'vector' if run['vectorize'] else 'scalar'}-"
                              f"{'fma' if run['fma'] else 'nofma'}")
            run["tagged"] = tagged
            run["base_label"] = base
            uploads.setdefault(run.pop("upload_id"), []).append(run)
        cpus: dict[tuple, dict] = {}
        for upload_id, runs in uploads.items():
            runs.reverse()                  # oldest first: the order they ran
            first = runs[0]
            key = (first["cpu_models"], first["cpu_layout"],
                   None if first["cpu_models"] else first["machine"])
            cpu = cpus.get(key)
            if cpu is None:
                cpu = cpus[key] = {
                    "cpu_models": first["cpu_models"], "cpu_layout": first["cpu_layout"],
                    "cpu_key": first["cpu_key"], "machine": first["machine"],
                    "cpus_offline": first["cpus_offline"], "runs": 0, "uploads": []}
            labels = {r["base_label"] for r in runs}
            cpu["uploads"].append({
                "id": upload_id,
                "created_at": first["created_at"],
                "last_at": runs[-1]["created_at"],
                # The label the upload was given, its variant tags aside; the
                # first run's whole label where they no longer agree.
                "label": first["base_label"] if len(labels) == 1 else first["label"],
                "notes": first["notes"],
                "runs": runs,
            })
            cpu["runs"] += len(runs)
        return list(cpus.values())

    def core_filters(self, *, scope: str | None = "cpu", target: str | None = None,
                     vectorize: int | None = None, fma: int | None = None,
                     sysname: str | None = None,
                     search: str | None = None, ids: list[int] | None = None,
                     run: int | None = None, user_id: int | None = None,
                     verified: str | None = None, cpu: str | None = None,
                     layout: str | None = None,
                     threads: int | None = None) -> tuple[list[str], list[object]]:
        """The WHERE that a listing and its count are both built from.

        In one place because they must agree: a count taken over different
        filters from the rows it counts pages the board wrong in exactly the
        way nobody notices until the last page is empty.
        """
        where = ["1 = 1"]
        args: list[object] = []
        # A listing that names its records -- one run's, or a comparison
        # link's -- shows them hidden or not, as the run's own page does. Every
        # other listing is the board, and a hidden run is not on it.
        if run is None and not ids:
            where.append(VISIBLE_SQL)
        if scope is not None:
            where.append("c.scope = ?")
            args.append(scope)
        if run is not None:
            where.append("c.run_id = ?")
            args.append(run)
        if user_id is not None:
            where.append("r.user_id = ?")
            args.append(user_id)
        # The one question a hub can actually answer about an upload: does
        # the checksum in it name a binary some release published. Not whether
        # the numbers are honest -- nothing here can establish that.
        if verified == "release":
            where.append("EXISTS (SELECT 1 FROM verified_builds vb "
                         "WHERE vb.sha256 = r.binary_sha256)")
        if target:
            where.append("r.target = ?")
            args.append(target)
        if vectorize is not None:
            where.append("r.vectorize = ?")
            args.append(vectorize)
        if fma is not None:
            where.append("r.fma = ?")
            args.append(fma)
        # uname's own spelling, matched case-insensitively: the picker offers
        # what this hub holds, but a hand-written link asking for "linux" means
        # the same machines as one asking for "Linux".
        if sysname:
            where.append("LOWER(r.sysname) = LOWER(?)")
            args.append(sysname)
        # The exact CPU line, for one row of the board grouped by CPU: the
        # search box matches a part of it, and "Ryzen 7" is several CPUs.
        if cpu:
            # The runs a group was made of, which leaves out those that were
            # not compared: each of them is a row of its own.
            where.append(f"r.cpu_key = ? AND {COMPARABLE_SQL}")
            args.append(cpu)
        if layout is not None:
            where.append(f"{LAYOUT_SQL} = ?" if layout else f"{LAYOUT_SQL} IS NULL")
            if layout:
                args.append(layout)
        if threads is not None:
            where.append("r.threads = ?")
            args.append(threads)
        if search:
            where.append("(r.cpu_models LIKE ? OR r.label LIKE ? OR r.machine LIKE ?)")
            pat = f"%{search}%"
            args += [pat, pat, pat]
        if ids:
            where.append(f"c.id IN ({','.join('?' * len(ids))})")
            args += ids
        return where, args

    def count_cores(self, *, group_run: bool = True, **filters) -> int:
        """How many rows a listing has in total, paging aside.

        With `group_run` a run is one row however many records matched, which
        is what the board counts; without it every matching record is its own.
        """
        where, args = self.core_filters(**filters)
        counted = "DISTINCT c.run_id" if group_run else "*"
        with self.connect() as db:
            row = db.execute(
                f"SELECT COUNT({counted}) AS n FROM cores c "
                f"JOIN runs r ON r.id = c.run_id "
                f"WHERE {' AND '.join(where)}", args).fetchone()
        return int(row["n"])

    def cores(self, *, group_run: bool = True, sort: str = "score",
              desc: bool = True, norm: str = "abs", limit: int = 50,
              offset: int = 0, **filters) -> list[dict]:
        """Records matching the filters, best first.

        With `group_run` (the leaderboard's default) an upload contributes one
        row, not one per core: the run's best record at `sort` stands for it,
        and `records` says how many it was picked from. Without it every
        matching record is a row of its own -- what the expanded view of a run
        and a shared comparison link both address.

        `norm` is which reading of the metric to rank on -- "abs" for the
        figure as measured, "ghz" for it per gigahertz of clock, the same view
        the page offers. It changes the order and, with `group_run`, which
        record represents a run: per GHz an upload is stood for by its most
        efficient core, not its fastest.

        `offset` is what makes the board browsable past its first page. The
        order below is total -- the metric, then the id -- so a row cannot sit
        on two pages at once or fall between them.
        """
        where, args = self.core_filters(**filters)
        # `sort` is whitelisted by the caller; never interpolate raw input here.
        assert sort in METRIC_KEYS or sort == "mhz"
        direction = "DESC" if desc else "ASC"
        key = sort_expr(sort, norm)
        # Nulls last whichever way the metric runs, then the metric, then the
        # id so the order is total. Used twice: once to pick each run's
        # representative, once to order the board itself.
        order = f"({key} IS NULL), {key} {direction}, c.id ASC"
        sql = (
            f"WITH matched AS ("
            f"  SELECT c.id AS core_id, "
            f"         COUNT(*)     OVER (PARTITION BY c.run_id) AS peers, "
            f"         ROW_NUMBER() OVER (PARTITION BY c.run_id "
            f"                            ORDER BY {order}) AS pick "
            f"  FROM cores c JOIN runs r ON r.id = c.run_id "
            f"  WHERE {' AND '.join(where)}) "
            f"SELECT c.id, c.run_id, c.scope, c.cpu, c.mhz, c.mhz_src, "
            f"       c.{', c.'.join(CORE_COLUMNS[3:])}, "
            f"       r.target, r.vectorize, r.fma, r.cpu_models, r.cpu_key, {LAYOUT_SQL} AS cpu_layout, r.cpus_offline, r.label, "
            f"       r.created_at, r.mode, r.threads, r.compiler, r.machine, "
            # Which operating system produced it. The family alone is what the
            # board prints -- Linux, Windows, macOS -- so the release string
            # this run also carries stays on the run's own page.
            f"       r.sysname, "
            f"       {SUBMITTER}, {RELEASE_BUILD}, "
            f"       {key} AS sort_value, "
            f"       m.peers AS records "
            f"FROM matched m "
            f"JOIN cores c ON c.id = m.core_id "
            f"JOIN runs r ON r.id = c.run_id "
            + ("WHERE m.pick = 1 " if group_run else "")
            + f"ORDER BY {order} "
            f"LIMIT ? OFFSET ?")
        args += [limit, offset]
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, args).fetchall()]

    def rank(self, run_id: int) -> dict:
        """Where this run's best value stands among every other run's, per metric.

        One value per run -- its best record in the scope -- so a run is set
        against other runs, not against every core on the hub: counting each
        core had a big core beating its own little ones and every little core
        anywhere. Only runs of the same scope built with the same fairness
        flags -- a vectorised build is not comparable with a scalar one -- and
        with every CPU online.
        """
        with self.connect() as db:
            run = db.execute(
                "SELECT vectorize, fma, mode FROM runs WHERE id = ?",
                (run_id,)).fetchone()
            if run is None:
                return {}
            scope = "thread" if run["mode"] == "threads" else "cpu"
            out = {"scope": scope, "metrics": {}}
            for m in METRICS:
                key = m["key"]
                if m["better"] == "none":
                    continue
                agg = "MIN" if m["better"] == "low" else "MAX"
                mine = db.execute(
                    f"SELECT {agg}({key}) AS v FROM cores "
                    f"WHERE run_id = ? AND scope = ?", (run_id, scope)).fetchone()["v"]
                if mine is None:
                    continue
                cmp_op = ">" if m["better"] == "low" else "<"
                row = db.execute(
                    f"SELECT COUNT(*) AS total, "
                    f"       SUM(CASE WHEN best.v {cmp_op} ? THEN 1 ELSE 0 END) AS beaten "
                    f"FROM (SELECT {agg}(c.{key}) AS v FROM cores c "
                    f"      JOIN runs r ON r.id = c.run_id "
                    f"      WHERE c.scope = ? AND c.{key} IS NOT NULL AND r.id != ? "
                    f"        AND r.vectorize IS ? AND r.fma IS ? "
                    # Runs with CPUs switched off are measured but not counted.
                    f"        AND COALESCE(r.cpus_offline, 0) = 0 "
                    f"        AND {VISIBLE_SQL} "
                    f"      GROUP BY c.run_id) best",
                    (mine, scope, run_id, run["vectorize"], run["fma"])).fetchone()
                total = row["total"] or 0
                beaten = row["beaten"] or 0
                out["metrics"][key] = {
                    "value": mine,
                    # How many other runs it is set against.
                    "population": total,
                    # Share of them it is strictly better than.
                    "percentile": (100.0 * beaten / total) if total else None,
                }
        out["cpu"] = self.cpu_fit(run_id)
        return out

    def count_download(self, release: str, name: str) -> None:
        """Add one to a release file's download count.

        A count and nothing else: no address, no user agent, no time. The
        privacy policy says what is kept, and this is all of it.
        """
        with self.connect() as db:
            db.execute("INSERT INTO download_counts (release, name, count) "
                       "VALUES (?, ?, 1) ON CONFLICT (release, name) "
                       "DO UPDATE SET count = count + 1", (release, name))

    def download_counts(self, release: str | None) -> dict:
        """What has been downloaded: {"total": all releases, "files": {name:
        count}} for `release`'s files."""
        with self.connect() as db:
            total = db.execute("SELECT COALESCE(SUM(count), 0) AS n "
                               "FROM download_counts").fetchone()["n"]
            files = {r["name"]: r["count"] for r in db.execute(
                "SELECT name, count FROM download_counts WHERE release = ?",
                (release,))}
        return {"total": total, "files": files}

    def stats(self) -> dict:
        with self.connect() as db:
            # What the board holds, so hidden runs are not counted: a picker
            # offering an architecture that shows nothing when picked is a
            # broken picker.
            runs = db.execute(f"SELECT COUNT(*) AS n FROM runs r "
                              f"WHERE {VISIBLE_SQL}").fetchone()["n"]
            cores = db.execute(f"SELECT COUNT(*) AS n FROM cores c "
                               f"JOIN runs r ON r.id = c.run_id "
                               f"WHERE {VISIBLE_SQL}").fetchone()["n"]
            users = db.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
            release_builds = db.execute(
                f"SELECT COUNT(*) AS n FROM runs r WHERE {VISIBLE_SQL} AND EXISTS "
                f"(SELECT 1 FROM verified_builds vb "
                f" WHERE vb.sha256 = r.binary_sha256)").fetchone()["n"]
            targets = db.execute(
                f"SELECT target, COUNT(*) AS n FROM runs r "
                f"WHERE target IS NOT NULL AND {VISIBLE_SQL} "
                f"GROUP BY target ORDER BY n DESC").fetchall()
            # The operating systems this hub actually holds, for the OS picker:
            # there is no fixed list to draw it from, since uname says whatever
            # the machine it ran on says.
            systems = db.execute(
                f"SELECT sysname, COUNT(*) AS n FROM runs r "
                f"WHERE sysname IS NOT NULL AND {VISIBLE_SQL} "
                f"GROUP BY sysname ORDER BY n DESC").fetchall()
            # Every CPU this hub can compare, with its core counts, for the CPU
            # picker: one entry per peer set, as group=cpu draws them. Grouped
            # by the expression rather than its alias, which SQLite would read
            # as the reported column alone and so fold guessed layouts together.
            cpus = db.execute(
                f"SELECT r.cpu_key, {LAYOUT_SQL} AS cpu_layout, COUNT(*) AS n "
                f"FROM runs r WHERE {COMPARABLE_SQL} "
                f"GROUP BY r.cpu_key, {LAYOUT_SQL} ORDER BY r.cpu_key").fetchall()
            # The newest release this hub knows of, for the page to point at
            # when it explains what a release build is. By version and not by
            # when the manifest happened to be loaded: an operator catching up
            # on old releases must not leave the page recommending one of them.
            latest = db.execute(
                "SELECT release, release_url FROM verified_builds "
                "ORDER BY release_rank DESC, added_at DESC LIMIT 1").fetchone()
        release = None
        if latest and latest["release"]:
            release = {"release": latest["release"], "url": latest["release_url"]}
        return {"runs": runs, "records": cores, "users": users,
                "release_builds": release_builds, "release": release,
                "targets": [dict(t) for t in targets],
                "systems": [dict(t) for t in systems],
                "cpus": [dict(c) for c in cpus]}

    # -- verified builds ---------------------------------------------------

    def add_verified_builds(self, manifest: object) -> tuple[int, str]:
        """Load a release manifest. Returns (builds stored, release tag).

        Re-loading a release replaces what it published rather than adding to
        it, so a corrected manifest is a re-run of the same command and not a
        database to clean up by hand. Other releases are untouched: a hub keeps
        verifying the runs of every version it has been told about.
        """
        release, url, builds = parse_builds_manifest(manifest)
        now = iso_now()
        rank = release_rank(release)
        rows = [(b["sha256"], release, rank, url, b["filename"],
                 b["target"], b["march"], now) for b in builds]
        with self.connect() as db:
            db.execute("DELETE FROM verified_builds WHERE release = ?", (release,))
            db.executemany(
                "INSERT OR REPLACE INTO verified_builds "
                "(sha256, release, release_rank, release_url, filename, target, "
                " march, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        return len(rows), release

    def verified_builds(self) -> list[dict]:
        """Every (binary, release) pair this hub holds, newest release first.

        A digest published unchanged by several releases appears once per
        release: that is the table's shape, and flattening it here would hide
        exactly the fact a reader of this list is checking -- which versions a
        given binary is current in.
        """
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM verified_builds "
                "ORDER BY release_rank DESC, added_at DESC, filename")]

    def forget_release(self, release: str) -> int:
        with self.connect() as db:
            cur = db.execute("DELETE FROM verified_builds WHERE release = ?",
                             (release,))
        return cur.rowcount

    # -- accounts ----------------------------------------------------------

    def create_user(self, name: str, password: str) -> dict:
        row = {"name": name, "password_hash": hash_password(password),
               "api_token": secrets.token_urlsafe(24), "created_at": iso_now()}
        try:
            with self.connect() as db:
                cur = db.execute(
                    "INSERT INTO users (name, password_hash, api_token, created_at) "
                    "VALUES (:name, :password_hash, :api_token, :created_at)", row)
        except sqlite3.IntegrityError:
            # The unique index over lower(name) is the only constraint an
            # ordinary registration can trip.
            raise Denied("that name is taken", HTTPStatus.CONFLICT)
        assert cur.lastrowid is not None
        return dict(row, id=int(cur.lastrowid))

    def user_by_name(self, name: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM users WHERE lower(name) = lower(?)",
                             (name,)).fetchone()
        return dict(row) if row else None

    def user_by_id(self, user_id: int) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def user_by_api_token(self, token: str) -> dict | None:
        """The account an `Authorization: Bearer` upload belongs to.

        Looked up by equality: the token is a 192-bit random string, so there
        is no prefix of it worth learning from how long the lookup took.
        """
        if not token:
            return None
        with self.connect() as db:
            row = db.execute("SELECT * FROM users WHERE api_token = ?",
                             (token,)).fetchone()
        return dict(row) if row else None

    def rename_user(self, user_id: int, name: str) -> dict:
        """Give an account a new name, once a week at most.

        The clash check and the write share one transaction, so two accounts
        reaching for one name cannot both pass it; the unique index over
        lower(name) is still what has the last word. Changing only the case of
        one's own name is a rename like any other -- it is not a clash with
        oneself -- and counts against the week.
        """
        try:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                user = db.execute("SELECT * FROM users WHERE id = ?",
                                  (user_id,)).fetchone()
                if user is None:
                    raise Denied("sign in first", HTTPStatus.UNAUTHORIZED)
                user = dict(user)
                if user["name"] == name:
                    raise Invalid("that is already your name")
                wait = rename_after(user)
                if wait is not None:
                    raise Denied(f"a name can be changed once every {RENAME_DAYS} "
                                 f"days; this one can be changed again after {wait}",
                                 HTTPStatus.TOO_MANY_REQUESTS)
                clash = db.execute("SELECT id FROM users WHERE lower(name) = lower(?) "
                                   "AND id != ?", (name, user_id)).fetchone()
                if clash is not None:
                    raise Denied("that name is taken", HTTPStatus.CONFLICT)
                user["name"], user["renamed_at"] = name, iso_now()
                db.execute("UPDATE users SET name = ?, renamed_at = ? WHERE id = ?",
                           (user["name"], user["renamed_at"], user_id))
        except sqlite3.IntegrityError:
            raise Denied("that name is taken", HTTPStatus.CONFLICT)
        return user

    def set_password(self, user_id: int, password: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                       (hash_password(password), user_id))

    def rotate_api_token(self, user_id: int) -> str:
        token = secrets.token_urlsafe(24)
        with self.connect() as db:
            db.execute("UPDATE users SET api_token = ? WHERE id = ?",
                       (token, user_id))
        return token

    def close_account(self, user_id: int) -> int:
        """Delete an account and everything uploaded under it.

        The runs go too, rather than being orphaned: an anonymous run nobody
        holds the delete token for is a row that can never be withdrawn, and
        leaving those behind is not what closing an account is understood to
        mean. Returns how many runs went with it.
        """
        with self.connect() as db:
            ids = [r["id"] for r in db.execute(
                "SELECT id FROM runs WHERE user_id = ?", (user_id,))]
            if ids:
                marks = ",".join("?" * len(ids))
                db.execute(f"DELETE FROM cores WHERE run_id IN ({marks})", ids)
                db.execute(f"DELETE FROM runs WHERE id IN ({marks})", ids)
            db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return len(ids)

    def run_count(self, user_id: int, include_hidden: bool = False) -> int:
        """How many runs an account has: what its profile says to everyone,
        or, with the hidden ones too, what its owner is told."""
        return self.run_total(user_id, include_hidden)

    def run_total(self, user_id: int | None = None,
                  include_hidden: bool = False) -> int:
        """How many runs a listing of them has in all: the whole hub, or one
        account's, matching whatever `runs()` was narrowed to."""
        where, args = self._runs_where(user_id, include_hidden)
        with self.connect() as db:
            return db.execute(
                f"SELECT COUNT(*) AS n FROM runs r {where}", args).fetchone()["n"]

    # -- sessions ----------------------------------------------------------

    def create_session(self, user_id: int) -> tuple[str, int]:
        """A new session for one browser: (cookie value, lifetime in seconds)."""
        token = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
        with self.connect() as db:
            # Cheap enough to do on every sign-in, and it means an abandoned
            # browser's row does not outlive the session it stands for.
            db.execute("DELETE FROM sessions WHERE expires_at < ?", (iso_now(),))
            db.execute("INSERT INTO sessions (token_hash, user_id, created_at, "
                       "expires_at) VALUES (?, ?, ?, ?)",
                       (token_hash(token), user_id, iso_now(),
                        expires.isoformat(timespec="seconds").replace("+00:00", "Z")))
        return token, SESSION_DAYS * 86400

    def user_by_session(self, token: str) -> dict | None:
        if not token:
            return None
        with self.connect() as db:
            row = db.execute(
                "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
                "WHERE s.token_hash = ? AND s.expires_at > ?",
                (token_hash(token), iso_now())).fetchone()
        return dict(row) if row else None

    def end_session(self, token: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash = ?",
                       (token_hash(token),))

    def end_all_sessions(self, user_id: int) -> None:
        """Sign every browser out -- what a password change has to mean."""
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class RateLimiter:
    """Hits per key -- a client's address, mostly -- over a sliding window.

    A key with no hit inside the window is forgotten within one more window:
    the privacy policy (PRIVACY.md) promises that an address is held for the limit's sake no
    longer than that, and that it is never written anywhere.
    """

    def __init__(self, limit: int, window: float):
        self.limit = limit
        self.window = window
        self.hits: dict[str, deque[float]] = defaultdict(deque)
        self.lock = threading.Lock()
        self.swept = time.monotonic()

    def sweep(self, now: float) -> None:
        """Forget every key whose last hit has left the window."""
        for k in [k for k, q in self.hits.items() if not q or now - q[-1] > self.window]:
            del self.hits[k]
        self.swept = now

    def allow(self, key: str, limit: int | None = None, cost: int = 1) -> bool:
        """Whether `key` may act now. `limit` overrides the default for one
        class of caller -- a signed-in account is a known submitter and gets
        more room than a bare address. `cost` is how many hits the act counts
        as -- a batch is as many as the results it stores -- and an act that
        would go past the limit is refused whole, counting nothing."""
        limit = self.limit if limit is None else limit
        if self.limit <= 0:                 # 0 disables the limiter outright
            return True
        now = time.monotonic()
        with self.lock:
            if now - self.swept > self.window:
                self.sweep(now)
            q = self.hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) + cost > limit:
                return False
            q.extend([now] * cost)
            if len(self.hits) > MAX_KEYS:
                self.shed()
            return True

    def shed(self) -> None:
        """Forget the half of the keys heard from least recently.

        A hard bound on memory, for a flood from more addresses than the
        sweep can keep up with -- an IPv6 network holds billions. Whoever is
        forgotten starts afresh, which is the price of the bound; the keys
        that go are the ones furthest from their limit in time.
        """
        by_age = sorted(self.hits, key=lambda k: self.hits[k][-1] if self.hits[k] else 0.0)
        for k in by_age[:len(by_age) // 2]:
            del self.hits[k]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

# What log_message() spells out or blanks in a request line.
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
TOKEN_IN_QUERY_RE = re.compile(r"([?&]token=)[^&\s\"]*")


# Cloudflare's edge, as https://www.cloudflare.com/ips/ lists it (checked
# 2026-10-30). Only consulted behind --trust-proxy, for the peer the proxy saw.
CLOUDFLARE_NETS = tuple(ipaddress.ip_network(n) for n in (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
    "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32",
    "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32"))


def is_address(s: str) -> bool:
    try:
        ipaddress.ip_address(s)
    except ValueError:
        return False
    return True


def from_cloudflare(addr: str) -> bool:
    """Whether an address is one of Cloudflare's edges."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in net for net in CLOUDFLARE_NETS)


def address_group(addr: str) -> str:
    """An address as a rate limit counts it: IPv4 whole, IPv6 by its /64."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return addr
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        return str(ipaddress.IPv6Network((ip, 64), strict=False))
    return str(ip)


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'; object-src 'none'"),
}

# Eighteen digits is every id a column can hold; a longer one names nothing.
ID_RE = re.compile(r"^/api/runs/(\d{1,18})(/raw|/rank)?$")
USER_RE = re.compile(r"^/api/users/([A-Za-z0-9._-]{3,32})$")


class Handler(BaseHTTPRequestHandler):
    server_version = "cpcpub-hub/1.0"
    protocol_version = "HTTP/1.1"       # every response below sets Content-Length
    timeout = HANDLER_TIMEOUT

    # -- plumbing ----------------------------------------------------------
    @property
    def hub(self) -> Server:
        """`self.server`, which is always the Server below.

        `BaseRequestHandler` declares that attribute as the base class, and it
        is not ours to redeclare, so the narrowing happens here once instead of
        at each of the half-dozen places that want what Server carries.
        """
        return cast("Server", self.server)

    @property
    def store(self) -> Store:
        return self.hub.store

    @property
    def limiter(self) -> RateLimiter:
        return self.hub.limiter

    @property
    def auth_limiter(self) -> RateLimiter:
        return self.hub.auth_limiter

    @property
    def register_limiter(self) -> RateLimiter:
        return self.hub.register_limiter

    @property
    def batch_limiter(self) -> RateLimiter:
        return self.hub.batch_limiter

    def client_key(self) -> str:
        """The client's address: the peer's, or behind a trusted proxy the one
        it reports.

        The last entry of X-Forwarded-For, not the first. A proxy appends the
        address it saw to whatever the client already sent, so everything
        before the last entry is the client's own say -- and a first entry
        believed would let anyone pick a new address per request and walk past
        every limit keyed on it.
        """
        if self.hub.trust_proxy:
            fwd = self.headers.get("X-Forwarded-For", "")
            if fwd.strip():
                peer = fwd.split(",")[-1].strip()[:64]
                # A Cloudflare edge is a proxy too, and names the visitor in a
                # header of its own -- believed only from Cloudflare's ranges,
                # where the edge overwrites whatever the visitor sent.
                if from_cloudflare(peer):
                    visitor = self.headers.get("CF-Connecting-IP", "").strip()
                    if is_address(visitor):
                        return visitor
                return peer
        return self.client_address[0]

    def limit_key(self) -> str:
        """Who a per-address limit counts this request against.

        An IPv6 address is counted by its /64: that is what one subscriber is
        handed, and any host on it can take a fresh address per request.
        """
        return address_group(self.client_key())

    def send_bytes(self, status: int, body: bytes, ctype: str, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        # A list rather than the dict `extra` is, because a response may carry
        # two Set-Cookie headers and a dict holds one.
        for k, v in getattr(self, "extra_headers", []):
            self.send_header(k, v)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status: int, obj: object, extra=None):
        body = json.dumps(obj, allow_nan=False).encode()
        self.send_bytes(status, body, "application/json; charset=utf-8", extra)

    def fail(self, status: int, message: str):
        self.send_json(status, {"error": message})

    # `format` shadows the builtin, and is the name BaseHTTPRequestHandler gives
    # this parameter -- an override that renames it is one a caller passing it
    # by keyword would not survive.
    def log_message(self, format, *args):   # one tidy line per request
        # `format % args` is this handler's own contract with http.server and
        # stays as it is; only the line around it is ours to write. The
        # request line is the client's own text: control characters in it are
        # spelled out rather than sent to a terminal, and a delete token in a
        # query string is a credential, not something to keep in a log.
        line = TOKEN_IN_QUERY_RE.sub(r"\1[redacted]",
                                     f"{self.client_key()} - {format % args}")
        sys.stderr.write(CONTROL_RE.sub(lambda m: f"\\x{ord(m.group()):02x}", line) + "\n")

    # -- who is asking -----------------------------------------------------
    #
    # Two ways in, deliberately different. A browser holds a session cookie the
    # page cannot read; a machine that ran the benchmark holds an upload token
    # it sends in a header. The difference matters for CSRF: a cookie rides
    # along on a cross-site request without the other site having to know it
    # exists, so cookie-authenticated writes carry a token the page had to read
    # to send. A header the other site would have to set itself needs no such
    # proof, and neither do anonymous requests, which claim nothing.

    def cookies(self) -> dict[str, str]:
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except CookieError:
            return {}
        return {k: m.value for k, m in jar.items()}

    def identify(self) -> tuple[dict | None, str | None]:
        """(account, how) for this request; (None, None) when anonymous.

        An upload token that names no account is an error rather than an
        anonymous request: a stale token in a submit script would otherwise
        keep uploading successfully, off the account it was meant for, with
        nothing to notice but the runs never appearing under the name. An
        expired session cookie is the opposite -- a browser is entitled to
        carry a dead one, and reading the board with it is not a mistake.
        """
        if not hasattr(self, "_who"):
            self._who: tuple[dict | None, str | None] = (None, None)
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                user = self.store.user_by_api_token(auth[7:].strip()[:128])
                if user is None:
                    raise Denied("unknown upload token; the account page has "
                                 "the current one", HTTPStatus.UNAUTHORIZED)
                self._who = (user, "token")
            elif self.cookie_name(SESSION_COOKIE) in self.cookies():
                user = self.store.user_by_session(
                    self.cookies()[self.cookie_name(SESSION_COOKIE)])
                if user:
                    self._who = (user, "cookie")
        return self._who

    def require_account(self) -> dict:
        user, how = self.identify()
        if user is None:
            raise Denied("sign in first", HTTPStatus.UNAUTHORIZED)
        if how == "cookie":
            self.check_csrf()
        return user

    def check_csrf(self) -> None:
        sent = self.headers.get(CSRF_HEADER, "")
        held = self.cookies().get(self.cookie_name(CSRF_COOKIE), "")
        if not sent or not held or not same_secret(sent, held):
            raise Denied("stale page: reload and try again")

    def is_secure(self) -> bool:
        """Whether the browser reached us over TLS, so `Secure` can be set.

        Only believed behind a proxy we were told to trust: an unproxied server
        that took X-Forwarded-Proto from the request would let any client
        decide the cookie flags.
        """
        if self.hub.trust_proxy:
            return self.headers.get("X-Forwarded-Proto", "").lower() == "https"
        return False

    def cookie_name(self, name: str) -> str:
        """A cookie's name as this request carries it.

        `__Host-` over TLS: a browser takes such a cookie only from this very
        host, never from a sibling that sets one for the parent domain. That
        matters on a host under a shared domain nobody listed as public, where
        any other name under it could otherwise plant a session or a CSRF
        token here. Plain http cannot have the prefix, so it keeps the bare
        name.
        """
        return "__Host-" + name if self.is_secure() else name

    def set_cookie(self, name: str, value: str, max_age: int, *,
                   http_only: bool = True) -> None:
        name = self.cookie_name(name)
        parts = [f"{name}={value}", "Path=/", f"Max-Age={max_age}", "SameSite=Lax"]
        if http_only:
            parts.append("HttpOnly")
        if self.is_secure():
            parts.append("Secure")
        self.extra_headers.append(("Set-Cookie", "; ".join(parts)))

    def start_session(self, user: dict) -> str:
        """Sign this browser in and hand the page its CSRF token."""
        token, ttl = self.store.create_session(user["id"])
        csrf = secrets.token_urlsafe(24)
        self.set_cookie(SESSION_COOKIE, token, ttl)
        self.set_cookie(CSRF_COOKIE, csrf, ttl, http_only=False)
        return csrf

    def end_session(self) -> None:
        token = self.cookies().get(self.cookie_name(SESSION_COOKIE))
        if token:
            self.store.end_session(token)
        self.set_cookie(SESSION_COOKIE, "", 0)
        self.set_cookie(CSRF_COOKIE, "", 0, http_only=False)

    def read_json(self, cap: int) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise Invalid("bad Content-Length")
        if length <= 0:
            raise Invalid("empty body")
        # A form on any other site can POST text/plain that parses as JSON,
        # and would sign its visitor in or up to an account of the form's
        # choosing. application/json is a type no page can send to another
        # origin without the browser asking first, and nothing here says yes.
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise HttpError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                            "send the body as Content-Type: application/json")
        if length > cap:
            self.drain(length)
            self.body_read = True
            raise HttpError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                            f"body larger than {cap} bytes")
        self.body_read = True
        payload = parse_json(self.rfile.read(length))
        if not isinstance(payload, dict):
            raise Invalid("body must be a JSON object")
        return payload

    # -- routing -----------------------------------------------------------
    def do_GET(self):
        self.route("GET")

    def do_HEAD(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_DELETE(self):
        self.route("DELETE")

    def do_PATCH(self):
        self.route("PATCH")

    def route(self, method: str):
        # Per request: this handler instance serves every request on a
        # keep-alive connection, so the response headers cannot accumulate.
        self.extra_headers: list[tuple[str, str]] = []
        self.body_read = False
        self.__dict__.pop("_who", None)
        try:
            url = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(url.path)
            query = urllib.parse.parse_qs(url.query)

            if path.startswith("/api/"):
                self.api(method, path, query)
            elif method == "GET" and path.startswith("/downloads/"):
                self.download(path)
            elif method == "GET" and path in ("/download", "/downloads"):
                # A short address to give out for the Download tab.
                self.redirect("/#download")
            elif method == "GET":
                self.static(path)
            else:
                self.fail(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")
        except HttpError as e:
            # A refusal decided before the body was read -- a failed CSRF check,
            # say -- would otherwise leave those bytes in front of the next
            # request on a keep-alive connection, which then reads a JSON
            # document where a request line should be.
            self.drain_unread()
            self.send_json(e.status, {"error": str(e), **e.extra})
        except BrokenPipeError:
            pass
        except Exception as e:                      # noqa: BLE001
            # Deliberately everything: an exception let out of a handler prints
            # a traceback where the response should be and takes the connection
            # with it. One line in the log, one plain 500 to the client.
            self.log_message("unhandled error: %r", e)
            try:
                self.fail(HTTPStatus.INTERNAL_SERVER_ERROR, "internal error")
            except Exception as unreportable:       # noqa: BLE001
                # The 500 could not be sent either: the socket has gone, or the
                # response had already started. Say so in the log rather than
                # swallowing it -- this is the one place a bug here would hide.
                self.log_message("could not report it: %r", unreportable)

    def api(self, method: str, path: str, query: dict):
        if path.startswith("/api/auth/"):
            return self.auth(method, path[len("/api/auth/"):])
        if path == "/api/metrics" and method == "GET":
            return self.send_json(HTTPStatus.OK, {"metrics": METRICS,
                                                  "scopes": sorted(SCOPES)})
        if path == "/api/stats" and method == "GET":
            # What a batch may hold here, for the page to say before anyone
            # prepares one.
            return self.send_json(HTTPStatus.OK, dict(
                self.store.stats(),
                batch={"max_results": MAX_BATCH,
                       "per_hour": self.batch_limiter.limit}))
        if path == "/api/downloads" and method == "GET":
            manifest = self.hub.downloads.manifest()
            body = {"release": None}
            if manifest:
                body = dict(layout(manifest), downloads=self.store.download_counts(
                    manifest["tag"]))
            return self.send_json(HTTPStatus.OK, body,
                                  {"Cache-Control": "no-cache"})
        if path == "/api/builds" and method == "GET":
            # Public on purpose: what a hub calls verified is a claim about
            # which binaries it trusts, and a claim nobody can read is not one
            # anybody should believe. Every digest here is also in the release
            # the manifest names, so the two can be checked against each other.
            return self.send_json(HTTPStatus.OK,
                                  {"builds": self.store.verified_builds()})
        if path == "/api/cores" and method == "GET":
            return self.send_json(HTTPStatus.OK, self.query_cores(query))
        if path == "/api/runs":
            if method == "GET":
                limit = clamp_int(query.get("limit", ["50"])[0], 1, MAX_PAGE, 50)
                offset = clamp_int(query.get("offset", ["0"])[0], 0, MAX_OFFSET, 0)
                who = self.who_filter(query)
                mine = self.is_asker(who)
                runs = self.store.runs(limit, offset, who, include_hidden=mine)
                return self.send_json(HTTPStatus.OK, {
                    "runs": runs, "limit": limit, "offset": offset,
                    "total": self.total(len(runs), limit, offset,
                                        lambda: self.store.run_total(who, mine))})
            if method == "POST":
                return self.upload()
            return self.fail(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")
        if path == "/api/batch":
            if method == "POST":
                return self.batch(query)
            return self.fail(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")
        if path == "/api/uploads" and method == "GET":
            return self.send_json(HTTPStatus.OK, self.list_uploads(query))

        m = USER_RE.match(path)
        if m and method == "GET":
            user = self.store.user_by_name(m.group(1))
            if user is None:
                return self.fail(HTTPStatus.NOT_FOUND, "no such account")
            return self.send_json(HTTPStatus.OK, {"user": public_user(
                user, runs=self.store.run_count(user["id"]))})

        m = ID_RE.match(path)
        if m:
            run_id, suffix = int(m.group(1)), m.group(2)
            if method == "GET" and suffix == "/raw":
                raw = self.store.raw(run_id)
                if raw is None:
                    return self.fail(HTTPStatus.NOT_FOUND, "no such run")
                return self.send_bytes(HTTPStatus.OK, raw.encode(),
                                       "application/json; charset=utf-8")
            if method == "GET" and suffix == "/rank":
                return self.send_json(HTTPStatus.OK, self.store.rank(run_id))
            if method == "GET" and suffix is None:
                run = self.store.run(run_id)
                if run is None:
                    return self.fail(HTTPStatus.NOT_FOUND, "no such run")
                return self.send_json(HTTPStatus.OK, run)
            if method == "DELETE" and suffix is None:
                return self.withdraw(run_id, query)
            if method == "PATCH" and suffix is None:
                return self.edit_run(run_id)
        return self.fail(HTTPStatus.NOT_FOUND, "no such endpoint")

    def list_uploads(self, query: dict) -> dict:
        """One account's runs grouped by CPU and upload, a page of CPUs at a
        time -- paging by run would cut a CPU, or an upload, in half."""
        who = self.who_filter(query)
        if who is None:
            raise Invalid("say whose uploads: user=<name>, or user=me")
        limit = clamp_int(query.get("limit", ["50"])[0], 1, MAX_PAGE, 50)
        offset = clamp_int(query.get("offset", ["0"])[0], 0, MAX_OFFSET, 0)
        cpus = (self.store.uploads(who, include_hidden=self.is_asker(who))
                if who >= 0 else [])
        return {"cpus": cpus[offset:offset + limit], "limit": limit,
                "offset": offset, "total": len(cpus),
                "runs": sum(c["runs"] for c in cpus),
                "uploads": sum(len(c["uploads"]) for c in cpus)}

    def edit_run(self, run_id: int):
        """Change a run's label or notes, for whoever could withdraw it.

        The body names only what changes: `{"label": "..."}` leaves the notes
        alone, and null or "" clears a field. The variant tag a --variants
        upload ends its labels with is the caller's to keep: the page puts it
        back on each run when it renames a whole upload.
        """
        body = self.read_json(MAX_EDIT_BODY)
        user, how = self.identify()
        if user is not None and how == "cookie":
            self.check_csrf()
        token = self.headers.get("X-Delete-Token")
        if not token and user is None:
            raise Denied("sign in, or send the run's delete token",
                         HTTPStatus.UNAUTHORIZED)
        changes: dict[str, object] = {}
        if "label" in body:
            changes["label"] = _text(body["label"], MAX_TEXT, "label")
        if "notes" in body:
            changes["notes"] = _text(body["notes"], MAX_NOTES, "notes")
        if "hidden" in body:
            if not isinstance(body["hidden"], bool):
                raise Invalid("hidden must be true or false")
            changes["hidden"] = body["hidden"]
        if not changes:
            raise Invalid("nothing to change: send label, notes or hidden")
        out = self.store.edit_run(run_id, changes, token or None,
                                  user["id"] if user else None)
        if out is None:
            raise Denied("no such run, or it is not yours to edit")
        return self.send_json(HTTPStatus.OK, out)

    def withdraw(self, run_id: int, query: dict):
        """Delete a run for its owner, or for whoever holds its delete token."""
        user, how = self.identify()
        if user is not None and how == "cookie":
            self.check_csrf()
        token = (self.headers.get("X-Delete-Token")
                 or query.get("token", [""])[0])
        if not token and user is None:
            return self.fail(HTTPStatus.UNAUTHORIZED,
                             "sign in, or send the run's delete token")
        if not self.store.delete(run_id, token or None,
                                 user["id"] if user else None):
            return self.fail(HTTPStatus.FORBIDDEN,
                             "no such run, or it is not yours to withdraw")
        return self.send_json(HTTPStatus.OK, {"deleted": run_id})

    def is_asker(self, user_id: int | None) -> bool:
        """Whether a listing narrowed to `user_id` is the asker's own -- the
        one listing that also shows the runs its owner has hidden."""
        if user_id is None:
            return False
        user, _ = self.identify()
        return user is not None and int(user["id"]) == user_id

    def who_filter(self, query: dict) -> int | None:
        """The account a listing is narrowed to, from `user=name` or `user=me`.

        An unknown name filters to nothing rather than to everything: a board
        that quietly widened when a name was misspelt would read as that
        person having uploaded the lot.
        """
        name = _text(query.get("user", [None])[0], 32, "user")
        if not name:
            return None
        if name == "me":
            user, _ = self.identify()
            if user is None:
                raise Denied("sign in first", HTTPStatus.UNAUTHORIZED)
            return int(user["id"])
        found = self.store.user_by_name(name)
        return int(found["id"]) if found else -1

    # -- accounts ----------------------------------------------------------

    def register_policy(self) -> dict:
        """What this hub asks of a new account. Costs nothing to answer."""
        return {"open": self.hub.register_open,
                "invite_required": bool(self.hub.signup_token),
                "bits": self.hub.register_pow}

    def auth(self, method: str, action: str):
        if action == "policy" and method == "GET":
            # Deliberately separate from the puzzle below, and deliberately
            # unlimited. The page reads this whenever it draws the sign-in form,
            # which is once per visitor who never registers at all: issuing a
            # challenge there would spend a store slot and a rate-limit slot per
            # page view, and a shared address could then exhaust the very limit
            # that registering needs. It is three booleans about the hub -- the
            # same class of answer as /api/stats.
            return self.send_json(HTTPStatus.OK, self.register_policy())

        if action == "challenge" and method == "GET":
            # A puzzle to register with, plus the policy again so a client that
            # wants both still needs only one request.
            #
            # Limited on its own key, and loosely: what the limit is for is a
            # script filling the challenge store, and for that a generous
            # ceiling is still a ceiling.
            if not self.auth_limiter.allow("chal:" + self.limit_key(),
                                           self.auth_limiter.limit * 5):
                raise Denied("too many requests from this address; try later",
                             HTTPStatus.TOO_MANY_REQUESTS)
            out = self.register_policy()
            if self.hub.register_open and self.hub.register_pow > 0:
                out["challenge"] = self.hub.challenges.issue()
            return self.send_json(HTTPStatus.OK, out)

        if action == "me" and method == "GET":
            user, how = self.identify()
            if user is None:
                return self.send_json(HTTPStatus.OK, {"user": None})
            # A signed-in page that has lost its CSRF cookie (it expires with
            # the session, but a browser may drop it alone) can do nothing but
            # read. Reissuing here costs one header and keeps a returning tab
            # working without a sign-in it does not need.
            if how == "cookie" and self.cookie_name(CSRF_COOKIE) not in self.cookies():
                csrf = secrets.token_urlsafe(24)
                self.set_cookie(CSRF_COOKIE, csrf, SESSION_DAYS * 86400,
                                http_only=False)
            return self.send_json(HTTPStatus.OK, {
                "user": public_user(user, runs=self.store.run_count(
                                        user["id"], include_hidden=True),
                                    token=(how == "cookie"))})

        if method != "POST":
            return self.fail(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")

        if action == "register":
            return self.register()
        if action == "login":
            return self.login()
        if action == "logout":
            # Deliberately not CSRF-guarded and deliberately always 200: being
            # signed out against your will is a nuisance, not a compromise, and
            # a logout that can fail leaves a page that cannot get rid of a
            # session it no longer wants.
            self.end_session()
            return self.send_json(HTTPStatus.OK, {"user": None})
        if action == "token":
            user = self.require_account()
            return self.send_json(HTTPStatus.OK,
                                  {"api_token": self.store.rotate_api_token(user["id"])})
        if action == "adopt":
            return self.adopt()
        if action == "password":
            return self.change_password()
        if action == "rename":
            return self.rename_account()
        if action == "close":
            return self.close_account()
        return self.fail(HTTPStatus.NOT_FOUND, "no such endpoint")

    def auth_allowed(self) -> None:
        """Rate limit on guessing, per address rather than per name.

        Per name would let anyone lock an account out by guessing at it.
        """
        if not self.auth_limiter.allow("auth:" + self.limit_key()):
            raise Denied("too many attempts from this address; try later",
                         HTTPStatus.TOO_MANY_REQUESTS)

    def register(self):
        """Create an account, for a client that can show it is not a script.

        Three gates, cheapest first: is this hub taking accounts at all, is the
        name and password even usable, and has the caller done the work. The
        per-address limit is checked last, after the work has been shown, so a
        flood of unsolved attempts cannot use up an honest neighbour's quota.
        """
        if not self.hub.register_open:
            raise Denied("this hub is not taking new accounts; uploads still "
                         "work without one")
        self.auth_allowed()
        body = self.read_json(MAX_AUTH_BODY)
        name = check_name(body.get("name"))
        password = check_password(body.get("password"))

        if self.hub.signup_token:
            given = body.get("invite")
            if not isinstance(given, str) or not same_secret(
                    given, self.hub.signup_token):
                raise Denied("this hub needs an invite code to register")

        # Before the work is checked rather than after it. A name already taken
        # is someone who forgot they had an account, or a name two people both
        # wanted -- neither is an attempt at abuse, and neither should cost a
        # solved puzzle and another wait. create_user's unique index is still
        # what decides it: two registrations racing for one name is exactly what
        # that index is there for, and this check cannot see the other one.
        if self.store.user_by_name(name) is not None:
            raise Denied("that name is taken", HTTPStatus.CONFLICT)

        bits = self.hub.register_pow
        if bits > 0:
            challenge = body.get("challenge")
            nonce = body.get("nonce")
            if not isinstance(challenge, str) or not isinstance(nonce, str) \
                    or len(nonce) > MAX_NONCE:
                raise Invalid("registering needs a solved challenge from "
                              "/api/auth/challenge")
            # Spent whether or not it turns out to be solved: a challenge that
            # survived a wrong answer would be a free retry, which is exactly
            # what the work is supposed to cost.
            if not self.hub.challenges.spend(challenge):
                raise Denied("that challenge is unknown or has expired; ask "
                             "for another")
            if not pow_solved(challenge, nonce, bits):
                raise Denied("the challenge was not solved")

        if not self.register_limiter.allow("reg:" + self.limit_key()):
            raise Denied("too many accounts from this address; try later",
                         HTTPStatus.TOO_MANY_REQUESTS)

        user = self.store.create_user(name, password)
        csrf = self.start_session(user)
        self.send_json(HTTPStatus.CREATED,
                       {"user": public_user(user, runs=0, token=True),
                        "csrf": csrf})

    def login(self):
        self.auth_allowed()
        body = self.read_json(MAX_AUTH_BODY)
        name = _text(body.get("name"), 32, "name") or ""
        password = body.get("password")
        user = self.store.user_by_name(name) if name else None
        if user is None or not isinstance(password, str) \
                or not password_matches(password, user["password_hash"]):
            # One message for both halves: which of the two was wrong is not
            # something a sign-in page should be willing to say.
            raise Denied("wrong name or password", HTTPStatus.UNAUTHORIZED)
        csrf = self.start_session(user)
        self.send_json(HTTPStatus.OK, {
            "user": public_user(user, runs=self.store.run_count(user["id"], include_hidden=True),
                                token=True),
            "csrf": csrf})

    def adopt(self):
        """Move anonymous runs onto the signed-in account by their delete
        tokens: `{"tokens": [...]}`, as many as MAX_ADOPT at once.

        A request with a token nobody holds still adopts the rest. The reply
        says how many matched nothing this account could take, but not which:
        a token that names another account's run is not something to confirm
        to whoever sent it.
        """
        user = self.require_account()
        body = self.read_json(MAX_ADOPT_BODY)
        tokens = body.get("tokens")
        if not isinstance(tokens, list) or not tokens:
            raise Invalid("send tokens: a list of the runs' delete tokens")
        if len(tokens) > MAX_ADOPT:
            raise Invalid(f"at most {MAX_ADOPT} tokens at once")
        if not all(isinstance(t, str) and 0 < len(t) <= 64 for t in tokens):
            raise Invalid("each token must be a delete token, as the upload "
                          "reply gave it")
        out = self.store.adopt(user["id"], [t.strip() for t in tokens])
        self.send_json(HTTPStatus.OK, out)

    def change_password(self):
        self.auth_allowed()
        user = self.require_account()
        body = self.read_json(MAX_AUTH_BODY)
        if not password_matches(str(body.get("current", "")), user["password_hash"]):
            raise Denied("the current password is wrong", HTTPStatus.UNAUTHORIZED)
        new = check_password(body.get("password"))
        self.store.set_password(user["id"], new)
        # Every other browser goes with it -- a password change is what you do
        # when you think someone else has one, so leaving them signed in would
        # undo the point of it. This one is signed straight back in.
        self.store.end_all_sessions(user["id"])
        csrf = self.start_session(user)
        self.send_json(HTTPStatus.OK, {"ok": True, "csrf": csrf})

    def rename_account(self):
        self.auth_allowed()
        user = self.require_account()
        body = self.read_json(MAX_AUTH_BODY)
        if not password_matches(str(body.get("password", "")), user["password_hash"]):
            raise Denied("wrong password", HTTPStatus.UNAUTHORIZED)
        renamed = self.store.rename_user(user["id"], check_name(body.get("name")))
        self.send_json(HTTPStatus.OK, {
            "user": public_user(renamed, runs=self.store.run_count(user["id"], include_hidden=True),
                                token=True)})

    def close_account(self):
        self.auth_allowed()
        user = self.require_account()
        body = self.read_json(MAX_AUTH_BODY)
        if not password_matches(str(body.get("password", "")), user["password_hash"]):
            raise Denied("wrong password", HTTPStatus.UNAUTHORIZED)
        removed = self.store.close_account(user["id"])
        self.end_session()
        self.send_json(HTTPStatus.OK, {"closed": user["name"], "runs_deleted": removed})

    def query_cores(self, query: dict) -> dict:
        def one(name, default=None):
            v = query.get(name, [default])[0]
            return v if v not in ("", None) else default

        # An explicit id list, or one run's records, addresses records directly
        # -- a shared comparison link, or the expanded view of one board row --
        # so neither is narrowed by the leaderboard's scope default, and neither
        # is collapsed to one row per run.
        raw_ids = one("ids")
        addressed = bool(raw_ids or one("run"))
        scope = one("scope", None if addressed else "cpu")
        if scope is not None and scope not in SCOPES:
            raise Invalid(f"unknown scope {scope!r}")
        group = one("group", "none" if addressed else "run")
        if group not in ("run", "none", "cpu"):
            raise Invalid("group must be 'run', 'none' or 'cpu'")
        if group == "cpu" and scope is None:
            raise Invalid("group=cpu needs a scope")
        run = one("run")
        if run is not None:
            try:
                run = int(run)
            except ValueError:
                raise Invalid("run must be an integer")
            if not 0 <= run <= SQLITE_MAX_INT:
                raise Invalid("no such run")
        sort = one("sort", "score")
        if sort not in METRIC_KEYS and sort != "mhz":
            raise Invalid(f"unknown sort metric {sort!r}")
        order = one("order", "desc")
        if order not in ("asc", "desc"):
            raise Invalid("order must be asc or desc")
        # Which reading of the metric to rank on. The page has the same choice
        # under `Values`, and the two have to agree or it sorts a column by
        # numbers it is not showing.
        norm = one("norm", "abs")
        if norm not in ("abs", "ghz"):
            raise Invalid("norm must be 'abs' or 'ghz'")
        ids = None
        if raw_ids:
            try:
                ids = [int(x) for x in raw_ids.split(",")[:64] if x]
            except ValueError:
                raise Invalid("ids must be a comma-separated list of integers")
            if not all(0 <= i <= SQLITE_MAX_INT for i in ids):
                raise Invalid("ids must be record ids")
        # One value left. `1`, `ci` and `attested` are older spellings from
        # when this hub also checked GitHub signatures; they are kept working
        # as the release filter rather than turned into errors, so a saved link
        # or a script still answers instead of breaking.
        verified = one("verified")
        if verified in ("1", "true", "ci", "attested"):
            verified = "release"
        if verified is not None and verified != "release":
            raise Invalid("verified must be 'release'")

        flag = {"1": 1, "0": 0, "true": 1, "false": 0}
        # Held as one mapping because the rows and their count must be asked
        # the same question; both calls below spread it.
        filters = {
            "scope": scope,
            "user_id": self.who_filter(query),
            "verified": verified,
            "target": _text(one("target"), 32, "target"),
            "vectorize": flag.get(str(one("vectorize", "")).lower()),
            "fma": flag.get(str(one("fma", "")).lower()),
            "sysname": _text(one("os"), 64, "os"),
            "search": _text(one("q"), 64, "q"),
            "ids": ids,
            "run": run,
            "cpu": _text(one("cpu"), MAX_TEXT, "cpu"),
            # Present-but-empty means the runs that reported no layout, which
            # `one` would fold into "not asked".
            "layout": (_text(query["layout"][0], MAX_TEXT, "layout") or ""
                       if "layout" in query else None),
            "threads": None if one("threads") is None
                       else clamp_int(one("threads"), 0, 1 << 20, 0),
        }
        group_run = (group == "run")
        limit = clamp_int(one("limit", "50"), 1, MAX_PAGE, 50)
        offset = clamp_int(one("offset", "0"), 0, MAX_OFFSET, 0)
        if group == "cpu":
            groups, total = self.store.cpu_groups(
                sort=sort, desc=(order == "desc"), norm=norm, limit=limit,
                offset=offset, **filters)
            return {"groups": groups, "limit": limit, "offset": offset,
                    "total": total}
        cores = self.store.cores(group_run=group_run, sort=sort,
                                 desc=(order == "desc"), norm=norm, limit=limit,
                                 offset=offset, **filters)
        if group_run and scope is not None:
            self.store.add_fit(cores, scope, sort, order == "desc", norm)
        # One run's records, as the board's expanded row shows them: each says
        # which kind of core it is, judged from the whole run and not only the
        # records this listing kept.
        if run is not None:
            kind = {i: t["name"] for t in self.store.types_of_run(run) for i in t["ids"]}
            for c in cores:
                c["type"] = kind.get(c["id"])
        return {"cores": cores, "limit": limit, "offset": offset,
                "total": self.total(len(cores), limit, offset,
                                    lambda: self.store.count_cores(
                                        group_run=group_run, **filters))}

    @staticmethod
    def total(shown: int, limit: int, offset: int, count) -> int:
        """How many rows the listing has in all, counted only when it matters.

        A first page that came back short is the whole answer already, and that
        is most requests here -- the expanded view of one run, a comparison
        link, the pick of one record. Only a page that filled up, or one past
        the start, has to ask the database how far the listing really goes.
        """
        if offset == 0 and shown < limit:
            return shown
        return count()

    def drain_unread(self):
        """Swallow a request body nothing got round to reading."""
        if getattr(self, "body_read", True):
            return
        self.body_read = True
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.close_connection = True
            return
        if length > 0:
            self.drain(length)

    def drain(self, length: int):
        """Swallow a body we are about to refuse.

        Without this the client is still writing when the response goes out and
        sees a broken pipe instead of the 413. Bounded, so an enormous upload
        cannot be turned into free bandwidth: past the cap the connection is
        closed rather than drained.
        """
        remaining = min(length, MAX_DRAIN)
        if length > MAX_DRAIN:
            self.close_connection = True
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    def upload(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise Invalid("bad Content-Length")
        if length <= 0:
            raise Invalid("empty body")
        if length > MAX_BODY:
            self.drain(length)
            self.body_read = True
            return self.fail(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                             f"body larger than {MAX_BODY} bytes")
        self.body_read = True
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise Invalid("truncated body")
        user, how = self.identify()
        if user is not None and how == "cookie":
            self.check_csrf()
        # After the body is consumed, so that a refused upload still leaves the
        # connection in a state the next request can use.
        #
        # An account is limited as itself rather than as an address, so a lab
        # behind one NAT is not one submitter, and gets more room because there
        # is something to take away if it is abused. Anonymous uploads keep the
        # per-address limit exactly as it was.
        if user is not None:
            allowed = self.limiter.allow(f"user:{user['id']}",
                                         self.limiter.limit * ACCOUNT_RATE_FACTOR)
            whose = "from this account"
        else:
            allowed = self.limiter.allow(self.limit_key())
            whose = "from this address"
        if not allowed:
            return self.fail(HTTPStatus.TOO_MANY_REQUESTS,
                             f"too many uploads {whose}; try later")

        payload = parse_json(raw)
        # Label and notes may ride along in the query string or inside the
        # document, so both the browser form and a plain curl can set them.
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        label = _text(query.get("label", [None])[0], MAX_TEXT, "label")
        notes = _text(query.get("notes", [None])[0], MAX_NOTES, "notes")
        if isinstance(payload, dict):
            label = label or _text(payload.get("label"), MAX_TEXT, "label")
            notes = notes or _text(payload.get("notes"), MAX_NOTES, "notes")

        run, cores = validate(payload)
        run_id, token = self.store.insert(
            run, cores, raw.decode("utf-8"), label, notes,
            user_id=user["id"] if user else None)
        self.send_json(HTTPStatus.CREATED, {
            "id": run_id,
            "delete_token": token,
            "user": user["name"] if user else None,
            "url": f"/#run={run_id}",
            "records": len(cores),
        })

    def batch(self, query: dict):
        """Many results in one file, for an account: POST /api/batch.

        The body is the file as `cat` made it -- documents and --variants
        arrays one after another -- or, from the page, {"batch": <that text>,
        "labels": [...], "notes": "..."}. Labels name the uploads in file
        order, from the body or as repeated `label=` in the query; a shorter
        list leaves the rest to the label inside their first document, if
        any. `notes` goes on every run. `dry=1` stores nothing and answers
        with the plan: the uploads the file makes and which of its results
        the hub already holds.

        All or nothing: one result the hub would refuse refuses the batch,
        named. Results it already holds are skipped, not refused, so sending
        a file again after adding to it stores only what is new.
        """
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise Invalid("bad Content-Length")
        if length <= 0:
            raise Invalid("empty body")
        if length > MAX_BATCH_BODY:
            self.drain(length)
            self.body_read = True
            return self.fail(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                             f"body larger than {MAX_BATCH_BODY} bytes")
        # Before the body is read, unlike a single upload: a batch is an
        # account's, and sixteen megabytes is too much to take in for anyone
        # who is not one. A refusal drains what it can (see route()).
        user = self.require_account()
        self.body_read = True
        body = self.rfile.read(length)
        if len(body) != length:
            raise Invalid("truncated body")
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            raise Invalid("body must be UTF-8")

        labels = [_text(v, MAX_TEXT, "label") for v in
                  urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query,
                                        keep_blank_values=True).get("label", [])]
        notes = _text(query.get("notes", [None])[0], MAX_NOTES, "notes")
        form = None
        if text.lstrip().startswith("{"):
            try:
                form = json.loads(text, parse_constant=_reject_constant)
            except ValueError:
                form = None             # documents back to back: the plain file
        if isinstance(form, dict) and "schema" not in form and "batch" in form:
            if not isinstance(form["batch"], str):
                raise Invalid("batch must be the file's text")
            text = form["batch"]
            given = form.get("labels") or []
            if not isinstance(given, list):
                raise Invalid("labels must be a list")
            labels = [_text(v, MAX_TEXT, "label") for v in given]
            notes = _text(form.get("notes"), MAX_NOTES, "notes")

        dry = query.get("dry", ["0"])[0] in ("1", "true")
        if dry and not self.limiter.allow(f"dry:{user['id']}",
                                          self.limiter.limit * ACCOUNT_RATE_FACTOR):
            raise Denied("too many batches checked from this account; try later",
                         HTTPStatus.TOO_MANY_REQUESTS)
        uploads = plan_batch(text)
        if len(labels) > len(uploads):
            raise Invalid(f"{len(labels)} labels for {len(uploads)} uploads")
        held = self.store.held([document_digest(r["raw"])
                                for up in uploads for r in up["runs"]])

        for i, up in enumerate(uploads):
            first = up["runs"][0]["payload"]
            own = split_variant(_text(first.get("label"), MAX_TEXT, "label"))[0]
            base = labels[i] if i < len(labels) and labels[i] is not None else own
            many = len(up["runs"]) > 1
            up["label"] = base
            up["notes"] = notes if notes is not None else _text(first.get("notes"),
                                                                MAX_NOTES, "notes")
            for r in up["runs"]:
                tag = variant_name(r["run"])
                r["label"] = (f"{base + ' ' if base else ''}({tag})" if many else base)

        for up in uploads:
            for r in up["runs"]:
                r["held"] = held.get(document_digest(r["raw"]) or "")
        if dry:
            plan = []
            for up in uploads:
                head = up["runs"][0]["run"]
                runs = []
                for r in up["runs"]:
                    entry = {"index": r["index"], "variant": variant_name(r["run"])}
                    if r["held"] is not None:
                        entry["duplicate_of"] = r["held"]
                    runs.append(entry)
                plan.append({"label": up["label"], "runs": runs,
                             **{k: head[k] for k in ("cpu_models", "machine", "sysname",
                                                     "mode", "threads")}})
            return self.send_json(HTTPStatus.OK, {
                "uploads": plan, "results": sum(len(up["runs"]) for up in uploads),
                "held": sum(1 for up in uploads for r in up["runs"]
                            if r["held"] is not None)})

        new = sum(1 for up in uploads for r in up["runs"] if r["held"] is None)
        if new and not self.batch_limiter.allow(f"batch:{user['id']}", cost=new):
            raise Denied(f"this batch would store {new} results, past this hub's "
                         f"{self.batch_limiter.limit} an hour per account; try "
                         f"later, or send it in parts", HTTPStatus.TOO_MANY_REQUESTS)
        stored = self.store.insert_batch(uploads, user["id"])
        count = sum(1 for up in stored for r in up["runs"] if "id" in r)
        self.send_json(HTTPStatus.CREATED if count else HTTPStatus.OK, {
            "user": user["name"], "stored": count,
            "skipped": sum(len(up["runs"]) for up in stored) - count,
            "uploads": stored})

    def redirect(self, location: str, cache: str = "no-cache"):
        self.send_bytes(HTTPStatus.FOUND, b"", "text/plain; charset=utf-8",
                        {"Location": location, "Cache-Control": cache})

    def download(self, path: str):
        """One file of the mirrored release, by /downloads/TAG/NAME.

        Looked up by name in the release's manifest, never by path on disk, so
        nothing outside it can be named. /downloads/latest/NAME is a redirect
        to the release being served, for a link that outlives it.
        """
        parts = path.split("/")
        if (len(parts) != 4 or not TAG_RE.fullmatch(parts[2])
                or not NAME_RE.fullmatch(parts[3])):
            return self.fail(HTTPStatus.NOT_FOUND, "not found")
        tag, name = parts[2], parts[3]
        mirror = self.hub.downloads
        if tag == "latest":
            manifest = mirror.manifest()
            if manifest and mirror.find(manifest["tag"], name):
                return self.redirect(f"/downloads/{manifest['tag']}/{name}")
            return self.fail(HTTPStatus.NOT_FOUND, "not in the latest release")
        found = mirror.find(tag, name)
        if not found:
            return self.fail(HTTPStatus.NOT_FOUND,
                             "not here: this hub serves only its latest release")
        file_path, entry = found
        try:
            # Opened apart from the `with` below, so that only a failed open
            # is a 404 and an error while sending is not.
            f = open(file_path, "rb")  # noqa: SIM115
        except OSError:
            return self.fail(HTTPStatus.NOT_FOUND, "not found")
        with f:
            size = os.fstat(f.fileno()).st_size
            start, end = 0, size - 1
            status = HTTPStatus.OK
            headers = {
                "Accept-Ranges": "bytes",
                "Content-Disposition": f'attachment; filename="{name}"',
                # A day, not forever: a release run again under the same tag
                # rebuilds its files, and the edge should catch up.
                "Cache-Control": "public, max-age=86400",
                "ETag": f'"{entry["sha256"]}"',
            }
            wanted = byte_range(self.headers.get("Range"), size)
            if wanted == "unsatisfiable":
                headers["Content-Range"] = f"bytes */{size}"
                return self.send_bytes(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
                                       b"", "text/plain; charset=utf-8", headers)
            if wanted:
                start, end = wanted
                status = HTTPStatus.PARTIAL_CONTENT
                headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            self.send_response(status)
            self.send_header("Content-Type", DOWNLOAD_TYPES.get(
                Path(name).suffix, "application/octet-stream"))
            self.send_header("Content-Length", str(end - start + 1))
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command == "GET" and start == 0:
                # A download is counted where it starts: a resumed one, which
                # asks for the rest of the file, is the same download, and a
                # HEAD is not one. One that is abandoned halfway still counts.
                try:
                    self.store.count_download(tag, name)
                except sqlite3.Error:
                    pass                # a counter must not fail a download
            if self.command != "HEAD" and end >= start:
                # Straight from the file to the socket: the Windows setup is
                # fifty megabytes, and the hub is given a quarter of a gigabyte.
                self.connection.sendfile(f, start, end - start + 1)

    def static(self, path: str):
        if path in ("/", ""):
            path = "/index.html"
        target = (STATIC_DIR / path.lstrip("/")).resolve()
        # is_relative_to, not a string prefix: "static" is a prefix of
        # "static-old" too.
        if not target.is_relative_to(STATIC_DIR) or not target.is_file():
            return self.fail(HTTPStatus.NOT_FOUND, "not found")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",
                                                  "text/javascript"):
            ctype += "; charset=utf-8"
        body = target.read_bytes()
        if target.name == "index.html":
            body = versioned_assets(body)
        self.send_bytes(HTTPStatus.OK, body, ctype, {"Cache-Control": "no-cache"})


# The page's own script and stylesheet, as index.html links them.
ASSET_LINK_RE = re.compile(rb'(src|href)="/(app\.js|style\.css)"')


def versioned_assets(page: bytes) -> bytes:
    """index.html with its script and stylesheet links naming their contents.

    A CDN in front may hold app.js for hours whatever Cache-Control says --
    Cloudflare's browser cache TTL overrides a shorter one -- and a page
    from a new hub running last deploy's script is a page whose requests the
    hub no longer understands. A link that changes when the file does is
    fetched afresh however long the old one is kept.
    """
    def link(m: re.Match) -> bytes:
        digest = hashlib.sha256((STATIC_DIR / m.group(2).decode()).read_bytes())
        return m.group(0)[:-1] + b"?v=" + digest.hexdigest()[:12].encode() + b'"'
    return ASSET_LINK_RE.sub(link, page)


# What a download is sent as. Everything else is a plain stream of bytes, which
# is what it is: the browser saves it, which is all a download page wants.
DOWNLOAD_TYPES = {".apk": "application/vnd.android.package-archive"}
RANGE_RE = re.compile(r"bytes=(\d{0,19})-(\d{0,19})")


def byte_range(header: str | None, size: int):
    """(first, last) for a single-range Range header, "unsatisfiable", or None
    to send the whole file -- which is also the answer to several ranges, or to
    a header this does not understand, as HTTP allows."""
    m = RANGE_RE.fullmatch((header or "").strip())
    if not m or (not m.group(1) and not m.group(2)):
        return None
    if not m.group(1):                          # bytes=-N: the last N bytes
        n = int(m.group(2))
        if n == 0:
            return "unsatisfiable"
        return max(0, size - n), size - 1
    first = int(m.group(1))
    last = int(m.group(2)) if m.group(2) else size - 1
    if m.group(2) and last < first:
        return None
    if first >= size:
        return "unsatisfiable"
    return first, min(last, size - 1)


def clamp_int(v, lo: int, hi: int, default: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


class Server(ThreadingHTTPServer):
    """The listening socket, plus the five things a handler reaches for.

    Declared here rather than only attached in `build_server`: a handler
    touches them on every request, and there is otherwise nothing that says
    they exist -- not for a reader, and not for a type checker. The three
    without a default are required; a server built without them is broken, so
    the AttributeError is the right outcome.
    """

    daemon_threads = True
    allow_reuse_address = True

    store: Store
    limiter: RateLimiter
    auth_limiter: RateLimiter
    # What it takes to get an account here: a proof of work, a hard limit on
    # how many an address may create, and optionally an invite code -- or the
    # door shut entirely.
    register_limiter: RateLimiter
    # Batches, per account and counted in results stored: a far larger
    # allowance than single uploads get, for an account only.
    batch_limiter: RateLimiter
    challenges: Challenges
    register_open: bool = True
    register_pow: int = REGISTER_POW_BITS
    signup_token: str | None = None
    trust_proxy: bool = False
    downloads: Mirror


def build_server(db_path: str, host: str, port: int, *, rate_limit: int = 30,
                 window: float = 3600.0, trust_proxy: bool = False,
                 auth_limit: int = 20, register_open: bool = True,
                 register_pow: int = REGISTER_POW_BITS,
                 register_limit: int = 5,
                 signup_token: str | None = None,
                 batch_limit: int = BATCH_LIMIT,
                 downloads: str | Path | None = None,
                 mirror: str | None = None) -> Server:
    srv = Server((host, port), Handler)
    # Beside the database unless told otherwise: the one place a hub is sure to
    # be allowed to write.
    srv.downloads = Mirror(downloads if downloads is not None
                           else Path(db_path).resolve().parent / "downloads", mirror)
    srv.store = Store(db_path)
    srv.limiter = RateLimiter(rate_limit, window)
    # Sign-ins are limited separately and much harder: uploading a lot is
    # enthusiasm, trying twenty passwords in a quarter of an hour is not.
    srv.auth_limiter = RateLimiter(auth_limit, 900.0)
    # New accounts are limited per hour rather than per quarter of an hour: a
    # person makes one, ever, and the number that matters is how many a script
    # can make overnight.
    srv.register_limiter = RateLimiter(register_limit, 3600.0)
    srv.batch_limiter = RateLimiter(batch_limit, window)
    srv.challenges = Challenges()
    srv.register_open = register_open
    srv.register_pow = max(0, min(MAX_POW_BITS, register_pow))
    srv.signup_token = signup_token or None
    srv.trust_proxy = trust_proxy
    return srv


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(HERE / "runs.sqlite3"),
                    help="SQLite database file (created if absent)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address (default 127.0.0.1; use 0.0.0.0 to expose)")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--rate-limit", type=int, default=30,
                    help="uploads per address per hour, 0 to disable "
                         f"(a signed-in account gets {ACCOUNT_RATE_FACTOR}x this)")
    ap.add_argument("--batch-limit", type=int, default=BATCH_LIMIT,
                    help="results an account may upload per hour in batches "
                         f"(default {BATCH_LIMIT}), 0 to disable; single "
                         "uploads keep --rate-limit")
    ap.add_argument("--auth-limit", type=int, default=20,
                    help="sign-in attempts per address per 15 minutes, "
                         "0 to disable")
    ap.add_argument("--register-limit", type=int, default=5,
                    help="new accounts per address per hour, 0 to disable")
    ap.add_argument("--register-pow", type=int, default=REGISTER_POW_BITS,
                    metavar="BITS",
                    help="proof-of-work a new account must show, in leading "
                         f"zero bits (default {REGISTER_POW_BITS}, max "
                         f"{MAX_POW_BITS}; 0 disables it). Each bit doubles "
                         "the work: on average 16 is half a second in the "
                         "browser, 20 is eight seconds and 22 is half a minute "
                         "-- a random search, so any one attempt may take "
                         "several times that")
    ap.add_argument("--signup-token", metavar="CODE",
                    help="also require this invite code to register, so the "
                         "hub is readable and uploadable by anyone but only "
                         "joinable by people you gave the code to")
    ap.add_argument("--no-register", action="store_true",
                    help="take no new accounts at all (existing ones, "
                         "anonymous uploads and the board are unaffected)")
    ap.add_argument("--trust-proxy", action="store_true",
                    help="take the client address from X-Forwarded-For, and "
                         "the scheme from X-Forwarded-Proto so session cookies "
                         "are marked Secure (only behind a proxy that sets them)")
    ap.add_argument("--downloads", metavar="DIR",
                    help="where the release downloads are kept and served "
                         "from (default: downloads/ beside --db)")
    ap.add_argument("--mirror", metavar="OWNER/REPO",
                    help="keep --downloads up to date with this GitHub "
                         "repository's latest release, checking hourly -- "
                         "e.g. lukaszsobala/cpcpub. Without it the hub serves "
                         "whatever downloads.py last put there")
    args = ap.parse_args(argv)
    if args.mirror and not REPO_RE.fullmatch(args.mirror):
        ap.error(f"--mirror wants OWNER/REPO, not {args.mirror!r}")

    srv = build_server(args.db, args.host, args.port,
                       rate_limit=args.rate_limit, trust_proxy=args.trust_proxy,
                       auth_limit=args.auth_limit,
                       register_open=not args.no_register,
                       register_pow=args.register_pow,
                       register_limit=args.register_limit,
                       signup_token=args.signup_token,
                       batch_limit=args.batch_limit,
                       downloads=args.downloads, mirror=args.mirror)
    host, port = srv.server_address[:2]
    print(f"cpcpub hub on http://{host}:{port}  (db: {args.db})", file=sys.stderr)
    if args.mirror:
        srv.downloads.start(lambda line: print(line, file=sys.stderr))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print(file=sys.stderr)
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
