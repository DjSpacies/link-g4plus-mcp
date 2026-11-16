# Setup

## Requirements
- A Link G4+ ECU with a supported firmware build (`maps/`: 5.6.8.3671; 5.6.7.3631 mapped but untested).
- Windows 10/11 (or Linux with USB passthrough) with the ECU's USB driver (installed with PCLink).
- Python 3.10 or later and `pip install pyserial`. Nothing else.
- PCLink G4+ installed: it is the recovery tool, and its executable is read (never modified) to decode your .pclr
  when building the writable-settings list.

## 1. Read only
Close PCLink, key on.

    python -I g4plus\check_connection.py auto <your-ecu-image.bin>     (optional: compare with a saved image)
    python -I g4plus\server.py record --seconds 10 --out first.live.json

The port is detected automatically (USB 12B8:EC60); pass `COM6` etc. to override.

## 2. Writable settings
Built once per ECU and tune, from a byte-exact image and the .pclr saved from the same ECU:

    python -I g4plus\image.py <capture.pcap> my-ecu-image      (from a USBPcap capture of PCLink connecting), or
    an image saved by the tool's backup()
    python -I g4plus\writable.py my-ecu-image.bin my-tune.pclr --exe "C:\Link G4\PCLink.exe" --out writable.json

Keep `writable.json` with your car's files. Rebuild it whenever the tune changes in PCLink.

## 3. Setup profile
A JSON file with your fuel and limits. There are no defaults; write only numbers you have a reason for:

    {
      "fuel": "98 RON petrol",
      "wideband_valid": true,
      "limits": {
        "fuel_change_rich_max_pct": 5,
        "fuel_change_lean_max_pct": 3,
        "lean_abort_lambda_margin": 0.08,
        "oil_pressure_min_kpa": 100,
        "coolant_warning_c": 100
      }
    }

## 4. Register the MCP server
Read only:

    claude mcp add link-g4plus -- python -I <path>\g4plus\mcp_server.py --phase 5 --log-dir <logs>

Write mode:

    claude mcp add link-g4plus -- python -I <path>\g4plus\mcp_server.py --phase 6 --stationary
        --settings <car>\writable.json --profile <car>\profile.json --backup-dir <car>\backups
        --journal <car>\journal.json --log-dir <car>\logs --trace <car>\trace.jsonl

Restart the client so the tools load. `--trace` records every frame sent and received (JSONL). If a session ends
uncleanly, the journal holds the old values: run `revert`, or power cycle the ECU.
