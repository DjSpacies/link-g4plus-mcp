# Link G4+ serial protocol

What is known about the protocol PCLink uses to talk to a Link G4+ ECU, from USB captures of PCLink and from the
firmware's command dispatcher. Everything here was observed on a G4+ Thunder with firmware 5.6.8.3671 unless noted.
Facts not yet known are listed at the end.

## Link
- The ECU appears as a USB serial device, "Link Engine Management USB ECU", USB ID 12B8:EC60.
- 625000 baud, 8N1, no flow control, DTR and RTS off. PCLink configures the port once and purges it before each
  request.
- **After power-up the ECU's link runs at 115200 baud.** PCLink tries `w` at 625000; with no reply it reopens the
  port at 115200, sends `w` (reply `$ 01 00`) and `Q 0003 0006 0000` (reply `$`), then reopens at 625000 and
  continues. The ECU stays at 625000 until power-off.

## Frames
    PC  -> ECU   'w' | 'C'                                   single byte, no checksum
                 'D' addr16 count16 ck16                     read
                 'P' addr16 count16 data[count] ck16         write
                 'Q' word word word ck16                     request family; first word is a sub-code
    ECU -> PC    '$' [data] [ck16]  or  '?' err16

- Little endian. `ck16 = (-sum(bytes)) & 0xFFFF`. For requests the sum covers every byte before the checksum; for
  `D` and `C` replies it covers the data only, not the `$`. `P` replies echo the data with no checksum.
- Error codes: 2 bad checksum, 3 bad length or parameter, 4 refused while the engine runs (only the firmware-control
  command checks this), 0x0B unknown command, 0x10 address out of range.

## Commands
| Cmd | Use | Notes |
|---|---|---|
| `w` | wake / probe | reply `$ 01 00` |
| `C` | status | reply `$` + 43 bytes + ck16; lists the regions PCLink reads at connect; ends with the firmware build (u16) |
| `D` | read memory | any address up to 0x665B |
| `P` | write memory | tune window only; 1-2 bytes: one block, reply `$` + echo; longer (PCLink sends up to 32 bytes, one table row): blocked, below |
| `Q 0003 0006 0000` | link speed to 625000 | sent at connect |
| `Q 000A 0000 0000` | MAP sensor calibration | the ECU calibrates itself and keeps the result without a Store |
| `Q 000B 0000 FFFF` | Store (RAM to permanent memory) | reply after about 1 s |
| `F`, `s`, `T`, `A`, `W`, `E` | firmware control, serial number, test, restricted mode, identify, config | never sent by this tool |

## Address space (PCLink addresses)
| Range | Contents |
|---|---|
| 0x0000-0x4CB5 | tune image, writable with `P` (changes are RAM only until Store) |
| 0x4C3E-0x4CB5 | inside the tune window: statistics counters, identity, MAP/BAP calibration (updated by the ECU) |
| 0x4CB6-0x4F7F | runtime values; PCLink polls `D 0x4CB6/714` about every 18 ms |
| 0x4F80-0x665B | a third region read at connect |

At connect PCLink reads 0x4C3E/120, 0x4F80/5852, 0x0000/0x3FC6, 0x3FC6/3192 and 0x4E22/2 (power-on counter), then
polls the runtime block. The image it downloads equals the tune in a saved .pclr file, byte for byte through the
address map.

Every DataId (setting, table or runtime value) has a PCLink address, type, size and scale in
`maps/address-map-<build>.tsv`. The map's scale is incomplete for some settings (for example, general-purpose tables
store raw 100 for a displayed 0, scale x0.5 -50); `g4plus/writable.py` checks each setting against a .pclr from the
same ECU before allowing writes.

## Behaviour
- **Live edits are RAM only.** A `P` write takes effect at once and is lost at the next power cycle unless Stored.
  PCLink's own undo is just another `P` with the old value; the ECU has no undo command.
- **Bit fields** (on/off and option settings) share a byte with other settings; a change writes the whole byte.
  Example: Lambda Mode is bits 2-3 of byte 0x0002 (0 OFF, 1 Stoich narrow band, 2 Stoich wide band, 3 Auto wide band).
- **Blocked writes.** A `P` frame longer than 8 bytes is sent in 8-byte blocks (`'P' addr16 count16 data...` split
  every 8 bytes), each followed by the ck16 of that block. The ECU answers `$` after every block and does not echo
  the data. PCLink edits a table one row per frame (used columns only). 3-byte writes have not been observed.
- **PCLink's edit counter.** PCLink increments tune byte 0x0037 on each edit and re-reads 0x0000/512 afterwards. The
  ECU does not need it; this tool leaves it alone.
- **Disconnect** has no command: PCLink stops polling and closes the port.

## Serial settings that matter
With pyserial on Windows: open the port, then clear DTR and RTS (setting them off before opening sent nothing on one
laptop); write each request one byte per USB transfer, as PCLink does; keep the read timeout fixed (pyserial
reconfigures the whole port whenever the timeout changes, which corrupted replies). With these settings 1,505
runtime reads ran with no link error; without them about one read in two seconds failed.

## Not yet known
- The full `C` payload layout beyond the region list and the build number; the meaning of the `w` reply.
- Blocked writes of 4-31 bytes on a real ECU (32 bytes is tested); writes across non-table settings.
- Store's parameters (`0000 FFFF`: whole image?).
- Other `Q 0003` speed codes; the units of MAP calibration.
