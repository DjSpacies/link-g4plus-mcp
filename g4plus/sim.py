"""A simulated Link G4+ for tests: answers w, C, D, P and Q like the frames observed from a real ECU.

It keeps a stored image and a RAM image. P changes RAM only; power_cycle() copies the stored image back over RAM
(unstored edits are lost, as the vendor documents); Q 000B stores RAM. Every request is recorded in `log`, so tests
can assert what a client did and did not send.
"""
import struct
import protocol as P

class SimEcu:
    def __init__(self, image, build=3671, engine_running=False, boot_baud=625000):
        """`boot_baud`: the link speed after power-up (a real ECU: 115200, until `Q 0003 0006 0000`). Tests that
        do not care default to 625000, as if PCLink had already connected since power-up."""
        self.boot_baud = self.link_baud = boot_baud
        self.stored = bytearray(image); self.ram = bytearray(image)
        self.build, self.engine_running, self.log = build, engine_running, []
        self.c_payload = bytes(41) + struct.pack("<H", build)
        self.on_read = None   # optional callable(ecu, addr, count) run before each D, e.g. to move live values

    def set_u16(self, addr, value): self.ram[addr:addr + 2] = struct.pack("<h", value)

    def power_cycle(self): self.ram = bytearray(self.stored); self.link_baud = self.boot_baud

    def handle(self, buf):
        if getattr(self, "blk", None) is not None:              # next block of a blocked P
            body, ck = bytes(buf[:-2]), bytes(buf[-2:])
            if len(buf) < 3 or not P.ck_ok(body, ck): self.blk = None; return b"?" + struct.pack("<H", 2)
            self.blk["frame"] += body
            if len(self.blk["frame"]) < self.blk["total"]: return b"$"
            b = self.blk; self.blk = None
            return self._apply_blocked(b)
        if buf[:1] == b"P" and len(buf) >= 7:
            a, k = struct.unpack_from("<HH", buf, 1)
            if 5 + k > P.BLOCK:                                 # first block of a blocked P: 8 bytes + ck16
                body, ck = bytes(buf[:-2]), bytes(buf[-2:])
                if len(body) != P.BLOCK or not P.ck_ok(body, ck): return b"?" + struct.pack("<H", 2)
                self.blk = dict(addr=a, count=k, total=5 + k, frame=bytearray(body)); return b"$"
        req, n = P.parse_request(buf)
        if req is None: return b"?" + struct.pack("<H", 3)
        self.log.append(req)
        c = req["cmd"]
        if c == "w": return b"$\x01\x00"
        if c == "C": return b"$" + self.c_payload + struct.pack("<H", P.ck16(self.c_payload))
        if not req.get("ck_ok", True): return b"?" + struct.pack("<H", 2)
        if c == "D":
            a, k = req["addr"], req["count"]
            if self.on_read: self.on_read(self, a, k)
            if a + k > len(self.ram): return b"?" + struct.pack("<H", 0x10)
            data = bytes(self.ram[a:a + k]); return b"$" + data + struct.pack("<H", P.ck16(data))
        if c == "P":
            a, d = req["addr"], req["data"]
            if a + len(d) > 0x4CB6: return b"?" + struct.pack("<H", 0x10)
            self.ram[a:a + len(d)] = d; return b"$" + d
        if c == "Q":
            if req["sub"] == 0x000B: self.stored = bytearray(self.ram)
            if (req["sub"], req["a"], req["b"]) == (0x0003, 0x0006, 0x0000): self.link_baud = 625000
            return b"$"
        return b"?" + struct.pack("<H", 0x0B)

    def _apply_blocked(self, b):
        d = bytes(b["frame"][5:5 + b["count"]])
        self.log.append(dict(cmd="P", addr=b["addr"], count=b["count"], data=d, blocks=P.p_blocks(b["count"])))
        if b["addr"] + b["count"] > 0x4CB6: return b"?" + struct.pack("<H", 0x10)
        self.ram[b["addr"]:b["addr"] + b["count"]] = d; return b"$"

class SimTransport:
    """`faults`: per-request actions, used in order: None (normal), "drop" (no reply), "truncate" (half the reply now,
    the rest arrives late: drained, or in front of the next reply if not drained)."""
    def __init__(self, ecu, faults=None, baud=625000):
        self.ecu, self.rx, self.late, self.faults, self.baud = ecu, b"", b"", list(faults or []), baud
    def set_baud(self, baud): self.baud = baud; self.rx = self.late = b""
    def write(self, b):
        if self.baud != self.ecu.link_baud: self.rx = b""; return   # wrong speed: the ECU sees noise, says nothing
        rep = self.ecu.handle(bytes(b)); f = self.faults.pop(0) if self.faults else None
        if f == "drop": rep = b""
        elif f == "truncate": k = len(rep) // 2; rep, late = rep[:k], rep[k:]; self.rx = self.late + rep; self.late = late; return
        self.rx = self.late + rep; self.late = b""
    def read(self, n, timeout):
        out, self.rx = self.rx[:n], self.rx[n:]; return out
    def drain(self):
        got = self.rx + self.late; self.rx = self.late = b""; return got
    def close(self): pass
