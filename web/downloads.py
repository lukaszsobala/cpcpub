#!/usr/bin/env python3
"""The latest release's downloads, mirrored from GitHub for the hub to serve.

    python3 web/downloads.py OWNER/REPO DIR     # fetch the latest release once

The hub serves what DIR holds at /downloads/TAG/NAME and lists it at
/api/downloads for its Download tab. Started with --mirror OWNER/REPO, it
also keeps DIR up to date itself, asking GitHub once an hour; without it, DIR
changes only when this script is run, and the hub notices without a restart.

Only what someone downloads to run is taken: the packages, the standalone
binaries, the Android app and the firmware. The Play Store bundle, the
checksums and the build manifest are for a store, a script and a hub operator,
and stay on GitHub. Every file is checked against the release's SHA256SUMS
before it is served, and a release is switched to whole or not at all: one
still being uploaded, or uploaded again, waits for the next check.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_URL = "https://api.github.com/repos/{repo}/releases/latest"
ASSET_URL = "https://github.com/{repo}/releases/download/{tag}/{name}"
USER_AGENT = "cpcpub-hub (release mirror)"
INTERVAL = 3600.0               # seconds between checks, with --mirror
TIMEOUT = 60.0                  # per network operation, not per file
MAX_FILE = 256 << 20            # the Windows setup is about 50 MB
MAX_TOTAL = 1 << 30
SUMS = "SHA256SUMS"
CURRENT = "CURRENT"             # names the release being served
MANIFEST = "manifest.json"

REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")
TAG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")
# What a file served from here may be called: no path separators, no leading
# dot, nothing a Content-Disposition header would need quoting for.
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}")
HEX64_RE = re.compile(r"[0-9a-f]{64}")


class MirrorError(Exception):
    """A release that cannot be mirrored as it stands; the next check retries."""


# -- what each file is --------------------------------------------------------
#
# The same grouping as packaging/release-notes.py, which writes the release's
# own downloads table: packages by system, standalone binaries by OS and
# architecture, firmware by chip. Kept as data here rather than imported, since
# the hub is deployed without the packaging tree's tools on its path. A new
# asset either side knows nothing of is left out here, not served unsorted.

ARCHES = ["x86-64", "Arm64", "32-bit Arm", "RISC-V", "LoongArch", "ppc64le", "s390x"]
DEB = {"amd64": "x86-64", "arm64": "Arm64", "armhf": "32-bit Arm", "riscv64": "RISC-V",
       "loong64": "LoongArch", "ppc64el": "ppc64le", "s390x": "s390x"}
RPM = {"x86_64": "x86-64", "aarch64": "Arm64", "armv7hl": "32-bit Arm", "riscv64": "RISC-V",
       "loongarch64": "LoongArch", "ppc64le": "ppc64le", "s390x": "s390x"}
PKG = {"x86_64": "x86-64", "aarch64": "Arm64", "armv7h": "32-bit Arm", "riscv64": "RISC-V",
       "loong64": "LoongArch", "ppc64le": "ppc64le", "s390x": "s390x"}
TERMUX = {"aarch64": "Arm64", "arm": "32-bit Arm"}

# (id, name, the benchmark's pattern and arch names, the window's pattern)
LINUX_PACKAGES = [
    ("debian", "Debian, Ubuntu", r"cpcpub_{v}-\d+_([^_.]+)\.deb", DEB,
     r"cpcpub-gui_{v}-\d+_all\.deb"),
    ("fedora", "Fedora, openSUSE", r"cpcpub-{v}-\d+\.([^.]+)\.rpm", RPM,
     r"cpcpub-gui-{v}-\d+\.noarch\.rpm"),
    ("arch", "Arch Linux", r"cpcpub-{v}-\d+-([^-]+)\.pkg\.tar\.zst", PKG,
     r"cpcpub-gui-{v}-\d+-any\.pkg\.tar\.zst"),
]

# Standalone binaries, cpcpub-OS-VARIANT[.exe], in rows by architecture.
BIN_ROWS = [
    ("x86-64", ["x86_64", "x86_64-v3"]),
    ("Arm64", ["aarch64", "arm64"]),
    ("32-bit Arm", ["armv7", "armv7-neon", "armv6"]),
    ("RISC-V", ["riscv64", "riscv64-rva23"]),
    ("Other", ["loongarch64", "ppc64le", "s390x"]),
]
BIN_OSES = [("linux", "Linux"), ("macos", "macOS"), ("windows", "Windows"),
            ("android", "Android")]

CHIPS = [
    ("Raspberry Pi", [("rp2040", "RP2040"), ("rp2350-arm", "RP2350, Arm"),
                      ("rp2350-riscv", "RP2350, RISC-V")]),
    ("ESP32", [("esp32", "ESP32"), ("esp32s2", "ESP32-S2"), ("esp32s3", "ESP32-S3"),
               ("esp32c3", "ESP32-C3"), ("esp32c6", "ESP32-C6"), ("esp32p4", "ESP32-P4")]),
]


def place(version: str, name: str) -> dict | None:
    """Where `name` goes on the Download tab, or None if it is not served."""
    v = re.escape(version)
    for sid, _, bench_re, alias, gui_re in LINUX_PACKAGES:
        m = re.fullmatch(bench_re.format(v=v), name)
        if m and m.group(1) in alias:
            return {"group": "package", "system": sid, "arch": alias[m.group(1)]}
        if re.fullmatch(gui_re.format(v=v), name):
            return {"group": "package", "system": sid, "gui": True}
    if name == f"cpcpub-{version}-windows-setup.exe":
        return {"group": "package", "system": "windows"}
    if name == f"cpcpub-{version}-android.apk":
        return {"group": "package", "system": "android"}
    m = re.fullmatch(rf"cpcpub-termux_{v}-\d+_([^_.]+)\.deb", name)
    if m and m.group(1) in TERMUX:
        return {"group": "package", "system": "termux", "arch": TERMUX[m.group(1)]}
    m = re.fullmatch(r"cpcpub-(linux|macos|windows|android)-([a-z0-9_-]+?)(\.exe)?", name)
    if m and (m.group(3) is not None) == (m.group(1) == "windows"):
        for row, variants in BIN_ROWS:
            if m.group(2) in variants:
                return {"group": "binary", "os": m.group(1), "row": row,
                        "variant": m.group(2)}
    m = re.fullmatch(r"cpcpub-([a-z0-9-]+)\.(uf2|bin)", name)
    if m:
        for family, chips in CHIPS:
            for key, label in chips:
                if key == m.group(1):
                    return {"group": "firmware", "family": family, "chip": label}
    return None


def layout(manifest: dict) -> dict:
    """The Download tab's contents: the manifest's files, grouped and ordered."""
    tag = manifest["tag"]
    version = tag.removeprefix("v")
    files = manifest["files"]
    placed = [(f, place(version, f["name"])) for f in files]

    def entry(f: dict, **extra) -> dict:
        return {"name": f["name"], "size": f["size"], "sha256": f["sha256"],
                "href": f"/downloads/{tag}/{f['name']}", **extra}

    def arch_order(item: dict) -> int:
        return ARCHES.index(item["arch"]) if item["arch"] in ARCHES else len(ARCHES)

    packages = []
    systems = [(sid, label) for sid, label, *_ in LINUX_PACKAGES] + [
        ("windows", "Windows"), ("android", "Android"), ("termux", "Termux")]
    for sid, label in systems:
        mine = [(f, p) for f, p in placed if p and p["group"] == "package"
                and p["system"] == sid]
        if not mine:
            continue
        bench = sorted((entry(f, arch=p["arch"]) for f, p in mine if "arch" in p),
                       key=arch_order)
        gui = next((entry(f) for f, p in mine if p.get("gui")), None)
        whole = next((entry(f) for f, p in mine if "arch" not in p
                      and not p.get("gui")), None)
        packages.append({"id": sid, "name": label, "bench": bench, "gui": gui,
                         "file": whole})

    rows = []
    for row, variants in BIN_ROWS:
        cells = {}
        for os_id, _ in BIN_OSES:
            got = [entry(f, variant=p["variant"]) for f, p in placed
                   if p and p["group"] == "binary" and p["os"] == os_id
                   and p["row"] == row]
            got.sort(key=lambda e: variants.index(e["variant"]))
            if got:
                cells[os_id] = got
        if cells:
            rows.append({"arch": row, "cells": cells})
    oses = [{"id": o, "name": n} for o, n in BIN_OSES
            if any(o in r["cells"] for r in rows)]

    firmware = []
    for family, chips in CHIPS:
        got = []
        for _, label in chips:
            got += [entry(f, chip=label) for f, p in placed
                    if p and p["group"] == "firmware" and p["chip"] == label]
        if got:
            firmware.append({"family": family, "chips": got})

    return {
        "release": tag, "version": version, "url": manifest.get("url"),
        "published": manifest.get("published"),
        "packages": packages,
        "binaries": {"oses": oses, "rows": rows},
        "firmware": firmware,
        "files": [entry(f) for f, p in placed if p],
    }


def parse_sums(text: str) -> dict[str, str]:
    """{name: sha256} from a sha256sum listing, binary-mode stars and all."""
    out = {}
    for line in text.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        digest, name = parts[0].lower(), parts[1].strip().lstrip("*")
        if HEX64_RE.fullmatch(digest) and NAME_RE.fullmatch(name):
            out[name] = digest
    return out


# -- the mirror ---------------------------------------------------------------

class Mirror:
    """A directory holding one release's downloads, and the means to refresh it.

    DIR/TAG/ holds the files and their manifest; DIR/CURRENT names the TAG
    being served. A new release is fetched into DIR/.incoming-TAG and only
    then put in place, so a request never sees half of one.
    """

    def __init__(self, root: Path | str, repo: str | None = None, *,
                 api_url: str = API_URL, asset_url: str = ASSET_URL):
        if repo is not None and not REPO_RE.fullmatch(repo):
            raise ValueError(f"not a GitHub repository: {repo!r} (want OWNER/REPO)")
        self.root = Path(root)
        self.repo = repo
        self.api_url = api_url
        self.asset_url = asset_url
        self._sync_lock = threading.Lock()
        self._seen: tuple | None = None
        self._manifest: dict | None = None
        self._etag: str | None = None

    # -- serving --------------------------------------------------------------

    def manifest(self) -> dict | None:
        """The release being served, or None.

        Read again whenever CURRENT changes on disk, which a sync always does
        (it replaces the file), so a run of this script shows on a hub that is
        already running.
        """
        try:
            st = (self.root / CURRENT).stat()
        except OSError:
            self._seen, self._manifest = None, None
            return None
        key = (st.st_ino, st.st_mtime_ns, st.st_size)
        if key != self._seen:
            manifest = None
            try:
                tag = (self.root / CURRENT).read_text().strip()
                if TAG_RE.fullmatch(tag):
                    manifest = json.loads((self.root / tag / MANIFEST).read_text())
                    if manifest.get("tag") != tag:
                        manifest = None
            except (OSError, ValueError):
                manifest = None
            self._manifest, self._seen = manifest, key
        return self._manifest

    def find(self, tag: str, name: str) -> tuple[Path, dict] | None:
        """The file `name` of release `tag`, if this mirror serves it now."""
        m = self.manifest()
        if not m or m["tag"] != tag:
            return None
        for f in m["files"]:
            if f["name"] == name:
                return self.root / tag / name, f
        return None

    # -- fetching -------------------------------------------------------------

    def start(self, log, interval: float = INTERVAL) -> threading.Thread:
        """Check now and then every `interval` seconds, in the background."""
        def loop():
            while True:
                try:
                    said = self.sync()
                    if said:
                        log(f"downloads: {said}")
                except Exception as e:          # noqa: BLE001 -- retried later
                    log(f"downloads: {e}")
                time.sleep(interval)
        t = threading.Thread(target=loop, name="release-mirror", daemon=True)
        t.start()
        return t

    def sync(self) -> str | None:
        """Bring the directory to the repository's latest release.

        Returns what changed, or None when nothing did; raises MirrorError
        when the release cannot be taken as it is, leaving what was served.
        """
        if not self.repo:
            raise MirrorError("no repository to mirror")
        with self._sync_lock:
            release, etag = self._latest()
            if release is None:
                return None
            tag = release.get("tag_name")
            if not isinstance(tag, str) or not TAG_RE.fullmatch(tag):
                raise MirrorError(f"unusable release tag {tag!r}")
            assets = {a.get("name"): a for a in release.get("assets") or []
                      if isinstance(a, dict) and a.get("state") == "uploaded"}
            if SUMS not in assets:
                raise MirrorError(f"{tag} has no {SUMS} yet")
            sums = parse_sums(self._get(self._asset(tag, SUMS), 1 << 20).decode(
                "utf-8", "replace"))
            version = tag.removeprefix("v")
            want = sorted(n for n in sums if place(version, n))
            if not want:
                raise MirrorError(f"{tag} lists nothing to serve in its {SUMS}")
            missing = [n for n in want if n not in assets]
            if missing:
                raise MirrorError(f"{tag} is not fully uploaded yet: "
                                  f"{len(missing)} file(s) missing, e.g. {missing[0]}")
            # The checksums are what the release is: an unchanged list is an
            # unchanged release, and a re-run that rebuilt anything differs.
            fingerprint = hashlib.sha256("".join(
                f"{sums[n]}  {n}\n" for n in want).encode()).hexdigest()
            current = self.manifest()
            if (current and current.get("tag") == tag
                    and current.get("fingerprint") == fingerprint):
                self._etag = etag
                return None

            sizes: dict[str, int] = {}
            for n in want:
                size = assets[n].get("size")
                if not isinstance(size, int) or not 0 <= size <= MAX_FILE:
                    raise MirrorError(f"{n}: size {size!r} is out of bounds")
                sizes[n] = size
            if sum(sizes.values()) > MAX_TOTAL:
                raise MirrorError(f"{tag} is larger than {MAX_TOTAL >> 20} MiB")

            self.root.mkdir(parents=True, exist_ok=True)
            incoming = self.root / f".incoming-{tag}"
            shutil.rmtree(incoming, ignore_errors=True)
            incoming.mkdir()
            try:
                for n in want:
                    self._download(self._asset(tag, n), incoming / n, sizes[n], sums[n])
                url = release.get("html_url")
                if not (isinstance(url, str) and url.startswith("https://github.com/")):
                    url = f"https://github.com/{self.repo}/releases/tag/{tag}"
                published = release.get("published_at")
                manifest = {
                    "tag": tag, "repo": self.repo, "url": url,
                    "published": published if isinstance(published, str) else None,
                    "fingerprint": fingerprint,
                    "mirrored": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "files": [{"name": n, "size": sizes[n], "sha256": sums[n]}
                              for n in want],
                }
                (incoming / MANIFEST).write_text(json.dumps(manifest, indent=1))
                final = self.root / tag
                if final.exists():
                    # The same tag again, rebuilt: moved aside rather than
                    # deleted first, so there is never a moment with neither.
                    old = self.root / f".old-{tag}"
                    shutil.rmtree(old, ignore_errors=True)
                    final.rename(old)
                incoming.rename(final)
                tmp = self.root / f".{CURRENT}.tmp"
                tmp.write_text(tag + "\n")
                os.replace(tmp, self.root / CURRENT)
            except BaseException:
                shutil.rmtree(incoming, ignore_errors=True)
                raise
            self._etag = etag
            self._prune(keep=tag)
            self.manifest()
            return f"now serving {tag}: {len(want)} files"

    def _prune(self, keep: str) -> None:
        """Remove releases no longer served -- only directories this mirror
        made, so a DIR pointed somewhere by mistake loses nothing else."""
        for p in self.root.iterdir():
            if not p.is_dir() or p.name == keep:
                continue
            ours = (p.name.startswith((".incoming-", ".old-"))
                    or (TAG_RE.fullmatch(p.name) and (p / MANIFEST).is_file()))
            if ours:
                shutil.rmtree(p, ignore_errors=True)

    def _asset(self, tag: str, name: str) -> str:
        return self.asset_url.format(repo=self.repo, tag=urllib.parse.quote(tag),
                                     name=urllib.parse.quote(name))

    def _latest(self) -> tuple[dict | None, str | None]:
        """The latest release, or (None, None) when GitHub says it is unchanged.

        Asked conditionally: an unchanged answer costs nothing against GitHub's
        hourly allowance, which a shared outgoing address may be spending
        already. The tag it answers to is kept only once a sync succeeds, so a
        release that was incomplete is asked about in full again.
        """
        headers = {"Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        if self._etag and self.manifest():
            headers["If-None-Match"] = self._etag
        try:
            body, response_headers = self._open(
                self.api_url.format(repo=self.repo), 4 << 20, headers)
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return None, None
            raise MirrorError(f"GitHub answered {e.code} for the latest release") from e
        try:
            release = json.loads(body)
        except ValueError as e:
            raise MirrorError("GitHub's answer was not JSON") from e
        if not isinstance(release, dict):
            raise MirrorError("GitHub's answer was not a release")
        return release, response_headers.get("ETag")

    def _get(self, url: str, limit: int) -> bytes:
        try:
            return self._open(url, limit, {})[0]
        except urllib.error.HTTPError as e:
            raise MirrorError(f"{url}: {e.code}") from e

    def _open(self, url: str, limit: int, headers: dict):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                self._same_scheme(url, r.geturl())
                body = r.read(limit + 1)
                if len(body) > limit:
                    raise MirrorError(f"{url}: larger than expected")
                return body, r.headers
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, OSError) as e:
            raise MirrorError(f"{url}: {e}") from e

    def _download(self, url: str, dest: Path, size: int, sha256: str) -> None:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        digest = hashlib.sha256()
        got = 0
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r, open(dest, "wb") as out:
                self._same_scheme(url, r.geturl())
                while chunk := r.read(1 << 16):
                    got += len(chunk)
                    if got > size:
                        raise MirrorError(f"{dest.name}: longer than the release says")
                    digest.update(chunk)
                    out.write(chunk)
        except (urllib.error.URLError, OSError) as e:
            raise MirrorError(f"{dest.name}: {e}") from e
        if got != size:
            raise MirrorError(f"{dest.name}: {got} bytes, the release says {size}")
        if digest.hexdigest() != sha256:
            raise MirrorError(f"{dest.name}: does not match {SUMS}")
        os.chmod(dest, 0o644)

    @staticmethod
    def _same_scheme(asked: str, landed: str) -> None:
        """A redirect may move a download, never drop it from https to http."""
        if urllib.parse.urlsplit(asked).scheme != urllib.parse.urlsplit(landed).scheme:
            raise MirrorError(f"{asked}: redirected to another scheme")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print((__doc__ or "").strip(), file=sys.stderr)
        return 2
    try:
        said = Mirror(argv[1], argv[0]).sync()
    except (MirrorError, ValueError) as e:
        print(f"downloads: {e}", file=sys.stderr)
        return 1
    print(f"downloads: {said or 'already up to date'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
