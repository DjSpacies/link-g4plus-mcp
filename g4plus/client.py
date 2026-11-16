"""Live client for Link G4+ ECUs. Enforces docs/SAFETY.md in code: phase-gated command allowlist, tune-window-only
writes, a journal of previous bytes before every write, echo check and read-back, and revert.

Nothing here runs by itself. A transport (SerialTransport for a real ECU, sim.SimTransport for tests) is passed in.
Only frames observed from PCLink are sent: `w`, `C`, `D`, `P`, and `Q` with an allowlisted sub-code. Writes also need
an armed session: a pedal handshake seen in live data, which only a person at the car can do (arm()).
"""
import json, struct, time
from pathlib import Path

import protocol as P, runtime as R

TUNE_END = 0x4CB6          # tune window 0x0000-0x4CB5 (P writes only here)
RUNTIME = (0x4CB6, 714)    # live block PCLink polls
READ_LIMIT = 0x665C        # highest address PCLink reads
SUPPORTED_BUILDS = {3671: "5.6.8.3671"}   # 5.6.7.3631 has a map too, but nothing observed yet

# Phase gates. A command not listed for the current phase is never sent.
PHASES = {
    3: {"w", "C", "D"},
    4: {"w", "C", "D", "P"},
    5: {"w", "C", "D"},
    6: {"w", "C", "D", "P"},
    7: {"w", "C", "D", "P", ("Q", 0x000B)},
}
NEVER = {"F", "s", "T", "A", "W", "E"}   # never sent by this tool (W/E: not observed from PCLink)
FAST_BAUD, BOOT_BAUD = 625000, 115200   # link speed PCLink uses; the speed an ECU has after power-up
SPEED_Q = (0x0003, 0x0006, 0x0000)       # Q 0003 0006 0000: switch the link to 625000 (only inside connect())
WRITE_SCALES = {4: {"verified", "fitted", "single-value", "unknown"},   # phase 4: one raw step in an unused setting
                6: {"verified", "fitted"}}                             # running engine: scale must be known

# Arming: a pedal handshake that only a person at the car can do. Project defaults, not vendor figures.
ARM = dict(press_tp=80.0,        # % pedal that counts as "pressed fully"
           release_margin=3.0,   # % above the resting TP that counts as "released"
           handshake_s=20.0,     # time allowed for press and release 
           armed_s=600.0)        # writes allowed for this long after a handshake
# Pedal travel that counts as "pressed" per phase. Engine off (4): a full press. Engine running (6, 7): a small blip,
# 10 %, so the engine is not revved much.
ARM_PRESS = {4: 80.0, 6: 10.0, 7: 10.0}

# Enumerated settings the client may write, per firmware build: one bit field each, with its allowed values. A field
# stays locked (confirmed=False) until a USB capture shows PCLink changing it with a single P to the same byte and bits
# and nothing else.
FIELD_WRITES = {3671: {
    # Confirmed by two USB captures of PCLink: OFF is `P 0x0002` CD -> C1, back is C1 -> CD, and CLL 1 Status follows.
    # PCLink also bumps byte 0x0037 ("PClink Confrim by", its own edit counter) and re-reads 0x0000/512; this tool
    # does not touch the counter.
    2394: dict(name="Lambda Mode", addr=0x0002, bit=2, bits=2, confirmed=True,
               values={0: "OFF", 1: "Stoich Mode (Narrow Band)", 2: "Stoich Mode (Wide Band)", 3: "Auto Mode (Wide Band)"}),
}}

ECU_USB = (0x12B8, 0xEC60)   # "Link Engine Management USB ECU"

class Refused(Exception):
    """The request breaks a rule in docs/SAFETY.md. Nothing was sent."""

def find_port():
    """The serial port of the one Link ECU plugged in by USB, found by its USB ID. Refuses on none or several."""
    try: from serial.tools import list_ports   # noqa: PLC0415
    except ImportError as e: raise RuntimeError("pyserial is needed for a real ECU connection") from e
    found = [p.device for p in list_ports.comports() if (p.vid, p.pid) == ECU_USB]
    if not found: raise Refused("no Link ECU found on USB (12B8:EC60): check the cable and that the key is on")
    if len(found) > 1: raise Refused(f"several Link ECUs found ({', '.join(found)}): give the port")
    return found[0]

def pclink_running():
    """The name of a running PCLink process, or None. PCLink holds the ECU's port while it runs."""
    import os, subprocess   # noqa: PLC0415
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=10).stdout
            names = [l.split('","')[0].strip('"') for l in out.splitlines() if l.strip()]
        else:   # PCLink under Wine
            names = []
            for pid in filter(str.isdigit, os.listdir("/proc")):
                try: names.append(Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace"))
                except OSError: pass
    except (OSError, subprocess.SubprocessError): return None
    import re   # noqa: PLC0415
    exe = re.compile(r"(^|[\\/\s\"])pclink\.exe(\"|\s|$)", re.I)   # the program itself, not paths that mention it
    return next((n for n in names if exe.search(n)), None)

def resolve_port(port):
    """`port`, or the auto-detected one for None/"auto". Refuses while PCLink runs."""
    p = pclink_running()
    if p: raise Refused(f"PCLink is running ({p.strip()[:60]}): close it first, it holds the ECU's port")
    return find_port() if port in (None, "", "auto") else port

class EcuError(Exception):
    """The ECU answered with '?' or an invalid reply."""

class SerialTransport:
    """A real serial port (pyserial). Settings as observed from PCLink: 625000 baud, 8N1, no flow control, DTR and RTS
    off (PCLink's SET_FLOW 00000000 00000000 and SET_MHS 0x0200/0x0100). pyserial asserts both lines on open unless
    told otherwise; with them asserted, test runs had link errors that PCLink never had.

    `lines`: "off-after-open" (default) opens the port, then clears DTR and RTS; "off" sets them off before opening
    (with pyserial 3.5 on Windows 11 nothing was then sent at all); "on" is pyserial's
    default (link errors; comparison only). `lines_on=True` is the old spelling of lines="on".
    `pace_us`: 0 (default) writes a request one byte per USB transfer, like PCLink (about 180 us apart); > 0 also
    waits that long between bytes; None writes it in one go (garbled requests at the ECU).
    Defaults chosen from test runs: off-after-open + pace 0 + fixed timeout ran 1,505 runtime reads with no link error
    (docs/PROTOCOL.md, "Serial settings").

    The port is configured once. pyserial on Windows re-applies the whole port setup (baud, line control, flow, DTR/RTS)
    every time its timeout changes, so reads use a short fixed timeout and loop until their own deadline."""
    LINES = ("off-after-open", "off", "on")
    POLL_S = 0.02    # fixed pyserial read timeout; each read() loops on this until its own deadline

    def __init__(self, port, timeout=0.5, lines="off-after-open", pace_us=0, lines_on=None):
        try: import serial   # noqa: PLC0415
        except ImportError as e: raise RuntimeError("pyserial is needed for a real ECU connection") from e
        if lines_on is not None: lines = "on" if lines_on else lines
        if lines not in self.LINES: raise ValueError(f"lines must be one of {self.LINES}")
        port = resolve_port(port)
        s = serial.Serial()
        s.port, s.baudrate, s.bytesize, s.parity, s.stopbits = port, FAST_BAUD, 8, "N", 1
        s.timeout, s.write_timeout = self.POLL_S, 1.0
        s.rtscts = s.dsrdtr = s.xonxoff = False
        self.s, self.pace_us, self.lines = s, pace_us, lines
        self._open()
        self.settings = dict(lines=lines, pace_us=pace_us, poll_s=self.POLL_S, baud=FAST_BAUD)
    def _open(self):
        s = self.s
        if self.lines == "off": s.dtr = s.rts = False                 # before open
        elif self.lines == "off-after-open": s.dtr = s.rts = True     # pyserial's default at open; cleared just after
        s.open()
        if self.lines == "off-after-open": s.dtr = False; s.rts = False   # after open: only the line states change
    def set_baud(self, baud):
        """Close the port and reopen it at `baud` (as PCLink does), so pyserial never reconfigures an open port."""
        self.s.close(); self.s.baudrate = baud; self._open()
        self.settings["baud"] = baud
    def write(self, b):
        self.s.reset_input_buffer()
        if self.pace_us is None: self.s.write(b); self.s.flush(); return
        for i, x in enumerate(b):
            if i and self.pace_us:
                end = time.perf_counter() + self.pace_us / 1e6
                while time.perf_counter() < end: pass
            self.s.write(bytes([x])); self.s.flush()
    def read(self, n, timeout):
        """Up to n bytes; keeps reading until n bytes arrived or `timeout` seconds passed in total."""
        out, end = b"", time.monotonic() + timeout
        while len(out) < n and time.monotonic() < end:
            out += self.s.read(n - len(out))     # returns after at most POLL_S; the timeout is never changed
        return out
    def drain(self, settle=0.1):
        """Wait for stray bytes to stop arriving, then return and discard them (for resynchronising after an error)."""
        got, end = b"", time.monotonic() + 1.0
        while time.monotonic() < end:
            time.sleep(settle); k = self.s.in_waiting
            if not k: break
            got += self.s.read(k)
        self.s.reset_input_buffer(); return got
    def close(self): self.s.close()

class Session:
    def __init__(self, transport, phase, journal_path=None, settings=None, max_step=1, clock=time.monotonic,
                 sleep=time.sleep, trace_path=None, arm_channel=None, read_retries=2, stationary=False, approval="handshake"):
        """`settings`: writable settings (writable.load()["settings"]); P is refused anywhere else. `max_step`: the
        largest change per write, in raw counts, from the value read just before it."""
        if phase not in PHASES: raise Refused(f"unknown phase {phase}")
        self.t, self.phase, self.allowed = transport, phase, PHASES[phase]
        self.journal_path = Path(journal_path) if journal_path else None
        self.journal = []          # [(addr, old_bytes, new_bytes)]
        self.settings = list(settings or [])
        self.max_step, self.clock, self.sleep = max_step, clock, sleep
        self.build, self.channels, self.armed_until = None, None, None
        self.bits = {}
        self._speed_switching = False
        self.stationary = bool(stationary)   # phases 6-7: the owner declares the vehicle stationary (dyno or parked)
        self.trace = open(trace_path, "a", encoding="utf8") if trace_path else None   # every frame, as hex
        self.arm_channel, self.read_retries = arm_channel, read_retries
        if approval not in ("handshake", "chat"): raise Refused(f"unknown approval mode {approval}")
        self.approval = approval
        self.errors = []           # [(t, cmd, error, raw_hex)] for reads that failed, including retried ones

    # -- framing ------------------------------------------------------------------------------------------------
    def _gate(self, key):
        cmd = key[0] if isinstance(key, tuple) else key
        if key == ("Q", SPEED_Q[0]) and self._speed_switching: return   # only from _switch_speed(), inside connect()
        if cmd in NEVER or key not in self.allowed:
            raise Refused(f"{key} is not allowed in phase {self.phase}")

    def _switch_speed(self):
        """`Q 0003 0006 0000`: switch the ECU's link from 115200 to 625000 baud. Only called by connect(), after a
        `w` at 115200 was answered; exact parameters; never retried (Q is not a read)."""
        self._speed_switching = True
        try:
            self._gate(("Q", SPEED_Q[0]))
            self._xfer(P.frame_q(*SPEED_Q), dict(cmd="Q"), 1)
        finally:
            self._speed_switching = False

    def _note(self, **kw):
        if self.trace: self.trace.write(json.dumps(dict(t=round(time.time(), 4), **kw)) + "\n"); self.trace.flush()

    def _once(self, req_bytes, req, n_expected, timeout):
        self._note(tx=req_bytes.hex() if len(req_bytes) < 40 else req_bytes[:40].hex() + "...")
        t0 = self.clock(); self.t.write(req_bytes)
        first = self.t.read(1, timeout)
        if not first: raise EcuError("no reply", b"")
        if first == b"?":
            rest = self.t.read(2, timeout); code = struct.unpack("<H", rest)[0] if len(rest) == 2 else None
            raise EcuError(P.ERRORS.get(code, f"error {code}"), first + rest)
        rest = self.t.read(n_expected - 1, timeout)
        raw = first + rest
        self._note(rx_len=len(raw), rx_head=raw[:16].hex(), ms=round((self.clock() - t0) * 1000, 1))
        if len(raw) < n_expected: raise EcuError(f"short reply: {len(raw)} of {n_expected} bytes", raw)
        rep = P.parse_reply(req, raw)
        if not rep["ok"]: raise EcuError(rep["error"], raw)
        return rep["data"]

    def _xfer(self, req_bytes, req, n_expected, timeout=0.5):
        """One request. Reads (w, C, D) are retried after resynchronising; a write (P) or Store is never retried."""
        tries = 1 + (self.read_retries if req["cmd"] in ("w", "C", "D") else 0)
        for k in range(tries):
            try: return self._once(req_bytes, req, n_expected, timeout)
            except EcuError as e:
                raw = e.args[1] if len(e.args) > 1 else b""
                self.errors.append((round(time.time(), 3), req["cmd"], e.args[0], raw[:24].hex()))
                stray = self.t.drain() if hasattr(self.t, "drain") else b""
                self._note(error=e.args[0], raw_head=raw[:24].hex(), raw_len=len(raw), stray_len=len(stray), attempt=k + 1)
                if k + 1 == tries: raise EcuError(e.args[0]) from None

    # -- read side ----------------------------------------------------------------------------------------------
    def wake(self):
        self._gate("w")
        data = self._xfer(b"w", dict(cmd="w"), 3)
        return data

    def connect(self):
        """`w`, then `C`; checks the firmware build is supported. Returns the `C` payload.

        A freshly powered ECU talks at 115200 baud until `Q 0003 0006 0000` switches it to 625000 (PCLink's first
        connect after a power-up). So: `w` at 625000; on no reply, `w` at 115200, and only if that
        answers exactly `$ 01 00`, the speed switch, then `w` at 625000 again. Nothing else is sent at 115200."""
        try:
            self.wake()
        except EcuError:
            if not hasattr(self.t, "set_baud"): raise
            self._note(event=f"no reply at {FAST_BAUD}; trying the power-up speed {BOOT_BAUD}")
            self.t.set_baud(BOOT_BAUD)
            if self.wake() != b"\x01\x00":
                raise EcuError(f"unexpected wake reply at {BOOT_BAUD}; not switching speed")
            self._switch_speed()
            self.t.set_baud(FAST_BAUD)
            self.wake()
        self._gate("C")
        c = self._xfer(b"C", dict(cmd="C"), 1 + 43 + 2)
        build = struct.unpack_from("<H", c, len(c) - 2)[0]
        if build not in SUPPORTED_BUILDS:
            raise Refused(f"firmware build {build} is not a mapped, supported version")
        self.build = build
        self.channels = R.channels(build); self.bits = R.bit_channels(build)
        return c

    def read(self, addr, count, timeout=1.0):
        self._gate("D")
        if addr < 0 or count <= 0 or addr + count > READ_LIMIT: raise Refused("read outside the observed address space")
        return self._xfer(P.frame_read(addr, count), dict(cmd="D", addr=addr, count=count), 1 + count + 2, timeout)

    def runtime(self):
        return self.read(*RUNTIME, timeout=0.5)

    def live(self, dataid, block=None):
        """One decoded runtime value (reads the block unless one is given)."""
        if self.channels is None: raise Refused("connect first (firmware check)")
        b = block if block is not None else self.runtime()
        if dataid in self.bits: return R.bit_value(b, self.bits[dataid])      # a switch state (0/1)
        return R.value(b, self.channels[dataid])

    # -- arming -------------------------------------------------------------------------------------------------
    def arm(self, prompt=print, approved_by=None):
        """With approval="chat": the owner's approval in the chat arms the session; `approved_by`
        must quote it and is traced. No physical handshake. Otherwise the pedal or switch handshake below.

        Pedal handshake: with the engine stopped (phase 4), a person at the car presses the accelerator fully and
        releases it, seen in the live pedal channel (`arm_channel`, a runtime DataId). Allows writes for ARM['armed_s']. Nothing is written here."""
        if self.channels is None: raise Refused("connect first (firmware check)")
        if self.approval == "chat":
            if not (isinstance(approved_by, str) and approved_by.strip()):
                raise Refused("chat approval: quote the owner's approval (approved_by)")
            self._interlocks()
            self.armed_until = self.clock() + ARM["armed_s"]
            self._note(event="armed_by_chat", approved_by=approved_by.strip()[:200])
            prompt(f"Armed by chat approval for {ARM['armed_s'] / 60:.0f} min."); return True
        if self.arm_channel is None: raise Refused("no arming channel set (the accelerator pedal's runtime DataId)")
        ch = self.arm_channel
        self._interlocks()
        switch = ch in self.bits
        rest, press = self.live(ch), ARM_PRESS.get(self.phase, ARM["press_tp"])
        if switch:   # the approval is the switch going from on to off inside the window (on first, if it starts off)
            prompt(f"At the car: switch {self.bits[ch][3]} {'off' if rest else 'on, then off'} (within {ARM['handshake_s']:.0f} s).")
        else:
            if rest >= press: raise Refused(f"pedal reads {rest} % before the handshake; release the pedal first")
            how = "fully" if press >= 50 else f"to at least {press:.0f} % (a small blip)"
            prompt(f"At the car: press the accelerator pedal {how}, then release it (within {ARM['handshake_s']:.0f} s).")
        self._note(event="arm_start", channel=ch, rest=rest, press=None if switch else press, switch=switch)
        t0, pressed = self.clock(), bool(switch and rest)
        while self.clock() - t0 < ARM["handshake_s"]:
            tp = self.live(ch); self._note(event="arm", value=tp)
            if not pressed and (tp if switch else tp >= press): pressed = True; prompt(f"Changed ({tp}). Now {'off' if switch else 'release'}.")
            elif pressed and ((not tp) if switch else (tp <= rest + ARM["release_margin"])):
                self._interlocks()
                self.armed_until = self.clock() + ARM["armed_s"]
                prompt(f"Armed for {ARM['armed_s'] / 60:.0f} min."); return True
            self.sleep(0.05)
        raise Refused("arming handshake not completed; not armed")

    def disarm(self): self.armed_until = None

    def _interlocks(self):
        # No engine-state check: the firmware's P handler has none (docs/PROTOCOL.md; only F refuses while
        # running), and writes behave the same engine on or off.
        if self.phase in (6, 7) and not self.stationary:
            raise Refused("engine-running writes need the vehicle declared stationary (dyno or parked); SAFETY.md rule 8")

    def _armed(self):
        if self.armed_until is None or self.clock() > self.armed_until:
            self.armed_until = None; raise Refused("not armed: arm() first (chat approval or the handshake at the car)")

    # -- write side ---------------------------------------------------------------------------------------------
    def write(self, addr, data):
        """Write bytes inside the tune window, at known DataId addresses only. Journals the old bytes first."""
        self._gate("P")
        if self.build is None: raise Refused("connect first (firmware check)")
        data = bytes(data)
        if not data or addr < 0 or addr + len(data) > TUNE_END: raise Refused("write outside the tune window")
        if len(data) > 2: raise Refused("only 1- and 2-byte writes have been observed")
        st = next((x for x in self.settings if x["start"] <= addr and addr + len(data) <= x["end"]), None)
        if st is None: raise Refused("address is not inside a known, writable setting")
        if (addr - st["start"]) % st["width"] or len(data) != st["width"]:
            raise Refused(f"{st['name']}: a write must be one whole {st['width']}-byte cell")
        if st["scale"] not in WRITE_SCALES.get(self.phase, ()):
            raise Refused(f"{st['name']}: scale '{st['scale']}' is not writable in phase {self.phase}")
        self._armed(); self._interlocks()
        old = self.read(addr, len(data))
        sg = st["signed"]
        step = abs(int.from_bytes(data, "little", signed=sg) - int.from_bytes(old, "little", signed=sg))
        if step > self.max_step: raise Refused(f"{st['name']}: change of {step} raw counts exceeds the limit of {self.max_step}")
        self._log(addr, old, data)
        try: self._xfer(P.frame_write(addr, data), dict(cmd="P", addr=addr, count=len(data), data=data), 1 + len(data))
        except EcuError as e:                       # never retried: find out what the ECU now holds
            now = self.read(addr, len(data))
            state = "written" if now == data else "unchanged" if now == old else f"other ({now.hex()})"
            raise EcuError(f"write reply failed ({e}); the cell is {state}; journal holds the old value") from None
        back = self.read(addr, len(data))
        if back != data: raise EcuError(f"read-back {back.hex()} differs from written {data.hex()}; revert advised")
        return old

    MAX_FRAME = 32   # largest P observed from PCLink (one table row of 16 two-byte cells, 5 blocks)

    def _send_write(self, addr, data):
        """One P frame. 1-2 bytes: a single block answered '$' + echo (as before). 4-32 bytes: a blocked frame, each
        8-byte block followed by its ck16 and answered '$' (as captured from PCLink). 3 bytes is refused: it is one block
        and its reply form was never observed. Never retried."""
        n = len(data)
        if n <= 2: return self._xfer(P.frame_write(addr, data), dict(cmd="P", addr=addr, count=n, data=data), 1 + n)
        if n == 3 or n > self.MAX_FRAME: raise Refused(f"a {n}-byte write frame is not supported (1-2 or 4-{self.MAX_FRAME})")
        blocks = P.frame_write_blocks(addr, data)
        for k, blk in enumerate(blocks):
            self._note(tx=blk.hex(), block=f"{k + 1}/{len(blocks)}")
            self.t.write(blk)
            first = self.t.read(1, 0.5)
            if first == b"$": continue
            if first == b"?":
                rest = self.t.read(2, 0.5); code = struct.unpack("<H", rest)[0] if len(rest) == 2 else None
                err = P.ERRORS.get(code, f"error {code}")
            else: err = "no reply" if not first else f"unexpected reply byte 0x{first[0]:02x}"
            self.errors.append((round(time.time(), 3), "P", f"{err} after block {k + 1} of {len(blocks)}", blk.hex()))
            self._note(error=err, block=f"{k + 1}/{len(blocks)}")
            if hasattr(self.t, "drain"): self.t.drain()
            raise EcuError(f"{err} after block {k + 1} of {len(blocks)}")
        self._note(rx="$ per block", blocks=len(blocks))

    def write_range(self, addr, data, step_limit=True):
        """Write whole cells of ONE known, writable setting (several cells at once), as PCLink sends table edits: frames of
        up to 32 bytes, blocked. Same rules as write(): tune window, setting scale, armed, journal first (the whole old
        range), read-back of the whole range. `step_limit`: each cell may move at most max_step raw counts (False only
        for a restore to backed-up values)."""
        self._gate("P")
        if self.build is None: raise Refused("connect first (firmware check)")
        data = bytes(data)
        if not data or addr < 0 or addr + len(data) > TUNE_END: raise Refused("write outside the tune window")
        st = next((x for x in self.settings if x["start"] <= addr and addr + len(data) <= x["end"]), None)
        if st is None: raise Refused("range is not inside one known, writable setting")
        w = st["width"]
        if (addr - st["start"]) % w or len(data) % w: raise Refused(f"{st['name']}: writes must be whole {w}-byte cells")
        if st["scale"] not in WRITE_SCALES.get(self.phase, ()):
            raise Refused(f"{st['name']}: scale '{st['scale']}' is not writable in phase {self.phase}")
        self._armed(); self._interlocks()
        old = self.read(addr, len(data), timeout=2.0)
        if old == data: return old
        if step_limit:
            sg = st["signed"]
            for i in range(0, len(data), w):
                d = abs(int.from_bytes(data[i:i + w], "little", signed=sg) - int.from_bytes(old[i:i + w], "little", signed=sg))
                if d > self.max_step: raise Refused(f"{st['name']}: change of {d} raw counts exceeds the limit of {self.max_step}")
        self._log(addr, old, data)
        frame = self.MAX_FRAME - self.MAX_FRAME % w
        try:
            for i in range(0, len(data), frame):
                chunk = data[i:i + frame]
                if len(chunk) == 3: chunk = data[i:i + 2]   # (1-byte cells only) never send a 3-byte frame
                self._send_write(addr + i, chunk)
                if len(chunk) < len(data[i:i + frame]): self._send_write(addr + i + 2, data[i + 2:i + 3])
        except EcuError as e:
            now = self.read(addr, len(data), timeout=2.0)
            state = "written" if now == data else "unchanged" if now == old else "partly written"
            raise EcuError(f"write failed ({e}); the range is {state}; journal holds the old values") from None
        back = self.read(addr, len(data), timeout=2.0)
        if back != data: raise EcuError(f"read-back of 0x{addr:04X}+{len(data)} differs from what was written; revert advised")
        return old

    def field(self, dataid):
        """(definition, current value) of an enumerated setting in FIELD_WRITES. Read only."""
        if self.build is None: raise Refused("connect first (firmware check)")
        f = FIELD_WRITES.get(self.build, {}).get(dataid)
        if f is None: raise Refused(f"DataId {dataid} is not a writable field for build {self.build}")
        byte = self.read(f["addr"], 1)[0]
        return f, (byte >> f["bit"]) & ((1 << f["bits"]) - 1)

    def write_field(self, dataid, value):
        """Set an enumerated setting (a bit field in one tune byte) to one of its listed values. The whole byte is read,
        only the field's bits are changed, the old byte is journalled first, then echo and read-back as for write()."""
        self._gate("P")
        f, cur = self.field(dataid)
        if not f["confirmed"]: raise Refused(f"{f['name']}: not yet confirmed by a PCLink capture; locked")
        if value not in f["values"]: raise Refused(f"{f['name']}: {value} is not one of {f['values']}")
        if self.phase not in WRITE_SCALES: raise Refused(f"no writes in phase {self.phase}")
        self._armed(); self._interlocks()
        old = self.read(f["addr"], 1)
        mask = ((1 << f["bits"]) - 1) << f["bit"]
        new = bytes([(old[0] & ~mask & 0xFF) | (value << f["bit"])])
        if new == old: return cur
        a = f["addr"]
        self._log(a, old, new)
        try: self._xfer(P.frame_write(a, new), dict(cmd="P", addr=a, count=1, data=new), 2)
        except EcuError as e:
            now = self.read(a, 1)
            state = "written" if now == new else "unchanged" if now == old else f"other ({now.hex()})"
            raise EcuError(f"write reply failed ({e}); {f['name']} is {state}; journal holds the old value") from None
        if self.read(a, 1) != new: raise EcuError(f"{f['name']}: read-back differs; revert advised")
        return cur

    def revert(self, auto=False):
        """Write every journalled old value back, newest first. `auto=True` (an abort or the end of a session) skips the
        arming check: it writes only values the ECU held before this tool changed them."""
        done = 0
        if self.journal and not auto: self._armed()
        for addr, old, new in reversed(self.journal):
            self._gate("P")
            frame = self.MAX_FRAME
            for i in range(0, len(old), frame):            # multi-byte entries (write_range) go back in blocked frames
                chunk = old[i:i + frame]
                if len(chunk) == 3: self._send_write(addr + i, chunk[:2]); self._send_write(addr + i + 2, chunk[2:])
                else: self._send_write(addr + i, chunk)
            if self.read(addr, len(old), timeout=2.0) != old: raise EcuError(f"revert of 0x{addr:04X} did not read back")
            done += 1
        self.journal.clear(); self._save()
        return done

    def store(self):
        """Persist RAM to permanent memory (Q 000B 0000 FFFF). Phase 7 only."""
        self._gate(("Q", 0x000B)); self._armed()
        return self._xfer(P.frame_q(0x000B, 0x0000, 0xFFFF), dict(cmd="Q"), 1, timeout=3.0)

    def load_journal(self, path):
        """Continue from a journal written by an earlier session (for revert after a reconnect)."""
        self.journal_path = Path(path)
        self.journal = [(int(e["addr"], 16), bytes.fromhex(e["old"]), bytes.fromhex(e["new"]))
                        for e in json.loads(self.journal_path.read_text())]
        return len(self.journal)

    def _log(self, addr, old, new):
        self.journal.append((addr, old, new)); self._save()

    def _save(self):
        if self.journal_path:
            self.journal_path.write_text(json.dumps([dict(addr=f"0x{a:04X}", old=o.hex(), new=n.hex()) for a, o, n in self.journal], indent=1))
