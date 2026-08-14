"""The 3x3 method: a weekly and monthly learning plan the brief reads each morning.

Three tiers, one per timescale. The daily tier is already the brief itself --
`paper` and `featured` are the paper and the news of the day -- so what lives
here is the two tiers a morning fetch cannot supply:

  weekly   three changes you picked on review day and run for seven days
  monthly  one topic a month for three months, three sources each, and an
           output produced in the final week

Nothing here touches the network. The plan is *yours*: this module reads what
you wrote in plan.json, does the calendar arithmetic, and reports where you are
in it. It never invents a topic, a source or a change -- an unfilled slot is
reported as unfilled, because a plan that quietly shows blanks as though they
were done is worse than no plan.

plan.json is gitignored like config.json; plan.example.json is the shipped copy.
"""
from __future__ import annotations

import calendar as calmod
import copy
import datetime as dt
import json
import urllib.parse
from pathlib import Path
from typing import Any

# What a brief may put in an href. Every other link in the page comes from a feed
# and is rewritten by netlib.normalise_link, which forces https; a plan URL is
# hand-written, so it is *checked* rather than rewritten -- a file:// link to a
# book PDF on your own disk is a legitimate source here, and https:// would break it.
SAFE_SCHEMES = ("http", "https", "file")

# The three source types a month's topic is learned from, in the order the
# weeks of the month present them.
SLOTS = ("video", "text", "misc")

SLOT_LABELS = {
    "video": "lecture video",
    "text": "article, book or long-form journalism",
    "misc": "course, series or anything else",
}

# Week N of a month-block does this. Week 4 is deliberately the tail of the
# month, not a literal seven days -- see week_of_month().
WEEK_PLAN = {1: "video", 2: "text", 3: "misc", 4: "output"}

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

VERDICTS = ("kept", "dropped", "retry")

# How long a session is worth sitting down for, and the hours a session may be
# proposed in. Both are overridable in plan.json; the day window exists because
# "any free gap" would otherwise happily offer you 03:00.
DEFAULT_SESSION_MINUTES = 45
DEFAULT_DAY_START = "08:00"
DEFAULT_DAY_END = "22:00"

DEFAULT_PLAN: dict[str, Any] = {
    "start": "",
    "review_day": "sunday",
    "session_minutes": DEFAULT_SESSION_MINUTES,
    "day_start": DEFAULT_DAY_START,
    "day_end": DEFAULT_DAY_END,
    "topics": [],
    "weekly": {"week_of": "", "changes": [], "history": []},
}


class PlanError(ValueError):
    """plan.json exists but cannot be trusted. Never silently substituted."""


# ----------------------------------------------------------------- file I/O


def blank_plan() -> dict:
    """A fresh empty plan, sharing nothing with DEFAULT_PLAN.

    dict(DEFAULT_PLAN, weekly=dict(...)) is one level too shallow: the lists
    inside `weekly` stay shared, and set_week appends to `history` in place, so
    one plan's archived week leaks into the next plan built from the default.
    """
    return copy.deepcopy(DEFAULT_PLAN)


def new_plan(start: dt.date) -> dict:
    """A fresh plan starting on `start`. Shares nothing with DEFAULT_PLAN."""
    plan = blank_plan()
    plan["start"] = start.isoformat()
    return plan


def load(path) -> dict:
    """Read plan.json, or return the empty default when there is none.

    utf-8-sig for the same reason config.json uses it: Notepad and PowerShell's
    `Set-Content -Encoding UTF8` both write a BOM, and plain utf-8 then throws
    away the whole file.
    """
    path = Path(path)
    if not path.exists():
        return blank_plan()
    try:
        raw = json.loads(path.read_text("utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise PlanError(f"{path.name} unreadable: {exc}") from exc
    if not isinstance(raw, dict):
        raise PlanError(f"{path.name} must contain a JSON object")

    plan = blank_plan()
    plan.update(raw)
    weekly = blank_plan()["weekly"]
    if isinstance(raw.get("weekly"), dict):
        weekly.update(raw["weekly"])
    plan["weekly"] = weekly
    if not isinstance(plan.get("topics"), list):
        raise PlanError("`topics` must be a list")
    return plan


def save(path, plan: dict) -> None:
    path = Path(path)
    path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", "utf-8")


# -------------------------------------------------------------- date helpers


def add_months(d: dt.date, n: int) -> dt.date:
    """Add n calendar months, clamping to the target month's last day.

    31 January + 1 month is 28 February, not 3 March. Clamping keeps every
    month-block boundary inside the month it names.
    """
    total = d.month - 1 + n
    year = d.year + total // 12
    month = total % 12 + 1
    return dt.date(year, month, min(d.day, calmod.monthrange(year, month)[1]))


def review_weekday(plan: dict) -> int:
    """Index of the review day, Monday=0. Unrecognised names fall back to Sunday.

    The fallback is reported as a problem by status() rather than applied
    quietly: a typo'd review_day that silently becomes Sunday would move your
    review by up to six days without ever saying so.
    """
    name = str(plan.get("review_day") or "sunday").strip().lower()
    return WEEKDAYS.index(name) if name in WEEKDAYS else 6


def _clean(value) -> str:
    """One line of plain text. Newlines would break a markdown list item."""
    return " ".join(str(value).split())


def safe_url(url: str) -> tuple[str, str]:
    """(usable_url, rejected_original). A bare host gains https://.

    Returns the rejection rather than swallowing it so status() can name it: a
    link that silently vanishes from the brief looks like a source you forgot
    to add, which is the one thing this section must never imply.
    """
    url = _clean(url)
    if not url:
        return "", ""
    try:
        scheme = urllib.parse.urlsplit(url).scheme.lower()
    except ValueError:
        return "", url
    if not scheme:
        return f"https://{url}", ""
    return (url, "") if scheme in SAFE_SCHEMES else ("", url)


def week_start(today: dt.date, weekday: int = 6) -> dt.date:
    """The most recent review day on or before `today` -- the week's anchor.

    On review day itself this returns today, so picking three changes that
    morning files them under the week they are about to govern.
    """
    return today - dt.timedelta(days=(today.weekday() - weekday) % 7)


def week_of_month(month_start: dt.date, today: dt.date) -> int:
    """Which of the four weeks of a month-block `today` falls in.

    Capped at 4 rather than running to 5: the fourth week is "produce the
    output", and a 31-day month must not roll past it into a week with no job.
    """
    return max(1, min(4, (today - month_start).days // 7 + 1))


# --------------------------------------------------------------- free time


def _hhmm(value, fallback: str) -> dt.time:
    """A HH:MM string as a time, falling back rather than raising."""
    try:
        hh, _, mm = str(value or fallback).partition(":")
        return dt.time(int(hh), int(mm or 0))
    except (TypeError, ValueError):
        hh, _, mm = fallback.partition(":")
        return dt.time(int(hh), int(mm))


def _end_is_unknown(ev: dict) -> bool:
    """Did icslib parse an end and then refuse it, leaving the length unknown?

    Checked via the explicit flag, with the warning text as a fallback so an
    occurrence built by an older path is still read correctly.
    """
    if ev.get("end_unknown"):
        return True
    return any("end dropped" in str(w) for w in (ev.get("warnings") or []))


def busy_intervals(events: list[dict]) -> tuple[list[tuple[dt.datetime, dt.datetime, str]],
                                                list[str], list[str]]:
    """Timed events as local (start, end, title), plus all-day and
    unknown-length titles.

    Three cases, and conflating any two of them books you twice:

    - **All-day** entries do NOT block time. Most are birthdays or labels, and
      blocking a whole day on one would leave you unable to schedule all week.
      Returned separately so a genuine "on leave" is visible beside the proposal.
    - **No DTEND at all** is a real zero-length instant in RFC 5545. Treated as
      one, rather than inventing a duration that would swallow a free evening.
    - **A DTEND that icslib parsed and then dropped** (a TZID with no VTIMEZONE,
      or an end before its start) means the length is UNKNOWN, not zero. Assumed
      to run to the end of the day, because the alternative is offering a slot
      inside a fourteen-hour workshop. Also named, so the day is not mysteriously
      full.
    """
    busy, all_day, unknown = [], [], []
    for ev in events or []:
        title = _clean(ev.get("summary") or "(no title)")
        if ev.get("all_day"):
            all_day.append(title)
            continue
        start, end = ev.get("start"), ev.get("end")
        if not start:
            continue
        start = start.astimezone()
        if _end_is_unknown(ev):
            unknown.append(title)
            # Clipped to the day's window by free_windows.
            busy.append((start, start + dt.timedelta(hours=24), title))
            continue
        end = end.astimezone() if end else start
        if end < start:
            start, end = end, start
        busy.append((start, end, title))
    busy.sort(key=lambda b: b[0])
    return busy, all_day, unknown


def free_windows(busy, day_start: dt.time, day_end: dt.time, day: dt.date,
                 not_before: dt.datetime | None = None) -> list[tuple[dt.datetime, dt.datetime]]:
    """Gaps between `busy` inside one day's waking window, merged and clipped."""
    lo = dt.datetime.combine(day, day_start).astimezone()
    hi = dt.datetime.combine(day, day_end).astimezone()
    if not_before and not_before > lo:
        lo = not_before
    if lo >= hi:
        return []

    # Merge overlapping meetings first: two back-to-back overlapping events
    # would otherwise each carve a gap out of the other.
    spans = []
    for start, end, _title in sorted(busy, key=lambda b: b[0]):
        start, end = max(start, lo), min(end, hi)
        if end <= start:
            continue
        if spans and start <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], end)
        else:
            spans.append([start, end])

    out, cursor = [], lo
    for start, end in spans:
        if start > cursor:
            out.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < hi:
        out.append((cursor, hi))
    return out


def propose_session(days: dict[dt.date, list[dict]], plan: dict, now: dt.datetime,
                    until: dt.date) -> dict:
    """The first window from `now` to `until` long enough for a session.

    Returns what was found AND what was in the way, because "Thursday is taken"
    is the half that makes the suggestion trustworthy.
    """
    minutes = plan.get("session_minutes")
    try:
        minutes = max(15, min(8 * 60, int(minutes)))
    except (TypeError, ValueError):
        minutes = DEFAULT_SESSION_MINUTES
    need = dt.timedelta(minutes=minutes)
    day_start = _hhmm(plan.get("day_start"), DEFAULT_DAY_START)
    day_end = _hhmm(plan.get("day_end"), DEFAULT_DAY_END)

    day = now.date()
    considered, blocked_today, unknown_seen = 0, [], []
    while day <= until:
        events = days.get(day)
        if events is None:            # never scanned; do not claim it is free
            day += dt.timedelta(days=1)
            continue
        considered += 1
        busy, all_day, unknown = busy_intervals(events)
        unknown_seen.extend(unknown)
        if day == now.date():
            blocked_today = busy
        windows = free_windows(busy, day_start, day_end, day,
                               not_before=now if day == now.date() else None)
        for start, end in windows:
            if end - start >= need:
                return {"found": True, "start": start, "end": start + need,
                        "minutes": minutes, "all_day": all_day,
                        "clashes": [b for b in busy if b[0].date() == day],
                        # Every day walked past, not just the one landed on: an
                        # unknown-length event is why an earlier day was skipped,
                        # and that is the half that makes this suggestion
                        # trustworthy rather than mysterious.
                        "unknown_length": list(unknown_seen),
                        "days_scanned": considered}
        day += dt.timedelta(days=1)
    return {"found": False, "minutes": minutes, "all_day": [],
            "clashes": blocked_today, "unknown_length": unknown_seen,
            "days_scanned": considered}


# ------------------------------------------------------------------- reading


def _topic_raw_name(topic: Any) -> str:
    """The name as written, normalised. Never the '(topic N unnamed)' stand-in.

    `or ""` on both branches: str(None) is "None", which is four non-blank
    characters and silently passes an is-it-blank test.
    """
    return str(((topic or {}).get("name") or "") if isinstance(topic, dict) else (topic or ""))


def _topic_name(topic: Any, i: int) -> str:
    return _clean(_topic_raw_name(topic)) or f"(topic {i + 1} unnamed)"


def _topic_sources(topic: Any) -> dict[str, dict | None]:
    """The three source slots, each either a filled dict or None. Never faked."""
    raw = topic.get("sources") if isinstance(topic, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    out: dict[str, dict | None] = {}
    for slot in SLOTS:
        entry = raw.get(slot)
        if isinstance(entry, str) and entry.strip():
            entry = {"title": entry, "url": ""}
        if isinstance(entry, dict) and _clean(entry.get("title") or ""):
            url, rejected = safe_url(entry.get("url") or "")
            out[slot] = {"title": _clean(entry["title"]), "url": url, "rejected_url": rejected}
        else:
            out[slot] = None
    return out


def status(plan: dict, today: dt.date) -> dict:
    """Everything the brief and the CLI need to say, computed once.

    Returns `problems` rather than raising: a half-filled plan should still
    show what it does have, with the gaps named.
    """
    problems: list[str] = []
    topics = plan.get("topics") or []

    start_raw = str(plan.get("start") or "").strip()
    start: dt.date | None = None
    if start_raw:
        try:
            start = dt.date.fromisoformat(start_raw)
        except ValueError:
            problems.append(f"`start` is not a YYYY-MM-DD date: {start_raw!r}")

    block: dict | None = None
    block_state = "unset"
    if start and topics:
        bounds = [add_months(start, i) for i in range(len(topics) + 1)]
        if today < bounds[0]:
            block_state = "not_started"
            block = {"starts": bounds[0], "topic_count": len(topics)}
        elif today >= bounds[-1]:
            block_state = "finished"
            block = {"ended": bounds[-1] - dt.timedelta(days=1), "topic_count": len(topics)}
        else:
            idx = max(i for i in range(len(topics)) if bounds[i] <= today)
            month_start, month_end = bounds[idx], bounds[idx + 1] - dt.timedelta(days=1)
            week = week_of_month(month_start, today)
            due = WEEK_PLAN[week]
            topic = topics[idx]
            sources = _topic_sources(topic)
            missing = [s for s in SLOTS if sources[s] is None]
            if missing:
                problems.append(
                    f"topic {idx + 1} ({_topic_name(topic, idx)}) has no "
                    + ", ".join(SLOT_LABELS[m] for m in missing)
                )
            for slot in SLOTS:
                bad = (sources[slot] or {}).get("rejected_url")
                if bad:
                    problems.append(f"{slot} link is not a linkable address, shown "
                                    f"unlinked: {bad[:60]}")
            if not _topic_raw_name(topic).strip():
                problems.append(f"topic {idx + 1} has no name - this month has no subject")
            # `output` may be anything a hand edit left behind, so test its shape
            # rather than calling .get() on whatever happens to be there.
            out_blk = topic.get("output") if isinstance(topic, dict) else None
            output_done = bool(out_blk.get("done")) if isinstance(out_blk, dict) else False
            block_state = "active"
            block = {
                "index": idx,
                "number": idx + 1,
                "topic_count": len(topics),
                "name": _topic_name(topic, idx),
                "month_start": month_start,
                "month_end": month_end,
                "week": week,
                "due": due,
                "source": sources.get(due) if due in SLOTS else None,
                "sources": sources,
                "missing": missing,
                "output_done": output_done,
                "days_left": (month_end - today).days,
            }
    elif not topics:
        problems.append("no topics yet - add three with `dailybrief.py 3x3 topic`")
    elif not start_raw:
        problems.append("no `start` date - set one with `dailybrief.py 3x3 start YYYY-MM-DD`")

    raw_day = str(plan.get("review_day") or "sunday").strip().lower()
    if raw_day not in WEEKDAYS:
        problems.append(f"`review_day` {raw_day!r} is not a weekday name - using sunday")
    rw = review_weekday(plan)
    this_week = week_start(today, rw)
    weekly = plan.get("weekly") if isinstance(plan.get("weekly"), dict) else {}

    # A bare string here would iterate per character and invent one "change" per
    # letter, which is exactly the sort of hand-edit the brief must not dress up
    # as real data.
    raw_changes = weekly.get("changes")
    if raw_changes and not isinstance(raw_changes, list):
        problems.append("`weekly.changes` must be a list of three strings")
        raw_changes = []
    changes = [_clean(c) for c in (raw_changes or []) if _clean(c)]
    filed_raw = str(weekly.get("week_of") or "").strip()
    filed: dt.date | None = None
    if filed_raw:
        try:
            filed = dt.date.fromisoformat(filed_raw)
        except ValueError:
            problems.append(f"`weekly.week_of` is not a YYYY-MM-DD date: {filed_raw!r}")

    stale = bool(changes) and filed is not None and filed < this_week
    return {
        "configured": bool(start_raw or topics or changes),
        "problems": problems,
        "block_state": block_state,
        "block": block,
        "start": start,
        # Carried so the session placement, which runs after collection, does
        # not have to read plan.json a second time.
        "settings": {"session_minutes": plan.get("session_minutes", DEFAULT_SESSION_MINUTES),
                     "day_start": plan.get("day_start", DEFAULT_DAY_START),
                     "day_end": plan.get("day_end", DEFAULT_DAY_END)},
        "weekly": {
            "changes": changes,
            "week_of": filed,
            "this_week": this_week,
            "review_day": WEEKDAYS[rw],
            # Review day, or any day you are still running a stale set: the
            # prompt has to persist past Sunday or a missed Sunday is silent.
            "review_due": today.weekday() == rw or stale or not changes,
            "stale": stale,
            "days_running": (today - filed).days if filed else None,
        },
    }


# ------------------------------------------------------------------- writing


def set_week(plan: dict, today: dt.date, changes: list[str]) -> dict:
    """File three new changes for the current week, archiving the outgoing set.

    The outgoing set keeps whatever verdicts it was given; unscored ones are
    archived as such rather than assumed kept.
    """
    weekly = plan.setdefault("weekly", blank_plan()["weekly"])
    previous = [str(c).strip() for c in (weekly.get("changes") or []) if str(c).strip()]
    if previous:
        history = weekly.setdefault("history", [])
        history.append({
            "week_of": weekly.get("week_of") or "",
            "changes": previous,
            "verdicts": list(weekly.get("verdicts") or []),
        })
        del history[:-52]        # a year of weeks is plenty to keep
    weekly["week_of"] = week_start(today, review_weekday(plan)).isoformat()
    weekly["changes"] = [c.strip() for c in changes if c.strip()]
    weekly["verdicts"] = []
    return plan


def score_week(plan: dict, verdicts: list[str]) -> dict:
    """Attach kept/dropped/retry verdicts to the changes currently on file."""
    weekly = plan.setdefault("weekly", blank_plan()["weekly"])
    bad = [v for v in verdicts if v.lower() not in VERDICTS]
    if bad:
        raise PlanError(f"verdict must be one of {', '.join(VERDICTS)}; got {bad[0]!r}")
    weekly["verdicts"] = [v.lower() for v in verdicts]
    return plan


# The method is three topics. A little slack is fine; a typo that pads the list
# to topic 99 with 98 blanks is not.
MAX_TOPICS = 12


def set_topic(plan: dict, number: int, name: str, sources: dict[str, str] | None = None) -> dict:
    """Create or update topic N (1-based), leaving the other slots alone."""
    if number < 1:
        raise PlanError("topic number starts at 1")
    if number > MAX_TOPICS:
        raise PlanError(f"topic {number} is beyond the {MAX_TOPICS}-topic limit "
                        f"-- the method is three")
    topics = plan.setdefault("topics", [])
    while len(topics) < number:
        topics.append({"name": "", "sources": {}, "output": {"done": False, "note": ""}})
    topic = topics[number - 1]
    if not isinstance(topic, dict):
        topic = {"name": str(topic), "sources": {}, "output": {"done": False, "note": ""}}
        topics[number - 1] = topic
    if name:
        topic["name"] = name.strip()
    slots = topic.setdefault("sources", {})
    for slot, value in (sources or {}).items():
        if slot not in SLOTS:
            raise PlanError(f"source slot must be one of {', '.join(SLOTS)}; got {slot!r}")
        if not value:
            continue
        title, _, url = str(value).partition("|")
        slots[slot] = {"title": title.strip(), "url": url.strip()}
    topic.setdefault("output", {"done": False, "note": ""})
    return plan


def mark_output(plan: dict, today: dt.date, note: str = "") -> tuple[dict, str]:
    """Mark the current month's output produced. Returns (plan, topic name)."""
    st = status(plan, today)
    block = st.get("block")
    if st["block_state"] != "active" or not block:
        raise PlanError("no month-block is running today, so nothing is due")
    topic = plan["topics"][block["index"]]
    if not isinstance(topic, dict):
        topic = {"name": str(topic), "sources": {}}
        plan["topics"][block["index"]] = topic
    topic["output"] = {"done": True, "note": note.strip(),
                       "produced": today.isoformat()}
    return plan, block["name"]
