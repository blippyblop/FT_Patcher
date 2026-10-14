#!/usr/bin/env python3
"""FT Meta Lite Patcher — standalone text patcher for Hash Edits face-tracking metas.

Rewrites the LeftEye/RightEye per-muscle limit lines in an FBX's .meta file so
your custom avatar gets the FT head's eye-rotation settings, without Unity or
binary hdiff/hpatchz tooling.

Usage:
    python3 ft_meta_patch.py <path/to/your.fbx> [options]
    python3 ft_meta_patch.py <path/to/your.fbx.meta> [options]

Options:
    --dry-run        show the changes without writing
    --min x,y,z      override eye min limit   (default: 0,-24,-12)
    --max x,y,z      override eye max limit   (default: 0,24,18)
    --bones a,b      human bone names to patch (default: LeftEye,RightEye)

After patching, open/focus your Unity project: it reimports the .meta
automatically. If Unity is closed, the change is applied on next launch.
"""
import argparse
import os
import sys

EYE_MIN_DEFAULT = "0,-24,-12"
EYE_MAX_DEFAULT = "0,24,18"
BONES_DEFAULT = "LeftEye,RightEye"


def get_indent(line: str) -> str:
    n = 0
    for c in line:
        if c == " ":
            n += 1
        else:
            break
    return " " * n


def resolve_meta_path(target: str) -> str:
    if target.endswith(".meta"):
        return target
    if target.endswith(".fbx"):
        return target + ".meta"
    return None


def fmt_vec(v: str) -> str:
    x, y, z = v.split(",")
    return "{x: " + x.strip() + ", y: " + y.strip() + ", z: " + z.strip() + "}"


def patch_meta(meta_path: str, min_vec: str, max_vec: str, bones) -> tuple:
    with open(meta_path, "r", encoding="utf-8") as f:
        lines = f.read().split("\n")

    report = []
    missing = []
    patched = 0

    for i, line in enumerate(lines):
        trimmed = line.strip()
        target = next((b for b in bones if trimmed == f"humanName: {b}"), None)
        if target is None:
            continue

        limit_start = -1
        for j in range(i + 1, min(len(lines), i + 4)):
            t = lines[j].strip()
            if t.startswith("- boneName:"):
                break
            if t == "limit:":
                limit_start = j
                break
        if limit_start < 0 or limit_start + 1 >= len(lines):
            missing.append(target)
            continue

        field_indent = get_indent(lines[limit_start + 1])
        old_min = old_max = ""
        for j in range(limit_start + 1, len(lines)):
            ln = lines[j]
            if ln.strip() and len(get_indent(ln)) < len(field_indent):
                break
            t = ln.strip()
            if t.startswith("min:"):
                old_min = t[4:].strip()
                lines[j] = field_indent + "min: " + fmt_vec(min_vec)
            elif t.startswith("max:"):
                old_max = t[4:].strip()
                lines[j] = field_indent + "max: " + fmt_vec(max_vec)
            elif t.startswith("modified:"):
                lines[j] = field_indent + "modified: 1"

        patched += 1
        report.append(
            f"{target}: min {old_min} -> {fmt_vec(min_vec)}, "
            f"max {old_max} -> {fmt_vec(max_vec)}, modified -> 1"
        )

    return lines, report, missing, patched


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("target", help="path to your .fbx or .fbx.meta")
    p.add_argument("--dry-run", action="store_true", help="show changes without writing")
    p.add_argument("--min", default=EYE_MIN_DEFAULT, metavar="X,Y,Z", help="eye min limit")
    p.add_argument("--max", default=EYE_MAX_DEFAULT, metavar="X,Y,Z", help="eye max limit")
    p.add_argument("--bones", default=BONES_DEFAULT, metavar="A,B", help="human bone names to patch")
    args = p.parse_args()

    try:
        min_vec, max_vec = args.min, args.max
        fmt_vec(min_vec)
        fmt_vec(max_vec)
    except ValueError:
        print("error: --min/--max must be x,y,z (e.g. 0,-24,-12)", file=sys.stderr)
        return 1

    bones = [b.strip() for b in args.bones.split(",") if b.strip()]
    meta_path = resolve_meta_path(args.target)
    if meta_path is None:
        print(f"error: {args.target} is neither a .fbx nor a .fbx.meta", file=sys.stderr)
        return 1
    if not os.path.isfile(meta_path):
        print(f"error: meta file not found: {meta_path}", file=sys.stderr)
        print("import the FBX in Unity first so the meta gets generated", file=sys.stderr)
        return 1

    lines, report, missing, patched = patch_meta(meta_path, min_vec, max_vec, bones)

    print(f"meta: {meta_path}")
    for r in report:
        print("  " + r)
    for m in missing:
        print(f"  {m}: NOT FOUND in humanoid mapping (map the eye bones in Rig -> Configure first)")

    if patched == 0:
        print("nothing to patch")
        return 2
    if args.dry_run:
        print("\n--dry-run: no files written")
        return 0

    with open(meta_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\nwrote {meta_path}")
    print("open/focus your Unity project to let it reimport")
    return 0


if __name__ == "__main__":
    sys.exit(main())