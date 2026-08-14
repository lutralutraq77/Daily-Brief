"""Where the brief lives, and how it reaches the user, on each platform.

DailyBrief began as Windows-only, but the collectors were always portable:
standard library in, HTML out. So the entire platform-specific surface is three
questions, and this module is the only place that answers them.

    where do writable files live?   app_home()
    where do bundled assets live?   resource_dir()
    how do we reach the user?       notify() and open_path()

Everything else -- sources.py, netlib.py, icslib.py, the HTML composer -- is
identical on Windows, Linux and Android, which is the point.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# "android" is set by the APK's Kotlin shell before importing anything else;
# there is no reliable way to detect Chaquopy from inside Python alone.
if os.environ.get("DAILYBRIEF_PLATFORM"):
    PLATFORM = os.environ["DAILYBRIEF_PLATFORM"].strip().lower()
elif os.name == "nt":
    PLATFORM = "windows"
elif sys.platform.startswith("linux"):
    PLATFORM = "linux"
elif sys.platform == "darwin":
    PLATFORM = "macos"
else:
    PLATFORM = "unknown"

IS_FROZEN = bool(getattr(sys, "frozen", False))

# Directories a distribution package owns and the user must not be sent to write into.
_SYSTEM_PREFIXES = ("/usr", "/opt", "/nix/store")


def _script_dir() -> Path:
    return Path(__file__).resolve().parent


def _under_system_prefix(path: Path) -> bool:
    text = str(path).replace("\\", "/")
    return any(text == p or text.startswith(p + "/") for p in _SYSTEM_PREFIXES)


def _writable(path: Path) -> bool:
    return os.access(str(path), os.W_OK)


def app_home() -> Path:
    """The writable directory holding config.json, briefs/, logs/ and state.json.

    A git checkout keeps everything beside the script, which is what makes the
    project self-contained and easy to inspect. A packaged install cannot do
    that -- /usr/lib is not the user's -- so those fall back to XDG.
    """
    override = os.environ.get("DAILYBRIEF_HOME")
    if override:
        return Path(override).expanduser()

    if IS_FROZEN:
        # PyInstaller: sit beside the .exe, not in the extracted temp dir, or the
        # user's config would vanish between runs.
        return Path(sys.executable).resolve().parent

    here = _script_dir()
    if PLATFORM in ("linux", "macos") and (_under_system_prefix(here) or not _writable(here)):
        root = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
        return Path(root) / "dailybrief"
    return here


def resource_dir() -> Path:
    """Where read-only bundled assets live (toast.ps1 and friends)."""
    if IS_FROZEN:
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return _script_dir()


def ensure_home() -> Path:
    home = app_home()
    home.mkdir(parents=True, exist_ok=True)
    return home


# --- reaching the user ------------------------------------------------------

CREATE_NO_WINDOW = 0x08000000


def notify(
    title: str,
    body: str,
    *,
    launch: str = "",
    attribution: str = "",
    icon: Path | None = None,
    log=lambda _msg: None,
) -> bool:
    """Show a desktop notification. False means it could not be shown, and the
    caller should say so rather than assume the user was told."""
    if PLATFORM == "windows":
        return _notify_windows(title, body, launch, attribution, log)
    if PLATFORM == "linux":
        return _notify_linux(title, body, icon, log)
    # Android posts its own notification from Kotlin, where the channel and the
    # tap target live; pretending to have done it here would be a lie.
    return False


def _notify_windows(title, body, launch, attribution, log) -> bool:
    script = resource_dir() / "toast.ps1"
    if not script.exists():
        log(f"WARN: {script.name} missing; cannot toast")
        return False
    cmd = [
        "powershell.exe", "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "Bypass", "-File", str(script),
        "-Title", title, "-Body", body, "-Aumid", "Local.DailyBrief",
    ]
    if launch:
        cmd += ["-Launch", launch]
    if attribution:
        cmd += ["-Attribution", attribution]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=45,
            creationflags=CREATE_NO_WINDOW,
        )
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if proc.returncode != 0 or "TOAST_FAILED" in out:
            log(f"WARN: toast failed: {out[:400]}")
            return False
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"WARN: toast could not run: {exc}")
        return False


def _notify_linux(title, body, icon, log) -> bool:
    cmd = ["notify-send", "--app-name=Daily Brief", "--urgency=normal"]
    if icon and Path(icon).exists():
        cmd.append(f"--icon={Path(icon).resolve()}")
    cmd += [title, body]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if proc.returncode != 0:
            log(f"WARN: notify-send failed: {(proc.stderr or '').strip()[:200]}")
            return False
        return True
    except FileNotFoundError:
        # Common on a headless or minimal install; not worth failing the run over.
        log("WARN: notify-send not found (install libnotify for desktop notifications)")
        return False
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"WARN: notify-send could not run: {exc}")
        return False


def open_path(target: Path, log=lambda _msg: None) -> bool:
    """Open a file with the desktop's default handler."""
    target = Path(target)
    try:
        if PLATFORM == "windows":
            os.startfile(str(target.resolve()))  # noqa: S606 - local file, default handler
            return True
        if PLATFORM == "linux":
            subprocess.Popen(
                ["xdg-open", str(target.resolve())],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return True
        if PLATFORM == "macos":
            subprocess.Popen(["open", str(target.resolve())])
            return True
    except FileNotFoundError:
        log("WARN: no handler found to open the brief (is xdg-utils installed?)")
        return False
    except OSError as exc:
        log(f"WARN: could not open the brief: {exc}")
        return False
    return False
