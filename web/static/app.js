"use strict";

// cpcpub results hub, front end. No dependencies, no inline script (the
// server sends a CSP that forbids it). Every value that came from an upload is
// written with textContent, never innerHTML.

const state = {
  metrics: [],          // from /api/metrics
  limit: 100,           // rows the board asks for, from its Show picker
  offset: 0,            // how far down the board this page starts
  total: 0,             // rows the board has in all, past this page
  mineLimit: 50,        // the same, for either list under My uploads
  mineOffset: 0,        // where the account half of My uploads starts
  mineTotal: 0,         // CPUs on the account's list, which it is paged by
  mineCounts: null,     // and the runs and uploads on them: {runs, uploads}
  localOffset: 0,       // and where this browser's own list starts
  accountRuns: null,    // this account's uploads, once fetched; null = not yet
  accountError: "",     // why that listing is missing, if it is
  rows: [],             // current leaderboard page: one row per upload, or per CPU
  grouped: false,       // the page is one row per CPU (Group by CPU)
  children: new Map(),  // run id (or CPU group key) -> its rows, once expanded
  expanded: new Set(),  // run ids (or CPU group keys) currently showing them
  kindsOpen: new Set(), // "run|scope|kind" of core kinds unfolded to their cores
  openRun: null,        // the run open over the page, kept in the URL
  selected: new Map(),  // core id -> row; all of one scope, see retargetSelection
  params: new URLSearchParams(),   // the filters the current board was loaded with
  sort: "score",        // which metric the board is ranked by, set by its header
  order: "desc",
  search: "",           // the last submitted search, not what is in the box
  cpus: new Map(),      // suggested CPU name -> [cpu_key, layout], from /api/stats
  pickable: [],         // the run rows the board is showing, for its tick-all box
  norm: "abs",
  detailed: false,      // every metric, rather than the headline set
  scopeNote: "",        // what changing scope did to the selection, if anything
  user: null,           // the signed-in account, from /api/auth/me
  submitter: "",        // board narrowed to one account's uploads, by name
  files: [],            // result documents chosen but not yet uploaded
  release: null,        // the newest release this hub verifies against, if any
  downloads: null,      // the release this hub serves, from /api/downloads
  wantDownload: false,  // the page was opened at #download
  held: null,           // how many runs this hub holds at all, from /api/stats
};

// How many results a list may show at once. The server clamps every listing to
// MAX_PAGE as well, so asking for more by hand gets the same answer as asking
// for the cap; the picker offers the sizes worth choosing between.
const MAX_PAGE = 500;
// The most runs one comparison holds. Two --variants uploads side by side is
// eight; past that a stack of bars is read by counting down it rather than by
// its colour, and the page has eight colours that stay apart (--c0..--c7).
const MAX_COMPARE = 8;
const FULL_NOTE = `A comparison holds at most ${MAX_COMPARE} runs.`;

function compareFull() {
  return state.selected.size >= MAX_COMPARE;
}
const PAGE_SIZES = [25, 50, 100, 250, MAX_PAGE];

// The page reads this one to prove a write came from the page rather than from
// another site that merely knows the session cookie exists. The session cookie
// itself is HttpOnly and unreadable here, which is the point.
const CSRF_COOKIE = "cpb_csrf";

// Which way a metric runs, in words and in one glyph. Every place a metric is
// named says this: a bar is drawn from the measurement itself, so nothing in
// the picture tells you which end of it wins.
const DIRECTIONS = {
  high: { text: "higher is better", mark: "↑" },
  low:  { text: "lower is better",  mark: "↓" },
  none: { text: "not a ranking",    mark: "" },
};

const SCOPE_LABELS = {
  total: "whole-machine",
  cpu: "per-core",
  thread: "per-thread",
};

function direction(metric) {
  return (metric && DIRECTIONS[metric.better]) || null;
}

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
};

// A page size that came from a picker, a stored preference or a stale saved
// value, made into one this page will actually ask for.
function pageSize(v, fallback) {
  const n = Number.parseInt(v, 10);
  if (!Number.isFinite(n) || n < 1) return fallback;
  return Math.min(n, MAX_PAGE);
}

// A page size chosen once is the one you meant on the next visit too. Kept in
// local storage, where a browser that refuses it costs the preference and
// nothing else.
function storedPage(key, fallback) {
  try {
    const n = pageSize(localStorage.getItem(key), fallback);
    // A size the picker does not offer -- an older build's, or a hand-edited
    // one -- would leave the picker showing nothing at all, so it is not kept.
    return PAGE_SIZES.includes(n) ? n : fallback;
  } catch {
    return fallback;
  }
}

function rememberPage(key, n) {
  try {
    localStorage.setItem(key, String(n));
  } catch {
    /* a browser with storage denied still gets the size it picked, once */
  }
}

// A page size is a window on a listing, not a cap on it: every list here can
// be walked from end to end, so the size only decides how much of it is on
// screen at a time. One pager draws that for all three.
//
// `go(offset)` is handed the first row of the page asked for; whoever owns the
// list refetches or redraws from it.
// Where the last page of a listing starts. Zero for a listing that has no
// rows at all, which is also where a page that has outlived its rows belongs.
function lastPage(total, limit) {
  return total > 0 ? Math.floor((total - 1) / limit) * limit : 0;
}

function pager(where, { offset, limit, total }, go) {
  if (total <= limit && offset === 0) return;      // one page holds the lot
  const bar = el("nav", "pager");
  bar.setAttribute("aria-label", "pages");
  const last = lastPage(total, limit);

  const step = (label, to, disabled, title) => {
    const b = el("button", "page", label);
    b.type = "button";
    b.disabled = disabled;
    if (title) b.title = title;
    b.addEventListener("click", () => go(to));
    return b;
  };
  bar.append(step("« first", 0, offset <= 0, "the first page"),
             step("‹ prev", Math.max(0, offset - limit), offset <= 0, null));
  // Where you are, in rows rather than in page numbers: the rows are what the
  // reader is looking at, and the count is the answer to "is there more".
  const from = total ? offset + 1 : 0;
  const to = Math.min(offset + limit, total);
  bar.append(el("span", "pagecount",
    `${from.toLocaleString()}–${to.toLocaleString()} of ${total.toLocaleString()}`));
  bar.append(step("next ›", offset + limit, offset + limit >= total, null),
             step("last »", last, offset >= last, "the last page"));
  where.append(bar);
}

// Both pickers are filled from PAGE_SIZES rather than written out in the
// markup, so the cap is stated once and cannot drift from the server's.
function fillPageSizes(select, current) {
  select.replaceChildren();
  for (const n of PAGE_SIZES) {
    const opt = el("option", null, String(n));
    opt.value = String(n);
    select.append(opt);
  }
  select.value = String(current);
}

function cookie(name) {
  for (const part of document.cookie.split(";")) {
    const [k, ...v] = part.trim().split("=");
    if (k === name) return decodeURIComponent(v.join("="));
  }
  return "";
}

async function api(path, opts) {
  const o = { ...(opts || {}) };
  // Anything that changes something carries the CSRF token. Reads do not need
  // it, and neither does a request with no session behind it.
  if (o.method && o.method !== "GET") {
    // Over https the hub names it with the __Host- prefix, which only this
    // host can set; the bare name is a plain-http hub's. The hub compares the
    // header only with the cookie of the name it uses, so a bare one planted
    // beside a prefixed one gains nothing.
    const csrf = cookie(`__Host-${CSRF_COOKIE}`) || cookie(CSRF_COOKIE);
    if (csrf) o.headers = { ...(o.headers || {}), "X-CSRF-Token": csrf };
  }
  const r = await fetch(path, o);
  const body = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  if (!r.ok) {
    // The rest of the reply rides along: a refusal can say more than words,
    // such as which run a re-upload already is.
    const err = new Error(body.error || `HTTP ${r.status}`);
    err.status = r.status;
    err.body = body;
    throw err;
  }
  return body;
}

// A button that puts a block of shell on the clipboard, for the machine that
// is not this one. Falls back to selecting the text when the clipboard is not
// available (it is not, over plain http on some browsers), so the gesture is
// never simply dead.
function copyButton(btn) {
  const pre = document.getElementById(btn.dataset.copy);
  if (!pre) return;
  const done = (msg) => {
    btn.textContent = msg;
    setTimeout(() => { btn.textContent = "Copy"; }, 1500);
  };
  const select = () => {
    const range = document.createRange();
    range.selectNodeContents(pre);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    done("selected");
  };
  if (!navigator.clipboard) return select();
  navigator.clipboard.writeText(pre.textContent).then(() => done("copied"), select);
}

// The same for one line of text that is not on the page, such as a link. Where
// there is no clipboard, the browser's own prompt holds it ready to copy.
function copyText(text, btn, what = "link") {
  const label = btn.textContent;
  const done = (msg) => {
    btn.textContent = msg;
    setTimeout(() => { btn.textContent = label; }, 1500);
  };
  const ask = () => window.prompt(`Copy this ${what}:`, text);
  if (!navigator.clipboard) return ask();
  navigator.clipboard.writeText(text).then(() => done("copied"), ask);
}

// A token and a button that copies it: a token is pasted somewhere else --
// the app, a command line, a note to keep -- and never read off the screen.
function tokenBox(token) {
  const box = el("span", "tokenbox");
  const copy = el("button", "copy", "Copy");
  copy.type = "button";
  copy.addEventListener("click", () => copyText(token, copy, "token"));
  box.append(el("code", "token", token), copy);
  return box;
}

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

// A moment as people read one: in their own time zone and words, "26 Aug 2026,
// 19:32 · 7 weeks ago", with the exact instant it was stored as on hover.
function when(iso) {
  const t = Date.parse(iso || "");
  const span = el("span", null, Number.isNaN(t) ? iso || "" : `${
    new Date(t).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })
  } · ${ago(t)}`);
  if (iso) span.title = iso;
  return span;
}

function ago(t) {
  const s = (Date.now() - t) / 1000;
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  for (const [unit, n] of [["year", 31557600], ["month", 2629800], ["week", 604800],
                           ["day", 86400], ["hour", 3600], ["minute", 60]]) {
    if (s >= n) return rtf.format(-Math.round(s / n), unit);
  }
  return "just now";
}

function fmt(v, digits) {
  if (v === null || v === undefined) return "—";
  const a = Math.abs(v);
  if (a === 0) return "0";
  if (a >= 10000) return v.toFixed(0);
  if (a >= 100) return v.toFixed(digits ?? 1);
  if (a >= 1) return v.toFixed(digits ?? 2);
  return v.toFixed(3);
}

function ordinal(n) {
  const t = n % 100;
  const suffix = t >= 11 && t <= 13 ? "th" : ["th", "st", "nd", "rd"][n % 10] || "th";
  return `${n}${suffix}`;
}

function mean(values) {
  const v = values.filter((x) => x !== null && x !== undefined);
  return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null;
}

// [4, 5, 6, 7, 9] -> "4–7, 9": the CPUs a kind of core is, as the kernel
// writes a CPU list.
function cpuList(cpus) {
  const out = [];
  for (let i = 0; i < cpus.length; i++) {
    let j = i;
    while (j + 1 < cpus.length && cpus[j + 1] === cpus[j] + 1) j++;
    out.push(j > i ? `${cpus[i]}–${cpus[j]}` : `${cpus[i]}`);
    i = j;
  }
  return out.join(", ");
}

// The mean of each metric over some records, in the reading the page shows.
function meanRow(members) {
  const out = { normalised: true, mhz: mean(members.map((r) => r.mhz)) };
  for (const m of state.metrics) out[m.key] = mean(members.map((r) => value(r, m)));
  return out;
}

// How a kind of core is named in a table: "large cores ×4", "all 8 threads".
function kindName(kind, scope, n) {
  const noun = scope === "thread" ? "threads" : "cores";
  if (kind.startsWith("all ")) return `all ${n} ${noun}`;
  return `${kind} ${noun} ×${n}`;
}

// Where a run stands among the runs it is compared with, from a standing
// ({value, low, high, rank}). "Top" for the best and for anything within
// TOP_BAND of it, which is as close as two runs of one machine come anyway;
// "2nd" and "3rd" for the next places, counted as ties usually are -- two
// runs sharing Top make the next one 3rd; and past those, how far behind the
// best it is: "−8%" for one that reached 92% of it. Nothing for a metric with
// no better end, which has no best. `gap` is the percentage behind, always.
const TOP_BAND = 1;         // percent behind the best that still reads as Top
const BEHIND_BAD = 5;       // percent behind the best that is worth a colour

function gapBehind(value, best, better) {
  if (!best || !value) return null;
  return 100 - (better === "low" ? best / value : value / best) * 100;
}

function behind(f, better) {
  if (!f || better === "none" || f.value === null || f.value === undefined) return null;
  const gap = f.rank === 1 ? 0 : gapBehind(f.value, better === "low" ? f.low : f.high, better);
  if (gap === null) return null;
  if (gap < TOP_BAND) return { text: "Top", gap, top: true, placed: true };
  if (f.rank <= 3) return { text: ordinal(f.rank), gap, top: false, placed: true };
  return { text: `−${gap.toFixed(0)}%`, gap, top: false, placed: false };
}

function behindClass(b) {
  return b.top ? "good" : b.gap >= BEHIND_BAD ? "bad" : null;
}

// Per-GHz normalisation. Rates divide by clock and latencies in ns become
// cycles; `ratio` and `fixed` pass through untouched -- the first is already
// clock-independent, the second is not the core clock's to set (see METRICS in
// server.py). Nothing here invents a unit the metric table did not ask for.
function value(row, metric) {
  const raw = row[metric.key];
  if (raw === null || raw === undefined) return null;
  // A median or a mean the page or the server made from rows already read
  // per GHz: dividing again would read it per GHz squared.
  if (row.normalised) return raw;
  if (state.norm === "abs" || !row.mhz) return raw;
  const ghz = row.mhz / 1000;
  if (!ghz) return raw;
  if (metric.kind === "rate") return raw / ghz;
  if (metric.kind === "time") return raw * ghz;
  return raw;
}

function unitOf(metric) {
  if (state.norm === "abs") return metric.unit;
  if (metric.kind === "rate") return metric.unit + "/GHz";
  if (metric.kind === "time") return "cycles";
  return metric.unit;
}

// A metric outside the score -- the FP32 pair -- is set apart wherever it is
// drawn: a dashed rule before it and a quieter header (style.css), with the
// reason in the tooltip. `node` is its header or cell; returns it.
function markAside(node, m) {
  if (!m || !m.aside) return node;
  node.classList.add("aside");
  node.title = node.title ? `${node.title} (${m.aside})` : `${m.label}: ${m.aside}`;
  return node;
}

// The columns a table shows. The metric being sorted on is always one of them —
// a board ordered by a number it does not print reads as arbitrary.
function shownMetrics() {
  if (state.detailed) return state.metrics;
  return state.metrics.filter((m) => m.headline || m.key === state.sort);
}

// Every architecture the benchmark can report -- the list in arch_string(),
// bench/src/bench.c -- each with a name a reader will recognise. All of them,
// not the three that used to be typed into the HTML: a hub holding a POWER,
// LoongArch or IBM Z result had no way to ask for it.
const TARGETS = [
  ["x86_64", "Intel / AMD, 64-bit"],
  ["aarch64", "Arm, 64-bit"],
  ["armv7", "Arm, 32-bit (ARMv7)"],
  ["armv6", "Arm, 32-bit (ARMv6)"],
  ["arm", "Arm, 32-bit (older)"],
  ["riscv64", "RISC-V, 64-bit"],
  ["loongarch64", "LoongArch, 64-bit"],
  ["ppc64le", "POWER, little-endian"],
  ["ppc64", "POWER, big-endian"],
  ["s390x", "IBM Z"],
];

// Fill the Arch picker: the list above, then anything this hub actually holds
// that is not on it -- a build newer than this page, or the "unknown" a target
// nobody taught the benchmark reports. Counts arrive with /api/stats, so an
// entry that would find nothing says so before it is picked rather than after.
// Called again when they do, which is why the current choice is put back.
function fillTargets(counts) {
  const sel = $("#filters").elements.target;
  const chosen = sel.value;
  const held = new Map((counts || []).map((t) => [t.target, t.n]));
  const known = new Map(TARGETS);
  const order = [...known.keys()];
  for (const t of held.keys()) if (!known.has(t) && t) order.push(t);

  sel.replaceChildren();
  const any = el("option", null, "any");
  any.value = "";
  sel.append(any);
  for (const t of order) {
    const name = known.get(t);
    const n = held.get(t) || 0;
    const o = el("option", null,
      (name ? `${t} — ${name}` : t) + (counts ? ` (${n})` : ""));
    o.value = t;
    sel.append(o);
  }
  sel.value = chosen;
  // A stored or hand-typed choice this hub has no option for would leave the
  // picker blank while still filtering; falling back to "any" says what the
  // board is actually showing.
  if (sel.value !== chosen) sel.value = "";
}

// Fill the search box's suggestions from what this hub can compare: each CPU
// with its core counts once, as the board grouped by CPU would draw it, so
// "4× Cortex-A55 + 4× Cortex-A76" and "6× Cortex-A55 + 2× Cortex-A76" are two
// entries. A search that is one of them exactly narrows the board to that CPU
// rather than matching text, since the counts are in no column to match.
function fillCpus(rows) {
  state.cpus = new Map();
  const list = $("#cpu-list");
  list.replaceChildren();
  const entries = (rows || []).filter((r) => r.cpu_key)
    .map((r) => ({ r, name: withCounts(r.cpu_key, r.cpu_layout) }))
    .sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
  for (const { r, name } of entries) {
    if (state.cpus.has(name)) continue;
    state.cpus.set(name, [r.cpu_key, r.cpu_layout || null]);
    // The name alone: given a label as well, Firefox shows only the label and
    // Chrome shows both, so a run count there reads wrong in one of them.
    const o = el("option");
    o.value = name;
    list.append(o);
  }
}

// What to call the operating system a result was measured on. The upload
// carries uname's own spelling, and "Darwin" is not what anyone calls that
// machine; anything else -- a BSD, an unknown -- passes through as it came.
const OS_NAMES = { linux: "Linux", darwin: "macOS", windows: "Windows" };

function osName(row, unknown = "—") {
  const s = (row.sysname || "").trim();
  if (!s) return unknown;
  return OS_NAMES[s.toLowerCase()] || s;
}

// Fill the OS picker from what this hub holds. There is no list to draw it
// from the way Arch has one: uname reports whatever the machine it ran on
// says, so an OS nobody has uploaded from is an option worth not offering.
// Empty until /api/stats answers, which is why the current choice is put back.
function fillSystems(counts) {
  const sel = $("#filters").elements.os;
  const chosen = sel.value;
  sel.replaceChildren();
  const any = el("option", null, "any");
  any.value = "";
  sel.append(any);
  for (const row of counts || []) {
    if (!row.sysname) continue;
    const o = el("option", null, `${osName(row)} (${row.n})`);
    o.value = row.sysname;
    sel.append(o);
  }
  sel.value = chosen;
  // Same as Arch: a choice this hub has nothing for falls back to "any" rather
  // than leaving the picker blank while still narrowing the board.
  if (sel.value !== chosen) sel.value = "";
}

// How fast each core design is, roughly: a higher tier is a faster core. Only
// the order matters, and only between designs that share a chip, so this is a
// rough guide to edit as needed rather than a benchmark of its own. Names are
// as the benchmark spells them from a MIDR.
const CORE_TIER = {
  "ARM11-MPCore": 1, ARM1136: 1, ARM1156: 1, ARM1176: 1,
  "Cortex-A5": 2, "Cortex-A7": 3, "Cortex-A32": 3, "Cortex-A8": 4,
  "Cortex-A34": 4, "Cortex-A35": 4, "Cortex-A9": 5, "Cortex-A53": 5,
  "Brahma-B53": 5, "Cortex-A55": 6, "Cortex-A65": 6, "Cortex-A65AE": 6,
  "Neoverse-E1": 6, "Kryo-3XX-Silver": 6, "Kryo-4XX-Silver": 6,
  "Cortex-A12": 7, "Cortex-A510": 7, "Cortex-A17": 8, "Cortex-A320": 8,
  "Cortex-A520": 8, "Cortex-A520AE": 8, "C1-Nano": 8, "Cortex-A15": 9,
  "Cortex-A57": 10, "Cortex-A72": 12, "Cortex-A73": 13, "Cortex-A75": 14,
  "Kryo-3XX-Gold": 14, "Cortex-A76": 16, "Cortex-A76AE": 16,
  "Neoverse-N1": 16, "Kryo-4XX-Gold": 16, "Cortex-A77": 17,
  "Cortex-A78": 18, "Cortex-A78AE": 18, "Cortex-A78C": 18, "Cortex-A710": 19,
  "Cortex-X1": 20, "Cortex-X1C": 20, "Cortex-A715": 20, "Neoverse-V1": 20,
  "Neoverse-N2": 20, "Cortex-A720": 21, "Cortex-A720AE": 21,
  "Cortex-A725": 22, "Cortex-X2": 22, "Neoverse-N3": 22, "C1-Pro": 23,
  "Cortex-X3": 24, "Neoverse-V2": 24, "Cortex-X4": 26, "C1-Premium": 26,
  "Neoverse-V3": 27, "Neoverse-V3AE": 27, "Cortex-X925": 28, "C1-Ultra": 29,
};

// A design's tier, or null where it is not known. Apple's cores are matched by
// family, as each chip gives its efficiency and performance cores new names.
function coreTier(name) {
  if (name in CORE_TIER) return CORE_TIER[name];
  if (/^(Icestorm|Blizzard|Sawtooth)\b/.test(name)) return 10;
  if (/^(Firestorm|Avalanche|Everest)\b/.test(name)) return 30;
  return null;
}

// A CPU line, or a layout, with its fastest core named first: "Cortex-A76 +
// Cortex-A55" where the machine listed the A55s first because they are CPU 0.
// Left as it is if any design in it is not in the table, so an unknown core is
// never put in the wrong place. Only what is drawn changes; stored lines and
// the keys runs are grouped by do not.
function fastFirst(line) {
  if (!line || !line.includes(" + ")) return line;
  const board = line.match(/\s*\([^()]*\)$/);
  const names = (board ? line.slice(0, board.index) : line).split(" + ");
  const tiers = names.map((n) => coreTier(n.replace(/^\d+x /, "").trim()));
  if (tiers.some((t) => t === null)) return line;
  const order = names.map((n, i) => i).sort((a, b) => tiers[b] - tiers[a]);
  return order.map((i) => names[i]).join(" + ") + (board ? board[0] : "");
}

// A CPU line with how many of each core it has: "4× Cortex-A76 + 4× Cortex-A55
// (Radxa ROCK 5B)", fastest core first. Where the line names the chip rather
// than its cores, as a vendor kernel's does, the counts lead all the same and
// the line's own name for the machine follows in their place: "4× Cortex-A76 +
// 4× Cortex-A55 (RK3588S CoolPi 4B Board)".
function withCounts(line, layout) {
  line = fastFirst(line);
  if (!layout) return line;
  const parts = fastFirst(layout).split(" + ").map((p) => p.match(/^(\d+)x (.+)$/));
  if (parts.some((p) => !p)) return line;
  const counted = (p) => `${p[1]}× ${p[2]}`;
  const board = line.match(/\s*\([^()]*\)$/);
  const names = (board ? line.slice(0, board.index) : line).split(" + ");
  if (parts.length === names.length && parts.every((p, i) => p[2] === names[i])) {
    return parts.map(counted).join(" + ") + (board ? board[0] : "");
  }
  const own = board ? board[0].trim().slice(1, -1) : line;
  return `${parts.map(counted).join(" + ")} (${own})`;
}

// What to call a machine whose run never said. uname's `machine` is the
// instruction set, not a CPU: printing it bare made a row read as a box called
// "aarch64", which is both untrue and already the target column's job. Runs
// uploaded by older binaries, before the benchmark could read a MIDR, are
// permanently in that state — nothing can go back and name them — so the label
// says what it knows and admits the rest.
function machineName(row, unknown = "unknown CPU") {
  if (row.cpu_models) return withCounts(row.cpu_models, row.cpu_layout);
  return row.machine ? `unnamed ${row.machine} CPU` : unknown;
}

function recordName(row) {
  if (row.scope === "total") return `all ${row.threads ?? "?"} threads`;
  const what = row.scope === "thread" ? "thread" : "cpu";
  return row.cpu === null || row.cpu === undefined ? what : `${what}${row.cpu}`;
}

// The label as the page prints it. An upload of several builds in one file has
// each label ended with its build, "(scalar-nofma)", which the Build column
// already says on the board -- and says the same on every row of a comparison
// of one build, so it is dropped wherever the page has the build in view.
// Stored labels are left alone: this only changes what is drawn.
function shownLabel(row, buildShown = true) {
  const label = row.label;
  if (!label || !buildShown || row.vectorize == null || row.fma == null) return label;
  const own = `${row.vectorize ? "vector" : "scalar"}-${row.fma ? "fma" : "nofma"}`;
  const tail = ` (${own})`;
  if (label === `(${own})`) return "";
  return label.endsWith(tail) ? label.slice(0, -tail.length) : label;
}

function flagsText(row) {
  const v = row.vectorize ? "vec" : "scalar";
  const f = row.fma ? "fma" : "no-fma";
  return `${v}, ${f}`;
}

// ---------------------------------------------------------------------------
// Leaderboard
// ---------------------------------------------------------------------------

function filterParams() {
  const form = $("#filters");
  const p = new URLSearchParams();
  // The dropdowns reload the board the moment they change, so their live value
  // is always the applied one.
  for (const name of ["scope", "target", "os", "vectorize", "fma", "norm"]) {
    const v = form.elements[name].value;
    if (v) p.set(name, v);
  }
  // The search box does not: half-typed text is not a filter anyone asked for,
  // so it applies on submit and is held here until then. One of its suggested
  // CPUs is that CPU and its core counts, as the pair the server keys it by.
  const pick = state.cpus.get(state.search);
  if (pick) {
    p.set("cpu", pick[0]);
    p.set("layout", pick[1] || "");
  } else if (state.search) {
    p.set("q", state.search);
  }
  // Set by clicking a submitter's name rather than by a control in the form,
  // and shown as a chip above the table so it is never a filter you cannot see.
  if (state.submitter) p.set("user", state.submitter);
  if (form.elements.verified.value) p.set("verified", form.elements.verified.value);
  if (form.elements.bycpu.checked) p.set("group", "cpu");
  // Sorting is not a filter either -- it lives on the column headers.
  p.set("sort", state.sort);
  p.set("order", state.order);
  p.set("limit", String(state.limit));
  p.set("offset", String(state.offset));
  return p;
}

// The direction a metric is worth reading in the first time you click it:
// down the rankings, whichever end of the scale that is.
function naturalOrder(key) {
  const m = state.metrics.find((x) => x.key === key);
  return m && m.better === "low" ? "asc" : "desc";
}

function sortBy(key) {
  if (state.sort === key) state.order = state.order === "desc" ? "asc" : "desc";
  else { state.sort = key; state.order = naturalOrder(key); }
  loadBoard().catch((e) => alert(e.message));
}

// Bumped per load, so a reply that was overtaken -- two filters changed in
// quick succession, a withdrawal refreshing the board while a search is still
// in flight -- is dropped rather than drawn over the newer one.
let boardGen = 0;

// `keepPage` is for the reloads that are not a new question: a withdrawal
// refreshing the board, or paging itself. Everything else -- a filter, a
// search, a different sort, a different page size -- asks something new, and
// the answer to something new starts at its first page.
async function loadBoard({ keepPage = false } = {}) {
  const gen = ++boardGen;
  const form = $("#filters");
  state.norm = form.elements.norm.value;
  state.detailed = form.elements.detailed.checked;
  const size = pageSize(form.elements.limit.value, state.limit);
  if (size !== state.limit) state.offset = 0;   // a resized page starts again
  state.limit = size;
  rememberPage("cpcpub-page-board", state.limit);
  if (!keepPage) state.offset = 0;
  // Remember what was actually applied: expanding a row has to ask for the
  // same filters and the same order, not whatever the form says by then.
  state.params = filterParams();
  // Which record represents an upload depends on the sort metric, so a reload
  // invalidates anything already expanded.
  state.children.clear();
  state.expanded.clear();
  // Follow the scope being ranked, so a comparison is never half one kind of
  // record and half another. Uses the params just applied, so the record it
  // picks per run is the one the board is about to show.
  await retargetSelection(state.params.get("scope"));
  const page = await api("/api/cores?" + state.params.toString());
  if (gen !== boardGen) return;        // a newer load is already on its way
  state.grouped = state.params.get("group") === "cpu";
  state.rows = state.grouped ? page.groups : page.cores;
  state.total = page.total ?? state.rows.length;
  // Withdrawing the last rows of the last page, or a filter narrowing under
  // you, can leave the board standing past the end of its own listing. Step
  // back to where the rows now stop rather than showing an empty board with a
  // pager saying there is more above it. Only ever backwards, so a count and a
  // page that disagree cost one retry and not an endless run of them.
  const back = lastPage(state.total, state.limit);
  if (!state.rows.length && back < state.offset) {
    state.offset = back;
    return loadBoard({ keepPage: true });
  }
  renderBoard();
  renderSelection();
}

// Paging is a reload of the same question at a different depth, so it keeps
// the filters, the sort and the ticks, and only moves the window.
function goToPage(offset) {
  state.offset = Math.max(0, offset);
  loadBoard({ keepPage: true })
    .then(() => $("#board-table").scrollIntoView({ block: "start" }))
    .catch((e) => alert(e.message));
}

// The records of one upload, in the board's own order. Fetched on demand: a
// per-core sweep of a 256-CPU machine is 256 rows nobody asked to see.
async function toggleExpand(row) {
  if (state.expanded.delete(row.run_id)) return renderBoard();
  if (!state.children.has(row.run_id)) {
    const p = new URLSearchParams(state.params);
    p.set("run", row.run_id);
    p.set("group", "none");
    p.set("limit", "1024");
    p.delete("offset");        // the board's page is not this run's records

    const { cores } = await api("/api/cores?" + p.toString());
    state.children.set(row.run_id, cores);
  }
  state.expanded.add(row.run_id);
  renderBoard();
}

function groupKey(g) {
  return JSON.stringify([g.cpu_key, g.cpu_layout, g.target, g.vectorize, g.fma, g.threads, g.run_id]);
}

// The runs one row of the CPU board stands for, under the same filters and in
// the same order, picked out by the exact CPU line rather than the search box.
async function loadGroup(g) {
  const key = groupKey(g);
  if (state.children.has(key)) return state.children.get(key);
  const p = new URLSearchParams(state.params);
  p.delete("group");
  p.delete("offset");
  p.set("cpu", g.cpu_key);
  p.set("layout", g.cpu_layout || "");
  p.set("target", g.target);
  p.set("vectorize", String(g.vectorize));
  p.set("fma", String(g.fma));
  if (g.scope !== "cpu" && g.threads !== null) p.set("threads", String(g.threads));
  p.set("limit", String(MAX_PAGE));
  const { cores } = await api("/api/cores?" + p.toString());
  state.children.set(key, cores);
  return cores;
}

async function toggleGroup(g) {
  const key = groupKey(g);
  if (state.expanded.delete(key)) return renderBoard();
  await loadGroup(g);
  state.expanded.add(key);
  renderBoard();
}

// How much of one CPU row's runs are ticked. A folded row's runs are not known
// until they are fetched, except that a CPU of one run is told apart by its run
// id, so a comparison arriving in a link ticks it without a request.
function groupTicks(g) {
  const kids = state.children.get(groupKey(g));
  if (kids) return { on: kids.filter((r) => state.selected.has(r.id)).length, total: kids.length };
  if (g.runs === 1 && g.run_id) {
    const on = [...state.selected.values()].some((r) => r.run_id === g.run_id);
    return { on: on ? 1 : 0, total: 1 };
  }
  return { on: 0, total: g.runs };
}

// The box on a CPU's row: ticks every run of that CPU, or unticks them when
// they all are ticked. Ticking from a folded row unfolds it, so the runs it
// just picked are in view rather than only in the comparison. A CPU of one run
// has nothing to unfold, and its row is the run.
function groupBox(g) {
  const key = groupKey(g);
  const box = el("td");
  const cb = el("input", "row-pick");
  cb.type = "checkbox";
  const { on, total } = groupTicks(g);
  cb.checked = total > 0 && on === total;
  cb.indeterminate = on > 0 && on < total;
  cb.title = cb.checked ? "untick every run on this CPU" : "tick every run on this CPU";
  const removes = takesOut(cb, on);
  cb.addEventListener("change", async () => {
    try {
      const rows = await loadGroup(g);
      if (g.runs > 1) state.expanded.add(key);
      if (rows.length) await selectRows(rows, !removes);
      else renderBoard();
    } catch (e) {
      alert(e.message);
    }
  });
  box.append(cb);
  return box;
}

// The header's box on the board grouped by CPU: every run of every CPU shown,
// the folded ones too, so it agrees with the box on each CPU's row. Ticking
// fetches the runs of the CPUs not yet fetched but leaves them folded -- a page
// of a hundred CPUs unfolded is not what ticking them all asks for.
function groupedTickAll(box, groups) {
  let on = 0;
  let full = 0;
  for (const g of groups) {
    const t = groupTicks(g);
    on += t.on;
    if (t.total > 0 && t.on === t.total) full++;
  }
  box.checked = groups.length > 0 && full === groups.length;
  box.indeterminate = on > 0 && full < groups.length;
  box.disabled = !groups.length;
  box.title = box.checked ? "untick every run shown" : "tick every run shown";
  const removes = takesOut(box, on);
  box.addEventListener("change", async () => {
    try {
      const want = !removes;
      let rows;
      if (want) {
        // A few at a time: one request per CPU, and a hundred at once is a
        // burst a small hub should not be handed.
        const todo = groups.filter((g) => !state.children.has(groupKey(g)));
        const pool = Array.from({ length: 6 }, async () => {
          for (let g = todo.shift(); g; g = todo.shift()) await loadGroup(g);
        });
        await Promise.all(pool);
        rows = groups.flatMap((g) => state.children.get(groupKey(g)) || []);
      } else {
        // Nothing needs fetching to untick: a run that was never fetched was
        // ticked, if at all, by a link, and is found among the selection.
        rows = groups.flatMap((g) => state.children.get(groupKey(g)) ||
          [...state.selected.values()].filter((r) => g.run_id && r.run_id === g.run_id));
      }
      if (rows.length) await selectRows(rows, want);
      else renderBoard();
    } catch (e) {
      alert(e.message);
    }
  });
}

function selectBox(row) {
  const box = el("td");
  const cb = el("input", "row-pick");
  cb.type = "checkbox";
  cb.checked = state.selected.has(row.id);
  if (!cb.checked && compareFull()) {
    cb.disabled = true;
    cb.title = `${FULL_NOTE} Remove one to add another.`;
  }
  cb.addEventListener("change", () =>
    toggleSelect(row, cb.checked).catch((e) => alert(e.message)));
  box.append(cb);
  return box;
}

// A column header that ranks the board by its own column. Clicking the one
// already sorted on turns it around; clicking another starts it at its natural
// end. Every metric the server will sort on has one, so the sort is wherever
// the number is rather than in a control away from it.
function sortHeader(label, unit, key) {
  const active = state.sort === key;
  // Spelling "higher is better" out fifteen times across one header row would
  // double the table's width, so a column carries the glyph and says the words
  // in its tooltip; the legend under the table gives both in full.
  const metric = state.metrics.find((x) => x.key === key);
  const dir = direction(metric);
  const th = el("th", active ? "num sorted" : "num");
  th.setAttribute("aria-sort", !active ? "none"
    : state.order === "desc" ? "descending" : "ascending");
  const btn = el("button", "sorth");
  btn.type = "button";
  btn.title = dir ? `sort by ${label} — ${dir.text}` : `sort by ${label}`;
  if (metric && metric.aside) {
    th.classList.add("aside");
    btn.title += ` (${metric.aside})`;
  }
  const name = el("span", "mname", label);
  if (active) name.append(el("span", "arrow", state.order === "desc" ? "▾" : "▴"));
  btn.append(name, el("span", "munit", dir && dir.mark ? `${unit} ${dir.mark}` : unit));
  btn.addEventListener("click", () => sortBy(key));
  th.append(btn);
  return th;
}

function fitHeader() {
  const th = el("th", "num fitcol", "vs CPU");
  th.title = "against the runs on the same CPU with the same build, at the metric " +
             `the board is sorted by: Top is the best or within ${TOP_BAND}% of it, then ` +
             "2nd and 3rd, then how far behind the best (−8%)";
  return th;
}

// A run measured with CPUs switched off: shown, but left out of every
// comparison, since its machine was less than its CPU line says.
function offlineText(row) {
  const n = row.cpus_offline;
  return `${n} CPU${n > 1 ? "s" : ""} offline`;
}

function offlineNote(row) {
  const note = el("span", "label", `${offlineText(row)} · not compared`);
  note.title = "measured with CPUs switched off, so left out of the comparisons with its CPU";
  return note;
}

// Pointing at a row lights up the others on the same CPU -- the runs its
// standing was drawn from -- so a percentage can be read against the rows it
// compares, and the rest of the board's percentages stay in the background.
function lightPeers(tr, peers) {
  tr.dataset.peers = peers;
  const set = (on) => {
    for (const other of tr.parentElement?.querySelectorAll("tr[data-peers]") || []) {
      if (other.dataset.peers === peers) other.classList.toggle("peer-lit", on);
    }
  };
  tr.addEventListener("mouseenter", () => set(true));
  tr.addEventListener("mouseleave", () => set(false));
}

// Where a run stands among the runs on its CPU, at the board's metric.
function fitCell(row) {
  const td = el("td", "num fit");
  const f = row.cpu_fit;
  const m = state.metrics.find((x) => x.key === state.sort);
  const b = f && m ? behind(f, m.better) : null;
  if (!b) return td;
  td.textContent = b.text;
  const cls = behindClass(b);
  if (cls) td.classList.add(cls);
  td.title = `${ordinal(f.rank)} of ${f.of} runs on this CPU` +
             (b.gap >= 0.05 ? `, ${b.gap.toFixed(1)}% behind the best` : "") +
             `; the best is ${fmt(m.better === "low" ? f.low : f.high)}`;
  return td;
}

function metricCells(tr, row) {
  tr.append(el("td", "num", row.mhz ? Math.round(row.mhz) : "—"));
  for (const m of shownMetrics()) tr.append(markAside(el("td", "num", fmt(value(row, m))), m));
}

// A run as a link, #run=N, so it can be opened in a new tab, copied or
// bookmarked like any other. A plain click opens it over the page.
function runLink(id, text) {
  const a = el("a", "linkish", text);
  a.href = `#run=${id}`;
  a.addEventListener("click", (e) => {
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    showRun(id).catch((err) => alert(err.message));
  });
  return a;
}

function filterBySubmitter(name) {
  state.submitter = name || "";
  if (state.openRun) closeRun();      // the run's page is where the link was
  showTab("board");
  syncHash();
  loadBoard().catch((e) => alert(e.message));
}

// A submitter as a link, #user=name, so one account's results can be opened in
// a new tab or bookmarked like a run. A plain click narrows the board to them.
function submitterLink(name) {
  const a = el("a", "linkish", name);
  a.href = `#user=${name}`;
  a.title = `show only ${name}'s uploads`;
  a.addEventListener("click", (e) => {
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    filterBySubmitter(name);
  });
  return a;
}

function renderSubmitterChip() {
  const box = $("#board-chip");
  box.replaceChildren();
  box.hidden = !state.submitter;
  if (!state.submitter) return;
  box.append(el("span", null, `Showing uploads by ${state.submitter}`));
  const clear = el("button", "linkish", "show everyone");
  clear.type = "button";
  clear.addEventListener("click", () => filterBySubmitter(""));
  box.append(clear);
}

// What an empty board says. It matters more than it looks now that Vectorize
// and FMA start at "off": the first thing a visitor sees can be nothing at all
// on a hub whose results were built the other way, and telling them to be the
// first to upload -- when there are results here they merely filtered out --
// sends them away for no reason. `state.held` is how many runs the hub has
// regardless of filters, so an empty hub still says it is empty; until
// /api/stats answers it is null, and an empty board is read as filtered, which
// is the harmless way to be wrong.
function renderEmptyNote() {
  const box = $("#board-empty");
  box.hidden = state.rows.length > 0;
  if (!box.hidden) return;
  box.textContent = state.held === 0
    ? "Nothing here yet. Be the first to upload a run."
    : "No result matches these filters. Vectorize and FMA start at " +
      "\u201coff\u201d \u2014 set either to \u201cany\u201d to see results " +
      "built the other way.";
}

function renderBoard() {
  renderSubmitterChip();
  const table = $("#board-table");
  const thead = table.tHead;
  const tbody = table.tBodies[0];
  thead.replaceChildren();
  tbody.replaceChildren();
  renderEmptyNote();
  const box = $("#board-pager");
  box.replaceChildren();
  pager(box, { offset: state.offset, limit: state.limit, total: state.total },
        goToPage);

  const hr = el("tr");
  const all = el("th");
  const allBox = el("input", "row-pick");
  allBox.type = "checkbox";
  all.append(allBox);
  hr.append(el("th", "num", "#"), all,
            el("th", "wide", state.grouped ? "CPU" : "machine"),
            el("th", "", "OS"), el("th", "", "arch"), el("th", "", "build"));
  // Grouped, each row already is its CPU, so standing against it says nothing.
  // The column stays, empty, so the board does not shift as grouping toggles.
  hr.append(state.grouped ? el("th", "num fitcol") : fitHeader());
  hr.append(sortHeader("MHz", "", "mhz"));
  for (const m of shownMetrics()) hr.append(sortHeader(m.label, unitOf(m), m.key));
  thead.append(hr);
  // Said under the table too while one of them is a column: the tooltip is
  // easy to miss, and a reader comparing FP32 with the score should not.
  const asides = new Map();
  for (const m of shownMetrics().filter((x) => x.aside)) {
    asides.set(m.aside, [...(asides.get(m.aside) || []), m.label]);
  }
  $("#board-legend").textContent = "↑ higher is better · ↓ lower is better" +
    [...asides].map(([why, labels]) => ` · ${labels.join(", ")}: ${why}`).join("");

  state.pickable = [];
  if (state.grouped) {
    state.rows.forEach((g, i) => renderGroup(tbody, g, state.offset + i + 1));
  } else {
    state.rows.forEach((row, i) => {
      // Its place in the whole ranking: on page three of a hundred-row board the
      // top row is 201st, and numbering it 1 would say the opposite.
      tbody.append(runRow(row, state.offset + i + 1));
      if (state.expanded.has(row.run_id)) renderRecords(tbody, row);
    });
  }
  if (state.grouped) groupedTickAll(allBox, state.rows);
  else tickAllBox(allBox, state.pickable);
}

// The header's box: ticks every run the board is showing, or unticks them all
// when they already are. The records unfolded under a run are not among them --
// a run beside its own records is the same machine twice. The board grouped by
// CPU has groupedTickAll instead.
function tickAllBox(box, rows) {
  const on = rows.filter((r) => state.selected.has(r.id)).length;
  box.checked = rows.length > 0 && on === rows.length;
  box.indeterminate = on > 0 && on < rows.length;
  box.disabled = !rows.length;
  box.title = box.checked ? "untick every run shown" : "tick every run shown";
  const removes = takesOut(box, on);
  box.addEventListener("change", () =>
    selectRows(rows, !removes).catch((e) => alert(e.message)));
}

// Whether a box over several runs takes them out when clicked rather than
// putting them in. It does when they are all in, as any such box does -- and
// also, once the comparison is full, when only some are: there is no room to
// put the rest in, and a click that can only say so is a dead one. A box none
// of whose runs are in has nothing to do while the comparison is full, and is
// switched off with the reason on it.
function takesOut(box, on) {
  if (box.checked) return true;
  if (!compareFull()) return false;
  if (on > 0) {
    box.title = "untick these runs: the comparison is full";
    return true;
  }
  box.disabled = true;
  box.title = `${FULL_NOTE} Remove one to add another.`;
  return false;
}

async function selectRows(rows, on) {
  if (on) {
    // The board's runs are all of its scope, so one retarget covers them.
    await retargetSelection(rows[0].scope);
    // Up to the cap, in the board's order, so what is left out is the bottom
    // of what was asked for. The Compare tab's "(8 of 8)" says it is full.
    const fresh = rows.filter((r) => !state.selected.has(r.id));
    const taken = fresh.slice(0, Math.max(0, MAX_COMPARE - state.selected.size));
    for (const r of taken) state.selected.set(r.id, r);
  } else {
    for (const r of rows) state.selected.delete(r.id);
    if (!state.selected.size) state.scopeNote = "";
  }
  syncHash();
  renderSelection();
  renderBoard();
}

// One upload's row. `nested` is a run listed under its CPU on the grouped
// board, which has no records to unfold there: its own page has them.
function runRow(row, number, nested = false) {
  const open = state.expanded.has(row.run_id);
  const tr = el("tr", nested ? "run-row nested" : "run-row");
  tr.append(el("td", "num", number), selectBox(row));
  state.pickable.push(row);
  if (row.peers) lightPeers(tr, row.peers);

  const name = el("td", "wide");
  const head = el("div", "run-head");
  if (!nested && (row.records ?? 1) > 1) {
    const ex = el("button", "expander", open ? "▾" : "▸");
    ex.type = "button";
    ex.title = open ? "hide this run's records" : "show every record in this run";
    ex.setAttribute("aria-expanded", String(open));
    ex.addEventListener("click", () => toggleExpand(row).catch((e) => alert(e.message)));
    head.append(ex);
  } else {
    head.append(el("span", "expander blank", " "));
  }
  head.append(runLink(row.run_id, nested ? shownLabel(row) || `run ${row.run_id}` : machineName(row)));
  name.append(head);
  if (shownLabel(row) && !nested) name.append(el("span", "label", shownLabel(row)));
  if (row.cpus_offline && !nested) name.append(offlineNote(row));
  tr.append(name);

  // The family only -- Linux, Windows, macOS. The build number the upload
  // also carries is a different question, and it is on the run's own page.
  tr.append(el("td", "", osName(row)), el("td", "", row.target || "—"),
            el("td", "flags", flagsText(row)));
  tr.append(state.grouped ? el("td", "num fit") : fitCell(row));
  metricCells(tr, row);
  return tr;
}

// An expanded run's records. Several cores of one kind are one averaged row,
// which unfolds to the cores; a kind of one, or a record of no kind, is shown
// as it is.
function renderRecords(tbody, row) {
  const kinds = new Map();
  for (const c of state.children.get(row.run_id) || []) {
    const key = c.type ? `${c.scope}|${c.type}` : `record|${c.id}`;
    if (!kinds.has(key)) kinds.set(key, []);
    kinds.get(key).push(c);
  }
  const blanks = () => [el("td", "", ""), el("td", "", ""), el("td", "", ""), el("td", "", "")];
  const record = (c, deeper) => {
    const sub = el("tr", deeper ? "child-row deeper" : "child-row");
    sub.append(el("td", "num", ""), selectBox(c));
    const cname = el("td", "wide");
    cname.append(el("span", "childname", recordName(c)));
    if (c.type && !deeper && !c.type.startsWith("all ")) cname.append(el("span", "hint", ` ${c.type}`));
    sub.append(cname, ...blanks());
    metricCells(sub, c);
    tbody.append(sub);
  };
  for (const [key, members] of kinds) {
    if (members.length < 2) { record(members[0], false); continue; }
    const c0 = members[0];
    const openKey = `${row.run_id}|${key}`;
    const open = state.kindsOpen.has(openKey);
    const sub = el("tr", "child-row kind-row");
    sub.append(el("td", "num", ""), el("td"));
    const cname = el("td", "wide");
    const label = el("span", "childname");
    const ex = el("button", "expander", open ? "▾" : "▸");
    ex.type = "button";
    ex.title = open ? "fold these cores into their average" : "show each of these";
    ex.setAttribute("aria-expanded", String(open));
    ex.addEventListener("click", () => {
      if (!state.kindsOpen.delete(openKey)) state.kindsOpen.add(openKey);
      renderBoard();
    });
    const cpus = members.map((m) => m.cpu).filter((x) => x !== null && x >= 0)
      .sort((a, b) => a - b);
    label.append(ex, `${kindName(c0.type, c0.scope, members.length)}`);
    if (cpus.length) label.append(el("span", "hint", ` cpu ${cpuList(cpus)} · mean`));
    cname.append(label);
    sub.append(cname, ...blanks());
    metricCells(sub, meanRow(members));
    tbody.append(sub);
    if (open) for (const c of members) record(c, true);
  }
}

// One row of the board grouped by CPU: the median of its runs, unfolding to
// the runs themselves, each with where it stands among them.
function renderGroup(tbody, g, number) {
  const key = groupKey(g);
  const open = state.expanded.has(key);
  const tr = el("tr", "run-row cpu-row");
  tr.append(el("td", "num", number), groupBox(g));
  const name = el("td", "wide");
  const head = el("div", "run-head");
  if (g.runs > 1) {
    const ex = el("button", "expander", open ? "▾" : "▸");
    ex.type = "button";
    ex.title = open ? "hide the runs" : "show each run on this CPU";
    ex.setAttribute("aria-expanded", String(open));
    ex.addEventListener("click", () => toggleGroup(g).catch((e) => alert(e.message)));
    // The name folds the row as well as the arrow: it is the bigger target.
    // The arrow stays the keyboard's way in, being the one that is a button.
    const cpu = el("strong", "foldname", machineName(g));
    cpu.addEventListener("click", () => toggleGroup(g).catch((e) => alert(e.message)));
    head.append(ex, cpu);
  } else {
    head.append(el("span", "expander blank", " "),
                g.run_id ? runLink(g.run_id, machineName(g)) : machineName(g));
  }
  name.append(head);
  if (g.cpus_offline) name.append(offlineNote(g));
  // The boards they ran on are in its tooltip: listed out, several of them
  // made the row mostly text.
  const runs = el("span", "label", g.runs > 1 ? `median of ${g.runs} runs` : "1 run");
  if (g.runs > 1 && g.boards && g.boards.length) runs.title = `on ${g.boards.join(", ")}`;
  name.append(runs);
  tr.append(name);
  const systems = (g.sysnames || []).map((s) => osName({ sysname: s })).join(", ");
  tr.append(el("td", "", systems || "—"), el("td", "", g.target || "—"),
            el("td", "flags", flagsText(g)), el("td", "num fit"));
  metricCells(tr, g);
  tbody.append(tr);
  if (!open) return;
  for (const r of state.children.get(key) || []) tbody.append(runRow(r, "", true));
}

// The record that stands for one run at the board's metric, in a given scope --
// the same pick the leaderboard makes, asked for one run at a time.
async function representative(runId, scope) {
  // Built from the ranking alone, not from state.params: the board's filters
  // narrow what is ranked, and a ticked run must not vanish because the search
  // box no longer matches it.
  const p = new URLSearchParams();
  p.set("run", String(runId));
  p.set("scope", scope);
  p.set("group", "run");
  p.set("sort", state.sort);
  p.set("order", state.order);
  // Per GHz a run is stood for by its most efficient record rather than its
  // fastest, so the pick has to be asked for the same way the board asked.
  p.set("norm", state.norm);
  p.set("limit", "1");
  const { cores } = await api("/api/cores?" + p.toString());
  return cores[0] || null;
}

// One comparison, one scope. A 'total' row is the sum over every thread, so
// setting it beside a single core's numbers compares a machine with a part of
// one -- the bars would be true and the reading of them false. Changing scope
// therefore carries the selection across rather than dropping it: the same
// machines stay ticked, as their record in the scope now being ranked, and the
// rows they were ticked on lose their tick. A run with nothing in the new scope
// (a --per-core upload has no whole-machine total) has to leave, and says so.
async function retargetSelection(scope) {
  const rows = [...state.selected.values()];
  if (!scope || !rows.length || rows.every((r) => r.scope === scope)) return;
  const next = new Map();
  const lost = [];
  for (const r of rows) {
    if (r.scope === scope) {
      next.set(r.id, r);
      continue;
    }
    const rec = await representative(r.run_id, scope);
    if (rec) next.set(rec.id, rec);
    else lost.push(machineName(r));
  }
  state.selected = next;
  state.scopeNote = lost.length
    ? `${lost.join(", ")} has no ${SCOPE_LABELS[scope] || scope} record, so it ` +
      `left the comparison when the ranking changed.`
    : "";
  syncHash();
}

async function toggleSelect(row, on) {
  if (on) {
    // Ticking across scopes moves the whole comparison to the new one rather
    // than mixing the two.
    await retargetSelection(row.scope);
    // The board disables the box once the comparison is full; this is for
    // whatever else calls here.
    if (!state.selected.has(row.id) && compareFull()) {
      renderBoard();
      throw new Error(`${FULL_NOTE} Remove one to add another.`);
    }
    state.selected.set(row.id, row);
  } else {
    state.selected.delete(row.id);
    if (!state.selected.size) state.scopeNote = "";
  }
  syncHash();
  renderSelection();
  // A record can appear twice -- as a run's representative and in its expanded
  // list -- so redraw to keep both boxes agreeing.
  renderBoard();
}

// Keep what the page is showing in the URL, so a comparison or one
// submitter's board can be shared as a link.
function syncHash() {
  const parts = [];
  if (state.submitter) parts.push(`user=${state.submitter}`);
  const ids = [...state.selected.keys()];
  if (ids.length) parts.push(`compare=${ids.join(",")}`);
  // The run open over the page, so the address bar is its link while it is.
  if (state.openRun) parts.push(`run=${state.openRun}`);
  const want = parts.length ? "#" + parts.join("&") : "";
  if (location.hash !== want) {
    history.replaceState(null, "", location.pathname + want);
  }
}

function showTab(name) {
  for (const b of document.querySelectorAll("#tabs button")) {
    b.classList.toggle("active", b.dataset.tab === name);
  }
  for (const s of document.querySelectorAll("main .tab")) {
    s.classList.toggle("active", s.id === name);
  }
}

// ---------------------------------------------------------------------------
// Compare
// ---------------------------------------------------------------------------

// "Detailed" is one setting with a box on the board and a box in Compare, so
// that neither panel makes you leave it to change what it is showing. Both
// boxes are set here, or the one you did not touch would soon be lying about
// what the other panel prints.
function setDetailed(on) {
  state.detailed = !!on;
  for (const box of [$("#filters").elements.detailed, $("#compare-detailed")]) {
    if (box) box.checked = state.detailed;
  }
  renderBoard();
  renderSelection();
}

function renderSelection() {
  const n = state.selected.size;
  $("#selcount").textContent = n ? `(${n}${n >= MAX_COMPARE ? ` of ${MAX_COMPARE}` : ""})` : "";
  $("#clearsel").hidden = n === 0;
  // The bar controls a comparison, so it appears with one and not before.
  $("#compare-bar").hidden = n === 0;
  $("#compare-note").hidden = n > 0;

  const body = $("#compare-body");
  const warn = $("#compare-warn");
  body.replaceChildren();
  warn.replaceChildren();
  if (state.scopeNote) warn.append(el("p", "warn", state.scopeNote));
  if (!n) return;
  if (n >= MAX_COMPARE) {
    warn.append(el("p", "note", `${FULL_NOTE} Remove one with its × to add another.`));
  }

  const rows = [...state.selected.values()];

  const builds = new Set(rows.map((r) => `${r.vectorize}/${r.fma}`));
  if (builds.size > 1) {
    warn.append(el("p", "warn",
      "These results were built with different vectorize/FMA settings. Those " +
      "switches let one instruction do more arithmetic at a time, so the " +
      "compute numbers are not measuring the same amount of work."));
  }
  // The backstop. retargetSelection keeps the selection to one scope at every
  // door into it, so this should not fire -- but a mixed comparison reads as a
  // result rather than as a mistake, and that is not a thing to leave to an
  // invariant holding.
  const scopes = new Set(rows.map((r) => r.scope));
  if (scopes.size > 1) {
    warn.append(el("p", "warn",
      "Whole-machine rows mixed with single-core ones. A whole-machine row is " +
      "every thread added together, so it will tower over the rest."));
  }

  const head = el("div", "cmp-head");
  // The chips do not print the build, so a label's own build stays on it
  // exactly when the comparison mixes builds -- the only time it tells them apart.
  const sameBuild = builds.size <= 1;
  rows.forEach((r, i) => {
    const chip = el("div", `chip c${i % MAX_COMPARE}`);
    const label = shownLabel(r, sameBuild);
    const named = label || machineName(r);

    // The label leads. It is the name its owner gave the machine, and it was
    // the one thing this panel did not print: four bars against four rows all
    // reading "unnamed aarch64 CPU" are four bars nobody can tell apart. The
    // CPU model stays directly under it, since that is what the label means to
    // everyone who did not write it.
    const top = el("div", "chip-top");
    const title = runLink(r.run_id, named);
    title.title = "open this result";
    // Dropping a row from here rather than going back to find its tick on the
    // board, which on page three of a long board is a hunt.
    const drop = el("button", "chip-x", "×");
    drop.type = "button";
    drop.title = "remove from the comparison";
    drop.setAttribute("aria-label", `remove ${named}`);
    drop.addEventListener("click", () => toggleSelect(r, false).catch((e) => alert(e.message)));
    top.append(title, drop);
    chip.append(top);

    if (label) chip.append(el("span", "chip-sub", machineName(r)));
    head.append(chip);
  });
  body.append(head);

  // The headline metrics unless the Detailed box is ticked, the same as the
  // board: fifteen stacks of bars is a page you scroll rather than read, and
  // the box is right there when the rest of them are the point.
  for (const m of shownMetrics()) {
    const vals = rows.map((r) => value(r, m));
    if (vals.every((v) => v === null)) continue;
    const finite = vals.filter((v) => v !== null);
    const best = m.better === "low" ? Math.min(...finite) : Math.max(...finite);
    const max = Math.max(...finite);
    const dir = direction(m);

    const block = el("div", "cmp-metric");
    const h = el("div", "cmp-title");
    h.append(el("strong", null, m.label), el("span", "munit", unitOf(m)));
    if (dir) h.append(el("span", "hint", dir.text));
    if (m.aside) h.append(el("span", "hint aside-note", m.aside));
    block.append(h);

    rows.forEach((r, i) => {
      const v = vals[i];
      const line = el("div", "bar-row");
      const bar = el("div", "bar");
      const fill = el("div", `fill c${i % MAX_COMPARE}`);
      // The bar is the measurement, not a verdict: its length is the value
      // against the largest in the selection, whichever way the metric runs.
      // Which end wins is said in words above the group, so a 384 ns latency
      // draws the long bar its number deserves and the "best" mark beside the
      // 109 ns one says who took it.
      const frac = v === null || !max ? 0 : v / max;
      fill.style.width = frac ? `${Math.max(1, frac * 100)}%` : "0%";
      bar.append(fill);
      line.append(bar, el("span", "bar-val", fmt(v)));
      // Always present, even when empty: a missing cell would let one row's
      // bar run wider than the rest and break the shared scale by eye.
      const rel = v !== null && best && m.better !== "none"
        ? (m.better === "low" ? best / v : v / best) : null;
      line.append(el("span", "bar-rel", rel === null ? ""
        : rel >= 0.999 ? "best" : `${(rel * 100).toFixed(0)}%`));
      block.append(line);
    });
    body.append(block);
  }
}

// ---------------------------------------------------------------------------
// Run detail
// ---------------------------------------------------------------------------

function metaList(pairs) {
  const dl = el("dl", "meta");
  for (const [k, v] of pairs) {
    if (!v) continue;
    const dd = el("dd");
    dd.append(v);       // text, or a node such as when() gives
    dl.append(el("dt", null, k), dd);
  }
  return dl;
}

async function showRun(id) {
  const run = await api(`/api/runs/${id}`);
  const rank = await api(`/api/runs/${id}/rank`).catch(() => ({}));
  const box = $("#detail-body");
  box.replaceChildren();

  const title = el("h2", null, machineName(run, `run ${run.id}`));
  box.append(title);
  const share = el("button", "linkish copylink", "copy link");
  share.type = "button";
  share.addEventListener("click", () =>
    copyText(`${location.origin}${location.pathname}#run=${run.id}`, share));
  box.append(share);
  if (run.label) box.append(el("p", "label big", run.label));
  if (run.hidden_at) box.append(el("p", "notes hiddennote",
    "Hidden by whoever uploaded it: it is not on the board and is not " +
    "compared with other runs. This link still works."));
  if (run.cpus_offline) box.append(el("p", "notes", offlineText(run) +
    ". It was measured with less than the whole machine, so it is not compared " +
    "with other runs of its CPU."));
  if (run.notes) box.append(el("p", "notes", run.notes));

  // What identifies the run, then everything that qualifies it. The second set
  // is what you go looking for once a number surprises you, not what you read
  // on the way in, so it starts folded.
  box.append(metaList([
    ["uploaded", run.created_at && when(run.created_at)],
    ["submitted by", run.user ? submitterLink(run.user) : "anonymous"],
    ["system", [run.sysname, run.os_release, run.machine].filter(Boolean).join(" ")],
  ]));

  const rest = metaList([
    ["mode", run.mode],
    ["arch", run.target],
    ["build", `${run.compiler || "?"}${run.compiler_version && run.compiler_version !== run.compiler ? ` (${run.compiler_version})` : ""}` +
              ` · ${run.vectorize ? "vectorize on" : "vectorize off"} · ${run.fma ? "FMA on" : "FMA off"}`],
    ["flags", run.build_flags ? `${run.cc || "cc"} ${run.build_flags}` : null],
    ["binary", run.binary_sha256
      ? `sha256:${run.binary_sha256}` +
        (run.release_build
          ? ` — matches a ${run.release_build} binary, as reported by the run`
          : " — not a published build, or a local one")
      : null],
    ["config", `${run.threads ?? "?"} threads · ${run.seconds ?? "?"}s/phase × ${run.reps ?? "?"} reps · ` +
                `${run.mem_bytes ? (run.mem_bytes / 1048576).toFixed(0) + " MiB/thread" : "no memory phases"} · ` +
                `pin ${run.pin ? "on" : "off"}`],
    ["DRAM", run.dram_name ? `${run.dram_name} at ${fmt(run.dram_mhz_min, 0)}–${fmt(run.dram_mhz_max, 0)} MHz` : null],
    ["checksum", run.checksum],
  ]);
  if (rest.childElementCount) {
    const more = el("details", "more");
    more.append(el("summary", null, "Build, config and checksum"), rest);
    box.append(more);
  }

  if (rank.metrics && Object.keys(rank.metrics).length) {
    const list = el("div", "percentiles");
    for (const m of shownMetrics()) {
      const r = rank.metrics[m.key];
      if (!r || r.percentile === null) continue;
      const dir = direction(m);
      const line = el("div", "pct-row");
      line.append(markAside(el("span", "pct-name", m.label), m));
      // These bars are a standing, not a measurement, and a standing has only
      // one good end however the metric runs -- so the direction is said here
      // too, to keep it from being read as one of the value bars in Compare.
      line.append(el("span", "pct-dir", dir ? dir.text : ""));
      const bar = el("div", "bar");
      const fill = el("div", "fill c0");
      fill.style.width = `${Math.max(1, r.percentile)}%`;
      bar.append(fill);
      line.append(bar);
      line.append(el("span", "pct-val",
        `beats ${r.percentile.toFixed(0)}% of ${r.population} run${r.population === 1 ? "" : "s"}`));
      list.append(line);
    }
    // One value per run, so a big core is not counted as beating its own
    // little ones, or every little core on the hub.
    if (list.childElementCount) {
      const what = rank.scope === "thread" ? "thread" : "core";
      box.append(el("h3", null, "Against every run"), list,
                 el("p", "note", `Its best ${what} against the best ${what} of every ` +
                                 "other run built with the same Vectorize and FMA."));
    }
  }

  if (rank.cpu && rank.cpu.scopes && Object.keys(rank.cpu.scopes).length) {
    box.append(sameCpu(rank.cpu.scopes));
  }

  box.append(el("h3", null, "Records"));
  box.append(recordsTable(run));
  if (run.cores.some((c) => c.scope === "thread")) {
    box.append(el("p", "note legend",
      "A thread's MEM is its share while every thread ran at once; the " +
      "whole-machine row is their sum."));
  }
  box.append(el("p", "note legend", "↑ higher is better · ↓ lower is better"));

  const dl = el("a", "linkish", "download the original JSON");
  dl.href = `/api/runs/${run.id}/raw`;
  dl.setAttribute("download", `cpcpub-run-${run.id}.json`);
  const dlp = el("p");
  dlp.append(dl);
  box.append(dlp);

  $("#detail").hidden = false;
  state.openRun = run.id;
  syncHash();
}

function closeRun() {
  $("#detail").hidden = true;
  state.openRun = null;
  syncHash();
}

// Where this run stands among the runs on the same CPU with the same build:
// its whole machine and its best core, each against the best of them.
function sameCpu(scopes) {
  const wrap = el("div");
  wrap.append(el("h3", null, "Against the same CPU"));
  const names = { total: "whole machine", cpu: "best core" };
  const order = ["total", "cpu"].filter((k) => scopes[k]);
  const table = el("table", "fit-table");
  const hr = el("tr");
  hr.append(el("th"));
  for (const k of order) hr.append(el("th", "num", `${names[k]} · ${scopes[k].runs} runs`));
  const thead = el("thead");
  thead.append(hr);
  const tbody = el("tbody");
  for (const m of shownMetrics()) {
    if (m.better === "none" || order.every((k) => !scopes[k].metrics[m.key])) continue;
    const tr = el("tr");
    tr.append(markAside(el("td", null, m.label), m));
    for (const k of order) {
      const f = scopes[k].metrics[m.key];
      const b = f ? behind(f, m.better) : null;
      const td = el("td", "num fit");
      if (b) {
        td.textContent = b.placed ? b.text : `${b.text} · ${ordinal(f.rank)}`;
        const cls = behindClass(b);
        if (cls) td.classList.add(cls);
        td.title = `${fmt(f.value)}; these runs went from ${fmt(f.low)} to ${fmt(f.high)}, ` +
                   `with a median of ${fmt(f.median)}`;
      }
      tr.append(td);
    }
    tbody.append(tr);
  }
  table.append(thead, tbody);
  const scroller = el("div", "scroller");
  scroller.append(table);
  wrap.append(scroller);
  wrap.append(el("p", "note legend",
    `Against those runs: Top is the best or within ${TOP_BAND}% of it, then 2nd and ` +
    "3rd, then how far behind the best (−8%)."));
  return wrap;
}

// A run's records, the whole machine first, then each kind of core and each
// kind of thread as one averaged row that unfolds to its records.
function recordsTable(run) {
  const scroller = el("div", "scroller");
  const table = el("table");
  const thead = el("thead");
  const hr = el("tr");
  hr.append(el("th", null, "scope"), el("th", "num", "cpu"), el("th", "num", "MHz"));
  for (const m of shownMetrics()) {
    const dir = direction(m);
    const th = el("th", "num", dir && dir.mark ? `${m.label} ${dir.mark}` : m.label);
    if (dir) th.title = `${m.label} — ${dir.text}`;
    hr.append(markAside(th, m));
  }
  thead.append(hr);
  const tbody = el("tbody");
  // The run page shows the figures as measured, whatever the board reads.
  const raw = (rec) => {
    const out = { mhz: rec.mhz };
    for (const m of state.metrics) out[m.key] = rec[m.key];
    return out;
  };
  const line = (name, cpu, rec, cls) => {
    const tr = el("tr", cls);
    tr.append(name, el("td", "num", cpu), el("td", "num", rec.mhz ? Math.round(rec.mhz) : "—"));
    for (const m of shownMetrics()) tr.append(markAside(el("td", "num", fmt(rec[m.key])), m));
    tbody.append(tr);
    return tr;
  };
  const byId = new Map(run.cores.map((c) => [c.id, c]));
  const kinded = new Set((run.types || []).flatMap((t) => t.ids));
  for (const c of run.cores) {
    if (c.scope === "total") line(el("td", null, "total"), "—", c, "");
  }
  for (const scope of ["cpu", "thread"]) {
    for (const t of (run.types || []).filter((k) => k.scope === scope)) {
      const members = t.ids.map((i) => byId.get(i)).filter(Boolean);
      // A kind of one -- a phone's prime core -- is its own record, named.
      if (members.length === 1) {
        const one = members[0];
        line(el("td", null, `${t.name} ${scope === "thread" ? "thread" : "core"}`),
             one.cpu ?? "—", raw(one), "");
        continue;
      }
      const avg = { mhz: mean(members.map((r) => r.mhz)) };
      for (const m of state.metrics) avg[m.key] = mean(members.map((r) => r[m.key]));
      const name = el("td");
      const ex = el("button", "expander", "▸");
      ex.type = "button";
      ex.title = "show each of these";
      ex.setAttribute("aria-expanded", "false");
      name.append(ex, kindName(t.name, scope, members.length));
      line(name, cpuList(t.cpus) || "—", avg, "kind-row");
      const rows = members.map((r) =>
        line(el("td", "childname", recordName(r)), r.cpu ?? "—", raw(r), "child-row"));
      for (const r of rows) r.hidden = true;
      ex.addEventListener("click", () => {
        const open = rows[0].hidden;
        for (const r of rows) r.hidden = !open;
        ex.textContent = open ? "▾" : "▸";
        ex.setAttribute("aria-expanded", String(open));
      });
    }
    for (const c of run.cores) {
      if (c.scope === scope && !kinded.has(c.id)) {
        line(el("td", null, recordName(c)), c.cpu ?? "—", c, "");
      }
    }
  }
  table.append(thead, tbody);
  scroller.append(table);
  return scroller;
}

// ---------------------------------------------------------------------------
// The registration puzzle
// ---------------------------------------------------------------------------
//
// Creating an account costs a small proof of work, so that a script cannot
// take every name on the hub overnight. The page has to hash to solve it, and
// crypto.subtle is unavailable on a hub served over plain http -- it needs a
// secure context, which http://a-machine-on-my-network:8782 is not. So SHA-256
// is here, in about forty lines, and works wherever the page loads.

const SHA_K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
  0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
  0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
  0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
  0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
  0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
  0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
  0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
  0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

const rotr = (x, n) => ((x >>> n) | (x << (32 - n))) >>> 0;

// The digest of `bytes`, as eight 32-bit words. Words rather than bytes
// because the only question asked of it is how many leading zero bits it has.
function sha256(bytes) {
  const h = new Uint32Array([
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
    0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]);
  const total = ((bytes.length + 9 + 63) >> 6) << 6;
  const buf = new Uint8Array(total);
  buf.set(bytes);
  buf[bytes.length] = 0x80;
  const view = new DataView(buf.buffer);
  view.setUint32(total - 8, Math.floor((bytes.length * 8) / 4294967296));
  view.setUint32(total - 4, (bytes.length * 8) >>> 0);

  const w = new Uint32Array(64);
  for (let off = 0; off < total; off += 64) {
    for (let i = 0; i < 16; i++) w[i] = view.getUint32(off + i * 4);
    for (let i = 16; i < 64; i++) {
      const a = w[i - 15], b = w[i - 2];
      const s0 = rotr(a, 7) ^ rotr(a, 18) ^ (a >>> 3);
      const s1 = rotr(b, 17) ^ rotr(b, 19) ^ (b >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
    }
    let [a, b, c, d, e, f, g, hh] = h;
    for (let i = 0; i < 64; i++) {
      const t1 = (hh + (rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)) +
                  ((e & f) ^ (~e & g)) + SHA_K[i] + w[i]) >>> 0;
      const t2 = ((rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)) +
                  ((a & b) ^ (a & c) ^ (b & c))) >>> 0;
      hh = g; g = f; f = e; e = (d + t1) >>> 0;
      d = c; c = b; b = a; a = (t1 + t2) >>> 0;
    }
    const next = [a, b, c, d, e, f, g, hh];
    for (let i = 0; i < 8; i++) h[i] = (h[i] + next[i]) >>> 0;
  }
  return h;
}

function leadingZeros(words, bits) {
  let i = 0, left = bits;
  while (left >= 32) {
    if (words[i++] !== 0) return false;
    left -= 32;
  }
  return left === 0 || (words[i] >>> (32 - left)) === 0;
}

// Find a nonce the hub will accept, without freezing the tab: a slice of
// attempts, then back to the event loop so the progress line paints and the
// page stays answerable.
async function solveChallenge(challenge, bits, progress) {
  const enc = new TextEncoder();
  const limit = 1 << 26;                  // ~67M: far past any sane difficulty
  for (let n = 0; n < limit;) {
    for (let i = 0; i < 4096; i++, n++) {
      if (leadingZeros(sha256(enc.encode(`${challenge}:${n}`)), bits)) {
        return String(n);
      }
    }
    if (progress) progress(n);
    await new Promise((done) => setTimeout(done, 0));
  }
  throw new Error("could not solve the registration challenge");
}

// ---------------------------------------------------------------------------
// Accounts
// ---------------------------------------------------------------------------
//
// An account does three things and claims nothing else: it puts a name on a
// run, it lets that run be withdrawn from any browser rather than only from
// the one holding a delete token, and it carries an upload token so the
// machine that ran the benchmark can post its own result. Uploading without
// one still works everywhere it did.

async function loadUser() {
  try {
    const { user } = await api("/api/auth/me");
    state.user = user;
  } catch {
    state.user = null;          // a hub that has not been signed into is fine
  }
  renderWhoami();
  renderAccount();
  renderCommands();
  renderMine();
}

function renderWhoami() {
  const box = $("#whoami");
  box.replaceChildren();
  if (state.user) {
    const me = el("button", "linkish", state.user.name);
    me.type = "button";
    me.title = "your account";
    me.addEventListener("click", () => showTab("account"));
    box.append(el("span", "muted", "signed in as "), me);
    const out = el("button", "linkish", "sign out");
    out.type = "button";
    out.addEventListener("click", () => signOut().catch((e) => alert(e.message)));
    box.append(el("span", "muted", " · "), out);
    return;
  }
  const go = el("button", "linkish", "Sign in or create an account");
  go.type = "button";
  go.addEventListener("click", () => showTab("account"));
  box.append(go);
  box.append(el("span", "muted", " — optional; uploading works without one"));
}

async function signOut() {
  await api("/api/auth/logout", { method: "POST" });
  await loadUser();
  await loadBoard();
}

// One form for both doors. Registering and signing in ask for exactly the same
// two things, so they are the same fields with a different button rather than
// two forms side by side that a reader has to tell apart.
function authForm() {
  const wrap = el("div", "panel");
  wrap.append(el("h2", null, "Sign in"));
  wrap.append(el("p", "note",
    "An account is a name and a password — no email is asked for, and so " +
    "there is no password reset. It puts your name on the runs you upload, " +
    "lets you withdraw them from any browser, and gives you an upload token " +
    "for submitting straight from the machine you measured."));

  const form = el("form", "authform");
  const name = el("input");
  name.type = "text";
  name.id = "auth-name";
  name.autocomplete = "username";
  name.maxLength = 32;
  name.placeholder = "3–32 characters: letters, digits, . _ -";
  const nameLabel = el("label", null, "Name");
  nameLabel.append(name);

  const pw = el("input");
  pw.type = "password";
  pw.id = "auth-password";
  pw.autocomplete = "current-password";
  pw.placeholder = "at least 8 characters";
  const pwLabel = el("label", null, "Password");
  pwLabel.append(pw);

  const row = el("div", "buttons");
  const signin = el("button", null, "Sign in");
  signin.type = "submit";
  const register = el("button", "secondary", "Create account");
  register.type = "button";
  row.append(signin, register);

  // Only drawn once the hub says it wants one. Most do not.
  const invite = el("input");
  invite.type = "text";
  invite.id = "auth-invite";
  invite.maxLength = 128;
  const inviteLabel = el("label", null, "Invite code");
  inviteLabel.append(invite);
  inviteLabel.hidden = true;

  const msg = el("p", "authmsg");
  const signedIn = async (out) => {
    state.user = out.user;
    pw.value = "";
    renderWhoami();
    renderAccount();
    renderCommands();
    renderMine();
    await loadBoard();
  };
  const say = (text, warn) => {
    msg.className = warn ? "authmsg warn" : "authmsg";
    msg.textContent = text;
  };

  const signIn = async () => {
    say("");
    try {
      await signedIn(await api("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name.value.trim(), password: pw.value }),
      }));
    } catch (e) {
      say(e.message, true);
    }
  };

  // Registering is the same two fields plus a puzzle: ask the hub for one,
  // solve it here, and send the answer with the form. The challenge is spent
  // whatever the outcome, so each attempt fetches its own.
  const createAccount = async () => {
    say("");
    register.disabled = true;
    try {
      const policy = await api("/api/auth/challenge");
      if (!policy.open) throw new Error("this hub is not taking new accounts");
      const body = { name: name.value.trim(), password: pw.value };
      if (policy.invite_required) body.invite = invite.value.trim();
      if (policy.bits > 0) {
        say("proving you are not a script…");
        body.nonce = await solveChallenge(
          policy.challenge, policy.bits,
          (n) => say(`proving you are not a script… ${(n / 1000) | 0}k tries`));
        body.challenge = policy.challenge;
      }
      await signedIn(await api("/api/auth/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }));
      say("");
    } catch (e) {
      say(e.message, true);
    } finally {
      register.disabled = false;
    }
  };

  form.addEventListener("submit", (e) => { e.preventDefault(); signIn(); });
  register.addEventListener("click", createAccount);

  form.append(nameLabel, pwLabel, inviteLabel, row, msg);
  wrap.append(form);

  // What this hub asks of a new account, filled in when it answers. A closed
  // hub says so on the button rather than on the failure.
  //
  // The policy rather than a challenge: this runs for every visitor who draws
  // the form, and most of them are here to read the board. A puzzle fetched
  // here would be one nobody solves -- createAccount asks for its own -- and
  // paid for out of the same limit that registering needs.
  api("/api/auth/policy").then((policy) => {
    inviteLabel.hidden = !policy.invite_required;
    if (!policy.open) {
      register.disabled = true;
      register.title = "this hub is not taking new accounts";
      wrap.append(el("p", "note",
        "This hub is not taking new accounts. Uploading without one still " +
        "works and always will."));
    }
  }).catch(() => {});
  return wrap;
}

function accountPanel() {
  const u = state.user;
  const wrap = el("div", "panel");
  wrap.append(el("h2", null, u.name));
  wrap.append(el("p", "note",
    `Account since ${(u.created_at || "").slice(0, 10)} · ` +
    `${u.runs} run${u.runs === 1 ? "" : "s"} uploaded.`));

  const seeMine = el("button", "linkish", "See them on the leaderboard");
  seeMine.type = "button";
  seeMine.addEventListener("click", () => filterBySubmitter(u.name));
  const p = el("p");
  p.append(seeMine);
  wrap.append(p);

  wrap.append(el("h3", null, "Upload token"));
  wrap.append(el("p", "note",
    "Send this from the machine that ran the benchmark and the result lands " +
    "on this account with nothing to copy back. It grants uploading and " +
    "withdrawing as you and nothing else — it cannot change your password or " +
    "read anything the board does not already show."));
  wrap.append(tokenBox(u.api_token));

  const rotate = el("button", "danger", "Issue a new token");
  rotate.type = "button";
  rotate.addEventListener("click", async () => {
    if (!confirm("Issue a new token? Anything still using the old one will " +
                 "start being refused.")) return;
    try {
      const out = await api("/api/auth/token", { method: "POST" });
      state.user.api_token = out.api_token;
      renderAccount();
      renderCommands();
    } catch (e) {
      alert(e.message);
    }
  });
  const rp = el("p");
  rp.append(rotate);
  wrap.append(rp);

  wrap.append(el("h3", null, "Change password"));
  const pwForm = el("form", "authform");
  const cur = el("input");
  cur.type = "password";
  cur.autocomplete = "current-password";
  const curLabel = el("label", null, "Current password");
  curLabel.append(cur);
  const next = el("input");
  next.type = "password";
  next.autocomplete = "new-password";
  next.placeholder = "at least 8 characters";
  const nextLabel = el("label", null, "New password");
  nextLabel.append(next);
  const pwMsg = el("p", "authmsg");
  const pwGo = el("button", null, "Change password");
  pwGo.type = "submit";
  pwForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    pwMsg.className = "authmsg";
    try {
      await api("/api/auth/password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ current: cur.value, password: next.value }),
      });
      cur.value = next.value = "";
      pwMsg.className = "authmsg ok";
      pwMsg.textContent = "Password changed. Other browsers have been signed out.";
    } catch (err) {
      pwMsg.className = "authmsg warn";
      pwMsg.textContent = err.message;
    }
  });
  pwForm.append(curLabel, nextLabel, pwGo, pwMsg);
  wrap.append(pwForm);

  wrap.append(el("h3", null, "Change name"));
  const waitUntil = u.rename_after ? new Date(u.rename_after) : null;
  wrap.append(el("p", "note",
    "A name can be changed once every 7 days, and not to one another account " +
    "already has. Your runs keep their place and show the new name." +
    (waitUntil ? ` You can change it again after ${waitUntil.toLocaleString()}.` : "")));
  const nameForm = el("form", "authform");
  const newName = el("input");
  newName.autocomplete = "off";
  newName.placeholder = "3 to 32 letters, digits, . _ -";
  const newNameLabel = el("label", null, "New name");
  newNameLabel.append(newName);
  const nameCur = el("input");
  nameCur.type = "password";
  nameCur.autocomplete = "current-password";
  const nameCurLabel = el("label", null, "Password");
  nameCurLabel.append(nameCur);
  const nameMsg = el("p", "authmsg");
  const nameGo = el("button", null, "Change name");
  nameGo.type = "submit";
  nameGo.disabled = !!waitUntil && waitUntil > new Date();
  nameForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    nameMsg.className = "authmsg";
    const wanted = newName.value.trim();
    if (!confirm(`Change your name from "${u.name}" to "${wanted}"? ` +
                 "You will not be able to change it again for 7 days.")) return;
    try {
      const out = await api("/api/auth/rename", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: wanted, password: nameCur.value }),
      });
      if (state.submitter === u.name) state.submitter = out.user.name;
      state.user = Object.assign(state.user, out.user);
      renderWhoami();
      renderAccount();
      renderCommands();
      renderMine();
      await loadBoard();
    } catch (err) {
      nameMsg.className = "authmsg warn";
      nameMsg.textContent = err.message;
    }
  });
  nameForm.append(newNameLabel, nameCurLabel, nameGo, nameMsg);
  wrap.append(nameForm);

  const danger = el("details", "more");
  danger.append(el("summary", null, "Close this account"));
  danger.append(el("p", "note",
    "Closing the account deletes every run uploaded under it — they are not " +
    "left on the board without a name, because a run nobody can withdraw is " +
    "worse than no run. This cannot be undone."));
  const closeForm = el("form", "authform");
  const closePw = el("input");
  closePw.type = "password";
  closePw.autocomplete = "current-password";
  const closeLabel = el("label", null, "Password");
  closeLabel.append(closePw);
  const closeGo = el("button", "danger", "Close account and delete my runs");
  closeGo.type = "submit";
  const closeMsg = el("p", "authmsg");
  closeForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!confirm(`Delete the account "${u.name}" and all ${u.runs} of its runs?`)) return;
    try {
      const out = await api("/api/auth/close", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: closePw.value }),
      });
      state.user = null;
      state.submitter = "";
      renderWhoami();
      renderAccount();
      renderCommands();
      renderMine();
      await loadBoard();
      alert(`Account closed. ${out.runs_deleted} run(s) removed.`);
    } catch (err) {
      closeMsg.className = "authmsg warn";
      closeMsg.textContent = err.message;
    }
  });
  closeForm.append(closeLabel, closeGo, closeMsg);
  danger.append(closeForm);
  wrap.append(danger);
  return wrap;
}

function renderAccount() {
  const box = $("#account-body");
  box.replaceChildren();
  box.append(state.user ? accountPanel() : authForm());
}

// ---------------------------------------------------------------------------
// Submitting
// ---------------------------------------------------------------------------

// The two shell blocks on the Submit tab. Both carry this hub's address, and
// both carry the token when there is one to carry -- a command you have to
// edit before it works is a command most people will get wrong once.
// Said where someone is about to produce a result rather than only where they
// read one: every result here is self-reported, and that is the design and not
// a shortfall. This used to explain how to earn a signature instead, which sent
// people towards a CI setup most of them could not use and none of them needed.
function renderVerifiedNote() {
  const box = $("#verified-note");
  box.replaceChildren();
  const p = el("p", "note");
  p.append(
    el("span", null, "Results are "),
    el("strong", null, "self-reported"),
    el("span", null, ": nobody checks the numbers. The board does show which " +
                     "results come from a published binary, and those compare " +
                     "fairly with each other, so use one if you can."));
  box.append(p);

  if (state.downloads) {
    const line = el("p", "note");
    const link = el("a", "linkish", "Download tab");
    link.href = "#download";
    link.addEventListener("click", (e) => {
      e.preventDefault();
      showTab("download");
      window.scrollTo(0, 0);
    });
    line.append(el("span", null, "Get a published binary from the "), link,
                el("span", null, "."));
    box.append(line);
  } else if (state.release) {
    const line = el("p", "note");
    line.append(el("span", null, "Get a published binary from "));
    const link = el("a", "linkish", `the ${state.release.release} release`);
    if (state.release.url) {
      link.href = state.release.url;
      link.rel = "noopener noreferrer";
      link.target = "_blank";
    }
    line.append(link, el("span", null, "."));
    box.append(line);
  }
}

// What a batch may hold on this hub, said where the batch is explained.
function renderBatchLimits(b) {
  $("#batch-limits").textContent =
    `This hub takes up to ${b.max_results} results in one batch` +
    (b.per_hour > 0 ? `, and ${b.per_hour} an hour per account.` : ".");
}

function renderCommands() {
  const token = state.user && state.user.api_token;
  $("#cmd-submit").textContent =
    `cpcpub --full --submit ${location.origin}` +
    (token ? ` --token ${token}` : "") + " --label \"my box\"";

  $("#shell-note").textContent = token
    ? "The command includes your upload token, so the result goes to your " +
      "account. Keep the token private."
    : "Sign in to have results saved to your account. Without signing in, " +
      "the upload is anonymous and you get a delete token to keep.";

  $("#cmd-batch").textContent =
    "# on each machine, one file each\n" +
    "cpcpub --full --variants --json > \"$(hostname).json\"\n\n" +
    "# then, with the files together in one directory\n" +
    "cat *.json > batch.json\n" +
    `curl -fsS -X POST "${location.origin}/api/batch?label=first+machine&label=second" \\\n` +
    `     -H "Authorization: Bearer ${token || "YOUR-TOKEN"}" --data-binary @batch.json`;

  $("#cmd-curl").textContent =
    `curl -fsS -X POST "${location.origin}/api/runs?label=my+box" \\\n` +
    (token ? `     -H "Authorization: Bearer ${token}" \\\n` : "") +
    "     -H 'Content-Type: application/json' --data-binary @run.json";
}

// What was dropped or chosen, before anything is sent. Parsed here so a file
// that is not a result says so immediately rather than after a round trip, and
// so a --variants array can be seen for what it is: several runs in one file.
function takeFiles(files) {
  $("#uploadresult").replaceChildren();
  return Promise.all([...files].map(async (f) => ({ name: f.name, text: await f.text() })))
    .then(takeTexts);
}

// One result, or one machine's --variants array, goes up as it always has.
// Signed in, anything more -- several files, a catted file -- is a batch: the
// hub reads and groups it, and is asked first what it would make, so each
// machine can be named before anything is stored. Signed out, a file of
// results back to back is refused here, since only an account may send one.
async function takeTexts(texts) {
  state.files = [];
  state.batch = null;
  const read = texts.map((t) => {
    try {
      const doc = JSON.parse(t.text);
      return { name: t.name, text: t.text, docs: Array.isArray(doc) ? doc : [doc] };
    } catch (e) {
      return { name: t.name, text: t.text, parseError: e.message };
    }
  });
  const loose = read.some((f) => f.parseError);
  const count = read.reduce((n, f) => n + (f.docs ? f.docs.length : 0), 0);
  if (state.user && (loose || count > 1)) {
    state.batch = { text: read.map((f) => f.text).join("\n"), names: read.map((f) => f.name),
                    plan: null, labels: [], error: "" };
    renderPicked();
    return planBatch(state.batch);
  }
  state.files = read.map((f) => (f.parseError
    ? { name: f.name, error: `not one JSON document (${f.parseError}). A file of ` +
        "several results one after another is a batch: sign in to upload one." }
    : { name: f.name, docs: f.docs }));
  renderPicked();
}

// What the hub would make of a batch: its uploads, one per machine, and which
// results it already holds. Nothing is stored.
async function planBatch(b) {
  try {
    const plan = await api("/api/batch?dry=1", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ batch: b.text }),
    });
    if (state.batch !== b) return;      // something else was picked since
    b.plan = plan;
    b.labels = plan.uploads.map((u) => u.label || "");
  } catch (e) {
    if (state.batch !== b) return;
    b.error = e.message;
  }
  renderPicked();
}

// One document per upload, in the order they will be sent, each with the label
// it will carry. A --variants run is an array of four builds; the hub keys a
// run on the two build flags, so they go up as four runs -- which is what makes
// them comparable with each other at all.
function pending() {
  const base = $("#label").value.trim();
  const out = [];
  state.files.forEach((f, file) => {
    if (!f.docs) return;
    const many = f.docs.length > 1;
    f.docs.forEach((doc) => {
      const build = (doc && doc.build) || {};
      const variant = `${build.vectorize ? "vector" : "scalar"}-${build.fma ? "fma" : "nofma"}`;
      out.push({
        doc,
        source: f.name,
        variant: many ? variant : null,
        label: many ? `${base ? base + " " : ""}(${variant})` : base,
        machine: (doc && doc.system && doc.system.cpu_models) || f.name,
        file,                   // the runs of one file are one upload
      });
    });
  });
  return out;
}

function renderPicked() {
  const box = $("#picked");
  box.replaceChildren();
  // A batch names each machine on its own, so the one label box is put away.
  $("#label").closest("label").hidden = Boolean(state.batch);
  $("#uploadgo").disabled = false;
  if (state.batch) return renderBatch(box, state.batch);
  const errors = state.files.filter((f) => f.error);
  const jobs = pending();
  box.hidden = !state.files.length;
  for (const e of errors) {
    box.append(el("p", "warn", `${e.name}: ${e.error}`));
  }
  if (jobs.length) {
    box.append(el("p", "ok", jobs.length === 1
      ? `Ready: ${jobs[0].machine}`
      : `Ready: ${jobs.length} runs`));
    const ul = el("ul", "picked-list");
    for (const j of jobs) {
      const li = el("li", null, j.machine);
      if (j.variant) li.append(el("span", "muted", ` · ${j.variant}`));
      ul.append(li);
    }
    box.append(ul);
    if (jobs.length > 1) {
      box.append(el("p", "note",
        "The file has one result per build variant. Each is uploaded as its " +
        "own run."));
    }
  }
  $("#uploadgo").textContent = jobs.length > 1 ? `Upload ${jobs.length} runs` : "Upload";
}

// A batch as the hub would store it: a row per machine with its builds, and a
// label box each. Results the hub already holds are shown, struck through.
function renderBatch(box, b) {
  box.hidden = false;
  const go = $("#uploadgo");
  go.textContent = "Upload";
  const files = b.names.length > 1 ? `${b.names.length} files` : b.names[0];
  if (b.error) {
    box.append(el("p", "warn", `${files}: ${b.error}`));
    return;
  }
  if (!b.plan) {
    box.append(el("p", "muted", `Reading ${files}…`));
    return;
  }
  const { uploads, results, held } = b.plan;
  box.append(el("p", "ok",
    `Ready: ${plural(results, "result")} from ${plural(uploads.length, "machine")}` +
    (held ? `, ${held} of them already on the hub and skipped.` : ".")));
  box.append(el("p", "note",
    "Each machine's results, one per build, become one upload. Give each a " +
    "label, or leave it blank."));
  const list = el("div", "batch-plan");
  uploads.forEach((u, i) => {
    const row = el("div", "batch-up");
    const head = el("div", "batch-head");
    head.append(el("strong", null, machineName(u)),
                el("span", "muted", [osName(u, ""), u.mode,
                                     u.threads ? plural(u.threads, "thread") : ""]
                  .filter(Boolean).join(" · ")));
    const builds = el("div", "batch-builds");
    for (const r of u.runs) {
      const tag = el("span", r.duplicate_of ? "count held" : "count", r.variant);
      if (r.duplicate_of) tag.title = `already on the hub as run ${r.duplicate_of}: skipped`;
      builds.append(tag);
    }
    const input = el("input");
    input.type = "text";
    input.maxLength = 200;
    input.value = b.labels[i];
    input.placeholder = "label, e.g. rack 2 node 7";
    input.setAttribute("aria-label", `label for ${machineName(u)}`);
    if (u.runs.every((r) => r.duplicate_of)) {
      input.disabled = true;
      input.value = "";
      input.placeholder = "already on the hub";
    }
    input.addEventListener("input", () => { b.labels[i] = input.value; });
    row.append(head, builds, input);
    list.append(row);
  });
  box.append(list);
  const fresh = results - held;
  go.textContent = fresh ? `Upload ${plural(fresh, "result")}` : "Nothing new to upload";
  go.disabled = !fresh;
}

// A planned batch, sent. One request: the hub stores it all or none of it.
async function doBatch(b) {
  const out = $("#uploadresult");
  if (!b.plan) {
    out.append(el("p", "warn", b.error || "Still reading the batch; try again in a moment."));
    return;
  }
  const go = $("#uploadgo");
  go.disabled = true;
  let done = false;
  try {
    const res = await api("/api/batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ batch: b.text, labels: b.labels.map((l) => l.trim()),
                             notes: $("#notes").value.trim() }),
    });
    const card = el("div", "receipt");
    const made = res.uploads.filter((u) => u.id).length;
    card.append(el("p", res.stored ? "ok" : "warn", res.stored
      ? `Stored ${plural(res.stored, "result")} in ${plural(made, "upload")} on your ` +
        `account (${res.user}).` + (res.skipped ? ` ${res.skipped} already on the hub, skipped.` : "")
      : "Nothing stored: every result in it is already on the hub."));
    const ul = el("ul", "picked-list");
    res.uploads.forEach((u, i) => {
      const li = el("li");
      const name = machineName(b.plan.uploads[i]);
      const first = u.runs.find((r) => r.id);
      li.append(first ? runLink(first.id, name) : el("span", null, name));
      if (u.label) li.append(el("span", "muted", ` · ${u.label}`));
      li.append(el("span", "muted", first
        ? ` · ${plural(u.runs.filter((r) => r.id).length, "run")}`
        : " · already on the hub"));
      ul.append(li);
    });
    card.append(ul, el("p", "muted", "They are under My uploads, one entry per machine."));
    out.append(card);
    state.batch = null;
    $("#paste").value = "";
    $("#file").value = "";
    renderPicked();
    $("#picked").hidden = true;
    done = true;
    await loadUser();
    loadBoard().catch(() => {});
  } catch (e) {
    out.append(el("p", "warn", e.message));
  }
  // A refusal leaves the batch picked, to fix a label and press again.
  if (!done) go.disabled = false;
}

// Everything chosen, in order, reporting each as it lands. Sequential rather
// than parallel: the rate limiter counts uploads, and a four-variant run that
// half-succeeded because four requests raced is a confusing thing to explain.
async function doUpload(ev) {
  ev.preventDefault();
  const out = $("#uploadresult");
  out.replaceChildren();
  if (state.batch) return doBatch(state.batch);

  let jobs = pending();
  const pasted = $("#paste").value.trim();
  if (!jobs.length && pasted) {
    // Read as a dropped file is. A batch is planned first and sent on the
    // next press, once its machines can be named.
    await takeTexts([{ name: "pasted", text: pasted }]);
    if (state.batch) return;
    jobs = pending();
    if (!jobs.length) return;           // why is in the list above
  }
  if (!jobs.length) {
    out.append(el("p", "warn", "Drop a result file, choose one, or paste the JSON first."));
    return;
  }

  const notes = $("#notes").value.trim();
  const go = $("#uploadgo");
  go.disabled = true;
  let stored = 0;
  const firstOf = new Map();            // file -> its first stored run's id
  for (const job of jobs) {
    const p = new URLSearchParams();
    if (job.label) p.set("label", job.label);
    if (notes) p.set("notes", notes);
    try {
      const res = await api("/api/runs?" + p.toString(), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(job.doc),
      });
      stored++;
      out.append(uploadReceipt(job, res));
      if (!firstOf.has(job.file)) firstOf.set(job.file, res.id);
      if (!res.user) {
        rememberRun({ id: res.id, token: res.delete_token, name: job.machine,
                      at: new Date().toISOString(), label: job.label || "",
                      notes, variant: job.variant, batch: firstOf.get(job.file) });
      }
    } catch (e) {
      if (e.status === 409 && e.body?.id) {
        // Already here: a link to it is the useful answer, not a failure.
        const p = el("p", "warn", `${job.machine}: this result is already on the hub, as `);
        p.append(runLink(e.body.id, `run ${e.body.id}`), ".");
        out.append(p);
      } else {
        out.append(el("p", "warn", `${job.machine}: ${e.message}`));
      }
    }
  }
  go.disabled = false;
  if (stored) {
    state.files = [];
    $("#paste").value = "";
    $("#file").value = "";
    renderPicked();
    $("#picked").hidden = true;
    if (state.user) await loadUser();
    loadBoard().catch(() => {});
  }
}

// What one stored run says back. On an account that is a link and nothing to
// keep; anonymous it is a link and a token that is the only way back, so the
// token is shown as prominently as the success.
function uploadReceipt(job, res) {
  const card = el("div", "receipt");
  card.append(el("p", "ok",
    `Uploaded as run ${res.id}${job.variant ? ` (${job.variant})` : ""}.`));
  const line = el("p");
  line.append(runLink(res.id, "See it on the board"));
  card.append(line);
  if (res.user) {
    card.append(el("p", "muted",
      `Saved to your account (${res.user}). You can remove it from My uploads.`));
  } else {
    card.append(el("p", "muted",
      "Uploaded anonymously. Keep this delete token: it's the only way to " +
      "remove the run. It's also saved in this browser, under My uploads:"));
    card.append(tokenBox(res.delete_token));
  }
  return card;
}

// ---------------------------------------------------------------------------
// My uploads
// ---------------------------------------------------------------------------

function myRuns() {
  try {
    return JSON.parse(localStorage.getItem("cpcpub-uploads") || "[]");
  } catch {
    return [];
  }
}

function rememberRun(entry) {
  const all = myRuns();
  all.unshift(entry);
  localStorage.setItem("cpcpub-uploads", JSON.stringify(all.slice(0, 100)));
  renderMine();
}

function forgetRun(id) {
  localStorage.setItem("cpcpub-uploads",
    JSON.stringify(myRuns().filter((e) => e.id !== id)));
}

// After anything leaves the board: a withdrawn run may still be ticked into a
// comparison, and a comparison of rows that no longer exist is worse than an
// empty one.
async function afterDelete() {
  state.selected.clear();
  state.scopeNote = "";
  renderSelection();
  // loadUser() redraws My uploads itself, so signed in this is one redraw and
  // not two -- two of them racing is what used to list every upload twice.
  if (state.user) await loadUser();
  else renderMine();
  // The board is being refreshed, not re-asked: whoever was reading page four
  // of it stays on page four (or on the last one, if that page has just gone).
  loadBoard({ keepPage: true }).catch(() => {});
}

// Which rows are ticked in each half, by run id. Held outside the lists so a
// redraw -- a refetch finishing, another withdrawal landing -- keeps the ticks
// that were made before it.
const minePicked = { account: new Set(), local: new Set() };

// Which CPUs and uploads are unfolded and which upload is being edited, by
// key -- kept outside the lists for the same reason as the ticks. Everything
// starts folded: the list is a line per CPU until one is opened, and an upload
// shows its variants only once it is opened in turn.
const mineOpen = new Set();
let mineEditing = null;

const VARIANT_TAIL = /(?:^| )\((?:scalar|vector)-(?:fma|nofma)\)$/;

// A label without the "(vector-fma)" a --variants upload ends each run's with,
// and whether it had one. The server's split_variant(), for labels the page
// holds on its own.
function splitLabel(label) {
  const m = (label || "").match(VARIANT_TAIL);
  if (!m) return { base: label || "", tagged: false };
  return { base: label.slice(0, m.index), tagged: true };
}

// The label a run should carry once its upload is called `base`. The runs of a
// --variants upload each end theirs in their build, as cpcpub names them, so
// they stay told apart wherever the build is not on screen; a run that came
// alone is left without one unless it already had it.
function labelFor(base, run, many) {
  if (!run.variant || !(many || run.tagged)) return base;
  return `${base ? base + " " : ""}(${run.variant})`;
}

// A box for a set of runs: ticked when all of them are, a dash when some are,
// and ticking it ticks or unticks the lot.
function pickBox(ids, picked, redraw, what) {
  const cb = el("input", "pick");
  cb.type = "checkbox";
  const on = ids.filter((id) => picked.has(id)).length;
  cb.checked = ids.length > 0 && on === ids.length;
  cb.indeterminate = on > 0 && on < ids.length;
  cb.setAttribute("aria-label", what);
  cb.addEventListener("change", () => {
    for (const id of ids) {
      if (cb.checked) picked.add(id);
      else picked.delete(id);
    }
    redraw();
  });
  return cb;
}

function foldArrow(open, toggle, show, hide) {
  const ex = el("button", "expander", open ? "▾" : "▸");
  ex.type = "button";
  ex.title = open ? hide : show;
  ex.setAttribute("aria-expanded", String(open));
  ex.addEventListener("click", toggle);
  return ex;
}

function mutedWhen(iso) {
  const at = when(iso);
  at.className = "muted";
  return at;
}

// Withdrawing whatever is ticked. One confirmation for the lot and then one
// request each -- there is no batch endpoint, and a run that will not go
// (someone else withdrew it, a token no longer matches) has to be named rather
// than folded into one word about all of them.
async function withdrawPicked(entries, picked, remove, redraw) {
  const chosen = entries.filter((e) => picked.has(e.id));
  // Both ways out redraw: the button was disabled on the way in, and a list
  // left with a dead button after a cancelled confirmation is worse than the
  // wasted redraw.
  if (!chosen.length) return redraw();
  if (!confirm(chosen.length === 1
      ? `Withdraw run ${chosen[0].id} from the leaderboard?`
      : `Withdraw these ${chosen.length} runs from the leaderboard?`)) {
    return redraw();
  }
  const failed = [];
  for (const e of chosen) {
    try {
      await remove(e);
      picked.delete(e.id);
      forgetRun(e.id);
    } catch (err) {
      failed.push(`run ${e.id}: ${err.message}`);
    }
  }
  if (failed.length) {
    alert(`${chosen.length - failed.length} of ${chosen.length} withdrawn. ` +
          `Could not withdraw:\n${failed.join("\n")}`);
  }
  redraw();
  await afterDelete();
}

// Hiding or showing whatever is ticked, one request a run, as withdrawing is.
// Only the runs that would change are asked about, so ticking a whole CPU
// and pressing Hide leaves the ones already hidden alone.
async function hidePicked(entries, picked, hidden, ctx) {
  const chosen = entries.filter((e) => picked.has(e.id) && Boolean(e.hidden) !== hidden);
  const failed = [];
  for (const e of chosen) {
    try {
      await ctx.hide(e, hidden);
    } catch (err) {
      failed.push(`run ${e.id}: ${err.message}`);
    }
  }
  if (failed.length) {
    alert(`${chosen.length - failed.length} of ${chosen.length} changed. ` +
          `Could not change:\n${failed.join("\n")}`);
  }
  picked.clear();
  await ctx.reload();
  loadBoard({ keepPage: true }).catch(() => {});
}

// Put anonymous runs on the signed-in account by their delete tokens. What
// moved is the account's now and is listed there, so this browser stops
// keeping it as its own: the account withdraws it without the token.
async function adoptTokens(tokens) {
  const res = await api("/api/auth/adopt", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tokens }),
  });
  for (const id of [...res.adopted, ...res.already]) {
    forgetRun(id);
    minePicked.local.delete(id);
  }
  await loadUser();                     // redraws both halves of My uploads
  loadBoard({ keepPage: true }).catch(() => {});   // the board names submitters
  return res;
}

function adoptedText(res) {
  const parts = [`${plural(res.adopted.length, "run")} added to your account.`];
  if (res.already.length) parts.push(`${plural(res.already.length, "run")} already on it.`);
  if (res.refused) {
    parts.push(`${res.refused} token${res.refused === 1 ? "" : "s"} matched nothing ` +
               "this account can take: withdrawn, mistyped, or another account's.");
  }
  return parts.join(" ");
}

async function adoptPicked(entries, picked) {
  const chosen = entries.filter((e) => picked.has(e.id) && e.token);
  if (!chosen.length) return drawLocalRuns();
  try {
    const res = await adoptTokens(chosen.map((e) => e.token));
    if (res.refused) alert(adoptedText(res));
  } catch (err) {
    alert(err.message);
    drawLocalRuns();
  }
}

// The account's box for tokens kept somewhere else: printed by cpcpub
// --submit, saved by the desktop or Android app, or copied from another
// browser. Made once and put back on every redraw, so a tick elsewhere in the
// list does not empty what was pasted into it.
let adoptBox = null;

function adoptForm() {
  if (adoptBox) return adoptBox;
  const more = el("details", "more mine-adopt");
  more.append(el("summary", null, "Add anonymous uploads by their delete tokens"));
  const form = el("form", "mine-edit");
  form.append(el("p", "note",
    "Uploaded without signing in, from the command line, the desktop or Android " +
    "app, or another browser? Paste their delete tokens here, one per line, and " +
    "they move to this account. A run already on another account stays there."));
  const box = el("textarea");
  box.rows = 3;
  box.placeholder = "delete tokens";
  box.setAttribute("aria-label", "delete tokens");
  const go = el("button", null, "Add to my account");
  go.type = "submit";
  const msg = el("p", "authmsg");
  const buttons = el("div", "buttons");
  buttons.append(go);
  form.append(box, buttons, msg);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const tokens = [...new Set(box.value.split(/[\s,]+/).filter(Boolean))];
    msg.className = "authmsg";
    if (!tokens.length) {
      msg.textContent = "Paste at least one token.";
      return;
    }
    go.disabled = true;
    msg.textContent = "Adding…";
    try {
      const res = await adoptTokens(tokens);
      msg.className = res.adopted.length ? "authmsg ok" : "authmsg warn";
      msg.textContent = adoptedText(res);
      if (!res.refused) box.value = "";
    } catch (err) {
      msg.className = "authmsg warn";
      msg.textContent = err.message;
    }
    go.disabled = false;
  });
  more.append(form);
  adoptBox = more;
  return more;
}

// Both halves of My uploads hold the same shape: a CPU, the uploads made on
// it, and the runs each upload sent -- one, or one per build variant. `ctx`
// is what differs between the halves: which ticks they keep, how a run is
// withdrawn or edited, and whether the delete tokens are shown.
//
//   cpu    { key, name, uploads }
//   upload { key, label, notes, at, runs, known }
//   run    { id, at, variant, tagged, token, hidden }

// The form that renames an upload and rewrites its notes. One label for the
// upload, put on each of its runs with that run's build after it.
function uploadEditor(up, ctx) {
  const form = el("form", "mine-extra mine-edit");
  const labelIn = el("input");
  labelIn.type = "text";
  labelIn.maxLength = 200;
  labelIn.value = up.label || "";
  labelIn.placeholder = "e.g. workstation, quiet, performance governor";
  const notesIn = el("textarea");
  notesIn.rows = 3;
  notesIn.maxLength = 2000;
  notesIn.value = up.notes || "";
  notesIn.placeholder = "cooling, kernel, anything that explains the numbers";
  const labelBox = el("label", null, "Label");
  labelBox.append(labelIn);
  const notesBox = el("label", null, "Notes");
  notesBox.append(notesIn);
  form.append(labelBox, notesBox);
  if (up.runs.length > 1) {
    form.append(el("p", "note",
      `Changes all ${up.runs.length} runs of this upload. Each keeps its build ` +
      `after the label, as in "${labelFor(labelIn.value.trim() || "label",
                                           up.runs[0], true)}".`));
  }
  const save = el("button", null, "Save");
  save.type = "submit";
  const cancel = el("button", "secondary", "Cancel");
  cancel.type = "button";
  cancel.addEventListener("click", () => {
    mineEditing = null;
    ctx.redraw();
  });
  const buttons = el("div", "buttons");
  buttons.append(save, cancel);
  const msg = el("p", "authmsg");
  form.append(buttons, msg);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    save.disabled = true;
    msg.className = "authmsg";
    msg.textContent = "Saving…";
    try {
      await ctx.edit(up, labelIn.value.trim(), notesIn.value.trim());
      mineEditing = null;
      ctx.redraw();
      // The board shows labels too, and searches them.
      loadBoard({ keepPage: true }).catch(() => {});
    } catch (err) {
      save.disabled = false;
      msg.className = "authmsg warn";
      msg.textContent = err.message;
    }
  });
  setTimeout(() => labelIn.focus(), 0);
  return form;
}

// PATCH every run of an upload, each with its own label. One request a run:
// a run that would not change is named rather than folded into the rest.
async function patchUpload(up, label, notes, headers = () => ({})) {
  const many = up.runs.length > 1;
  const failed = [];
  for (const run of up.runs) {
    try {
      await api(`/api/runs/${run.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", ...headers(run) },
        body: JSON.stringify({ label: labelFor(label, run, many), notes }),
      });
    } catch (err) {
      failed.push(`run ${run.id}: ${err.message}`);
    }
  }
  if (failed.length) {
    throw new Error(`${up.runs.length - failed.length} of ${up.runs.length} ` +
                    `changed. Could not change ${failed.join("; ")}`);
  }
}

// One variant of an upload, under it once it is unfolded.
function runLine(run, ctx) {
  const row = el("div", "mine-row mine-run");
  row.append(pickBox([run.id], ctx.picked, ctx.redraw, `select run ${run.id}`),
             el("span", "expander blank", " "));
  const main = el("div", "mine-main");
  // An entry this browser kept before it recorded variants has only its id.
  if (run.variant) main.append(runLink(run.id, run.variant), el("span", "muted", `run ${run.id}`));
  else main.append(runLink(run.id, `run ${run.id}`));
  main.append(mutedWhen(run.at));
  if (run.hidden) main.append(hiddenTag("hidden"));
  row.append(main);
  if (ctx.tokens && run.token) {
    const tok = el("div", "mine-extra");
    tok.append(tokenBox(run.token));
    row.append(tok);
  }
  return row;
}

function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

function hiddenTag(text) {
  const tag = el("span", "count hiddentag", text);
  tag.title = "hidden: not on the board and not compared with other runs";
  return tag;
}

// "hidden" when all of these runs are, "2 of 4 hidden" when some are.
function hiddenText(runs) {
  const n = runs.filter((r) => r.hidden).length;
  if (!n) return null;
  return n === runs.length ? "hidden" : `${n} of ${runs.length} hidden`;
}

// One upload: its label, when it went up, its notes, and its runs folded
// under it -- one run or one per build variant, opened the same way either way.
function uploadBlock(up, ctx) {
  const key = `${ctx.prefix}|${up.key}`;
  const open = mineOpen.has(key);
  const toggle = () => {
    if (!mineOpen.delete(key)) mineOpen.add(key);
    ctx.redraw();
  };
  const row = el("div", "mine-row mine-upload");
  row.append(pickBox(up.runs.map((r) => r.id), ctx.picked, ctx.redraw,
                     `select the ${plural(up.runs.length, "run")} of this upload`),
             foldArrow(open, toggle, "show the runs of this upload", "hide the runs"));

  const main = el("div", "mine-main");
  // A browser entry from before labels were kept here may well have one; it is
  // fetched when the upload is edited, and not claimed absent until then.
  const title = el("strong", "foldname",
                   up.label || (up.known === false ? "Label not recorded" : "No label"));
  title.addEventListener("click", toggle);
  if (!up.label) title.classList.add("unlabelled");
  main.append(title, mutedWhen(up.at),
              el("span", "count", plural(up.runs.length, "variant")));
  const hid = hiddenText(up.runs);
  if (hid) main.append(hiddenTag(hid));
  if (ctx.edit) {
    const edit = el("button", "mine-editbtn", "Edit");
    edit.type = "button";
    edit.title = "change the label and notes";
    edit.disabled = mineEditing === key;
    edit.addEventListener("click", async () => {
      edit.disabled = true;
      try {
        if (ctx.prepare) await ctx.prepare(up);
        mineEditing = key;
      } catch (err) {
        alert(err.message);
      }
      ctx.redraw();
    });
    main.append(edit);
  }
  row.append(main);

  if (up.notes) row.append(el("div", "mine-extra mine-notes", up.notes));
  if (mineEditing === key) row.append(uploadEditor(up, ctx));
  if (open) {
    const body = el("div", "mine-extra mine-body");
    for (const run of up.runs) body.append(runLine(run, ctx));
    row.append(body);
  }
  return row;
}

// One CPU: a card headed by the CPU, its uploads listed under it newest first,
// however many there are. Two machines with one chip are one CPU here --
// nothing an upload carries says which box it came from, so the list does not
// guess.
function cpuBlock(cpu, ctx) {
  const card = el("div", "mine-card");
  const key = `${ctx.prefix}|cpu|${cpu.key}`;
  const open = mineOpen.has(key);
  const toggle = () => {
    if (!mineOpen.delete(key)) mineOpen.add(key);
    ctx.redraw();
  };
  const ids = cpu.uploads.flatMap((u) => u.runs.map((r) => r.id));
  const row = el("div", "mine-row mine-cpu");
  row.append(pickBox(ids, ctx.picked, ctx.redraw, `select all ${plural(ids.length, "run")} on this CPU`),
             foldArrow(open, toggle, "show the uploads on this CPU", "hide the uploads"));
  const main = el("div", "mine-main");
  const title = el("strong", "foldname", cpu.name);
  title.addEventListener("click", toggle);
  main.append(title, el("span", "count",
    `${plural(cpu.uploads.length, "upload")} · ${plural(ids.length, "run")}`));
  const hid = hiddenText(cpu.uploads.flatMap((u) => u.runs));
  if (hid) main.append(hiddenTag(hid));
  if (!open) main.append(el("span", "muted", "latest"), mutedWhen(cpu.uploads[0].at));
  row.append(main);
  if (open) {
    const body = el("div", "mine-extra mine-body");
    for (const up of cpu.uploads) body.append(uploadBlock(up, ctx));
    row.append(body);
  }
  card.append(row);
  return card;
}

// One half of My uploads: a bar that acts on whatever is ticked, then a card
// per CPU. Both halves are the same list with a different way of proving the
// run is yours, so one function draws them both.
function renderUploadList(box, cpus, ctx, remove) {
  const runs = cpus.flatMap((c) => c.uploads.flatMap((u) => u.runs));
  const ids = new Set(runs.map((r) => r.id));
  // Ticks for rows that are no longer here -- withdrawn in another tab, or off
  // the end of a smaller page -- would otherwise be counted forever.
  for (const id of [...ctx.picked]) if (!ids.has(id)) ctx.picked.delete(id);
  const picked = ctx.picked;

  const bar = el("div", "batch");
  const all = el("label", "check");
  const allBox = el("input");
  allBox.type = "checkbox";
  allBox.checked = runs.length > 0 && picked.size === runs.length;
  allBox.indeterminate = picked.size > 0 && picked.size < runs.length;
  allBox.addEventListener("change", () => {
    picked.clear();
    if (allBox.checked) for (const r of runs) picked.add(r.id);
    ctx.redraw();
  });
  all.append(allBox, el("span", null, "Select all"));

  const go = el("button", "danger", picked.size > 1
    ? `Withdraw ${picked.size} results` : "Withdraw selected");
  go.type = "button";
  go.disabled = !picked.size;
  go.addEventListener("click", () => {
    go.disabled = true;                 // no second click while the first runs
    withdrawPicked(runs, picked, remove, ctx.redraw).catch((e) => alert(e.message));
  });
  bar.append(all, go);

  // Hiding is withdrawing that can be undone: off the board and out of every
  // comparison, still here, still the uploader's.
  const chosen = runs.filter((r) => picked.has(r.id));
  for (const [hidden, text, title] of [
    [true, "Hide", "take off the board and out of comparisons, without deleting"],
    [false, "Show", "put back on the board"],
  ]) {
    const b = el("button", "plain", text);
    b.type = "button";
    b.title = title;
    b.disabled = !chosen.some((r) => Boolean(r.hidden) !== hidden);
    b.addEventListener("click", () => {
      b.disabled = true;
      hidePicked(runs, picked, hidden, ctx).catch((e) => alert(e.message));
    });
    bar.append(b);
  }
  if (ctx.adopt) {
    const move = el("button", "plain", "Move to my account");
    move.type = "button";
    move.title = `put these on ${state.user.name}, withdrawable from any browser`;
    move.disabled = !picked.size;
    move.addEventListener("click", () => {
      move.disabled = true;
      adoptPicked(runs, picked).catch((e) => alert(e.message));
    });
    bar.append(move);
  }
  box.append(bar);

  for (const cpu of cpus) box.append(cpuBlock(cpu, ctx));
}

// The account's listing as the server groups it, in the list's own shape.
function accountCpus(cpus) {
  return cpus.map((c) => ({
    key: JSON.stringify([c.cpu_models, c.cpu_layout, c.cpu_models ? null : c.machine]),
    name: machineName(c),
    uploads: c.uploads.map((u) => ({
      key: String(u.id),
      label: u.label || "",
      notes: u.notes || "",
      at: u.created_at,
      runs: u.runs.map((r) => ({ id: r.id, at: r.created_at, variant: r.variant,
                                 tagged: r.tagged, hidden: r.hidden })),
    })),
  }));
}

// This browser's anonymous uploads, grouped the same way here rather than on
// the server, which cannot tell whose they are. An upload is the runs one file
// sent, as recorded when it was sent; an entry from before that was recorded
// stands alone.
function localCpus(entries) {
  const cpus = new Map();
  for (const e of entries) {                // newest first
    const name = e.name ? fastFirst(e.name) : `run ${e.id}`;
    let cpu = cpus.get(name);
    if (!cpu) cpus.set(name, cpu = { key: name, name, uploads: [], byKey: new Map() });
    const k = String(e.batch ?? e.id);
    let up = cpu.byKey.get(k);
    if (!up) {
      up = { key: k, label: splitLabel(e.label).base, notes: e.notes || "",
             known: "label" in e, at: e.at, runs: [] };
      cpu.byKey.set(k, up);
      cpu.uploads.push(up);
    }
    up.runs.unshift({ id: e.id, at: e.at, token: e.token, variant: e.variant || null,
                      tagged: Boolean(e.variant), hidden: Boolean(e.hidden) });
    up.at = e.at;                           // ends on the first run's time
  }
  return [...cpus.values()];
}

// The account half is fetched, so its drawing is kept apart from its loading:
// ticking a box or withdrawing redraws from the last listing, and only a real
// change asks the server for a new one. It is paged by CPU, so a CPU's uploads
// and an upload's variants are never split across two pages.
let accountGen = 0;

async function loadAccountRuns() {
  const gen = ++accountGen;             // a reply from an older load is stale
  if (!state.user) {
    state.accountRuns = null;
    state.accountError = "";
    state.mineOffset = 0;
    state.mineTotal = 0;
    minePicked.account.clear();   // whoever signs in next ticked none of these
    return drawAccountRuns();
  }
  try {
    const page = await api(`/api/uploads?user=me&limit=${state.mineLimit}` +
                           `&offset=${state.mineOffset}`);
    if (gen !== accountGen) return;     // a newer load is already on its way
    state.accountRuns = page.cpus;
    state.mineTotal = page.total ?? page.cpus.length;
    state.mineCounts = { runs: page.runs, uploads: page.uploads };
    state.accountError = "";
    // Withdrawing a whole page leaves this one standing past the end of the
    // listing; step back to where the uploads now stop, and only backwards.
    const back = lastPage(state.mineTotal, state.mineLimit);
    if (!page.cpus.length && back < state.mineOffset) {
      state.mineOffset = back;
      return loadAccountRuns();
    }
  } catch (e) {
    if (gen !== accountGen) return;
    state.accountRuns = [];
    state.accountError = e.message;
  }
  drawAccountRuns();
}

// A withdrawn run taken out of the last listing, and any upload or CPU it
// leaves empty with it.
function dropAccountRun(id) {
  state.accountRuns = state.accountRuns
    .map((c) => ({ ...c, uploads: c.uploads
      .map((u) => ({ ...u, runs: u.runs.filter((r) => r.id !== id) }))
      .filter((u) => u.runs.length) }))
    .filter((c) => c.uploads.length);
}

function drawAccountRuns() {
  const box = $("#mine-account");
  box.replaceChildren();
  if (!state.user) {
    box.append(el("p", "note",
      "Sign in and your uploads are listed here on the account instead, " +
      "withdrawable from any browser without a token."));
    return;
  }
  const head = el("h2", null, `On your account (${state.user.name})`);
  box.append(head);
  if (state.accountError) {
    box.append(el("p", "warn", state.accountError));
    return;
  }
  if (state.accountRuns === null) {
    box.append(el("p", "empty", "Loading…"));
    return;
  }
  if (!state.accountRuns.length) {
    box.append(el("p", "empty", "Nothing uploaded to this account yet."), adoptForm());
    return;
  }
  const n = state.mineCounts || {};
  if (n.runs) {
    box.append(el("p", "note",
      `${n.runs} run${n.runs === 1 ? "" : "s"} in ${n.uploads} upload` +
      `${n.uploads === 1 ? "" : "s"}, on ${state.mineTotal} ` +
      `CPU${state.mineTotal === 1 ? "" : "s"}. An upload of several build ` +
      `variants is one entry; open it for each variant's run.`));
  }
  const ctx = {
    prefix: "a",
    picked: minePicked.account,
    redraw: drawAccountRuns,
    tokens: false,
    // A refusal leaves the form open with the reason in it; the listing is
    // fetched again only once the change has landed.
    edit: async (up, label, notes) => {
      await patchUpload(up, label, notes);
      await loadAccountRuns();
    },
    hide: (run, hidden) => api(`/api/runs/${run.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ hidden }),
    }),
    reload: () => loadAccountRuns(),
  };
  renderUploadList(box, accountCpus(state.accountRuns), ctx,
                   // Taken out of the listing as it goes rather than waiting
                   // for the refetch, so a withdrawn run is never redrawn.
                   (e) => api(`/api/runs/${e.id}`, { method: "DELETE" })
                     .then(() => dropAccountRun(e.id)));
  pager(box, { offset: state.mineOffset, limit: state.mineLimit,
               total: state.mineTotal },
        (offset) => {
          state.mineOffset = Math.max(0, offset);
          loadAccountRuns().catch(() => {});
        });
  box.append(adoptForm());
}

// Rewrite this browser's record of some uploads, by run id.
function updateLocal(changes) {
  const all = myRuns().map((e) => (changes.has(e.id) ? { ...e, ...changes.get(e.id) } : e));
  localStorage.setItem("cpcpub-uploads", JSON.stringify(all));
}

function drawLocalRuns() {
  const box = $("#minelist");
  box.replaceChildren();
  const cpus = localCpus(myRuns());
  if (!cpus.length) {
    box.append(el("p", "empty", "No anonymous uploads from this browser."));
    return;
  }
  if (state.localOffset >= cpus.length) {
    state.localOffset = lastPage(cpus.length, state.mineLimit);
  }
  const shown = cpus.slice(state.localOffset, state.localOffset + state.mineLimit);
  const ctx = {
    prefix: "l",
    picked: minePicked.local,
    redraw: drawLocalRuns,
    tokens: true,
    // Entries saved before this browser kept labels have none to start the
    // form from, so the hub is asked for what the upload says now.
    prepare: async (up) => {
      if (up.known) return;
      const run = await api(`/api/runs/${up.runs[0].id}`);
      const s = splitLabel(run.label);
      up.label = s.base;
      up.notes = run.notes || "";
      up.runs[0].tagged = s.tagged;
      up.known = true;
      updateLocal(new Map([[up.runs[0].id, { label: run.label || "", notes: up.notes }]]));
    },
    edit: async (up, label, notes) => {
      await patchUpload(up, label, notes, (run) => ({ "X-Delete-Token": run.token }));
      const many = up.runs.length > 1;
      updateLocal(new Map(up.runs.map((r) => [r.id, { label: labelFor(label, r, many), notes }])));
    },
    // What this browser knows of a run's being hidden is what it did itself;
    // the hub is the one that holds it, and the run's own page says so.
    hide: async (run, hidden) => {
      await api(`/api/runs/${run.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", "X-Delete-Token": run.token },
        body: JSON.stringify({ hidden }),
      });
      updateLocal(new Map([[run.id, { hidden }]]));
    },
    reload: async () => drawLocalRuns(),
    adopt: Boolean(state.user),
  };
  renderUploadList(box, shown, ctx,
                   (e) => api(`/api/runs/${e.id}`, {
                     method: "DELETE",
                     headers: { "X-Delete-Token": e.token },
                   }));
  pager(box, { offset: state.localOffset, limit: state.mineLimit,
               total: cpus.length },
        (offset) => {
          state.localOffset = Math.max(0, offset);
          drawLocalRuns();
        });
}

function renderMine() {
  loadAccountRuns().catch(() => {});
  drawLocalRuns();
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

function setupTabs() {
  for (const b of document.querySelectorAll("#tabs button")) {
    b.addEventListener("click", () => showTab(b.dataset.tab));
  }
}

// How many results each list shows. Read back from local storage before the
// first load, so the board comes up at the size that was chosen last time
// rather than at the default and then jumping.
function setupPageSizes() {
  state.limit = storedPage("cpcpub-page-board", state.limit);
  state.mineLimit = storedPage("cpcpub-page-mine", state.mineLimit);
  fillPageSizes($("#filters").elements.limit, state.limit);
  const mine = $("#mine-limit");
  fillPageSizes(mine, state.mineLimit);
  mine.addEventListener("change", () => {
    state.mineLimit = pageSize(mine.value, state.mineLimit);
    rememberPage("cpcpub-page-mine", state.mineLimit);
    state.mineOffset = 0;
    state.localOffset = 0;
    renderMine();
  });
}

// The drop target is also a file picker and also keyboard-reachable: the same
// step done by dragging, by clicking, or by tabbing to it and pressing enter.
function setupDropzone() {
  const zone = $("#drop");
  const input = $("#file");
  const take = (files) => {
    if (files && files.length) takeFiles(files).catch((e) => alert(e.message));
  };
  zone.addEventListener("click", () => input.click());
  zone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      input.click();
    }
  });
  input.addEventListener("change", () => take(input.files));
  for (const type of ["dragenter", "dragover"]) {
    zone.addEventListener(type, (e) => {
      e.preventDefault();
      zone.classList.add("over");
    });
  }
  for (const type of ["dragleave", "dragend"]) {
    zone.addEventListener(type, () => zone.classList.remove("over"));
  }
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    zone.classList.remove("over");
    take(e.dataTransfer && e.dataTransfer.files);
  });
  // Dropping anywhere else must not make the browser navigate to the file,
  // which looks exactly like the upload silently failing.
  for (const type of ["dragover", "drop"]) {
    window.addEventListener(type, (e) => {
      if (!zone.contains(e.target)) e.preventDefault();
    });
  }
  // The label is part of what each pending upload will be called, so the
  // preview follows it as it is typed.
  $("#label").addEventListener("input", () => {
    if (state.files.length) renderPicked();
  });
}

// ---------------------------------------------------------------------------
// Download
// ---------------------------------------------------------------------------

// The release this hub serves, grouped as the release's own notes group it:
// packages by system, standalone binaries by OS and architecture, firmware by
// chip. A hub that serves none has no Download tab at all.
async function loadDownloads() {
  let d;
  try {
    d = await api("/api/downloads");
  } catch {
    return;
  }
  if (!d || !d.release) return;
  state.downloads = d;
  $("#tab-download").hidden = false;
  renderDownloads(d);
  renderVerifiedNote();
  if (state.wantDownload) showTab("download");
}

function fmtSize(n) {
  return n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1e3))} kB`;
}

// A small link to one file, its name and size in the tooltip.
function fileChip(file, label) {
  const a = el("a", "dl-chip", label);
  a.href = file.href;
  a.download = file.name;
  a.title = `${file.name}, ${fmtSize(file.size)}`;
  return a;
}

// A large one, for what this device most likely wants.
function fileButton(file, title, sub, secondary) {
  const a = el("a", secondary ? "dl-button secondary" : "dl-button");
  a.href = file.href;
  a.download = file.name;
  a.title = file.name;
  a.append(el("strong", null, title),
           el("span", null, [sub, fmtSize(file.size)].filter(Boolean).join(" · ")));
  return a;
}

function chipRow(what, chips) {
  const row = el("div", "dl-row");
  if (what) row.append(el("span", "what", what));
  row.append(...chips);
  return row;
}

// A shell line with its Copy button, as the Submit tab draws them.
function commandBlock(id, text) {
  const box = el("div", "cmd");
  const pre = el("pre", null, text);
  pre.id = id;
  const copy = el("button", "copy", "Copy");
  copy.type = "button";
  copy.dataset.copy = id;
  copy.addEventListener("click", () => copyButton(copy));
  box.append(pre, copy);
  return box;
}

// What this browser runs on, as far as it says. Only a guess, which is why the
// whole list is always there under it.
function guessDevice() {
  const ua = navigator.userAgent || "";
  const plat = (navigator.userAgentData && navigator.userAgentData.platform) ||
               navigator.platform || "";
  if (/Android/i.test(ua)) return { os: "android" };
  // iPadOS asks for the desktop site as a Mac; its touch screen gives it away.
  if (/iPhone|iPad|iPod/i.test(ua) ||
      (/Macintosh/i.test(ua) && navigator.maxTouchPoints > 1)) return { os: "ios" };
  if (/Windows/i.test(ua) || /^Win/i.test(plat)) return { os: "windows" };
  if (/Macintosh|Mac OS X/i.test(ua) || /^Mac/i.test(plat)) return { os: "macos" };
  if (/Linux|X11|CrOS/i.test(ua) || /Linux/i.test(plat)) {
    const s = `${ua} ${plat}`;
    const arch = /aarch64|arm64/i.test(s) ? "Arm64"
      : /armv[67]|armv8l|armhf/i.test(s) ? "32-bit Arm"
      : /riscv64/i.test(s) ? "RISC-V"
      : /loongarch/i.test(s) ? "LoongArch"
      : /ppc64/i.test(s) ? "ppc64le"
      : /s390/i.test(s) ? "s390x"
      : /x86_64|amd64|x64/i.test(s) ? "x86-64" : null;
    return { os: "linux", arch };
  }
  return { os: null };
}

// The standalone binary each Linux architecture runs anywhere: the plain one,
// never a newer-ISA build that would not start on an older machine.
const LINUX_PLAIN = {
  "x86-64": "x86_64", "Arm64": "aarch64", "32-bit Arm": "armv7", "RISC-V": "riscv64",
  "LoongArch": "loongarch64", "ppc64le": "ppc64le", "s390x": "s390x",
};

function findBinary(d, os, variant) {
  for (const row of d.binaries.rows) {
    for (const f of row.cells[os] || []) if (f.variant === variant) return f;
  }
  return null;
}

function runCommand(file) {
  return `curl -fLo cpcpub ${location.origin}${file.href}\n` +
         "chmod +x cpcpub\n./cpcpub --full";
}

function renderDownloadHero(d) {
  const pkg = Object.fromEntries(d.packages.map((p) => [p.id, p]));
  const dev = guessDevice();
  const hero = el("div", "dl-hero");
  const buttons = el("div", "dl-buttons");
  const say = (title) => hero.append(el("h3", null, title));

  if (dev.os === "windows" && pkg.windows && pkg.windows.file) {
    say("For Windows");
    buttons.append(fileButton(pkg.windows.file, "Windows setup",
                              "x64 and Arm64, with the GUI"));
    hero.append(buttons);
    const bare = ["x86_64", "arm64"].map((v) => findBinary(d, "windows", v))
      .filter(Boolean).map((f) => fileChip(f, f.variant));
    if (bare.length) hero.append(chipRow("Or just the .exe:", bare));
    hero.append(el("p", "note", "Unsigned, so SmartScreen may ask before the first run."));
  } else if (dev.os === "android" && pkg.android && pkg.android.file) {
    say("For Android");
    buttons.append(fileButton(pkg.android.file, "Android app", "64- and 32-bit Arm"));
    hero.append(buttons);
    if (pkg.termux && pkg.termux.bench.length) {
      hero.append(chipRow("Or for Termux:",
                          pkg.termux.bench.map((f) => fileChip(f, f.arch))));
    }
  } else if (dev.os === "macos") {
    const arm = findBinary(d, "macos", "arm64");
    const intel = findBinary(d, "macos", "x86_64");
    if (!arm && !intel) return null;
    say("For macOS");
    if (arm) buttons.append(fileButton(arm, "Apple silicon", "M1 and later"));
    if (intel) buttons.append(fileButton(intel, "Intel Mac", "", !!arm));
    hero.append(buttons);
    hero.append(el("p", "note", "Saved from the browser, it needs " +
      "xattr -d com.apple.quarantine FILE before it runs. Or in Terminal:"));
    hero.append(commandBlock("dl-cmd", runCommand(arm || intel)));
  } else if (dev.os === "linux") {
    const arch = dev.arch && LINUX_PLAIN[dev.arch] ? dev.arch : "x86-64";
    say(`For Linux on ${arch}`);
    for (const [id, label] of [["debian", "Debian, Ubuntu"],
                               ["fedora", "Fedora, openSUSE"],
                               ["arch", "Arch Linux"]]) {
      const f = pkg[id] && pkg[id].bench.find((b) => b.arch === arch);
      if (f) buttons.append(fileButton(f, label, f.name.match(/\.(deb|rpm|pkg\.tar\.zst)$/)[0]));
    }
    if (buttons.childElementCount) hero.append(buttons);
    const plain = findBinary(d, "linux", LINUX_PLAIN[arch]);
    if (plain) {
      hero.append(el("p", "note", "Or run it without installing:"));
      hero.append(commandBlock("dl-cmd", runCommand(plain)));
    }
    if (!dev.arch) {
      hero.append(el("p", "note", "Not x86-64? Every architecture is below."));
    }
  } else {
    hero.append(el("p", "note", "No build for this device."));
  }
  return hero;
}

function renderDownloads(d) {
  const box = $("#download-body");
  box.replaceChildren();

  const head = el("div", "dl-head");
  head.append(el("h2", null, `cpcpub ${d.version}`));
  const sub = el("p", "note");
  if (d.published) {
    const when = new Date(d.published);
    if (!Number.isNaN(when.getTime())) {
      sub.append(el("span", null, `Released ${when.toLocaleDateString(undefined,
        { year: "numeric", month: "long", day: "numeric" })}. `));
    }
  }
  if (d.url) {
    const notes = el("a", "linkish", "Release notes");
    notes.href = d.url;
    notes.rel = "noopener noreferrer";
    notes.target = "_blank";
    sub.append(notes);
  }
  head.append(sub);
  box.append(head);

  const hero = renderDownloadHero(d);
  if (hero) box.append(hero);

  // Packages.
  if (d.packages.length) {
    box.append(el("h2", null, "Install a package"));
    const grid = el("div", "dl-grid");
    const blurb = {
      debian: "Debian 13, Ubuntu 24.04 or newer.",
      fedora: "Fedora 39, openSUSE Tumbleweed or newer.",
      windows: "x64 and Arm64, with the GUI.",
    };
    for (const p of d.packages) {
      const card = el("div", "dl-card");
      card.append(el("h3", null, p.name));
      if (p.bench.length) {
        card.append(chipRow("Benchmark", p.bench.map((f) => fileChip(f, f.arch))));
      }
      if (p.gui) card.append(chipRow("GUI", [fileChip(p.gui, "any architecture")]));
      if (p.file) {
        card.append(chipRow(null, [fileChip(p.file,
          p.id === "windows" ? "Setup" : p.id === "android" ? "App (.apk)" : p.file.name)]));
      }
      if (blurb[p.id]) card.append(el("p", "note", blurb[p.id]));
      grid.append(card);
    }
    box.append(grid);
  }

  // Standalone binaries.
  if (d.binaries.rows.length) {
    box.append(el("h2", null, "Or a standalone binary"));
    box.append(el("p", "note", "-v3, -rva23 and -neon need a newer CPU; the plain " +
      "build runs everywhere."));
    const table = el("table", "dl-table");
    const hr = el("tr");
    hr.append(el("th"));
    for (const o of d.binaries.oses) hr.append(el("th", null, o.name));
    const thead = el("thead");
    thead.append(hr);
    const tbody = el("tbody");
    for (const row of d.binaries.rows) {
      const tr = el("tr");
      tr.append(el("th", null, row.arch));
      for (const o of d.binaries.oses) {
        const td = el("td");
        const files = row.cells[o.id] || [];
        if (files.length) td.append(chipRow(null, files.map((f) => fileChip(f, f.variant))));
        tr.append(td);
      }
      tbody.append(tr);
    }
    table.append(thead, tbody);
    const scroller = el("div", "scroller");
    scroller.append(table);
    box.append(scroller);
  }

  // Firmware.
  if (d.firmware.length) {
    box.append(el("h2", null, "Microcontroller firmware"));
    const grid = el("div", "dl-grid");
    for (const fam of d.firmware) {
      const card = el("div", "dl-card");
      card.append(el("h3", null, fam.family));
      card.append(chipRow(null, fam.chips.map((f) => fileChip(f, f.chip))));
      card.append(el("p", "note", fam.family === "Raspberry Pi"
        ? "Hold BOOTSEL, plug in, copy the .uf2 to the drive."
        : "Flash at 0x0 with esptool."));
      grid.append(card);
    }
    box.append(grid);
    if (d.url) {
      const more = el("p", "note");
      const link = el("a", "linkish", "mcu/README.md");
      link.href = d.url.replace("/releases/tag/", "/blob/") + "/mcu/README.md";
      link.rel = "noopener noreferrer";
      link.target = "_blank";
      more.append(el("span", null, "More in "), link, el("span", null, "."));
      box.append(more);
    }
  }

  // Checksums.
  const sums = el("details", "more dl-sums");
  sums.append(el("summary", null, "Check a download"));
  sums.append(el("p", "note", "Save as SHA256SUMS, then: sha256sum -c --ignore-missing SHA256SUMS"));
  sums.append(commandBlock("dl-sums",
    d.files.map((f) => `${f.sha256}  ${f.name}`).join("\n")));
  box.append(sums);
}

async function boot() {
  // Read before anything rewrites the hash: restoring a comparison does, and
  // would drop the run from a link carrying both.
  const deepRun = location.hash.match(/run=(\d+)/);
  state.wantDownload = location.hash === "#download";
  setupTabs();
  setupDropzone();
  setupPageSizes();
  for (const b of document.querySelectorAll("button.copy[data-copy]")) {
    b.addEventListener("click", () => copyButton(b));
  }
  renderCommands();
  // Arch gets its full list at once, so the picker is usable before any
  // request finishes; OS has no list to draw until /api/stats says what this
  // hub holds, so it starts as "any" alone. Both are filled again with the
  // counts when they arrive.
  fillTargets(null);
  fillSystems(null);
  fillCpus(null);
  renderVerifiedNote();
  loadDownloads();

  const meta = await api("/api/metrics");
  state.metrics = meta.metrics;

  // Which release this hub verifies against, and how many results it holds
  // per architecture and per operating system. Not fatal if it fails: the
  // board is readable without it, and the Arch and OS pickers have already
  // been drawn once with what little that leaves them -- for OS, just "any",
  // which is what they default to anyway.
  api("/api/stats").then((s) => {
    state.release = s.release || null;
    state.held = typeof s.runs === "number" ? s.runs : null;
    if (s.batch) renderBatchLimits(s.batch);
    fillTargets(s.targets || []);
    fillSystems(s.systems || []);
    fillCpus(s.cpus || []);
    renderVerifiedNote();
    // Only ever corrects a message already on screen: an empty board drawn
    // before the counts arrived cannot yet know whether the hub is empty or
    // merely filtered.
    if (!$("#board-empty").hidden) renderEmptyNote();
  }).catch(() => {});

  $("#filters").addEventListener("submit", (e) => {
    e.preventDefault();
    // Submitting the form is the search: it is the one filter that waits to be
    // asked for, since a board that reshuffles on every keystroke is unusable.
    state.search = $("#filters").elements.q.value.trim();
    loadBoard().catch((err) => alert(err.message));
  });
  // Picking from a dropdown is the whole gesture -- there is nothing to confirm
  // afterwards, so each one reloads the board itself.
  // `norm` is in here rather than with the redraw-only control below because
  // it is what the board is ranked by as well as what it prints: switching to
  // per GHz reorders the rows, so it has to ask the server again.
  // Picking a suggested CPU is a whole gesture too, so it searches at once;
  // text typed by hand still waits to be submitted.
  $("#filters").elements.q.addEventListener("input", (e) => {
    $("#search-clear").hidden = !e.target.value;
    const text = e.target.value.trim();
    if (text === state.search || !state.cpus.has(text)) return;
    state.search = text;
    loadBoard().catch((err) => alert(err.message));
  });
  // Clearing is a whole gesture as well: the board stops being searched at once.
  $("#search-clear").addEventListener("click", () => {
    const box = $("#filters").elements.q;
    box.value = "";
    $("#search-clear").hidden = true;
    box.focus();
    if (!state.search) return;
    state.search = "";
    loadBoard().catch((err) => alert(err.message));
  });
  for (const name of ["scope", "target", "os", "vectorize", "fma", "verified",
                      "limit", "norm", "bycpu"]) {
    $("#filters").elements[name].addEventListener("change", () => {
      loadBoard().catch((err) => alert(err.message));
    });
  }
  // This one changes how the rows are drawn, not which rows they are or in
  // what order, so it does not refetch.
  for (const box of [$("#filters").elements.detailed, $("#compare-detailed")]) {
    box.addEventListener("change", (e) => setDetailed(e.target.checked));
  }
  $("#uploadform").addEventListener("submit", doUpload);
  $("#clearsel").addEventListener("click", () => {
    state.selected.clear();
    state.scopeNote = "";
    syncHash();
    renderSelection();
    renderBoard();
  });
  $("#detail-close").addEventListener("click", closeRun);
  $("#detail").addEventListener("click", (e) => {
    if (e.target.id === "detail") closeRun();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#detail").hidden) closeRun();
  });
  // A #run=N typed or pasted into the address bar, or reached by Back, opens
  // that run; its absence closes the one open.
  window.addEventListener("hashchange", () => {
    if (location.hash === "#download" && state.downloads) showTab("download");
    const want = location.hash.match(/run=(\d+)/);
    if (want && Number(want[1]) !== state.openRun) {
      showRun(Number(want[1])).catch((e) => alert(e.message));
    } else if (!want && state.openRun) {
      closeRun();
    }
  });

  // Who is signed in decides what the Account tab and the shell commands say,
  // and whether My uploads has an account half. It never blocks the board.
  await loadUser();
  renderSelection();

  // #user=name is a link to one submitter's uploads, so it has to be read
  // before the board is first loaded rather than reloading it afterwards.
  const who = location.hash.match(/user=([A-Za-z0-9._-]{3,32})/);
  if (who) state.submitter = who[1];
  await loadBoard();

  // Deep links: #run=N opens a run, #compare=1,2,3 restores a shared comparison.
  const cmp = location.hash.match(/compare=([\d,]+)/);
  if (cmp) {
    const { cores } = await api("/api/cores?limit=64&ids=" + cmp[1]);
    // A link names records directly, so it is the one way a mixed selection can
    // still arrive. Take the scope of the first and keep the board on it; the
    // rest of that scope come along, anything else is left behind rather than
    // silently compared against a whole machine.
    const scope = cores.length ? cores[0].scope : null;
    const same = cores.filter((c) => c.scope === scope);
    const kept = same.slice(0, MAX_COMPARE);
    for (const row of kept) state.selected.set(row.id, row);
    const notes = [];
    if (same.length < cores.length) {
      notes.push(`${cores.length - same.length} row(s) in that link were a ` +
                 `different scope and were left out: one comparison holds one ` +
                 `kind of record.`);
    }
    if (kept.length < same.length) {
      notes.push(`${FULL_NOTE} The first ${kept.length} in the link are shown.`);
    }
    state.scopeNote = notes.join(" ");
    if (scope && $("#filters").elements.scope.value !== scope) {
      $("#filters").elements.scope.value = scope;
      await loadBoard();
    }
    syncHash();
    renderSelection();
    renderBoard();
    if (kept.length) showTab("compare");
  }
  if (deepRun) showRun(Number(deepRun[1])).catch(() => {});
}

boot().catch((e) => {
  document.body.prepend(el("p", "warn", `Could not start: ${e.message}`));
});
