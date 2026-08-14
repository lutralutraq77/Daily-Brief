# Daily Brief

Builds a daily briefing from keyless public APIs, renders it as a standalone
HTML page, and announces it with a Windows toast. Click the toast, the brief
opens in a chromeless window.

**No credentials. No API keys. No sign-in. No cost.** Python 3.13 standard
library only — the HTTP layer, the feed parsers, the markdown renderer, the PNG
icon writer and the toast bridge are all stdlib + PowerShell.

Claude is optional. If a credential happens to exist, it can write the prose
instead; if it does not, you get the same data rendered locally.

---

## Setup

```bash
python dailybrief.py setup "Your City" --country GB
```

That is the only setup step. It resolves the city once, stores the coordinates,
and picks the right bank-holiday calendar (Scotland's genuinely differs from
England & Wales). Ambiguous names print a numbered pick-list — plain "Newcastle"
resolves to New South Wales, Australia on population alone, so `--country GB`
and `--pick N` are there for that.

Then:

```bash
python dailybrief.py run --force --open
```

To have it run every morning by itself:

```bash
powershell -ExecutionPolicy Bypass -File install.ps1 -Time 08:00
```

**What that installer touches**, so it holds no surprises: it registers a
per-user scheduled task called *Daily Brief*, two `HKCU` keys (a toast app
identity and a `dailybrief:` URL handler), and Start Menu / Desktop shortcuts.
All of it is user-scope — no administrator rights, nothing written outside your
own profile — and `uninstall.ps1` removes every piece of it. You do not need the
installer to use the program; the commands above work on their own.

---

## Location and unit settings

Everything lives in `config.json`. **Edit it and save — changes take effect on
the next run.** There is no need to re-run `setup`: if you change `city` or
`postcode`, the stored coordinates no longer match a fingerprint of what they
were derived from, so they are re-resolved automatically. An unchanged config
makes no lookup at all.

```jsonc
"location": {
  "city": "Edinburgh",     // a city name...
  "postcode": "",          // ...or a UK postcode. Postcode wins if both are set.
  "country": "GB",         // ISO code, disambiguates a city name
  "latitude": 55.9521,     // written for you; or set these yourself and clear
  "longitude": -3.1965,    //   city + postcode to pin exact coordinates
  "label": "Edinburgh, Scotland, United Kingdom",   // written for you
  "resolved_from": "|edinburgh|gb"                  // written for you, do not edit
},
"units": {
  "temperature": "celsius",   // celsius | fahrenheit
  "wind": "mph",              // mph | kmh | ms | kn
  "precipitation": "mm",      // mm | inch
  "clock": "24h"              // 24h | 12h  (sunrise/sunset)
}
```

Precedence is **latitude/longitude > postcode > city**. A postcode is more
precise than a city name and also determines the correct bank-holiday calendar
from the actual country rather than guessing.

Or do it from the command line:

```bash
python dailybrief.py setup --postcode "EH1 1YZ" --temperature fahrenheit --clock 12h
```

`dailybrief.py status` shows the current settings and flags `(CHANGED — re-resolves
next run)` when you have edited them.

Two safeguards worth knowing:

- If a place stops resolving (typo, or the geocoder is down) but coordinates were
  cached, the brief still runs on the old coordinates **and says so at the top**
  rather than silently reporting the wrong city's weather.
- `config.json` is read as `utf-8-sig`, because Notepad and PowerShell's
  `Set-Content -Encoding UTF8` both write a UTF-8 BOM. Plain UTF-8 decoding
  fails on that, which would throw away your whole config silently.
  Units are requested from the API in the units you asked for rather than
  converted afterwards, so there is no rounding drift.

---

## Google Calendar

This is the one feature that cannot be credential-free — reading a private
calendar means proving it is you. It uses Google's **secret iCal URL**, which
avoids OAuth entirely: no Cloud Console project, no client secret, and no
refresh token to expire and break the 08:00 run.

Open `calendars.txt`. It has the click-by-click path (Google Calendar →
Settings → *your calendar* → Integrate calendar → "Secret address in iCal
format"), then paste the URL on its own line. Check it with:

```bash
python dailybrief.py calendar
```

**That URL is a bearer token.** Anyone holding it can read every detail of that
calendar, forever, without signing in — so it lives in `calendars.txt`, never in
`config.json`, which is the file you would open, edit and paste into a support
thread. The program registers it as a secret and scrubs it from the log, the
toast, `state.json`, the rendered brief, and the payload sent to Claude when the
Claude engine is on. It is never used to name a cache file.

Two things that surprise people: **one URL is one calendar**, not your account —
add a line per calendar. And a calendar you merely *subscribe* to has no secret
address you can use; only the owner has one.

Prefer no URL at all? A plain file path works too, so you can export an `.ics`
by hand and keep the whole thing offline.

Set `"calendar_emails": ["you@gmail.com"]` in `config.json` so invitations you
have **declined** are not shown as though you were going.

### What it gets right

Recurrence is the hard part, and most of these fail silently rather than loudly:

- **Cancelled** occurrences and cancelled series are hidden. Showing a meeting
  that was cancelled is worse than showing nothing.
- A **moved** occurrence (`RECURRENCE-ID`) appears once, at its new time — not
  twice, at both.
- **Declined** invitations are dropped; no attendee information means the event
  is shown, since absence of data is not a decline.
- All-day `DTEND` is **exclusive** — a one-day event does not span two.
- **Timed** multi-day events survive past their first day.
- `EXDATE` still consumes a `COUNT`, so `COUNT=5` with one exclusion yields four.
- 29 February yearly events **skip** non-leap years rather than clamping to the 28th.
- "Second Tuesday", "last Friday", `BYSETPOS`, `INTERVAL`, and UTC-vs-local
  `UNTIL` all behave.
- Timezones come from the feed's own `VTIMEZONE` blocks, so BST/GMT is correct
  **without** an IANA database — which matters, because Windows has none here.
  A `TZID` with no `VTIMEZONE` is flagged in the brief rather than rendered at a
  plausible but wrong hour.
- A `VALARM` inside an event cannot overwrite its parent's `UID` or `SUMMARY`.
- A truncated feed is rejected, not parsed as a shorter calendar.

Verified against 97 test vectors and 8 real Google-served feeds, the largest
3.3 MB / 2427 events (181 ms).

Known limits, deliberately: `BYWEEKNO` and `BYYEARDAY` are unsupported, and
`BYSETPOS` in a `YEARLY` rule applies per-month. Any event using one is flagged
in the brief rather than silently misreported.

---

## Adding and editing sources

```bash
python dailybrief.py sources list
python dailybrief.py sources test https://cdm.link/feed/
python dailybrief.py sources add "CDM" https://cdm.link/feed/ --section audio --max-age 168
python dailybrief.py sources edit "BBC" --max-items 3
python dailybrief.py sources disable "Guardian"
python dailybrief.py sources remove "CDM"
```

`add` **fetches and parses the feed before saving it.** A source that no longer
serves a feed is refused rather than silently added and then silently empty —
which is exactly how Reuters, AP and KVR fail today. Use `--force` to override.

`--section` is just a heading name. Give it one that does not exist yet and a new
section appears in the brief, positioned in feed order and titled from
`section_titles` (with sensible defaults for `news`, `tech`, `audio`, `sport`,
`science`, `local`). Enabling and disabling keeps `sections` in step for you.

`--max-age` matters more than it looks. The default 24h is right for a news wire
but wrong for a low-cadence blog: at 24h a weekly blog looks permanently broken.
CDM is set to 168h (a week) for that reason.

Feeds are also plain entries in `config.json` if you would rather edit them
directly — RSS 2.0, Atom and RSS 1.0/RDF all parse.

---

## The 3×3 plan

A learning routine on three timescales. The **daily** tier is already the brief
itself — *Paper of the day* and *News of the day* are two thirds of it — so what
this section adds is the two tiers a morning fetch cannot supply.

```bash
python dailybrief.py 3x3 init
```

That writes `plan.json` and switches the section on. Then fill it in:

```bash
python dailybrief.py 3x3 start 2026-09-01
python dailybrief.py 3x3 topic 1 "Psychoacoustics" --video "Lecture 4|https://..." \
                                                   --text  "Moore, Psychology of Hearing" \
                                                   --misc  "Ear-training course|https://..."
python dailybrief.py 3x3 week "20 min app limit" "Tue/Thu 8-9pm blocked" "call, don't text"
python dailybrief.py 3x3 score kept retry dropped
python dailybrief.py 3x3 output "10-minute voice memo on masking"
python dailybrief.py 3x3 status
```

**Weekly** — three changes, picked on your review day (`review_day`, default
Sunday) and run for seven days. When the filed set falls behind the current week
the brief marks it **stale** and keeps asking, because a review missed on Sunday
that goes quiet on Monday is a review you will never do.

**Monthly** — three topics, one calendar month each. Week 1 is the lecture
video, week 2 the long-form text, week 3 the miscellaneous source, week 4 is
producing an output. Week 4 is the tail of the month rather than a literal seven
days, so a 31-day month cannot roll into a fifth week with no job. Month
boundaries are calendar months with the day clamped to the target month's
length, so a block starting 31 January hands over to its second topic on
28 February rather than skipping into March.

**The session is placed in real free time.** Each morning the brief scans your
calendar from now to the day before the next review and offers the first gap at
least `session_minutes` long inside `day_start`–`day_end` (45 minutes, 08:00–22:00
by default), naming what it worked around:

> **Session today 08:00–08:45** (45 min, free in your calendar). Around Standup
> 09:00, Dinner with friends 18:00.

If today is full it rolls to the next day that is not; if the whole week is
booked it says so rather than inventing a slot. The week costs no extra request —
`calendar()` expands seven days from the feed it already downloaded, and
`icslib.events_between` parses that feed once rather than seven times.

Three kinds of event, deliberately treated differently, because conflating any
two of them books you twice:

- **All-day** entries do not block the day. Most are labels rather than
  commitments, and one birthday would otherwise wipe out a week. They are named
  next to the proposal instead, so a genuine "on leave" is visible.
- **No `DTEND` at all** is a real zero-length instant in RFC 5545, and is treated
  as one rather than given an invented duration that would swallow an evening.
- **A `DTEND` the parser had to drop** — a `TZID` with no `VTIMEZONE` (Exchange
  exports really do emit these), or an end before its start — means the length is
  *unknown*, not zero. That day is treated as busy from the event onward and the
  event is named, because the alternative is offering you a slot inside a
  fourteen-hour workshop.

Anything that makes a proposal doubtful is said out loud beside it: a calendar
served from cache, a feed that returned no events at all, or an event whose
length could not be determined.

The rule that matters: **an unchecked calendar is never reported as a free one.**
No calendar connected, a failed fetch, or an offline morning all say the session
could not be placed — because "your evening is free" is a claim, and making it
from missing data is exactly the double-booking this is meant to prevent.

Two further refusals, both the same principle:

- **An empty slot is reported empty.** A topic with no sources says it has none
  and names the command that fixes it. It never shows a blank where a source
  should be, and `plan.example.json` ships with no topic names for the same
  reason — nobody's plan should start with a subject they did not choose.
- **A stale week is never shown as current.** Last week's three appear with
  their filing date and a review prompt, not as though you picked them today.

`plan.json` is gitignored like `config.json`; `plan.example.json` documents every
field. Nothing here is fetched, so it is the one section that still renders on a
morning the network is down — which is also the morning you most want to see
what you committed to.

---

## What's in it

| Section | Source | Cost |
|---|---|---|
| Calendar | Your ICS feeds (see below), with end times and durations | see below |
| Weather | Open-Meteo forecast (`timezone=auto`) | keyless |
| Headlines | BBC + Guardian RSS, 2 each | keyless |
| Audio & DSP | any feeds you add via `sources add` | keyless |
| Tech | Hacker News via Algolia, 1 request | keyless |
| Paper of the day | newest arXiv submission in `paper_categories` (default `eess.AS`, `cs.SD`) | keyless |
| News of the day | one story + standfirst from `featured_feed` (default Guardian Long Read) | keyless |
| 3×3 | your `plan.json`: this week's three changes, this month's topic and its source, and a session placed in free calendar time | local (rides on the calendar fetch) |
| On this day | Wikimedia on-this-day feed | keyless |
| Bank holiday | GOV.UK, **only** if one is within 10 days | keyless |

All requests run in parallel, typically about a second end to end.

## The look

The page is Danny's **"Daily brief app template"** from claude.ai/design — a
cherry-red retheme of the Nocturne design system: `#1b1216` ground, `#ef4a5f`
accent used as marks and lines rather than fills, Inter at weight 500,
left-aligned with the whitespace on the right. The token block sits at the top
of `PAGE` in `dailybrief.py`; the layout is generated per-section by
`compose_page`, so every element in the mock is real data here, and failed
sections say so instead of disappearing.

The **Refresh** button works from the static page: it fires `dailybrief:refresh`
(the same protocol the toast uses), which regenerates `latest.html` in place;
the page polls its own generation stamp and reloads when it changes. The first
click, Edge asks "Open pythonw?" — tick *always allow* and it never asks again.

The Claude engine (`"engine": "claude"`) still renders its prose in the same
chrome, via the `.prose` styles.

---

## How it runs

A per-user Scheduled Task named **Daily Brief** runs `pythonw.exe dailybrief.py run`
daily at 08:00. `pythonw` means no console window flashes.

Registered with `-StartWhenAvailable`, so if the PC was off at 08:00 it runs
shortly after your next logon rather than skipping the day.

Change the time by re-running the installer — it is idempotent:

```bash
powershell -ExecutionPolicy Bypass -File install.ps1 -Time 07:15
```

---

## Commands

| Command | What it does |
|---|---|
| `dailybrief.py setup "City" --country GB` | Resolve and store your location |
| `dailybrief.py run --force --open` | Generate now and open the window |
| `dailybrief.py 3x3 status` | Where you are in the 3×3 plan (`init`, `start`, `topic`, `week`, `score`, `output`) |
| `dailybrief.py check` | Hit every source, report what worked and how fast |
| `dailybrief.py status` | Engine, location, sections, last run |
| `dailybrief.py open` | Reopen the most recent brief |
| `dailybrief.py toast-test` | Fire a sample toast |
| `dailybrief.py render-last` | Re-render the last markdown, no network |
| `Start-ScheduledTask -TaskName 'Daily Brief'` | Test the scheduled path as Windows runs it |

---

## Optional: let Claude write it

Set `"engine": "claude"` (or `"auto"`) in `config.json`. The data is still
fetched the same way — Claude receives it as JSON and only writes the prose, so
it cannot invent a headline or a temperature, and it never web-searches.

It needs one of:

- `claude setup-token` in a terminal (long-lived token, survives unattended runs), or
- an `ANTHROPIC_API_KEY` environment variable.

`"auto"` uses Claude if a credential exists and falls back to local silently.
If Claude fails mid-run for any reason, the brief renders locally rather than
failing — you never lose the brief to the optional layer.

Edit `prompt.md` for style. It is ignored entirely by the local engine.

---

## Design notes

Every source here was verified with live requests, and the failure modes below
are ones that actually happened during that testing. They are worth knowing
before changing anything.

**Dead sources — do not re-add.** Reuters RSS is gone. AP serves 2 MB of HTML
where a feed used to be. CNN returns HTTP 200 with content frozen since 2023 —
the worst kind of failure, because it looks fine. Sound on Sound is 410 and KVR
returns its HTML homepage with a 200.

**A failed section never looks like a quiet one.** Every collector returns
`ok` / `empty` / `failed`, and `failed` is printed with its reason. Without this
the brief silently shrinks over months and reads like slow news.

**The footer is load-bearing.** `Sources 5/5 OK` is what makes a source that
quietly starts 403ing visible on the morning it happens.

**Things that bite on this specific machine:**

- Windows ships no IANA tz database, so `zoneinfo.ZoneInfo('Europe/London')`
  raises. Nothing here imports `zoneinfo`; Open-Meteo returns pre-localised
  naive timestamps instead.
- The console is cp1252. Writing `°C` or a Cyrillic name to it raises
  `UnicodeEncodeError`, and under `pythonw` `sys.stdout` is `None` so `print()`
  raises `AttributeError`. `main()` handles both.
- `parsedate_to_datetime` returns *naive* datetimes for the `-0000` form and
  *raises* on garbage, so mixing feeds crashes a merged sort. Everything goes
  through `netlib.parse_when`.
- One undefined entity (`&nbsp;`) makes ElementTree throw and takes the whole
  feed with it. `netlib.xml_safe` resolves them first.
- Open-Meteo geocoding returns HTTP 200 with **no `results` key at all** for a
  no-match, and its results are not ordered by importance.
- `daily[0]` is not necessarily today — it is the *location's* day, not the
  machine's. The weather collector indexes by date and never by `[0]`.
- `value or 0` treats a genuine 0 °C or 0% rain as missing. Only `is None` is
  correct; `sources.fmt()` enforces it.
- Captive portals return HTTP 200 with a login page for every request. There is
  a connectivity pre-flight, and bodies are sniffed before parsing.

## Tests

```bash
python tests\run_all.py
```

Five suites, ~380 checks: the markdown renderer, the failure-mode hardening
(feed parsing, encodings, tri-state sections), location/units settings, the
ICS/recurrence engine, and the 3×3 plan (month-boundary arithmetic, stale-week
detection, free-time placement across DST and overnight events, both renderers).
Run them after changing anything in `netlib.py`, `icslib.py`, `sources.py`,
`plan.py` or the renderer.

## Uninstall

```bash
powershell -ExecutionPolicy Bypass -File uninstall.ps1
```

Removes the scheduled task and both `HKCU` registry keys. `-Purge` also deletes
generated briefs and logs. Everything is user-scope; nothing needed admin.
