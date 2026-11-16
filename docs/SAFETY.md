# Safety

This tool writes to a running engine's ECU. The rules below are enforced in code (`g4plus/client.py`,
`g4plus/tuning.py`); they are not instructions to the agent that it could ignore.

## Before you start
- Save your tune from PCLink (F2) to two places, and keep the PCLink installer for your firmware.
- Know the recovery steps below. A power cycle undoes every change this tool makes.
- Car parked or on a dyno. Writes are refused unless the session is declared stationary (`--stationary`).
- PCLink closed: the tool refuses to connect while it runs.

## Rules in code
- **Commands.** Each session mode allows a fixed set of commands; anything else is refused before it reaches the
  port. Never sent: firmware control (`F`), serial number (`s`), tests (`T`), restricted mode (`A`), identify (`W`),
  config (`E`), and every `Q` sub-code except the link-speed switch at connect. Store (`Q 000B`) is not enabled.
- **Firmware.** The tool refuses a firmware build it has no address map for.
- **Where writes can go.** Only inside the tune window, only one whole cell of a setting in the writable-settings
  list (`g4plus/writable.py`, built from your ECU's image and .pclr), never in the statistics, identity and
  calibration block. Running-engine writes only to settings whose scale was confirmed against your .pclr. Only
  Fuel Table 1 through the MCP tools, and enumerated settings listed and confirmed per build (Lambda Mode).
- **How far.** Each cell may change by at most your profile's `fuel_change_rich_max_pct` /
  `fuel_change_lean_max_pct`. No defaults: an undeclared limit refuses writes.
- **Approval.** Every write needs a single-use token from `arm()`, which quotes the owner's approval (chat mode) or
  needs a physical handshake seen in live data (handshake mode). Chat approval is recorded, not enforced
  physically: keep your MCP client's tool-approval prompts on.
- **Backup first.** A verified backup of the whole tune image in the same session.
- **Journal and read-back.** The old bytes are saved to disk before every write; every write is checked by its
  echo and read back; a failed write reply reports whether the cell holds the old or new value.
- **Watchdog.** While any change is in place: knock count rising, lambda leaner than target by more than
  `lean_abort_lambda_margin` (0.5 s), oil pressure below `oil_pressure_min_kpa` (0.5 s), coolant above
  `coolant_warning_c`, or 5 failed reads in a row -> every change is written back and the session refuses further
  writes.
- **End of session.** Changes are written back unless kept. Kept changes live in RAM until the next power cycle.

## Recovery
1. `revert` (or `end_session`): writes every journalled old value back. `restore(backup_id)` writes whole tables
   back to a backup taken earlier.
2. Power cycle the ECU (key off for 10 s): every unstored change is lost.
3. PCLink: connect, open your saved .pclr, send it to the ECU, Store.
4. PCLink's firmware updater: reinstall the same firmware (it keeps the tune).
5. Link Engine Management support.
