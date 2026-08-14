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

DEFAULT_PLAN: dict[str, Any] = {
    "start": "",
    "review_day": "sunday",
    "topics": [],
    "weekly": {"week_of": "", "changes": [], "history": []},
}


class PlanError(ValueError):
    """plan.json exists but cannot be trusted. Never silently substituted."""


# ----------------------------------------------------------------- file I/O


def load(path) -> dict:
    """Read plan.json, or return the empty default when there is none.

    utf-8-sig for the same reason config.json uses it: Notepad and PowerShell's
    `Set-Content -Encoding UTF8` both write a BOM, and plain utf-8 then throws
    away the whole file.
    """
    path = Path(path)
    if not path.exists():
        return dict(DEFAULT_PLAN, weekly=dict(DEFAULT_PLAN["weekly"]), topics=[])
    try:
        raw = json.loads(path.read_text("utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise PlanError(f"{path.name} unreadable: {exc}") from exc
    if not isinstance(raw, dict):
        raise PlanError(f"{path.name} must contain a JSON object")

    plan = dict(DEFAULT_PLAN, weekly=dict(DEFAULT_PLAN["weekly"]), topics=[])
    plan.update(raw)
    weekly = dict(DEFAULT_PLAN["weekly"])
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


# ------------------------------------------------------------------- reading


def _topic_name(topic: Any, i: int) -> str:
    if isinstance(topic, dict):
        return _clean(topic.get("name") or "") or f"(topic {i + 1} unnamed)"
    return _clean(topic) or f"(topic {i + 1} unnamed)"


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
            if not str((topic or {}).get("name") if isinstance(topic, dict) else topic or "").strip():
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
    weekly = plan.setdefault("weekly", dict(DEFAULT_PLAN["weekly"]))
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
    weekly = plan.setdefault("weekly", dict(DEFAULT_PLAN["weekly"]))
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
