#!/usr/bin/env python3
"""Read-only runtime probe (phase 3 rules: only `w`, `C`, `D` can be sent). Reads the runtime block repeatedly for a
while, so that a person at the car can do something (press and release the accelerator pedal) and we can see:
  - which runtime values moved, and by how much (to confirm the pedal's DataId for arming), and
  - whether link errors happen, when, and of what kind (every frame is traced).

  python -I probe_runtime.py COM6 --seconds 40 --out probe.json [--interval 0.05] [--blocks blocks.jsonl]
                             [--lines off-after-open|off|on] [--pace-us US]

Close PCLink first. Nothing here can write: the session is phase 3.
"""
import argparse, json, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import client as C, runtime as R   # noqa: E402

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("port"); ap.add_argument("--seconds", type=float, default=40)
    ap.add_argument("--interval", type=float, default=0.05); ap.add_argument("--out", required=True)
    ap.add_argument("--blocks", help="also save every runtime block (hex, JSONL)")
    ap.add_argument("--watch", type=int, nargs="*", default=[], help="DataIds (bit fields too) whose every change is printed and reported")
    ap.add_argument("--lines", choices=C.SerialTransport.LINES, default="off-after-open",
                    help="DTR/RTS handling (default: clear them after opening)")
    ap.add_argument("--lines-on", action="store_true", help="same as --lines on (pyserial's default; comparison only)")
    ap.add_argument("--pace-us", type=float, default=None,
                    help="byte pacing: default is the transport's (0: one byte per transfer, as PCLink); "
                         "N > 0 adds N us between bytes; a negative value writes requests in one go (comparison only)")
    a = ap.parse_args()
    trace = str(Path(a.out).with_suffix(".trace.jsonl"))
    kw = {} if a.pace_us is None else dict(pace_us=None if a.pace_us < 0 else a.pace_us)
    tr = C.SerialTransport(a.port, lines="on" if a.lines_on else a.lines, **kw)
    s = C.Session(tr, phase=3, trace_path=trace)
    rep = dict(port=a.port, seconds=a.seconds, interval=a.interval, trace=trace, transport=tr.settings)
    blocks_f = open(a.blocks, "w") if a.blocks else None
    stats, reads, failed, t_start = {}, 0, [], None
    watched, changes = {}, []
    try:
        s.connect(); rep["build"] = s.build
        chs = s.channels
        print(f"Reading for {a.seconds:.0f} s. At the car: press the accelerator fully and release it, two or three "
              "times, a few seconds apart.", flush=True)
        t_start = time.monotonic()
        while time.monotonic() - t_start < a.seconds:
            t = round(time.monotonic() - t_start, 3)
            try: b = s.runtime()
            except C.EcuError as e: failed.append(dict(t=t, error=str(e))); time.sleep(a.interval); continue
            reads += 1
            for d in a.watch:
                v = s.live(d, block=b)
                if watched.get(d, object()) != v:
                    if d in watched: print(f"  t={t:6.2f}s  DataId {d}: {watched[d]} -> {v}", flush=True)
                    changes.append(dict(t=t, dataid=d, value=v)); watched[d] = v
            if blocks_f: blocks_f.write(json.dumps(dict(t=t, hex=b.hex())) + "\n")
            for i, ch in chs.items():
                v = R.value(b, ch)
                if v is None: continue
                st = stats.setdefault(i, dict(name=ch[4], first=v, min=v, max=v, t_min=t, t_max=t))
                if v < st["min"]: st["min"], st["t_min"] = v, t
                if v > st["max"]: st["max"], st["t_max"] = v, t
                st["last"] = v
            time.sleep(a.interval)
    except (C.Refused, C.EcuError) as e:
        rep["stopped"] = f"{type(e).__name__}: {e}"
    finally:
        s.t.close()
        if blocks_f: blocks_f.close()
        if s.trace: s.trace.close()
    moved = {i: st for i, st in stats.items() if st["max"] != st["min"]}
    rep.update(reads_ok=reads, reads_failed=failed, watch_changes=changes, link_errors_incl_retried=s.errors,
               moved=dict(sorted(((str(i), st) for i, st in moved.items()), key=lambda x: -(x[1]["max"] - x[1]["min"]))),
               pedal_candidates={str(i): stats[i] for i in (1650, 1654, R.TP) if i in stats})
    Path(a.out).write_text(json.dumps(rep, indent=1))
    print(json.dumps(dict(transport=rep["transport"], reads_ok=reads, reads_failed=len(failed), retried_errors=len(s.errors),
                          pedal_candidates=rep["pedal_candidates"],
                          moved=[f"{i} {st['name']}: {st['min']}..{st['max']}" for i, st in list(rep["moved"].items())[:25]]), indent=1))

if __name__ == "__main__":
    main()
