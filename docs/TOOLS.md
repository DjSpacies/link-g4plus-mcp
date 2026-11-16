# MCP tools

`g4plus/mcp_server.py` serves these over stdio (MCP, JSON-RPC 2.0). Every result is JSON in the tool's text
content; a refused or failed call is a tool error with `{error, reason}`. The same operations are available without
MCP through `g4plus/server.py serve` (one JSON-RPC object per line, method = tool name).

The ECU connects on the first tool call, so the server can be registered before the ECU is plugged in.

## Read mode (`--phase 5`) and write mode (`--phase 6`)
| Tool | Mode | Does |
|---|---|---|
| `capabilities` | both | ECU family, firmware, supported operations; in write mode the declared limits and any unmet gates |
| `connection_status` | both | connected, engine running, logging, link errors; in write mode armed state and changes in place |
| `list_channels` | both | canonical live channels: unit, native name, DataId, `verified` against PCLink |
| `read_channels(names)` | both | current values with units |
| `fuel_context` | both | closed-loop status and trims, Fuel Table 1/2 values and blend, ethanol, background fuel corrections |
| `start_log(channels, rate)` / `stop_log` / `get_log(id)` | both | log to `.live.json` |
| `list_tables` / `get_table(id)` | write | tables by role (`ve`, `ign_base`): values, axes, scale class, writable |
| `backup(label)` | write | read and save the whole tune image (verified); required before any write |
| `arm(approved_by)` | write | a single-use approval token; `approved_by` quotes the owner's approval verbatim |
| `propose_write(changes)` | write | dry run of `[{table, row, col, value}]` against the step limits; returns a diff |
| `apply_write(proposal_id, approval_token)` | write | apply a proposal; RAM only, journalled, read back |
| `closed_loop_status` | write | Lambda Mode as the ECU holds it |
| `closed_loop(enabled, approval_token)` | write | closed-loop lambda off, or back to the mode it had |
| `restore(backup_id, approval_token, tables?)` | write | write tables back to a backup's values (differing cells only, row frames); a backup of the current state is taken first |
| `revert` | write | write every change of this session back |
| `end_session(keep)` | write | stop the watchdog; revert everything unless `keep` |

## A write, step by step
1. `backup("before-idle-test")`
2. `get_table("ve")` and `read_channels([...])` to find the cell at the operating point
3. `propose_write([{table: "ve", row, col, value}])`: check the diff
4. The owner approves in the chat; `arm(approved_by="<their message>")`
5. `apply_write(proposal_id, token)`; read `fuel_context` and the channels
6. `end_session()` reverts, or `end_session(keep=true)` leaves the change in RAM until the next power cycle (to keep
   it permanently, Store from PCLink)

For a real fuelling change on a running engine, turn closed loop off first (`closed_loop(false, token)`), or the
closed-loop trim will cancel the change. `end_session` restores it with everything else.

## Error reasons
`unknown_channel`, `unknown_table`, `unknown_log`, `unknown_proposal`, `out_of_range`, `out_of_bounds`,
`not_approved`, `no_backup`, `not_logging`, `not_writable`, `safety_gate`, `write_failed`, `link`.
