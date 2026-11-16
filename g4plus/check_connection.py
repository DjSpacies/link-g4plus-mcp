#!/usr/bin/env python3
"""Connection check, READ ONLY: compares the ECU's tune with a saved image and reads live values.

  python -I check_connection.py COM6 path\\to\\ecu-image.bin [--out report.json]

Sends only `w`, `C` and `D` (the session is phase 3, so anything else is refused in code before it is sent).
Reads the regions PCLink reads, compares them byte for byte with the backup image, reads the runtime block 20 times
and decodes a few values. Close PCLink first: it holds the port exclusively.
"""
import argparse, hashlib, json, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import client as C, runtime as R   # noqa: E402

REGIONS = [(0x4C3E, 120), (0x4F80, 5852), (0x0000, 0x3FC6), (0x3FC6, 3192)]
SHOW = {1000: "Engine Speed", 1001: "MAP", 1004: "ECT", 1078: "TP", 2302: "PowerOnCounter"}   # by DataId
SHOW_NAMES = {"Battery Voltage", "IAT"}   # first entry of each name

def decode_runtime(block, build):
    chs, out = R.channels(build), {}
    for i, name in SHOW.items():
        if i in chs: out[name] = R.value(block, chs[i])
    for i, ch in sorted(chs.items()):
        if ch[4] in SHOW_NAMES and ch[4] not in out: out[ch[4]] = R.value(block, ch)
    return out

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("port"); ap.add_argument("image"); ap.add_argument("--out", default="connection-report.json")
    a = ap.parse_args()
    ref = Path(a.image).read_bytes()
    s = C.Session(C.SerialTransport(a.port), phase=3)
    rep = dict(port=a.port, image=a.image, image_sha256=hashlib.sha256(ref).hexdigest())
    try:
        c = s.connect(); rep["build"] = s.build; rep["c_reply"] = c.hex()
        diffs = []
        for addr, n in REGIONS:
            got = s.read(addr, n, timeout=3.0)
            d = [addr + k for k in range(n) if got[k] != ref[addr + k]]
            diffs.append(dict(addr=f"0x{addr:04X}", count=n, differing=len(d), first=[f"0x{x:04X}" for x in d[:10]]))
        rep["regions"] = diffs
        samples = []
        for _ in range(20):
            samples.append(decode_runtime(s.runtime(), s.build)); time.sleep(0.05)
        rep["runtime_first"], rep["runtime_last"] = samples[0], samples[-1]
        rep["ok"] = all(d["differing"] == 0 for d in diffs)
    finally:
        s.t.close()
    Path(a.out).write_text(json.dumps(rep, indent=1))
    print(json.dumps(rep, indent=1))

if __name__ == "__main__":
    main()
