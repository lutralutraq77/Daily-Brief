"""Regression tests for the specific failure modes the review pass identified.

These are the bugs that produce a SILENTLY WRONG brief rather than a crash,
so they are the ones worth pinning down with tests.
"""
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import netlib
import sources as S
import dailybrief as db

checks = {}


def check(name, cond):
    checks[name] = bool(cond)


# --- parse_when: naive vs aware mixing crashed the merged sort ---------------
naive = netlib.parse_when("Thu, 13 Aug 2026 13:38:30 -0000")
aware = netlib.parse_when("Thu, 13 Aug 2026 14:58:00 +0100")
check("parse_when: -0000 returns aware", naive is not None and naive.tzinfo is not None)
check("parse_when: +0100 returns aware", aware is not None and aware.tzinfo is not None)
check("parse_when: mixed sort does not raise", sorted([naive, aware]) is not None)
check("parse_when: garbage -> None", netlib.parse_when("not a date") is None)
check("parse_when: empty -> None", netlib.parse_when("") is None)
check("parse_when: None -> None", netlib.parse_when(None) is None)
check("parse_when: ISO8601 works", netlib.parse_when("2026-08-13T09:00:00Z") is not None)

# --- xml_safe: one &nbsp; used to destroy an entire feed ---------------------
import xml.etree.ElementTree as ET

bad = b'<rss><channel><item><title>A&nbsp;B &mdash; C</title><link>https://x.tld/a</link></item></channel></rss>'
try:
    ET.fromstring(bad)
    check("xml_safe: baseline actually fails without it", False)
except ET.ParseError:
    check("xml_safe: baseline actually fails without it", True)
items = netlib.parse_feed(bad, "T")
check("xml_safe: feed survives undefined entities", len(items) == 1)

# --- HTML/entity cleaning ---------------------------------------------------
check("strip_html removes tags", netlib.clean_text("<p>Hello <b>world</b></p>") == "Hello world")
check("clean_text unescapes once", netlib.clean_text("A &amp; B") == "A & B")
check("clean_text collapses nbsp", "\u00a0" not in netlib.clean_text("a\u00a0b"))
check("clean_text handles '<' as less-than", "3" in netlib.clean_text("2 < 3 is true"))
check("demojibake repairs cp1252-decoded utf8", netlib.demojibake("New Yearâ€™s") == "New Year\u2019s")
check("demojibake leaves clean text alone", netlib.demojibake("New Year\u2019s") == "New Year\u2019s")

# --- feed sniffing: portals and retired feeds return 200 + HTML -------------
check("looks_like_feed rejects HTML", not netlib.looks_like_feed(b"<!DOCTYPE html><html><body>hi"))
check("looks_like_feed accepts RSS", netlib.looks_like_feed(b"<?xml version='1.0'?><rss>"))
check("looks_like_feed accepts Atom", netlib.looks_like_feed(b"<feed xmlns='http://www.w3.org/2005/Atom'>"))
try:
    netlib.parse_feed(b"<!DOCTYPE html><html><body>login</body></html>", "portal")
    check("parse_feed rejects an HTML body", False)
except netlib.FetchError:
    check("parse_feed rejects an HTML body", True)

# --- Atom link is an ATTRIBUTE, not text ------------------------------------
atom = b"""<feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Atom item</title><link rel="alternate" href="https://a.tld/post"/>
<updated>2026-08-13T09:00:00Z</updated><id>tag:a.tld,2026:1</id></entry></feed>"""
ai = netlib.parse_feed(atom, "Atom")
check("atom: link read from href attribute", len(ai) == 1 and ai[0].link.startswith("https://a.tld/post"))
check("atom: id used as dedupe key", len(ai) == 1 and ai[0].key == "tag:a.tld,2026:1")

# --- tracking params stripped from displayed link ---------------------------
rss = b"""<rss><channel><item><title>T</title>
<link>https://www.bbc.co.uk/news/articles/x?at_medium=RSS&amp;at_campaign=rss&amp;utm_source=z</link>
<pubDate>Thu, 13 Aug 2026 13:38:30 GMT</pubDate></item></channel></rss>"""
ri = netlib.parse_feed(rss, "BBC")
check("tracking params stripped from link", "at_medium" not in ri[0].link and "utm_source" not in ri[0].link)
check("link path preserved", ri[0].link.endswith("/news/articles/x"))

# --- fmt(): 0 is a real value, not "missing" --------------------------------
check("fmt: 0 renders as 0 not dash", S.fmt(0, "%") == "0%")
check("fmt: 0.0 C renders", S.fmt(0.0, "\u00b0C") == "0\u00b0C")
check("fmt: None renders as dash", S.fmt(None, "%") == "\u2014")

# --- weather day index must match TODAY, never [0] --------------------------
class FakeResp:
    def __init__(self, payload): self._p = payload
    def json(self): return self._p

today = dt.date(2026, 8, 13)
payload = {
    "daily": {
        "time": ["2026-08-12", "2026-08-13"],     # today is index 1, NOT 0
        "weather_code": [61, 3], "temperature_2m_max": [19.0, 33.0],
        "temperature_2m_min": [11.0, 19.0], "precipitation_probability_max": [90, 0],
        "precipitation_sum": [4.0, 0.0], "wind_speed_10m_max": [22.0, 13.0],
        "sunrise": ["2026-08-12T05:35", "2026-08-13T05:37"],
        "sunset": ["2026-08-12T20:46", "2026-08-13T20:44"],
    },
    "current": {"temperature_2m": 30.0, "apparent_temperature": 29.0, "weather_code": 0},
    "timezone": "Europe/London",
}
orig_fetch = S.fetch
S.fetch = lambda *a, **k: FakeResp(payload)
w = S.weather(0, 0, today)
S.fetch = orig_fetch
check("weather: indexes by date, not [0]", w.data["high"] == 33.0 and w.data["low"] == 19.0)
check("weather: for_date is today", w.data["for_date"] == "2026-08-13")
check("weather: 0% precip preserved", w.data["precip_prob"] == 0)
check("weather: sunrise sliced to HH:MM", w.data["sunrise"] == "05:37")
check("weather: daily vs current condition both kept",
      w.data["condition"] == "Overcast" and w.data["now_condition"] == "Clear sky")

# --- tri-state rendering: failed must never look like empty -----------------
secs = {
    "weather": S.Section("weather", S.FAILED, reason="timed out after 15s"),
    "feed:news": S.Section("feed:news", S.FAILED, reason="BBC: HTTP 403"),
    "tech": S.Section("tech", S.EMPTY, reason="nothing above 100 points"),
    "onthisday": S.Section("onthisday", S.OK, data={"year": 1940, "text": "Something happened."}),
}
# The feeds are stated explicitly rather than relying on whatever DEFAULT_FEEDS
# happens to ship: this test is about how a FAILED section renders, not about
# which sources are configured by default.
_news_cfg = {"tech_items": 2,
             "feeds": [{"name": "BBC", "url": "https://x.tld/rss", "section": "news"}]}
md = db.compose_markdown(_news_cfg, today, secs, ["System clock is 45 min off server time"])
check("failed weather is stated", "Weather unavailable" in md and "timed out" in md)
check("failed feed section is stated", "## Headlines" in md and "Unavailable" in md and "403" in md)
check("empty tech says so distinctly", "score threshold" in md)
check("failed != empty wording", "Tech unavailable" not in md)
check("notice rendered as banner", "clock is 45 min off" in md)
check("status footer counts failures", "Sources 2/4" in md and "weather" in md.split("---")[-1])

# --- custom feed sections render under their own heading --------------------
import netlib as _nl
custom_cfg = {
    "tech_items": 2,
    "feeds": [
        {"name": "BBC", "url": "https://x.tld/a", "section": "news"},
        {"name": "CDM", "url": "https://y.tld/b", "section": "audio", "max_items": 1},
    ],
}
custom_secs = {
    "feed:news": S.Section("feed:news", S.OK, data=[
        _nl.Item("A headline", "https://x.tld/1", None, "BBC", "k1")]),
    "feed:audio": S.Section("feed:audio", S.OK, data=[
        _nl.Item("A synth thing", "https://y.tld/1", None, "CDM", "k2")]),
}
md3 = db.compose_markdown(custom_cfg, today, custom_secs, [])
check("custom section gets its own heading", "## Audio & DSP" in md3)
check("custom section renders items", "A synth thing" in md3)
check("news section still titled Headlines", "## Headlines" in md3)
check("section order follows feed order", md3.index("## Headlines") < md3.index("## Audio & DSP"))

# --- feed normalisation -----------------------------------------------------
check("old triple format still parses",
      S.normalise_feed(["X", "https://x.tld", 5])["max_items"] == 5)
check("dict format fills defaults",
      S.normalise_feed({"name": "X", "url": "https://x.tld"})["section"] == "news")
check("garbage feed rejected", S.normalise_feed({"name": "X"}) is None)
check("max_items clamped", S.normalise_feed({"name": "X", "url": "u", "max_items": 999})["max_items"] == 20)
check("bad max_items falls back",
      S.normalise_feed({"name": "X", "url": "u", "max_items": "lots"})["max_items"] == 2)
check("disabled feeds excluded from sections",
      "audio" not in S.feed_sections({"feeds": [
          {"name": "C", "url": "u", "section": "audio", "enabled": False}]}))

# --- all-good footer --------------------------------------------------------
good = {"weather": S.Section("weather", S.OK, data=dict(
    condition="Overcast", high=33.0, low=19.0, precip_prob=0, precip_mm=0.0, wind_mph=13.0,
    now_temp=30.0, now_feels=29.0, now_condition="Clear sky", sunrise="05:37", sunset="20:44",
    for_date="2026-08-13", code=3, timezone="Europe/London"))}
md2 = db.compose_markdown({"tech_items": 2}, today, good, [])
check("clean run says OK", "Sources 1/1 OK" in md2)
check("0% rain shown, not swallowed", "rain 0%" in md2)

# --- markdown link escaping -------------------------------------------------
check("brackets in headline do not break link",
      db._md_link("A [bracketed] title", "https://x.tld") == "[A (bracketed) title](https://x.tld)")

# --- the whole thing still renders to HTML ---------------------------------
html = db.markdown_to_html(md)
check("renders to html", "<h2>" in html and "\x00" not in html)

# --- defaults must be self-consistent -----------------------------------------
# A feed section only renders when it is named in DEFAULT_CONFIG["sections"].
# Adding a feed without adding its section makes a fresh install silently ship
# without it, which is exactly how the science/nature/film sections went missing
# from the first build that shipped them.
_feed_sections = {f["section"] for f in map(S.normalise_feed, S.DEFAULT_FEEDS) if f}
_default_sections = set(db.DEFAULT_CONFIG["sections"])
check("every DEFAULT_FEEDS section is enabled by default",
      _feed_sections <= _default_sections)
check("every default feed has a title",
      all(s in db.DEFAULT_SECTION_TITLES for s in _feed_sections))
check("default feeds all normalise", all(S.normalise_feed(f) for f in S.DEFAULT_FEEDS))

bad_n = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(("  PASS " if v else "  FAIL ") + k)
print(f"\n{len(checks) - len(bad_n)}/{len(checks)} passed")
sys.exit(1 if bad_n else 0)
