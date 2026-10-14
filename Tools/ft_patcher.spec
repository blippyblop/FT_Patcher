# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the FT Patcher GUI (onedir build -> share the whole folder).
# Build on the target OS:  python -m PyInstaller ft_patcher.spec --noconfirm
import os
import sys

# PyInstaller >= 6.22 execs the spec with no __file__ and an empty sys.argv,
# so resolution has a fallback chain: __file__ -> FT_SPEC_DIR env (set by the
# build scripts) -> spec path in argv -> cwd.
def _spec_dir():
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:
        pass
    env = os.environ.get("FT_SPEC_DIR")
    if env and os.path.isdir(env):
        return os.path.abspath(env)
    for arg in sys.argv[1:]:
        if arg.lower().endswith(".spec") and os.path.isfile(arg):
            return os.path.dirname(os.path.abspath(arg))
    if os.path.isfile("ft_patcher.spec"):
        return os.getcwd()
    raise SystemExit("ft_patcher.spec: cannot locate spec directory")

SPEC_DIR = _spec_dir()

# The pure-Python patcher (ft_hpatchz.py) is bundled next to ft_transplant.py;
# no platform hpatchz binary is required anymore. Paths are anchored to the
# spec's directory so the build does not depend on the current working dir.
datas = [(os.path.join(SPEC_DIR, "ft_transplant.py"), "."),
         (os.path.join(SPEC_DIR, "ft_hpatchz.py"), ".")]

a = Analysis(
    [os.path.join(SPEC_DIR, "ft_gui.py")],
    pathex=[SPEC_DIR],
    datas=datas,
    hiddenimports=["ft_meta_patch"],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="FT-Patcher",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="FT-Patcher",
)