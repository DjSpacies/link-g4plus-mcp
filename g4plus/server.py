#!/usr/bin/env python3
"""Live, read-only ECU tool for Link G4+: the read side of the ECU tool interface (docs/TOOLS.md).

  python -I server.py serve  COM6 [--log-dir DIR]                         JSON-RPC 2.0 over stdin/stdout
  python -I server.py record COM6 --seconds 60 --out RUN.live.json [--rate 20]

Operations: capabilities, connection_status, list_channels, read_channels, start_log, stop_log, get_log. With the
default --phase 5 there is no write operation: only `w`, `C`, `D` (and the connect speed switch after a power-up) can
be sent; anything else is refused in client.py before it reaches the port. --phase 6 serves tuning.TuningTool instead
(bounded fuel-table writes with the engine running; see tuning.py). Close PCLink first: it holds the port.

Logs are saved as `.live.json` (time_s, canonical channels, meta), which the ECU tuning skill's Link adapter
(github.com/DjSpacies/ecu-tuning-skill, platforms/link-g4plus/parser/adapter.py) reads, so its analyses run on them.
"""
import argparse, json, sys, threading, time, uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import client as C                   # noqa: E402
from units import CHANNELS           # noqa: E402

# Run as a script, this module is __main__; tuning.py imports it as `server`. Register it under that name too, so both
# share one ToolError class (otherwise handle() cannot catch errors raised in tuning.py).
if __name__ == "__main__": sys.modules.setdefault("server", sys.modules[__name__])

# Canonical channel -> runtime DataId (5.6.8.3671 address map). VERIFIED: checked against PCLink's display on a G4+
# Thunder (engine off and idling). The rest decode with the address map's scale and are plausible but not yet compared
# with PCLink. Injector duty is not mapped: DataId 1096 "Inj Duty Cycle (Stg)" is the staged duty.
CANONICAL = {
    "rpm": 1000, "map_kpa": 1001, "baro_kpa": 1051, "tps": 1078, "ect": 1004, "iat": 1011, "battery_v": 1081,
    "ign_adv": 1064, "inj_pw": 2967, "lambda": 3040, "lambda_target": 2936, "fuel_trim": 2392,
    "fuel_pressure_kpa": 1013, "fuel_temp": 1015, "oil_pressure_kpa": 1012, "oil_temp": 1014, "knock_count": 1099,
    "boost_target_kpa": 1317, "ethanol_pct": 2397,
}
VERIFIED = {"rpm", "map_kpa", "ect", "iat", "tps", "battery_v", "fuel_pressure_kpa", "ign_adv", "oil_pressure_kpa"}
DERIVED = {"mgp_kpa": ("map_kpa", "baro_kpa")}          # map - baro
# Fuel context: what else shapes the delivered fuel besides the cell being written. Recorded around every live write so
# a lambda change (or its absence) can be read correctly. Decoded with the address map's scale; NOT yet compared with
# PCLink, so treat values as indicative. Bit fields are raw states (meaning of each state not yet mapped).
FUEL_CONTEXT = {
    # closed loop
    "cll1_status": 2395, "lambda_trim_pct": 2392, "lambda_trim2_pct": 2400, "lambda1": 3040, "lambda_target": 2936,
    "lambda1_status": 1892,
    # table blend (Fuel Table 1 / 2)
    "fuel_table1_pct": 3025, "fuel_table2_pct": 3026, "fuel_blend_pct": 3730, "active_fuel_table_b0": 2576,
    "active_fuel_table_b1": 2577, "ethanol_pct": 2397, "ethanol_sensor_status": 1270,
    # background corrections and enrichments
    "warmup_enrich_pct": 3008, "post_start_pct": 3056, "iat_fuel_corr_pct": 2895, "fuel_4d_corr_pct": 3110,
    "accel_fuel_pct": 3038, "lag_fuel_trim_pct": 3068, "gear_cut_fuel_pct": 3063, "overrun_fuel_cut": 1622,
    "cut_pct": 1680, "baro_kpa": 1051, "fuel_pressure_kpa": 1013, "fuel_dp_kpa": 2398,
    # result
    "inj_pw_ms": 2967, "eff_inj_pw_ms": 2968, "inj_deadtime_ms": 3131,
    "ftrim_cyl1_pct": 2884, "ftrim_cyl2_pct": 2885, "ftrim_cyl3_pct": 2886, "ftrim_cyl4_pct": 2887,
    "ftrim_cyl5_pct": 2888, "ftrim_cyl6_pct": 2889, "ftrim_cyl7_pct": 2890, "ftrim_cyl8_pct": 2891,
}
OPS = ["capabilities", "connection_status", "list_channels", "read_channels", "fuel_context", "start_log", "stop_log", "get_log"]
NOT_SUPPORTED = ["list_tables", "get_table", "get_scalar", "list_scalars", "backup", "restore", "propose_write",
                 "apply_write", "set_trim_live", "get_protections", "event_stream"]

class ToolError(Exception):
    def __init__(self, code, message): super().__init__(message); self.code = code

def decode(block, ch):
    """One runtime value: 2-byte values signed (as checked so far), 1-byte values unsigned (temperatures with -50)."""
    off, size, multi, offset, _ = ch
    if off + size > len(block): return None
    raw = int.from_bytes(block[off:off + size], "little", signed=size == 2)
    return round(raw * multi + offset, 4)

class LiveTool:
    OPS = OPS
    def __init__(self, session, log_dir=None, clock=time.monotonic, ecu_name="Link G4+"):
        self.s, self.log_dir, self.clock, self.ecu_name = session, Path(log_dir) if log_dir else None, clock, ecu_name
        self.lock = threading.Lock()
        self.c_payload = self.s.connect()
        self.chs = {n: self.s.channels[d] for n, d in CANONICAL.items() if d in self.s.channels}
        self.t0, self.last, self.logs, self.logging = clock(), None, {}, None

    # ---- sampling ----
    def _sample(self):
        with self.lock: b = self.s.runtime()
        t = round(self.clock() - self.t0, 4)
        vals = {n: decode(b, ch) for n, ch in self.chs.items()}
        for n, (a, z) in DERIVED.items():
            if vals.get(a) is not None and vals.get(z) is not None: vals[n] = round(vals[a] - vals[z], 4)
        self.last = (t, vals); return t, vals

    def _names(self): return list(self.chs) + [n for n in DERIVED if all(x in self.chs for x in DERIVED[n])]

    def _check(self, names):
        ok = set(self._names())
        for n in names:
            if n not in ok: raise ToolError("unknown_channel", n)

    # ---- always available ----
    def capabilities(self):
        return dict(ecu_family=self.ecu_name, firmware=C.SUPPORTED_BUILDS.get(self.s.build, str(self.s.build)),
                    ops=["read_live", "logging"], limits={}, read_only=True)

    def connection_status(self):
        last = self.last[1] if self.last else {}
        return dict(connected=True, engine_running=bool(last.get("rpm")), logging=self.logging is not None,
                    link_errors=len(self.s.errors), transport=getattr(self.s.t, "settings", {}))

    # ---- live data ----
    def list_channels(self):
        out = [dict(name=n, unit=CHANNELS[n], native_name=ch[4], dataid=CANONICAL[n], verified=n in VERIFIED) for n, ch in self.chs.items()]
        out += [dict(name=n, unit=CHANNELS[n], native_name=" - ".join(DERIVED[n]), derived=True, verified=all(x in VERIFIED for x in DERIVED[n]))
                for n in self._names() if n in DERIVED]
        return out

    def read_channels(self, names, rate=None):
        self._check(names)
        if self.logging and self.last and self.clock() - self.t0 - self.last[0] < 0.25: t, vals = self.last   # sampler running
        else: t, vals = self._sample()
        return dict(time_s=t, values={n: dict(value=vals.get(n), unit=CHANNELS[n]) for n in names})

    def fuel_context(self):
        """One snapshot of closed-loop state, Fuel Table 1/2 blend and background fuel corrections (indicative)."""
        import runtime as R   # noqa: PLC0415
        with self.lock: b = self.s.runtime()
        out = {}
        for n, d in FUEL_CONTEXT.items():
            if d in self.s.bits: out[n] = dict(value=R.bit_value(b, self.s.bits[d]), dataid=d, native_name=self.s.bits[d][3], raw_state=True)
            elif d in self.s.channels: out[n] = dict(value=decode(b, self.s.channels[d]), dataid=d, native_name=self.s.channels[d][4])
        return dict(time_s=round(self.clock() - self.t0, 4), values=out, verified=False,
                    note="address-map scale, not yet compared with PCLink; bit fields are raw states")

    # ---- logging ----
    def start_log(self, channels, rate=20.0):
        if self.logging: raise ToolError("not_logging", "a log is already running; stop it first")
        self._check(channels)
        st = dict(channels=list(channels), rate=float(rate), time_s=[], data={n: [] for n in channels}, errors=[], stop=threading.Event())
        def run():
            period = 1.0 / st["rate"]; fails = 0
            while not st["stop"].is_set():
                t_next = time.monotonic() + period
                try:
                    t, vals = self._sample(); fails = 0
                    if not st["time_s"] or t > st["time_s"][-1]:
                        st["time_s"].append(t)
                        for n in st["channels"]: st["data"][n].append(vals.get(n))
                except C.EcuError as e:
                    fails += 1; st["errors"].append(dict(t=round(self.clock() - self.t0, 3), error=str(e)))
                    if fails >= 5: st["stopped"] = "5 reads in a row failed"; break
                time.sleep(max(0.0, t_next - time.monotonic()))
        st["thread"] = threading.Thread(target=run, daemon=True); self.logging = st; st["thread"].start()
        return dict(started=True, rate=st["rate"])

    def stop_log(self):
        st = self.logging
        if not st: raise ToolError("not_logging", "no log in progress")
        st["stop"].set(); st["thread"].join(timeout=5); self.logging = None
        id_ = uuid.uuid4().hex[:8]
        meta = dict(adapter="link-g4plus-live", ecu=self.ecu_name, firmware=self.s.build, rate_hz=st["rate"],
                    native_names={n: (self.chs[n][4] if n in self.chs else " - ".join(DERIVED[n])) for n in st["channels"]},
                    dataids={n: CANONICAL[n] for n in st["channels"] if n in CANONICAL},
                    verified=sorted(n for n in st["channels"] if n in VERIFIED or (n in DERIVED and all(x in VERIFIED for x in DERIVED[n]))),
                    read_errors=st["errors"], stopped=st.get("stopped"), started=time.strftime("%Y-%m-%dT%H:%M:%S"))
        keep = [i for i in range(len(st["time_s"])) if all(st["data"][n][i] is not None for n in st["channels"])]
        log = dict(time_s=[st["time_s"][i] for i in keep], channels={n: [st["data"][n][i] for i in keep] for n in st["channels"]},
                   units={n: CHANNELS[n] for n in st["channels"]}, meta=meta)
        self.logs[id_] = log
        out = dict(id=id_, samples=len(log["time_s"]), read_errors=len(st["errors"]), stopped=st.get("stopped"))
        if self.log_dir:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            p = self.log_dir / f"{time.strftime('%Y-%m-%d_%H%M%S')}_{id_}.live.json"; p.write_text(json.dumps(log))
            out["path"] = str(p)
        return out

    def get_log(self, id):
        if id not in self.logs: raise ToolError("unknown_log", id)
        return self.logs[id]

    def close(self):
        if self.logging: self.stop_log()
        self.s.t.close()
        if self.s.trace: self.s.trace.close()

def handle(tool, req):
    """One JSON-RPC 2.0 request to one response ."""
    rid = req.get("id"); method = req.get("method"); params = req.get("params") or {}
    if method not in getattr(tool, "OPS", OPS): return dict(jsonrpc="2.0", id=rid, error=dict(code=-32601, message=f"not supported: {method}" if method in NOT_SUPPORTED else f"unknown method {method}"))
    try: return dict(jsonrpc="2.0", id=rid, result=getattr(tool, method)(**params))
    except ToolError as e: return dict(jsonrpc="2.0", id=rid, error=dict(code=-32000, message=str(e), data=dict(reason=e.code)))
    except (C.EcuError, C.Refused) as e: return dict(jsonrpc="2.0", id=rid, error=dict(code=-32000, message=str(e), data=dict(reason="link")))
    except TypeError as e: return dict(jsonrpc="2.0", id=rid, error=dict(code=-32602, message=str(e)))

def serve(tool, stdin=sys.stdin, stdout=sys.stdout):
    for line in stdin:
        if line.strip(): stdout.write(json.dumps(handle(tool, json.loads(line))) + "\n"); stdout.flush()

def open_tool(port, log_dir=None, trace=None, phase=5, settings=None, profile=None, backup_dir=None, journal=None,
              stationary=False, arm_channel=None, approval="chat"):
    if phase == 5:
        return LiveTool(C.Session(C.SerialTransport(port), phase=5, trace_path=trace), log_dir=log_dir)
    import tuning, writable   # noqa: PLC0415
    if not (settings and profile and backup_dir and journal): sys.exit("phase 6 needs --settings --profile --backup-dir --journal")
    if Path(journal).exists() and json.loads(Path(journal).read_text() or "[]"):
        sys.exit("journal is not empty: a previous session's changes may still be in the ECU; revert or archive it first")
    sets = writable.load(settings)["settings"]
    s = C.Session(C.SerialTransport(port), phase=6, trace_path=trace, settings=sets, journal_path=journal,
                  stationary=stationary, arm_channel=arm_channel, approval=approval)
    return tuning.TuningTool(s, sets, json.loads(Path(profile).read_text()), backup_dir, log_dir=log_dir)

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("cmd", choices=["serve", "record"]); ap.add_argument("port", nargs="?", default="auto")
    ap.add_argument("--log-dir"); ap.add_argument("--seconds", type=float, default=60); ap.add_argument("--rate", type=float, default=20)
    ap.add_argument("--out", help="record: the .live.json file to write"); ap.add_argument("--trace", help="JSONL frame trace")
    ap.add_argument("--phase", type=int, choices=[5, 6], default=5, help="5: read only (default); 6: fuel-table writes, engine running")
    ap.add_argument("--settings", help="phase 6: writable settings JSON (writable.py)"); ap.add_argument("--profile", help="phase 6: setup profile JSON")
    ap.add_argument("--backup-dir", help="phase 6: where backups go"); ap.add_argument("--journal", help="phase 6: write journal JSON")
    ap.add_argument("--stationary", action="store_true", help="phase 6: the owner declares the vehicle stationary (dyno or parked)")
    ap.add_argument("--arm-channel", type=int, default=None, help="phase 6, handshake approval: runtime DataId of the pedal or a switch")
    ap.add_argument("--approval", choices=["chat", "handshake"], default="chat",
                    help="phase 6: how writes are approved (default chat: the owner approves in the chat)")
    a = ap.parse_args()
    tool = open_tool(a.port, a.log_dir, a.trace, a.phase, a.settings, a.profile, a.backup_dir, a.journal, a.stationary,
                     a.arm_channel, a.approval)
    try:
        if a.cmd == "serve": serve(tool); return
        if not a.out: sys.exit("record needs --out")
        names = tool._names(); tool.start_log(names, rate=a.rate); t_end = time.monotonic() + a.seconds
        show = [n for n in ("rpm", "map_kpa", "lambda", "ect", "tps", "battery_v") if n in names]
        while time.monotonic() < t_end:
            time.sleep(min(5.0, max(0.0, t_end - time.monotonic())))
            if tool.last: print("  " + "  ".join(f"{n}={tool.last[1].get(n)}" for n in show), flush=True)
        r = tool.stop_log(); log = tool.get_log(r["id"])
        Path(a.out).write_text(json.dumps(log))
        print(json.dumps(dict(r, out=a.out, link_errors=len(tool.s.errors), seconds=a.seconds), indent=1))
    finally:
        tool.close()

if __name__ == "__main__":
    main()
