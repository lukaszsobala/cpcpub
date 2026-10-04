"""Pack PNG files into one .ico, each stored as PNG: pack-ico.py OUT PNG..."""

import struct
import sys

out, pngs = sys.argv[1], [open(p, "rb").read() for p in sys.argv[2:]]
offset = 6 + 16 * len(pngs)
head, body = [struct.pack("<HHH", 0, 1, len(pngs))], []
for png in pngs:
    width, height = struct.unpack(">II", png[16:24])  # from the IHDR chunk
    # 256 does not fit a byte, and the format spells it 0.
    head.append(struct.pack("<BBBBHHII", width % 256, height % 256, 0, 0, 1, 32,
                            len(png), offset))
    body.append(png)
    offset += len(png)
with open(out, "wb") as fh:
    fh.write(b"".join(head + body))
