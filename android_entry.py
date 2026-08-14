"""Entry points for the Android build.

The APK embeds CPython (Chaquopy) and runs exactly the collectors the desktop
builds run -- sources.py, netlib.py, icslib.py and the HTML composer, byte for
byte. Kotlin owns the things Python has no business doing on Android: the UI,
the schedule, and the notification.

So this module is deliberately thin. It points the app at its data directory,
runs a brief, and hands back JSON the UI can render. Everything it returns is
a fact about what happened, never a guess.
"""

from __future__ import annotations

import json
import os
import traceback
import types

_configured = False


def configure(home: str) -> str:
    """Point the app at its data directory. Must run before dailybrief imports.

    dailybrief resolves BASE once at import time, so the environment has to be
    right first -- calling this afterwards would silently have no effect.
    """
    global _configured
    os.environ["DAILYBRIEF_HOME"] = home
    os.environ["DAILYBRIEF_PLATFORM"] = "android"
    os.makedirs(home, exist_ok=True)
    _configured = True
    return home


def _app(home: str):
    if not _configured:
        configure(home)
    import dailybrief
    return dailybrief


def seed_config(home: str) -> bool:
    """Create an empty config.json if there is not one yet.

    Empty is genuinely enough: load_config() merges DEFAULT_CONFIG underneath,
    so the feeds, sections and units all come from the same defaults the desktop
    builds use. Shipping a copy of config.example.json in the APK would only
    create a second source of truth to drift from.

    Returns True when a config was created, False when one already existed.
    """
    configure(home)
    target = os.path.join(home, "config.json")
    if os.path.exists(target):
        return False
    with open(target, "w", encoding="utf-8") as fh:
        json.dump({"location": {"city": "", "country": ""}}, fh, indent=2)
    return True


def set_location(home: str, city: str, country: str = "") -> str:
    """Set the configured city, clearing any coordinates resolved from the old one."""
    configure(home)
    path = os.path.join(home, "config.json")
    with open(path, encoding="utf-8-sig") as fh:
        cfg = json.load(fh)
    loc = cfg.setdefault("location", {})
    loc["city"] = city.strip()
    loc["country"] = country.strip()
    # Stale coordinates would quietly win over the new city.
    loc["latitude"] = None
    loc["longitude"] = None
    loc["label"] = ""
    loc["resolved_from"] = ""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    return json.dumps({"ok": True, "city": loc["city"]})


def status(home: str) -> str:
    """What the UI needs to decide what to show, as JSON."""
    configure(home)
    briefs = os.path.join(home, "briefs")
    latest = os.path.join(briefs, "latest.html")
    city = ""
    configured = os.path.exists(os.path.join(home, "config.json"))
    if configured:
        try:
            with open(os.path.join(home, "config.json"), encoding="utf-8-sig") as fh:
                city = (json.load(fh).get("location") or {}).get("city") or ""
        except (OSError, ValueError):
            configured = False
    return json.dumps({
        "configured": configured,
        "city": city,
        "has_brief": os.path.exists(latest),
        "latest": latest if os.path.exists(latest) else "",
        "generated_at": os.path.getmtime(latest) if os.path.exists(latest) else 0,
    })


def run_brief(home: str, force: bool = True) -> str:
    """Generate today's brief. Returns JSON describing what actually happened."""
    try:
        dailybrief = _app(home)
    except Exception:
        return json.dumps({"ok": False, "error": "could not start: " + traceback.format_exc(limit=3)})

    try:
        args = types.SimpleNamespace(force=force, open=False)
        code = dailybrief.cmd_run(args)
        latest = str(dailybrief.BRIEFS_DIR / "latest.html")
        return json.dumps({
            "ok": code == 0,
            "exit_code": code,
            "latest": latest if os.path.exists(latest) else "",
            "sections": _last_sections(dailybrief),
        })
    except Exception:
        # A collector crash must reach the user as text, not vanish into logcat.
        return json.dumps({"ok": False, "error": traceback.format_exc(limit=6)})


def _last_sections(dailybrief) -> str:
    """The section summary from the tail of the log, for the UI to show."""
    try:
        lines = dailybrief.LOG_PATH.read_text("utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        if "sections:" in line:
            return line.split("sections:", 1)[1].strip()
    return ""
