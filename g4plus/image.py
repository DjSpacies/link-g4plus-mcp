#!/usr/bin/env python3
"""Extract the ECU memory image that PCLink downloaded at connect, from a USBPcap capture. Read-only.

  python3 -I image.py CAPTURE.pcap OUT_PREFIX

Writes OUT_PREFIX.bin (bytes 0x0000-0x665B as read; bytes never read are 0xFF) and OUT_PREFIX.json (regions read,
their checksums, the 'C' reply, and SHA-256 of the image). The image is a byte-exact record of the tune that ran,
independent of PCLink's .pclr file.
"""
import hashlib, json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import usbpcap, protocol as P   # noqa: E402

RUNTIME = 0x4CB6
SIZE = 0x665C

def extract(path):
    chunks, _ = usbpcap.read(path)
    img, regions, c_reply = bytearray(b"\xff" * SIZE), [], None
    for t, req, rep in P.decode_session(usbpcap.streams(chunks)):
        if req["cmd"] == "C" and rep.get("ok"): c_reply = rep["data"]
        if req["cmd"] != "D" or not rep.get("ok") or req["addr"] == RUNTIME: continue
        a, n = req["addr"], req["count"]
        if a + n > SIZE: continue
        img[a:a + n] = rep["data"]
        regions.append(dict(addr=a, count=n, t=round(t, 3)))
    return bytes(img), regions, c_reply

def main():
    if len(sys.argv) != 3: sys.exit(__doc__)
    img, regions, c = extract(sys.argv[1])
    out = Path(sys.argv[2])
    out.with_suffix(".bin").write_bytes(img)
    meta = dict(capture=Path(sys.argv[1]).name, size=len(img), sha256=hashlib.sha256(img).hexdigest(),
                regions=[dict(addr=f"0x{r['addr']:04X}", count=r["count"], t=r["t"]) for r in regions],
                c_reply=c.hex() if c else None, unread_is=0xFF)
    out.with_suffix(".json").write_text(json.dumps(meta, indent=1))
    print(f"{len(regions)} regions, {sum(r['count'] for r in regions)} bytes, sha256 {meta['sha256'][:16]}")

if __name__ == "__main__":
    main()
