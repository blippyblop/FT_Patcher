# FT Patcher

Gives a **custom/edited avatar FBX** the face-tracking blend shapes from the
Hash Edits FT files (Linne, Pawlygon, ...), without touching your original
character files. The main entry point is the packaged Windows GUI,
`FT-Patcher.exe` (see `FT-Patcher-win.zip`); a command-line version is also
provided for the rest of the platform.

## What you need

- **Windows** (the packaged GUI). The CLI scripts also run on macOS/Linux with
  Python 3 + Blender.
- **Blender** installed (any recent 3.x; the GUI detects common install
  locations and verifies the build before running). The GUI only uses Blender
  as a compute engine — it never opens the Blender UI.
- From the Hash Edits package, e.g. for Linne:
  - the **original character FBX** — the exact file the diff was built
    against (Linne: v1.0.3; v1.0.4 is byte-identical and also works). The
    tool verifies the base hash and tells you if you point it at the wrong
    version.
  - the **diff file**: `Hash's_Things/Linne/Patcher/Data/DiffFiles/Linne.hdiff`
    (and `LinneMeta.hdiff` for the meta patch).
- One or more **custom avatar FBX** files to patch — e.g. a retextured,
  resculpted or decimated version of the character.

## Quick start (GUI)

1. Extract `FT-Patcher-win.zip` and run `FT-Patcher.exe`.
2. **Blender** — an installed build is auto-detected and verified; pick or
   browse for a different one if needed.
3. **Diff file** — `.../Linne/Patcher/Data/DiffFiles/Linne.hdiff`.
4. **Original FBX** — the unpatched character FBX the diff was built on.
5. **Custom FBX file(s) to patch** — add one or more of your avatar FBX files.
6. **Mode**:
   - **Shape Key Transfer** (default) — resamples the new shape keys onto
     your mesh. Best when your edits to the shape-carrying mesh are mild.
     If the mesh topology differs (decimated/edited), the shapes are
     interpolated onto the new vertices after a best-fit alignment; the log
     reports how faithful that is and which shapes are "at risk" (they act on
     geometry that was removed, so that detail is lost).
   - **Head Swap** — replaces exactly the mesh(es) the diff adds shapes to
     (e.g. the head) with the good-sample mesh, all of its shape keys and
     vertex groups included, wearing *your* file's materials and keeping
     *your* file's mesh names. Nothing else in the file is touched. Use it
     when your edits to those meshes are too severe to resample onto (your
     edits to them are lost).
7. **Patch meta file** — if `<file>.fbx.meta` sits next to the FBX (as in a
   Unity project), also apply the text-based meta patch (eye-rotation limits).
8. **Force resample on custom mesh** — only shown when **Shape Key
   Transfer** is selected: apply the resample even if its safety check says
   the topology differs too much (best effort — shapes may warp or lose
   detail).
9. **Backup location** — a folder, preferably **outside your Unity project**,
   where a copy of each file is written before it is modified. Leave blank to
   get `file.fbx.bak` / `.bak2` next to each file instead.
10. Press **Dry run** first: it does all the matching/safety analysis and
    reports what would happen, without writing anything. Then **Run**.

Per custom FBX the tool: materializes the FT file from original + diff
(the bundled pure-Python patcher, byte-for-byte equivalent to hpatchz),
matches the shape-carrying mesh (by name, then by geometry), adds
the new shape keys (existing ones are preserved, duplicates skipped), and
re-exports **in place** — the file is only replaced after the new one is
produced and verified, so a failed run never clobbers your file. Your
original/base FBX is never modified. Before exporting, the tool renames
meshes/materials to their clean names (dropping `Name.001`/`Name.002`
suffixes that an earlier run of this tool may have left in the file, e.g.
`Body.002` → `Body`), so the exported file keeps your file's own mesh
names. When finished, reopen or refocus Unity so it reimports the files.

Only blend shapes are transplanted. Materials, textures, organizers and skin
definitions from the FT file are **not** carried over; the output is a
Blender re-export of *your* file.

## Troubleshooting

- **"topology differs too much for a reliable resample"** — your edits to the
  shape mesh are too far from the original. Either tick *Force resample on
  custom mesh* (accepting possible warping/detail loss) or use **Head Swap**
  instead.
- **"at-risk shapes" listed in the log** — those shapes strongly deform
  geometry that was removed from your mesh; they still get transplanted but
  part of their detail is lost.
- **"FT file is missing shapes from the original"** — the diff and the
  original FBX don't go together (wrong base version or a pre-patched file).
- **Meshes named `Something.001`/`.002` in the output** — a previous
  version of this tool (or a previous run) baked Blender's duplicate-name
  suffixes into the file. Just run the tool again: it detects the clean
  name behind the suffix and renames the mesh/material back to it before
  exporting.
  Point at the exact original the diff was built on.
- **No Blender found** — install Blender, or use *Browse...* next to the
  Blender field to pick the executable.
- **Blender add-on noise in the log** (CATS, ...): harmless — the detected
  Blender's installed add-ons load in background mode.

## Command line

Everything the GUI does can be done by hand. `ft_transplant.py` runs inside
Blender (it uses Blender's FBX I/O and shape-key API):

```
blender --background --python ft_transplant.py -- \
    --original "Linne.fbx" \
    --diff "Hash's_Things/Linne/Patcher/Data/DiffFiles/Linne.hdiff" \
    --custom "MyAvatar.fbx" \
    --out "MyAvatar_FT.fbx" [--mode transfer|swap] [--force-transfer] \
    [--mesh NAME] [--transfer-radius X] [--keep-anim] [--dry-run] [--verbose]
```

- `--ft PATH` — use a pre-built FT FBX instead of `--diff`.
- `--mesh NAME` — force the target mesh in the custom file (only when
  exactly one mesh gains shapes).
- `--hpatchz PATH` — legacy fallback: an external hpatchz executable, only
  used if the bundled pure-Python patcher (`ft_hpatchz.py`) can't be imported.
  Normally you don't need to set this.
- Exit codes: `0` ok, `1` fatal input/tool error, `2` nothing to transplant,
  `3` topology/identity mismatch on the custom mesh.

Other scripts:

- `ft_meta_patch.py` — standalone text patcher for the eye-limit lines in an
  FBX's `.meta` file (no Blender, no binary diffing):
  `python3 ft_meta_patch.py your.fbx [--dry-run]`.

## Building the Windows package

From a machine with Python 3 + PyInstaller:

```
python -m PyInstaller ft_patcher.spec --noconfirm
```

and share the resulting `dist/FT-Patcher` folder (or its zip). The package
bundles `ft_transplant.py` and the pure-Python patcher (`ft_hpatchz.py`); no
`hpatchz` binary is required, and only Blender itself is still needed at run
time. `ft_gui.py --selftest` builds the UI headlessly to verify the package.

## File layout

```
Tools/
  ft_gui.py            GUI front-end (packaged as FT-Patcher.exe)
  ft_transplant.py     the Blender script that does the FBX work
  ft_hpatchz.py        pure-Python HDiffPatch patch applier (no binary needed)
  ft_meta_patch.py     CLI: patch an FBX .meta file (text)
  ft_patcher.spec      PyInstaller spec for the Windows package
```