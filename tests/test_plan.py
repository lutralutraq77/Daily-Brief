"""The 3x3 plan: calendar arithmetic, the tri-state collector, and both renderers.

Network-free. Every date is fixed, so a test that passes in September still
passes in February -- the bugs this catches are month-boundary bugs.
"""
import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import dailybrief as db
import plan as P
import sources as S

checks = {}
def check(n, c): checks[n] = bool(c)

TMP = Path(tempfile.mkdtemp(prefix="dailybrief-plan-"))

# 2026-09-01 is a Tuesday; 2026-09-06 is the first Sunday after it.
START = dt.date(2026, 9, 1)


def plan_with(**kw):
    base = {
        "start": START.isoformat(),
        "review_day": "sunday",
        "topics": [
            {"name": "Psychoacoustics",
             # video and text carry urls, misc deliberately does not: the renderers
             # must emit a link for the first two and plain text for the third.
             "sources": {"video": {"title": "Lecture 1", "url": "https://example.com/v"},
                         "text": {"title": "A Book", "url": "https://example.com/t"},
                         "misc": {"title": "A Course", "url": ""}},
             "output": {"done": False, "note": ""}},
            {"name": "Ottoman history", "sources": {}, "output": {"done": False}},
            {"name": "Fermentation", "sources": {}, "output": {"done": False}},
        ],
        "weekly": {"week_of": "", "changes": [], "history": []},
    }
    base.update(kw)
    return base


# --- date arithmetic --------------------------------------------------------

check("add_months clamps 31 Jan to 28 Feb",
      P.add_months(dt.date(2026, 1, 31), 1) == dt.date(2026, 2, 28))
check("add_months clamps into a leap February",
      P.add_months(dt.date(2028, 1, 31), 1) == dt.date(2028, 2, 29))
check("add_months rolls the year",
      P.add_months(dt.date(2026, 11, 15), 3) == dt.date(2027, 2, 15))
check("add_months of 0 is identity",
      P.add_months(START, 0) == START)

# A block starting on the 31st clamps, so month one is short and month two starts
# on the 28th -- it must never skip a month by landing in March.
jan31 = plan_with(start="2026-01-31")
check("a 31 Jan block ends month 1 on 27 Feb",
      P.status(jan31, dt.date(2026, 2, 1))["block"]["month_end"] == dt.date(2026, 2, 27))
check("a 31 Jan block starts month 2 on 28 Feb",
      P.status(jan31, dt.date(2026, 2, 28))["block"]["month_start"] == dt.date(2026, 2, 28))
check("a 31 Jan block does not skip into March",
      P.status(jan31, dt.date(2026, 2, 28))["block"]["number"] == 2)

# Sunday review: the week's anchor is the Sunday on or before today.
check("week_start on a Sunday is that Sunday",
      P.week_start(dt.date(2026, 9, 6), 6) == dt.date(2026, 9, 6))
check("week_start on the Monday after looks back one day",
      P.week_start(dt.date(2026, 9, 7), 6) == dt.date(2026, 9, 6))
check("week_start on the Saturday before looks back six days",
      P.week_start(dt.date(2026, 9, 5), 6) == dt.date(2026, 8, 30))
check("week_start honours a Monday review day",
      P.week_start(dt.date(2026, 9, 5), 0) == dt.date(2026, 8, 31))

check("week 1 on the first day", P.week_of_month(START, START) == 1)
check("week 1 on day 7", P.week_of_month(START, dt.date(2026, 9, 7)) == 1)
check("week 2 on day 8", P.week_of_month(START, dt.date(2026, 9, 8)) == 2)
check("week 3 on day 15", P.week_of_month(START, dt.date(2026, 9, 15)) == 3)
check("week 4 on day 22", P.week_of_month(START, dt.date(2026, 9, 22)) == 4)
# A 30- or 31-day month must not roll into a fifth week with no job.
check("week 4 still on day 29", P.week_of_month(START, dt.date(2026, 9, 29)) == 4)
check("week 4 still on day 31", P.week_of_month(dt.date(2026, 10, 1), dt.date(2026, 10, 31)) == 4)

# --- block state ------------------------------------------------------------

st = P.status(plan_with(), dt.date(2026, 8, 20))
check("before the start date the block has not started", st["block_state"] == "not_started")
check("not-started reports when it starts", st["block"]["starts"] == START)

st = P.status(plan_with(), dt.date(2026, 12, 2))
check("after three months the block is finished", st["block_state"] == "finished")
check("finished reports the last day", st["block"]["ended"] == dt.date(2026, 11, 30))

st = P.status(plan_with(), dt.date(2026, 9, 3))
b = st["block"]
check("month 1 is active", st["block_state"] == "active" and b["number"] == 1)
check("month 1 names the first topic", b["name"] == "Psychoacoustics")
check("month 1 week 1 is due the video", b["week"] == 1 and b["due"] == "video")
check("week 1 serves the video source", b["source"]["title"] == "Lecture 1")
check("month 1 ends 30 Sep", b["month_end"] == dt.date(2026, 9, 30))
check("days_left counts to month end", b["days_left"] == 27)

check("week 2 is due the text",
      P.status(plan_with(), dt.date(2026, 9, 10))["block"]["due"] == "text")
check("week 3 is due the misc",
      P.status(plan_with(), dt.date(2026, 9, 17))["block"]["due"] == "misc")
st4 = P.status(plan_with(), dt.date(2026, 9, 24))
check("week 4 is due the output", st4["block"]["due"] == "output")
check("week 4 has no source slot", st4["block"]["source"] is None)

# The second month starts on 1 October, not 29 days after the first.
st = P.status(plan_with(), dt.date(2026, 10, 2))
check("month 2 starts on the calendar month", st["block"]["number"] == 2
      and st["block"]["month_start"] == dt.date(2026, 10, 1))
check("month 2 names the second topic", st["block"]["name"] == "Ottoman history")

# --- gaps are reported, never faked ----------------------------------------

st = P.status(plan_with(), dt.date(2026, 10, 2))
check("a topic with no sources is reported as missing all three",
      set(st["block"]["missing"]) == {"video", "text", "misc"})
check("missing sources become a stated problem",
      any("Ottoman history" in p for p in st["problems"]))
check("an unfilled slot is None, not a placeholder", st["block"]["source"] is None)

unnamed = plan_with(topics=[{"name": "", "sources": {}}])
check("an unnamed topic is a stated problem",
      any("no name" in p for p in P.status(unnamed, dt.date(2026, 9, 3))["problems"]))
check("a bad start date is a stated problem",
      any("not a YYYY-MM-DD" in p
          for p in P.status(plan_with(start="soon"), dt.date(2026, 9, 3))["problems"]))
check("no topics is a stated problem",
      any("no topics" in p for p in P.status(plan_with(topics=[]), dt.date(2026, 9, 3))["problems"]))

# --- the weekly three -------------------------------------------------------

fresh = plan_with(weekly={"week_of": "2026-09-06", "changes": ["a", "b", "c"], "history": []})
st = P.status(fresh, dt.date(2026, 9, 9))          # Wednesday of that same week
check("this week's three are not stale", st["weekly"]["stale"] is False)
check("no review is due mid-week", st["weekly"]["review_due"] is False)
check("the three are returned in order", st["weekly"]["changes"] == ["a", "b", "c"])
check("days_running counts from the filing", st["weekly"]["days_running"] == 3)

st = P.status(fresh, dt.date(2026, 9, 13))         # the following Sunday
check("review is due on review day", st["weekly"]["review_due"] is True)
check("last week's three go stale", st["weekly"]["stale"] is True)

# A missed Sunday must keep nagging, or the prompt is silent for six days.
st = P.status(fresh, dt.date(2026, 9, 16))
check("a missed review keeps nagging past its day", st["weekly"]["review_due"] is True)
check("a missed review is still flagged stale", st["weekly"]["stale"] is True)

st = P.status(plan_with(), dt.date(2026, 9, 9))
check("an empty week asks for three", st["weekly"]["review_due"] is True)
check("an empty week is not 'stale'", st["weekly"]["stale"] is False)

# --- mutating ---------------------------------------------------------------

p = plan_with(weekly={"week_of": "2026-09-06", "changes": ["old1", "old2"],
                      "verdicts": ["kept", "dropped"], "history": []})
P.set_week(p, dt.date(2026, 9, 13), ["new1", "new2", "new3"])
check("set_week files the new three", p["weekly"]["changes"] == ["new1", "new2", "new3"])
check("set_week anchors to the review day", p["weekly"]["week_of"] == "2026-09-13")
check("set_week archives the outgoing set", p["weekly"]["history"][-1]["changes"] == ["old1", "old2"])
check("set_week archives their verdicts",
      p["weekly"]["history"][-1]["verdicts"] == ["kept", "dropped"])
check("set_week clears the old verdicts", p["weekly"]["verdicts"] == [])

P.score_week(p, ["kept", "retry", "dropped"])
check("score_week records verdicts", p["weekly"]["verdicts"] == ["kept", "retry", "dropped"])
try:
    P.score_week(p, ["kept", "maybe", "dropped"])
    check("score_week rejects an unknown verdict", False)
except P.PlanError as exc:
    check("score_week rejects an unknown verdict", "maybe" in str(exc))

p = plan_with(topics=[])
P.set_topic(p, 2, "Bread", {"video": "Baking 101|https://example.com/b"})
check("set_topic pads up to the requested number", len(p["topics"]) == 2)
check("set_topic sets the name", p["topics"][1]["name"] == "Bread")
check("set_topic splits Title|url", p["topics"][1]["sources"]["video"] ==
      {"title": "Baking 101", "url": "https://example.com/b"})
P.set_topic(p, 2, "", {"text": "Just a title"})
check("set_topic keeps the name when given none", p["topics"][1]["name"] == "Bread")
check("set_topic accepts a title with no url",
      p["topics"][1]["sources"]["text"] == {"title": "Just a title", "url": ""})
check("set_topic leaves other slots alone", p["topics"][1]["sources"]["video"]["title"] == "Baking 101")
try:
    P.set_topic(p, 1, "x", {"audio": "y"})
    check("set_topic rejects an unknown slot", False)
except P.PlanError:
    check("set_topic rejects an unknown slot", True)
try:
    P.set_topic(p, 0, "x")
    check("set_topic rejects topic 0", False)
except P.PlanError:
    check("set_topic rejects topic 0", True)
# A typo must not pad the list with 90-odd blank topics.
try:
    P.set_topic(p, 99, "x")
    check("set_topic refuses an absurd topic number", False)
except P.PlanError:
    check("set_topic refuses an absurd topic number", len(p["topics"]) <= P.MAX_TOPICS)

p = plan_with()
p, name = P.mark_output(p, dt.date(2026, 9, 24), "voice memo")
check("mark_output marks the running month", p["topics"][0]["output"]["done"] is True)
check("mark_output returns the topic name", name == "Psychoacoustics")
check("mark_output records the note", p["topics"][0]["output"]["note"] == "voice memo")
check("output_done surfaces in status",
      P.status(p, dt.date(2026, 9, 24))["block"]["output_done"] is True)
try:
    P.mark_output(plan_with(), dt.date(2026, 12, 2))
    check("mark_output refuses outside a block", False)
except P.PlanError:
    check("mark_output refuses outside a block", True)

# --- file I/O ---------------------------------------------------------------

check("a missing plan file loads as empty", P.load(TMP / "nope.json")["topics"] == [])
good = TMP / "good.json"
P.save(good, plan_with())
check("save then load round-trips", P.load(good)["topics"][0]["name"] == "Psychoacoustics")

bom = TMP / "bom.json"
bom.write_bytes(b"\xef\xbb\xbf" + json.dumps(plan_with()).encode("utf-8"))
check("a UTF-8 BOM does not throw the file away", P.load(bom)["review_day"] == "sunday")

bad = TMP / "bad.json"
bad.write_text("{not json", "utf-8")
try:
    P.load(bad)
    check("malformed JSON raises PlanError", False)
except P.PlanError:
    check("malformed JSON raises PlanError", True)

notdict = TMP / "list.json"
notdict.write_text("[1, 2]", "utf-8")
try:
    P.load(notdict)
    check("a JSON list is refused", False)
except P.PlanError:
    check("a JSON list is refused", True)

badtopics = TMP / "badtopics.json"
badtopics.write_text(json.dumps({"topics": "nope"}), "utf-8")
try:
    P.load(badtopics)
    check("non-list topics is refused", False)
except P.PlanError:
    check("non-list topics is refused", True)

# --- url safety -------------------------------------------------------------

check("https passes through", P.safe_url("https://a.example/x") == ("https://a.example/x", ""))
check("http passes through", P.safe_url("http://a.example") == ("http://a.example", ""))
check("file:// is allowed, for a book on disk",
      P.safe_url("file:///C:/books/x.pdf") == ("file:///C:/books/x.pdf", ""))
check("a bare host gains https", P.safe_url("a.example/x") == ("https://a.example/x", ""))
check("javascript: is rejected", P.safe_url("javascript:alert(1)") == ("", "javascript:alert(1)"))
check("data: is rejected", P.safe_url("data:text/html,x")[0] == "")
check("an empty url stays empty", P.safe_url("") == ("", ""))

evil = plan_with(topics=[{"name": "T", "sources": {
    "video": {"title": "Bad", "url": "javascript:alert(1)"}}}])
est = P.status(evil, dt.date(2026, 9, 3))
check("a rejected url does not reach the source", est["block"]["sources"]["video"]["url"] == "")
check("a rejected url is a stated problem",
      any("not a linkable address" in p for p in est["problems"]))
ehtml = db.compose_page({"sections": ["threexthree"], "units": {}, "location": {}, "feeds": []},
                        dt.date(2026, 9, 3),
                        {"threexthree": S.Section("threexthree", S.OK, data=est)}, [], {}, "")
check("no javascript: url reaches the page", "javascript:" not in ehtml)
check("the title still shows, unlinked", "Bad" in ehtml)

# --- hand-edited plan.json must degrade loudly, never invent -----------------

# A bare string would iterate per character and invent one change per letter.
strung = P.status(plan_with(weekly={"week_of": "2026-09-06", "changes": "abc"}),
                  dt.date(2026, 9, 9))
check("a string in weekly.changes is refused, not iterated", strung["weekly"]["changes"] == [])
check("a string in weekly.changes is a stated problem",
      any("must be a list" in p for p in strung["problems"]))

# `output` left as something other than an object must not raise.
odd = plan_with(topics=[{"name": "T", "sources": {}, "output": "yes"}])
check("a non-dict output does not crash status",
      P.status(odd, dt.date(2026, 9, 3))["block"]["output_done"] is False)

check("an unparseable review_day is reported, not applied silently",
      any("not a weekday name" in p
          for p in P.status(plan_with(review_day="funday"), dt.date(2026, 9, 3))["problems"]))
check("an unparseable review_day still falls back to sunday",
      P.review_weekday({"review_day": "funday"}) == 6)
check("a valid non-default review_day is honoured",
      P.review_weekday({"review_day": "Monday"}) == 0)

# Newlines would break a markdown list item and could inject structure.
multiline = plan_with(topics=[{"name": "Line\nbreak",
                               "sources": {"video": {"title": "Ti\ntle", "url": ""}}}])
mst = P.status(multiline, dt.date(2026, 9, 3))
check("a newline in a topic name is collapsed", "\n" not in mst["block"]["name"])
check("a newline in a source title is collapsed",
      "\n" not in mst["block"]["sources"]["video"]["title"])

# changes on file but no filing date: status tolerates it, so the CLI must too.
nodate = P.status(plan_with(weekly={"week_of": "", "changes": ["a", "b", "c"]}),
                  dt.date(2026, 9, 9))
check("changes with no week_of still parse", nodate["weekly"]["changes"] == ["a", "b", "c"])
check("changes with no week_of are not stale", nodate["weekly"]["stale"] is False)
check("the CLI echo survives a null week_of",
      "no filing date" in "\n".join(db._plan_lines(nodate, dt.date(2026, 9, 9))))

# --- free time and session placement ----------------------------------------

def ev(day, h1, m1, h2, m2, title="Busy", all_day=False):
    if all_day:
        return {"all_day": True, "summary": title, "start": None, "end": None}
    return {"all_day": False, "summary": title,
            "start": dt.datetime.combine(day, dt.time(h1, m1)).astimezone(),
            "end": dt.datetime.combine(day, dt.time(h2, m2)).astimezone()}

D1 = dt.date(2026, 9, 9)
D2 = dt.date(2026, 9, 10)
NOW = dt.datetime.combine(D1, dt.time(8, 0)).astimezone()
SETTINGS = {"session_minutes": 45, "day_start": "08:00", "day_end": "22:00"}

busy, allday, unknown = P.busy_intervals([ev(D1, 9, 0, 10, 0), ev(D1, 0, 0, 0, 0, "Birthday", True)])
check("timed events become busy intervals", len(busy) == 1)
check("all-day events do not block time", busy[0][2] == "Busy" and allday == ["Birthday"])
check("a normal event has a known length", unknown == [])

# icslib returns end=None for TWO reasons and they must never be conflated.
# No DTEND at all is a genuine zero-length instant...
inst = {"all_day": False, "summary": "Ping",
        "start": dt.datetime.combine(D1, dt.time(9, 0)).astimezone(), "end": None}
b, _a, u = P.busy_intervals([inst])
check("no DTEND at all stays a zero-length instant", b[0][0] == b[0][1] and u == [])
check("an instant leaves the day free",
      len(P.free_windows(b, dt.time(8, 0), dt.time(22, 0), D1)) == 1)

# ...but a DTEND that icslib parsed and DROPPED means the length is unknown, and
# treating that as an instant offers a slot inside a 14-hour workshop.
for marker in ({"end_unknown": True}, {"warnings": ["unresolved end timezone X — end dropped"]}):
    dropped = {"all_day": False, "summary": "Client workshop", "end": None,
               "start": dt.datetime.combine(D1, dt.time(8, 0)).astimezone(), **marker}
    b, _a, u = P.busy_intervals([dropped])
    tag = "flag" if "end_unknown" in marker else "warning fallback"
    check(f"a dropped end is not an instant ({tag})", b[0][1] > b[0][0] and u == ["Client workshop"])
    check(f"a dropped end blocks the rest of the day ({tag})",
          P.free_windows(b, dt.time(8, 0), dt.time(22, 0), D1) == [])
    s = P.propose_session({D1: [dropped]}, SETTINGS,
                          dt.datetime.combine(D1, dt.time(8, 0)).astimezone(), D1)
    check(f"no session is offered inside an unknown-length event ({tag})", s["found"] is False)
    check(f"the unknown-length event is named ({tag})", s["unknown_length"] == ["Client workshop"])

w = P.free_windows(busy, dt.time(8, 0), dt.time(22, 0), D1)
check("free windows bracket a meeting", len(w) == 2)
check("the first window ends at the meeting", w[0][1].hour == 9)
check("the second window starts after it", w[1][0].hour == 10)

# Overlapping meetings must merge, or each carves a phantom gap from the other.
ov, _a, _u = P.busy_intervals([ev(D1, 9, 0, 11, 0), ev(D1, 10, 0, 12, 0)])
w = P.free_windows(ov, dt.time(8, 0), dt.time(22, 0), D1)
check("overlapping meetings merge into one block", len(w) == 2)
check("the merged block ends at the later end", w[1][0].hour == 12)

# A full day yields nothing, and must not fall back to "free".
full, _a, _u = P.busy_intervals([ev(D1, 8, 0, 22, 0, "All-day workshop")])
check("a fully booked day has no windows",
      P.free_windows(full, dt.time(8, 0), dt.time(22, 0), D1) == [])

s = P.propose_session({D1: [ev(D1, 9, 0, 10, 0)]}, SETTINGS, NOW, D1)
check("a session is placed in the first free gap", s["found"] and s["start"].hour == 8)
check("the session is exactly session_minutes long",
      (s["end"] - s["start"]) == dt.timedelta(minutes=45))

# Not before "now": a slot earlier today has already gone.
late = dt.datetime.combine(D1, dt.time(20, 30)).astimezone()
s = P.propose_session({D1: [ev(D1, 9, 0, 10, 0)]}, SETTINGS, late, D1)
check("a session is never proposed in the past", s["found"] and s["start"] >= late)
check("a session stays inside day_end", s["end"].time() <= dt.time(22, 0))

# Today full, tomorrow free -> roll to tomorrow rather than give up.
s = P.propose_session({D1: [ev(D1, 8, 0, 22, 0)], D2: []}, SETTINGS, NOW, D2)
check("a full day rolls to the next", s["found"] and s["start"].date() == D2)
check("the day it landed on reports its own clashes",
      all(c[0].date() == s["start"].date() for c in s["clashes"]))

# Nothing free anywhere in range -> found False, never a fabricated slot.
s = P.propose_session({D1: [ev(D1, 8, 0, 22, 0)], D2: [ev(D2, 8, 0, 22, 0)]}, SETTINGS, NOW, D2)
check("no free window is reported honestly", s["found"] is False)
check("a failed search still reports the length wanted", s["minutes"] == 45)

# A day that was never scanned must not be treated as free.
s = P.propose_session({D1: [ev(D1, 8, 0, 22, 0)]}, SETTINGS, NOW, D2)
check("an unscanned day is not claimed free", s["found"] is False)
check("only scanned days are counted", s["days_scanned"] == 1)

check("a bad session_minutes falls back to the default",
      P.propose_session({D1: []}, {"session_minutes": "soon"}, NOW, D1)["minutes"] == 45)
check("a bad day_start does not raise",
      P.propose_session({D1: []}, {"day_start": "nope"}, NOW, D1)["found"] is True)

# An event running past midnight is returned on BOTH days by icslib, carrying its
# original start/end. On the second day it must still block the morning, or a
# night shift ending at 11:00 reads as a free 08:00.
_night = {"all_day": False, "summary": "Night shift",
          "start": dt.datetime.combine(D1, dt.time(23, 0)).astimezone(),
          "end": dt.datetime.combine(D2, dt.time(11, 0)).astimezone()}
_b, _a, _u = P.busy_intervals([_night])
_w = P.free_windows(_b, dt.time(8, 0), dt.time(22, 0), D2)
check("an overnight event blocks the next morning", _w and _w[0][0].hour == 11)
check("and leaves the rest of that day free", _w[0][1].hour == 22)
_s = P.propose_session({D2: [_night]}, SETTINGS,
                       dt.datetime.combine(D2, dt.time(8, 0)).astimezone(), D2)
check("no session is proposed inside an overnight event",
      _s["found"] and _s["start"].hour == 11)

# A window must anchor to the target day's OWN utc offset, not today's, or every
# boundary shifts by an hour across a DST change and the whole comparison is
# wrong for that week. Written offset-agnostically so it holds in any timezone.
for _d in (dt.date(2026, 10, 24), dt.date(2026, 10, 26), dt.date(2026, 3, 28)):
    _w = P.free_windows([], dt.time(8, 0), dt.time(22, 0), _d)
    _want = dt.datetime.combine(_d, dt.time(8, 0)).astimezone()
    check(f"window on {_d} uses that day's own offset",
          _w and _w[0][0] == _want and _w[0][0].utcoffset() == _want.utcoffset())

# An event in a different timezone from the machine must still line up.
_far = {"all_day": False, "summary": "Remote call",
        "start": dt.datetime(2026, 9, 9, 14, 0, tzinfo=dt.timezone(dt.timedelta(hours=9))),
        "end": dt.datetime(2026, 9, 9, 15, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))}
_b, _a, _u = P.busy_intervals([_far])
check("a foreign-timezone event is converted to local",
      _b[0][0] == _far["start"].astimezone() and _b[0][0].utcoffset() is not None)

# --- attach_session: never call an unchecked calendar free -------------------

def attached(cal_section):
    st = P.status(plan_with(), D1)
    secs = {"threexthree": S.Section("threexthree", S.OK, data=st)}
    if cal_section is not None:
        secs["calendar"] = cal_section
    db.attach_session(secs, D1, now=NOW)
    return st.get("session")

s = attached(None)
check("no calendar section means not checked", s and s["checked"] is False)
s = attached(S.Section("calendar", S.EMPTY, reason="no calendars configured — see calendars.txt",
                       detail={"checked": 0}))
check("an unconfigured calendar is not checked", s["checked"] is False)
check("it says a calendar is missing", "calendars.txt" in s["why"])
s = attached(S.Section("calendar", S.FAILED, reason="HTTP 500"))
check("a failed calendar is not checked", s["checked"] is False)
check("the failure reason is carried", "could not be read" in s["why"])
s = attached(S.Section("calendar", S.EMPTY, reason="nothing scheduled",
                       detail={"checked": 1, "ahead": {D2: []}, "horizon_days": 7, "scanned": True}))
check("an empty but scanned calendar does place a session", s["checked"] and s["found"])
s = attached(S.Section("calendar", S.OK, data=[ev(D1, 8, 0, 22, 0)],
                       detail={"checked": 1, "ahead": {D2: [ev(D2, 8, 0, 22, 0)]},
                               "horizon_days": 7, "scanned": True}))
check("a booked-out week reports no window", s["checked"] and s["found"] is False)

# A feed that parses but holds nothing is very likely a wrong URL, and would
# otherwise read as a gloriously free week.
s = attached(S.Section("calendar", S.EMPTY, reason="feed contains no events at all",
                       detail={"checked": 1, "ahead": {D2: []}, "horizon_days": 7,
                               "scanned": True, "suspect": ["Work"]}))
check("an empty feed still places a session", s["found"])
check("but the session is qualified", "no events at all" in (s.get("caveat") or ""))
check("the suspect calendar is named", "Work" in (s.get("caveat") or ""))
s = attached(S.Section("calendar", S.OK, data=[ev(D1, 9, 0, 10, 0)],
                       detail={"checked": 1, "ahead": {D2: []}, "horizon_days": 7,
                               "scanned": True, "suspect": []}))
check("a healthy calendar carries no caveat", s.get("caveat") is None)

# An unknown-length event on a SKIPPED day must still be named, or Thursday is
# mysteriously unavailable and the suggestion looks arbitrary.
_drop = {"all_day": False, "summary": "Client workshop", "end": None, "end_unknown": True,
         "start": dt.datetime.combine(D1, dt.time(8, 0)).astimezone()}
_s = P.propose_session({D1: [_drop], D2: []}, SETTINGS,
                       dt.datetime.combine(D1, dt.time(8, 0)).astimezone(), D2)
check("the session rolls past an unknown-length day", _s["found"] and _s["start"].date() == D2)
check("the skipped day's event is still named", _s["unknown_length"] == ["Client workshop"])

# The caveat must reach BOTH renderers, not just the HTML one.
_st = P.status(plan_with(), D1)
_secs = {"threexthree": S.Section("threexthree", S.OK, data=_st),
         "calendar": S.Section("calendar", S.OK, data=[_drop],
                               detail={"checked": 1, "ahead": {D2: []}, "horizon_days": 7,
                                       "scanned": True, "suspect": [], "stale": []})}
db.attach_session(_secs, D1, now=dt.datetime.combine(D1, dt.time(8, 0)).astimezone())
_CFG = {"sections": ["threexthree"], "units": {}, "location": {}, "feeds": []}
check("the caveat reaches the markdown brief",
      "no usable end time" in db.compose_markdown(_CFG, D1, _secs, []))
check("the caveat reaches the html brief",
      "no usable end time" in db.compose_page(_CFG, D1, _secs, [], {}, ""))

# A stale (cached) calendar is a reason to doubt a proposal, and must say so.
_secs2 = {"threexthree": S.Section("threexthree", S.OK, data=P.status(plan_with(), D1)),
          "calendar": S.Section("calendar", S.OK, data=[],
                                detail={"checked": 1, "ahead": {D2: []}, "horizon_days": 7,
                                        "scanned": True, "suspect": [],
                                        "stale": ["Work: cache 2h old"]})}
db.attach_session(_secs2, D1, now=dt.datetime.combine(D1, dt.time(8, 0)).astimezone())
check("a cached calendar qualifies the proposal",
      "cached calendar" in (_secs2["threexthree"].data["session"].get("caveat") or ""))

# The horizon stops the day BEFORE the next review, since the week's job rolls
# over then -- including when today IS the review day.
for _today, _label in ((dt.date(2026, 9, 9), "mid-week"), (dt.date(2026, 9, 13), "on review day")):
    _stt = P.status(plan_with(), _today)
    _sec = {"threexthree": S.Section("threexthree", S.OK, data=_stt),
            "calendar": S.Section("calendar", S.OK, data=[],
                                  detail={"checked": 1, "ahead": {}, "horizon_days": 7,
                                          "scanned": True, "suspect": [], "stale": []})}
    db.attach_session(_sec, _today, now=dt.datetime.combine(_today, dt.time(9, 0)).astimezone())
    _u = _stt["session"]["until"]
    check(f"horizon ends the day before the next review ({_label})",
          _u == _stt["weekly"]["this_week"] + dt.timedelta(days=6))

# --- the collector ----------------------------------------------------------

sec = S.threexthree(dt.date(2026, 9, 3), TMP / "nope.json")
check("no plan file collects as EMPTY", sec.status == S.EMPTY)
check("EMPTY names the command that fixes it", "3x3 init" in sec.reason)
check("the collector keys itself correctly", sec.key == "threexthree")

sec = S.threexthree(dt.date(2026, 9, 3), bad)
check("malformed plan collects as FAILED", sec.status == S.FAILED)
check("FAILED carries a reason", bool(sec.reason))

sec = S.threexthree(dt.date(2026, 9, 3), good)
check("a good plan collects as OK", sec.status == S.OK and sec.usable)
check("OK carries the computed block", sec.data["block"]["name"] == "Psychoacoustics")
check("elapsed_ms is recorded", isinstance(sec.elapsed_ms, int))

# The offline path calls this collector DIRECTLY, outside collect()'s pool, so an
# escaping exception would take down the whole brief. Nothing may escape.
crasher = TMP / "crasher.json"
crasher.write_text(json.dumps({"start": "2026-09-01", "review_day": "sunday",
                               "topics": [{"name": "T", "sources": {"video": 5},
                                           "output": "not-a-dict"}],
                               "weekly": {"changes": {"a": 1}}}), "utf-8")
sec = S.threexthree(dt.date(2026, 9, 3), crasher)
check("a hostile plan never raises out of the collector",
      sec.status in (S.OK, S.EMPTY, S.FAILED))
check("a hostile plan still records elapsed_ms", isinstance(sec.elapsed_ms, int))

gappy = TMP / "gappy.json"
P.save(gappy, plan_with(topics=[{"name": "Bare", "sources": {}}]))
sec = S.threexthree(dt.date(2026, 9, 3), gappy)
check("a half-filled plan is still OK", sec.status == S.OK)
check("its gaps travel on reason", "Bare" in sec.reason)

# --- rendering --------------------------------------------------------------

CFG = {"sections": ["threexthree"], "units": {}, "location": {}, "feeds": []}
full = plan_with(weekly={"week_of": "2026-09-06", "changes": ["x1", "x2", "x3"], "history": []})
day = dt.date(2026, 9, 9)
secs = {"threexthree": S.Section("threexthree", S.OK, data=P.status(full, day))}

md = db.compose_markdown(CFG, day, secs, [])
check("markdown has the 3x3 heading", "## 3x3" in md)
check("markdown names the topic", "Psychoacoustics" in md)
# 9 Sep is week 2, so the job is the text and not the video.
check("markdown states the week's job", "article, book" in md)
check("markdown links the week's source", "https://example.com/t" in md)
check("markdown does not show a later week's source", "https://example.com/v" not in md)
check("markdown lists the three", "1. x1" in md and "3. x3" in md)

html = db.compose_page(CFG, day, secs, [], {}, "")
check("html has the 3x3 section", "3×3" in html)
check("html names the topic", "Psychoacoustics" in html)
check("html links the week's source", 'href="https://example.com/t"' in html)
check("html numbers the three", ">x2<" in html)

# Week 1 serves the video; week 3's source has no url and must render as text.
html1 = db.compose_page(CFG, dt.date(2026, 9, 3), {"threexthree": S.Section(
    "threexthree", S.OK, data=P.status(full, dt.date(2026, 9, 3)))}, [], {}, "")
check("week 1 links the video", 'href="https://example.com/v"' in html1)
html3 = db.compose_page(CFG, dt.date(2026, 9, 17), {"threexthree": S.Section(
    "threexthree", S.OK, data=P.status(full, dt.date(2026, 9, 17)))}, [], {}, "")
check("week 3 names its source", "A Course" in html3)
check("a source with no url renders unlinked",
      '<span class="feature-title">A Course</span>' in html3)

# Week 4 renders the output job, not a phantom source.
w4 = {"threexthree": S.Section("threexthree", S.OK, data=P.status(full, dt.date(2026, 9, 24)))}
html4 = db.compose_page(CFG, dt.date(2026, 9, 24), w4, [], {}, "")
check("week 4 asks for the output", "Produce the output" in html4)
check("week 4 shows no source link", 'href="https://example.com/v"' not in html4)

# A failed section must say so in both renderers, never look quiet.
failed = {"threexthree": S.Section("threexthree", S.FAILED, reason="plan.json unreadable")}
check("markdown reports a failed plan",
      "unavailable" in db.compose_markdown(CFG, day, failed, []))
check("html reports a failed plan",
      "plan.json unreadable" in db.compose_page(CFG, day, failed, [], {}, ""))

empty = {"threexthree": S.Section("threexthree", S.EMPTY, reason="no plan yet - run init")}
check("html reports an unconfigured plan",
      "no plan yet" in db.compose_page(CFG, day, empty, [], {}, ""))

# A stale set must be visibly stale in the brief, not silently current.
stale_plan = plan_with(weekly={"week_of": "2026-08-30", "changes": ["s1"], "history": []})
stale_secs = {"threexthree": S.Section("threexthree", S.OK, data=P.status(stale_plan, day))}
stale_html = db.compose_page(CFG, day, stale_secs, [], {}, "")
check("html flags a stale set", "still showing" in stale_html)
check("markdown flags a stale set",
      "due for review" in db.compose_markdown(CFG, day, stale_secs, []))

import shutil
shutil.rmtree(TMP, ignore_errors=True)

bad_n = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(("  PASS " if v else "  FAIL ") + k)
print(f"\n{len(checks) - len(bad_n)}/{len(checks)} passed")
sys.exit(1 if bad_n else 0)
