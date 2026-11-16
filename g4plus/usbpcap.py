"""Read USBPcap captures (pcap link type 249) into the ECU's serial byte streams. Standard library only.

A capture of PCLink and a G4+ holds USB transfers to the ECU's USB-serial bridge (CP210x-style). Bulk OUT data is
what PCLink sent; bulk IN completions are what the ECU answered. Control transfers are the bridge's own settings
(baud, purge, modem lines) and are reported separately. Read-only: this never touches a device.
"""
import struct
from dataclasses import dataclass

LINKTYPE_USBPCAP = 249
CP210X_REQUESTS = {0x00: "IFC_ENABLE", 0x07: "SET_MHS", 0x08: "GET_MDMSTS", 0x10: "GET_COMM_STATUS", 0x12: "PURGE",
                   0x1E: "SET_BAUDRATE", 0x03: "SET_LINE_CTL", 0x13: "SET_FLOW", 0x19: "SET_CHARS"}

@dataclass
class Chunk:
    t: float          # seconds since the first packet
    direction: str    # "pc" (PC -> ECU) or "ecu" (ECU -> PC)
    data: bytes

@dataclass
class Control:
    t: float
    request: int
    name: str
    value: int
    data: bytes

def read(path, device=None):
    """Return (chunks, controls). `device` limits to one USB device address (None: the busiest bulk device)."""
    raw = open(path, "rb").read()
    magic = struct.unpack_from("<I", raw)[0]
    end = "<" if magic in (0xA1B2C3D4, 0xA1B23C4D) else ">"
    nano = magic in (0xA1B23C4D, 0x4D3CB2A1)
    linktype = struct.unpack_from(end + "I", raw, 20)[0]
    if linktype != LINKTYPE_USBPCAP: raise ValueError(f"not a USBPcap capture (link type {linktype})")
    pos, t0, pkts = 24, None, []
    while pos + 16 <= len(raw):
        sec, frac, incl, _ = struct.unpack_from(end + "IIII", raw, pos); pos += 16
        body = raw[pos:pos + incl]; pos += incl
        t = sec + frac / (1e9 if nano else 1e6)
        if t0 is None: t0 = t
        hlen, irp, status, func, info, bus, dev, ep, xfer, dlen = struct.unpack_from("<HQIHBHHBBI", body)
        pkts.append((t - t0, info & 1, dev, ep, xfer, body[hlen:hlen + dlen] if xfer != 2 else body, hlen, status))
    if device is None:
        counts = {}
        for p in pkts:
            if p[4] == 3 and p[5]: counts[p[2]] = counts.get(p[2], 0) + 1
        device = max(counts, key=counts.get) if counts else None
    chunks, controls = [], []
    for t, from_dev, dev, ep, xfer, data, hlen, status in pkts:
        if dev != device: continue
        if xfer == 3:                                            # bulk
            if ep & 0x80 and from_dev and data: chunks.append(Chunk(t, "ecu", data))
            elif not ep & 0x80 and not from_dev and data: chunks.append(Chunk(t, "pc", data))
        elif xfer == 2 and not from_dev:                         # control request: stage byte, then the 8-byte setup
            stage = data[hlen - 1] if hlen >= 28 else None
            if stage == 0 and len(data) >= hlen + 8:
                bmrt, breq, wval, widx, wlen = struct.unpack_from("<BBHHH", data, hlen)
                if bmrt & 0x60 == 0x40:                          # vendor request
                    extra = data[hlen + 8:hlen + 8 + wlen] if not bmrt & 0x80 else b""
                    controls.append(Control(t, breq, CP210X_REQUESTS.get(breq, f"0x{breq:02x}"), wval, extra))
    return chunks, controls

def streams(chunks):
    """Merge consecutive chunks of the same direction into runs: [(t_start, direction, bytes)]."""
    out = []
    for c in chunks:
        if out and out[-1][1] == c.direction: out[-1] = (out[-1][0], c.direction, out[-1][2] + c.data)
        else: out.append((c.t, c.direction, c.data))
    return out
