#!/usr/bin/env python3
"""Build the list of settings the live client may write, from the address map, a byte-exact ECU image and the
.pclr saved from the same ECU. Read-only; nothing here talks to an ECU.

  python -I writable.py IMAGE.bin TUNE.pclr --exe PCLink.exe --out SETTINGS.json

A setting is listed only if it is a numeric table or scalar inside the tune window (outside the statistics, identity
and calibration block 0x4C3E-0x4CB5), its cells are 1 or 2 bytes, and every value in the .pclr equals
raw * multi + offset from the image. `scale`:
  verified      the address map's multi/offset fit, with at least two distinct values
  fitted        the map's scale does not fit; multi/offset fitted exactly from two or more distinct values
  single-value  one distinct value, which the map's scale fits (the multiplier is not confirmed)
  unknown       one distinct value that the map's scale does not give (multi/offset unknown; raw only) Bit fields and enumerations are never
listed. The output describes one ECU and tune, so keep it with that car's files, not in this repository.
"""
import argparse, csv, hashlib, json, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import client as C   # noqa: E402

PROTECTED = (0x4C3E, 0x4CB6)    # statistics, identity, MAP/BAP calibration: updated by the ECU itself, never written

def _fit(raws, want, multi, offset):
    return all(abs(r * multi + offset - w) <= abs(multi) / 2 + 1e-6 for r, w in zip(raws, want))

def _linear(raws, want):
    pts = sorted(set(zip(raws, want)))
    if len({x for x, _ in pts}) != len(pts) or len(pts) < 2: return None, None
    (x0, y0), (x1, y1) = pts[0], pts[-1]
    m = (y1 - y0) / (x1 - x0); return round(m, 9), round(y0 - m * x0, 6)

def build(image, tune, rows, build_no):
    out, skipped = [], {}
    def skip(why): skipped[why] = skipped.get(why, 0) + 1
    for r in rows:
        if r["region"] != "tune": continue
        if r["type"] == "3" or r["bits"]: skip("bit field or enumeration"); continue
        i, a, size = int(r["dataid"]), int(r["pclink_addr"], 16), int(r["size"] or 0)
        multi, offset = float(r["multi"] or 1), float(r["offset"] or 0)
        if i in tune["tables"]:
            t = tune["tables"][i]; want = [v for row in t for v in row]; shape = [len(t), len(t[0]) if t else 0]
        elif i in tune["scalars"]:
            try: want = [float(tune["scalars"][i])]
            except (TypeError, ValueError): skip("text value"); continue
            shape = [1, 1]
        else: skip("not in the .pclr"); continue
        n = len(want)
        if not n or not size or size % n: skip("size unknown"); continue
        w = size // n
        if w not in (1, 2): skip("cell width not 1 or 2 bytes"); continue
        if a + size > C.TUNE_END: skip("outside the tune window"); continue
        if a < PROTECTED[1] and a + size > PROTECTED[0]: skip("statistics, identity or calibration"); continue
        distinct = len(set(want)); fits, scale = [], None
        raws_by = {sg: [int.from_bytes(image[a + k * w:a + (k + 1) * w], "little", signed=sg) for k in range(n)]
                   for sg in (False, True)}
        for signed in (False, True):
            if _fit(raws_by[signed], want, multi, offset): fits.append(signed)
        if fits: scale = "verified" if distinct >= 2 else "single-value"
        elif distinct >= 2:                                     # the map's multi/offset are wrong: fit from the data
            for signed in (False, True):
                m, o = _linear(raws_by[signed], want)
                if m and _fit(raws_by[signed], want, m, o): fits.append(signed); multi, offset = m, o
            if fits: scale = "fitted"
        else:                                                   # one value, and the map's scale does not give it
            fits, scale, multi, offset = [False, True], "unknown", None, None
        if not fits: skip("scale does not match the .pclr"); continue
        out.append(dict(dataid=i, name=r["name"].strip(), kind="table" if i in tune["tables"] else "scalar",
                        addr=f"0x{a:04X}", width=w, signed=fits[-1] if len(fits) == 1 else False,
                        signed_known=len(fits) == 1, multi=multi, offset=offset, units=r["units"], shape=shape,
                        scale=scale, raw_first=raws_by[False][0], value_first=want[0]))
    return dict(build=build_no, image_sha256=hashlib.sha256(image).hexdigest(), settings=out, skipped=skipped)

def add_parents(d, definitions, tune):
    """From PCLink's parameter definitions (VisibleChildren): each table's PCLink name and the setting that shows it
    (e.g. a GP table's function), with that setting's value in this tune. Read-only; nothing is stored but names."""
    defs = json.loads(Path(definitions).read_text())["definitions"]
    parent = {}
    for pid, recs in defs.items():
        p = recs[0].get("properties", {}); vc = (p.get("VisibleChildren") or [{}])[0].get("Value", "")
        for c in {x.strip("\" ") for x in vc.split(",")}:
            if c.isdigit(): parent.setdefault(int(c), (int(pid), recs[0]["name"]))
    for st in d["settings"]:
        if st["dataid"] in parent:
            pid, pname = parent[st["dataid"]]
            st["parent"] = dict(dataid=pid, name=pname, value=tune["scalars"].get(pid))
    return d

def load(path):
    """Settings for client.Session: the JSON written by this script."""
    d = json.loads(Path(path).read_text())
    for s in d["settings"]: s["start"] = int(s["addr"], 16); s["end"] = s["start"] + s["width"] * s["shape"][0] * s["shape"][1]
    return d

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("image"); ap.add_argument("pclr")
    ap.add_argument("--exe", required=True, help="local PCLink.exe (the tune file's key is read from it, never stored)")
    ap.add_argument("--build", type=int, default=3671); ap.add_argument("--out", required=True)
    ap.add_argument("--definitions", help="PCLink parameter definitions JSON (pclink-g4plus analysis), for table parents")
    a = ap.parse_args()
    import pclr   # noqa: PLC0415
    rows = list(csv.DictReader(open(HERE.parent / "maps" / f"address-map-{C.SUPPORTED_BUILDS[a.build]}.tsv", encoding="utf8"), delimiter="\t"))
    tune = pclr.parse_tune(a.pclr, exe=a.exe)
    d = build(Path(a.image).read_bytes(), tune, rows, a.build)
    if a.definitions: add_parents(d, a.definitions, tune)
    Path(a.out).write_text(json.dumps(d, indent=1))
    by = {}
    for s in d["settings"]: by[s["scale"]] = by.get(s["scale"], 0) + 1
    print(f"{len(d['settings'])} writable settings {by}; skipped {d['skipped']}")

if __name__ == "__main__":
    main()
