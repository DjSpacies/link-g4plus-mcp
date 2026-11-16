"""Live table reads and bounded fuel-cell writes with the engine running.

TuningTool extends server.LiveTool with the write side of the ECU tool interface (docs/TOOLS.md): list_tables, get_table, backup,
arm, propose_write, apply_write, revert, end_session. Every rule below is enforced in code (here or in client.py):

- Only tables whose role is in WRITE_ROLES ("ve": Fuel Table 1), and only cells whose scale is verified or fitted.
- Profile gates: fuel known, wideband declared valid, and the owner has declared the limits
  in REQUIRED_LIMITS (safety/limits.yaml has no default for them). Missing any -> every proposal is refused.
- Step limits (rule 3): each cell may change by at most fuel_change_rich_max_pct (more fuel) or
  fuel_change_lean_max_pct (less fuel) of its current value; at most MAX_CELLS cells per proposal.
- A backup of the whole tune window, taken in this session, before any write (rule 2).
- An approval token per apply, single use, issued only by arm(). Approval mode "chat" (default):
  arm(approved_by=...) quotes the owner's approval from the chat, and it is traced; no physical handshake. Mode
  "handshake": the pedal or switch handshake at the car (client.Session.arm). The vehicle must be declared stationary (rule 8; client.py refuses otherwise).
- A watchdog while any change is in place: knock count rising, lambda leaner than target by more than
  lean_abort_lambda_margin, oil pressure below oil_pressure_min_kpa, coolant above coolant_warning_c, or the link lost
  -> every journalled change is written back at once (rule 6) and the session refuses further writes.
- end_session() writes everything back unless keep=True. Nothing is ever Stored (phase 7).
"""
import hashlib, json, threading, time, uuid
from pathlib import Path

import client as C, server as V

import sys
from tables import TABLES

class Limits:
    """The owner's declared limits from the setup profile. There are no built-in defaults: a limit nobody declared
    is None, and the gates refuse writes that need it."""
    KNOWN = {"fuel_change_rich_max_pct", "fuel_change_lean_max_pct", "lean_abort_lambda_margin", "oil_pressure_min_kpa",
             "coolant_warning_c", "max_rpm", "max_boost_kpa"}
    def __init__(self, user=None):
        self.user = {k: float(v) for k, v in (user or {}).items() if k in self.KNOWN and v is not None}
    def get(self, key): return self.user.get(key)

WRITE_ROLES = {"ve"}
REQUIRED_LIMITS = ["fuel_change_rich_max_pct", "fuel_change_lean_max_pct", "lean_abort_lambda_margin", "oil_pressure_min_kpa"]
MAX_CELLS = 32
READ_REGIONS = [(0x0000, 0x3FC6), (0x3FC6, 3192), (0x4C3E, 120), (0x4F80, 5852)]   # as PCLink reads them
WATCH_S = 0.1
ABORT_PERSIST = 5   # lambda and oil-pressure aborts need this many samples in a row (about 0.5 s); knock and coolant act at once

def load_tables_yaml():
    return {role: dict(t, role=role) for role, t in TABLES.items()}

def trim_axis(ax):
    """Used zones of an axis: the leading strictly increasing part (unused zones repeat a filler value)."""
    out = [ax[0]]
    for v in ax[1:]:
        if v <= out[-1]: break
        out.append(v)
    return out

class TuningTool(V.LiveTool):
    OPS = V.OPS + ["list_tables", "get_table", "backup", "arm", "propose_write", "apply_write", "closed_loop_status", "closed_loop", "restore", "revert", "end_session"]

    def __init__(self, session, settings, profile, backup_dir, log_dir=None, clock=time.monotonic, prompt=None, **kw):
        super().__init__(session, log_dir=log_dir, clock=clock, **kw)
        self.settings = {s["dataid"]: s for s in settings}
        self.profile = profile or {}
        user = {k: v for k, v in (self.profile.get("limits") or {}).items() if not k.startswith("_") and v is not None}
        self.limits = Limits(user)
        self.backup_dir = Path(backup_dir)
        self.prompt = prompt or (lambda m: print(m, file=sys.stderr, flush=True))
        self.roles = {r: t for r, t in load_tables_yaml().items() if int(t["id"]) in self.settings}
        self.backups, self.proposals, self.tokens = {}, {}, set()
        self.aborted, self.events, self.watch, self.kept = None, [], None, False

    # ---- gates ----
    def gates(self):
        """Unmet conditions for any write. Empty when writes may be proposed."""
        out, p = [], self.profile
        if str(p.get("fuel", "unknown")).lower() in ("", "unknown"): out.append("fuel type is unknown (profile 'fuel')")
        if p.get("wideband_valid") not in (True, "yes", "true", "True"): out.append("wideband not declared valid (profile 'wideband_valid')")
        for k in REQUIRED_LIMITS:
            if self.limits.get(k) is None: out.append(f"limit '{k}' not declared (profile limits)")
        if not self.s.stationary: out.append("vehicle not declared stationary (--stationary)")
        if self.aborted: out.append(f"session aborted: {self.aborted}")
        return out

    def capabilities(self):
        c = super().capabilities()
        c.update(ops=["read_live", "logging", "read_tables", "backup", "write_dryrun", "write"], read_only=False,
                 write_roles=sorted(WRITE_ROLES), limits={k: self.limits.get(k) for k in REQUIRED_LIMITS + ["coolant_warning_c"]},
                 gates=self.gates())
        return c

    def connection_status(self):
        c = super().connection_status()
        c.update(armed=self.s.armed_until is not None and self.clock() < self.s.armed_until, tokens=len(self.tokens),
                 changes_in_place=len(self.s.journal), aborted=self.aborted, watchdog=self.watch is not None)
        return c

    # ---- tables ----
    def _setting(self, dataid):
        st = self.settings.get(int(dataid))
        if st is None: raise V.ToolError("unknown_table", f"DataId {dataid} not in the writable settings")
        return st

    def _decode_block(self, st, raw):
        w, sg = st["width"], st["signed"]
        vals = [int.from_bytes(raw[i:i + w], "little", signed=sg) * st["multi"] + st["offset"] for i in range(0, len(raw), w)]
        return [round(v, 4) for v in vals]

    def _read_setting(self, st):
        with self.lock: raw = self.s.read(st["start"], st["end"] - st["start"], timeout=2.0)
        return raw

    def _role(self, id):
        if id in self.roles: return id, self.roles[id]
        for r, t in self.roles.items():
            if t["native"] == id: return r, t
        raise V.ToolError("unknown_table", id)

    def list_tables(self):
        out = []
        for r, t in self.roles.items():
            st = self.settings[int(t["id"])]
            out.append(dict(id=r, role=r, native_name=t["native"], unit=t.get("unit", st["units"]), shape=st["shape"],
                            scale=st["scale"], writable=r in WRITE_ROLES and st["scale"] in C.WRITE_SCALES[6]))
        return out

    def get_table(self, id):
        role, t = self._role(id); st = self._setting(t["id"])
        rows, cols = st["shape"]; flat = self._decode_block(st, self._read_setting(st))
        values = [flat[r * cols:(r + 1) * cols] for r in range(rows)]
        axes = {}
        for k in ("x_labels", "y_labels"):
            if t.get(k) and int(t[k]) in self.settings:
                a = self.settings[int(t[k])]; axes[k] = self._decode_block(a, self._read_setting(a))
        x, y = axes.get("x_labels"), axes.get("y_labels")
        return dict(id=role, role=role, native_name=t["native"], unit=t.get("unit", st["units"]), values=values,
                    x_axis=x, y_axis=y, x_used=len(trim_axis(x)) if x else None, y_used=len(trim_axis(y)) if y else None,
                    scale=st["scale"], rows_are="y", note=t.get("note"))

    # ---- backup ----
    def backup(self, label):
        img = bytearray(b"\xff" * 0x665C)
        with self.lock:
            for a, n in READ_REGIONS: img[a:a + n] = self.s.read(a, n, timeout=3.0)
        sha = hashlib.sha256(img).hexdigest(); id_ = sha[:12]
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stem = self.backup_dir / f"{time.strftime('%Y-%m-%d_%H%M%S')}_live-{label}"
        Path(str(stem) + ".bin").write_bytes(bytes(img))
        Path(str(stem) + ".json").write_text(json.dumps(dict(sha256=sha, label=label, firmware=self.s.build,
                                                          regions=[dict(addr=f"0x{a:04X}", count=n) for a, n in READ_REGIONS], unread_is=255), indent=1))
        if hashlib.sha256(Path(str(stem) + ".bin").read_bytes()).hexdigest() != sha: raise V.ToolError("no_backup", "backup did not verify")
        self.backups[id_] = str(stem) + ".bin"
        return dict(backup_id=id_, label=label, path=str(stem) + ".bin", sha256=sha)

    def _backup_image(self, backup_id):
        """A backup's image: one taken in this session, or an earlier one in the backup folder whose recorded sha256
        starts with backup_id (at least 8 characters). Checked against its recorded checksum and firmware build."""
        p = self.backups.get(backup_id)
        if p is None and len(str(backup_id)) >= 8:
            for j in sorted(self.backup_dir.rglob("*.json")):
                try: meta = json.loads(j.read_text())
                except (ValueError, OSError): continue
                if str(meta.get("sha256", "")).startswith(str(backup_id)) and j.with_suffix(".bin").exists():
                    p = str(j.with_suffix(".bin")); break
        if p is None or not Path(p).exists(): raise V.ToolError("unknown_backup", str(backup_id))
        img = Path(p).read_bytes(); mp = Path(p).with_suffix(".json")
        meta = json.loads(mp.read_text()) if mp.exists() else {}
        if meta.get("sha256") and hashlib.sha256(img).hexdigest() != meta["sha256"]:
            raise V.ToolError("unknown_backup", "backup file does not match its recorded checksum")
        if meta.get("firmware") not in (None, self.s.build):
            raise V.ToolError("unknown_backup", f"backup is from firmware build {meta.get('firmware')}, the ECU runs {self.s.build}")
        return img, p

    def restore(self, backup_id, approval_token, tables=None):
        """Write tables back to a backup's values (default: every writable table). A backup of the current state is
        taken first, so a restore can itself be undone. Only differing cells are written, in row frames (blocked P, as
        PCLink sends table edits), each block checked; the whole table is read back. The restored ranges then equal the
        backup, so their journal entries are dropped (end_session does not undo a restore). Needs a token from arm() and
        the stationary declaration; allowed after an abort (it is a recovery path)."""
        if approval_token not in self.tokens: raise V.ToolError("not_approved", "an approval token from arm() is required")
        if not self.s.stationary: raise V.ToolError("safety_gate", "vehicle not declared stationary (--stationary)")
        roles = list(tables) if tables else sorted(WRITE_ROLES)
        for r in roles:
            if r not in WRITE_ROLES: raise V.ToolError("not_writable", f"{r}: only {sorted(WRITE_ROLES)} can be restored")
        img, path = self._backup_image(backup_id)
        pre = self.backup("pre-restore"); self.tokens.discard(approval_token)
        out, spans = [], []
        with self.lock:
            for r in roles:
                _, t = self._role(r); st = self._setting(t["id"]); rows, cols = st["shape"]; w = st["width"]
                a0, n = st["start"], st["end"] - st["start"]
                cur, want = self.s.read(a0, n, timeout=3.0), img[a0:a0 + n]
                cells, rows_written = 0, 0
                for row in range(rows):
                    base = row * cols * w
                    diff = [c for c in range(cols) if cur[base + c * w:base + (c + 1) * w] != want[base + c * w:base + (c + 1) * w]]
                    if not diff: continue
                    lo, hi = base + min(diff) * w, base + (max(diff) + 1) * w
                    self.s.write_range(a0 + lo, want[lo:hi], step_limit=False)
                    cells += len(diff); rows_written += 1
                ok = self.s.read(a0, n, timeout=3.0) == want
                if not ok: raise V.ToolError("write_failed", f"{r}: read-back after restore does not match the backup")
                out.append(dict(table=r, cells_restored=cells, rows_written=rows_written, matches_backup=ok)); spans.append((a0, a0 + n))
        self.s.journal = [e for e in self.s.journal if not any(s <= e[0] and e[0] + len(e[1]) <= z for s, z in spans)]
        self.s._save()
        self.events.append(dict(t=round(self.clock() - self.t0, 3), restore=backup_id, tables=out))
        return dict(restored=out, from_backup=path, pre_restore_backup=pre["backup_id"], changes_in_place=len(self.s.journal))

    # ---- arming (the human approval) ----
    def arm(self, approved_by=None):
        """Chat approval (quote the owner's message in approved_by) or, in handshake mode, the handshake at the car.
        Returns a single-use approval token."""
        self.s.arm(prompt=self.prompt, approved_by=approved_by)
        tok = uuid.uuid4().hex[:10]; self.tokens.add(tok)
        self.events.append(dict(t=round(self.clock() - self.t0, 3), armed=self.s.approval, approved_by=approved_by))
        return dict(armed=True, approval_token=tok, single_use=True, armed_for_s=C.ARM["armed_s"])

    # ---- writes ----
    def _cell(self, ch):
        role = ch.get("table")
        if role not in WRITE_ROLES: raise V.ToolError("not_writable", f"{role}: only {sorted(WRITE_ROLES)} can be written live")
        role, t = self._role(role); st = self._setting(t["id"])
        rows, cols = st["shape"]; r, c = int(ch["row"]), int(ch["col"])
        if not (0 <= r < rows and 0 <= c < cols): raise V.ToolError("out_of_range", f"cell {r},{c} outside {rows}x{cols}")
        return st, st["start"] + (r * cols + c) * st["width"]

    def propose_write(self, changes):
        """Dry run: validates every rule and returns a diff. Has no effect on the ECU."""
        g = self.gates()
        if g: raise V.ToolError("safety_gate", "; ".join(g))
        if not changes or len(changes) > MAX_CELLS: raise V.ToolError("out_of_bounds", f"1 to {MAX_CELLS} cells per proposal")
        seen, diff = set(), []
        for ch in changes:
            st, addr = self._cell(ch)
            if addr in seen: raise V.ToolError("out_of_range", f"cell {ch['row']},{ch['col']} given twice")
            seen.add(addr)
            with self.lock: old_raw = self.s.read(addr, st["width"])
            old_i = int.from_bytes(old_raw, "little", signed=st["signed"]); before = round(old_i * st["multi"] + st["offset"], 4)
            new_i = round((float(ch["value"]) - st["offset"]) / st["multi"]); after = round(new_i * st["multi"] + st["offset"], 4)
            if before <= 0: raise V.ToolError("out_of_bounds", f"cell {ch['row']},{ch['col']} holds {before}; a percentage step is undefined")
            pct = (after - before) / before * 100
            lim = self.limits.get("fuel_change_rich_max_pct" if pct > 0 else "fuel_change_lean_max_pct")
            if abs(pct) > lim + 1e-9:
                raise V.ToolError("out_of_bounds", f"cell {ch['row']},{ch['col']}: {before} -> {after} is {pct:+.2f} %, limit {lim} %")
            try: new_raw = new_i.to_bytes(st["width"], "little", signed=st["signed"])
            except OverflowError: raise V.ToolError("out_of_bounds", f"{after} does not fit the cell") from None
            diff.append(dict(table=ch["table"], row=int(ch["row"]), col=int(ch["col"]), before=before, after=after,
                             change_pct=round(pct, 2), direction="richer" if pct > 0 else "leaner" if pct < 0 else "none",
                             addr=f"0x{addr:04X}", _old=old_raw.hex(), _new=new_raw.hex()))
        id_ = uuid.uuid4().hex[:8]; self.proposals[id_] = diff
        return dict(proposal_id=id_, diff=[{k: v for k, v in d.items() if not k.startswith("_")} for d in diff])

    def _abort_checks(self, vals, base):
        """{kind: reason} for every abort condition present in one sample."""
        out = {}
        lam, tgt, rpm = vals.get("lambda"), vals.get("lambda_target"), vals.get("rpm") or 0
        if base.get("knock_count") is not None and vals.get("knock_count") is not None and vals["knock_count"] > base["knock_count"]:
            out["knock"] = f"knock count rose {base['knock_count']} -> {vals['knock_count']}"
        if rpm > 0 and lam and tgt and 0.5 < lam < 1.6 and 0.5 < tgt < 1.6 and lam - tgt > self.limits.get("lean_abort_lambda_margin"):
            out["lean"] = f"lambda {lam} leaner than target {tgt} by more than {self.limits.get('lean_abort_lambda_margin')}"
        if rpm > 500 and vals.get("oil_pressure_kpa") is not None and vals["oil_pressure_kpa"] < self.limits.get("oil_pressure_min_kpa"):
            out["oil"] = f"oil pressure {vals['oil_pressure_kpa']} kPa below {self.limits.get('oil_pressure_min_kpa')}"
        cw = self.limits.get("coolant_warning_c")
        if cw is not None and vals.get("ect") is not None and vals["ect"] > cw: out["coolant"] = f"coolant {vals['ect']} C above {cw}"
        return out

    def _abort_reason(self, vals, base):
        c = self._abort_checks(vals, base)
        return next(iter(c.values()), None)

    def _abort(self, reason):
        self.aborted = reason
        try:
            with self.lock: n = self.s.revert(auto=True)
            self.events.append(dict(t=round(self.clock() - self.t0, 3), abort=reason, reverted=n))
        except (C.EcuError, C.Refused) as e:
            self.events.append(dict(t=round(self.clock() - self.t0, 3), abort=reason, revert_failed=str(e)))
        self.prompt(f"ABORT: {reason}. Changes written back; power-cycling the ECU also reverts any unstored change.")

    def _start_watchdog(self, base):
        stop = threading.Event()
        def run():
            fails, streak = 0, {}
            while not stop.is_set() and self.s.journal and not self.aborted:
                try: _, vals = self._sample(); fails = 0
                except C.EcuError: fails += 1; vals = None
                if fails >= 5: self._abort("link lost (5 reads in a row failed)"); break
                if vals:
                    found = self._abort_checks(vals, base)
                    streak = {k: streak.get(k, 0) + 1 for k in found}
                    hit = [found[k] for k in found if k in ("knock", "coolant") or streak[k] >= ABORT_PERSIST]
                    if hit: self._abort(hit[0]); break
                stop.wait(WATCH_S)
            self.watch = None
        self.watch = dict(stop=stop, thread=threading.Thread(target=run, daemon=True), base=base); self.watch["thread"].start()

    def apply_write(self, proposal_id, approval_token):
        if proposal_id not in self.proposals: raise V.ToolError("unknown_proposal", proposal_id)
        if approval_token not in self.tokens: raise V.ToolError("not_approved", "an approval token from arm() is required")
        if not self.backups: raise V.ToolError("no_backup", "take a backup in this session first")
        g = self.gates()
        if g: raise V.ToolError("safety_gate", "; ".join(g))
        diff = self.proposals.pop(proposal_id); self.tokens.discard(approval_token)
        _, now = self._sample()
        base = self.watch["base"] if self.watch else dict(knock_count=now.get("knock_count"))
        r = self._abort_reason(now, base)
        if r: raise V.ToolError("safety_gate", f"abort condition active before writing: {r}")
        done = []
        with self.lock:
            for d in diff:
                addr, old, new = int(d["addr"], 16), bytes.fromhex(d["_old"]), bytes.fromhex(d["_new"])
                if self.s.read(addr, len(old)) != old:
                    raise V.ToolError("out_of_range", f"cell {d['row']},{d['col']} changed since the proposal; nothing more written")
                st = next(x for x in self.s.settings if x["start"] <= addr < x["end"])
                self.s.max_step = abs(int.from_bytes(new, "little", signed=st["signed"]) - int.from_bytes(old, "little", signed=st["signed"]))
                try: self.s.write(addr, new)
                except (C.EcuError, C.Refused) as e:
                    self.s.max_step = 1; n = self.s.revert(auto=True)
                    raise V.ToolError("write_failed", f"{e}; all {n} change(s) in this session written back") from None
                done.append({k: v for k, v in d.items() if not k.startswith("_")})
            self.s.max_step = 1
        if not self.watch: self._start_watchdog(base)
        return dict(applied=True, changes=done, changes_in_place=len(self.s.journal))

    # ---- closed-loop lambda (Lambda Mode, an enumerated setting) ----
    LAMBDA_MODE = 2394

    def closed_loop_status(self):
        """Lambda Mode as the ECU holds it now (read only)."""
        with self.lock: f, v = self.s.field(self.LAMBDA_MODE)
        return dict(lambda_mode=v, name=f["values"][v], closed_loop=v != 0, locked=not f["confirmed"],
                    original=getattr(self, "cl_original", None))

    def closed_loop(self, enabled, approval_token):
        """Turn closed-loop lambda off (Lambda Mode OFF) or back on (the mode it had before this session turned it off).
        RAM only and journalled, so revert() and end_session() also restore it. Needs a backup and a token from arm()."""
        if approval_token not in self.tokens: raise V.ToolError("not_approved", "an approval token from arm() is required")
        if not self.backups: raise V.ToolError("no_backup", "take a backup in this session first")
        if self.aborted: raise V.ToolError("safety_gate", f"session aborted: {self.aborted}")
        st = self.closed_loop_status()
        if enabled:
            if st["lambda_mode"] != 0: return dict(changed=False, **self.closed_loop_status())
            orig = getattr(self, "cl_original", None)
            if not orig: raise V.ToolError("out_of_range", "closed loop was not turned off in this session; its mode is unknown")
            target = orig
        else:
            if st["lambda_mode"] == 0: return dict(changed=False, **self.closed_loop_status())
            self.cl_original = st["lambda_mode"]; target = 0
        self.tokens.discard(approval_token)
        _, now = self._sample()
        base = self.watch["base"] if self.watch else dict(knock_count=now.get("knock_count"))
        try:
            with self.lock: self.s.write_field(self.LAMBDA_MODE, target)
        except C.Refused as e: raise V.ToolError("not_writable", str(e)) from None
        except C.EcuError as e: raise V.ToolError("write_failed", str(e)) from None
        self.events.append(dict(t=round(self.clock() - self.t0, 3), closed_loop="on" if enabled else "off", lambda_mode=target))
        if self.s.journal and not self.watch: self._start_watchdog(base)
        return dict(changed=True, **self.closed_loop_status())

    def revert(self):
        """Write every change of this session back (risk-reducing; no token needed)."""
        with self.lock: n = self.s.revert(auto=True)
        return dict(reverted=n)

    def end_session(self, keep=False):
        """Stop the watchdog; write everything back unless keep=True (then the changes stay in RAM until power-off)."""
        if self.watch: self.watch["stop"].set(); self.watch["thread"].join(timeout=2)
        n = 0 if keep else self.revert()["reverted"]
        self.kept = bool(keep and self.s.journal)
        return dict(kept=len(self.s.journal) if keep else 0, reverted=n, events=self.events,
                    journal=str(self.s.journal_path) if self.s.journal_path else None)

    def close(self):
        if self.s.journal and not self.kept: self.end_session(keep=False)
        elif self.watch: self.watch["stop"].set()
        super().close()
