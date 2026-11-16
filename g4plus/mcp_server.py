#!/usr/bin/env python3
"""MCP server (stdio) for the Link G4+ live tool: exposes the operations of server.LiveTool (phase 5, read only) or
tuning.TuningTool (phase 6, bounded Fuel Table 1 writes) as MCP tools, so an MCP client (Claude Code, etc.) can use
them directly. Standard library only: newline-delimited JSON-RPC 2.0 with initialize, tools/list, tools/call, ping.

  python -I mcp_server.py [PORT|auto] [--phase 5|6] [server.py options...]

PORT defaults to auto: the ECU is found by its USB ID (12B8:EC60). Connecting is refused while PCLink is running.

  Claude Code:  claude mcp add link-g4plus -- python -I <path>/mcp_server.py COM6 --phase 6 --stationary
                    --settings ... --profile ... --backup-dir ... --journal ... --log-dir ... --trace ...

The ECU is connected on the first tool call (not at startup), so the client can start the server before the ECU is
plugged in; a failed connect is returned as a tool error and retried on the next call. Every rule of the underlying
tool applies unchanged: phase-gated commands, chat approval quoted in arm(), single-use tokens, backup first, step
limits, watchdog auto-revert, revert at session end, no Store. Nothing is written to stdout except protocol messages.
"""
import argparse, json, sys, threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import server as V   # noqa: E402
T_OPS = V.OPS + ["list_tables", "get_table", "backup", "arm", "propose_write", "apply_write", "closed_loop_status",
                 "closed_loop", "restore", "revert", "end_session"]   # tuning.TuningTool.OPS (not imported here: it needs the profile tools)

PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]

def _obj(props=None, required=()):
    return dict(type="object", properties=props or {}, required=list(required), additionalProperties=False)

STR, NUM, BOOL = dict(type="string"), dict(type="number"), dict(type="boolean")
TOOLS = {
    "capabilities": ("ECU family, firmware, supported operations, declared limits and any unmet write gates.", _obj()),
    "connection_status": ("Connected, engine running, logging, link errors, armed state, changes in place.", _obj()),
    "list_channels": ("Live channels: canonical name, unit, native name, DataId, whether verified against PCLink.", _obj()),
    "read_channels": ("Read live values now.", _obj(dict(names=dict(type="array", items=STR), rate=NUM), ["names"])),
    "fuel_context": ("Snapshot of closed-loop status and trims, Fuel Table 1/2 values and blend, ethanol and the "
                     "background fuel corrections (address-map scale, indicative).", _obj()),
    "start_log": ("Start logging channels at a rate (Hz).", _obj(dict(channels=dict(type="array", items=STR), rate=NUM), ["channels"])),
    "stop_log": ("Stop the running log and save it (.live.json, readable by the analyses).", _obj()),
    "get_log": ("A saved log by id.", _obj(dict(id=STR), ["id"])),
    "list_tables": ("Tables the tool can read, with role, shape, scale class and whether writable.", _obj()),
    "get_table": ("Read a table live from the ECU (values and axes).", _obj(dict(id=STR), ["id"])),
    "backup": ("Read the whole tune window from the ECU and save a verified backup. Required before any write.",
               _obj(dict(label=STR), ["label"])),
    "arm": ("Issue a single-use approval token. approved_by must quote the owner's approval message from the chat "
            "verbatim; never call this without the owner's explicit approval of the specific change.",
            _obj(dict(approved_by=STR), ["approved_by"])),
    "propose_write": ("Dry run: validate changes [{table, row, col, value}] against the step limits and return a diff. "
                      "No effect on the ECU.", _obj(dict(changes=dict(type="array", items=_obj(
                          dict(table=STR, row=dict(type="integer"), col=dict(type="integer"), value=NUM),
                          ["table", "row", "col", "value"]))), ["changes"])),
    "apply_write": ("Apply a proposal with an approval token from arm(). Changes are RAM only and journalled; the "
                    "watchdog reverts on knock, lean, low oil pressure, coolant or link loss.",
                    _obj(dict(proposal_id=STR, approval_token=STR), ["proposal_id", "approval_token"])),
    "closed_loop_status": ("Lambda Mode as the ECU holds it now (closed-loop lambda on or off), read only.", _obj()),
    "closed_loop": ("Turn closed-loop lambda off (enabled=false) or back on (enabled=true, restores the mode it had). "
                    "RAM only, journalled; revert/end_session restore it. Needs a backup and a token from arm().",
                    _obj(dict(enabled=BOOL, approval_token=STR), ["enabled", "approval_token"])),
    "restore": ("Write tables (default: the writable ones) back to a backup's values: a backup id from this session or an "
                "earlier backup in the backup folder. Takes a pre-restore backup first, writes only differing cells in "
                "row frames, reads back. Needs a token from arm().",
                _obj(dict(backup_id=STR, approval_token=STR, tables=dict(type="array", items=STR)), ["backup_id", "approval_token"])),
    "revert": ("Write every change of this session back.", _obj()),
    "end_session": ("Stop the watchdog and revert everything unless keep is true (then changes stay in RAM until "
                    "power-off; nothing is Stored).", _obj(dict(keep=BOOL))),
}

class McpServer:
    def __init__(self, make_tool, out=sys.stdout):
        self.make_tool, self.tool, self.out, self.lock = make_tool, None, out, threading.Lock()
        self.ops = None   # known after the first connect; before that, all tools of the configured phase are listed

    def _ensure(self):
        if self.tool is None:
            self.tool = self.make_tool()
            self.ops = list(getattr(self.tool, "OPS", V.OPS))
        return self.tool

    def tool_list(self, phase_ops):
        return [dict(name=n, description=TOOLS[n][0], inputSchema=TOOLS[n][1]) for n in phase_ops if n in TOOLS]

    def call(self, name, args):
        if name not in TOOLS: return dict(content=[dict(type="text", text=f"unknown tool {name}")], isError=True)
        try: tool = self._ensure()
        except SystemExit as e: return dict(content=[dict(type="text", text=f"cannot open the tool: {e}")], isError=True)
        except Exception as e:   # noqa: BLE001 - a failed connect is reported, and retried on the next call
            return dict(content=[dict(type="text", text=f"connect failed: {type(e).__name__}: {e}")], isError=True)
        if name not in self.ops:
            return dict(content=[dict(type="text", text=f"{name} is not available in this phase")], isError=True)
        with self.lock: r = V.handle(tool, dict(jsonrpc="2.0", id=0, method=name, params=args or {}))
        if "error" in r:
            e = r["error"]; reason = (e.get("data") or {}).get("reason")
            return dict(content=[dict(type="text", text=json.dumps(dict(error=e["message"], reason=reason)))], isError=True)
        return dict(content=[dict(type="text", text=json.dumps(r["result"], default=str))], isError=False)

    def handle(self, msg, phase_ops):
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None: return None                                   # notifications (initialized, cancelled)
        if method == "initialize":
            v = params.get("protocolVersion")
            res = dict(protocolVersion=v if v in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                       capabilities=dict(tools=dict(listChanged=False)),
                       serverInfo=dict(name="link-g4plus-live", version="0.1"),
                       instructions="Live Link G4+ ECU tool. Writes are RAM only. Before arm(), get the owner's explicit "
                                    "approval in the chat and quote it verbatim in approved_by.")
        elif method == "ping": res = {}
        elif method == "tools/list": res = dict(tools=self.tool_list(self.ops or phase_ops))
        elif method == "tools/call": res = self.call(params.get("name"), params.get("arguments"))
        else: return dict(jsonrpc="2.0", id=mid, error=dict(code=-32601, message=f"method not found: {method}"))
        return dict(jsonrpc="2.0", id=mid, result=res)

    def serve(self, inp, phase_ops):
        for line in inp:
            if not line.strip(): continue
            try: msg = json.loads(line)
            except json.JSONDecodeError:
                self._send(dict(jsonrpc="2.0", id=None, error=dict(code=-32700, message="parse error"))); continue
            r = self.handle(msg, phase_ops)
            if r is not None: self._send(r)
        if self.tool is not None: self.tool.close()

    def _send(self, obj): self.out.write(json.dumps(obj) + "\n"); self.out.flush()

PHASE_OPS = {5: V.OPS, 6: T_OPS}

def main(argv=None):
    ap = argparse.ArgumentParser(); ap.add_argument("port", nargs="?", default="auto"); ap.add_argument("--phase", type=int, choices=[5, 6], default=5)
    for k in ("log_dir", "trace", "settings", "profile", "backup_dir", "journal"): ap.add_argument("--" + k.replace("_", "-"))
    ap.add_argument("--stationary", action="store_true"); ap.add_argument("--approval", choices=["chat", "handshake"], default="chat")
    ap.add_argument("--arm-channel", type=int, default=None)
    a = ap.parse_args(argv)
    real_stdout = sys.stdout; sys.stdout = sys.stderr          # anything printed by the tools goes to stderr
    make = lambda: V.open_tool(a.port, a.log_dir, a.trace, a.phase, a.settings, a.profile, a.backup_dir, a.journal,   # noqa: E731
                               a.stationary, a.arm_channel, a.approval)
    McpServer(make, out=real_stdout).serve(sys.stdin, PHASE_OPS[a.phase])

if __name__ == "__main__":
    main()
