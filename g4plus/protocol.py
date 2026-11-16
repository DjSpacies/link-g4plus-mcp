"""PCLink <-> Link G4+ serial protocol: frame building, checksums, and decoding of captured byte streams.

What is implemented is only what has been observed on the wire or read in the firmware's command dispatcher
(docs/PROTOCOL.md). Frames:
  PC  -> ECU   'w' | 'C'                                   single byte, no checksum
               'D' addr16 count16 ck16                     read
               'P' addr16 count16 data[count] ck16         write (frames over 8 bytes go in blocks, below)
               'Q' word word word ck16                     request family; first word is a sub-code
  ECU -> PC    '$' [data] [ck16]  or  '?' err16
ck16 = (-sum(bytes)) & 0xFFFF, little endian. For requests the sum covers every byte before the checksum; for D and C
replies it covers the data only, not the '$'. P replies echo the data with no checksum.

Blocked P (as PCLink sends a whole table row): the frame bytes 'P' addr16 count16 data[count] are sent in 8-byte
blocks, each followed by ck16 of that block; the last block may be shorter. The ECU answers '$' after every block and
does not echo the data. A frame of 8 bytes or less (count 1-3) is one block, the same as the plain frame above;
observed: count 1-2 answered '$' + echo, count 32 (5 blocks) answered '$' per block.
"""
import struct

ERRORS = {2: "bad checksum", 3: "bad length or parameter", 4: "refused while engine running",
          0x10: "address out of range", 0x0B: "unknown command"}
Q_SUBCODES = {0x0003: "connect step (meaning unknown)", 0x000B: "store to permanent memory"}

def ck16(b): return (-sum(b)) & 0xFFFF
def with_ck(b): return bytes(b) + struct.pack("<H", ck16(b))
def ck_ok(body, trailer): return struct.unpack("<H", trailer)[0] == ck16(body)

def frame_read(addr, count): return with_ck(b"D" + struct.pack("<HH", addr, count))
def frame_write(addr, data): return with_ck(b"P" + struct.pack("<HH", addr, len(data)) + bytes(data))
def frame_q(sub, a, b): return with_ck(b"Q" + struct.pack("<HHH", sub, a, b))

BLOCK = 8
def p_blocks(count): return -(-(5 + count) // BLOCK)                       # blocks a P frame of `count` data bytes needs
def frame_write_blocks(addr, data):
    """A P frame as it goes on the wire: 8-byte blocks, each with its own ck16. One block for 1-3 data bytes."""
    f = b"P" + struct.pack("<HH", addr, len(data)) + bytes(data)
    return [with_ck(f[i:i + BLOCK]) for i in range(0, len(f), BLOCK)]

def parse_request(buf, i=0):
    """One request at buf[i:]. Returns (dict, next_index) or (None, i) when incomplete."""
    if i >= len(buf): return None, i
    c = chr(buf[i])
    if c in "wC": return dict(cmd=c), i + 1
    if c == "D":
        if len(buf) - i < 7: return None, i
        addr, count = struct.unpack_from("<HH", buf, i + 1)
        return dict(cmd="D", addr=addr, count=count, ck_ok=ck_ok(buf[i:i + 5], buf[i + 5:i + 7])), i + 7
    if c == "Q":
        if len(buf) - i < 9: return None, i
        sub, a, b = struct.unpack_from("<HHH", buf, i + 1)
        return dict(cmd="Q", sub=sub, a=a, b=b, ck_ok=ck_ok(buf[i:i + 7], buf[i + 7:i + 9])), i + 9
    if c == "P":
        if len(buf) - i < 5: return None, i
        addr, count = struct.unpack_from("<HH", buf, i + 1)
        nb = p_blocks(count)
        n = 5 + count + 2 * nb
        if len(buf) - i < n: return None, i
        frame, ok, j = bytearray(), True, i
        for k in range(nb):
            size = min(BLOCK, 5 + count - k * BLOCK)
            ok = ok and ck_ok(buf[j:j + size], buf[j + size:j + size + 2]); frame += buf[j:j + size]; j += size + 2
        return dict(cmd="P", addr=addr, count=count, data=bytes(frame[5:]), ck_ok=ok, blocks=nb), i + n
    return dict(cmd="unknown", byte=buf[i]), i + 1

def reply_length(req, first):
    """Expected reply length for a request, given the reply's first byte; None if not known."""
    if first == ord("?"): return 3
    c = req["cmd"]
    if c == "w": return 3
    if c == "D": return 1 + req["count"] + 2
    if c == "P": return 1 if req.get("blocks", 1) > 1 else 1 + req["count"]   # blocked: the last block's '$'
    if c == "Q": return 1
    return None

def parse_reply(req, buf):
    """Decode a complete reply to `req`."""
    if not buf: return dict(ok=False, error="no reply")
    if buf[0] == ord("?"):
        code = struct.unpack_from("<H", buf, 1)[0] if len(buf) >= 3 else None
        return dict(ok=False, error=ERRORS.get(code, f"error 0x{code:04x}" if code is not None else "short error"), code=code)
    if buf[0] != ord("$"): return dict(ok=False, error=f"unexpected reply byte 0x{buf[0]:02x}")
    c = req["cmd"]
    if c in "DC":
        data, trailer = buf[1:-2], buf[-2:]
        return dict(ok=ck_ok(data, trailer), data=bytes(data), error=None if ck_ok(data, trailer) else "reply checksum")
    if c == "P" and req.get("blocks", 1) > 1: return dict(ok=True, data=b"")   # '$' per block, no echo
    if c == "P":
        echo = bytes(buf[1:])
        return dict(ok=echo == req["data"], data=echo, error=None if echo == req["data"] else "write echo differs")
    return dict(ok=True, data=bytes(buf[1:]))

def regions_from_c(payload):
    """The region list in the 'C' reply: (addr, count) pairs, as observed. Layout beyond that is not yet known."""
    words = struct.unpack_from(f"<{len(payload) // 2}H", payload)
    return words

def decode_session(runs):
    """Pair captured PC and ECU runs (usbpcap.streams) into [(t, request, reply)]. The ECU only answers, so each PC
    request is followed by its reply bytes; a run may hold several requests (rare) and replies may span runs."""
    out, pending, rx, tx, t_tx = [], [], b"", b"", None
    for t, direction, data in runs:
        if direction == "pc":
            if not tx: t_tx = t
            tx += data; i = 0                    # a blocked P spans several runs, with the ECU's '$' between its blocks
            while True:
                req, j = parse_request(tx, i)
                if req is None: break
                pending.append((t_tx, req)); i = j; t_tx = t
            tx = tx[i:]
        else:
            if not pending: rx = b""; continue                 # capture began mid-reply: nothing to pair it with
            rx += data
            while pending and rx and rx[0] not in (0x24, 0x3F): rx = rx[1:]   # resynchronise on '$' or '?'
            while pending:
                t0, req = pending[0]
                if req["cmd"] == "C":
                    if len(rx) < 1: break
                    n = 1 + 43 + 2 if rx[0] == ord("$") else 3
                else:
                    n = reply_length(req, rx[0])
                if n is None or len(rx) < n: break
                out.append((t0, req, parse_reply(req, rx[:n]))); rx = rx[n:]; pending.pop(0)
    return out
