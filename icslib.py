"""iCalendar parsing and recurrence, standard library only.

Written against a live Google Calendar feed and an adversarial review of two
earlier reference implementations. The bugs it deliberately avoids, all of which
produce a SILENTLY WRONG brief rather than an error:

- A VALARM's UID overwriting the VEVENT's UID. Properties are only read at
  component depth 1, so sub-components cannot clobber their parent. (Highest
  severity: it breaks master/override grouping and double-books a moved meeting.
  4 of 15 alarms in a real Google feed carry a UID.)
- STATUS:CANCELLED honoured only on overrides, so a cancelled standalone event
  or a cancelled series still prints. Showing a cancelled meeting is worse than
  showing nothing.
- Declined invitations shown as if you were attending. ATTENDEE;PARTSTAT is
  present in Google feeds and is filtered here.
- Timed multi-day events vanishing after their first day.
- RDATE (extra occurrences) dropped, and DURATION ignored.
- Foreign TZIDs rendered at the wrong hour. Offsets come from the feed's own
  VTIMEZONE blocks, so no IANA tz database is needed -- which matters because
  Windows has none and zoneinfo raises here.
- UTC-anchored recurrences expanded in local wall clock, an hour or a day out.
- Unfolding with str.replace('\\r\\n '), which fails silently on LF-only bodies.

Recurrence is answered by arithmetic, not expansion: for a target date we compute
the period index directly, so a daily event running since 2015 costs a
subtraction rather than 4000 iterations.
"""

from __future__ import annotations

import re
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
MAX_WALK = 4000          # hard cap for COUNT walks

# Rule parts we knowingly do not implement. Encountering one is reported in the
# brief rather than silently changing the answer.
UNSUPPORTED_PARTS = ("BYWEEKNO", "BYYEARDAY", "BYHOUR", "BYMINUTE", "BYSECOND")


class IcsError(Exception):
    pass


# ---------------------------------------------------------------- lexing


def unfold(text: str) -> list[str]:
    """RFC 5545 line unfolding, line-based.

    A continuation line begins with a space or tab. Doing this with
    text.replace('\\r\\n ', '') breaks on bodies that arrive with bare LF,
    leaving orphan continuations that parse as junk properties.
    """
    out: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and out:
            out[-1] += raw[1:]
        else:
            out.append(raw)
    return out


def split_line(line: str) -> tuple[str, dict, str]:
    # Find the head/value colon with the same quote-awareness the parameter
    # loop below uses: TZID="(UTC+01:00) Amsterdam" is legal and common.
    quoted = False
    i = -1
    for n, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            i = n
            break
    if i < 0:
        raise IcsError("property without a colon")
    head, value = line[:i], line[i + 1 :]
    # Parameters may contain quoted colons/semicolons, e.g. TZID="GMT+01:00".
    parts, buf, quoted = [], "", False
    for ch in head:
        if ch == '"':
            quoted = not quoted
        if ch == ";" and not quoted:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    parts.append(buf)
    params = {}
    for p in parts[1:]:
        k, _, v = p.partition("=")
        params[k.upper()] = v.strip('"')
    return parts[0].upper(), params, value


_UNESCAPE = re.compile(r"\\([\\;,nN])")


def unescape(value: str) -> str:
    """RFC 5545 TEXT unescaping. Must run after unfolding, never before."""
    return _UNESCAPE.sub(lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


# ---------------------------------------------------------------- timezones


@dataclass
class TzRule:
    offset_to: timedelta
    month: int = 0
    weekday: int = -1
    ordinal: int = 0
    monthday: int = 0
    at: time = time(2, 0)
    start: datetime | None = None       # naive local, for non-recurring blocks


@dataclass
class VTimezone:
    tzid: str
    rules: list[TzRule] = field(default_factory=list)

    def _transition(self, rule: TzRule, year: int) -> datetime | None:
        if rule.month == 0:
            return rule.start
        if rule.monthday:
            try:
                d = date(year, rule.month, rule.monthday)
            except ValueError:
                return None
        elif rule.weekday >= 0:
            d = _nth_weekday(year, rule.month, rule.weekday, rule.ordinal or 1)
            if d is None:
                return None
        else:
            return None
        return datetime.combine(d, rule.at)

    def offset_for(self, naive_local: datetime) -> timedelta:
        """The UTC offset in force at a wall-clock instant in this zone."""
        if not self.rules:
            return timedelta(0)
        if len(self.rules) == 1:
            return self.rules[0].offset_to
        best, best_when = None, None
        # Consider this year and last, so January falls to last year's autumn rule.
        for year in (naive_local.year, naive_local.year - 1):
            for rule in self.rules:
                when = self._transition(rule, year)
                if when is None or when > naive_local:
                    continue
                if best_when is None or when > best_when:
                    best, best_when = rule, when
        if best is None:
            # Before every transition: use whichever rule the earliest one left.
            return min(self.rules, key=lambda r: r.offset_to).offset_to
        return best.offset_to


def _nth_weekday(year: int, month: int, weekday: int, ordinal: int) -> date | None:
    last = monthrange(year, month)[1]
    days = [d for d in (date(year, month, i) for i in range(1, last + 1)) if d.weekday() == weekday]
    if not days:
        return None
    idx = ordinal - 1 if ordinal > 0 else len(days) + ordinal
    return days[idx] if 0 <= idx < len(days) else None


def _parse_offset(text: str) -> timedelta:
    text = text.strip()
    sign = -1 if text.startswith("-") else 1
    text = text.lstrip("+-")
    hours, minutes = int(text[0:2]), int(text[2:4])
    seconds = int(text[4:6]) if len(text) >= 6 else 0
    return sign * timedelta(hours=hours, minutes=minutes, seconds=seconds)


# ---------------------------------------------------------------- values


@dataclass
class DT:
    """A parsed DTSTART/DTEND.

    frame: 'date'    all-day, no time at all
           'utc'     had a trailing Z
           'wall'    had a TZID we resolved, or was floating (assumed local)
           'foreign' had a TZID with no VTIMEZONE -- time is NOT trustworthy
    """
    frame: str
    d: date | None = None
    naive: datetime | None = None
    tzid: str = ""
    offset: timedelta | None = None
    # Kept so each OCCURRENCE can resolve its own offset. Inheriting DTSTART's
    # offset renders a series that began in January an hour late all summer.
    zone: "VTimezone | None" = None

    @property
    def all_day(self) -> bool:
        return self.frame == "date"

    def local(self) -> datetime | None:
        """Aware datetime in the machine's local zone."""
        if self.naive is None:
            return None
        if self.frame == "utc":
            return self.naive.replace(tzinfo=timezone.utc).astimezone()
        if self.offset is not None:
            return self.naive.replace(tzinfo=timezone(self.offset)).astimezone()
        return self.naive.astimezone()      # floating: local wall clock

    def local_date(self) -> date | None:
        if self.frame == "date":
            return self.d
        got = self.local()
        return got.date() if got else None


def parse_dt(value: str, params: dict, zones: dict[str, VTimezone]) -> DT:
    value = value.strip()
    if params.get("VALUE") == "DATE" or (len(value) == 8 and "T" not in value):
        return DT("date", d=date(int(value[0:4]), int(value[4:6]), int(value[6:8])))
    naive = datetime(
        int(value[0:4]), int(value[4:6]), int(value[6:8]),
        int(value[9:11]), int(value[11:13]), int(value[13:15]),
    )
    if value.endswith("Z"):
        return DT("utc", naive=naive)
    tzid = params.get("TZID", "")
    if not tzid:
        return DT("wall", naive=naive)                 # floating -> local
    zone = zones.get(tzid)
    if zone is None:
        return DT("foreign", naive=naive, tzid=tzid)   # never render a bare time
    return DT("wall", naive=naive, tzid=tzid, offset=zone.offset_for(naive), zone=zone)


_DUR = re.compile(
    r"^(?P<sign>[+-])?P(?:(?P<w>\d+)W)?(?:(?P<d>\d+)D)?"
    r"(?:T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?)?$"
)


def parse_duration(value: str) -> timedelta | None:
    m = _DUR.match(value.strip())
    if not m:
        return None
    g = {k: int(v) if v else 0 for k, v in m.groupdict(default="0").items() if k != "sign"}
    delta = timedelta(weeks=g["w"], days=g["d"], hours=g["h"], minutes=g["m"], seconds=g["s"])
    return -delta if m.group("sign") == "-" else delta


# ---------------------------------------------------------------- events


@dataclass
class VEvent:
    uid: str = ""
    summary: str = ""
    location: str = ""
    status: str = ""
    start: DT | None = None
    end: DT | None = None
    duration: timedelta | None = None
    rrule: dict = field(default_factory=dict)
    exdates: list[DT] = field(default_factory=list)
    rdates: list[DT] = field(default_factory=list)
    recurrence_id: DT | None = None
    attendees: list[tuple[str, str]] = field(default_factory=list)   # (email, partstat)
    warnings: list[str] = field(default_factory=list)

    def span_days(self) -> int:
        """How many calendar days the event covers, minimum 1.

        DTEND is EXCLUSIVE for all-day events: a one-day event on the 13th has
        DTEND 20260814. Treating that as inclusive makes every all-day event
        appear to span two days.
        """
        if self.start is None:
            return 1
        if self.start.all_day:
            if self.end is not None and self.end.all_day and self.end.d and self.start.d:
                return max(1, (self.end.d - self.start.d).days)
            if self.duration:
                return max(1, self.duration.days)
            return 1
        s = self.start.local()
        e = self.end.local() if self.end else (s + self.duration if s and self.duration else None)
        if s is None or e is None:
            return 1
        # Half-open: an event ending exactly at midnight does not touch the next day.
        return max(1, (e.date() - s.date()).days + (0 if e.time() == time(0, 0) and e.date() > s.date() else 1))


def parse_calendar(text: str) -> tuple[list[VEvent], dict[str, VTimezone], list[str]]:
    """Parse a whole VCALENDAR. Returns (events, timezones, problems).

    A malformed event is dropped and counted, never allowed to abort the run --
    but the count is surfaced so the loss is visible rather than silent.
    """
    lines = unfold(text)
    zones: dict[str, VTimezone] = {}
    events: list[VEvent] = []
    problems: list[str] = []

    stack: list[str] = []
    ev_lines: list[tuple[str, dict, str]] = []
    tz_current: VTimezone | None = None
    tz_sub: dict | None = None

    for line in lines:
        if not line.strip():
            continue
        try:
            name, params, value = split_line(line)
        except IcsError:
            continue

        if name == "BEGIN":
            comp = value.strip().upper()
            stack.append(comp)
            if comp == "VEVENT" and len(stack) == 2:
                ev_lines = []
            elif comp == "VTIMEZONE":
                tz_current = VTimezone(tzid="")
            elif comp in ("STANDARD", "DAYLIGHT") and tz_current is not None:
                tz_sub = {"offset_to": None, "rrule": {}, "dtstart": None}
            continue

        if name == "END":
            comp = value.strip().upper()
            if comp == "VEVENT" and len(stack) == 2:
                try:
                    ev = _build_event(ev_lines, zones)
                    if ev is not None:
                        events.append(ev)
                except Exception as exc:  # noqa: BLE001 - one bad event must not lose the rest
                    problems.append(str(exc)[:120])
            elif comp in ("STANDARD", "DAYLIGHT") and tz_current is not None and tz_sub:
                try:
                    _finish_tz_rule(tz_current, tz_sub)
                except (ValueError, KeyError, IndexError) as exc:
                    # A garbage transition rule drops that rule; the zone keeps
                    # the rest. Losing the calendar over it is not a trade.
                    problems.append(f"{tz_current.tzid or 'VTIMEZONE'} rule: {exc}"[:120])
                tz_sub = None
            elif comp == "VTIMEZONE" and tz_current is not None:
                if tz_current.tzid:
                    zones[tz_current.tzid] = tz_current
                tz_current = None
            if stack:
                stack.pop()
            continue

        # VTIMEZONE internals
        if tz_current is not None:
            if tz_sub is not None:
                try:
                    if name == "TZOFFSETTO":
                        tz_sub["offset_to"] = _parse_offset(value)
                    elif name == "RRULE":
                        tz_sub["rrule"] = parse_rrule(value)
                    elif name == "DTSTART":
                        tz_sub["dtstart"] = value.strip()
                except (ValueError, KeyError, IndexError) as exc:
                    problems.append(f"{tz_current.tzid or 'VTIMEZONE'} {name}: {exc}"[:120])
            elif name == "TZID":
                tz_current.tzid = value.strip()
            continue

        # Only read properties at the VEVENT's own depth. A VALARM nested inside
        # must not be able to overwrite UID or SUMMARY.
        if len(stack) == 2 and stack[-1] == "VEVENT":
            ev_lines.append((name, params, value))

    return events, zones, problems


def _finish_tz_rule(zone: VTimezone, sub: dict) -> None:
    if sub["offset_to"] is None:
        return
    rule = TzRule(offset_to=sub["offset_to"])
    R = sub["rrule"]
    if R:
        rule.month = int(R.get("BYMONTH", 0) or 0)
        if "BYDAY" in R:
            tok = R["BYDAY"].split(",")[0].strip()
            rule.weekday = WEEKDAYS.get(tok[-2:], -1)
            head = tok[:-2].replace("+", "")
            rule.ordinal = int(head) if head else 1
        if "BYMONTHDAY" in R:
            rule.monthday = int(R["BYMONTHDAY"].split(",")[0])
    if sub["dtstart"]:
        v = sub["dtstart"]
        try:
            rule.at = time(int(v[9:11]), int(v[11:13]), int(v[13:15]))
            rule.start = datetime(int(v[0:4]), int(v[4:6]), int(v[6:8]),
                                  rule.at.hour, rule.at.minute, rule.at.second)
        except (ValueError, IndexError):
            pass
    zone.rules.append(rule)


def parse_rrule(value: str) -> dict:
    out = {}
    for kv in value.split(";"):
        k, _, v = kv.partition("=")
        if k:
            out[k.strip().upper()] = v.strip()
    return out


def _build_event(props: list[tuple[str, dict, str]], zones: dict) -> VEvent | None:
    ev = VEvent()
    for name, params, value in props:
        if name == "UID":
            ev.uid = value.strip()
        elif name == "SUMMARY":
            ev.summary = unescape(value).strip()
        elif name == "LOCATION":
            ev.location = unescape(value).strip()
        elif name == "STATUS":
            ev.status = value.strip().upper()
        elif name == "DTSTART":
            ev.start = parse_dt(value, params, zones)
        elif name == "DTEND":
            ev.end = parse_dt(value, params, zones)
        elif name == "DURATION":
            ev.duration = parse_duration(value)
        elif name == "RRULE":
            ev.rrule = parse_rrule(value)
        elif name == "EXDATE":
            for part in value.split(","):
                if part.strip():
                    ev.exdates.append(parse_dt(part, params, zones))
        elif name == "RDATE":
            for part in value.split(","):
                if part.strip():
                    ev.rdates.append(parse_dt(part, params, zones))
        elif name == "RECURRENCE-ID":
            ev.recurrence_id = parse_dt(value, params, zones)
        elif name == "ATTENDEE":
            email = value.strip()
            if email.lower().startswith("mailto:"):
                email = email[7:]
            ev.attendees.append((email.lower(), params.get("PARTSTAT", "").upper()))

    if ev.start is None:
        return None
    if ev.start.frame == "foreign":
        ev.warnings.append(f"unresolved timezone {ev.start.tzid}")
    for part in UNSUPPORTED_PARTS:
        if part in ev.rrule:
            ev.warnings.append(f"unsupported rule part {part}")
    return ev


# ---------------------------------------------------------------- recurrence


def _byday(spec: str) -> list[tuple[int | None, int]]:
    out = []
    for tok in spec.split(","):
        tok = tok.strip()
        if len(tok) < 2 or tok[-2:] not in WEEKDAYS:
            continue
        head = tok[:-2].replace("+", "")
        out.append((int(head) if head else None, WEEKDAYS[tok[-2:]]))
    return out


def _bymonthday_ok(d: date, vals: list[int]) -> bool:
    last = monthrange(d.year, d.month)[1]
    return any((v > 0 and d.day == v) or (v < 0 and d.day == last + v + 1) for v in vals)


def _byday_ok_month(d: date, pairs) -> bool:
    last = monthrange(d.year, d.month)[1]
    pos = (d.day - 1) // 7 + 1
    neg = -(((last - d.day) // 7) + 1)
    return any(d.weekday() == wd and (o is None or o == pos or o == neg) for o, wd in pairs)


def _setpos(days: list[date], spec: str) -> list[date]:
    picked = []
    for p in (int(x) for x in spec.split(",")):
        idx = p - 1 if p > 0 else len(days) + p
        if 0 <= idx < len(days):
            picked.append(days[idx])
    return sorted(set(picked))


def _month_days(year: int, month: int, R: dict, anchor: date) -> list[date]:
    if "BYMONTH" in R and month not in [int(x) for x in R["BYMONTH"].split(",")]:
        return []
    last = monthrange(year, month)[1]
    days = [date(year, month, i) for i in range(1, last + 1)]
    if "BYMONTHDAY" in R:
        vals = [int(x) for x in R["BYMONTHDAY"].split(",")]
        days = [d for d in days if _bymonthday_ok(d, vals)]
        if "BYDAY" in R:
            pairs = _byday(R["BYDAY"])
            days = [d for d in days if any(d.weekday() == wd for _, wd in pairs)]
    elif "BYDAY" in R:
        days = [d for d in days if _byday_ok_month(d, _byday(R["BYDAY"]))]
    else:
        days = [d for d in days if d.day == anchor.day]
    if "BYSETPOS" in R:
        days = _setpos(days, R["BYSETPOS"])
    return days


def _period_days(index: int, R: dict, anchor: date) -> list[date]:
    """The candidate dates in period `index` (0 = the period containing DTSTART)."""
    freq = R.get("FREQ", "").upper()
    step = max(1, int(R.get("INTERVAL", 1) or 1))

    if freq == "DAILY":
        d = anchor + timedelta(days=index * step)
        days = [d]
        if "BYDAY" in R:
            wds = {wd for _, wd in _byday(R["BYDAY"])}
            days = [x for x in days if x.weekday() in wds]
        if "BYMONTHDAY" in R:
            vals = [int(x) for x in R["BYMONTHDAY"].split(",")]
            days = [x for x in days if _bymonthday_ok(x, vals)]
        if "BYMONTH" in R:
            months = [int(x) for x in R["BYMONTH"].split(",")]
            days = [x for x in days if x.month in months]
        return days

    if freq == "WEEKLY":
        wkst = WEEKDAYS.get(R.get("WKST", "MO").upper(), 0)
        start_week = anchor - timedelta(days=(anchor.weekday() - wkst) % 7)
        week = start_week + timedelta(weeks=index * step)
        wds = {wd for _, wd in _byday(R["BYDAY"])} if "BYDAY" in R else {anchor.weekday()}
        days = [week + timedelta(days=i) for i in range(7)]
        days = [d for d in days if d.weekday() in wds]
        if "BYMONTH" in R:
            months = [int(x) for x in R["BYMONTH"].split(",")]
            days = [d for d in days if d.month in months]
        if "BYSETPOS" in R:
            days = _setpos(days, R["BYSETPOS"])
        return days

    if freq == "MONTHLY":
        total = anchor.year * 12 + (anchor.month - 1) + index * step
        return _month_days(total // 12, total % 12 + 1, R, anchor)

    if freq == "YEARLY":
        year = anchor.year + index * step
        if any(k in R for k in ("BYMONTH", "BYMONTHDAY", "BYDAY")):
            months = ([int(x) for x in R["BYMONTH"].split(",")]
                      if "BYMONTH" in R else [anchor.month])
            out: list[date] = []
            inner = {k: v for k, v in R.items() if k != "BYMONTH"}
            for m in months:
                out += _month_days(year, m, inner, anchor)
            return sorted(out)
        try:
            return [date(year, anchor.month, anchor.day)]     # 29 Feb: skip, never clamp
        except ValueError:
            return []
    return []


def _period_index(target: date, R: dict, anchor: date) -> int | None:
    """Which period `target` falls in, or None if it cannot be in the series.

    Arithmetic, not iteration: a daily event running since 2015 costs one
    subtraction. Day maths is done on DATE objects only -- doing it on aware
    datetimes drifts by one across a DST boundary and flips INTERVAL tests.
    """
    freq = R.get("FREQ", "").upper()
    step = max(1, int(R.get("INTERVAL", 1) or 1))
    if target < anchor and freq != "WEEKLY":
        # A weekly anchor can start mid-week, so allow the containing week.
        return None
    if freq == "DAILY":
        delta = (target - anchor).days
        return delta // step if delta >= 0 and delta % step == 0 else None
    if freq == "WEEKLY":
        wkst = WEEKDAYS.get(R.get("WKST", "MO").upper(), 0)
        a = anchor - timedelta(days=(anchor.weekday() - wkst) % 7)
        t = target - timedelta(days=(target.weekday() - wkst) % 7)
        weeks = (t - a).days // 7
        return weeks // step if weeks >= 0 and weeks % step == 0 else None
    if freq == "MONTHLY":
        months = (target.year - anchor.year) * 12 + (target.month - anchor.month)
        return months // step if months >= 0 and months % step == 0 else None
    if freq == "YEARLY":
        years = target.year - anchor.year
        return years // step if years >= 0 and years % step == 0 else None
    return None


def _combine(day: date, src: DT) -> datetime | None:
    """Rebuild an occurrence's local datetime, expanding in the anchor's frame.

    A Z-anchored rule expanded in local wall clock is an hour wrong for half the
    year, and lands on the wrong DAY near midnight.
    """
    if src.naive is None:
        return None
    naive = datetime.combine(day, src.naive.time())
    if src.frame == "utc":
        return naive.replace(tzinfo=timezone.utc).astimezone()
    if src.zone is not None:
        # Resolve the offset for THIS occurrence, not for DTSTART.
        off = src.zone.offset_for(naive)
        return naive.replace(tzinfo=timezone(off)).astimezone()
    if src.offset is not None:
        return naive.replace(tzinfo=timezone(src.offset)).astimezone()
    return naive.astimezone()


def _anchor_date(src: DT) -> date | None:
    """The DTSTART date in the frame the rule is anchored in."""
    if src.frame == "date":
        return src.d
    if src.naive is None:
        return None
    return src.naive.date()      # UTC date for Z, wall date for TZID/floating


def _until_instant(R: dict, zones: dict) -> datetime | None:
    raw = R.get("UNTIL")
    if not raw:
        return None
    got = parse_dt(raw, {}, zones)
    if got.frame == "date":
        return datetime.combine(got.d, time(23, 59, 59)).astimezone()
    return got.local()


def occurrences_on(ev: VEvent, target: date, zones: dict, overrides: set) -> list[dict]:
    """Every occurrence of this event visible on `target`, as local datetimes."""
    if ev.start is None or ev.status == "CANCELLED":
        return []

    span = ev.span_days()
    results: list[dict] = []

    # Length of one occurrence, applied to every generated start.
    #
    # Same frame on both ends -> the naive (wall-clock) diff, so a recurring
    # meeting keeps its duration across a DST change. Mixed frames -- a
    # Z-anchored DTEND against a TZID DTSTART, or a cross-zone flight -- must
    # be diffed as aware instants, or the duration is off by the zone gap.
    length: timedelta | None = None
    end_warning = ""
    if ev.start.naive is not None:
        if ev.end is not None and ev.end.naive is not None:
            if ev.end.frame == "foreign":
                end_warning = f"unresolved end timezone {ev.end.tzid} — end dropped"
            elif ev.end.frame == ev.start.frame and ev.end.tzid == ev.start.tzid:
                length = ev.end.naive - ev.start.naive
            else:
                s_aware, e_aware = ev.start.local(), ev.end.local()
                if s_aware is not None and e_aware is not None:
                    length = e_aware - s_aware
        elif ev.duration is not None:
            length = ev.duration
    if length is not None and length <= timedelta(0):
        # An end at or before its start is producer garbage; dropping it
        # silently would hide the problem, so say so on the occurrence.
        end_warning = end_warning or "end not after start — end dropped"
        length = None

    def emit(start_local, all_day, began_on: date):
        """began_on is the day this OCCURRENCE started, which for a multi-day
        event is earlier than the day being rendered."""
        end_local = None
        last_day = None
        if all_day and span > 1:
            # Inclusive final day: DTEND is exclusive in ICS.
            last_day = began_on + timedelta(days=span - 1)
        if start_local is not None and length is not None:
            end_local = start_local + length
        warnings = list(ev.warnings)
        if end_warning:
            warnings.append(end_warning)
        results.append({
            "start": start_local,
            "end": end_local,
            "all_day": all_day,
            "last_day": last_day,
            "date": began_on,
            "summary": ev.summary,
            "location": ev.location,
            "uid": ev.uid,
            "warnings": warnings,
            # end is None for two different reasons, and a consumer deciding
            # whether time is free must not confuse them: no DTEND at all is a
            # genuine zero-length instant (RFC 5545), whereas a DTEND we parsed
            # and then refused means the duration is UNKNOWN. Only the second
            # sets this.
            "end_unknown": bool(end_warning),
        })

    def excluded(day: date, start_local) -> bool:
        for ex in ev.exdates:
            if ex.frame == "date":
                if ex.d == day:
                    return True
            else:
                got = ex.local()
                if got and start_local and abs((got - start_local).total_seconds()) < 60:
                    return True
                if got and got.date() == day and start_local is None:
                    return True
        return False

    def suppressed(start_local, day: date) -> bool:
        # A moved or cancelled single occurrence is carried by a separate VEVENT
        # with RECURRENCE-ID. Without this the master still emits it and the
        # event shows twice, at both the old and new time.
        for key_uid, key_dt, key_date in overrides:
            if key_uid != ev.uid:
                continue
            if key_date is not None and key_date == day:
                return True
            if key_dt is not None and start_local is not None:
                if abs((key_dt - start_local).total_seconds()) < 60:
                    return True
            if key_dt is not None and start_local is None and key_dt.date() == day:
                return True
        return False

    # --- non-recurring, including RECURRENCE-ID overrides -------------------
    if not ev.rrule:
        first = ev.start.local_date()
        if first is None:
            return []
        for offset in range(span):
            day = first + timedelta(days=offset)
            if day != target:
                continue
            emit(None if ev.start.all_day else ev.start.local(), ev.start.all_day, first)
        # RDATEs on a non-recurring event still add occurrences -- and each
        # added occurrence carries the event's full span, so a 3-day block
        # added via RDATE stays visible on its middle and last days too.
        for rd in ev.rdates:
            rd_day = rd.local_date()
            if rd_day is not None and 0 <= (target - rd_day).days < span:
                # RFC 5545: EXDATE removes occurrences from RDATE too, not just
                # from RRULE. Pass the same value emit() gets -- excluded()
                # compares instants with a <60s tolerance.
                start_local = None if rd.all_day else rd.local()
                if excluded(rd_day, start_local):
                    continue
                emit(start_local, rd.all_day, rd_day)
        return results

    anchor = _anchor_date(ev.start)
    if anchor is None:
        return []
    until = _until_instant(ev.rrule, zones)
    count = int(ev.rrule["COUNT"]) if ev.rrule.get("COUNT", "").isdigit() else None

    # A multi-day recurring event is visible on days after its own start, so
    # test each day the span could have begun on.
    # (day, source) -- an RDATE carries its OWN time, which is usually different
    # from the series' time, so it cannot be rebuilt from DTSTART.
    candidates: dict[date, DT] = {}
    for back in range(span):
        day = target - timedelta(days=back)
        idx = _period_index(day, ev.rrule, anchor)
        if idx is None or idx < 0:
            continue
        if day in _period_days(idx, ev.rrule, anchor):
            candidates[day] = ev.start

    for rd in ev.rdates:
        rd_day = rd.local_date()
        if rd_day is not None and 0 <= (target - rd_day).days < span:
            candidates[rd_day] = rd

    for day in sorted(candidates):
        src = candidates[day]
        is_rdate = src is not ev.start
        if day < anchor and not is_rdate:
            continue
        start_local = None if src.all_day else _combine(day, src)
        # UNTIL and COUNT bound the RRULE, not explicitly-added RDATEs.
        if not is_rdate:
            if until is not None:
                probe = start_local or datetime.combine(day, time(0, 0)).astimezone()
                if probe > until:
                    continue
            if count is not None and not _within_count(ev, day, anchor, count):
                continue
        if excluded(day, start_local):
            continue
        if suppressed(start_local, day):
            continue
        emit(start_local, src.all_day, day)

    return results


def _within_count(ev: VEvent, day: date, anchor: date, count: int) -> bool:
    """COUNT is the only part needing a walk, and COUNT itself bounds it.

    EXDATE does NOT consume a count: an excluded occurrence still uses one of
    the N, so a COUNT=5 series with one EXDATE yields four visible occurrences.
    """
    seen = 0
    index = 0
    while seen < count and index < MAX_WALK:
        for candidate in _period_days(index, ev.rrule, anchor):
            if candidate < anchor:
                continue
            seen += 1
            if candidate == day:
                return seen <= count
            if seen >= count:
                break
        index += 1
    return False


def collect_overrides(events: list[VEvent]) -> set:
    """Keys identifying occurrences replaced or cancelled by an override."""
    keys = set()
    for ev in events:
        if ev.recurrence_id is None:
            continue
        rid = ev.recurrence_id
        keys.add((ev.uid, None if rid.frame == "date" else rid.local(),
                  rid.d if rid.frame == "date" else None))
    return keys


def declined_by(ev: VEvent, my_emails: set[str]) -> bool:
    """True only when the user is explicitly listed as having declined.

    No ATTENDEE line means no information, which is not the same as attending --
    so those events are shown.
    """
    if not my_emails:
        return False
    for email, partstat in ev.attendees:
        if email in my_emails and partstat == "DECLINED":
            return True
    return False


def _expand(text: str, targets: list[date], my_emails: set[str] | None) -> dict:
    """Expand every event across `targets`, parsing the feed exactly once.

    The parse is by far the expensive half -- a 3.3 MB feed is 181 ms -- so a
    week must not be built by calling events_on() seven times.
    """
    body = text.lstrip("﻿").strip()
    if not body.startswith("BEGIN:VCALENDAR"):
        raise IcsError("body is not an iCalendar feed")
    if not body.endswith("END:VCALENDAR"):
        # A truncated response parses cleanly as a shorter calendar, and feeds
        # are not chronologically ordered, so the missing tail is arbitrary.
        raise IcsError("feed is truncated (no END:VCALENDAR)")

    events, zones, problems = parse_calendar(body)
    overrides = collect_overrides(events)
    mine = {e.lower() for e in (my_emails or set())}

    by_day: dict[date, list[dict]] = {t: [] for t in targets}
    declined = 0
    for ev in events:
        if declined_by(ev, mine):
            declined += 1
            continue
        # An override that only cancels an occurrence suppresses it and emits
        # nothing itself.
        if ev.recurrence_id is not None and ev.status == "CANCELLED":
            continue
        try:
            for target in targets:
                by_day[target].extend(occurrences_on(ev, target, zones, overrides))
        except (ValueError, KeyError, OverflowError, IndexError) as exc:
            # A malformed RRULE/RDATE costs its own event, never the feed --
            # the same rule the per-event build guard keeps at line 332.
            problems.append(f"{ev.summary or ev.uid}: {exc}"[:120])

    for day in by_day.values():
        day.sort(key=lambda o: (not o["all_day"],
                                o["start"] or datetime.min.replace(tzinfo=timezone.utc)))
    return {
        "by_day": by_day,
        "total_events": len(events),
        "problems": problems,
        "declined": declined,
        "timezones": sorted(zones),
    }


def events_on(text: str, target: date, my_emails: set[str] | None = None) -> dict:
    """Everything on `target`, plus what went wrong getting there."""
    got = _expand(text, [target], my_emails)
    return {
        "events": got["by_day"][target],
        "total_events": got["total_events"],
        "problems": got["problems"],
        "declined": got["declined"],
        "timezones": got["timezones"],
    }


def events_between(text: str, start: date, end: date,
                   my_emails: set[str] | None = None) -> dict:
    """Every occurrence from `start` to `end` inclusive, keyed by date.

    Same shape as events_on() but with `by_day` in place of `events`. Used to
    find genuinely free time rather than merely to report today.
    """
    if end < start:
        raise IcsError("end is before start")
    span = (end - start).days
    targets = [start + timedelta(days=i) for i in range(span + 1)]
    return _expand(text, targets, my_emails)
