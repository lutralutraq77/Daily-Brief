# PyInstaller spec for the Daily Brief Windows release.
#
# Two executables from one analysis, sharing one payload:
#   DailyBrief.exe   console, for the CLI (run / status / check / sources / setup)
#   DailyBriefw.exe  windowless, for the scheduled 08:00 run -- a console app on a
#                    daily timer flashes a black window in the user's face.
#
# They differ only in the PE subsystem flag. As two onefile exes they each
# embedded a byte-identical 8.0 MiB payload -- python3xx.dll, libcrypto, the
# .pyd files and base_library.zip, downloaded twice. exclude_binaries=True plus
# a single COLLECT ships that once.
#
# The cost, and it is a real one: the exes now live in a folder next to an
# _internal\ directory they cannot start without. Copying DailyBrief.exe out on
# its own is a broken install, which onefile made impossible. READ-ME-FIRST.txt
# says so in as many words. If that hazard is ever judged worse than 5 MiB of
# download, going back to two onefile EXE(...) calls is a legitimate answer --
# but then record the duplication here rather than leaving it undocumented.
#
# toast.ps1 is bundled as data and found at runtime through
# platform_shim.resource_dir(), which resolves to sys._MEIPASS -- the _internal
# folder in this layout. Everything writable (config.json, briefs/, logs/) lives
# beside the .exe instead, because app_home() is the executable's parent
# directory, which is the folder COLLECT builds.

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
    #
    # The second group is reachable only down paths this app never executes:
    # concurrent.futures resolves ProcessPoolExecutor lazily in __getattr__ and
    # collect() uses ThreadPoolExecutor; statistics enters only through
    # random._test_generator; shutil/zipfile wrap `import bz2`/`import lzma` in
    # try/except and we call only shutil.which and shutil.copyfile; tarfile and
    # py_compile are late imports in functions nothing reaches; warnings imports
    # tracemalloc inside a try.
    #
    # The one to know about: `decimal` is imported by fractions, statistics and
    # _pylong. The first two go with it, but _pylong is imported by the
    # INTERPRETER when converting an int above ~4,300 digits to or from str.
    # Nothing here does that -- the biggest integers are timestamps and
    # Content-Length -- but a future collector parsing a huge numeric field
    # would get a bare ImportError from inside int().
    #
    # A wrong exclude here only shows up at runtime, inside whichever collector
    # needed it. Verify by running the built exe through run/status/check/sources,
    # not by reading the TOC.
    excludes=[
        "tkinter", "test", "unittest", "pydoc_data", "lib2to3", "distutils",
        "multiprocessing", "concurrent.futures.process", "xmlrpc",
        "statistics", "fractions", "decimal", "_pydecimal", "numbers",
        "bz2", "lzma", "tarfile", "py_compile", "tracemalloc",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

# exclude_binaries=True: the payload is not baked into either exe, it goes into
# the single COLLECT below.
exe_console = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
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
    [],
    exclude_binaries=True,
    name="DailyBriefw",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ROOT / "icon.ico"),
)

# One payload, two exes. name="DailyBrief" makes <distpath>\DailyBrief\, which
# is the folder the user keeps -- app_home() is the exe's parent, so config.json,
# briefs\ and logs\ land in it, and build.ps1 copies the example files there too.
coll = COLLECT(
    exe_console,
    exe_windowed,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="DailyBrief",
)
