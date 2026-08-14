"""HTTP + parsing layer for the daily brief. Standard library only.

Every hard-won lesson from live-testing these sources is encoded here rather
than in the collectors, so the collectors stay readable:

- We always send our OWN descriptive User-Agent. Wikimedia hard-403s the default
  Python UA, and the Telegraph inverts the usual rule (it blocks spoofed Chrome
  strings and allows honest ones). Sending an honest UA everywhere sidesteps both.
- urllib does NOT decompress for you the way requests does, so gzip is manual.
- A response body is sniffed before parsing. A captive portal, a retired feed
  301'd to a landing page, and a CDN error page all return HTTP 200 with HTML.
- Undefined HTML entities (`&nbsp;`) make ElementTree throw and take a whole
  feed with them, so bytes are sanitised first.
- `parsedate_to_datetime` returns NAIVE datetimes for `-0000` and RAISES on
  garbage, so mixing feeds crashes a merged sort. Everything goes through
  parse_when().
- Windows here has no IANA tz database: zoneinfo.ZoneInfo('Europe/London')
  raises ZoneInfoNotFoundError. Nothing in this file may import zoneinfo.
"""

from __future__ import annotations

import gzip
import html as htmllib
import json
import random
import re
import socket
import ssl
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

USER_AGENT = "DailyBrief/1.1 (personal daily-brief script; stdlib urllib)"
MAX_BYTES = 5 * 1024 * 1024          # AP served 2 MB of HTML where a feed was expected
DEFAULT_TIMEOUT = 15
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
MAX_BACKOFF = 8.0                    # the brief has a deadline; do not sleep past it
MIN_RATE_LIMIT_BACKOFF = 3.0         # arXiv's stated limit is one request per 3s
MAX_RETRY_AFTER = 30.0               # longer than this, give up instead of waiting

# Hosts that returned a captive-portal-style body get reported, not parsed.
CONNECTIVITY_URL = "http://www.msftconnecttest.com/connecttest.txt"
CONNECTIVITY_EXPECT = "Microsoft Connect Test"

_ATOM = "{http://www.w3.org/2005/Atom}"
_RDF_ITEM = "{http://purl.org/rss/1.0/}item"

# Populated by the first successful response, for the clock-skew check.
server_date: datetime | None = None


class FetchError(Exception):
    """Any failure to get a usable body, with a short human-readable reason."""


# --- secret scrubbing -------------------------------------------------------
# A Google secret ICS URL is a bearer token: anyone holding it can read the whole
# calendar, forever. It must never reach the log, the toast, state.json, the
# rendered brief, or a prompt sent to Claude. Register it here and scrub every
# string on its way out.
_SECRETS: set[str] = set()
_SECRET_PATTERNS = [
    re.compile(r"https?://[^\s\"'<>]*?/private-[0-9a-fA-F]{8,}/[^\s\"'<>]*", re.I),
    re.compile(r"private-[0-9a-fA-F]{16,}", re.I),
]
REDACTED = "<calendar-url redacted>"


def register_secret(value: str | None) -> None:
    if value and len(value.strip()) > 12:
        _SECRETS.add(value.strip())


def scrub(text) -> str:
    """Remove any registered secret, plus anything shaped like one."""
    s = text if isinstance(text, str) else str(text)
    for secret in _SECRETS:
        if secret in s:
            s = s.replace(secret, REDACTED)
    for pat in _SECRET_PATTERNS:
        s = pat.sub(REDACTED, s)
    return s


@dataclass
class Response:
    url: str
    status: int
    body: bytes
    headers: dict = field(default_factory=dict)
    redirected: bool = False

    def json(self):
        return json.loads(self.body.decode("utf-8", "replace"))


def _opener() -> urllib.request.OpenerDirector:
    # Python's default context loads the Windows certificate store, so unlike
    # requests+certifi this does not rot as CAs rotate.
    ctx = ssl.create_default_context()
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))


def fetch(
    url: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    accept: str = "*/*",
    retries: int = 2,
    user_agent: str = USER_AGENT,
) -> Response:
    """GET a URL and return decoded bytes, or raise FetchError with a reason."""
    global server_date
    last = "unknown error"
    last_status: int | None = None
    retry_after: float | None = None

    for attempt in range(retries + 1):
        if attempt:
            # Jittered backoff. Naive retries are how you turn a transient 429
            # into a lasting block on Open-Meteo / arXiv / ZenQuotes.
            delay = retry_after if retry_after is not None else (2 ** attempt) * (0.5 + random.random())
            if last_status == 429:
                # arXiv asks for one request per 3s, and answering a rate limit
                # 1.5s later is how a transient block becomes a lasting one.
                delay = max(delay, MIN_RATE_LIMIT_BACKOFF)
            _sleep(min(delay, MAX_BACKOFF))
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": user_agent,
                    "Accept": accept,
                    "Accept-Encoding": "gzip",
                    "Accept-Language": "en-GB,en;q=0.9",
                    "Connection": "close",
                },
            )
            with _opener().open(req, timeout=timeout) as resp:
                raw = resp.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise FetchError(f"body exceeded {MAX_BYTES // 1024 // 1024} MB")
                headers = {k.lower(): v for k, v in resp.headers.items()}
                if headers.get("content-encoding", "").lower() == "gzip":
                    try:
                        raw = gzip.decompress(raw)
                    except (OSError, EOFError) as exc:
                        raise FetchError(f"gzip decode failed: {exc}") from exc
                if server_date is None and headers.get("date"):
                    server_date = parse_when(headers["date"])
                final = resp.geturl()
                if urllib.parse.urlparse(final).scheme != "https" and url.startswith("https"):
                    raise FetchError(f"redirected off https to {final}")
                return Response(
                    url=final,
                    status=resp.status,
                    body=raw,
                    headers=headers,
                    redirected=final.rstrip("/") != url.rstrip("/"),
                )
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
            last_status = exc.code
            if exc.code not in RETRY_STATUSES:
                raise FetchError(last) from exc
            # Honour Retry-After when the server sends one. If it wants longer
            # than the brief can wait, stop now and say so rather than burning
            # the remaining attempts on requests that are certain to be refused.
            retry_after = _retry_after_seconds(exc.headers.get("Retry-After"))
            if retry_after is not None and retry_after > MAX_RETRY_AFTER:
                raise FetchError(f"{last}, server asked to wait {retry_after:.0f}s") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, ssl.SSLError):
                raise FetchError(f"TLS failure: {reason}") from exc
            last = f"network error: {reason}"
        except socket.timeout:
            last = f"timed out after {timeout}s"
        except FetchError:
            raise
        except OSError as exc:
            last = f"connection failed: {exc}"

    raise FetchError(last)


def _retry_after_seconds(value: str | None) -> float | None:
    """Parse Retry-After, which is either delta-seconds or an HTTP-date.

    Returns None when the header is absent or unusable, so the caller falls back
    to its own backoff rather than treating a malformed header as "retry now".
    """
    if not value:
        return None
    raw = value.strip()
    if raw.isdigit():
        return float(raw)
    when = parse_when(raw)
    if when is None:
        return None
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


def online() -> tuple[bool, str]:
    """Detect no-network AND captive portals, which return 200 + a login page."""
    try:
        r = fetch(CONNECTIVITY_URL, timeout=8, retries=1)
    except FetchError as exc:
        return False, str(exc)
    text = r.body.decode("utf-8", "replace").strip()
    if CONNECTIVITY_EXPECT not in text:
        return False, "captive portal intercepting requests"
    return True, "ok"


def clock_skew_minutes() -> float | None:
    """Compare the local clock against a server Date header.

    A clock that is a day out makes every date-keyed section confidently wrong
    with no exception anywhere: on-this-day, the weather day index, freshness.
    """
    if server_date is None:
        return None
    return abs((datetime.now(timezone.utc) - server_date).total_seconds()) / 60.0


# ------------------------------------------------------------------ text


def demojibake(s: str) -> str:
    """Repair UTF-8 bytes that were decoded as cp1252 ('New Yearâ€™s Day').

    Parsing from raw bytes does not prevent this: a feed whose XML declaration
    claims ISO-8859-1 while its bytes are really UTF-8 corrupts silently.
    """
    if "Ã" in s or "â€" in s:
        try:
            return s.encode("cp1252").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return s


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data):
        self.parts.append(data)


def strip_html(s: str) -> str:
    """Remove tags with the stdlib parser. A regex breaks on '<' used as
    less-than and on the unclosed tags real feeds contain."""
    try:
        ex = _TextExtractor()
        ex.feed(s)
        ex.close()
        return "".join(ex.parts)
    except Exception:  # noqa: BLE001 - malformed markup must never kill a section
        return re.sub(r"<[^>]*>", " ", s)


def clean_text(s: str | None, limit: int = 0) -> str:
    """Normalise anything destined for the brief."""
    if not s:
        return ""
    s = demojibake(s)
    if "<" in s or "&" in s:
        s = strip_html(s)
    s = htmllib.unescape(s)
    s = unicodedata.normalize("NFC", s)
    s = s.replace(" ", " ")          # Wikipedia is full of these
    s = " ".join(s.split())
    if limit and len(s) > limit:
        import textwrap

        s = textwrap.shorten(s, width=limit, placeholder="…")
    return s


def parse_when(s: str | None) -> datetime | None:
    """RFC-2822 / ISO date -> aware UTC datetime, or None.

    parsedate_to_datetime returns a NAIVE datetime for the '-0000' form and
    RAISES ValueError on empty/garbage input, so sorting a merged feed pool
    without this helper throws TypeError after all the network work is done.
    """
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
    except (ValueError, TypeError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


_ENTITY = re.compile(rb"&(?!#|amp;|lt;|gt;|quot;|apos;)([A-Za-z][A-Za-z0-9]{1,31});")


def xml_safe(b: bytes) -> bytes:
    """Resolve undefined HTML entities before ElementTree sees them.

    A single `&nbsp;` raises 'undefined entity' and loses the entire feed.
    """
    def sub(m: re.Match) -> bytes:
        resolved = htmllib.unescape("&" + m.group(1).decode("ascii") + ";")
        if resolved.startswith("&"):
            return b"&amp;" + m.group(1) + b";"
        return resolved.encode("utf-8")

    return _ENTITY.sub(sub, b)


def looks_like_feed(b: bytes) -> bool:
    head = b.lstrip()[:400].lower()
    return any(t in head for t in (b"<?xml", b"<rss", b"<feed", b"<rdf:rdf"))


# ------------------------------------------------------------------ feeds


@dataclass
class Item:
    title: str
    link: str
    when: datetime | None
    source: str
    key: str
    summary: str = ""      # description/abstract, cleaned; empty for most feeds


def _first_text(el, *paths) -> str:
    for p in paths:
        found = el.find(p)
        if found is not None and (found.text or "").strip():
            return found.text
    return ""


def parse_feed(body: bytes, source: str) -> list[Item]:
    """Parse RSS 2.0, Atom and RSS 1.0/RDF into a common shape."""
    if not looks_like_feed(body):
        raise FetchError("response is not a feed (HTML or portal page?)")
    try:
        root = ET.fromstring(xml_safe(body))
    except ET.ParseError as exc:
        raise FetchError(f"XML parse error: {exc}") from exc

    items = []
    nodes = (
        root.findall(".//item")
        or root.findall(f".//{_ATOM}entry")
        or root.findall(f".//{_RDF_ITEM}")
    )
    for node in nodes:
        title = clean_text(_first_text(node, "title", f"{_ATOM}title"), 200)

        link = _first_text(node, "link", f"{_RDF_ITEM}link").strip()
        if not link:
            # Atom puts the URL in an attribute, not in text.
            for ln in node.findall(f"{_ATOM}link"):
                rel = ln.get("rel", "alternate")
                if rel == "alternate" and ln.get("href"):
                    link = ln.get("href", "")
                    break
            if not link:
                ln = node.find(f"{_ATOM}link")
                link = (ln.get("href") if ln is not None else "") or ""

        when = parse_when(
            _first_text(node, "pubDate", "published", f"{_ATOM}published", f"{_ATOM}updated", "updated")
            or _first_text(node, "{http://purl.org/dc/elements/1.1/}date")
        )
        if not title or not link:
            continue
        summary = clean_text(
            _first_text(node, "description", f"{_ATOM}summary",
                        "{http://purl.org/rss/1.0/modules/content/}encoded"),
            420,
        )
        # Strip feed tracking params (?at_medium=RSS&at_campaign=rss) from the
        # link we actually show, not just from the dedupe key.
        items.append(
            Item(title=title, link=normalise_link(link), when=when, source=source,
                 key=item_key(node, link), summary=summary)
        )
    return items


_TRACKING = re.compile(r"^(utm_|at_medium|at_campaign|at_custom|traffic_source|CMP|ns_)", re.I)


def urlsplit_host(link: str) -> str:
    """Bare hostname for display ('zed.dev'), empty on anything unparsable."""
    try:
        host = urllib.parse.urlsplit(link).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def normalise_link(link: str) -> str:
    try:
        u = urllib.parse.urlsplit(link)
    except ValueError:
        return link
    q = [(k, v) for k, v in urllib.parse.parse_qsl(u.query) if not _TRACKING.match(k)]
    return urllib.parse.urlunsplit(
        ("https", u.netloc.lower(), u.path.rstrip("/"), urllib.parse.urlencode(q), "")
    )


def item_key(node, link: str) -> str:
    guid = node.find("guid")
    if guid is not None and (guid.text or "").strip() and guid.get("isPermaLink") != "false":
        return normalise_link(guid.text.strip())
    ident = node.find(f"{_ATOM}id")
    if ident is not None and (ident.text or "").strip():
        return ident.text.strip()
    return normalise_link(link)


_STOP = {"the", "a", "an", "of", "to", "in", "on", "for", "and", "as", "at", "is", "by", "with", "after"}


def title_fingerprint(title: str) -> str:
    """For cross-source dedupe. URL dedupe fails: the BBC appends tracking
    params and Google News wraps publisher links in a redirect."""
    words = re.findall(r"[a-z0-9]+", title.lower())
    return " ".join(sorted(w for w in words if w not in _STOP and len(w) > 2)[:8])
