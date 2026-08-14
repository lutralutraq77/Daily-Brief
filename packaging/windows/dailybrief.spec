# PyInstaller spec for the Daily Brief Windows release.
#
# Two executables from one analysis:
#   DailyBrief.exe   console, for the CLI (run / status / check / sources / setup)
#   DailyBriefw.exe  windowless, for the scheduled 08:00 run -- a console app on a
#                    daily timer flashes a black window in the user's face.
#
# toast.ps1 is bundled as data and found at runtime through
# platform_shim.resource_dir(), which resolves to sys._MEIPASS when frozen.
# Everything writable (config.json, briefs/, logs/) lives beside the .exe
# instead, so the user's data survives the temp dir being torn down.

import pathlib

ROOT = pathlib.Path(SPECPATH).resolve().parents[1]

a = Analysis(
    [str(ROOT / "dailybrief.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[(str(ROOT / "toast.ps1"), ".")],
    # These are imported lazily inside functions, so give the analyser a nudge.
    # Keep in step with the top-level modules; a missing one only shows up at
    # runtime, inside whichever collector needed it.
    hiddenimports=[
        p.stem for p in ROOT.glob("*.py") if p.stem != "dailybrief"
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # The app is standard library only; excluding the heavy optional stdlib
    # trees keeps the download honest rather than shipping tkinter and pydoc.
    excludes=["tkinter", "test", "unittest", "pydoc_data", "lib2to3", "distutils"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe_console = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="DailyBrief",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    icon=str(ROOT / "icon.ico"),
)

exe_windowed = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="DailyBriefw",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ROOT / "icon.ico"),
)
