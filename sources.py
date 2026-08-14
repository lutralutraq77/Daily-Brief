"""Keyless data collectors for the daily brief. Standard library only.

Every collector returns a Section with an explicit status:

    ok      -> data is present
    empty   -> the fetch worked and there is genuinely nothing to report
    failed  -> we could not get the data, and `reason` says why

That distinction is the whole point. "No flood warnings" and "the API timed
out" must never render identically, or the brief silently shrinks over months
and reads like a quiet news day.

Sources here were verified live. Notable negative results, do not re-add:
  Reuters RSS      dead
  AP RSS           returns 2 MB of HTML
  CNN RSS          HTTP 200, but frozen since 2023
  Sound on Sound   410
  KVR Audio        200 HTML homepage
  OpenWeatherMap / NewsAPI / Wordnik  all require a key despite folklore
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Callable

import netlib
from netlib import FetchError, Item, clean_text, fetch, parse_feed, title_fingerprint

OK, EMPTY, FAILED = "ok", "empty", "failed"


@dataclass
class Section:
    key: str
    status: str
    data: Any = None
    reason: str = ""
    elapsed_ms: int = 0
    detail: dict = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.status == OK


# Authoritative Open-Meteo mapping, scraped from their docs during verification.
WMO = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Freezing fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    56: "Light freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    66: "Light freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Light showers", 81: "Showers", 82: "Violent showers",
    85: "Light snow showers", 86: "Snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with hail", 99: "Thunderstorm with heavy hail",
}

DEFAULT_FEEDS = [
    {"name": "BBC", "url": "https://feeds.bbci.co.uk/news/rss.xml",
     "section": "news", "max_items": 2, "max_age_hours": 24, "enabled": True},
    {"name": "Guardian", "url": "https://www.theguardian.com/uk/rss",
     "section": "news", "max_items": 2, "max_age_hours": 24, "enabled": True},
]

FEED_DEFAULTS = {"section": "news", "max_items": 2, "max_age_hours": 24, "enabled": True}


def normalise_feed(entry) -> dict | None:
    """Accept a dict, or the older ["Name", "url", n] triple, and fill defaults."""
    if isinstance(entry, dict):
        feed = dict(FEED_DEFAULTS, **entry)
    elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
        feed = dict(FEED_DEFAULTS, name=str(entry[0]), url=str(entry[1]))
        if len(entry) > 2:
            try:
                feed["max_items"] = int(entry[2])
            except (TypeError, ValueError):
                pass
    else:
        return None
    if not feed.get("name") or not feed.get("url"):
        return None
    feed["section"] = str(feed.get("section") or "news").strip().lower() or "news"
    for k, lo, hi in (("max_items", 1, 20), ("max_age_hours", 1, 24 * 90)):
        try:
            feed[k] = max(lo, min(hi, int(feed[k])))
        except (TypeError, ValueError, KeyError):
            feed[k] = FEED_DEFAULTS[k]
    feed["enabled"] = bool(feed.get("enabled", True))
    return feed


def configured_feeds(cfg: dict) -> list[dict]:
    """Every configured feed, normalised. Falls back to the verified defaults."""
    raw = cfg.get("feeds")
    if raw is None:
        raw = cfg.get("news_feeds")          # older key, still honoured
    if not raw:
        return [dict(f) for f in DEFAULT_FEEDS]
    out = [normalise_feed(e) for e in raw]
    return [f for f in out if f]


def feed_sections(cfg: dict) -> dict[str, list[dict]]:
    """Enabled feeds grouped by section, preserving first-appearance order."""
    grouped: dict[str, list[dict]] = {}
    for f in configured_feeds(cfg):
        if f["enabled"]:
            grouped.setdefault(f["section"], []).append(f)
    return grouped

WIKIMEDIA_UA = "DailyBrief/1.1 (personal daily-brief script; contact: local user)"


def fmt(v, unit: str = "", dp: int = 0) -> str:
    """Render a number that may legitimately be zero.

    `v or '-'` turns 0 C into a dash in winter and a genuine 0% chance of rain
    into "unknown". Only an explicit `is None` test is correct here.
    """
    if v is None:
        return "—"
    try:
        return f"{float(v):.{dp}f}{unit}"
    except (TypeError, ValueError):
        return str(v)


# ------------------------------------------------------------------ location


def _place(r: dict) -> dict:
    return {
        "latitude": round(r["latitude"], 4),
        "longitude": round(r["longitude"], 4),
        "label": ", ".join(x for x in (r.get("name"), r.get("admin1"), r.get("country")) if x),
        "country": r.get("country", ""),
        "country_code": r.get("country_code", ""),
        "population": r.get("population") or 0,
    }


def postcode_lookup(postcode: str) -> Section:
    """UK postcode -> coordinates, via postcodes.io.

    Better than a city name for weather (street-level rather than city-centre),
    and it returns `country`, which is what correctly picks the bank-holiday
    calendar instead of guessing from a label string.
    """
    started = _now_ms()
    pc = urllib.parse.quote(postcode.strip().upper())
    try:
        resp = fetch(f"https://api.postcodes.io/postcodes/{pc}", accept="application/json")
        payload = resp.json()
    except FetchError as exc:
        if "404" in str(exc):
            return Section("postcode", FAILED, reason=f"{postcode!r} is not a valid UK postcode",
                           elapsed_ms=_since(started))
        return Section("postcode", FAILED, reason=str(exc), elapsed_ms=_since(started))
    except ValueError as exc:
        return Section("postcode", FAILED, reason=str(exc), elapsed_ms=_since(started))

    # The envelope carries its own status, which can disagree with the HTTP code.
    if payload.get("status") != 200 or not payload.get("result"):
        return Section("postcode", FAILED, reason=f"{postcode!r} not found",
                       elapsed_ms=_since(started))

    r = payload["result"]
    return Section(
        "postcode",
        OK,
        data={
            "latitude": round(r["latitude"], 4),
            "longitude": round(r["longitude"], 4),
            "label": ", ".join(x for x in (r.get("admin_district"), r.get("country")) if x),
            "postcode": r.get("postcode", postcode),
            "country": r.get("country", ""),
            "country_code": "GB",
            "population": 0,
        },
        elapsed_ms=_since(started),
    )


def geocode(city: str, country_code: str = "") -> Section:
    """City name -> coordinates. Cached by the caller; not called every morning.

    Returns the best guess in .data and every candidate in .detail['candidates'],
    because "best" is genuinely ambiguous: plain "Newcastle" resolves to New
    South Wales, Australia on population alone.
    """
    started = _now_ms()
    url = (
        "https://geocoding-api.open-meteo.com/v1/search?"
        f"name={urllib.parse.quote(city)}&count=10&language=en&format=json"
    )
    if country_code:
        url += f"&countryCode={urllib.parse.quote(country_code.upper())}"
    try:
        payload = fetch(url, accept="application/json").json()
    except (FetchError, ValueError) as exc:
        return Section("geocode", FAILED, reason=str(exc), elapsed_ms=_since(started))

    # A no-match returns HTTP 200 with NO 'results' key at all -- not an empty
    # list, not a 404. payload['results'] would KeyError on the first typo.
    results = payload.get("results") or []
    if not results:
        hint = f" in country {country_code.upper()}" if country_code else ""
        return Section("geocode", FAILED, reason=f"no place matched {city!r}{hint}",
                       elapsed_ms=_since(started))

    # Results are NOT ordered by importance, so sort by population -- but keep
    # every candidate so the caller can offer a pick-list.
    ranked = sorted(results, key=lambda r: r.get("population") or 0, reverse=True)
    candidates = [_place(r) for r in ranked]
    return Section("geocode", OK, data=candidates[0],
                   detail={"candidates": candidates}, elapsed_ms=_since(started))


# ------------------------------------------------------------------ weather


# Open-Meteo converts server-side, so we ask for the units we want rather than
# converting afterwards. Values are (api_value, symbol).
TEMP_UNITS = {"celsius": ("celsius", "°C"), "fahrenheit": ("fahrenheit", "°F")}
WIND_UNITS = {
    "mph": ("mph", " mph"), "kmh": ("kmh", " km/h"),
    "ms": ("ms", " m/s"), "kn": ("kn", " kn"),
}
PRECIP_UNITS = {"mm": ("mm", "mm"), "inch": ("inch", "in")}


def units_from(cfg_units: dict | None) -> dict:
    """Resolve unit settings, falling back to metric on anything unrecognised."""
    u = cfg_units or {}
    temp = TEMP_UNITS.get(str(u.get("temperature", "celsius")).lower(), TEMP_UNITS["celsius"])
    wind = WIND_UNITS.get(str(u.get("wind", "mph")).lower(), WIND_UNITS["mph"])
    precip = PRECIP_UNITS.get(str(u.get("precipitation", "mm")).lower(), PRECIP_UNITS["mm"])
    return {
        "temperature": temp[0], "temperature_symbol": temp[1],
        "wind": wind[0], "wind_symbol": wind[1],
        "precipitation": precip[0], "precipitation_symbol": precip[1],
        "clock": "12h" if str(u.get("clock", "24h")).lower() in ("12h", "12") else "24h",
    }


def weather(lat: float, lon: float, today: dt.date, units: dict | None = None) -> Section:
    started = _now_ms()
    u = units or units_from(None)
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,"
        "precipitation_probability_max,precipitation_sum,sunrise,sunset,wind_speed_10m_max"
        "&current=temperature_2m,apparent_temperature,weather_code"
        "&timezone=auto&forecast_days=2"
        f"&temperature_unit={u['temperature']}"
        f"&wind_speed_unit={u['wind']}"
        f"&precipitation_unit={u['precipitation']}"
    )
    try:
        payload = fetch(url, accept="application/json").json()
    except (FetchError, ValueError) as exc:
        return Section("weather", FAILED, reason=str(exc), elapsed_ms=_since(started))

    if payload.get("error"):
        return Section("weather", FAILED, reason=str(payload.get("reason", "API error")),
                       elapsed_ms=_since(started))

    daily = payload.get("daily") or {}
    days = daily.get("time") or []
    # Never hardcode [0]. Open-Meteo returns the LOCATION's local day while
    # date.today() is the MACHINE's day; they diverge on a wrong clock or a
    # just-after-midnight retry, printing yesterday's weather under today.
    try:
        i = days.index(today.isoformat())
    except ValueError:
        if not days:
            return Section("weather", FAILED, reason="no daily data returned",
                           elapsed_ms=_since(started))
        i = 0

    def col(name):
        arr = daily.get(name) or []
        return arr[i] if i < len(arr) else None

    current = payload.get("current") or {}
    sunrise, sunset = col("sunrise"), col("sunset")
    return Section(
        "weather",
        OK,
        data={
            "for_date": days[i] if i < len(days) else today.isoformat(),
            "high": col("temperature_2m_max"),
            "low": col("temperature_2m_min"),
            "precip_prob": col("precipitation_probability_max"),
            "precip_mm": col("precipitation_sum"),
            "wind_mph": col("wind_speed_10m_max"),
            # The daily code is the day's DOMINANT/worst condition, which can
            # read as "Overcast" on a morning that is currently clear.
            "code": col("weather_code"),
            "condition": WMO.get(col("weather_code"), "Unknown"),
            "now_temp": current.get("temperature_2m"),
            "now_feels": current.get("apparent_temperature"),
            "now_condition": WMO.get(current.get("weather_code"), ""),
            # Naive pre-localised strings, so no zoneinfo is needed anywhere.
            "sunrise": _clock(sunrise, u["clock"]),
            "sunset": _clock(sunset, u["clock"]),
            "timezone": payload.get("timezone", ""),
            "units": u,
        },
        elapsed_ms=_since(started),
    )


def _clock(iso: str | None, style: str) -> str | None:
    """'2026-08-13T05:40' -> '05:40' or '5:40am'. Naive local, no tz maths."""
    if not isinstance(iso, str) or len(iso) < 16:
        return None
    hh, mm = iso[11:13], iso[14:16]
    if style != "12h":
        return f"{hh}:{mm}"
    h = int(hh)
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}:{mm}{suffix}"


# ------------------------------------------------------------------ news


def probe_feed(url: str, name: str = "feed") -> Section:
    """Fetch and parse one feed, for validating a source before saving it.

    This is what stops a dead source being added silently -- Reuters and AP both
    still resolve, they just do not serve feeds any more.
    """
    started = _now_ms()
    try:
        resp = fetch(url, accept="application/rss+xml, application/xml, text/xml")
        items = parse_feed(resp.body, name)
    except FetchError as exc:
        return Section("probe", FAILED, reason=str(exc), elapsed_ms=_since(started))
    if not items:
        return Section("probe", FAILED, reason="parsed, but contains no items",
                       elapsed_ms=_since(started))
    newest = max((i.when for i in items if i.when), default=None)
    age_h = (dt.datetime.now(dt.timezone.utc) - newest).total_seconds() / 3600 if newest else None
    return Section(
        "probe", OK, data=items,
        detail={"count": len(items), "newest_age_hours": age_h, "final_url": resp.url,
                "redirected": resp.redirected},
        elapsed_ms=_since(started),
    )


def feed_group(section: str, feeds: list[dict]) -> Section:
    """Collect one section's feeds, taking items per-source rather than pooled.

    A global recency sort hands the whole list to whoever publishes most: the
    Guardian's feed carries 138 items against the BBC's 39.
    """
    started = _now_ms()
    picked: list[Item] = []
    problems: list[str] = []
    seen_fp: set[str] = set()
    now = dt.datetime.now(dt.timezone.utc)

    for f in feeds:
        cutoff = now - dt.timedelta(hours=f["max_age_hours"])
        try:
            resp = fetch(f["url"], accept="application/rss+xml, application/xml, text/xml")
            items = parse_feed(resp.body, f["name"])
            if not items:
                problems.append(f"{f['name']}: 0 items")
                continue
            fresh = [it for it in items if it.when is None or it.when >= cutoff]
            if not fresh:
                newest = max((it.when for it in items if it.when), default=None)
                age = f"{(now - newest).days}d" if newest else "unknown"
                problems.append(f"{f['name']}: stale (newest {age})")
                continue
            taken = 0
            for it in fresh:
                fp = title_fingerprint(it.title)
                if fp and fp in seen_fp:
                    continue           # same story already taken from another outlet
                seen_fp.add(fp)
                picked.append(it)
                taken += 1
                if taken >= f["max_items"]:
                    break
        except FetchError as exc:
            problems.append(f"{f['name']}: {exc}")

    key = f"feed:{section}"
    if not picked:
        return Section(key, FAILED, reason="; ".join(problems) or "no items",
                       elapsed_ms=_since(started))
    return Section(key, OK, data=picked, reason="; ".join(problems), elapsed_ms=_since(started))


def tech(limit: int = 2, min_points: int = 100) -> Section:
    """Hacker News front page via Algolia: one request, includes scores.

    The Firebase API needs N+1 requests for N stories because there is no batch
    item endpoint, and it still lacks points without a further call each.
    """
    started = _now_ms()
    url = "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=20"
    try:
        payload = fetch(url, accept="application/json").json()
    except (FetchError, ValueError) as exc:
        return Section("tech", FAILED, reason=str(exc), elapsed_ms=_since(started))

    hits = payload.get("hits") or []
    if not hits:
        return Section("tech", FAILED, reason="no hits returned", elapsed_ms=_since(started))

    items = []
    for h in hits:
        title = clean_text(h.get("title"), 160)
        if not title:
            continue
        points = h.get("points") or 0
        if points < min_points:
            continue
        # Through the same normaliser as every feed link: it rebuilds with an
        # https scheme, which renders a javascript:/data: payload inert.
        link = netlib.normalise_link(
            h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
        )
        items.append(
            Item(
                title=title,
                link=link,
                when=netlib.parse_when(h.get("created_at")),
                source=f"HN {points}",
                key=str(h.get("objectID") or link),
            )
        )
        if len(items) >= limit * 3:
            break

    if not items:
        return Section("tech", EMPTY, reason=f"nothing above {min_points} points",
                       elapsed_ms=_since(started))
    return Section("tech", OK, data=items, elapsed_ms=_since(started))


_PAPER_CACHE_MAX_AGE = dt.timedelta(days=7)


def _write_paper_cache(path: pathlib.Path, data: dict) -> None:
    """Best effort. A cache that cannot be written must never break the brief."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(data)
        published = payload.get("published")
        payload["published"] = published.isoformat() if published else None
        path.write_text(
            json.dumps({
                "cached_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "data": payload,
            }),
            "utf-8",
        )
    except (OSError, TypeError, ValueError):
        pass


def _read_paper_cache(path: pathlib.Path) -> tuple[dict | None, dt.datetime | None]:
    """Return (data, cached_at), or (None, None) if absent, unreadable or too old."""
    try:
        blob = json.loads(path.read_text("utf-8"))
        data = dict(blob["data"])
    except (OSError, ValueError, KeyError, TypeError):
        return None, None
    cached_at = netlib.parse_when(blob.get("cached_at"))
    if cached_at is None:
        return None, None
    age = dt.datetime.now(dt.timezone.utc) - cached_at
    # A little negative age is just clock skew; a lot means the file is not trustworthy.
    if age > _PAPER_CACHE_MAX_AGE or age < -dt.timedelta(minutes=5):
        return None, None
    data["published"] = netlib.parse_when(data.get("published"))
    return data, cached_at


def _paper_from_cache(cache_dir, live_error: str, started: int) -> Section:
    """Serve the last good paper, saying plainly that it is not today's."""
    if cache_dir is not None:
        data, cached_at = _read_paper_cache(pathlib.Path(cache_dir) / "paper.json")
        if data:
            return Section(
                "paper", OK, data=data,
                reason=f"arXiv unreachable ({live_error}); showing the cached paper",
                detail={
                    "stale": f"carried over from {cached_at.astimezone():%a %d %b}",
                    "live_error": live_error,
                },
                elapsed_ms=_since(started),
            )
    return Section("paper", FAILED, reason=live_error, elapsed_ms=_since(started))


def paper(categories: list[str] | None = None, cache_dir=None) -> Section:
    """Paper of the day: the newest submission in the configured arXiv categories.

    One request per morning -- arXiv asks for 1 req/3s and was seen 429ing under
    burst testing from this IP, so never retry-loop this one aggressively.

    A rate limit is transient but the brief is not: it is written once and read
    all day. So the last good paper is cached and served when the live fetch
    fails, labelled as carried over rather than passed off as today's.
    """
    import xml.etree.ElementTree as ET

    started = _now_ms()
    cats = [c for c in (categories or ["eess.AS", "cs.SD"]) if c]
    query = "+OR+".join(f"cat:{c}" for c in cats)
    url = (f"https://export.arxiv.org/api/query?search_query={query}"
           "&sortBy=submittedDate&sortOrder=descending&max_results=5")
    try:
        resp = fetch(url, accept="application/atom+xml", retries=1)
        root = ET.fromstring(netlib.xml_safe(resp.body))
    except (FetchError, ET.ParseError) as exc:
        return _paper_from_cache(cache_dir, str(exc), started)

    A = "{http://www.w3.org/2005/Atom}"
    entries = root.findall(f"{A}entry")
    if not entries:
        # A genuinely quiet category is not a failure, so this does not fall back
        # to the cache -- that would report an old paper as though it were new.
        return Section("paper", EMPTY, reason=f"no recent submissions in {', '.join(cats)}",
                       elapsed_ms=_since(started))

    e = entries[0]
    arxiv_id = (e.findtext(f"{A}id") or "").rsplit("/abs/", 1)[-1]
    primary = e.find("{http://arxiv.org/schemas/atom}primary_category")
    authors = [clean_text(a.findtext(f"{A}name"), 60) for a in e.findall(f"{A}author")]
    abstract = clean_text(e.findtext(f"{A}summary"), 460)
    # An honest reading-time: the abstract's own word count at ~200 wpm.
    minutes = max(1, round(len(abstract.split()) / 200))
    data = {
        "id": arxiv_id.split("v")[0] or arxiv_id,
        "title": clean_text(e.findtext(f"{A}title"), 220),
        "abstract": abstract,
        "authors": authors,
        "category": primary.get("term") if primary is not None else (cats[0] if cats else ""),
        "published": netlib.parse_when(e.findtext(f"{A}published")),
        "link": f"https://arxiv.org/abs/{arxiv_id}",
        "read_minutes": minutes,
    }
    if cache_dir is not None:
        _write_paper_cache(pathlib.Path(cache_dir) / "paper.json", data)
    return Section("paper", OK, data=data, elapsed_ms=_since(started))


def featured(feed: dict | None = None) -> Section:
    """News of the day: one story with its standfirst, from a single-story feed."""
    started = _now_ms()
    f = feed or {"name": "Long Read",
                 "url": "https://www.theguardian.com/news/series/the-long-read/rss"}
    try:
        resp = fetch(f["url"], accept="application/rss+xml, application/xml, text/xml")
        items = parse_feed(resp.body, f.get("name", "Featured"))
    except FetchError as exc:
        return Section("featured", FAILED, reason=str(exc), elapsed_ms=_since(started))
    if not items:
        return Section("featured", FAILED, reason="feed contains no items",
                       elapsed_ms=_since(started))
    it = items[0]
    minutes = max(1, round(len(it.summary.split()) / 200)) if it.summary else None
    return Section(
        "featured", OK,
        data={"source": f.get("name", "Featured"), "title": it.title, "link": it.link,
              "summary": it.summary, "when": it.when, "read_minutes": minutes},
        elapsed_ms=_since(started),
    )


# ------------------------------------------------------------------ almanac


def on_this_day(today: dt.date) -> Section:
    started = _now_ms()
    # Must be zero-padded MM/DD -- '13/01' returns HTTP 500.
    path = f"{today:%m/%d}"
    urls = [
        f"https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday/selected/{path}",
        f"https://en.wikipedia.org/api/rest_v1/feed/onthisday/selected/{path}",
    ]
    last = "unavailable"
    for url in urls:
        try:
            # The default Python UA is hard-403'd by Wikimedia policy.
            payload = fetch(url, accept="application/json", user_agent=WIKIMEDIA_UA).json()
            events = payload.get("selected") or []
            if not events:
                last = "no events listed"
                continue
            picked = max(events, key=lambda e: len(e.get("text") or ""))
            return Section(
                "onthisday",
                OK,
                data={
                    "year": picked.get("year"),
                    "text": clean_text(picked.get("text"), 260),
                },
                elapsed_ms=_since(started),
            )
        except (FetchError, ValueError) as exc:
            last = str(exc)
    return Section("onthisday", FAILED, reason=last, elapsed_ms=_since(started))


def read_calendar_sources(path) -> list[dict]:
    """Parse calendars.txt: one calendar per line, optional `Label = ` prefix.

    Kept OUT of config.json deliberately. A Google secret ICS URL is a bearer
    token granting read of the whole calendar forever, and config.json is the
    file the README tells you to open, edit and paste into a support thread.
    """
    out = []
    if not path.exists():
        return out
    for raw in path.read_text("utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        label = ""
        if "=" in line and not line.split("=", 1)[0].strip().lower().startswith(("http", "file")):
            label, _, line = (p.strip() for p in line.partition("="))
        if not line:
            continue
        netlib.register_secret(line)
        out.append({"label": label, "target": line})
    return out


def _load_calendar_body(target: str, cache_dir) -> tuple[str, str]:
    """Return (text, provenance). Falls back to cache, but says so."""
    is_url = target.lower().startswith(("http://", "https://"))
    if not is_url:
        p = pathlib.Path(target).expanduser()
        if not p.exists():
            raise FetchError(f"file not found: {p.name}")
        return p.read_text("utf-8-sig", errors="replace"), "file"

    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(target.encode()).hexdigest()[:16]   # never name a cache after the URL
    cached = cache_dir / f"cal-{key}.ics"
    try:
        resp = fetch(target, accept="text/calendar, application/calendar+xml", timeout=25)
        text = resp.body.decode("utf-8-sig", errors="replace")
        head = text.lstrip()[:200].upper()
        if "BEGIN:VCALENDAR" not in head:
            raise FetchError("response is not a calendar (revoked URL, or a login page?)")
        if not text.rstrip().upper().endswith("END:VCALENDAR"):
            raise FetchError("feed is truncated")
        # Only cache a body that already validated, or a bad response poisons
        # the one good copy we have.
        try:
            cached.write_text(text, "utf-8")
        except OSError:
            pass
        return text, "live"
    except FetchError:
        if cached.exists():
            age = dt.datetime.now() - dt.datetime.fromtimestamp(cached.stat().st_mtime)
            hours = age.total_seconds() / 3600
            return cached.read_text("utf-8-sig", errors="replace"), f"cache {hours:.0f}h old"
        raise


def calendar(cfg: dict, today: dt.date, base_dir, horizon_days: int = 0) -> Section:
    """Today's events across every configured calendar.

    `horizon_days` additionally expands the following N days into
    `detail["ahead"]`, so the 3x3 section can place a session in genuinely free
    time. It rides on this fetch deliberately: the feed is already downloaded
    and parsed here, and a second collector asking the same URL for the same
    week would double both.
    """
    import icslib

    started = _now_ms()
    sources = read_calendar_sources(base_dir / "calendars.txt")
    if not sources:
        return Section("calendar", EMPTY,
                       reason="no calendars configured — see calendars.txt",
                       elapsed_ms=_since(started))

    my_emails = {e.lower() for e in (cfg.get("calendar_emails") or []) if e}
    events, notes, failures, stale = [], [], [], []
    ahead: dict[dt.date, list[dict]] = {}
    suspect: list[str] = []

    for src in sources:
        name = src["label"] or "calendar"
        try:
            text, provenance = _load_calendar_body(src["target"], base_dir / "cache")
        except FetchError as exc:
            failures.append(f"{name}: {netlib.scrub(str(exc))}")
            continue
        if provenance.startswith("cache"):
            stale.append(f"{name}: {provenance}")
        try:
            if horizon_days > 0:
                span = icslib.events_between(
                    text, today, today + dt.timedelta(days=horizon_days), my_emails)
                by_day = span["by_day"]
                got = {"events": by_day[today], "total_events": span["total_events"],
                       "problems": span["problems"]}
                for day, evs in by_day.items():
                    if day == today:
                        continue
                    for ev in evs:
                        ev["calendar"] = src["label"]
                    ahead.setdefault(day, []).extend(evs)
            else:
                got = icslib.events_on(text, today, my_emails)
        except icslib.IcsError as exc:
            failures.append(f"{name}: {netlib.scrub(str(exc))}")
            continue
        for ev in got["events"]:
            ev["calendar"] = src["label"]
            events.append(ev)
        if got["problems"]:
            notes.append(f"{name}: {len(got['problems'])} event(s) unparseable")
        if got["total_events"] == 0:
            notes.append(f"{name}: feed contains no events at all — check the URL")
            # A wrong URL that still returns a valid, empty calendar is the worst
            # kind of failure here: it looks like a clear week. Recorded so the
            # 3x3 can qualify a session it placed on the strength of it.
            suspect.append(name)

    events.sort(key=lambda e: (not e["all_day"],
                               e["start"] or dt.datetime.min.replace(tzinfo=dt.timezone.utc)))
    reason = "; ".join(failures + stale + notes)

    # `horizon_days` travels with the result so a consumer can tell "the week is
    # genuinely free" from "nobody ever looked at the week". Treating those two
    # alike is how you get told an evening is clear when it was never checked.
    detail = {"checked": len(sources), "ahead": ahead, "horizon_days": horizon_days,
              "scanned": not failures, "suspect": suspect, "stale": list(stale)}
    if failures and not events:
        return Section("calendar", FAILED, reason=reason, elapsed_ms=_since(started))
    if not events:
        # "Nothing scheduled" and "the fetch died" must never look the same.
        return Section("calendar", EMPTY, reason=reason or "nothing scheduled",
                       detail=detail, elapsed_ms=_since(started))
    return Section("calendar", OK, data=events, reason=reason,
                   detail=detail, elapsed_ms=_since(started))


def bank_holiday(today: dt.date, division: str = "england-and-wales", within_days: int = 10) -> Section:
    """Exception-only: reports nothing unless one is close."""
    started = _now_ms()
    try:
        payload = fetch("https://www.gov.uk/bank-holidays.json", accept="application/json").json()
    except (FetchError, ValueError) as exc:
        return Section("bankholiday", FAILED, reason=str(exc), elapsed_ms=_since(started))

    events = (payload.get(division) or {}).get("events") or []
    if not events:
        return Section("bankholiday", FAILED, reason=f"no events for division {division}",
                       elapsed_ms=_since(started))

    upcoming = []
    horizon = today
    for e in events:
        try:
            d = dt.date.fromisoformat(e["date"])
        except (KeyError, ValueError):
            continue
        horizon = max(horizon, d)
        if today <= d <= today + dt.timedelta(days=within_days):
            upcoming.append((d, clean_text(e.get("title"), 80)))

    # The published file ends in 2028; once it lapses this section would go
    # permanently and silently blank.
    if (horizon - today).days < 90:
        return Section("bankholiday", FAILED,
                       reason=f"gov.uk data runs out {horizon.isoformat()} - update source",
                       elapsed_ms=_since(started))
    if not upcoming:
        return Section("bankholiday", EMPTY, reason="none within %d days" % within_days,
                       elapsed_ms=_since(started))
    upcoming.sort()
    d, title = upcoming[0]
    return Section("bankholiday", OK,
                   data={"date": d, "title": title, "days": (d - today).days},
                   elapsed_ms=_since(started))


def threexthree(today: dt.date, plan_path=None) -> Section:
    """The 3x3 plan you wrote, read back and placed on today's calendar.

    The only collector here that touches no network at all, which is the point:
    the weekly three and the month's topic are commitments you already made, so
    they must still show on a morning when every feed is down. `collect_sections`
    keeps it out of the offline short-circuit for that reason.
    """
    started = _now_ms()
    import plan as P

    try:
        data = P.load(plan_path) if plan_path else dict(P.DEFAULT_PLAN)
        st = P.status(data, today)
    except P.PlanError as exc:
        return Section("threexthree", FAILED, reason=str(exc), elapsed_ms=_since(started))
    except Exception as exc:  # noqa: BLE001 - see below
        # Deliberately broad, and not merely defensive: `collect()` runs its jobs
        # in a pool that turns any escaping exception into a FAILED section, but
        # the offline path in collect_sections calls this function DIRECTLY. An
        # AttributeError from a hand-edited plan.json would there take down the
        # whole brief -- on precisely the morning the network is already gone.
        return Section("threexthree", FAILED, reason=f"plan.json is malformed: {exc}",
                       elapsed_ms=_since(started))

    if not st["configured"]:
        return Section("threexthree", EMPTY,
                       reason="no plan yet - start one with `dailybrief.py 3x3 init`",
                       elapsed_ms=_since(started))
    # Gaps travel on `reason` the way a partly-failed feed group's do, so a
    # half-filled plan still renders what it has and names what it is missing.
    return Section("threexthree", OK, data=st, reason="; ".join(st["problems"]),
                   elapsed_ms=_since(started))


# ------------------------------------------------------------------ driver


def collect(cfg: dict, today: dt.date, deadline_s: int = 90, base_dir=None) -> dict[str, Section]:
    """Run every enabled collector in parallel under one absolute deadline.

    A per-request timeout is not a ceiling: it bounds inter-byte gaps, so a
    server dripping a byte every few seconds can pin a run indefinitely.
    """
    loc = cfg.get("location") or {}
    lat, lon = loc.get("latitude"), loc.get("longitude")
    enabled = set(cfg.get("sections") or ["weather", "news", "tech", "onthisday", "bankholiday"])

    units = units_from(cfg.get("units"))
    base = pathlib.Path(base_dir) if base_dir else pathlib.Path(__file__).resolve().parent
    jobs: dict[str, Callable[[], Section]] = {}
    if "calendar" in enabled:
        # A week ahead only when the 3x3 needs it to place a session; otherwise
        # the calendar section expands one day, exactly as it always has.
        horizon = 7 if "threexthree" in enabled else 0
        jobs["calendar"] = lambda: calendar(cfg, today, base, horizon_days=horizon)
    if "weather" in enabled and lat is not None and lon is not None:
        jobs["weather"] = lambda: weather(lat, lon, today, units)

    # Every feed section the user has configured, not just "news". A section is
    # on when its name appears in `sections`; `sources add` puts it there.
    for section, feeds in feed_sections(cfg).items():
        if section in enabled:
            jobs[f"feed:{section}"] = lambda s=section, f=list(feeds): feed_group(s, f)

    if "tech" in enabled:
        jobs["tech"] = lambda: tech(int(cfg.get("tech_items", 2)),
                                    int(cfg.get("tech_min_points", 100)))
    if "paper" in enabled:
        jobs["paper"] = lambda: paper(cfg.get("paper_categories"), base / "cache")
    if "featured" in enabled:
        jobs["featured"] = lambda: featured(cfg.get("featured_feed"))
    if "onthisday" in enabled:
        jobs["onthisday"] = lambda: on_this_day(today)
    if "bankholiday" in enabled:
        jobs["bankholiday"] = lambda: bank_holiday(today, cfg.get("bank_holiday_division",
                                                                  "england-and-wales"))
    if "threexthree" in enabled:
        jobs["threexthree"] = lambda: threexthree(today, base / "plan.json")

    out: dict[str, Section] = {}
    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as pool:
        futures = {pool.submit(fn): key for key, fn in jobs.items()}
        done, pending = wait(futures, timeout=deadline_s)
        for fut in done:
            key = futures[fut]
            try:
                out[key] = fut.result()
            except Exception as exc:  # noqa: BLE001 - a collector bug must not kill the brief
                out[key] = Section(key, FAILED, reason=f"collector crashed: {exc}")
        for fut in pending:
            key = futures[fut]
            fut.cancel()
            out[key] = Section(key, FAILED, reason=f"exceeded {deadline_s}s deadline")

    if lat is None or lon is None:
        if "weather" in enabled:
            out["weather"] = Section("weather", FAILED,
                                     reason="no location set - run `dailybrief.py setup <city>`")
    return out


def _now_ms() -> float:
    import time

    return time.monotonic() * 1000


def _since(started: float) -> int:
    return int(_now_ms() - started)
