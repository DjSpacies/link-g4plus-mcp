"""Decode values from the runtime block (D 0x4CB6/714) with the address map in maps/. Read-only helpers."""
import csv
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNTIME_START = 0x4CB6

VERSIONS = {3671: "5.6.8.3671", 3631: "5.6.7.3631"}

def _rows(build):
    with open(HERE.parent / "maps" / f"address-map-{VERSIONS[build]}.tsv", encoding="utf8") as f:
        return list(csv.DictReader(f, delimiter="\t"))

def channels(build):
    """{dataid: (offset_in_block, size, multi, offset, name)} for runtime values with a known size."""
    out = {}
    for r in _rows(build):
        if r["region"] != "rtv" or not r["size"]: continue
        off = int(r["pclink_addr"], 16) - RUNTIME_START
        if off < 0: continue
        out[int(r["dataid"])] = (off, int(r["size"]), float(r["multi"] or 1), float(r["offset"] or 0), r["name"].strip())
    return out

def bit_channels(build):
    """{dataid: (offset_in_block, bit, bits, name)} for runtime bit fields (switch states such as AC Request)."""
    out = {}
    for r in _rows(build):
        if r["region"] != "rtv" or r["bit"] in ("", None) or r["bits"] in ("", None): continue
        off = int(r["pclink_addr"], 16) - RUNTIME_START
        if off < 0: continue
        out[int(r["dataid"])] = (off, int(r["bit"]), int(r["bits"]), r["name"].strip())
    return out

def bit_value(block, bc):
    off, bit, bits, _ = bc
    if off >= len(block): return None
    return (block[off] >> bit) & ((1 << bits) - 1)

def value(block, ch):
    """One decoded value, or None if the block is too short. Signed, as every runtime value checked so far."""
    off, size, multi, offset, _ = ch
    if off + size > len(block): return None
    return round(int.from_bytes(block[off:off + size], "little", signed=True) * multi + offset, 3)

ENGINE_SPEED, MAP, ECT, TP, POWER_ON_COUNTER = 1000, 1001, 1004, 1078, 2302
