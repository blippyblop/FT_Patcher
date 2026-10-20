#!/usr/bin/env python3
"""FT Patcher GUI — a small Tkinter front-end for the Hash Edits face-tracking tools.

Run it with any Python 3 that has tkinter (on Windows, a standard python.org
install has it; you can also rename this file to ft_gui.pyw so double-clicking
opens it without a console window):

    python ft_gui.py

Or use the packaged Windows build (no Python needed on the target machine):
build with  python -m PyInstaller ft_patcher.spec --noconfirm, then share the
dist/FT-Patcher folder. The packaged app
bundles ft_transplant.py plus the pure-Python patcher (ft_hpatchz.py); no
hpatchz binary is required, and only Blender itself is still needed at run
time.

What it does, per selected custom FBX:
  * transplants the FT blend shapes from (original + diff) onto the custom
    FBX, in place, using Blender's bundled Python (ft_transplant.py). The
    original file is only replaced after the new one is produced and verified,
    so a failed run never clobbers your file. Two modes:
      - transfer (default): resamples the new shape keys onto your mesh;
        works best when your edits to it are mild.
      - head swap: replaces exactly the mesh(es) the diff adds shapes to
        (e.g. the head) with the good-sample mesh(es), shape keys and
        vertex groups included, wearing your file's materials and keeping
        your file's mesh names. Use it when your edits to those meshes are
        too severe to resample onto (your edits to those meshes are lost,
        everything else in the file is untouched).
  * before exporting, the result file's meshes/materials are renamed to
    their clean names (Name.001/Name.002 duplicate suffixes that an
    earlier run of this tool may have baked into the file are dropped,
    e.g. "Body.002" goes back to "Body").
  * if "also patch the .meta file" is ticked and <file>.fbx.meta sits next to
    the FBX (as in a Unity project), it applies the text-based meta patch
    (ft_meta_patch.py) in place.
  * before modifying any file, a backup of the original is written: to the
    backup folder you choose, or next to the file as .bak / .bak2 / .bak3.
    Prefer a backup folder OUTSIDE your Unity project so the backups don't
    clutter it or get imported as assets.

It detects installed Blenders on start and lets you confirm or pick one; the
Blender you choose is only used to run the transplant (its bundled Python does
the FBX work). The meta patch runs on the Python that is running this GUI.
"""

import glob
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

try:
    import ft_meta_patch
except ImportError:
    ft_meta_patch = None

if getattr(sys, "frozen", False):
    BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = BASE_DIR
FT_TRANSPLANT = os.path.join(BASE_DIR, "ft_transplant.py")
FT_META_PATCH = os.path.join(BASE_DIR, "ft_meta_patch.py")

BLENDER_TIMEOUT = 1800
MISC_TIMEOUT = 60


def find_blender_installs():
    sysname = platform.system()
    found = []
    if sysname == "Windows":
        bases = [r"C:\Program Files\Blender Foundation",
                 r"C:\Program Files (x86)\Blender Foundation",
                 r"C:\Program Files\Blender",
                 os.path.expandvars(r"%LOCALAPPDATA%\Programs\Blender Foundation")]
        for base in bases:
            if not os.path.isdir(base):
                continue
            for d in sorted(glob.glob(os.path.join(base, "*")), reverse=True):
                exe = os.path.join(d, "blender.exe")
                if os.path.isfile(exe):
                    found.append(exe)
    elif sysname == "Darwin":
        for app in ("/Applications/Blender.app",
                    os.path.expanduser("~/Applications/Blender.app")):
            exe = os.path.join(app, "Contents", "MacOS", "Blender")
            if os.path.isfile(exe):
                found.append(exe)
    else:
        w = shutil.which("blender")
        if w:
            found.append(w)
        for cand in ("/usr/bin/blender", "/usr/local/bin/blender", "/snap/bin/blender"):
            if os.path.isfile(cand):
                found.append(cand)
        for d in (glob.glob("/opt/blender*")
                  + glob.glob(os.path.expanduser("~/blender*"))
                  + glob.glob(os.path.expanduser("~/Applications/Blender*"))):
            for sub in ("blender", os.path.join("blender-bin", "blender")):
                exe = os.path.join(d, sub)
                if os.path.isfile(exe):
                    found.append(exe)
    seen, out = set(), []
    for f in found:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def next_bak_path(path):
    p = path + ".bak"
    if not os.path.exists(p):
        return p
    n = 2
    while os.path.exists(path + ".bak%d" % n):
        n += 1
    return path + ".bak%d" % n


class FTGui:
    def __init__(self, root):
        self.root = root
        root.title("FT Patcher — Hash Edits face tracking")
        root.geometry("760x640")
        root.minsize(680, 520)

        self.custom_files = []
        self._blender_versions = {}

        self._build_ui()
        self._detect_blender()

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        top = tk.Frame(self.root)
        top.pack(fill="x", **pad)
        tk.Label(top, text="Blender:", font=("", 9, "bold")).grid(row=0, column=0, sticky="w")
        self.blender_var = tk.StringVar()
        self.blender_cb = ttk.Combobox(top, textvariable=self.blender_var, width=52,
                                       state="combobox")
        self.blender_cb.grid(row=0, column=1, sticky="we", padx=(6, 4))
        self.blender_cb.bind("<<ComboboxSelected>>",
                             lambda e: self._verify_blender(self.blender_var.get()))
        tk.Button(top, text="Browse...", width=9,
                  command=self._browse_blender).grid(row=0, column=2)
        self.blender_status = tk.Label(top, text="", foreground="#666")
        self.blender_status.grid(row=1, column=1, sticky="we")
        top.columnconfigure(1, weight=1)

        tk.Frame(self.root, height=1).pack(fill="x")

        def path_row(parent, row, label, hint, var):
            tk.Label(parent, text=label, font=("", 9, "bold")).grid(row=row, column=0, sticky="w", padx=(0, 6))
            e = tk.Entry(parent, textvariable=var, width=56)
            e.grid(row=row, column=1, sticky="we")
            tk.Button(parent, text="...", width=3,
                      command=lambda: self._browse_file(var, is_dir=False)).grid(row=row, column=2)
            if hint:
                tk.Label(parent, text=hint, foreground="#666", anchor="w").grid(
                    row=row + 1, column=1, columnspan=2, sticky="we")
            return e

        f = tk.Frame(self.root)
        f.pack(fill="x", **pad)
        self.diff_var = tk.StringVar()
        self.original_var = tk.StringVar()
        path_row(f, 0, "Diff file:",
                 "the hash diff from the package, e.g.  "
                 "Hash's_Things/Linne/Patcher/Data/DiffFiles/Linne.hdiff",
                 self.diff_var)
        path_row(f, 2, "Original FBX:",
                 "This is the FBX the hash diff is meant to be used on; use the exact "
                 "version and file it asks for.",
                 self.original_var)
        f.columnconfigure(1, weight=1)

        m = tk.LabelFrame(self.root, text="Custom FBX file(s) to patch")
        m.pack(fill="both", expand=True, **pad)
        self.fbx_list = tk.Listbox(m, selectmode="extended", exportselection=False)
        sb = tk.Scrollbar(m, command=self.fbx_list.yview)
        self.fbx_list.configure(yscrollcommand=sb.set)
        self.fbx_list.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        btns = tk.Frame(m)
        btns.pack(fill="x", pady=(4, 0))
        tk.Button(btns, text="Add...", command=self._add_fbx).pack(side="left")
        tk.Button(btns, text="Remove", command=self._remove_fbx).pack(side="left", padx=4)
        tk.Button(btns, text="Clear", command=self._clear_fbx).pack(side="left")
        tk.Label(btns, text="select one or more avatar FBX files",
                 foreground="#666").pack(side="left", padx=10)

        o = tk.Frame(self.root)
        o.pack(fill="x", **pad)
        self.meta_var = tk.BooleanVar(value=True)
        tk.Checkbutton(o, variable=self.meta_var,
                       text="Patch meta file"
                       ).pack(anchor="w")
        tk.Label(o, text="Mode:", font=("", 9, "bold"), anchor="w").pack(anchor="w", pady=(6, 0))
        self.mode_var = tk.StringVar(value="transfer")
        tk.Radiobutton(o, variable=self.mode_var, value="transfer",
                       text="Shape Key Transfer"
                       ).pack(anchor="w")
        tk.Radiobutton(o, variable=self.mode_var, value="swap",
                       text="Head Swap"
                       ).pack(anchor="w")
        self.force_xfer_var = tk.BooleanVar(value=False)
        self.force_xfer_cb = tk.Checkbutton(o, variable=self.force_xfer_var,
                                            text="Force resample on custom mesh")
        self.force_xfer_cb.pack(anchor="w")
        self.mode_var.trace_add("write", self._sync_force_xfer)
        tk.Label(o, text="Backup location (recommended: a folder OUTSIDE your Unity project):",
                 font=("", 9, "bold"), anchor="w").pack(anchor="w", pady=(6, 0))
        b = tk.Frame(o)
        b.pack(fill="x")
        self.backup_var = tk.StringVar()
        tk.Entry(b, textvariable=self.backup_var).pack(side="left", fill="x", expand=True)
        tk.Button(b, text="...", width=3,
                  command=self._browse_backup).pack(side="left", padx=(4, 0))
        tk.Label(o,
                 text="leave blank to store .bak / .bak2 / .bak3 next to each file — but a folder "
                 "outside the Unity project keeps your project clean",
                 foreground="#666", anchor="w").pack(anchor="w")

        r = tk.Frame(self.root)
        r.pack(fill="x", **pad)
        self.dry_btn = tk.Button(r, text="Dry run", width=14,
                                 command=lambda: self._start(dry_run=True))
        self.dry_btn.pack(side="left")
        self.run_btn = tk.Button(r, text="Run", width=14,
                                 command=lambda: self._start(dry_run=False))
        self.run_btn.pack(side="left", padx=6)

        lg = tk.LabelFrame(self.root, text="Log")
        lg.pack(fill="both", expand=True, **pad)
        self.log_text = scrolledtext.ScrolledText(lg, height=10, state="disabled",
                                                  font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True)

    def _sync_force_xfer(self, *_a):
        if self.mode_var.get() == "transfer":
            self.force_xfer_cb.pack(anchor="w")
        else:
            self.force_xfer_cb.pack_forget()

    def _detect_blender(self):
        installs = find_blender_installs()
        self._blender_installs = installs
        self.blender_cb["values"] = installs
        if installs:
            self.blender_var.set(installs[0])
            self._verify_blender(installs[0])
        else:
            self.blender_status.config(text="no Blender found — point to one with Browse...",
                                       foreground="#a00")

    def _verify_blender(self, path):
        if not path:
            self.blender_status.config(text="no Blender selected", foreground="#a00")
            return
        if path in self._blender_versions:
            self._show_blender_status(self._blender_versions[path])
            return
        self.blender_status.config(text="checking %s ..." % os.path.basename(path),
                                   foreground="#666")
        def check():
            ok, text = self._blender_version(path)
            self._blender_versions[path] = (ok, text)
            self.root.after(0, lambda: self._show_blender_status((ok, text)))
        threading.Thread(target=check, daemon=True).start()

    def _show_blender_status(self, ok_text):
        ok, text = ok_text
        if ok:
            self.blender_status.config(text=text, foreground="#080")
        else:
            self.blender_status.config(text=text or "not a runnable Blender", foreground="#a00")

    def _blender_version(self, path):
        try:
            r = subprocess.run([path, "--version"], capture_output=True, text=True,
                               timeout=MISC_TIMEOUT)
            out = (r.stdout or r.stderr).strip()
            first = out.splitlines()[0] if out else ""
            if r.returncode == 0 and "Blender" in first:
                return True, first
            return False, out or "no output"
        except (OSError, subprocess.SubprocessError):
            return False, "could not run: %s" % path

    def _browse_blender(self):
        p = filedialog.askopenfilename(title="Select the Blender executable",
                                       filetypes=[("Blender", "blender.exe Blender blender"),
                                                  ("All files", "*")])
        if p:
            self.blender_var.set(p)
            self._verify_blender(p)

    def _browse_file(self, var, is_dir=False):
        if is_dir:
            p = filedialog.askdirectory(title="Select folder")
        else:
            p = filedialog.askopenfilename(title="Select file")
        if p:
            var.set(p)

    def _browse_backup(self):
        p = filedialog.askdirectory(title="Select a backup folder (outside the Unity project)")
        if p:
            self.backup_var.set(p)

    def _add_fbx(self):
        ps = filedialog.askopenfilenames(
            title="Select one or more custom FBX files",
            filetypes=[("FBX", "*.fbx"), ("All files", "*")])
        if not ps:
            return
        for p in ps:
            ap = os.path.abspath(p)
            if ap not in self.custom_files:
                self.custom_files.append(ap)
        self._refresh_fbx_list()

    def _remove_fbx(self):
        for i in sorted(self.fbx_list.curselection(), reverse=True):
            del self.custom_files[i]
        self._refresh_fbx_list()

    def _clear_fbx(self):
        self.custom_files = []
        self._refresh_fbx_list()

    def _refresh_fbx_list(self):
        self.fbx_list.delete(0, "end")
        for p in self.custom_files:
            self.fbx_list.insert("end", p)

    def _log(self, line):
        def append():
            self.log_text.configure(state="normal")
            self.log_text.insert("end", line + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.root.after(0, append)

    def _validate(self):
        errors = []
        blender = self.blender_var.get().strip()
        if not blender or not os.path.isfile(blender):
            errors.append("select a valid Blender executable")
        diff = self.diff_var.get().strip()
        if not diff or not os.path.isfile(diff):
            errors.append("set an existing diff (.hdiff) file")
        original = self.original_var.get().strip()
        if not original or not os.path.isfile(original):
            errors.append("set an existing original FBX (the exact file the diff was built on)")
        if not self.custom_files:
            errors.append("add at least one custom FBX to patch")
        for p in self.custom_files:
            if not os.path.isfile(p):
                errors.append("missing file: %s" % p)
        return errors

    def _set_buttons(self, enabled):
        state = "normal" if enabled else "disabled"
        self.dry_btn.config(state=state)
        self.run_btn.config(state=state)

    def _start(self, dry_run):
        errors = self._validate()
        if errors:
            messagebox.showerror("FT Patcher", "Fix these first:\n\n" + "\n".join(errors))
            return
        self._set_buttons(False)
        self._log("\n=== %s: %d file(s) ===" % ("dry run" if dry_run else "run",
                                                 len(self.custom_files)))
        if not self.backup_var.get().strip() and not dry_run:
            self._log("note: no backup folder set — .bak files will be created next to each file.")
        threading.Thread(target=self._worker, args=(dry_run,), daemon=True).start()

    def _run_cmd(self, cmd, timeout):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return r.returncode, (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
        except subprocess.TimeoutExpired:
            return -1, "timed out after %ds" % timeout
        except OSError as e:
            return -1, "could not run: %s" % e

    def _backup(self, path, backup_dir):
        if backup_dir:
            os.makedirs(backup_dir, exist_ok=True)
            dest = os.path.join(backup_dir, os.path.basename(path))
            base = dest
            n = 0
            while os.path.exists(dest):
                n += 1
                dest = "%s.bak%d" % (base, n) if n > 1 else base + ".bak"
            shutil.copy2(path, dest)
            return dest
        dest = next_bak_path(path)
        shutil.copy2(path, dest)
        return dest

    def _hpatchz_path(self):
        # Legacy fallback only: the pure-Python patcher (ft_hpatchz) is the
        # primary path and needs no binary. This merely passes --hpatchz to
        # ft_transplant.py if someone happens to have dropped a binary into the
        # package dir; it is ignored unless the bundled patcher is unavailable.
        name = "hpatchz.exe" if os.name == "nt" else "hpatchz"
        p = os.path.join(BASE_DIR, name)
        if os.path.isfile(p):
            return p
        return None

    def _process_fbx(self, blender, diff, original, custom, backup_dir, dry_run,
                  force_xfer=False, mode="transfer"):
        self._log("\n--- %s" % os.path.basename(custom))
        tmp = os.path.join(tempfile.mkdtemp(prefix="ftgui_"), "out.fbx")
        cmd = [blender, "--background", "--python", FT_TRANSPLANT, "--",
               "--original", original, "--diff", diff,
               "--custom", custom, "--out", tmp, "--verbose"]
        if mode == "swap":
            cmd += ["--mode", "swap"]
        hp = self._hpatchz_path()
        if hp:
            cmd += ["--hpatchz", hp]
        if dry_run:
            cmd.append("--dry-run")
        if force_xfer:
            cmd.append("--force-transfer")
        self._log("$ %s" % " ".join(cmd))
        code, out = self._run_cmd(cmd, BLENDER_TIMEOUT)
        for line in out.strip().splitlines():
            self._log("    " + line)
        ok = (code == 0)
        if dry_run:
            self._log("    [dry] would transplant" if ok else "    [dry] skipped (code %d)" % code)
            self._cleanup(os.path.dirname(tmp))
            return ok
        if not ok or not os.path.isfile(tmp) or os.path.getsize(tmp) == 0:
            self._log("    ERROR: transplant failed (code %d) — original left untouched" % code)
            self._cleanup(os.path.dirname(tmp))
            return False
        bak = self._backup(custom, backup_dir)
        try:
            os.replace(tmp, custom)
        except OSError:
            shutil.copy2(tmp, custom)
            os.remove(tmp)
        self._log("    OK: %s updated (backup: %s)" % (os.path.basename(custom), bak))
        self._cleanup(os.path.dirname(tmp))
        return True

    @staticmethod
    def _cleanup(path):
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass

    def _meta_patch(self, meta, dry_run):
        code, lines = self._meta_patch_impl(meta, dry_run)
        return code, lines

    def _meta_patch_impl(self, meta, dry_run):
        if not os.path.isfile(meta):
            return 1, ["meta file not found: %s" % meta]
        if ft_meta_patch is not None:
            bones = [b.strip() for b in ft_meta_patch.BONES_DEFAULT.split(",") if b.strip()]
            lines, report, missing, patched = ft_meta_patch.patch_meta(
                meta, ft_meta_patch.EYE_MIN_DEFAULT, ft_meta_patch.EYE_MAX_DEFAULT, bones)
            log = ["meta: %s" % meta]
            log += ["  " + r for r in report]
            log += ["  %s: NOT FOUND in humanoid mapping (map the eye bones in Rig -> Configure first)"
                    % m for m in missing]
            if patched == 0:
                log.append("nothing to patch")
                return 2, log
            if dry_run:
                log.append("--dry-run: no files written")
            else:
                with open(meta, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines))
                log.append("wrote %s" % meta)
            return 0, log
        cmd = [sys.executable, FT_META_PATCH, meta] + (["--dry-run"] if dry_run else [])
        code, out = self._run_cmd(cmd, MISC_TIMEOUT)
        return code, out.strip().splitlines()

    def _process_meta(self, custom, dry_run):
        meta = custom + ".meta"
        if not os.path.isfile(meta):
            self._log("    meta: no %s next to file — nothing to do" % os.path.basename(meta))
            return True
        code, lines = self._meta_patch(meta, True)
        for line in lines:
            self._log("    " + line)
        if dry_run:
            return code in (0, 2)
        if code == 2:
            self._log("    meta: no eye bones to patch — left untouched")
            return True
        if code != 0:
            self._log("    meta: dry-run failed (code %d) — left untouched" % code)
            return False
        bak = self._backup(meta, self.backup_var.get().strip())
        code2, lines2 = self._meta_patch(meta, False)
        for line in lines2:
            self._log("    " + line)
        if code2 == 0:
            self._log("    OK: %s patched (backup: %s)" % (os.path.basename(meta), bak))
            return True
        self._log("    meta: patch failed (code %d)" % code2)
        return False

    def _worker(self, dry_run):
        blender = self.blender_var.get().strip()
        diff = self.diff_var.get().strip()
        original = self.original_var.get().strip()
        backup_dir = self.backup_var.get().strip()
        patch_meta = self.meta_var.get()
        mode = self.mode_var.get()
        force_xfer = self.force_xfer_var.get() and mode == "transfer"

        n_ok = n_total = 0
        for custom in list(self.custom_files):
            n_total += 1
            fbx_ok = self._process_fbx(blender, diff, original, custom, backup_dir,
                                        dry_run, force_xfer, mode)
            meta_ok = True
            if patch_meta:
                meta_ok = self._process_meta(custom, dry_run)
            if fbx_ok and meta_ok:
                n_ok += 1

        verb = "would patch" if dry_run else "patched"
        self._log("\n=== done: %d/%d %s ===" % (n_ok, n_total, verb))
        self.root.after(0, lambda: self._set_buttons(True))
        self.root.after(0, lambda: messagebox.showinfo(
            "FT Patcher",
            "%s %d of %d file(s).\n\nSee the log for details." % (
                "Dry run — would patch" if dry_run else "Patched", n_ok, n_total)))


def main():
    selftest = "--selftest" in sys.argv[1:]
    try:
        root = tk.Tk()
    except tk.TclError as e:
        print("could not start the Tk GUI: %s" % e)
        print("make sure Python has tkinter installed (Windows: it is included in a")
        print("standard python.org install; Debian/Ubuntu:  sudo apt install python3-tk)")
        sys.exit(1)
    gui = FTGui(root)
    if selftest:
        # Build and exercise the UI once, then exit: verifies widgets, ttk
        # imports, and Blender detection without entering the mainloop.
        for _ in range(5):
            root.update()
        root.update_idletasks()
        root.destroy()
        print("SELFTEST_OK blenders=%d" % len(gui._blender_installs))
        sys.exit(0)
    root.mainloop()


if __name__ == "__main__":
    main()