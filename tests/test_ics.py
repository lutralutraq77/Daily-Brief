"""ICS engine against the researched test vectors + the nine silent-wrong bugs."""
import sys
from pathlib import Path
from datetime import date

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import icslib

checks = {}
def check(n, c): checks[n] = bool(c)

LONDON_VTZ = """BEGIN:VTIMEZONE
TZID:Europe/London
BEGIN:DAYLIGHT
TZOFFSETFROM:+0000
TZOFFSETTO:+0100
TZNAME:BST
DTSTART:19700329T010000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:+0100
TZOFFSETTO:+0000
TZNAME:GMT
DTSTART:19701025T020000
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
END:STANDARD
END:VTIMEZONE
"""

def cal(body, vtz=LONDON_VTZ):
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n" + vtz + body + "END:VCALENDAR"

def on(body, d, emails=None, vtz=LONDON_VTZ):
    return icslib.events_on(cal(body, vtz), d, emails)["events"]

def titles(body, d, **kw):
    return [e["summary"] for e in on(body, d, **kw)]

def hhmm(body, d):
    return [(e["start"].strftime("%H:%M") if e["start"] else "all-day") for e in on(body, d)]

# TV1 weekly standup
TV1 = """BEGIN:VEVENT
UID:standup@x
DTSTART;TZID=Europe/London:20240108T093000
DTEND;TZID=Europe/London:20240108T094500
RRULE:FREQ=WEEKLY;BYDAY=MO
SUMMARY:Standup
END:VEVENT
"""
check("TV1 Monday yes", titles(TV1, date(2026, 8, 10)) == ["Standup"])
check("TV1 Thursday no", titles(TV1, date(2026, 8, 13)) == [])
check("TV1 before DTSTART no", titles(TV1, date(2024, 1, 1)) == [])
check("TV1 time is 09:30", hhmm(TV1, date(2026, 8, 10)) == ["09:30"])

# TV2 fortnightly
TV2 = """BEGIN:VEVENT
UID:fort@x
DTSTART;TZID=Europe/London:20250107T140000
RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=TU
SUMMARY:Fortnightly
END:VEVENT
"""
check("TV2 anchor yes", titles(TV2, date(2025, 1, 7)) == ["Fortnightly"])
check("TV2 off-week no", titles(TV2, date(2025, 1, 14)) == [])
check("TV2 on-week yes", titles(TV2, date(2025, 1, 21)) == ["Fortnightly"])
check("TV2 far off-week no", titles(TV2, date(2026, 8, 11)) == [])
check("TV2 far on-week yes", titles(TV2, date(2026, 8, 18)) == ["Fortnightly"])

# TV3 second Tuesday
TV3 = """BEGIN:VEVENT
UID:board@x
DTSTART;TZID=Europe/London:20250211T190000
RRULE:FREQ=MONTHLY;BYDAY=2TU
SUMMARY:Board
END:VEVENT
"""
check("TV3 2nd Tue yes", titles(TV3, date(2026, 8, 11)) == ["Board"])
check("TV3 1st Tue no", titles(TV3, date(2026, 8, 4)) == [])
check("TV3 3rd Tue no", titles(TV3, date(2026, 8, 18)) == [])
check("TV3 next month yes", titles(TV3, date(2026, 9, 8)) == ["Board"])

# TV4 last Friday
TV4 = """BEGIN:VEVENT
UID:retro@x
DTSTART;TZID=Europe/London:20250131T160000
RRULE:FREQ=MONTHLY;BYDAY=-1FR
SUMMARY:Retro
END:VEVENT
"""
check("TV4 last Fri yes", titles(TV4, date(2026, 8, 28)) == ["Retro"])
check("TV4 penultimate Fri no", titles(TV4, date(2026, 8, 21)) == [])
check("TV4 Jan last Fri yes", titles(TV4, date(2026, 1, 30)) == ["Retro"])

# TV5 monthly on the 31st: skip short months, never clamp
TV5 = """BEGIN:VEVENT
UID:pay@x
DTSTART;TZID=Europe/London:20250131T090000
RRULE:FREQ=MONTHLY
SUMMARY:Payday
END:VEVENT
"""
check("TV5 Jan31 yes", titles(TV5, date(2025, 1, 31)) == ["Payday"])
check("TV5 Feb28 no (not clamped)", titles(TV5, date(2025, 2, 28)) == [])
check("TV5 Mar31 yes", titles(TV5, date(2025, 3, 31)) == ["Payday"])
check("TV5 Apr30 no", titles(TV5, date(2025, 4, 30)) == [])

# TV6 yearly 29 Feb
TV6 = """BEGIN:VEVENT
UID:leap@x
DTSTART;VALUE=DATE:20240229
RRULE:FREQ=YEARLY
SUMMARY:Leapday
END:VEVENT
"""
check("TV6 leap yes", titles(TV6, date(2024, 2, 29)) == ["Leapday"])
check("TV6 non-leap Feb28 no", titles(TV6, date(2025, 2, 28)) == [])
check("TV6 non-leap Mar01 no", titles(TV6, date(2025, 3, 1)) == [])
check("TV6 next leap yes", titles(TV6, date(2028, 2, 29)) == ["Leapday"])

# TV7 UNTIL in UTC vs local BST start (inclusive)
TV7 = """BEGIN:VEVENT
UID:until@x
DTSTART;TZID=Europe/London:20250602T090000
RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20250630T080000Z
SUMMARY:Sprint sync
END:VEVENT
"""
check("TV7 before until yes", titles(TV7, date(2025, 6, 23)) == ["Sprint sync"])
check("TV7 exactly until yes (inclusive)", titles(TV7, date(2025, 6, 30)) == ["Sprint sync"])
check("TV7 after until no", titles(TV7, date(2025, 7, 7)) == [])

# TV8 COUNT
TV8 = """BEGIN:VEVENT
UID:count@x
DTSTART;TZID=Europe/London:20260810T080000
RRULE:FREQ=DAILY;COUNT=5
SUMMARY:Course
END:VEVENT
"""
check("TV8 day1 yes", titles(TV8, date(2026, 8, 10)) == ["Course"])
check("TV8 day5 yes", titles(TV8, date(2026, 8, 14)) == ["Course"])
check("TV8 day6 no", titles(TV8, date(2026, 8, 15)) == [])

# TV8b COUNT + EXDATE: excluded occurrence still consumes a count
TV8B = """BEGIN:VEVENT
UID:ce@x
DTSTART;TZID=Europe/London:20260810T080000
RRULE:FREQ=DAILY;COUNT=5
EXDATE;TZID=Europe/London:20260812T080000
SUMMARY:Course
END:VEVENT
"""
check("TV8b excluded day no", titles(TV8B, date(2026, 8, 12)) == [])
check("TV8b day4 yes", titles(TV8B, date(2026, 8, 13)) == ["Course"])
check("TV8b day5 yes", titles(TV8B, date(2026, 8, 14)) == ["Course"])
check("TV8b day6 no", titles(TV8B, date(2026, 8, 15)) == [])

# TV9 EXDATE with TZID, and the same spelled in UTC
TV9 = """BEGIN:VEVENT
UID:ex@x
DTSTART;TZID=Europe/London:20260805T090000
RRULE:FREQ=WEEKLY;BYDAY=WE
EXDATE;TZID=Europe/London:20260812T090000
SUMMARY:Weekly 1:1
END:VEVENT
"""
check("TV9 normal week yes", titles(TV9, date(2026, 8, 5)) == ["Weekly 1:1"])
check("TV9 excluded week no", titles(TV9, date(2026, 8, 12)) == [])
check("TV9 following week yes", titles(TV9, date(2026, 8, 19)) == ["Weekly 1:1"])

TV9B = TV9.replace("EXDATE;TZID=Europe/London:20260812T090000", "EXDATE:20260812T080000Z")
check("TV9b UTC-spelled EXDATE excluded", titles(TV9B, date(2026, 8, 12)) == [])
check("TV9b following week yes", titles(TV9B, date(2026, 8, 19)) == ["Weekly 1:1"])

# TV10 moved instance must not appear twice
TV10 = """BEGIN:VEVENT
UID:mv@x
DTSTART;TZID=Europe/London:20260806T100000
DTEND;TZID=Europe/London:20260806T110000
RRULE:FREQ=WEEKLY;BYDAY=TH
SUMMARY:Design review
END:VEVENT
BEGIN:VEVENT
UID:mv@x
RECURRENCE-ID;TZID=Europe/London:20260813T100000
DTSTART;TZID=Europe/London:20260814T150000
DTEND;TZID=Europe/London:20260814T160000
SUMMARY:Design review (moved)
END:VEVENT
"""
check("TV10 master week yes", titles(TV10, date(2026, 8, 6)) == ["Design review"])
check("TV10 original slot suppressed", titles(TV10, date(2026, 8, 13)) == [])
check("TV10 moved slot shows once", titles(TV10, date(2026, 8, 14)) == ["Design review (moved)"])
check("TV10 moved time is 15:00", hhmm(TV10, date(2026, 8, 14)) == ["15:00"])
check("TV10 master resumes", titles(TV10, date(2026, 8, 20)) == ["Design review"])

# TV11 single instance cancelled via STATUS on the override
TV11 = """BEGIN:VEVENT
UID:cx@x
DTSTART;TZID=Europe/London:20260803T113000
RRULE:FREQ=WEEKLY;BYDAY=MO
SUMMARY:Team sync
END:VEVENT
BEGIN:VEVENT
UID:cx@x
RECURRENCE-ID;TZID=Europe/London:20260810T113000
DTSTART;TZID=Europe/London:20260810T113000
STATUS:CANCELLED
SUMMARY:Team sync
END:VEVENT
"""
check("TV11 normal week yes", titles(TV11, date(2026, 8, 3)) == ["Team sync"])
check("TV11 cancelled week shows nothing", titles(TV11, date(2026, 8, 10)) == [])
check("TV11 following week yes", titles(TV11, date(2026, 8, 17)) == ["Team sync"])

# MUST-FIX: whole cancelled standalone event must not print
CANC = """BEGIN:VEVENT
UID:c1@x
DTSTART;TZID=Europe/London:20260813T090000
STATUS:CANCELLED
SUMMARY:Cancelled thing
END:VEVENT
"""
check("cancelled standalone hidden", titles(CANC, date(2026, 8, 13)) == [])

# TV12 BYSETPOS last weekday of month
TV12 = """BEGIN:VEVENT
UID:sp@x
DTSTART;TZID=Europe/London:20250131T170000
RRULE:FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1
SUMMARY:Month end
END:VEVENT
"""
check("TV12 last weekday yes", titles(TV12, date(2026, 8, 31)) == ["Month end"])
check("TV12 earlier Friday no", titles(TV12, date(2026, 8, 28)) == [])
check("TV12 May last weekday yes", titles(TV12, date(2026, 5, 29)) == ["Month end"])
check("TV12 May Sunday no", titles(TV12, date(2026, 5, 31)) == [])

# TV13 multi-day ALL-DAY, DTEND exclusive
TV13 = """BEGIN:VEVENT
UID:hol@x
DTSTART;VALUE=DATE:20260812
DTEND;VALUE=DATE:20260815
SUMMARY:Conference
END:VEVENT
"""
check("TV13 day before no", titles(TV13, date(2026, 8, 11)) == [])
check("TV13 first day yes", titles(TV13, date(2026, 8, 12)) == ["Conference"])
check("TV13 middle day yes", titles(TV13, date(2026, 8, 13)) == ["Conference"])
check("TV13 last day yes", titles(TV13, date(2026, 8, 14)) == ["Conference"])
check("TV13 DTEND day itself no (exclusive)", titles(TV13, date(2026, 8, 15)) == [])

# MUST-FIX: TIMED multi-day event must survive past day one
TIMED_MULTI = """BEGIN:VEVENT
UID:trip@x
DTSTART;TZID=Europe/London:20260812T180000
DTEND;TZID=Europe/London:20260815T090000
SUMMARY:Offsite
END:VEVENT
"""
check("timed multi-day day1", titles(TIMED_MULTI, date(2026, 8, 12)) == ["Offsite"])
check("timed multi-day middle", titles(TIMED_MULTI, date(2026, 8, 13)) == ["Offsite"])
check("timed multi-day last", titles(TIMED_MULTI, date(2026, 8, 15)) == ["Offsite"])
check("timed multi-day after", titles(TIMED_MULTI, date(2026, 8, 16)) == [])

# TV14 quarterly INTERVAL=3
TV14 = """BEGIN:VEVENT
UID:q@x
DTSTART;TZID=Europe/London:20250115T100000
RRULE:FREQ=MONTHLY;INTERVAL=3;BYMONTHDAY=15
SUMMARY:Quarterly review
END:VEVENT
"""
check("TV14 anchor yes", titles(TV14, date(2025, 1, 15)) == ["Quarterly review"])
check("TV14 month+1 no", titles(TV14, date(2025, 2, 15)) == [])
check("TV14 month+3 yes", titles(TV14, date(2025, 4, 15)) == ["Quarterly review"])
check("TV14 far on-quarter yes", titles(TV14, date(2026, 7, 15)) == ["Quarterly review"])
check("TV14 far off-quarter no", titles(TV14, date(2026, 8, 15)) == [])

# TV15 all-day weekly, DATE-valued UNTIL and EXDATE
TV15 = """BEGIN:VEVENT
UID:ad@x
DTSTART;VALUE=DATE:20260803
RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20260817
EXDATE;VALUE=DATE:20260810
SUMMARY:Bin day
END:VEVENT
"""
check("TV15 first yes", titles(TV15, date(2026, 8, 3)) == ["Bin day"])
check("TV15 excluded no", titles(TV15, date(2026, 8, 10)) == [])
check("TV15 until day yes (inclusive)", titles(TV15, date(2026, 8, 17)) == ["Bin day"])
check("TV15 past until no", titles(TV15, date(2026, 8, 24)) == [])

# TV16 folded line
TV16 = ("BEGIN:VEVENT\r\nUID:fold@x\r\nDTSTART;TZID=Europe/London:20260805T090000\r\n"
        "RRULE:FREQ=MONT\r\n HLY;BYDAY=1WE\r\nSUMMARY:First Wednesday\r\nEND:VEVENT\r\n")
check("TV16 folded RRULE parses", titles(TV16, date(2026, 8, 5)) == ["First Wednesday"])
check("TV16 wrong week no", titles(TV16, date(2026, 8, 12)) == [])

# --- the nine silent-wrong bugs ---------------------------------------------

# 1. VALARM UID must not clobber the VEVENT UID
ALARM = """BEGIN:VEVENT
UID:real-uid@x
DTSTART;TZID=Europe/London:20260813T090000
SUMMARY:Real title
BEGIN:VALARM
UID:alarm-uid@x
ACTION:EMAIL
SUMMARY:Alarm subject
TRIGGER:-PT30M
END:VALARM
END:VEVENT
"""
ev = on(ALARM, date(2026, 8, 13))
check("VALARM does not clobber UID", ev and ev[0]["uid"] == "real-uid@x")
check("VALARM does not clobber SUMMARY", ev and ev[0]["summary"] == "Real title")

# 2. declined invitations hidden; tentative/no-attendee shown
DECL = """BEGIN:VEVENT
UID:d1@x
DTSTART;TZID=Europe/London:20260813T090000
SUMMARY:Declined meeting
ATTENDEE;PARTSTAT=DECLINED:mailto:Me@Example.com
END:VEVENT
BEGIN:VEVENT
UID:d2@x
DTSTART;TZID=Europe/London:20260813T100000
SUMMARY:Accepted meeting
ATTENDEE;PARTSTAT=ACCEPTED:mailto:me@example.com
END:VEVENT
BEGIN:VEVENT
UID:d3@x
DTSTART;TZID=Europe/London:20260813T110000
SUMMARY:No attendee info
END:VEVENT
"""
got = titles(DECL, date(2026, 8, 13), emails={"me@example.com"})
check("declined hidden", "Declined meeting" not in got)
check("accepted shown", "Accepted meeting" in got)
check("no-attendee shown", "No attendee info" in got)
check("case-insensitive email match", len(got) == 2)
check("without my email nothing is filtered", len(titles(DECL, date(2026, 8, 13))) == 3)

# 3. RDATE adds occurrences
RD = """BEGIN:VEVENT
UID:rd@x
DTSTART;TZID=Europe/London:20260803T090000
RRULE:FREQ=WEEKLY;BYDAY=MO
RDATE;TZID=Europe/London:20260813T160000
SUMMARY:With RDATE
END:VEVENT
"""
check("RDATE occurrence appears", titles(RD, date(2026, 8, 13)) == ["With RDATE"])
check("RDATE keeps its own time", hhmm(RD, date(2026, 8, 13)) == ["16:00"])

# 4. DURATION honoured for span
DUR = """BEGIN:VEVENT
UID:du@x
DTSTART;VALUE=DATE:20260812
DURATION:P3D
SUMMARY:Duration span
END:VEVENT
"""
check("DURATION span middle day", titles(DUR, date(2026, 8, 14)) == ["Duration span"])
check("DURATION span ends", titles(DUR, date(2026, 8, 15)) == [])

# 5. foreign TZID flagged, never rendered as a bare local time
FOREIGN = """BEGIN:VEVENT
UID:fg@x
DTSTART;TZID=America/Chicago:20260813T100000
SUMMARY:Foreign zone
END:VEVENT
"""
fev = on(FOREIGN, date(2026, 8, 13))
check("foreign TZID event still surfaces", len(fev) == 1)
check("foreign TZID is flagged", fev and any("America/Chicago" in w for w in fev[0]["warnings"]))

# 6. UTC-anchored recurrence expands in UTC
UTCR = """BEGIN:VEVENT
UID:z@x
DTSTART:20260810T233000Z
RRULE:FREQ=DAILY
SUMMARY:Late UTC
END:VEVENT
"""
zev = on(UTCR, date(2026, 8, 13))
check("UTC-anchored recurrence lands on the right local day", len(zev) == 1)
check("UTC-anchored time converted to local (00:30 BST)",
      zev and zev[0]["start"].strftime("%H:%M") == "00:30")

# 7. RFC 5545 text unescaping
ESC = """BEGIN:VEVENT
UID:e@x
DTSTART;TZID=Europe/London:20260813T090000
SUMMARY:Lunch\\, then review\\; bring notes
END:VEVENT
"""
check("text unescaped", titles(ESC, date(2026, 8, 13)) == ["Lunch, then review; bring notes"])

# 8. truncated and non-calendar bodies rejected loudly
for bad, label in ((cal(TV1)[:200], "truncated"), ("<!DOCTYPE html><html>hi</html>", "html")):
    try:
        icslib.events_on(bad, date(2026, 8, 13))
        check(f"{label} body rejected", False)
    except icslib.IcsError:
        check(f"{label} body rejected", True)

# 9. one malformed event does not lose the others
MIXED = """BEGIN:VEVENT
UID:bad@x
DTSTART;TZID=Europe/London:NOTADATE
SUMMARY:Broken
END:VEVENT
BEGIN:VEVENT
UID:good@x
DTSTART;TZID=Europe/London:20260813T090000
SUMMARY:Good one
END:VEVENT
"""
res = icslib.events_on(cal(MIXED), date(2026, 8, 13))
check("good event survives a broken sibling", [e["summary"] for e in res["events"]] == ["Good one"])
check("parse problem is reported", len(res["problems"]) == 1)

# --- LF-only body (the unfold trap) -----------------------------------------
lf_body = cal(TV1).replace("\r\n", "\n")
check("LF-only body parses", [e["summary"] for e in icslib.events_on(lf_body, date(2026, 8, 10))["events"]] == ["Standup"])

# --- DST correctness from VTIMEZONE, with no tzdata -------------------------
DST = """BEGIN:VEVENT
UID:dst@x
DTSTART;TZID=Europe/London:20260101T120000
RRULE:FREQ=MONTHLY;BYMONTHDAY=1
SUMMARY:Noon check
END:VEVENT
"""
jan = on(DST, date(2026, 1, 1))[0]["start"]
jul = on(DST, date(2026, 7, 1))[0]["start"]
check("January is GMT (+00:00)", jan.utcoffset().total_seconds() == 0)
check("July is BST (+01:00)", jul.utcoffset().total_seconds() == 3600)
check("both render as 12:00 wall clock",
      jan.strftime("%H:%M") == "12:00" and jul.strftime("%H:%M") == "12:00")

# --- ordering: all-day first, then by time ----------------------------------
ORDER = """BEGIN:VEVENT
UID:o1@x
DTSTART;TZID=Europe/London:20260813T140000
SUMMARY:Afternoon
END:VEVENT
BEGIN:VEVENT
UID:o2@x
DTSTART;VALUE=DATE:20260813
SUMMARY:All day
END:VEVENT
BEGIN:VEVENT
UID:o3@x
DTSTART;TZID=Europe/London:20260813T090000
SUMMARY:Morning
END:VEVENT
"""
check("ordering all-day then chronological",
      titles(ORDER, date(2026, 8, 13)) == ["All day", "Morning", "Afternoon"])

bad_n = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    if not v:
        print("  FAIL " + k)
print(f"\n{len(checks) - len(bad_n)}/{len(checks)} passed")
sys.exit(1 if bad_n else 0)
