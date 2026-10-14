#!/usr/bin/env python3
"""FT patcher — applies a Hash Edits face-tracking binary diff (hdiff) to an FBX,
optionally the matching meta diff to its .meta file, using the bundled pure-Python
patcher (byte-for-byte equivalent to HDiffPatch's hpatchz; an external hpatchz
binary is only used as a fallback if that module is unavailable).

Replaces the in-Unity patcher: works from any shell, cross-platform, and does
pre/post verification (base hash, output magic, FT marker strings).

Usage:
    python3 ft_patch.py --diff <path.hdiff> --fbx <target.fbx> [options]

Where to find the args:
    --diff  ships in the package at
            Hash's_Things/<Character>/Patcher/Data/DiffFiles/<Character>.hdiff
    --fbx   the ORIGINAL (unpatched) character FBX, the exact version the diff
            was built against (e.g. Linne v1.0.3 from the original avatar
            download). The tool verifies known base hashes and will tell you
            which version it expects if yours doesn't match.

If a .meta file sits next to the target FBX and a matching meta diff exists
(<diff name>Meta.hdiff next to the diff), the meta is patched too.
"""
import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile


def _import_patcher():
    """Import the bundled pure-Python patcher (``ft_hpatchz``) if available.

    Returns the module, or ``None`` (callers then fall back to an external
    ``hpatchz`` binary). ``ft_hpatchz.py`` sits next to this script in the
    source tree and in the PyInstaller bundle, so the script's own directory
    is added to ``sys.path`` before the retry.
    """
    try:
        import ft_hpatchz  # noqa: F401  (sibling module)
        return ft_hpatchz
    except ImportError:
        pass
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)
        import ft_hpatchz  # noqa: F401
        return ft_hpatchz
    except ImportError:
        return None

FT_MARKERS = (b"--FTEffects--", b"--FaceTracking--")
FBX_MAGIC = b"Kaydara FBX Binary"

KNOWN_BASES = {
    "Linne.hdiff": {
        "character": "Linne",
        "expects": "the ORIGINAL Linne FBX, v1.0.3 (the diff was built against this exact file; v1.0.4 shipped byte-identical so it also passes)",
        "sha256": "74dc64355a560619572f1eca24139257c1efbdcdead9f5661328639005a81a80",
        "size": 14256572,
    },
}


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def probe_executable(bin_path):
    try:
        subprocess.run([bin_path], capture_output=True, timeout=10)
        return True
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return False


def find_hpatchz(explicit, diff_path):
    pinned = []
    if explicit:
        pinned.append(("the --hpatchz argument", explicit))
    env = os.environ.get("FTPATCH_HPATCHZ")
    if env:
        pinned.append(("FTPATCH_HPATCHZ env var", env))

    for label, path in pinned:
        if not os.path.isfile(path):
            die(f"{label} not found: {path}")
        if probe_executable(path):
            return path
        die(f"{label} was found but does not run on this system: {path}")

    candidates = []
    on_path = shutil.which("hpatchz")
    if on_path:
        candidates.append(("PATH", on_path))
    system = platform.system()
    if system == "Windows":
        subdir, name = "Windows", "hpatchz.exe"
    elif system == "Darwin":
        subdir, name = "Mac", "hpatchz"
    else:
        subdir, name = "Linux", "hpatchz"
    data_dir = os.path.normpath(os.path.join(os.path.dirname(diff_path), ".."))
    bundled = os.path.join(data_dir, "hdiff", "hpatchz", subdir, name)
    if os.path.isfile(bundled):
        candidates.append(("package bundle (Patcher/Data/hdiff/hpatchz/)", bundled))
    local_build = "/tmp/opencode/hpatchz/hpatchz"
    if os.path.isfile(local_build):
        candidates.append(("local build", local_build))

    tried = []
    for label, path in candidates:
        if not os.path.isfile(path):
            continue
        if probe_executable(path):
            if label != "local build":
                return path
            print(f"note: bundled/PATH hpatchz binaries could not run on this system; using local build")
            return path
        tried.append(f"  {label}: {path} (found, but does not run on this system)")

    die(
        "could not find a runnable hpatchz binary. Tried:\n"
        + ("\n".join(tried) if tried else "  (no candidates found)")
        + "\nOn Alpine/musl systems the package's glibc binaries won't run; build hpatchz "
        "(HDiffPatch v4.5.2) or point --hpatchz at a working binary."
    )


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def run_hpatchz(hpatchz, base, diff, out):
    r = subprocess.run([hpatchz, base, diff, out], capture_output=True, text=True)
    detail = (r.stdout + r.stderr).strip()
    if r.returncode != 0 or "patch ok" not in r.stdout:
        os.path.exists(out) and os.remove(out)
        die(f"hpatchz failed on {os.path.basename(base)}:\n{detail or 'no output'}")
    return detail.strip().splitlines()[-1] if detail.strip() else "patch ok!"


def apply_diff(patcher, hpatchz, base, diff, out):
    """Apply ``diff`` to ``base`` -> ``out`` via the pure-Python patcher
    (``patcher``) when available, else the ``hpatchz`` binary."""
    if patcher is not None:
        try:
            patcher.apply_patch_file(base, diff, out)
        except patcher.HdiffPatchError as e:
            os.path.exists(out) and os.remove(out)
            die(f"patch failed on {os.path.basename(base)}: {e}")
        except OSError as e:
            os.path.exists(out) and os.remove(out)
            die(f"patch failed on {os.path.basename(base)}: {e}")
        return "patch ok!"
    return run_hpatchz(hpatchz, base, diff, out)


def in_place_via_temp(patcher, hpatchz, target, diff, tmp_out):
    apply_diff(patcher, hpatchz, target, diff, tmp_out)
    bak = os.path.join(tempfile.gettempdir(), os.path.basename(target) + ".preft-backup")
    shutil.copy2(target, bak)
    os.replace(tmp_out, target)
    return bak


def verify_fbx_output(path, expect_markers=True):
    problems = []
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(32)
        body = f.read()
    if not head.startswith(FBX_MAGIC):
        problems.append("output does not start with the FBX binary magic — not a valid FBX")
    markers_present = any(m in head + body for m in FT_MARKERS)
    if expect_markers and not markers_present:
        problems.append("no FT marker strings found in the output (patch may not have applied)")
    return size, problems


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--diff", required=True,
                   help="binary diff file (hdiff) from the package: "
                        "Hash's_Things/<Character>/Patcher/Data/DiffFiles/<Character>.hdiff")
    p.add_argument("--fbx", required=True,
                   help="the ORIGINAL (unpatched) FBX to patch. Must be the exact "
                        "version the diff was built against — the tool prints what it "
                        "expects for known diffs and verifies the base hash")
    p.add_argument("--meta-diff", default=None,
                   help="meta diff to apply next to the FBX meta. Default: <diff name>Meta.hdiff "
                        "in the same folder as --diff")
    p.add_argument("--hpatchz", default=None,
                   help="legacy fallback: path to an hpatchz binary, only used if the "
                        "bundled pure-Python patcher (ft_hpatchz) is unavailable")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--in-place", action="store_true", help="overwrite the original files (a backup is saved to your temp dir)")
    mode.add_argument("--copy", action="store_true", help='write a new "<name> FT" file instead (default when non-interactive)')
    p.add_argument("--dry-run", action="store_true", help="run all checks without writing anything")
    p.add_argument("--force", action="store_true", help="overwrite existing output files")
    args = p.parse_args()

    if not os.path.isfile(args.diff):
        die(f"diff file not found: {args.diff}")
    if not os.path.isfile(args.fbx):
        die(f"target FBX not found: {args.fbx}")

    diff_name = os.path.basename(args.diff)
    known = KNOWN_BASES.get(diff_name)
    if known:
        print(f"diff: {diff_name} — expects: {known['expects']}")

    patcher = _import_patcher()
    if patcher is not None:
        print("patcher: bundled pure-Python patcher (ft_hpatchz) — no hpatchz binary needed")
        hpatchz = None
    else:
        hpatchz = find_hpatchz(args.hpatchz, args.diff)
        print(f"hpatchz: {hpatchz}")

    size = os.path.getsize(args.fbx)
    if known:
        digest = sha256_of(args.fbx)
        if digest != known["sha256"]:
            die(
                f"base file does not match what {diff_name} was built against.\n"
                f"  expected: sha256 {known['sha256']}  ({known['size']} bytes)\n"
                f"  got:      sha256 {digest}  ({size} bytes)\n"
                f"  the diff is byte-exact; you need the exact original file "
                f"({known['expects']})"
            )
        print(f"base hash: OK ({known['sha256'][:16]}…, {size} bytes)")
    else:
        print(f"base: {size} bytes (no known reference hash for {diff_name}; the patcher will verify)")

    with open(args.fbx, "rb") as f:
        sample = f.read(1 << 20)
    if any(m in sample for m in FT_MARKERS):
        die("target already looks patched (FT marker strings present) — use the unpatched original")

    stem = os.path.splitext(os.path.basename(args.fbx))[0]
    base_dir = os.path.dirname(os.path.abspath(args.fbx))
    out_fbx = args.fbx if args.in_place else os.path.join(base_dir, f"{stem} FT.fbx")
    meta_src = args.fbx + ".meta"
    out_meta = out_fbx + ".meta"

    meta_diff = args.meta_diff
    if meta_diff is None:
        guessed = os.path.join(os.path.dirname(args.diff), diff_name.replace(".hdiff", "Meta.hdiff"))
        meta_diff = guessed if os.path.isfile(guessed) else None

    if not args.in_place and not args.copy:
        print(f"\ntarget: {args.fbx}")
        print("  1) make a copy:  %s" % os.path.relpath(out_fbx, os.getcwd()))
        print("  2) overwrite the original in place")
        while True:
            try:
                choice = input("patch how? [1] ").strip() or "1"
            except EOFError:
                choice = "1"
            if choice in ("1", "2"):
                break
            print("pick 1 or 2")
        if choice == "2":
            args.in_place = True
            out_fbx = args.fbx
            out_meta = out_fbx + ".meta"

    for out in (out_fbx, out_meta):
        if out not in (args.fbx, meta_src) and os.path.exists(out) and not args.force:
            die(f"output already exists: {out} (use --force to overwrite)")

    print(f"\nmode: {'in place' if args.in_place else 'copy -> ' + out_fbx}")

    if args.dry_run:
        print("\ndry run: all checks passed, nothing written")
        return

    tmp_out = os.path.join(tempfile.mkdtemp(prefix="ftpatch_"), "out")
    if args.in_place:
        bak = in_place_via_temp(patcher, hpatchz, args.fbx, args.diff, tmp_out)
        print(f"patched: {args.fbx}  (backup: {bak})")
    else:
        apply_diff(patcher, hpatchz, args.fbx, args.diff, tmp_out)
        shutil.move(tmp_out, out_fbx)
        print(f"patched: {out_fbx}")

    fsize, problems = verify_fbx_output(out_fbx)
    for problem in problems:
        die(problem)
    print(f"verified: {fsize} bytes, valid FBX binary, FT markers present")

    if os.path.isfile(meta_src):
        if meta_diff:
            print(f"meta diff: {meta_diff}")
            tmp_meta = os.path.join(os.path.dirname(tmp_out), "meta_out")
            if args.in_place:
                bak_m = os.path.join(tempfile.gettempdir(), os.path.basename(meta_src) + ".preft-backup")
                shutil.copy2(meta_src, bak_m)
                apply_diff(patcher, hpatchz, meta_src, meta_diff, tmp_meta)
                os.replace(tmp_meta, meta_src)
                print(f"patched: {meta_src}  (backup: {bak_m})")
            else:
                apply_diff(patcher, hpatchz, meta_src, meta_diff, tmp_meta)
                shutil.move(tmp_meta, out_meta)
                print(f"patched: {out_meta}")
            print("meta verified: patched and written")
        else:
            print(f"warning: {os.path.basename(meta_src)} exists but no meta diff found "
                  f"(looked for {diff_name.replace('.hdiff', 'Meta.hdiff')} next to the diff) — "
                  "meta left untouched. For a custom meta, use ft_meta_patch.py instead.")
    else:
        print(f"note: no .meta next to {os.path.basename(args.fbx)} — Unity will generate one on import; "
              "if you need the FT rig settings, run ft_meta_patch.py on it afterward")

    print("\ndone.")
    if args.in_place:
        print("reimport the asset in Unity (or refocus the editor) to pick up the change")
    else:
        print(f"drop the new pair into your project: {os.path.basename(out_fbx)}"
              + (f" + {os.path.basename(out_meta)}" if os.path.isfile(meta_src) else ""))


if __name__ == "__main__":
    main()