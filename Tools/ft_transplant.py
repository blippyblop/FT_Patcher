"""ft_transplant.py -- transplant face-tracking blend shapes onto a custom avatar FBX.

Runs inside Blender (the only robust, format-aware FBX reader/writer with
shape-key support; no pip dependencies -- Blender ships its own Python):

    blender --background --python ft_transplant.py -- \
        --original "Linne1.03.fbx" \
        --diff "Hash's_Things/Linne/Patcher/Data/DiffFiles/Linne.hdiff" \
        --custom MyAvatar.fbx \
        --out MyAvatar_FT.fbx

If you already have a pre-built FT FBX, pass it directly instead of --diff:

    blender --background --python ft_transplant.py -- \
        --original "Linne1.03.fbx" --ft "Linne FT.fbx" --custom MyAvatar.fbx

What it does:
  1. If --diff is given (and --ft is not), materialize the FT FBX from the
     original with the bundled pure-Python patcher (ft_hpatchz, byte-for-byte
     equivalent to HDiffPatch's hpatchz); an external hpatchz binary is only
     used as a legacy fallback if that module is unavailable.
  2. Load the custom FBX first, then the original and the FT FBX (Blender
     dedupes datablock names globally across all scenes, so the first file
     loaded keeps its original names -- and the custom file is the one that
     gets exported). The FT file must import cleanly and must contain every
     shape key of the original (sanity check that the diff was applied to
     the right base).
  3. Compute the per-mesh set of blend shapes (shape keys) ADDED by the FT
     patch, together with each shape's per-vertex offset payload. Only the
     meshes this diff touches are ever modified -- in swap mode exactly
     these meshes are replaced, nothing else.
  4. For each of those meshes, find the matching mesh in the custom file:
     by name (object or mesh-data name, case-insensitive
     fallback), then by geometry. Two ways to match:
       * exact: same vertex count and rest positions within --tolerance
         (default 0.05, centroid-aligned mean displacement normalized by
         radius) -- offsets are copied 1:1 by vertex index.
       * resample: for customised/decimated meshes (fewer or different
         verts), or files in a different scene scale/orientation. Any
         translation, uniform scale or rotation difference between the two
         files is first removed by a best-fit alignment, then each shape's
         per-vertex offset field is interpolated onto the custom mesh with
         distance-weighted nearest neighbours (radius: --transfer-radius in
         source units, default 2.5% of the source mesh radius). A
         safety analysis runs first: what fraction of the custom verts lies
         near the original geometry, what fraction of the original verts was
         removed, and for every shape how much of its displacement range
         lives on the removed verts. Shapes that strongly deform removed
         geometry are listed as "at risk" (that detail is lost on the
         decimated mesh).
  5. If the exact match fails and the resample safety check fails too, the
     tool aborts. Pass --force-transfer to apply the resample anyway (best
     effort -- shapes may warp or lose detail).
  6. Add the new shape keys to the matched meshes (existing shape keys are
     preserved; shape names that already exist are skipped).
  7. Before exporting, the result file's meshes and materials are renamed
     to their clean names (any .001/.002 dedup suffixes Blender attaches
     when several files are loaded at once -- including leftovers an
     earlier run of this tool may have baked into the file -- are dropped,
     e.g. "Body.002" goes back to "Body"), so the exported FBX uses your
     file's own mesh names.
  8. Export the custom scene to --out (default: <custom stem>_FT.fbx).

Only blend shapes are transplanted. Organizer empties, materials, textures,
skin definitions and re-export encoding differences from the FT file are not
carried over. The output is a Blender re-export of the custom file.

Options:
  --original PATH   base FBX the diff was built against (required)
  --ft PATH         pre-built FT FBX (alternative to --diff)
  --diff PATH       FT diff for --original (required if --ft is absent)
  --hpatchz PATH    legacy fallback: external hpatchz executable, only used if
                    the bundled pure-Python patcher (ft_hpatchz) is unavailable
  --custom PATH     custom avatar FBX (required)
  --out PATH        output FBX (default: <custom stem>_FT.fbx)
  --mesh NAME       force the target mesh in the custom file (single-mesh only)
--tolerance X     max normalized rest-position difference (default 0.05)
    --mode MODE       transfer (default): resample the new shapes onto the
                      custom mesh. swap: replace exactly the mesh(es) the diff
                      adds shapes to (e.g. the head) with the good-sample mesh
                      (shape keys and vertex groups included), re-expressed in
                      the target object's space and wearing the target's
                      materials; the exported file keeps the target file's mesh
                      names. Use swap when your edits to that mesh are too
                      severe to resample onto.
    --force-transfer  apply the resample even if its safety check fails
   --transfer-radius X  resample interpolation radius (default: 2.5% of the
                        source mesh's radius)
  --keep-anim       keep animations in the export (default: drop)
  --dry-run         stop after matching/reporting, do not export
  --verbose         print the full per-shape report

Exit codes: 0 ok, 1 fatal input/tool error, 2 nothing to transplant,
            3 topology/identity mismatch on the custom mesh.
"""

import argparse
import array
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import bpy
    from mathutils import Vector
except ImportError:
    print(
        "ft_transplant must run inside Blender:\n"
        "  blender --background --python ft_transplant.py -- --original ... --diff ... --custom ...",
        file=sys.stderr,
    )
    sys.exit(1)


def _import_patcher():
    """Import the bundled pure-Python patcher (``ft_hpatchz``) if available.

    Returns the module, or ``None`` if it cannot be imported (callers then fall
    back to an external ``hpatchz`` binary, if one happens to be present). In a
    PyInstaller onedir build ``ft_hpatchz.py`` sits next to this script, so the
    script's own directory is added to ``sys.path`` before the retry.
    """
    try:
        import ft_hpatchz  # noqa: F401  (sibling module)
        return ft_hpatchz
    except ImportError:
        pass
    try:
        script_dir = str(Path(__file__).resolve().parent)
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)
        import ft_hpatchz  # noqa: F401
        return ft_hpatchz
    except ImportError:
        return None


def parse_args():
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    else:
        argv = []
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--original")
    p.add_argument("--ft")
    p.add_argument("--diff")
    p.add_argument("--hpatchz")
    p.add_argument("--custom")
    p.add_argument("--out")
    p.add_argument("--mesh")
    p.add_argument("--tolerance", type=float, default=0.05)
    p.add_argument("--mode", choices=("transfer", "swap"), default="transfer")
    p.add_argument("--force-transfer", action="store_true")
    p.add_argument("--transfer-radius", type=float, default=None)
    p.add_argument("--keep-anim", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("-h", "--help", action="store_true")
    a = p.parse_args(argv)
    if a.help:
        p.print_help()
        sys.exit(0)
    if not a.original or not a.custom:
        p.print_help()
        sys.exit(1)
    if not a.ft and not a.diff:
        print("error: need --ft or --diff", file=sys.stderr)
        sys.exit(1)
    return a


def find_hpatchz(args):
    candidates = []
    if args.hpatchz:
        candidates.append(Path(args.hpatchz))
    env = os.environ.get("FTPATCH_HPATCHZ")
    if env:
        candidates.append(Path(env))
    which = shutil.which("hpatchz")
    if which:
        candidates.append(Path(which))
    script_dir = Path(__file__).resolve().parent
    bundle = script_dir.parent / "Linne" / "Patcher" / "Data" / "hdiff" / "hpatchz"
    if sys.platform.startswith("win"):
        candidates.append(bundle / "Windows" / "hpatchz.exe")
    elif sys.platform == "darwin":
        candidates.append(bundle / "Mac" / "hpatchz")
    else:
        candidates.append(bundle / "Linux" / "hpatchz")
    candidates.append(Path("/tmp/opencode/hpatchz/hpatchz"))
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return c
    print(
        "error: no usable hpatchz found (tried: %s)" % ", ".join(str(c) for c in candidates),
        file=sys.stderr,
    )
    sys.exit(1)


def materialize_ft(args, original):
    tmp = Path(tempfile.mkdtemp(prefix="ft_transplant_"))
    out = tmp / (original.stem + "_ft.fbx")
    diff = args.diff
    patcher = _import_patcher()
    if patcher is not None:
        print("[patch] pure-Python: %s + %s -> %s" % (original, diff, out))
        try:
            patcher.apply_patch_file(str(original), str(diff), str(out))
        except patcher.HdiffPatchError as e:
            print("error: patch failed: %s" % e, file=sys.stderr)
            sys.exit(1)
        except OSError as e:
            print("error: patch failed: %s" % e, file=sys.stderr)
            sys.exit(1)
    else:
        hpatchz = find_hpatchz(args)
        print("[hpatchz] %s" % hpatchz)
        print("[hpatchz] %s + %s -> %s" % (original, diff, out))
        r = subprocess.run([str(hpatchz), str(original), str(diff), str(out)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print("error: hpatchz failed:\n%s%s" % (r.stdout, r.stderr), file=sys.stderr)
            sys.exit(1)
    if not out.is_file() or out.stat().st_size == 0:
        print("error: patch produced no output -- refusing", file=sys.stderr)
        sys.exit(1)
    print("[patch] ok: %d bytes" % out.stat().st_size)
    return out


_cleaned = False


def load_fbx(path):
    # Each file gets its own scene so the previous files' datablocks stay
    # alive (a factory reset in between would purge them, leaving dangling
    # Object/Mesh references for later passes). The reset happens once, up
    # front, so the first file starts from a clean slate.
    global _cleaned
    if not _cleaned:
        bpy.ops.wm.read_factory_settings(use_empty=True)
        _cleaned = True
    sc = bpy.data.scenes.new("ft_%s" % Path(path).stem[:32])
    win = bpy.context.window_manager.windows[0]
    win.scene = sc
    bpy.ops.import_scene.fbx(filepath=str(path), bake_space_transform=False)
    sc.view_layers[0].update()
    return sc


def _logical_name(name):
    # Blender dedupes object/data names that collide *across all scenes*
    # (Body, Body.001, Body.002 ...). All files are loaded at once, so match
    # on the name with any dedup suffixes stripped (also covers copy names
    # like "Body.001.001" made by data-block copies).
    return re.sub(r"(\.\d{1,4})+$", "", name or "")


_IDNAME_TO_COLL = {"Object": "objects", "Mesh": "meshes", "Material": "materials",
                   "Armature": "armatures", "Action": "actions", "Image": "images",
                   "Camera": "cameras", "Light": "lights"}


def _idname_of(db):
    """The ID type name ("Object", "Mesh", "Material", ...) of a datablock.
    bl_idname is not exposed on every type in every Blender build
    (e.g. Object in 3.6), so bl_rna.identifier is the fallback."""
    idname = getattr(db, "bl_idname", None)
    if idname:
        return idname
    rna = getattr(db, "bl_rna", None)
    if rna is not None:
        return getattr(rna, "identifier", None)
    return None


def _try_rename(db, new_name):
    """Rename a datablock to new_name unless something already holds that
    name (or the datablock kind is unhandled). Returns True on rename."""
    if not new_name or db.name == new_name:
        return False
    coll = getattr(bpy.data, _IDNAME_TO_COLL.get(_idname_of(db) or "", ""), None)
    if coll is None or new_name in coll:
        return False
    db.name = new_name
    return True


def _scene_mats(sc):
    """The scene's distinct named materials (each reported once)."""
    seen = set()
    for obj in sc.objects:
        d = obj.data
        if d is None or not hasattr(d, "materials"):
            continue
        for m in d.materials:
            if m is not None and m.name and m.name not in seen:
                seen.add(m.name)
                yield m


def _prefix_scene(sc, prefix):
    """Give a read-only scene's datablocks reserved names (orig_Body,
    ft_Body, ...), freeing the clean names for the scene that gets
    exported."""
    for obj in sc.objects:
        if not obj.name.startswith(prefix):
            _try_rename(obj, prefix + _logical_name(obj.name))
        d = obj.data
        if d is not None and not d.name.startswith(prefix):
            _try_rename(d, prefix + _logical_name(d.name))
    for m in _scene_mats(sc):
        if not m.name.startswith(prefix):
            _try_rename(m, prefix + _logical_name(m.name))


def _normalize_scene_names(sc):
    """Rename the scene's objects, meshes and materials to their logical
    names (any Blender dedup suffixes dropped), so the exported file uses
    the clean original names -- e.g. a "Body.002" left behind by an earlier
    run goes back to "Body"."""
    for obj in sc.objects:
        _try_rename(obj, _logical_name(obj.name))
        d = obj.data
        if d is not None:
            _try_rename(d, _logical_name(d.name))
    for m in _scene_mats(sc):
        _try_rename(m, _logical_name(m.name))


def _purge_orphans():
    """Drop datablocks with no users (e.g. a mesh a swap replaced) so their
    stale names can't block the renames in _normalize_scene_names."""
    for name in ("meshes", "materials", "objects", "armatures", "actions",
                 "images", "cameras", "lights"):
        coll = getattr(bpy.data, name, None)
        if coll is None:
            continue
        for db in list(coll):
            if db.users == 0:
                coll.remove(db)


def mesh_inventory(scene):
    inv = {}
    for obj in scene.objects:
        if obj.type != "MESH" or obj.data is None:
            continue
        me = obj.data
        names = []
        if me.shape_keys is not None:
            names = [k.name for k in me.shape_keys.key_blocks if k.name != "Basis"]
        key = _logical_name(obj.name)
        if key in inv:
            key = obj.name  # genuine duplicate: keep both under raw names
        inv[key] = {
            "data_name": _logical_name(me.name),
            "raw_name": obj.name,
            "vcount": len(me.vertices),
            "shapes": names,
            "obj": obj,
        }
    return inv


def rest_flat(me, typ="f"):
    a = array.array(typ)
    for v in me.vertices:
        a.append(v.co.x)
        a.append(v.co.y)
        a.append(v.co.z)
    return a


def shape_offsets(obj, key_name):
    me = obj.data
    if me.shape_keys is None:
        return None
    kb = me.shape_keys.key_blocks.get(key_name)
    if kb is None:
        return None
    a = array.array("f")
    for i, v in enumerate(me.vertices):
        d = kb.data[i].co - v.co
        a.append(d.x)
        a.append(d.y)
        a.append(d.z)
    return a


def _vecs(rest, n):
    return [Vector((rest[3 * i], rest[3 * i + 1], rest[3 * i + 2])) for i in range(n)]


def _normalize(vecs):
    """Center and scale a point set to unit radius (removes any translation
    and uniform scale difference between files). Returns (norm_vecs, radius)."""
    n = len(vecs)
    if n == 0:
        return [], 0.0
    cx = sum(v.x for v in vecs) / n
    cy = sum(v.y for v in vecs) / n
    cz = sum(v.z for v in vecs) / n
    m = 0.0
    for v in vecs:
        dx = v.x - cx
        dy = v.y - cy
        dz = v.z - cz
        m = max(m, dx * dx + dy * dy + dz * dz)
    rad = m ** 0.5 or 1.0
    out = [Vector(((v.x - cx) / rad, (v.y - cy) / rad, (v.z - cz) / rad))
           for v in vecs]
    return out, rad


def _kabsch(src_pts, tgt_pts):
    """Rotation matrix R such that R @ p ~= q for the (centered) pairs
    (p in src_pts, q in tgt_pts). Returns an identity matrix when it cannot
    be computed (stub/unsupported environment or <3 pairs)."""
    from mathutils import Matrix
    pairs = list(zip(src_pts, tgt_pts))
    if len(pairs) < 3:
        return Matrix.Identity(3)
    try:
        H = Matrix((0.0, 0.0, 0.0,
                    0.0, 0.0, 0.0,
                    0.0, 0.0, 0.0))
        for p, q in pairs:
            H = H + Matrix(((p.x * q.x, p.x * q.y, p.x * q.z),
                            (p.y * q.x, p.y * q.y, p.y * q.z),
                            (p.z * q.x, p.z * q.y, p.z * q.z)))
        U, _S, Vt = H.SVD()  # H = U * S * Vt  (mathutils convention)
        D = Matrix((1.0, 0.0, 0.0,
                    0.0, 1.0, 0.0,
                    0.0, 0.0, 1.0 if (U.determinant() * Vt.determinant()) > 0
                    else -1.0))
        return Vt.inverted() @ D @ U.transposed()
    except Exception:
        return Matrix.Identity(3)


def _align_and_cover(src_norm, tgt_norm, radius):
    """Best-fit (Procrustes) rotation of tgt onto src -- both already
    centered/unit-scaled -- then nearest-neighbour coverage both ways, in
    the normalized space. Returns a stats dict; 'tgt_aligned' is the
    rotation that produced it."""
    kd_s = _build_kd_vecs(src_norm)
    rot_total = None
    tgt_aligned = tgt_norm
    for _ in range(2):
        pairs_s, pairs_t = [], []
        for v in tgt_aligned:
            res = _kd_res(kd_s.find(v))
            if res is not None and res[0] < 0.25:
                pairs_s.append(src_norm[res[1]])
                pairs_t.append(v)
        if len(pairs_s) >= 3:
            R = _kabsch(pairs_t, pairs_s)  # R @ q ~= p  (tgt -> src)
            rot_total = R if rot_total is None else (R @ rot_total)
            tgt_aligned = [rot_total @ v for v in tgt_norm]
        else:
            break
    kd_t = _build_kd_vecs(tgt_aligned)
    src_near = []
    for v in src_norm:
        res = _kd_res(kd_t.find(v))
        src_near.append(res[0] if res is not None else float("inf"))
    tgt_near = []
    for v in tgt_aligned:
        res = _kd_res(kd_s.find(v))
        tgt_near.append(res[0] if res is not None else float("inf"))
    t_mean, t_p95, t_max = coverage_stats(tgt_near)
    s_p95 = coverage_stats(src_near)[1]
    tgt_frac = (sum(1 for x in tgt_near if x <= radius) / len(tgt_near)
                if tgt_near else 0.0)
    removed_frac = (1.0 - (sum(1 for x in src_near if x <= radius)
                           / len(src_near))) if src_norm else 1.0
    return {"tgt_aligned": tgt_aligned, "src_near": src_near,
            "t_mean": t_mean, "t_p95": t_p95, "t_max": t_max,
            "s_p95": s_p95, "tgt_frac": tgt_frac, "removed_frac": removed_frac}


def _kd_insert(kd, v, i):
    # Blender renamed KDTree methods between versions: insert() in 3.x,
    # new() in some others; same for find_n()/find_k().
    if hasattr(kd, "insert"):
        kd.insert(v, i)
    else:
        kd.new(v, i)


def _kd_findn(kd, v, n):
    if hasattr(kd, "find_n"):
        return kd.find_n(v, n)
    return kd.find_k(v, n)


def _kd_res(r):
    """Decode a KDTree find()/find_n() result into (dist, index). Blender
    changed the element order between versions -- some builds return
    (co, index, dist), others (co, dist, index); the index is the int
    element, the distance the float one."""
    if r is None:
        return None
    a, b = r[1], r[2]
    if isinstance(a, int):
        return float(b), a
    return float(a), int(b)


def _build_kd_vecs(vecs):
    from mathutils.kdtree import KDTree
    kd = KDTree(len(vecs))
    for i, v in enumerate(vecs):
        _kd_insert(kd, v, i)
    kd.balance()
    return kd


def coverage_stats(distances):
    if not distances:
        return 0.0, 0.0, 0.0
    ds = sorted(distances)
    n = len(ds)
    return sum(ds) / n, ds[min(n - 1, int(n * 0.95))], ds[-1]


def resample_offsets(offs, kd_s, tgt_vecs, radius):
    """Interpolate the source per-vertex offset field onto the target verts
    using distance-weighted nearest neighbours (falls back to the single
    nearest source vertex when nothing lies within the radius)."""
    out = array.array("f")
    for v in tgt_vecs:
        hits = []
        for _t in _kd_findn(kd_s, v, 8):
            res = _kd_res(_t)
            if res is not None and res[0] < radius:
                hits.append((1.0 - res[0] / radius, res[1]))
        if hits:
            tw = sum(w for w, _ in hits)
            ox = sum(w * offs[3 * j] for w, j in hits)
            oy = sum(w * offs[3 * j + 1] for w, j in hits)
            oz = sum(w * offs[3 * j + 2] for w, j in hits)
            out.extend((ox / tw, oy / tw, oz / tw))
        else:
            res = _kd_res(kd_s.find(v))
            j = res[1] if res is not None else 0
            out.extend((offs[3 * j], offs[3 * j + 1], offs[3 * j + 2]))
    return out


def shape_risk(offs, removed_mask):
    """How much of a shape's total displacement range sits on removed verts.
    Returns (ratio, removed_max, shape_max)."""
    gmax = 0.0
    rmax = 0.0
    for i, rm in enumerate(removed_mask):
        x, y, z = offs[3 * i], offs[3 * i + 1], offs[3 * i + 2]
        m = (x * x + y * y + z * z) ** 0.5
        if m > gmax:
            gmax = m
        if rm and m > rmax:
            rmax = m
    if gmax <= 0.0:
        return 0.0, 0.0, 0.0
    return rmax / gmax, rmax, gmax


def match_materials(src_names, tgt_names):
    """For each source material slot, pick the target slot to wear: exact
    name match first, then a match ignoring Blender's cross-scene dedup
    suffixes (.001 ...), then the same index, then the first non-empty
    target slot. Returns [(src_name, tgt_index_or_None), ...]."""
    first = None
    for i, n in enumerate(tgt_names):
        if n:
            first = i
            break
    picks = []
    for i, sn in enumerate(src_names):
        j = None
        if sn:
            for j2, tn in enumerate(tgt_names):
                if tn == sn:
                    j = j2
                    break
            if j is None:
                ls = _logical_name(sn)
                for j2, tn in enumerate(tgt_names):
                    if tn and _logical_name(tn) == ls:
                        j = j2
                        break
        if j is None and i < len(tgt_names) and tgt_names[i]:
            j = i
        if j is None:
            j = first
        picks.append((sn, j))
    return picks


def swap_into(src_obj, tgt_obj, verbose=False):
    """Replace tgt_obj's mesh with a copy of src_obj's mesh (shape keys and
    vertex groups included), re-expressed in the target object's space and
    wearing the target's materials. Returns (new_mesh, mapping)."""
    src_me = src_obj.data
    tgt_me = tgt_obj.data
    new_me = src_me.copy()
    M = tgt_obj.matrix_world @ src_obj.matrix_world.inverted()
    if not M.is_identity:
        for v in new_me.vertices:
            v.co = M @ v.co
        if new_me.shape_keys is not None:
            for kb in new_me.shape_keys.key_blocks:
                for pv in kb.data:
                    pv.co = M @ pv.co
        print("      note: re-expressed swapped mesh in the target object's"
              " space (object transforms differed)")
    src_names = [m.name if m else "" for m in src_me.materials]
    tgt_names = [m.name if m else "" for m in tgt_me.materials]
    mapping = match_materials(src_names, tgt_names)
    if any(j is not None for _s, j in mapping):
        new_me.materials.clear()
        for _s, j in mapping:
            if j is not None:
                new_me.materials.append(tgt_me.materials[j])
            else:
                new_me.materials.append(None)
        tgt_mat_names = [tgt_names[j] if j is not None else "(none)"
                         for _s, j in mapping]
        if verbose or len(mapping) <= 4:
            for (s, _j), t in zip(mapping, tgt_mat_names):
                print("      material: %-24s -> %s" % (s or "(none)", t))
    tgt_obj.data = new_me
    return new_me, mapping


def similarity(rest_a, rest_b, n):
    ca = (sum(rest_a[0::3]) / n, sum(rest_a[1::3]) / n, sum(rest_a[2::3]) / n)
    cb = (sum(rest_b[0::3]) / n, sum(rest_b[1::3]) / n, sum(rest_b[2::3]) / n)
    tot = 0.0
    scale = 0.0
    for i in range(n):
        ax = rest_a[3 * i] - ca[0]
        ay = rest_a[3 * i + 1] - ca[1]
        az = rest_a[3 * i + 2] - ca[2]
        scale += (ax * ax + ay * ay + az * az) ** 0.5
        tot += (abs(rest_b[3 * i] - cb[0] - ax)
                + abs(rest_b[3 * i + 1] - cb[1] - ay)
                + abs(rest_b[3 * i + 2] - cb[2] - az))
    if scale <= 0:
        return 0.0
    return (tot / (n * 3.0)) / (scale / n)


def _scan_transfer(src_norm, src_rad, radius, custom_inv, cands_named):
    """Alignment scan over the custom meshes: name matches first, then every
    other mesh. Returns (chosen, best) -- chosen is the first candidate that
    passes the resample safety gate, best the closest one regardless."""
    cands = list(cands_named)
    for n in custom_inv:
        if n not in cands:
            cands.append(n)
    chosen = best = None
    for cn in cands:
        tgt_vecs = _vecs(rest_flat(custom_inv[cn]["obj"].data),
                         custom_inv[cn]["vcount"])
        tgt_norm, tgt_rad = _normalize(tgt_vecs)
        if not tgt_norm:
            continue
        cov = _align_and_cover(src_norm, tgt_norm, radius)
        entry = {
            "cn": cn, "tgt_vecs": tgt_vecs, "tgt_norm": cov.pop("tgt_aligned"),
            "radius": radius, "src_rad": src_rad, "tgt_rad": tgt_rad,
            "tgt_frac": cov["tgt_frac"], "removed_frac": cov["removed_frac"],
            "t_mean": cov["t_mean"], "t_p95": cov["t_p95"],
            "t_max": cov["t_max"], "s_p95": cov["s_p95"],
            "src_near": cov["src_near"],
            "ok": (cov["tgt_frac"] >= 0.98) and (cov["removed_frac"] <= 0.5),
            "score": cov["t_p95"],
        }
        if entry["ok"]:
            chosen = entry
            break
        if best is None or entry["score"] < best["score"]:
            best = entry
    return chosen, best


def _fbx_unit_scale_factor(data):
    """Read UnitScaleFactor from a binary FBX (6.1/7.x alike): the double
    stored right after the PS "UnitScaleFactor" entry. Returns None if not
    found. 1.0 == centimeters, 100.0 == meters."""
    L = len(b"UnitScaleFactor")
    off = 0
    while True:
        i = data.find(b"UnitScaleFactor", off)
        if i < 0:
            return None
        off = i + 1
        if data[max(0, i - 8):i] == b"Original":
            continue
        d = i + L + 27
        if d + 9 > len(data) or data[d] != 0x44:  # 0x44 == 'D'
            continue
        return struct.unpack_from("<d", data, d + 1)[0]


def _fbx_mark_meters(data):
    """Rewrite UnitScaleFactor + OriginalUnitScaleFactor to 100.0 (meters).
    Only touches a file that currently declares centimeters (~1.0)."""
    cur = _fbx_unit_scale_factor(data)
    if cur is None or abs(cur - 1.0) > 0.01:
        return data
    out = bytearray(data)
    new = struct.pack("<d", 100.0)
    for name in (b"UnitScaleFactor", b"OriginalUnitScaleFactor"):
        L = len(name)
        off = 0
        while True:
            i = out.find(name, off)
            if i < 0:
                break
            off = i + 1
            if name == b"UnitScaleFactor" and out[max(0, i - 8):i] == b"Original":
                continue
            d = i + L + 27
            if d + 9 > len(out) or out[d] != 0x44:
                continue
            out[d + 1:d + 9] = new
    return bytes(out)


def main():
    args = parse_args()
    original = Path(args.original).expanduser().resolve()
    custom_path = Path(args.custom).expanduser().resolve()
    if not original.is_file():
        print("error: original not found: %s" % original, file=sys.stderr)
        sys.exit(1)
    if not custom_path.is_file():
        print("error: custom not found: %s" % custom_path, file=sys.stderr)
        sys.exit(1)
    out_path = (Path(args.out).expanduser().resolve() if args.out
                else custom_path.with_name(custom_path.stem + "_FT.fbx"))

    print("== ft_transplant ==")
    print("original : %s" % original)
    print("custom   : %s" % custom_path)
    print("out      : %s" % out_path)
    # FBX "UnitScaleFactor": 1.0 = centimeters, 100.0 = meters. Blender's
    # FBX exporter writes a cm-convention file for shape-key scenes no matter
    # what, so a meter-scale source (e.g. the schuul variant) would come out
    # 100x too large. Detect the source convention and export with
    # global_scale=0.01 so every node transform lands on the source's own
    # values, then re-tag the output file as meters afterwards.
    src_usf = _fbx_unit_scale_factor(custom_path.read_bytes())
    src_meters = src_usf is not None and src_usf > 1.0
    print("source units: %s" % ("meters" if src_meters
                                else "centimeters" if src_usf is not None
                                else "unknown (treating as cm)"))

    if args.ft:
        ft_path = Path(args.ft).expanduser().resolve()
        if not ft_path.is_file():
            print("error: --ft not found: %s" % ft_path, file=sys.stderr)
            sys.exit(1)
    else:
        diff_path = Path(args.diff).expanduser().resolve()
        if not diff_path.is_file():
            print("error: --diff not found: %s" % diff_path, file=sys.stderr)
            sys.exit(1)
        ft_path = materialize_ft(args, original)
    print("ft       : %s" % ft_path)

    # The custom file is loaded FIRST: Blender dedupes datablock names
    # globally across all scenes, so the first file loaded keeps its original
    # names while later ones get .001/.002 suffixes. The custom file is the
    # one that is exported, so it must keep its own names; the original and
    # FT files are only read, and all matching below is suffix-insensitive
    # (_logical_name).
    print("\n[1/5] loading custom (first, so the export keeps its original names) ...")
    custom_scene = load_fbx(custom_path)
    custom_inv = mesh_inventory(custom_scene)
    print("      %d meshes" % len(custom_inv))

    print("[2/5] loading original ...")
    orig_scene = load_fbx(original)
    orig_inv = mesh_inventory(orig_scene)
    n_orig_shapes = sum(len(m["shapes"]) for m in orig_inv.values())
    print("      %d meshes, %d shape keys" % (len(orig_inv), n_orig_shapes))

    print("[3/5] loading FT file ...")
    ft_scene = load_fbx(ft_path)
    ft_inv = mesh_inventory(ft_scene)
    n_ft_shapes = sum(len(m["shapes"]) for m in ft_inv.values())
    print("      %d meshes, %d shape keys" % (len(ft_inv), n_ft_shapes))

    missing = []
    for name, m in orig_inv.items():
        ftm = ft_inv.get(name)
        if ftm is None:
            if m["shapes"]:
                missing.append("%s (mesh vanished)" % name)
            continue
        fset = set(ftm["shapes"])
        for s in m["shapes"]:
            if s not in fset:
                missing.append("%s / %s" % (name, s))
    if missing:
        print("error: FT file is missing shapes from the original -- wrong base"
              " or corrupt patch:\n  " + "\n  ".join(missing[:20]), file=sys.stderr)
        sys.exit(1)

    delta = []
    for name, m in ft_inv.items():
        o = orig_inv.get(name)
        oset = set(o["shapes"]) if o else set()
        added = [s for s in m["shapes"] if s not in oset]
        if not added:
            continue
        if args.mode == "swap":
            # swap replaces the whole mesh; no per-vertex payloads needed
            payload = {s: None for s in added}
        else:
            payload = {}
            for s in added:
                off = shape_offsets(m["obj"], s)
                if off is None or len(off) != 3 * m["vcount"]:
                    print("error: could not read offset payload for %s / %s" % (name, s),
                          file=sys.stderr)
                    sys.exit(1)
                payload[s] = off
        delta.append({
            "name": name,
            "new_in_ft": o is None,
            "vcount": m["vcount"],
            "rest": rest_flat(m["obj"].data),
            "payload": payload,
        })
    if not delta:
        print("\nno new blend shapes found in the FT file -- nothing to transplant.")
        sys.exit(2)

    print("\n[4/5] delta: %d mesh(es) touched by the diff, %d new shape(s)"
          " (only these meshes are modified):"
          % (len(delta), sum(len(d["payload"]) for d in delta)))
    for d in delta:
        tag = " (mesh only in FT file)" if d["new_in_ft"] else ""
        shapes = list(d["payload"])
        shown = shapes if (args.verbose or len(shapes) <= 12) else shapes[:12]
        print("      %s%s [%d verts]: +%d shapes: %s%s" % (
            d["name"], tag, d["vcount"], len(shapes),
            ", ".join(shown),
            " (+%d more, --verbose)" % (len(shapes) - 12) if not args.verbose and len(shapes) > 12 else ""))

    print("[5/5] matching into the custom file ...")

    results = []
    for d in delta:
        cands_named = [n for n in custom_inv
                       if n == d["name"] or custom_inv[n]["data_name"] == d["name"]]
        if not cands_named:
            cands_named = [n for n in custom_inv
                           if n.lower() == d["name"].lower()
                           or custom_inv[n]["data_name"].lower() == d["name"].lower()]
        if args.mesh:
            if len(delta) != 1:
                print("error: --mesh can only be used when exactly one mesh gains shapes"
                      " (got %d)" % len(delta), file=sys.stderr)
                sys.exit(1)
            hit = [n for n in custom_inv
                   if n.lower() == args.mesh.lower()
                   or custom_inv[n]["data_name"].lower() == args.mesh.lower()]
            if not hit:
                print("error: --mesh %r not found in custom file" % args.mesh,
                      file=sys.stderr)
                sys.exit(1)
            cands_named = hit

        if args.mode == "swap":
            # head swap: replace the target mesh wholesale with the
            # good-sample mesh. A name match is enough (no topology
            # requirements); otherwise take the best geometric candidate.
            cn = cands_named[0] if cands_named else None
            if cn is None:
                src_norm, src_rad = _normalize(_vecs(d["rest"], d["vcount"]))
                if not src_norm:
                    print("error: source mesh %s has no usable geometry" % d["name"],
                          file=sys.stderr)
                    sys.exit(3)
                radius = (args.transfer_radius / src_rad
                          if (args.transfer_radius is not None and src_rad > 0)
                          else 0.025)
                _chosen, best = _scan_transfer(src_norm, src_rad, radius, custom_inv,
                                               list(custom_inv))
                if best is not None and best["t_p95"] <= 0.5:
                    cn = best["cn"]
                    print("      swap: %s -> %r (geometric match, target->source"
                          " p95 %.3f in unit space)"
                          % (d["name"], cn, best["t_p95"]))
                elif d["new_in_ft"]:
                    print("warn: %s exists only in the FT file and no similar mesh"
                          " was found in the custom file -- nothing to swap into"
                          % d["name"])
                    results.append({"d": d, "cn": None, "mode": "skip", "xfer": None})
                    continue
                else:
                    print("error: no mesh in the custom file looks like %s"
                          % d["name"], file=sys.stderr)
                    print("hint: use --mesh NAME to name the target mesh explicitly",
                          file=sys.stderr)
                    sys.exit(3)
            else:
                print("      swap: %s -> %r (name match)" % (d["name"], cn))
            print("      swap: replace custom mesh (%d verts, %d shapes) with the"
                  " good-sample mesh (%d verts, %d shapes); keeps the target's"
                  " materials" % (
                      custom_inv[cn]["vcount"], len(custom_inv[cn]["shapes"]),
                      d["vcount"], len(ft_inv[d["name"]]["shapes"])))
            results.append({"d": d, "cn": cn, "mode": "swap", "xfer": None})
            continue

        # pass 1: classic exact-topology match (same name, same vertex count,
        # rest positions within --tolerance)
        best_exact = None
        for cn in cands_named:
            if custom_inv[cn]["vcount"] != d["vcount"]:
                continue
            rest_c = rest_flat(custom_inv[cn]["obj"].data)
            sim = similarity(d["rest"], rest_c, d["vcount"])
            if best_exact is None or sim < best_exact[1]:
                best_exact = (cn, sim)
        if best_exact is not None and best_exact[1] <= args.tolerance:
            cn, sim = best_exact
            print("      match: %s -> %r (exact topology, similarity %.4f)"
                  % (d["name"], cn, sim))
            results.append({"d": d, "cn": cn, "mode": "exact", "xfer": None})
            continue
        if best_exact is not None and not args.force_transfer:
            cn, sim = best_exact
            print("error: rest positions of %r (custom) vs %r (FT) differ by %.3f of the"
                  " mesh radius (tolerance %.3f) -- topology does not match; offsets"
                  " cannot be transplanted." % (cn, d["name"], sim, args.tolerance),
                  file=sys.stderr)
            print("hint: the mesh may have been decimated/edited; rerun with"
                  " --force-transfer to attempt a best-effort resample",
                  file=sys.stderr)
            sys.exit(3)
        if best_exact is not None:
            print("      %s -> %r similarity %.4f > tolerance; --force-transfer:"
                  " attempting resample" % (d["name"], best_exact[0], best_exact[1]))

        # pass 2: resample/transfer -- for decimated, edited or renamed
        # meshes. Candidates: name matches first, then every other mesh.
        cands = list(cands_named)
        for n in custom_inv:
            if n not in cands:
                cands.append(n)
        if not cands:
            if d["new_in_ft"]:
                print("warn: %s exists only in the FT file; no matching mesh in the"
                      " custom file -- skipping %d shape(s)"
                      % (d["name"], len(d["payload"])))
                results.append({"d": d, "cn": None, "mode": "skip", "xfer": None})
                continue
            print("error: no candidate mesh in the custom file for %s" % d["name"],
                  file=sys.stderr)
            sys.exit(3)

        src_norm, src_rad = _normalize(_vecs(d["rest"], d["vcount"]))
        if not src_norm:
            print("error: source mesh %s has no usable geometry" % d["name"],
                  file=sys.stderr)
            sys.exit(3)
        radius = (args.transfer_radius / src_rad
                  if (args.transfer_radius is not None and src_rad > 0)
                  else 0.025)
        chosen, best = _scan_transfer(src_norm, src_rad, radius, custom_inv, cands_named)
        if chosen is None and best is None:
            print("error: no candidate mesh with usable geometry in the custom"
                  " file for %s" % d["name"], file=sys.stderr)
            sys.exit(3)
        x = chosen if chosen is not None else best
        scale = x["tgt_rad"] / x["src_rad"] if x["src_rad"] > 0 else 0.0
        if x["ok"]:
            print("      match: %s -> %r (resample: %.1f%% of target verts near"
                  " source after best-fit alignment; %.1f%% of source verts"
                  " removed; file scale %.3fx)" % (
                      d["name"], x["cn"], 100 * x["tgt_frac"],
                      100 * x["removed_frac"], scale))
        else:
            print("      match: %s -> %r (resample check FAILED: only %.1f%% of"
                  " target verts near source after alignment; target->source"
                  " nearest mean %.4f / p95 %.4f / max %.4f in unit space;"
                  " file scale %.3fx)" % (
                      d["name"], x["cn"], 100 * x["tgt_frac"],
                      x["t_mean"], x["t_p95"], x["t_max"], scale))
            if not args.force_transfer:
                print("error: topology differs too much for a reliable resample;"
                      " aborting", file=sys.stderr)
                print("hint: rerun with --force-transfer to apply anyway (shapes may"
                      " warp or lose detail)", file=sys.stderr)
                sys.exit(3)
            print("      note: --force-transfer: applying anyway")
        x["kd_s"] = _build_kd_vecs(src_norm)
        x["removed_mask"] = [dn > x["radius"] for dn in x["src_near"]]
        at_risk = []
        for s, offs in d["payload"].items():
            ratio, rmax, gmax = shape_risk(offs, x["removed_mask"])
            if ratio > 0.25:
                at_risk.append((s, ratio, rmax, gmax))
        if at_risk:
            print("      at-risk shapes (move removed verts by >25% of the shape's"
                  " range; expect detail loss where they act):")
            for s, ratio, rmax, gmax in at_risk:
                print("        %-30s removed=%.4f  shape range=%.4f  (%.0f%%)"
                      % (s, rmax, gmax, 100 * ratio))
            print("        (%d other shape(s) stay on the kept geometry)"
                  % (len(d["payload"]) - len(at_risk)))
        elif d["payload"]:
            print("      all %d shape(s) stay on the kept geometry -- resample"
                  " should be faithful" % len(d["payload"]))
        results.append({"d": d, "cn": x["cn"], "mode": "transfer", "xfer": x})

    to_apply = [r for r in results if r["mode"] in ("exact", "transfer", "swap")]
    if not to_apply:
        print("\nno mesh could be matched -- nothing transplanted.")
        sys.exit(3)

    if args.dry_run:
        sw = [r for r in to_apply if r["mode"] == "swap"]
        tr = [r for r in to_apply if r["mode"] != "swap"]
        if sw:
            print("\ndry run: would swap %d mesh(es) to the good sample (%s);"
                  " keeps each target's materials and mesh names; not exporting."
                  % (len(sw), ", ".join(r["cn"] for r in sw)))
        if tr:
            print("dry run: would add %d shape key(s) across %d mesh(es); not"
                  " exporting." % (sum(len(r["d"]["payload"]) for r in tr), len(tr)))
        sys.exit(0)

    total_added = 0
    total_skipped = 0
    total_swapped = 0
    for r in to_apply:
        d, cn = r["d"], r["cn"]
        xfer = r["xfer"]
        if r["mode"] == "swap":
            src_obj = ft_inv[d["name"]]["obj"]
            swap_into(src_obj, custom_inv[cn]["obj"], verbose=args.verbose)
            total_swapped += 1
            continue
        obj = custom_inv[cn]["obj"]
        me = obj.data
        if me.shape_keys is None:
            obj.shape_key_add(name="Basis", from_mix=False)
        existing = {k.name for k in me.shape_keys.key_blocks}
        added_here = 0
        if xfer is not None:
            n = len(xfer["tgt_vecs"])
        else:
            n = len(me.vertices)
        for s, offs in d["payload"].items():
            if s in existing:
                total_skipped += 1
                continue
            key = obj.shape_key_add(name=s, from_mix=False)
            if xfer is not None:
                src_rad = xfer["src_rad"]
                offs_n = (array.array("f", (v / src_rad for v in offs))
                          if src_rad > 0 else offs)
                offs_t = resample_offsets(offs_n, xfer["kd_s"], xfer["tgt_norm"],
                                          xfer["radius"])
                k = xfer["tgt_rad"] or 1.0
                for i in range(n):
                    b = xfer["tgt_vecs"][i]
                    key.data[i].co = Vector((b.x + offs_t[3 * i] * k,
                                             b.y + offs_t[3 * i + 1] * k,
                                             b.z + offs_t[3 * i + 2] * k))
            else:
                for i in range(n):
                    b = me.vertices[i].co
                    key.data[i].co = Vector((b.x + offs[3 * i],
                                             b.y + offs[3 * i + 1],
                                             b.z + offs[3 * i + 2]))
            key.value = 0.0
            total_added += 1
            added_here += 1
        how = "resampled" if xfer is not None else "applied"
        print("      apply: %r: +%d shape key(s) (%d pre-existing skipped), %s on %d vertices"
              % (cn, added_here, len(d["payload"]) - added_here, how, n))

    # Blender renamed FBX exporter options between versions (use_anim ->
    # bake_anim in 3.6+); keep only the kwargs this build accepts.
    # export the custom (result) scene. First free the clean names: the
    # read-only original/FT scenes (loaded after the custom one) hold the
    # .001/.002-deduped copies, and any datablocks a swap replaced are
    # orphaned -- move/purge those, then rename the result scene's
    # datablocks to their clean names (this also repairs names that an
    # earlier run baked into the file).
    _prefix_scene(orig_scene, "orig_")
    _prefix_scene(ft_scene, "ft_")
    _purge_orphans()
    _normalize_scene_names(custom_scene)
    win = bpy.context.window_manager.windows[0]
    win.scene = custom_scene
    custom_scene.view_layers[0].update()
    op = bpy.ops.export_scene.fbx
    valid = set(op.get_rna_type().properties.keys())
    kw = dict(filepath=str(out_path),
              use_mesh_modifiers=False,
              bake_space_transform=False,
              path_mode="AUTO",
              global_scale=0.01 if src_meters else 1.0)
    kw["use_anim" if "use_anim" in valid else "bake_anim"] = args.keep_anim
    op(**{k: v for k, v in kw.items() if k in valid})
    size = out_path.stat().st_size if out_path.is_file() else -1
    print("\nexported: %s (%d bytes)" % (out_path, size))
    if size <= 0:
        print("error: export produced no file", file=sys.stderr)
        sys.exit(1)
    if src_meters:
        # The exporter always tags shape-key exports as centimeters; the
        # 0.01 global scale already put every value on the meter grid, so
        # just re-tag the unit declaration to match the source file.
        data = out_path.read_bytes()
        out_usf = _fbx_unit_scale_factor(data)
        if out_usf is not None and abs(out_usf - 1.0) < 0.01:
            out_path.write_bytes(_fbx_mark_meters(data))
            print("unit fix: retagged export centimeters -> meters (source is meter-scale)")
        elif out_usf is not None and abs(out_usf - 100.0) < 1.0:
            pass  # exporter already wrote a meters-convention file
        else:
            print("warning: export declared UnitScaleFactor=%s; expected ~1.0 or ~100.0"
                  % out_usf, file=sys.stderr)
    if total_swapped:
        swapped_names = ", ".join(r["cn"] for r in to_apply if r["mode"] == "swap")
        print("OK: %d mesh(es) swapped to the good sample (%s); %d shape key(s)"
              " transplanted, %d skipped as pre-existing."
              % (total_swapped, swapped_names, total_added, total_skipped))
    else:
        print("OK: %d shape key(s) transplanted, %d skipped as pre-existing."
              % (total_added, total_skipped))


if __name__ == "__main__":
    # Blender exits 0 even when a background script raises, so surface
    # unexpected failures as a real non-zero exit code.
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("error: unexpected failure: %r" % (e,), file=sys.stderr)
        sys.exit(1)