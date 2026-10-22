#!/usr/bin/env python3
"""Run cpcpub on a microcontroller over its serial console and save the result.

    python3 mcu/cpcpub-serial.py                     # --full --json, saved to a file
    python3 mcu/cpcpub-serial.py -o run.json -- --time 1 --reps 5
    python3 mcu/cpcpub-serial.py --port /dev/ttyACM0 -- --per-core

Finds the board -- the one USB serial port there is, or --port -- types the
options after `--` at its prompt (always with --json added, since the document
is what this is for), shows everything the board prints as it prints it, and
writes the document it printed between its -----BEGIN CPCPUB JSON----- and
-----END CPCPUB JSON----- lines. A --variants run prints one document per
variant, and they are written as a JSON array, the same as
`cpcpub --variants --json` writes on a desktop.

Standard library only on Linux and macOS. Windows has no termios, so there it
needs pyserial (`pip install pyserial`).
"""

import argparse
import glob
import json
import os
import re
import select
import sys
import time
from datetime import datetime

PROMPT = "cpcpub> "
BEGIN = re.compile(r"^-----BEGIN CPCPUB JSON-----$")
END = re.compile(r"^-----END CPCPUB JSON-----$")

# USB serial devices as each system names them: a board's own USB port
# (ttyACM, usbmodem) first, then USB-serial adapters and bridges.
PORT_GLOBS = {
    "linux": ["/dev/ttyACM*", "/dev/ttyUSB*"],
    "darwin": ["/dev/cu.usbmodem*", "/dev/cu.usbserial*", "/dev/cu.SLAB_USBtoUART*",
               "/dev/cu.wchusbserial*"],
}


class PosixPort:
    """A serial device through termios: raw, 8N1, no flow control."""

    def __init__(self, path: str, baud: int):
        import termios
        import tty
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
        tty.setraw(self.fd)
        attrs = termios.tcgetattr(self.fd)
        speed = getattr(termios, f"B{baud}", None)
        if speed is None:
            raise SystemExit(f"{baud} is not a baud rate this system knows")
        attrs[4] = attrs[5] = speed
        attrs[2] |= termios.CLOCAL | termios.CREAD
        attrs[2] &= ~(termios.CRTSCTS if hasattr(termios, "CRTSCTS") else 0)
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)

    def read(self, timeout: float) -> bytes:
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return b""
        data = os.read(self.fd, 4096)
        if not data:
            raise SystemExit("the board went away")
        return data

    def write(self, data: bytes) -> None:
        os.write(self.fd, data)

    def close(self) -> None:
        os.close(self.fd)


class PyserialPort:
    """The same through pyserial, for Windows."""

    def __init__(self, path: str, baud: int):
        import serial
        self.s = serial.Serial(path, baud, timeout=0)

    def read(self, timeout: float) -> bytes:
        self.s.timeout = timeout
        return self.s.read(4096)

    def write(self, data: bytes) -> None:
        self.s.write(data)

    def close(self) -> None:
        self.s.close()


def find_port() -> str:
    if sys.platform == "win32":
        try:
            from serial.tools import list_ports
        except ImportError:
            raise SystemExit("on Windows this needs pyserial: pip install pyserial") from None
        found = [p.device for p in list_ports.comports() if p.vid is not None]
    else:
        key = "darwin" if sys.platform == "darwin" else "linux"
        found = sorted(p for g in PORT_GLOBS[key] for p in glob.glob(g))
    if len(found) == 1:
        return found[0]
    if not found:
        raise SystemExit("no USB serial port found: is the board plugged in and "
                         "flashed? Name it with --port")
    raise SystemExit("more than one serial port; pick one with --port:\n  "
                     + "\n  ".join(found))


def open_port(path: str, baud: int):
    if sys.platform == "win32":
        try:
            return PyserialPort(path, baud)
        except ImportError:
            raise SystemExit("on Windows this needs pyserial: pip install pyserial") from None
    return PosixPort(path, baud)


def type_line(port, line: str) -> str:
    """Type `line` and Enter, each character once the board has echoed the last.

    The board echoes every character it takes, which makes the echo a flow
    control: a console whose receive buffer is a 16-byte FIFO drops what
    arrives faster than it is read, and a line typed all at once by a program
    -- or by an emulator, which delivers it at no baud rate at all -- loses its
    tail. Returns what came back, so the caller sees the echo too.
    """
    seen = ""
    for ch in line:
        port.write(ch.encode())
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not seen.endswith(ch):
            data = port.read(0.05)
            if data:
                seen += data.decode("utf-8", "replace")
    port.write(b"\r")
    return seen


def run(port, command: str, silence: float, show) -> list[dict]:
    """Type `command`, follow the output to the next prompt, return the documents."""
    # Whatever the board printed before now is shown and dropped, so it cannot
    # be mistaken for a reply. A board that is quiet for a second is sitting at
    # its prompt already. One that is talking may be booting -- opening the port
    # resets many ESP32 boards -- and anything typed before its console driver
    # is up is lost, so then the wait is for the prompt itself.
    seen = ""
    quiet_until = time.monotonic() + 1.0
    boot_until = time.monotonic() + 15.0
    while time.monotonic() < (boot_until if seen else quiet_until):
        data = port.read(0.2)
        if data:
            text = data.decode("utf-8", "replace")
            show(text)
            seen = (seen + text)[-len(PROMPT):]
            if seen.endswith(PROMPT):
                break
    buf = type_line(port, command).replace("\r", "")

    docs: list[dict] = []
    body: list[str] | None = None
    echoed = False
    last = time.monotonic()
    while True:
        data = port.read(0.5)
        if not data:
            if time.monotonic() - last > silence:
                raise SystemExit(f"nothing from the board for {silence:.0f} s; "
                                 "giving up (raise --silence for a long run)")
            continue
        last = time.monotonic()
        buf += data.decode("utf-8", "replace").replace("\r", "")
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            if body is not None:
                if END.match(line):
                    docs.append(json.loads("\n".join(body)))
                    show(f"[document {len(docs)} received]\n")
                    body = None
                else:
                    body.append(line)
                continue
            if BEGIN.match(line):
                body = []
                continue
            show(line + "\n")
            if line.endswith(command):
                echoed = True
        # The prompt after the echoed command is the board saying it is done,
        # whether the run finished or the options were refused.
        if echoed and buf.endswith(PROMPT):
            return docs


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run cpcpub on a microcontroller and save its result.",
        epilog="Options after -- go to the board; the default is --full.")
    ap.add_argument("--port", help="serial device (default: the only one there is)")
    ap.add_argument("--baud", type=int, default=115200,
                    help="for a UART console; USB ignores it (default 115200)")
    ap.add_argument("-o", "--output",
                    help="where to write the document; - for stdout "
                         "(default: cpcpub-<chip>-<time>.json)")
    ap.add_argument("--silence", type=float, default=600.0,
                    help="seconds without output before giving up (default 600)")
    ap.add_argument("board_args", nargs=argparse.REMAINDER)
    a = ap.parse_args()

    args = [x for x in a.board_args if x != "--"] or ["--full"]
    if "--json" not in args:
        args.append("--json")
    command = " ".join(args)

    path = a.port or find_port()
    print(f"cpcpub-serial: {path}: {command}", file=sys.stderr)
    port = open_port(path, a.baud)
    try:
        docs = run(port, command, a.silence,
                   lambda s: (sys.stderr.write(s), sys.stderr.flush()))
    finally:
        port.close()

    if not docs:
        print("cpcpub-serial: the board printed no result", file=sys.stderr)
        return 1
    out = docs[0] if len(docs) == 1 else docs
    text = json.dumps(out, indent=2) + "\n"
    if a.output == "-":
        sys.stdout.write(text)
        return 0
    name = a.output
    if not name:
        chip = re.sub(r"[^A-Za-z0-9]+", "-", docs[0].get("system", {}).get("machine", "mcu"))
        stamp = datetime.now(tz=None).astimezone().strftime("%Y%m%d-%H%M%S")
        name = f"cpcpub-{chip}-{stamp}.json"
    with open(name, "w") as f:
        f.write(text)
    print(f"cpcpub-serial: wrote {name}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
