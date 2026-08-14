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
import threading
import traceback
import types

# fileio is a leaf: stdlib only, no import-time state. Deliberately NOT
# platform_shim, which freezes PLATFORM at first import -- importing that here
# would beat configure() to it.
from fileio import write_text_atomic

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
    # Atomic: a kill between truncate and write would leave no config at all,
    # and Android kills backgrounded processes at will.
    write_text_atomic(path, json.dumps(cfg, indent=2))
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


# The Refresh button runs cmd_run in-process (MainActivity) and BriefWorker runs
# it on its own thread in the SAME Chaquopy interpreter. Two runs share
# briefs/<today>.html, latest.html and state.json.
_RUN_LOCK = threading.Lock()


def run_brief(home: str, force: bool = True) -> str:
    """Generate today's brief. Returns JSON describing what actually happened."""
    try:
        dailybrief = _app(home)
    except Exception:
        return json.dumps({"ok": False, "error": "could not start: " + traceback.format_exc(limit=3)})

    if not _RUN_LOCK.acquire(blocking=False):
        # Busy is a third state: not a brief, and not a failure either.
        return json.dumps({"ok": False, "busy": True,
                           "error": "a brief is already being generated"})
    try:
        args = types.SimpleNamespace(force=force, open=False)
        code = dailybrief.cmd_run(args)
        latest = str(dailybrief.BRIEFS_DIR / "latest.html")
        return json.dumps({
            "ok": code == 0,
            "exit_code": code,
            "latest": latest if os.path.exists(latest) else "",
            # cmd_run only logs a `sections:` line on the success path, so on a
            # failure the newest one in the log belongs to an earlier run --
            # possibly days old. Never present that as this run's result.
            "sections": _last_sections(dailybrief) if code == 0 else "",
        })
    except Exception:
        # A collector crash must reach the user as text, not vanish into logcat.
        return json.dumps({"ok": False, "error": traceback.format_exc(limit=6)})
    finally:
        _RUN_LOCK.release()


# --------------------------------------------------------------- 3x3 plan
#
# Every one of these goes through plan.py. The rules about month blocks, stale
# weeks and which source a week is due are genuinely intricate, and a second
# implementation in Kotlin would drift from the desktop one within a week.


def _json_safe(value):
    """plan.status() returns date objects; JSON does not have those."""
    import datetime as _dt
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.isoformat()
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def _plan_paths(home: str):
    dailybrief = _app(home)
    return dailybrief, dailybrief.BASE / "plan.json"


def plan_state(home: str) -> str:
    """Current 3x3 status plus the raw plan, so the editor shows what is on file."""
    import datetime as dt
    try:
        dailybrief, path = _plan_paths(home)
        import plan as P
    except Exception:
        return json.dumps({"ok": False, "error": traceback.format_exc(limit=3)})

    try:
        raw = P.load(path)
    except P.PlanError as exc:
        # A corrupt plan.json is reported, never silently replaced.
        return json.dumps({"ok": False, "exists": path.exists(), "error": str(exc)})

    try:
        st = P.status(raw, dt.date.today())
    except Exception:
        return json.dumps({"ok": False, "error": traceback.format_exc(limit=3)})

    return json.dumps({
        "ok": True,
        "exists": path.exists(),
        "status": st,
        "plan": raw,
        "weekdays": list(P.WEEKDAYS),
        "verdicts": list(P.VERDICTS),
        "slots": list(P.SLOTS),
    }, default=_json_safe)


def plan_init(home: str, start_iso: str = "") -> str:
    """Create plan.json and switch the section on, as `3x3 init` does."""
    import datetime as dt
    try:
        dailybrief, path = _plan_paths(home)
        import plan as P

        if not path.exists():
            if start_iso:
                start = dt.date.fromisoformat(start_iso)
            else:
                start = P.add_months(dt.date.today().replace(day=1), 1)
            # P.new_plan deep-copies DEFAULT_PLAN: spreading it here shared the
            # lists inside `weekly`, so set_week later appended one plan's
            # history onto the module default and into the next plan built here.
            P.save(path, P.new_plan(start))

        # The section has to be enabled or the plan renders nowhere.
        cfg_path = dailybrief.BASE / "config.json"
        cfg = json.loads(cfg_path.read_text("utf-8-sig")) if cfg_path.exists() else {}
        sections = list(cfg.get("sections") or dailybrief.DEFAULT_CONFIG.get("sections") or [])
        if "threexthree" not in sections:
            at = sections.index("calendar") + 1 if "calendar" in sections else len(sections)
            sections.insert(at, "threexthree")
            cfg["sections"] = sections
            write_text_atomic(cfg_path, json.dumps(cfg, indent=2, ensure_ascii=False))
        return json.dumps({"ok": True})
    except Exception:
        return json.dumps({"ok": False, "error": traceback.format_exc(limit=3)})


def _mutate_plan(home: str, fn) -> str:
    """Load, apply fn, save. Any PlanError is returned as a message, not raised."""
    try:
        _, path = _plan_paths(home)
        import plan as P
        raw = P.load(path)
        result = fn(P, raw)
        P.save(path, raw)
        return json.dumps({"ok": True, "message": result or ""})
    except Exception as exc:
        name = type(exc).__name__
        if name == "PlanError":
            return json.dumps({"ok": False, "error": str(exc)})
        return json.dumps({"ok": False, "error": traceback.format_exc(limit=3)})


def plan_set_topic(home: str, number: int, name: str,
                   video: str = "", text: str = "", misc: str = "") -> str:
    """Set topic N. Each source is "Title|url", the same form the CLI takes."""
    def apply(P, raw):
        sources = {k: v for k, v in (("video", video), ("text", text), ("misc", misc)) if v}
        P.set_topic(raw, int(number), name, sources)
        return f"Topic {number} saved"
    return _mutate_plan(home, apply)


def plan_set_week(home: str, a: str = "", b: str = "", c: str = "") -> str:
    def apply(P, raw):
        import datetime as dt
        changes = [s for s in (a, b, c) if s.strip()]
        if not changes:
            raise P.PlanError("give at least one change to run this week")
        P.set_week(raw, dt.date.today(), changes)
        return f"{len(changes)} change(s) filed for this week"
    return _mutate_plan(home, apply)


def plan_score_week(home: str, verdicts_csv: str) -> str:
    def apply(P, raw):
        verdicts = [v.strip() for v in verdicts_csv.split(",") if v.strip()]
        P.score_week(raw, verdicts)
        return "Scored"
    return _mutate_plan(home, apply)


def plan_mark_output(home: str, note: str = "") -> str:
    def apply(P, raw):
        import datetime as dt
        _, topic = P.mark_output(raw, dt.date.today(), note)
        return f"Output recorded for {topic}"
    return _mutate_plan(home, apply)


def plan_set_settings(home: str, start_iso: str = "", review_day: str = "",
                      session_minutes: int = 0, day_start: str = "", day_end: str = "") -> str:
    def apply(P, raw):
        import datetime as dt
        if start_iso:
            dt.date.fromisoformat(start_iso)      # validate before storing
            raw["start"] = start_iso
        if review_day:
            if review_day.lower() not in P.WEEKDAYS:
                raise P.PlanError(f"review day must be one of {', '.join(P.WEEKDAYS)}")
            raw["review_day"] = review_day.lower()
        if session_minutes:
            raw["session_minutes"] = max(5, min(600, int(session_minutes)))
        if day_start:
            raw["day_start"] = day_start
        if day_end:
            raw["day_end"] = day_end
        return "Saved"
    return _mutate_plan(home, apply)


# -------------------------------------------------------------- calendars
#
# calendars.txt holds a Google secret iCal URL, which is a permanent bearer
# token for the whole calendar. It is kept out of config.json for that reason,
# and the same care applies here: the UI is never handed the full URL back.


def _calendars_path(home: str):
    dailybrief = _app(home)
    return dailybrief.BASE / "calendars.txt"


def _mask(target: str) -> str:
    """Enough to recognise a calendar, not enough to read it."""
    import re
    if not target.lower().startswith(("http://", "https://", "webcal://")):
        return target.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    try:
        from urllib.parse import urlparse
        parsed = urlparse(target)
        host = parsed.netloc
    except ValueError:
        host = "?"
    tail = re.sub(r"[^A-Za-z0-9]", "", target)[-4:]
    return f"{host} · ends {tail}"


def calendars_list(home: str) -> str:
    try:
        import sources as S
        path = _calendars_path(home)
        entries = S.read_calendar_sources(path)
        return json.dumps({
            "ok": True,
            "calendars": [
                {"label": e["label"], "masked": _mask(e["target"]),
                 "is_url": e["target"].lower().startswith(("http://", "https://", "webcal://"))}
                for e in entries
            ],
        })
    except Exception:
        return json.dumps({"ok": False, "error": traceback.format_exc(limit=3)})


def calendars_add(home: str, label: str, target: str) -> str:
    """Append a calendar. webcal:// is rewritten to https://, which is the same URL."""
    try:
        path = _calendars_path(home)
        target = target.strip()
        if not target:
            return json.dumps({"ok": False, "error": "Paste the calendar's secret iCal address."})
        if target.lower().startswith("webcal://"):
            target = "https://" + target[len("webcal://"):]
        if not target.lower().startswith(("http://", "https://")):
            return json.dumps({"ok": False,
                               "error": "That is not a calendar address. It should start with https:// "
                                        "(Google Calendar → Settings → your calendar → "
                                        "Secret address in iCal format)."})

        import sources as S
        # `existing` is only for the trailing-newline test below. Duplicates are
        # decided on parsed entries: a substring test on the raw text refused a
        # real calendar because a commented-out line, or a longer URL, contained
        # it. The webcal:// rewrite is already done, so both spellings collide.
        existing = path.read_text("utf-8-sig") if path.exists() else ""
        if any(e["target"] == target for e in S.read_calendar_sources(path)):
            return json.dumps({"ok": False, "error": "That calendar is already connected."})

        label = label.strip().replace("=", "-")
        line = f"{label} = {target}" if label else target
        with open(path, "a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write(line + "\n")
        return json.dumps({"ok": True})
    except Exception:
        return json.dumps({"ok": False, "error": traceback.format_exc(limit=3)})


def calendars_remove(home: str, index: int) -> str:
    """Remove the calendar at [index] in the order calendars_list returned."""
    try:
        import sources as S
        path = _calendars_path(home)
        entries = list(S.iter_calendar_sources(path))
        index = int(index)
        if not (0 <= index < len(entries)):
            return json.dumps({"ok": False, "error": "That calendar is no longer there."})
        # Delete the exact line this entry was parsed from. Matching the target
        # back against the raw text deleted the wrong calendar whenever one URL
        # was a prefix of another, and consumed commented-out duplicates.
        lineno, _entry = entries[index]

        lines = path.read_text("utf-8-sig").splitlines()
        del lines[lineno]
        write_text_atomic(path, "\n".join(lines).rstrip("\n") + ("\n" if lines else ""))
        return json.dumps({"ok": True})
    except Exception:
        return json.dumps({"ok": False, "error": traceback.format_exc(limit=3)})


def calendars_test(home: str) -> str:
    """Fetch every connected calendar and report what actually came back."""
    import datetime as dt
    try:
        dailybrief = _app(home)
        import sources as S
        import netlib
        section = S.calendar(dailybrief.load_config(), dt.date.today(), dailybrief.BASE)
        return json.dumps({
            "ok": section.status != "failed",
            "status": section.status,
            # scrub(): the reason can quote a URL that failed, token and all.
            "reason": netlib.scrub(section.reason or ""),
            "events": len(section.data or []) if isinstance(section.data, list) else 0,
        })
    except Exception:
        return json.dumps({"ok": False, "status": "failed",
                           "reason": traceback.format_exc(limit=3), "events": 0})


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
