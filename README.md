# link-g4plus-mcp

An MCP server and Python client for live tuning on **Link G4+** ECUs. It talks to the ECU directly over its USB
serial link, so an AI agent (or any MCP client) can read live data, read tables, and make bounded, journalled,
RAM-only fuel-table changes. Nothing on the ECU is ever stored permanently by this tool.

It is the live-ECU companion to the [ECU tuning skill](https://github.com/DjSpacies/ecu-tuning-skill): logs recorded here
load straight into the skill's analyses.

> **Status: experimental.** Developed and tested on one Link G4+ Thunder running firmware 5.6.8.3671. Other G4+
> models share the same firmware image and should work, but have not been tested. Use at your own risk; read
> [docs/SAFETY.md](docs/SAFETY.md) first. Not affiliated with or endorsed by Link Engine Management.

## What it does
- **Live data**: about 19 reads a second of the ECU's runtime block, decoded to canonical channels (rpm, MAP, lambda,
  temperatures, pressures, trims, ethanol…), with a flag for channels checked against PCLink's display.
- **Logging** to `.live.json`, readable by the ECU tuning skill.
- **Fuel context**: closed-loop status and trims, Fuel Table 1/2 values and blend, and the background fuel
  corrections, so a lambda change (or its absence) after a write can be read correctly.
- **Tables**: read any mapped table live; write Fuel Table 1 cells within your declared step limits.
- **Closed-loop lambda** off/on as a journalled change (confirmed against PCLink for 5.6.8.3671).
- **Backup** of the whole tune image before any write; **restore** of tables from a backup (multi-byte row writes,
  as PCLink sends them); **revert** of every change; a **watchdog** that reverts on
  knock, lean, low oil pressure, coolant temperature or link loss; revert at session end.
- **Connects like PCLink**: auto-detects the ECU's USB port, handles the boot-speed switch after power-up, refuses
  to start while PCLink is running.

## What it does not do
- No Store (permanent write), no firmware operations, no output tests, no serial-number or identity writes.
  Those commands are blocked in code, not by convention.
- PCLink and this tool cannot use the ECU at the same time (PCLink holds the port).

## Quick start
Windows 10/11 laptop connected to the ECU by USB, Python 3.10+:

    pip install pyserial
    git clone https://github.com/DjSpacies/link-g4plus-mcp

Read-only check (close PCLink first):

    python -I g4plus\server.py record --seconds 10 --out first.live.json

Register the MCP server in Claude Code, read only:

    claude mcp add link-g4plus -- python -I <path>\g4plus\mcp_server.py --phase 5 --log-dir <logs>

See [docs/SETUP.md](docs/SETUP.md) for the write mode (`--phase 6`), the setup profile and the writable-settings
list, and [docs/TOOLS.md](docs/TOOLS.md) for every tool.

## Documentation
- [docs/SETUP.md](docs/SETUP.md): installation, profile, writable settings, MCP registration
- [docs/TOOLS.md](docs/TOOLS.md): the MCP tools and what each returns
- [docs/SAFETY.md](docs/SAFETY.md): the rules enforced in code, and how to recover
- [docs/PROTOCOL.md](docs/PROTOCOL.md): the G4+ serial protocol as far as it is known

## Layout
| Path | |
|---|---|
| `g4plus/` | the client, live tool, tuning tool and MCP server (standard library plus pyserial) |
| `maps/` | DataId address maps per firmware build |
| `docs/` | documentation |

## Licence
MIT. See [LICENSE](LICENSE). PCLink and Link G4+ are products of Link Engine Management Ltd.
