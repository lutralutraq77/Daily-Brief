#!/usr/bin/env python3
"""Daily Brief -- generate a briefing with the Claude Code CLI, render it as a
standalone HTML page, and announce it with a Windows toast.

Subcommands
    run           Generate today's brief, write the HTML, fire a toast.
    open          Open the most recent brief in a window.
    toast-test    Fire a sample toast (plumbing check).
    render-last   Re-render the last raw markdown without calling Claude.
    icon          (Re)generate the notification icon.
    status        Print where things stand.

Nothing here needs third-party packages: the markdown renderer, the PNG icon
writer and the toast bridge are all stdlib + PowerShell.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html as htmllib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import urllib.parse
import zlib
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
BRIEFS_DIR = BASE / "briefs"
LOGS_DIR = BASE / "logs"
LOG_PATH = LOGS_DIR / "dailybrief.log"
STATE_PATH = BASE / "state.json"
ICON_PATH = BASE / "icon.png"
ICO_PATH = BASE / "icon.ico"
TOAST_PS1 = BASE / "toast.ps1"
LATEST_HTML = BRIEFS_DIR / "latest.html"

AUMID = "Local.DailyBrief"
LAUNCH_URI = "dailybrief:open"

# Windows: keep the child process from flashing a console window under pythonw.
CREATE_NO_WINDOW = 0x08000000

DEFAULT_CONFIG = {
    # local  = build the brief from keyless APIs. No credentials, no cost.
    # claude = same data, but Claude writes the prose (needs a signed-in CLI
    #          or ANTHROPIC_API_KEY).
    # auto   = claude if a credential happens to exist, else local.
    "engine": "local",

    # Edit city or postcode and the coordinates re-resolve on the next run.
    # Set latitude/longitude directly (with city and postcode blank) to pin them.
    "location": {
        "city": "",
        "postcode": "",
        "country": "",          # ISO code, e.g. GB, to disambiguate a city name
        "latitude": None,
        "longitude": None,
        "label": "",
        "resolved_from": "",    # internal: what the cached coordinates came from
    },
    "units": {
        "temperature": "celsius",     # celsius | fahrenheit
        "wind": "mph",                # mph | kmh | ms | kn
        "precipitation": "mm",        # mm | inch
        "clock": "24h",               # 24h | 12h
    },
    "sections": ["calendar", "threexthree", "weather", "news", "tech", "paper", "featured",
                 "onthisday", "bankholiday"],
    # Paper of the day: newest arXiv submission in these categories.
    "paper_categories": ["eess.AS", "cs.SD"],
    # News of the day: one story with its standfirst, from a single-story feed.
    "featured_feed": {"name": "Long Read",
                      "url": "https://www.theguardian.com/news/series/the-long-read/rss"},
    # Your own addresses, so declined invitations are not shown as if you were
    # going. Not secret -- the calendar URLs live in calendars.txt.
    "calendar_emails": [],
    "feeds": None,                    # None -> sources.DEFAULT_FEEDS
    "section_titles": {},             # override a section heading, e.g. {"audio": "Audio & DSP"}
    "tech_items": 2,
    "tech_min_points": 100,
    "bank_holiday_division": "england-and-wales",
    "deadline_seconds": 90,

    "claude_path": "auto",
    "model": "sonnet",
    "prompt_file": "prompt.md",
    "tools": [],
    "add_dirs": [],
    "timeout_seconds": 900,
    "max_budget_usd": 1.0,
    "extra_args": [],
    "toast": True,
    "auto_open": False,
    "keep_days": 60,
    "browser": "auto",
    "window_size": "1040,1120",
}

# Appended to whatever the user wrote in prompt.md, so the output is always
# shaped the same way regardless of how the free-form prompt is phrased.
OUTPUT_CONTRACT = """
--- OUTPUT CONTRACT (from the Daily Brief runner, not the user) ---
Respond with GitHub-flavored Markdown and nothing else. Specifically:
- The FIRST line must be exactly `TLDR: <one sentence, at most 110 characters>`.
- Then one blank line, then the brief itself.
- No preamble, no "here is your brief", no sign-off, no follow-up questions.
- Do not wrap the whole response in a code fence.
- Prefer `##` for section headings; keep the whole brief scannable in one screen
  or two. Use bullets over paragraphs. Bold the things that need a decision.
- If a tool call fails or a source is unavailable, say so in one short bullet
  under a `## Gaps` heading rather than guessing or inventing detail.
"""

AUTH_FIX_MD = """
The `claude` CLI on this machine is **not signed in**, so the brief could not be
generated. The desktop app having a session does not help: the CLI keeps its own.

Fix it once, in a normal terminal window (it opens a browser to sign in):

```
claude setup-token
```

Use `setup-token` rather than `claude auth login`. It issues a long-lived token,
which is what an unattended scheduled task needs -- an ordinary login session can
expire and would silently break the morning run.

Check it took, then generate a brief immediately:

```
claude auth status
python "%s" run --force --open
```
"""


class BriefError(Exception):
    """A failure worth explaining properly on the error page."""

    def __init__(self, message: str, markdown: str | None = None):
        super().__init__(message)
        self.markdown = markdown


# ---------------------------------------------------------------- infrastructure


def log(msg: str) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Every string on its way out gets scrubbed: a calendar URL is a bearer
    # token, and the log is the easiest place for one to end up.
    try:
        import netlib

        msg = netlib.scrub(msg)
    except ImportError:
        pass
    line = f"[{stamp}] {msg}\n"
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 512 * 1024:
            tail = LOG_PATH.read_text("utf-8", errors="replace").splitlines()[-1500:]
            LOG_PATH.write_text("\n".join(tail) + "\n", "utf-8")
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass
    if sys.stdout is not None:
        try:
            sys.stdout.write(line)
            sys.stdout.flush()
        except (OSError, ValueError):
            pass


def read_json(path: Path) -> dict:
    """Read JSON tolerantly.

    utf-8-sig, not utf-8: Notepad and PowerShell's `Set-Content -Encoding UTF8`
    both write a UTF-8 BOM, and plain utf-8 decoding then fails with
    "Unexpected UTF-8 BOM". That silently threw away the whole config -- exactly
    the kind of edit a person makes by hand.
    """
    data = json.loads(path.read_text("utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return data


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            loaded = read_json(CONFIG_PATH)
        except (OSError, ValueError) as exc:
            log(f"WARN: config.json unreadable ({exc}); USING DEFAULTS -- your settings are ignored")
            return cfg
        # Merge nested blocks so a partial edit keeps the untouched keys.
        for key, value in loaded.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                merged = dict(cfg[key])
                merged.update(value)
                cfg[key] = merged
            else:
                cfg[key] = value
    return cfg


def save_state(**kw) -> None:
    try:
        import netlib

        kw = {k: (netlib.scrub(v) if isinstance(v, str) else v) for k, v in kw.items()}
    except ImportError:
        pass
    state = {}
    if STATE_PATH.exists():
        try:
            state = read_json(STATE_PATH)
        except (OSError, ValueError):
            state = {}
    state.update(kw)
    try:
        STATE_PATH.write_text(json.dumps(state, indent=2), "utf-8")
    except OSError:
        pass


def resolve_claude(cfg: dict) -> str | None:
    configured = cfg.get("claude_path", "auto")
    if configured and configured != "auto" and Path(configured).exists():
        return configured
    found = shutil.which("claude")
    if found:
        return found
    for cand in (
        Path.home() / ".local" / "bin" / "claude.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "claude" / "claude.exe",
    ):
        if cand.exists():
            return str(cand)
    return None


def check_auth(claude: str) -> tuple[bool, str]:
    """Ask the CLI whether it is signed in. Returns (ok, method)."""
    try:
        proc = subprocess.run(
            [claude, "auth", "status"], capture_output=True, text=True, timeout=60,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        payload = json.loads((proc.stdout or "").strip() or "{}")
        return bool(payload.get("loggedIn")), str(payload.get("authMethod", "unknown"))
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        # Never block a run on an inconclusive check; let the real call decide.
        log(f"WARN: auth status check inconclusive ({exc})")
        return True, "unknown"


def auth_error() -> BriefError:
    return BriefError(
        "The claude CLI is not signed in. Run `claude setup-token` in a terminal.",
        AUTH_FIX_MD % (BASE / "dailybrief.py"),
    )


# ---------------------------------------------------------------- markdown


_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_MD_LINK = re.compile(r"\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_BARE_URL = re.compile(r"(?<![\"'>=])\bhttps?://[^\s<>()\[\]]+")


def _emphasis(text: str) -> str:
    text = re.sub(r"\*\*\*(?=\S)(.+?)(?<=\S)\*\*\*", r"<strong><em>\1</em></strong>", text)
    text = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*(?=\S)([^*]+?)(?<=\S)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(r"(?<![A-Za-z0-9_])_(?=\S)([^_]+?)(?<=\S)_(?![A-Za-z0-9_])", r"<em>\1</em>", text)
    text = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<del>\1</del>", text)
    return text


def inline(text: str) -> str:
    """Inline markdown -> HTML, with code spans and links stashed so later
    passes cannot corrupt them."""
    codes: list[str] = []
    links: list[tuple[str, str]] = []

    def stash_code(m: re.Match) -> str:
        codes.append(m.group(1))
        return f"\x00C{len(codes) - 1}\x00"

    def stash_link(m: re.Match) -> str:
        links.append((m.group(1), m.group(2)))
        return f"\x00L{len(links) - 1}\x00"

    text = _CODE_SPAN.sub(stash_code, text)
    text = _MD_LINK.sub(stash_link, text)
    text = htmllib.escape(text, quote=False)
    # target=_blank matters more than it looks: the brief opens in a chromeless
    # app window with no back button, so a link that navigates in place strands
    # you on the article with no way back to the brief.
    text = _BARE_URL.sub(
        lambda m: f'<a href="{m.group(0)}" target="_blank" rel="noopener noreferrer">{m.group(0)}</a>',
        text,
    )
    text = _emphasis(text)

    def pop_link(m: re.Match) -> str:
        label, href = links[int(m.group(1))]
        label = _emphasis(htmllib.escape(label, quote=False)) or htmllib.escape(href, quote=False)
        return (f'<a href="{htmllib.escape(href, quote=True)}" '
                f'target="_blank" rel="noopener noreferrer">{label}</a>')

    def pop_code(m: re.Match) -> str:
        return f"<code>{htmllib.escape(codes[int(m.group(1))], quote=False)}</code>"

    text = re.sub(r"\x00L(\d+)\x00", pop_link, text)
    text = re.sub(r"\x00C(\d+)\x00", pop_code, text)
    return text


_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_HR = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_ULI = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_OLI = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_FENCE = re.compile(r"^\s*(```+|~~~+)\s*([A-Za-z0-9_+-]*)\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")


def _split_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def markdown_to_html(md: str) -> str:
    lines = md.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    stack: list[tuple[str, int]] = []  # (tag, indent)
    para: list[str] = []
    quote: list[str] = []
    i = 0

    def close_lists(to_indent: int = -1) -> None:
        while stack and stack[-1][1] > to_indent:
            out.append(f"</li></{stack.pop()[0]}>")

    def flush_para() -> None:
        if para:
            out.append(f"<p>{inline(' '.join(para).strip())}</p>")
            para.clear()

    def flush_quote() -> None:
        if quote:
            out.append(f"<blockquote>{markdown_to_html(chr(10).join(quote))}</blockquote>")
            quote.clear()

    def flush_all(to_indent: int = -1) -> None:
        flush_para()
        flush_quote()
        close_lists(to_indent)

    while i < len(lines):
        line = lines[i]

        fence = _FENCE.match(line)
        if fence:
            flush_all()
            marker, lang = fence.group(1), fence.group(2)
            body: list[str] = []
            i += 1
            while i < len(lines) and not (
                lines[i].strip().startswith(marker[0] * len(marker))
                and set(lines[i].strip()) <= set(marker[0])
            ):
                body.append(lines[i])
                i += 1
            cls = f' class="lang-{htmllib.escape(lang, quote=True)}"' if lang else ""
            code = htmllib.escape("\n".join(body), quote=False)
            out.append(f"<pre><code{cls}>{code}</code></pre>")
            i += 1
            continue

        if not line.strip():
            flush_all()
            i += 1
            continue

        if line.lstrip().startswith(">"):
            flush_para()
            close_lists()
            quote.append(re.sub(r"^\s*>\s?", "", line))
            i += 1
            continue
        flush_quote()

        if _HR.match(line):
            flush_all()
            out.append("<hr>")
            i += 1
            continue

        heading = _HEADING.match(line)
        if heading:
            flush_all()
            level = len(heading.group(1))
            out.append(f"<h{level}>{inline(heading.group(2))}</h{level}>")
            i += 1
            continue

        # GFM table: a header row followed by a |---|---| separator.
        if "|" in line and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            flush_all()
            head = _split_row(line)
            aligns = []
            for cell in _split_row(lines[i + 1]):
                left, right = cell.startswith(":"), cell.endswith(":")
                aligns.append("center" if left and right else "right" if right else "left")
            rows = []
            i += 2
            while i < len(lines) and lines[i].strip() and "|" in lines[i]:
                rows.append(_split_row(lines[i]))
                i += 1
            th = "".join(
                f'<th style="text-align:{aligns[n] if n < len(aligns) else "left"}">{inline(c)}</th>'
                for n, c in enumerate(head)
            )
            body_html = ""
            for row in rows:
                tds = "".join(
                    f'<td style="text-align:{aligns[n] if n < len(aligns) else "left"}">{inline(c)}</td>'
                    for n, c in enumerate(row)
                )
                body_html += f"<tr>{tds}</tr>"
            out.append(
                f'<div class="table-wrap"><table><thead><tr>{th}</tr></thead>'
                f"<tbody>{body_html}</tbody></table></div>"
            )
            continue

        uli, oli = _ULI.match(line), _OLI.match(line)
        if uli or oli:
            flush_para()
            indent = len(((uli or oli).group(1)).expandtabs(4))
            tag = "ul" if uli else "ol"
            content = uli.group(2) if uli else oli.group(3)
            if stack and indent > stack[-1][1]:
                out.append(f"<{tag}>")
                stack.append((tag, indent))
            else:
                close_lists(indent)
                if stack and stack[-1][1] == indent:
                    if stack[-1][0] != tag:
                        out.append(f"</li></{stack.pop()[0]}>")
                        out.append(f"<{tag}>")
                        stack.append((tag, indent))
                    else:
                        out.append("</li>")
                else:
                    out.append(f"<{tag}>")
                    stack.append((tag, indent))
            out.append(f"<li>{inline(content)}")
            i += 1
            continue

        if stack:
            # A plain line under a bullet is a continuation of that bullet.
            out.append(" " + inline(line.strip()))
            i += 1
            continue

        para.append(line.strip())
        i += 1

    flush_all()
    return "\n".join(out)


# ---------------------------------------------------------------- page render


# The visual design is Danny's "Daily brief app template" from claude.ai/design
# (project a4819508, a cherry-red retheme of the Nocturne design system).
# Canvas artifacts from the design tool -- absolute positioning, fixed pixel
# widths on text -- are deliberately not ported; the token values are verbatim.
PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<base target="_blank">
<title>{title}</title>
<meta name="generated" content="{generated}">
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
:root {{
  color-scheme: dark;
  /* Nocturne token roles, cherry ramp -- every value from the design file. */
  --color-bg: #1b1216;
  --color-surface: #2a1b21;
  --color-text: #f2dde1;
  --color-accent: #ef4a5f;
  --color-divider: color-mix(in srgb, #f2dde1 15%, transparent);
  --color-accent-100: #fff1f3; --color-accent-200: #ffdee2; --color-accent-300: #ffb9c2;
  --color-accent-400: #f8808f; --color-accent-500: #ef4a5f; --color-accent-600: #cb3a4d;
  --color-accent-700: #9d2c3c; --color-accent-800: #6e202c; --color-accent-900: #45161d;
  --color-neutral-100: #f6f2f3; --color-neutral-800: #453a3e; --color-neutral-900: #2e2529;
  --font-heading: "Inter", system-ui, sans-serif;
  --font-heading-weight: 500;
  --font-body: "Inter", system-ui, sans-serif;
  --space-1: 2.8px; --space-2: 5.6px; --space-3: 8.4px;
  --space-4: 11.2px; --space-6: 16.8px; --space-8: 22.4px;
  --radius-sm: 4px; --radius-md: 8px; --radius-lg: 14px;
  /* Legacy aliases so the markdown/prose path and error pages share the theme */
  --bg: var(--color-bg); --panel: var(--color-surface); --ink: var(--color-text);
  --muted: color-mix(in srgb, var(--color-text) 55%, transparent);
  --line: var(--color-divider); --accent: var(--color-accent);
  --accent-soft: color-mix(in srgb, var(--color-accent) 14%, transparent);
  --code-bg: var(--color-neutral-900);
  --shadow: 0 0 0 1px #453a3e, 0 6px 18px rgba(0,0,0,0.55);
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; padding: 0 var(--space-6) var(--space-8);
  background: var(--color-bg); color: var(--color-text);
  font-family: var(--font-body); font-size: 15px; line-height: 1.55;
  -webkit-font-smoothing: antialiased; max-width: 1180px;
}}
h1, h2, h3, h4, h5, h6 {{
  font-family: var(--font-heading); font-weight: var(--font-heading-weight);
  line-height: 1.12; letter-spacing: -0.015em; margin: 0 0 var(--space-2);
}}
h1 {{ font-size: 42px; }}
h6 {{ font-size: 13px; letter-spacing: 0.08em; text-transform: uppercase; }}
a {{ color: var(--color-accent); text-underline-offset: 3px; }}
:focus {{ outline: none; }}
:focus-visible {{ outline: 2px solid var(--color-accent); outline-offset: 2px; }}
::selection {{ background: color-mix(in srgb, var(--color-accent) 30%, transparent); }}

.topbar {{
  position: sticky; top: 0; z-index: 5;
  display: flex; align-items: center; gap: var(--space-4);
  padding: var(--space-3) 0 var(--space-4);
  background: linear-gradient(var(--color-bg) 78%, transparent);
}}
.brand {{ font-family: var(--font-heading); font-weight: var(--font-heading-weight); font-size: 18px; margin-right: auto; }}
.btn {{
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  cursor: pointer; text-decoration: none;
  font-family: var(--font-heading); font-weight: var(--font-heading-weight);
  font-size: 14px; line-height: 1.2; color: var(--color-accent);
  background: transparent; border: 1px solid var(--color-accent);
  padding: var(--space-2) calc(var(--space-3) * 1.2); border-radius: var(--radius-md);
}}
.btn:hover {{ background: color-mix(in srgb, var(--color-accent) 12%, transparent); }}
.btn:active {{ background: color-mix(in srgb, var(--color-accent) 22%, transparent); }}
.btn svg {{ width: 15px; height: 15px; flex: none; }}
.btn.busy {{ opacity: 0.55; pointer-events: none; }}

.brief {{ max-width: 660px; margin-left: min(6vw, calc(var(--space-8) * 3)); }}
.lede {{ font-size: 16px; line-height: 1.5; margin: 0 0 var(--space-2); max-width: 46ch; text-wrap: pretty; }}
.placeline {{ margin: 0 0 var(--space-2); font-size: 17px; color: color-mix(in srgb, var(--color-text) 78%, transparent); }}

.sect {{ margin: var(--space-8) 0 var(--space-4); display: flex; align-items: center; gap: var(--space-2); }}
.sect h6 {{ margin: 0; color: color-mix(in srgb, var(--color-text) 62%, transparent); }}
.sect::before {{ content: ""; width: 2px; height: 13px; flex: none; background: var(--color-accent); border-radius: 1px; }}

.wx {{ display: flex; align-items: flex-end; gap: var(--space-6); flex-wrap: wrap; }}
.wx-now {{ display: flex; align-items: baseline; gap: 2px; }}
.wx-now .n {{ font-family: var(--font-heading); font-weight: var(--font-heading-weight); font-size: 38px; line-height: 0.9; letter-spacing: -0.02em; }}
.wx-now .u {{ font-size: 16px; color: var(--color-accent); }}
.wx-stats {{ display: flex; gap: var(--space-6); flex-wrap: wrap; padding-bottom: 3px; }}
.wx-stat {{ display: grid; gap: 1px; }}
.wx-stat .k {{ font-size: 10px; letter-spacing: 0.1em; text-transform: uppercase; color: color-mix(in srgb, var(--color-text) 45%, transparent); }}
.wx-stat .v {{ font-size: 15px; }}
.wx-cond {{ font-size: 14px; color: color-mix(in srgb, var(--color-text) 72%, transparent); margin-top: var(--space-2); }}

.tl {{ display: grid; gap: var(--space-1); }}
.tl-row {{ display: grid; grid-template-columns: 64px 5px 1fr; align-items: stretch; gap: var(--space-3); padding: var(--space-2) 0; }}
.tl-times {{ display: grid; gap: 2px; font-size: 13px; font-variant-numeric: tabular-nums; padding-top: 1px; align-content: start; }}
.tl-times .s {{ color: color-mix(in srgb, var(--color-text) 85%, transparent); }}
.tl-times .e {{ color: color-mix(in srgb, var(--color-text) 66%, transparent); }}
.tl-bar {{ width: 3px; min-height: 30px; border-radius: 2px; background: var(--color-accent); align-self: start; height: 100%; }}
.tl-bar.dim {{ background: color-mix(in srgb, var(--color-accent) 45%, transparent); }}
.tl-body {{ display: grid; gap: 2px; align-content: start; }}
.tl-title {{ font-size: 15px; }}
.tl-meta {{ font-size: 11px; color: color-mix(in srgb, var(--color-text) 60%, transparent); }}

.stories {{ display: grid; gap: var(--space-1); }}
.story {{
  display: grid; grid-template-columns: auto 1fr; gap: var(--space-3); align-items: baseline;
  padding: var(--space-2) var(--space-2) var(--space-2) 0; border-radius: var(--radius-sm);
}}
.story:hover {{ background: color-mix(in srgb, var(--color-text) 4%, transparent); }}
.story .tag {{ align-self: center; }}
.story a {{ color: var(--color-text); text-decoration: none; font-size: 15px; line-height: 1.4; }}
.story a:hover {{ color: var(--color-accent-300); text-decoration: underline; }}
.story .t {{ font-size: 11px; font-variant-numeric: tabular-nums; color: color-mix(in srgb, var(--color-text) 38%, transparent); margin-left: var(--space-2); white-space: nowrap; }}

.tag {{ display: inline-flex; align-items: center; font-size: 11px; letter-spacing: 0.02em; padding: 3px 10px; border-radius: calc(var(--radius-md) * 0.75); }}
.tag-accent {{ background: var(--color-accent-800); color: var(--color-accent-100); }}
.tag-neutral {{ background: var(--color-neutral-800); color: var(--color-neutral-100); }}
.tag-outline {{ border: 1px solid var(--color-accent); color: var(--color-accent); }}

.feature {{ display: grid; gap: var(--space-2); max-width: 58ch; }}
.feature-meta {{ display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap; }}
.feature-meta .t {{ font-size: 11px; font-variant-numeric: tabular-nums; color: color-mix(in srgb, var(--color-text) 55%, transparent); }}
.feature-title {{ color: var(--color-text); text-decoration: none; font-size: 17px; line-height: 1.35; }}
a.feature-title:hover {{ color: var(--color-accent-300); text-decoration: underline; }}
.feature-body {{ margin: 0; font-size: 14px; line-height: 1.55; color: color-mix(in srgb, var(--color-text) 78%, transparent); text-wrap: pretty; }}
.feature-foot {{ font-size: 12px; color: color-mix(in srgb, var(--color-text) 55%, transparent); }}

.otd {{ font-size: 14px; line-height: 1.55; max-width: 56ch; color: color-mix(in srgb, var(--color-text) 82%, transparent); text-wrap: pretty; }}
.otd .yr {{ color: var(--color-accent); font-family: var(--font-heading); font-weight: var(--font-heading-weight); }}

.unavail {{ font-size: 13px; color: color-mix(in srgb, var(--color-text) 55%, transparent); font-style: italic; }}
/* The 3x3 section quotes commands you are meant to type, outside .prose. */
.feature-body code, .wx-cond code {{ font: 12px/1.5 ui-monospace, Consolas, monospace; background: var(--code-bg); padding: 1.5px 5px; border-radius: var(--radius-sm); }}
.notice {{
  margin: var(--space-4) 0; padding: var(--space-3) var(--space-4);
  border: 1px solid var(--color-accent-700); border-radius: var(--radius-md);
  background: color-mix(in srgb, var(--color-accent) 8%, transparent); font-size: 14px;
}}

.foot {{ margin-top: var(--space-8); display: flex; align-items: center; gap: var(--space-2); font-size: 11px; color: color-mix(in srgb, var(--color-text) 42%, transparent); }}
.foot .dot {{ width: 6px; height: 6px; border-radius: 50%; background: var(--color-accent); flex: none; }}
.foot .dot.bad {{ background: var(--color-accent-700); }}

/* Prose fallback: the Claude-engine and error paths render markdown here. */
.prose {{ max-width: 62ch; }}
.prose h2 {{ font-size: 13px; letter-spacing: 0.08em; text-transform: uppercase; color: color-mix(in srgb, var(--color-text) 62%, transparent); margin: var(--space-8) 0 var(--space-3); }}
.prose p {{ margin: 0 0 var(--space-3); }}
.prose ul, .prose ol {{ margin: 0 0 var(--space-3); padding-left: 22px; }}
.prose li {{ margin: 5px 0; }}
.prose a {{ color: var(--color-text); }}
.prose a:hover {{ color: var(--color-accent-300); }}
.prose code {{ font: 13px/1.5 ui-monospace, Consolas, monospace; background: var(--code-bg); padding: 1.5px 5px; border-radius: var(--radius-sm); }}
.prose pre {{ background: var(--code-bg); border: 1px solid var(--line); border-radius: var(--radius-md); padding: 13px 15px; overflow-x: auto; }}
.prose blockquote {{ margin: 0 0 var(--space-3); padding: 2px 0 2px 16px; border-left: 3px solid var(--color-accent); color: var(--muted); }}
.prose .table-wrap {{ overflow-x: auto; margin: 0 0 var(--space-4); }}
.prose table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
.prose th, .prose td {{ padding: 8px 11px; border-bottom: 1px solid var(--line); }}
.prose .error {{ background: var(--accent-soft); border: 1px solid var(--color-accent-700); border-radius: var(--radius-md); padding: 16px 18px; }}
</style>
<div class="topbar">
  <span class="brand">Daily brief</span>
  <a class="btn" id="refresh" href="dailybrief:refresh" target="_self" title="Regenerate today's brief">
    <svg viewBox="0 0 256 256" fill="none" stroke="currentColor" stroke-width="18" stroke-linecap="round" stroke-linejoin="round"><path d="M224 128a96 96 0 1 1-28-68"/><path d="M196 24v40h-40"/></svg>
    Refresh
  </a>
</div>
<main class="brief">
  <header>
    <h1>{heading}</h1>
    {lede}
  </header>
  {body}
</main>
<script>
// The protocol handler regenerates latest.html in place; this page reloads
// itself until the embedded generation stamp changes. State lives in
// sessionStorage because location.reload() destroys every timer, so a
// slow regeneration would otherwise strand the old page after one poll.
(function () {{
  var STAMP = (document.querySelector('meta[name=generated]') || {{}}).content || '';
  var btn = document.getElementById('refresh');
  var busy = function () {{
    if (!btn) return;
    btn.classList.add('busy');
    btn.lastChild.textContent = ' Refreshing…';
  }};
  try {{
    var want = sessionStorage.getItem('db-refresh-from');
    var tries = parseInt(sessionStorage.getItem('db-refresh-tries') || '0', 10);
    if (want !== null) {{
      if (want !== STAMP || tries >= 15) {{
        sessionStorage.removeItem('db-refresh-from');   // new content arrived, or give up
        sessionStorage.removeItem('db-refresh-tries');
      }} else {{
        busy();
        sessionStorage.setItem('db-refresh-tries', String(tries + 1));
        setTimeout(function () {{ location.replace('latest.html'); }}, 4000);
      }}
    }}
  }} catch (e) {{ /* sessionStorage unavailable: refresh still works, one shot */ }}
  // replace('latest.html'), not reload(): a window showing an archived day's
  // file would reload a page Refresh never rewrites and appear to do nothing.
  // Dated briefs and latest.html are siblings, so the relative path holds.
  if (btn) btn.addEventListener('click', function () {{
    busy();
    try {{
      sessionStorage.setItem('db-refresh-from', STAMP);
      sessionStorage.setItem('db-refresh-tries', '0');
    }} catch (e) {{}}
    setTimeout(function () {{ location.replace('latest.html'); }}, 4000);
  }});
}})();
</script>
"""


def render_page(*, heading: str, lede: str, body_html: str, meta_bits: list[str], footer: str) -> str:
    """Prose shell: markdown-derived bodies (Claude engine, error pages,
    render-last) inside the same cherry chrome the structured brief uses."""
    lede_html = f'<p class="lede">{inline(lede)}</p>' if lede else ""
    meta = " · ".join(htmllib.escape(b) for b in [*meta_bits, footer] if b)
    foot = (f'<div class="foot"><span class="dot"></span><span>{meta}</span></div>' if meta else "")
    return PAGE.format(
        title=htmllib.escape(f"Daily Brief - {heading}"),
        heading=htmllib.escape(heading),
        lede=lede_html,
        body=f'<div class="prose">{body_html}</div>{foot}',
        generated=dt.datetime.now().isoformat(timespec="seconds"),
    )


def split_tldr(md: str) -> tuple[str, str]:
    """Peel the `TLDR:` contract line off the front, if the model honoured it."""
    lines = md.strip().split("\n")
    for idx, line in enumerate(lines[:4]):
        m = re.match(r"^\s*(?:\*\*)?TL;?DR:?(?:\*\*)?\s*(.+?)\s*(?:\*\*)?$", line, re.I)
        if m and m.group(1).strip():
            rest = "\n".join(lines[:idx] + lines[idx + 1 :]).strip()
            return m.group(1).strip(), rest
    # Fall back to the first real sentence so the toast still says something.
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and not _HR.match(s):
            return re.sub(r"[*_`]", "", s)[:110], md.strip()
    return "Your brief is ready.", md.strip()


# ---------------------------------------------------------------- composition


DEFAULT_SECTION_TITLES = {
    "news": "Headlines",
    "tech": "Tech",
    "audio": "Audio & DSP",
    "sport": "Sport",
    "science": "Science",
    "local": "Local",
}


def _md_link(text: str, url: str) -> str:
    """Markdown-safe link text: ] and ) in a headline would break the link."""
    safe = text.replace("[", "(").replace("]", ")")
    return f"[{safe}]({url})" if url else safe


def compose_markdown(cfg: dict, today: dt.date, secs: dict, notices: list[str]) -> str:
    """Turn collected sections into the brief.

    Deliberately terse: one screen, headlines only, no feed descriptions, and a
    fixed section order so the eye learns where to look. Sections that failed
    say so out loud -- a missing section and a broken one must never look alike.
    """
    import sources as S

    out: list[str] = []
    head: list[str] = []

    # --- TLDR ---------------------------------------------------------------
    w = secs.get("weather")
    u = (w.data.get("units") if w and w.usable else None) or S.units_from(cfg.get("units"))
    TEMP, WIND, PRECIP = u["temperature_symbol"], u["wind_symbol"], u["precipitation_symbol"]

    bits = []
    if w and w.usable:
        d = w.data
        cond = d["condition"]
        degree = TEMP[:1]          # "°" from "°C", or "" if a symbol ever lacks it
        bits.append(f"{cond}, {S.fmt(d['high'], degree)}/{S.fmt(d['low'], TEMP)}")
        if d["precip_prob"] is not None and d["precip_prob"] >= 30:
            bits.append(f"rain {S.fmt(d['precip_prob'], '%')}")
    n_news = sum(
        len(s.data) for k, s in secs.items() if k.startswith("feed:") and s.usable
    )
    n_tech = len(secs["tech"].data[: int(cfg.get("tech_items", 2))]) if secs.get("tech") and secs["tech"].usable else 0
    if n_news or n_tech:
        bits.append(f"{n_news + n_tech} things to read")
    tldr = ", ".join(bits) if bits else "Data sources were unavailable this morning."
    out.append(f"TLDR: {tldr[:110]}")
    out.append("")

    for note in notices:
        head.append(f"> **{note}**")
    if head:
        out.extend(head + [""])

    # --- Calendar, first: it is the thing you open the brief for ------------
    calsec = secs.get("calendar")
    if calsec is not None:
        out.append("## Today's calendar")
        if calsec.usable:
            for ev in calsec.data:
                when = "all day" if ev["all_day"] else f"{ev['start']:%H:%M}"
                title = ev["summary"] or "(no title)"
                label = f" · {ev['calendar']}" if ev.get("calendar") else ""
                flag = f"  *[{'; '.join(ev['warnings'])}]*" if ev.get("warnings") else ""
                out.append(f"- **{when}** — {title}{label}{flag}")
            if calsec.reason:
                out.append(f"- *{calsec.reason}*")
        elif calsec.status == S.EMPTY:
            # Three distinct outcomes, never one ambiguous blank.
            if "no calendars configured" in calsec.reason:
                out.append("*No calendar connected — see `calendars.txt`.*")
            else:
                out.append(f"*Nothing scheduled.{' ' + calsec.reason if calsec.reason else ''}*")
        else:
            out.append(f"*Calendar unavailable — {calsec.reason}*")
        out.append("")

    # --- Today --------------------------------------------------------------
    out.append("## Today")
    if w is None:
        pass
    elif w.usable:
        d = w.data
        line = f"{d['condition']}, **{S.fmt(d['high'], TEMP)} / {S.fmt(d['low'], TEMP)}**"
        if d["precip_prob"] is not None:
            line += f", rain {S.fmt(d['precip_prob'], '%')}"
        if d["precip_mm"]:
            line += f" ({S.fmt(d['precip_mm'], PRECIP, 1)})"
        if d["wind_mph"] is not None:
            line += f", wind {S.fmt(d['wind_mph'], WIND)}"
        out.append(line)
        # The daily code is the day's worst condition, so it can say "Overcast"
        # on a morning that is currently clear. Show both or it reads wrong.
        now = []
        if d["now_temp"] is not None:
            now.append(f"Now **{S.fmt(d['now_temp'], TEMP)}**")
            if d["now_condition"] and d["now_condition"] != d["condition"]:
                now[-1] += f" ({d['now_condition']})"
        if d["sunrise"] and d["sunset"]:
            now.append(f"sun {d['sunrise']}–{d['sunset']}")
        if now:
            out.append(" · ".join(now) + ".")
    else:
        out.append(f"*Weather unavailable — {w.reason}*")

    bh = secs.get("bankholiday")
    if bh and bh.usable:
        d = bh.data
        when = "today" if d["days"] == 0 else ("tomorrow" if d["days"] == 1 else f"in {d['days']} days")
        out.append(f"**{d['title']}** bank holiday {when} ({d['date']:%a %d %b}).")
    elif bh and bh.status == S.FAILED:
        out.append(f"*Bank holidays unavailable — {bh.reason}*")
    out.append("")

    # --- 3x3 ----------------------------------------------------------------
    tx = secs.get("threexthree")
    if tx is not None:
        import plan as P

        out.append("## 3x3")
        if tx.usable:
            st = tx.data
            blk, state = st.get("block"), st.get("block_state")
            if state == "active":
                left = blk["days_left"]
                left_txt = "last day" if left == 0 else f"{left} day{'s' if left != 1 else ''} left"
                out.append(f"**{blk['name']}** — month {blk['number']} of {blk['topic_count']}, "
                           f"week {blk['week']}, {left_txt}.")
                if blk["due"] == "output":
                    out.append("- **This week: produce the output.** "
                               + ("Already done." if blk["output_done"] else
                                  "Ten minutes, one sitting, no editing."))
                else:
                    src = blk["source"]
                    label = P.SLOT_LABELS.get(blk["due"], blk["due"])
                    if src:
                        out.append(f"- **This week: {label}** — {_md_link(src['title'], src['url'])}")
                    else:
                        out.append(f"- **This week: {label}** — *nothing lined up for this slot.*")
                out.append(f"- Output due by {blk['month_end']:%a %d %b}.")
            elif state == "not_started":
                out.append(f"Block starts {blk['starts']:%a %d %b}.")
            elif state == "finished":
                out.append(f"Block finished {blk['ended']:%d %b} — pick three new topics.")

            ses = st.get("session")
            if ses and state == "active":
                if not ses.get("checked"):
                    out.append(f"- *Session not placed — {ses['why']}.*")
                elif ses.get("found"):
                    out.append(f"- **Session {_session_when(ses['start'], ses['end'], today)}** "
                               f"({ses['minutes']} min, free in your calendar).")
                    if ses.get("caveat"):
                        out.append(f"  - *{ses['caveat']}.*")
                else:
                    out.append(f"- **No free {ses['minutes']}-minute window** before "
                               f"{ses['until']:%a %d %b}.")
                    if ses.get("caveat"):
                        out.append(f"  - *{ses['caveat']}.*")

            wk = st["weekly"]
            if wk["changes"]:
                stale = " *(last week's — due for review)*" if wk["stale"] else ""
                out.append(f"\nThis week's three{stale}:")
                for i, change in enumerate(wk["changes"], 1):
                    out.append(f"{i}. {change}")
            if wk["review_due"]:
                out.append(f"\n*{wk['review_day'].title()} review — score these and pick three new ones.*")
            if tx.reason:
                out.append(f"\n*{tx.reason}*")
        elif tx.status == S.EMPTY:
            out.append(f"*{tx.reason}*")
        else:
            out.append(f"*3x3 plan unavailable — {tx.reason}*")
        out.append("")

    # --- Feed sections, in the order the feeds are configured ---------------
    titles = dict(DEFAULT_SECTION_TITLES, **(cfg.get("section_titles") or {}))
    for section in S.feed_sections(cfg):
        sec = secs.get(f"feed:{section}")
        if sec is None:
            continue
        out.append(f"## {titles.get(section, section.replace('-', ' ').title())}")
        if sec.usable:
            for it in sec.data:
                when = f" {it.when.astimezone():%H:%M}" if it.when else ""
                out.append(f"- **{it.source}**{when} — {_md_link(it.title, it.link)}")
            if sec.reason:
                out.append(f"- *partial: {sec.reason}*")
        else:
            out.append(f"*Unavailable — {sec.reason}*")
        out.append("")

    # --- Tech ---------------------------------------------------------------
    techs = secs.get("tech")
    if techs is not None:
        out.append("## Tech")
        if techs.usable:
            for it in techs.data[: int(cfg.get("tech_items", 2))]:
                out.append(f"- **{it.source}** — {_md_link(it.title, it.link)}")
        elif techs.status == S.EMPTY:
            out.append(f"*Nothing above the score threshold ({techs.reason}).*")
        else:
            out.append(f"*Tech unavailable — {techs.reason}*")
        out.append("")

    # --- On this day --------------------------------------------------------
    otd = secs.get("onthisday")
    if otd is not None and otd.usable:
        out.append("## On this day")
        out.append(f"**{otd.data['year']}** — {otd.data['text']}")
        out.append("")
    elif otd is not None and otd.status == S.FAILED:
        out.append("## On this day")
        out.append(f"*Unavailable — {otd.reason}*")
        out.append("")

    # --- Status footer ------------------------------------------------------
    # This single line is what turns every silent degradation into a visible
    # one: a source that quietly starts 403ing shows up here, not as a brief
    # that just gets shorter month by month.
    total = len(secs)
    ok = sum(1 for s in secs.values() if s.status in (S.OK, S.EMPTY))
    broken = [f"{k} ({s.reason[:40]})" for k, s in sorted(secs.items()) if s.status == S.FAILED]
    out.append("---")
    if broken:
        out.append(f"Sources {ok}/{total} — failed: " + ", ".join(broken))
    else:
        out.append(f"Sources {ok}/{total} OK")

    import netlib

    return netlib.scrub("\n".join(out).strip())


def _location_fingerprint(loc: dict) -> str:
    """What the stored coordinates were derived from."""
    return "|".join(
        str(loc.get(k, "") or "").strip().lower() for k in ("postcode", "city", "country")
    )


def resolve_location(cfg: dict, *, save: bool = True) -> tuple[dict, str | None]:
    """Turn the location settings into coordinates, re-resolving when they change.

    This is what makes `location` a real setting rather than a display field: if
    you hand-edit `city` or `postcode` in config.json, the cached lat/lon no
    longer matches its fingerprint, so it is recomputed instead of silently
    giving you last month's city's weather.

    Precedence: explicit lat/lon > postcode > city.
    """
    import sources as S

    loc = dict(cfg.get("location") or {})
    want = _location_fingerprint(loc)
    has_coords = loc.get("latitude") is not None and loc.get("longitude") is not None

    # Coordinates typed in by hand, with nothing to derive them from: trust them.
    if has_coords and not (loc.get("postcode") or loc.get("city")):
        return loc, None
    # Cache still matches what it was derived from.
    if has_coords and loc.get("resolved_from") == want:
        return loc, None

    if loc.get("postcode"):
        sec = S.postcode_lookup(loc["postcode"])
        source = f"postcode {loc['postcode']}"
    elif loc.get("city"):
        sec = S.geocode(loc["city"], loc.get("country", ""))
        source = f"city {loc['city']}"
    else:
        return loc, "no location set — run `dailybrief.py setup \"Your City\"`"

    if not sec.usable:
        if has_coords:
            # Keep working with the stale coordinates, but say so out loud.
            return loc, f"could not re-resolve {source} ({sec.reason}); using previous coordinates"
        return loc, f"could not resolve {source}: {sec.reason}"

    d = sec.data
    loc.update(
        latitude=d["latitude"], longitude=d["longitude"], label=d["label"], resolved_from=want
    )
    log(f"location re-resolved from {source} -> {d['label']} ({d['latitude']}, {d['longitude']})")

    division = _uk_division(d)
    if division:
        cfg["bank_holiday_division"] = division

    if save:
        cfg["location"] = loc
        try:
            CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), "utf-8")
        except OSError as exc:
            log(f"WARN: could not cache resolved location ({exc})")
    return loc, None


def _uk_division(place: dict) -> str | None:
    """Scotland and Northern Ireland have genuinely different bank holidays."""
    text = f"{place.get('country', '')} {place.get('label', '')}"
    if "Scotland" in text:
        return "scotland"
    if "Northern Ireland" in text:
        return "northern-ireland"
    if place.get("country_code") == "GB" or "England" in text or "Wales" in text:
        return "england-and-wales"
    return None


def _esc(s) -> str:
    return htmllib.escape(str(s or ""), quote=True)


def _duration_text(start, end) -> str:
    if start is None or end is None or end <= start:
        return ""
    mins = int((end - start).total_seconds() // 60)
    if mins < 60:
        return f"{mins} min"
    h, m = divmod(mins, 60)
    return f"{h} hr" if m == 0 else f"{h} hr {m:02d}"


def _session_when(start: dt.datetime, end: dt.datetime, today: dt.date) -> str:
    """'today 20:15-21:00' / 'tomorrow ...' / 'Thu 21 Aug ...'."""
    day = start.date()
    delta = (day - today).days
    label = "today" if delta == 0 else ("tomorrow" if delta == 1 else f"{start:%a %d %b}")
    return f"{label} {start:%H:%M}–{end:%H:%M}"


def _sect(title: str) -> str:
    return f'<div class="sect"><h6>{_esc(title)}</h6></div>'


def _unavail(reason: str) -> str:
    return f'<p class="unavail">Unavailable — {_esc(reason)}</p>'


def compose_page(cfg: dict, today: dt.date, secs: dict, notices: list[str],
                 stats: dict, tldr: str) -> str:
    """Render collected sections straight into the design's layout.

    Ported from Danny's "Daily brief app template" (claude.ai/design). Every
    element in that mock is generated from real data here, and every section
    keeps the ok/empty/failed distinction the mock had no reason to carry.
    """
    import netlib
    import plan as P
    import sources as S

    B: list[str] = []

    for note in notices:
        B.append(f'<div class="notice">{_esc(note)}</div>')

    # --- Today: place line + weather ----------------------------------------
    B.append(_sect("Today"))
    loc_label = ((cfg.get("location") or {}).get("label") or "").split(",")[0]
    B.append(f'<p class="placeline">{_esc(loc_label or "Location not set")} · '
             f'{dt.datetime.now():%H:%M}</p>')
    w = secs.get("weather")
    if w is not None and w.usable:
        d = w.data
        u = d.get("units") or S.units_from(cfg.get("units"))
        deg, unit_letter = "°", u["temperature_symbol"].lstrip("°")
        stats_bits = [
            ("High / low", f"{S.fmt(d['high'], deg)} / {S.fmt(d['low'], deg)}"),
            ("Rain", S.fmt(d["precip_prob"], "%")),
            ("Wind", S.fmt(d["wind_mph"], u["wind_symbol"])),
        ]
        if d.get("sunrise") and d.get("sunset"):
            stats_bits.append(("Sun", f"{d['sunrise']} – {d['sunset']}"))
        cells = "".join(
            f'<div class="wx-stat"><span class="k">{_esc(k)}</span>'
            f'<span class="v">{_esc(v)}</span></div>' for k, v in stats_bits
        )
        now_n = S.fmt(d["now_temp"]) if d.get("now_temp") is not None else S.fmt(d["high"])
        B.append(
            f'<div class="wx"><div class="wx-now"><span class="n">{_esc(now_n)}</span>'
            f'<span class="u">°{_esc(unit_letter)}</span></div>'
            f'<div class="wx-stats">{cells}</div></div>'
        )
        cond = d.get("condition") or ""
        now_cond = d.get("now_condition") or ""
        line = cond + (f". Currently {now_cond.lower()}." if now_cond and now_cond != cond else ".")
        B.append(f'<p class="wx-cond">{_esc(line)}</p>')
    elif w is not None:
        B.append(_unavail(w.reason))

    bh = secs.get("bankholiday")
    if bh is not None and bh.usable:
        d = bh.data
        when = "today" if d["days"] == 0 else ("tomorrow" if d["days"] == 1 else f"in {d['days']} days")
        B.append(f'<p class="wx-cond"><strong>{_esc(d["title"])}</strong> bank holiday '
                 f'{_esc(when)} ({d["date"]:%a %d %b}).</p>')

    # --- Calendar ------------------------------------------------------------
    cal = secs.get("calendar")
    if cal is not None:
        B.append(_sect("Calendar"))
        if cal.usable:
            rows = []
            for ev in cal.data:
                warn = f' <span class="t">[{_esc("; ".join(ev["warnings"]))}]</span>' if ev.get("warnings") else ""
                meta_bits = [b for b in (ev.get("calendar"),) if b]
                if ev["all_day"]:
                    s_txt, e_txt = "all day", ""
                    if ev.get("last_day") and ev["last_day"] != today:
                        e_txt = f"to {ev['last_day']:%d %b}"
                        meta_bits.append("multi-day")
                    dim = " dim" if e_txt else ""
                else:
                    s_txt = f"{ev['start']:%H:%M}"
                    e_txt = f"{ev['end']:%H:%M}" if ev.get("end") else ""
                    dur = _duration_text(ev.get("start"), ev.get("end"))
                    if dur:
                        meta_bits.append(dur)
                    dim = ""
                meta = " · ".join(meta_bits)
                rows.append(
                    f'<div class="tl-row"><span class="tl-times">'
                    f'<span class="s">{_esc(s_txt)}</span>'
                    + (f'<span class="e">{_esc(e_txt)}</span>' if e_txt else "")
                    + f'</span><span class="tl-bar{dim}"></span>'
                    f'<span class="tl-body"><span class="tl-title">{_esc(ev["summary"] or "(no title)")}{warn}</span>'
                    + (f'<span class="tl-meta">{_esc(meta)}</span>' if meta else "")
                    + "</span></div>"
                )
            B.append(f'<div class="tl">{"".join(rows)}</div>')
            if cal.reason:
                B.append(f'<p class="unavail">{_esc(cal.reason)}</p>')
        elif cal.status == S.EMPTY:
            if "no calendars configured" in cal.reason:
                B.append('<p class="unavail">No calendar connected — see calendars.txt.</p>')
            else:
                B.append(f'<p class="unavail">Nothing scheduled.'
                         f'{" " + _esc(cal.reason) if cal.reason else ""}</p>')
        else:
            B.append(_unavail(cal.reason))

    # --- 3x3 ------------------------------------------------------------------
    tx = secs.get("threexthree")
    if tx is not None:
        B.append(_sect("3×3"))
        if tx.usable:
            st = tx.data
            blk, state = st.get("block"), st.get("block_state")
            if state == "active":
                due = blk["due"]
                left = blk["days_left"]
                left_txt = "last day" if left == 0 else f"{left} day{'s' if left != 1 else ''} left"
                B.append(
                    '<div class="feature"><div class="feature-meta">'
                    f'<span class="tag tag-outline">Month {blk["number"]} of {blk["topic_count"]}'
                    f' · week {blk["week"]}</span>'
                    f'<span class="t">{_esc(blk["name"])} · {_esc(left_txt)}</span></div>'
                )
                if due == "output":
                    done = blk["output_done"]
                    B.append(
                        f'<span class="feature-title">{"Output produced" if done else "Produce the output"}</span>'
                        f'<p class="feature-body">'
                        + ("Done — this month is closed."
                           if done else
                           "Ten minutes, one sitting, no editing. Explain it as if to a friend; "
                           "where you stall is what did not land. Mark it with "
                           "<code>dailybrief.py 3x3 output</code>.")
                        + f'</p><span class="feature-foot">Month ends {blk["month_end"]:%a %d %b}</span></div>'
                    )
                else:
                    src = blk["source"]
                    label = P.SLOT_LABELS.get(due, due)
                    if src:
                        title_html = (
                            f'<a class="feature-title" href="{_esc(src["url"])}" '
                            f'rel="noopener noreferrer">{_esc(src["title"])}</a>'
                            if src["url"] else
                            f'<span class="feature-title">{_esc(src["title"])}</span>'
                        )
                    else:
                        title_html = ('<span class="feature-title">Nothing lined up for this '
                                      'week\'s slot</span>')
                    B.append(
                        title_html
                        + f'<p class="feature-body">This week: {_esc(label)}.'
                        + ("" if src else
                           " Add it with <code>dailybrief.py 3x3 topic</code> — "
                           "the week has a job and no source to do it with.")
                        + f'</p><span class="feature-foot">Output due by '
                          f'{blk["month_end"]:%a %d %b}</span></div>'
                    )
            elif state == "not_started":
                B.append(f'<p class="wx-cond">Block starts {blk["starts"]:%a %d %b} — '
                         f'{blk["topic_count"]} topic{"s" if blk["topic_count"] != 1 else ""} lined up.</p>')
            elif state == "finished":
                B.append(f'<p class="wx-cond"><strong>Block finished</strong> {blk["ended"]:%d %b}. '
                         f'Pick three new topics and set a new start date.</p>')

            ses = st.get("session")
            if ses and state == "active":
                if not ses.get("checked"):
                    B.append(f'<p class="unavail">Session not placed — {_esc(ses["why"])}.</p>')
                elif ses.get("found"):
                    when = _session_when(ses["start"], ses["end"], today)
                    taken = ", ".join(f'{t} {s.astimezone():%H:%M}' for s, _e, t in ses["clashes"][:2])
                    note = f' Around {_esc(taken)}.' if taken else ""
                    if ses.get("all_day"):
                        note += f' Note: {_esc(", ".join(ses["all_day"][:2]))} all day.'
                    if ses.get("caveat"):
                        note += f' <em>{_esc(ses["caveat"])}.</em>'
                    B.append(f'<p class="wx-cond"><strong>Session {_esc(when)}</strong> '
                             f'({ses["minutes"]} min, free in your calendar).{note}</p>')
                else:
                    tail = f' <em>{_esc(ses["caveat"])}.</em>' if ses.get("caveat") else ""
                    B.append(f'<p class="wx-cond"><strong>No free {ses["minutes"]}-minute window'
                             f'</strong> before {ses["until"]:%a %d %b}. '
                             f'Something has to give — or shorten the session.{tail}</p>')

            wk = st["weekly"]
            if wk["changes"]:
                rows = []
                for i, change in enumerate(wk["changes"], 1):
                    rows.append(
                        f'<div class="story"><span class="tag tag-accent">{i}</span>'
                        f'<span>{_esc(change)}'
                        + (f'<span class="t">week of {wk["week_of"]:%d %b}</span>'
                           if wk["stale"] and wk["week_of"] else "")
                        + "</span></div>"
                    )
                B.append(f'<div class="stories">{"".join(rows)}</div>')
            if wk["review_due"]:
                if not wk["changes"]:
                    prompt = "No three on file. Pick three changes for the week"
                elif wk["stale"]:
                    prompt = ("Last week's three are still showing — score them and pick "
                              "three new ones")
                else:
                    prompt = f"{wk['review_day'].title()} review — score these and pick three new ones"
                B.append(f'<p class="wx-cond"><strong>{_esc(prompt)}:</strong> '
                         f'<code>dailybrief.py 3x3 week "..." "..." "..."</code></p>')
            if tx.reason:
                B.append(f'<p class="unavail">{_esc(tx.reason)}</p>')
        elif tx.status == S.EMPTY:
            B.append(f'<p class="unavail">{_esc(tx.reason)}</p>')
        else:
            B.append(_unavail(tx.reason))

    # --- Feed sections (Headlines, Audio & DSP, ...) -------------------------
    titles = dict(DEFAULT_SECTION_TITLES, **(cfg.get("section_titles") or {}))
    tag_class = {"news": "tag-outline"}
    for section in S.feed_sections(cfg):
        sec = secs.get(f"feed:{section}")
        if sec is None:
            continue
        B.append(_sect(titles.get(section, section.replace("-", " ").title())))
        if sec.usable:
            rows = []
            for it in sec.data:
                when = f"{it.when.astimezone():%H:%M}" if it.when else ""
                cls = tag_class.get(section, "tag-neutral")
                rows.append(
                    f'<div class="story"><span class="tag {cls}">{_esc(it.source)}</span>'
                    f'<span><a href="{_esc(it.link)}" rel="noopener noreferrer">{_esc(it.title)}</a>'
                    + (f'<span class="t">{_esc(when)}</span>' if when else "")
                    + "</span></div>"
                )
            B.append(f'<div class="stories">{"".join(rows)}</div>')
            if sec.reason:
                B.append(f'<p class="unavail">partial: {_esc(sec.reason)}</p>')
        else:
            B.append(_unavail(sec.reason))

    # --- Tech (HN) -----------------------------------------------------------
    techs = secs.get("tech")
    if techs is not None:
        B.append(_sect(dict(DEFAULT_SECTION_TITLES).get("tech", "Tech")))
        if techs.usable:
            rows = []
            for it in techs.data[: int(cfg.get("tech_items", 2))]:
                points = _esc(it.source.replace("HN ", ""))
                host = netlib.urlsplit_host(it.link)
                rows.append(
                    f'<div class="story"><span class="tag tag-accent">{points}</span>'
                    f'<span><a href="{_esc(it.link)}" rel="noopener noreferrer">{_esc(it.title)}</a>'
                    f'<span class="t">{_esc(host)}</span></span></div>'
                )
            B.append(f'<div class="stories">{"".join(rows)}</div>')
        elif techs.status == S.EMPTY:
            B.append(f'<p class="unavail">Nothing above the score threshold ({_esc(techs.reason)}).</p>')
        else:
            B.append(_unavail(techs.reason))

    # --- Paper of the day ----------------------------------------------------
    pap = secs.get("paper")
    if pap is not None:
        B.append(_sect("Paper of the day"))
        if pap.usable:
            d = pap.data
            when = f"{d['published'].astimezone():%a %d %b}" if d.get("published") else ""
            authors = ", ".join(d["authors"][:3]) + (" et al." if len(d["authors"]) > 3 else "")
            # A cached paper is still worth reading, but it is not today's, and
            # the brief must never let those two look identical.
            stale = pap.detail.get("stale")
            B.append(
                '<div class="feature">'
                f'<div class="feature-meta"><span class="tag tag-outline">arXiv</span>'
                f'<span class="t">{_esc(d["id"])} · {_esc(d["category"])} · '
                f'~{d["read_minutes"]} min abstract'
                + (f' · {_esc(stale)}' if stale else '')
                + '</span></div>'
                f'<a class="feature-title" href="{_esc(d["link"])}" rel="noopener noreferrer">{_esc(d["title"])}</a>'
                f'<p class="feature-body">Abstract — {_esc(d["abstract"])}</p>'
                f'<span class="feature-foot">{_esc(authors)}'
                + (f" · submitted {_esc(when)}" if when else "") + "</span></div>"
            )
        elif pap.status == S.EMPTY:
            B.append(f'<p class="unavail">{_esc(pap.reason)}</p>')
        else:
            B.append(_unavail(pap.reason))

    # --- News of the day -----------------------------------------------------
    feat = secs.get("featured")
    if feat is not None:
        B.append(_sect("News of the day"))
        if feat.usable:
            d = feat.data
            read = f" · ~{d['read_minutes']} min" if d.get("read_minutes") else ""
            B.append(
                '<div class="feature">'
                f'<div class="feature-meta"><span class="tag tag-accent">{_esc(d["source"])}</span>'
                f'<span class="t">{d["when"].astimezone():%a %d %b}{read}</span></div>'
                if d.get("when") else
                '<div class="feature">'
                f'<div class="feature-meta"><span class="tag tag-accent">{_esc(d["source"])}</span></div>'
            )
            B.append(
                f'<a class="feature-title" href="{_esc(d["link"])}" rel="noopener noreferrer">{_esc(d["title"])}</a>'
                + (f'<p class="feature-body">{_esc(d["summary"])}</p>' if d.get("summary") else "")
                + "</div>"
            )
        else:
            B.append(_unavail(feat.reason))

    # --- On this day ----------------------------------------------------------
    otd = secs.get("onthisday")
    if otd is not None:
        B.append(_sect("On this day"))
        if otd.usable:
            B.append(f'<p class="otd"><span class="yr">{_esc(otd.data["year"])}</span> — '
                     f'{_esc(otd.data["text"])}</p>')
        else:
            B.append(_unavail(otd.reason))

    # --- Status footer --------------------------------------------------------
    total = len(secs)
    ok = sum(1 for s in secs.values() if s.status in (S.OK, S.EMPTY))
    took = (stats.get("duration_ms", 0) or 0) / 1000 or stats.get("elapsed_s", 0)
    bits = [f"{ok} of {total} sources OK"]
    broken = [f"{k.replace('feed:', '')} ({s.reason[:40]})"
              for k, s in sorted(secs.items()) if s.status == S.FAILED]
    if broken:
        bits[0] = f"{ok} of {total} sources OK — failed: " + ", ".join(broken)
    if took:
        bits.append(f"built in {took:.1f}s")
    # Only claim credential-freedom when it is true: a calendar URL is one.
    has_cal_url = any(
        s["target"].lower().startswith("http")
        for s in S.read_calendar_sources(BASE / "calendars.txt")
    )
    if stats.get("engine", "local") == "local" and not has_cal_url:
        bits.append("no credentials")
    dot = ' bad"' if broken else '"'
    B.append(f'<div class="foot"><span class="dot{dot}></span><span>{_esc(" · ".join(bits))}</span></div>')

    heading = today.strftime("%A %d %B")
    lede_html = f'<p class="lede">{_esc(tldr)}</p>' if tldr else ""
    return netlib.scrub(PAGE.format(
        title=_esc(f"Daily Brief - {heading}"),
        heading=_esc(heading),
        lede=lede_html,
        body="\n".join(B),
        generated=dt.datetime.now().isoformat(timespec="seconds"),
    ))


def attach_session(secs: dict, today: dt.date, now: dt.datetime | None = None) -> None:
    """Place this week's 3x3 session in genuinely free calendar time.

    Runs after collection rather than inside the collector because it needs the
    calendar and the plan at once, and those are fetched in parallel.

    The rule that matters: an unchecked calendar is never reported as a free
    one. If no calendar is connected, or the fetch failed, or the week was
    never expanded, the section says it could not check -- because "your
    Thursday evening is free" is a claim, and claiming it from missing data is
    exactly the double-booking this is meant to prevent.
    """
    import plan as P
    import sources as S

    tx = secs.get("threexthree")
    if tx is None or not tx.usable or not isinstance(tx.data, dict):
        return
    st = tx.data
    if st.get("block_state") != "active":
        return

    cal = secs.get("calendar")
    detail = (cal.detail if cal is not None else None) or {}
    if cal is None:
        st["session"] = {"checked": False, "why": "the calendar section is switched off"}
        return
    if cal.status == S.FAILED or not detail.get("scanned"):
        st["session"] = {"checked": False,
                         "why": f"the calendar could not be read ({cal.reason[:60]})"
                                if cal.reason else "the calendar could not be read"}
        return
    if not detail.get("horizon_days"):
        st["session"] = {"checked": False, "why": "no calendar is connected — see calendars.txt"}
        return

    now = now or dt.datetime.now().astimezone()
    days = {today: list(cal.data) if cal.usable else []}
    for day, evs in (detail.get("ahead") or {}).items():
        days[day] = list(evs)

    # Only up to the day before the next review, since the week's job rolls
    # over then; and never past what the calendar was actually asked for.
    this_week = st["weekly"]["this_week"]
    until = min(this_week + dt.timedelta(days=6),
                today + dt.timedelta(days=int(detail["horizon_days"])))
    found = P.propose_session(days, st.get("settings") or {}, now, until)
    found["checked"] = True
    found["until"] = until
    # Everything that could make this proposal wrong, said out loud. A slot is
    # still offered -- refusing to suggest anything is not more honest -- but
    # never as a bare claim when the data behind it is doubtful.
    caveats = []
    suspect = detail.get("suspect") or []
    if suspect:
        # A wrong URL that still returns a valid, empty calendar reads as a
        # gloriously free week.
        caveats.append(f"{', '.join(suspect[:2])} returned no events at all — "
                       f"if that is wrong, so is this")
    if detail.get("stale"):
        caveats.append("working from a cached calendar, so a booking made since "
                       "may be missing")
    if found.get("unknown_length"):
        caveats.append(f"{', '.join(found['unknown_length'][:2])} has no usable end time, "
                       f"so the rest of that day is treated as busy")
    if caveats:
        found["caveat"] = "; ".join(caveats)
    st["session"] = found


def collect_sections(cfg: dict, today: dt.date) -> tuple[dict, list[str]]:
    """Fetch everything, plus any whole-run warnings worth a banner."""
    import netlib
    import sources as S

    notices: list[str] = []

    reachable, why = netlib.online()
    if not reachable:
        # A captive portal returns HTTP 200 with a login page for every request,
        # so without this check the brief looks merely quiet rather than broken.
        notices.append(f"OFFLINE — {why}. Nothing fetched today; your 3x3 plan is local.")
        offline = {k: S.Section(k, S.FAILED, reason=why)
                   for k in (cfg.get("sections") or []) if k != "threexthree"}
        # The plan is read off disk, so it is the one section an outage cannot
        # take away -- and the one most worth seeing on a morning with no feeds.
        if "threexthree" in (cfg.get("sections") or []):
            offline["threexthree"] = S.threexthree(today, BASE / "plan.json")
            # Says "could not check" rather than proposing a slot: offline, the
            # calendar is exactly the thing we cannot see.
            attach_session(offline, today)
        return offline, notices

    # Only after we know the network is usable: a hand-edited city or postcode
    # must take effect on the next run rather than silently keep serving the
    # previous coordinates' weather. No-ops when nothing changed.
    _loc, loc_problem = resolve_location(cfg)
    if loc_problem:
        notices.append(loc_problem)

    # base_dir explicitly: collect() otherwise resolves plan.json and the paper
    # cache next to sources.py, which stops being the data directory the moment
    # the code and the user's files are not in the same place.
    secs = S.collect(cfg, today, int(cfg.get("deadline_seconds", 90)), base_dir=BASE)
    attach_session(secs, today)

    skew = netlib.clock_skew_minutes()
    if skew is not None and skew > 10:
        notices.append(
            f"System clock is {skew:.0f} min off server time — date-based sections may be wrong."
        )
    return secs, notices


# ---------------------------------------------------------------- claude


def sections_to_json(secs: dict) -> str:
    def enc(o):
        if isinstance(o, (dt.date, dt.datetime)):
            return o.isoformat()
        if hasattr(o, "__dict__"):
            return dict(o.__dict__)
        return str(o)

    import netlib

    payload = {
        k: {"status": s.status, "reason": s.reason, "data": s.data} for k, s in sorted(secs.items())
    }
    # This whole blob is sent to Claude when engine is claude/auto, so it is the
    # last place a calendar URL may survive.
    return netlib.scrub(json.dumps(payload, default=enc, ensure_ascii=False, indent=2))


def build_prompt(cfg: dict, secs: dict, notices: list[str], today: dt.date) -> str:
    """Claude is a *writer* here, not a researcher.

    All the data is already fetched deterministically, so Claude never web-searches.
    That makes the run fast, cheap, and impossible to hallucinate a headline into.
    """
    style = ""
    prompt_file = BASE / cfg.get("prompt_file", "prompt.md")
    if prompt_file.exists():
        style = prompt_file.read_text("utf-8").strip()

    now = dt.datetime.now().astimezone()
    parts = [
        f"Today is {now.strftime('%A, %d %B %Y')}, local time {now.strftime('%H:%M')}.",
        "",
        "Below is TODAY'S DATA, already fetched from live sources. Write my daily brief",
        "using ONLY this data. Do not search, do not add facts, do not invent headlines.",
        "",
        "```json",
        sections_to_json(secs),
        "```",
        "",
        "Rules:",
        '- A section with status "failed" MUST be reported as unavailable, with its reason.',
        '  Never silently omit it -- a broken section and a quiet one must not look alike.',
        '- A section with status "empty" genuinely has nothing to report; say so in a few words.',
        "- Keep the whole brief to roughly one screen. Headlines only, no summaries of articles.",
        "- Keep every link as a markdown link to the exact url given.",
        "- End with a `---` rule and a one-line source count.",
    ]
    if notices:
        parts += ["", "Lead with these warnings, prominently:"] + [f"- {n}" for n in notices]
    if style:
        parts += ["", "Style preferences from the user:", "", style]
    return "\n".join(parts)


def run_claude(cfg: dict, secs: dict, notices: list[str], today: dt.date) -> tuple[str, dict]:
    claude = resolve_claude(cfg)
    if not claude:
        raise BriefError(
            "Could not find the `claude` executable. Set \"claude_path\" in config.json.",
            "The `claude` executable was not found on PATH.\n\n"
            "Set an explicit path in `config.json`:\n\n"
            "```\n\"claude_path\": \"C:\\\\Users\\\\Danny\\\\.local\\\\bin\\\\claude.exe\"\n```",
        )
    if not os.environ.get("ANTHROPIC_API_KEY"):
        signed_in, _method = check_auth(claude)
        if not signed_in:
            raise auth_error()
    prompt = build_prompt(cfg, secs, notices, today)
    tools = [t for t in cfg.get("tools", []) if t]

    args = [claude, "-p", "--output-format", "json", "--no-session-persistence"]
    if cfg.get("model"):
        args += ["--model", str(cfg["model"])]
    if tools:
        args += ["--tools", ",".join(tools), "--allowedTools", ",".join(tools)]
    add_dirs = [d for d in cfg.get("add_dirs", []) if d]
    if add_dirs:
        args += ["--add-dir", *add_dirs]
    if cfg.get("max_budget_usd"):
        args += ["--max-budget-usd", str(cfg["max_budget_usd"])]
    args += ["--append-system-prompt", OUTPUT_CONTRACT]
    args += [str(a) for a in cfg.get("extra_args", [])]

    workspace = BASE / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    log(f"Invoking: {claude} -p --model {cfg.get('model')} (tools: {','.join(tools) or 'default'})")
    started = dt.datetime.now()
    proc = subprocess.run(
        args,
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(workspace),
        timeout=int(cfg.get("timeout_seconds", 900)),
        creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    elapsed = (dt.datetime.now() - started).total_seconds()

    def looks_like_auth(text: str) -> bool:
        low = text.lower()
        return "authenticate" in low or "oauth" in low or "not logged in" in low

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-1500:]
        if looks_like_auth(detail):
            raise auth_error()
        raise BriefError(f"claude exited {proc.returncode}.\n\n{detail}")

    raw = (proc.stdout or "").strip()
    stats: dict = {"elapsed_s": elapsed}
    text = raw
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            if payload.get("is_error"):
                detail = str(payload.get("result") or raw[:1500])
                if looks_like_auth(detail):
                    raise auth_error()
                raise BriefError(f"claude reported an error: {detail}")
            text = payload.get("result") or ""
            for key in ("total_cost_usd", "duration_ms", "num_turns", "session_id"):
                if key in payload:
                    stats[key] = payload[key]
    except json.JSONDecodeError:
        log("WARN: --output-format json did not return JSON; using raw stdout")

    text = (text or "").strip()
    if not text:
        raise BriefError("claude returned an empty brief.")
    return text, stats


def credential_available(cfg: dict) -> tuple[bool, str]:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True, "ANTHROPIC_API_KEY"
    claude = resolve_claude(cfg)
    if claude:
        ok, method = check_auth(claude)
        if ok:
            return True, f"claude CLI ({method})"
    return False, "none"


def generate(cfg: dict, today: dt.date) -> tuple[str, dict, dict]:
    """Collect the data, then render it with whichever engine is available.

    The data collection is identical either way, so a missing credential costs
    you the prose styling and nothing else -- never the brief itself.
    """
    secs, notices = collect_sections(cfg, today)

    engine = str(cfg.get("engine", "local")).lower()
    if engine == "auto":
        have, how = credential_available(cfg)
        engine = "claude" if have else "local"
        log(f"engine=auto resolved to {engine} (credential: {how})")

    if engine == "claude":
        try:
            markdown, stats = run_claude(cfg, secs, notices, today)
            stats["engine"] = "claude"
            return markdown, stats, secs, notices
        except Exception as exc:  # noqa: BLE001 - never let the AI layer break the brief
            log(f"WARN: Claude synthesis failed ({exc}); rendering locally instead")
            notices.append(f"Claude synthesis failed ({str(exc).splitlines()[0][:120]}) — rendered locally.")

    return compose_markdown(cfg, today, secs, notices), {"engine": "local"}, secs, notices


# ---------------------------------------------------------------- toast / window


def send_toast(title: str, body: str, *, launch: str = LAUNCH_URI, attribution: str = "") -> bool:
    import netlib

    title, body = netlib.scrub(title), netlib.scrub(body)
    if not TOAST_PS1.exists():
        log(f"WARN: {TOAST_PS1.name} missing; cannot toast")
        return False
    cmd = [
        "powershell.exe", "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "Bypass", "-File", str(TOAST_PS1),
        "-Title", title, "-Body", body, "-Aumid", AUMID,
    ]
    if launch:
        cmd += ["-Launch", launch]
    if attribution:
        cmd += ["-Attribution", attribution]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=45,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if proc.returncode != 0 or "TOAST_FAILED" in out:
            log(f"WARN: toast failed: {out[:400]}")
            return False
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"WARN: toast could not run: {exc}")
        return False


def find_browser(cfg: dict) -> str | None:
    configured = cfg.get("browser", "auto")
    if configured and configured not in ("auto", "default") and Path(configured).exists():
        return configured
    if configured == "default":
        return None
    pf, pf86, lad = (
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", ""),
    )
    for cand in (
        Path(pf86) / "Microsoft/Edge/Application/msedge.exe",
        Path(pf) / "Microsoft/Edge/Application/msedge.exe",
        Path(pf) / "Google/Chrome/Application/chrome.exe",
        Path(pf86) / "Google/Chrome/Application/chrome.exe",
        Path(lad) / "Google/Chrome/Application/chrome.exe" if lad else Path("/nonexistent"),
    ):
        if cand.exists():
            return str(cand)
    return None


def open_brief(cfg: dict | None = None, path: Path | None = None) -> None:
    cfg = cfg or load_config()
    target = path or LATEST_HTML
    if not target.exists():
        LATEST_HTML.parent.mkdir(parents=True, exist_ok=True)
        LATEST_HTML.write_text(
            render_page(
                heading="No brief yet",
                lede="Nothing has been generated on this machine so far.",
                body_html="<p>Run <code>python dailybrief.py run</code> to generate one now, "
                "or wait for the scheduled task to fire.</p>",
                meta_bits=[],
                footer="Daily Brief",
            ),
            "utf-8",
        )
        target = LATEST_HTML
    url = "file:///" + urllib.parse.quote(str(target.resolve()).replace("\\", "/"), safe="/:")
    browser = find_browser(cfg)
    if browser:
        try:
            subprocess.Popen(
                [browser, f"--app={url}", f"--window-size={cfg.get('window_size', '1040,1120')}"],
                creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            return
        except OSError as exc:
            log(f"WARN: app-window launch failed ({exc}); falling back to default handler")
    os.startfile(str(target.resolve()))  # noqa: S606 - local file, default handler


# ---------------------------------------------------------------- icon


def _render_icon(size: int) -> list[list[tuple[int, int, int, int]]]:
    """Rounded gradient tile with 'text line' bars, drawn proportionally.

    Geometry scales with the size rather than being hardcoded, because a 64px
    design shrunk to 16px turns to mush -- at small sizes the bars are dropped
    to two and thickened so they stay legible in the taskbar.
    """
    radius = max(2.0, size * 0.22)
    bar_h = max(1, round(size * 0.085))
    if size < 20:
        spec = ((0.34, 0.22, 0.78), (0.58, 0.22, 0.62))
    else:
        spec = ((0.35, 0.23, 0.77), (0.52, 0.23, 0.69), (0.69, 0.23, 0.58))
    bars = [(round(y * size), round(x0 * size), round(x1 * size)) for y, x0, x1 in spec]

    def coverage(x: int, y: int) -> float:
        dx = max(radius - x, x - (size - 1 - radius), 0.0)
        dy = max(radius - y, y - (size - 1 - radius), 0.0)
        return max(0.0, min(1.0, radius - (dx * dx + dy * dy) ** 0.5 + 0.5))

    rows = []
    for y in range(size):
        t = y / max(1, size - 1)
        base = (int(37 + (124 - 37) * t), int(99 + (58 - 99) * t), int(235 + (237 - 235) * t))
        row = []
        for x in range(size):
            r, g, b = base
            for by, bx0, bx1 in bars:
                if by <= y < by + bar_h and bx0 <= x < bx1:
                    r = g = b = 255
                    break
            row.append((r, g, b, int(coverage(x, y) * 255)))
        rows.append(row)
    return rows


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def _png_bytes(rows) -> bytes:
    size = len(rows)
    raw = b"".join(bytes([0]) + bytes(v for px in row for v in px) for row in rows)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(raw, 9))
        + _png_chunk(b"IEND", b"")
    )


def _dib_bytes(rows) -> bytes:
    """A 32bpp BGRA DIB for an ICO entry.

    BMP rather than PNG for the small sizes: PNG-in-ICO is only guaranteed from
    256px, and the shell is the one place a half-supported format shows up as a
    black square rather than an error.
    """
    size = len(rows)
    # Height is doubled: the DIB carries an XOR image plus an AND mask.
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, size * size * 4, 0, 0, 0, 0)
    px = bytearray()
    for y in range(size - 1, -1, -1):          # DIBs are bottom-up
        for r, g, b, a in rows[y]:
            px += bytes((b, g, r, a))
    mask_stride = ((size + 31) // 32) * 4      # 1bpp, rows padded to 4 bytes
    return header + bytes(px) + bytes(mask_stride * size)


def write_icon(path: Path = ICON_PATH) -> Path:
    path.write_bytes(_png_bytes(_render_icon(64)))
    return path


def write_ico(path: Path = ICO_PATH, sizes=(16, 24, 32, 48, 64, 128, 256)) -> Path:
    """A multi-resolution .ico, which is what a Windows shortcut needs.

    128 is a BMP on purpose: the shell reads the 256 PNG entry happily, but
    GDI+ (System.Drawing.Icon) cannot, and would otherwise fall back to 64px
    and look soft on a high-DPI display.
    """
    blobs = []
    for size in sizes:
        rows = _render_icon(size)
        blobs.append(_png_bytes(rows) if size >= 256 else _dib_bytes(rows))
    out = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    for size, blob in zip(sizes, blobs):
        dim = 0 if size >= 256 else size       # 0 means 256 in an ICO directory
        out += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    path.write_bytes(out + b"".join(blobs))
    return path


# ---------------------------------------------------------------- commands


def publish_latest(src: Path) -> None:
    """Atomically replace latest.html.

    The Refresh flow has a browser reloading this exact file on a timer, so a
    plain copyfile can be read mid-write and render as a truncated page.
    os.replace is atomic on the same volume: readers see old or new, never half.
    """
    tmp = LATEST_HTML.with_name("latest.html.tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, LATEST_HTML)


def prune_old(keep_days: int) -> None:
    if keep_days <= 0:
        return
    cutoff = dt.date.today() - dt.timedelta(days=keep_days)
    for f in BRIEFS_DIR.glob("????-??-??.*"):
        try:
            if dt.date.fromisoformat(f.stem) < cutoff:
                f.unlink()
        except (ValueError, OSError):
            continue


def cmd_run(args) -> int:
    cfg = load_config()
    BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
    today = dt.date.today().isoformat()
    html_path = BRIEFS_DIR / f"{today}.html"

    if html_path.exists() and not args.force:
        log(f"Brief for {today} already exists; skipping (use --force to regenerate)")
        if args.open:
            open_brief(cfg, html_path)
        return 0

    log(f"--- generating brief for {today} (engine={cfg.get('engine')}) ---")
    try:
        markdown, stats, secs, notices = generate(cfg, dt.date.today())
        log(
            "sections: "
            + ", ".join(f"{k}={s.status}" for k, s in sorted(secs.items()))
        )
    except Exception as exc:  # noqa: BLE001 - surface every failure to the user
        log(f"ERROR: {exc}")
        explain = getattr(exc, "markdown", None)
        detail = markdown_to_html(explain) if explain else markdown_to_html(f"```\n{exc}\n```")
        html_path.write_text(
            render_page(
                heading=dt.date.today().strftime("%A %d %B"),
                lede="The brief could not be generated.",
                body_html=f'<div class="error"><h3>What went wrong</h3>{detail}</div>'
                f"<p>Full log: <code>logs\\dailybrief.log</code></p>",
                meta_bits=["Failed", dt.datetime.now().strftime("%H:%M")],
                footer="Daily Brief - failed run",
            ),
            "utf-8",
        )
        publish_latest(html_path)
        save_state(last_run=dt.datetime.now().isoformat(timespec="seconds"), last_status="error",
                   last_error=str(exc)[:500])
        if cfg.get("toast", True):
            send_toast("Daily Brief failed", str(exc).split("\n")[0][:180],
                       attribution="Click to see details")
        return 1

    (BRIEFS_DIR / f"{today}.md").write_text(markdown, "utf-8")
    tldr, body_md = split_tldr(markdown)

    engine = stats.get("engine", "local")
    if engine == "local":
        # The structured renderer: the design's layout built from section data.
        page = compose_page(cfg, dt.date.today(), secs, notices, stats, tldr)
    else:
        # Claude wrote prose; keep it, in the same chrome.
        meta_bits = [dt.datetime.now().strftime("%H:%M"), f"{engine}/{cfg.get('model')}"]
        if stats.get("total_cost_usd") is not None:
            meta_bits.append(f"${float(stats['total_cost_usd']):.3f}")
        page = render_page(
            heading=dt.date.today().strftime("%A %d %B"),
            lede=tldr,
            body_html=markdown_to_html(body_md),
            meta_bits=meta_bits,
            footer=str(today),
        )
    html_path.write_text(page, "utf-8")
    publish_latest(html_path)
    prune_old(int(cfg.get("keep_days", 60)))
    save_state(last_run=dt.datetime.now().isoformat(timespec="seconds"), last_status="ok",
               last_brief=str(html_path), last_cost=stats.get("total_cost_usd"),
               last_engine=stats.get("engine", "local"),
               last_sections={k: s.status for k, s in sorted(secs.items())},
               last_error="")   # clear it, or a stale error outlives the failure
    log(f"Brief written to {html_path} ({len(markdown)} chars)")

    if cfg.get("toast", True):
        send_toast("Your daily brief is ready", tldr[:180], attribution="Click to read it")
    if args.open or cfg.get("auto_open", False):
        open_brief(cfg, html_path)
    return 0


def cmd_render_last(args) -> int:
    mds = sorted(BRIEFS_DIR.glob("????-??-??.md"))
    if not mds:
        print("No saved markdown to re-render.")
        return 1
    src = mds[-1]
    tldr, body_md = split_tldr(src.read_text("utf-8"))
    out = src.with_suffix(".html")
    out.write_text(
        render_page(
            heading=dt.date.fromisoformat(src.stem).strftime("%A %d %B"),
            lede=tldr,
            body_html=markdown_to_html(body_md),
            meta_bits=["Re-rendered", dt.datetime.now().strftime("%H:%M")],
            footer=src.stem,
        ),
        "utf-8",
    )
    publish_latest(out)
    print(f"Re-rendered {src.name} -> {out}")
    if args.open:
        open_brief(load_config(), out)
    return 0


def has_coords_stale(loc: dict) -> bool:
    """True when the settings have been edited since the coordinates were cached."""
    if loc.get("latitude") is None:
        return False
    if not (loc.get("city") or loc.get("postcode")):
        return False
    return loc.get("resolved_from") != _location_fingerprint(loc)


def _S_units(cfg: dict) -> dict:
    import sources as S

    return S.units_from(cfg.get("units"))


def cmd_setup(args) -> int:
    """Resolve a place to coordinates and store the location settings."""
    import sources as S

    cfg = load_config()
    loc = dict(cfg.get("location") or {})

    if args.postcode:
        sec = S.postcode_lookup(args.postcode)
        if not sec.usable:
            print(f"Could not resolve postcode {args.postcode!r}: {sec.reason}")
            return 1
        chosen = sec.data
        loc.update(postcode=chosen["postcode"], city="", country="GB")
    else:
        if not args.city:
            print("Give a city, or --postcode for a UK postcode.")
            return 1
        sec = S.geocode(args.city, args.country or "")
        if not sec.usable:
            print(f"Could not resolve {args.city!r}: {sec.reason}")
            print("Try a fuller name (e.g. 'Newcastle upon Tyne') or add --country GB.")
            return 1

        candidates = sec.detail["candidates"]
        if args.pick is not None:
            if not 0 <= args.pick < len(candidates):
                print(f"--pick must be 0..{len(candidates) - 1}")
                return 1
            chosen = candidates[args.pick]
        else:
            chosen = candidates[0]

        if len(candidates) > 1:
            print(f"{len(candidates)} places matched {args.city!r}:")
            for n, c in enumerate(candidates):
                mark = "->" if c is chosen else "  "
                pop = f"{c['population']:,}" if c["population"] else "unknown"
                print(f" {mark} [{n}] {c['label']}  (pop {pop})")
            print("\nRe-run with --pick N to choose a different one, or --country GB to filter.\n")
        loc.update(city=args.city, postcode="", country=args.country or "")

    loc.update(
        latitude=chosen["latitude"], longitude=chosen["longitude"], label=chosen["label"]
    )
    loc["resolved_from"] = _location_fingerprint(loc)
    cfg["location"] = loc

    # Scotland and Northern Ireland genuinely differ; a hardcoded division would
    # be wrong every year with no error at all.
    division = _uk_division(chosen)
    if division:
        cfg["bank_holiday_division"] = division
    elif "bankholiday" in (cfg.get("sections") or []):
        cfg["sections"] = [s for s in cfg["sections"] if s != "bankholiday"]
        print("Non-UK location: the UK bank-holiday section has been switched off.")

    for key, value in (("temperature", args.temperature), ("wind", args.wind), ("clock", args.clock)):
        if value:
            cfg.setdefault("units", dict(DEFAULT_CONFIG["units"]))[key] = value

    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), "utf-8")
    units = cfg.get("units") or DEFAULT_CONFIG["units"]
    print(f"Location  : {chosen['label']} ({chosen['latitude']}, {chosen['longitude']})")
    print(f"Holidays  : {cfg.get('bank_holiday_division', 'off')}")
    print(f"Units     : {units['temperature']}, {units['wind']}, {units['clock']}")
    print(f"\nSaved to {CONFIG_PATH}. Generate a brief now:\n  python dailybrief.py run --force --open")
    return 0


def _save_cfg(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), "utf-8")


def _find_feed(feeds: list[dict], name: str) -> int:
    for i, f in enumerate(feeds):
        if f["name"].lower() == name.lower():
            return i
    return -1


def cmd_sources(args) -> int:
    """List, add, edit, enable/disable, remove and test the brief's feeds."""
    import sources as S

    cfg = load_config()
    feeds = S.configured_feeds(cfg)
    action = args.action

    # `sources test https://...` and `sources add "Name" https://...` both read
    # naturally; accept a bare URL in the name slot rather than demanding a
    # placeholder argument to hold its place.
    if args.name.startswith(("http://", "https://")) and not args.url:
        args.url, args.name = args.name, ""

    if action == "list":
        if not feeds:
            print("No feeds configured.")
            return 0
        width = max(len(f["name"]) for f in feeds)
        print(f"{'':2} {'NAME'.ljust(width)}  {'SECTION':<10} {'MAX':>3} {'MAXAGE':>7}  URL")
        for f in feeds:
            mark = "  " if f["enabled"] else "off"
            print(f"{mark:2} {f['name'].ljust(width)}  {f['section']:<10} "
                  f"{f['max_items']:>3} {str(f['max_age_hours']) + 'h':>7}  {f['url']}")
        builtins = [s for s in ("weather", "tech", "onthisday", "bankholiday")
                    if s in (cfg.get("sections") or [])]
        print(f"\nBuilt-in sections on: {', '.join(builtins) or 'none'}")
        print(f"Enabled feed sections: {', '.join(S.feed_sections(cfg)) or 'none'}")
        return 0

    if action == "test":
        targets = [f for f in feeds if not args.name or f["name"].lower() == args.name.lower()]
        if args.url:
            targets = [dict(S.FEED_DEFAULTS, name="(url)", url=args.url)]
        if not targets:
            print(f"No feed named {args.name!r}. Try `sources list`.")
            return 1
        bad = 0
        for f in targets:
            sec = S.probe_feed(f["url"], f["name"])
            if sec.usable:
                d = sec.detail
                age = d["newest_age_hours"]
                agetxt = "unknown age" if age is None else f"newest {age:.1f}h old"
                warn = "  <-- STALE" if age is not None and age > f.get("max_age_hours", 24) else ""
                print(f"  OK   {f['name']}: {d['count']} items, {agetxt}, {sec.elapsed_ms}ms{warn}")
                if d["redirected"]:
                    print(f"       redirected to {d['final_url']}")
                for it in sec.data[:3]:
                    print(f"         - {it.title[:88]}")
            else:
                bad += 1
                print(f"  FAIL {f['name']}: {sec.reason}")
        return 1 if bad else 0

    # --- mutating actions ---------------------------------------------------
    if action == "add":
        if _find_feed(feeds, args.name) >= 0 and not args.force:
            print(f"A feed named {args.name!r} already exists. Use `sources edit`, or --force.")
            return 1
        entry = S.normalise_feed({
            "name": args.name, "url": args.url, "section": args.section,
            "max_items": args.max_items, "max_age_hours": args.max_age, "enabled": True,
        })
        if entry is None:
            print("Need both a name and a url.")
            return 1

        # Validate before saving. Reuters and AP still resolve -- they just do
        # not serve feeds any more -- so "it returned 200" proves nothing.
        print(f"Checking {entry['url']} ...")
        probe = S.probe_feed(entry["url"], entry["name"])
        if not probe.usable:
            print(f"  FAIL {probe.reason}")
            if not args.force:
                print("\nNot saved. Re-run with --force to add it anyway.")
                return 1
            print("  --force given: adding despite the failure.")
        else:
            age = probe.detail["newest_age_hours"]
            age_txt = f", newest {age:.1f}h old" if age is not None else ""
            print(f"  OK   {probe.detail['count']} items{age_txt}")
            for it in probe.data[:3]:
                print(f"         - {it.title[:88]}")
            if probe.detail["redirected"]:
                print(f"  NOTE redirected to {probe.detail['final_url']} — saving the original")

        idx = _find_feed(feeds, args.name)
        if idx >= 0:
            feeds[idx] = entry
        else:
            feeds.append(entry)

    elif action == "edit":
        idx = _find_feed(feeds, args.name)
        if idx < 0:
            print(f"No feed named {args.name!r}. Try `sources list`.")
            return 1
        f = dict(feeds[idx])
        for key, value in (("url", args.url), ("section", args.section),
                           ("max_items", args.max_items), ("max_age_hours", args.max_age)):
            if value is not None:
                f[key] = value
        feeds[idx] = S.normalise_feed(f)
        print(f"Updated {args.name}.")

    elif action in ("enable", "disable"):
        idx = _find_feed(feeds, args.name)
        if idx < 0:
            print(f"No feed named {args.name!r}. Try `sources list`.")
            return 1
        feeds[idx]["enabled"] = action == "enable"
        print(f"{args.name} {action}d.")

    elif action == "remove":
        idx = _find_feed(feeds, args.name)
        if idx < 0:
            print(f"No feed named {args.name!r}. Try `sources list`.")
            return 1
        removed = feeds.pop(idx)
        print(f"Removed {removed['name']} ({removed['url']}).")

    else:
        print(f"Unknown action {action!r}.")
        return 1

    cfg["feeds"] = feeds
    cfg.pop("news_feeds", None)          # migrated into `feeds`
    cfg.pop("news_per_source", None)     # superseded by per-feed max_items

    # A section only renders when it is listed in `sections`, so keep that in
    # step automatically rather than making it a second thing to remember.
    sections = list(cfg.get("sections") or [])
    live = set(S.feed_sections(cfg))
    for s in live:
        if s not in sections:
            sections.append(s)
            print(f"Enabled the '{s}' section.")
    known_feed_sections = {f["section"] for f in feeds}
    for s in list(sections):
        if s in known_feed_sections and s not in live:
            sections.remove(s)
            print(f"Disabled the '{s}' section (no enabled feeds left in it).")
    cfg["sections"] = sections

    _save_cfg(cfg)
    print(f"\nSaved. {len(feeds)} feed(s) configured. Preview with:\n"
          f"  python dailybrief.py run --force --open")
    return 0


PLAN_PATH = BASE / "plan.json"


def _plan_lines(st: dict, today: dt.date) -> list[str]:
    """The plan as text, shared by `3x3 status` and every mutating command's echo."""
    import plan as P

    lines: list[str] = []
    blk, state = st.get("block"), st.get("block_state")
    if state == "active":
        lines.append(f"Month {blk['number']} of {blk['topic_count']}: {blk['name']}")
        lines.append(f"  {blk['month_start']:%d %b} - {blk['month_end']:%d %b}"
                     f"  (week {blk['week']} of 4, {blk['days_left']} days left)")
        for slot in P.SLOTS:
            src = blk["sources"][slot]
            due = "->" if slot == blk["due"] else "  "
            label = P.SLOT_LABELS[slot]
            lines.append(f"  {due} {slot:<6} {src['title'] if src else '(not chosen)'}"
                         + (f"   [{label}]" if not src else ""))
        mark = "produced" if blk["output_done"] else f"due by {blk['month_end']:%a %d %b}"
        lines.append(f"  {'->' if blk['due'] == 'output' else '  '} output {mark}")
        ses = st.get("session")
        if ses and not ses.get("checked"):
            lines.append(f"  session not placed: {ses['why']}")
        elif ses and ses.get("found"):
            lines.append(f"  session {_session_when(ses['start'], ses['end'], today)}"
                         f"  ({ses['minutes']} min, free)")
        elif ses:
            lines.append(f"  no free {ses['minutes']}-min window before "
                         f"{ses['until']:%a %d %b}")
    elif state == "not_started":
        lines.append(f"Block starts {blk['starts']:%a %d %b} ({blk['topic_count']} topics).")
    elif state == "finished":
        lines.append(f"Block finished {blk['ended']:%a %d %b}. Set a new start and three topics.")
    elif st.get("start"):
        lines.append(f"Start date {st['start']:%d %b %Y} is set, but no topics are.")
    else:
        # Be precise about which half is missing; the problems list below says why
        # a start date that *was* given did not parse.
        lines.append("No month-block: " + ("the start date did not parse."
                                           if st.get("problems") else
                                           "no start date and no topics."))

    wk = st["weekly"]
    lines.append("")
    if wk["changes"]:
        # week_of is None when the plan was hand-edited without one; status()
        # tolerates that, so the echo must too rather than crashing on %d %b.
        if wk["week_of"]:
            age = f"  (filed {wk['week_of']:%d %b}" + (", STALE)" if wk["stale"] else ")")
        else:
            age = "  (no filing date)"
        lines.append(f"This week's three{age}")
        for i, c in enumerate(wk["changes"], 1):
            lines.append(f"  {i}. {c}")
    else:
        lines.append("No changes on file for this week.")
    if wk["review_due"]:
        lines.append(f"  Review due ({wk['review_day']}): "
                     f'3x3 week "..." "..." "..."')
    for problem in st.get("problems") or []:
        lines.append(f"  ! {problem}")
    return lines


def cmd_threexthree(args) -> int:
    """Read and edit the 3x3 plan: the weekly three and the monthly topics."""
    import plan as P

    today = dt.date.today()
    action = args.action
    items = list(args.items or [])

    try:
        plan = P.load(PLAN_PATH)
    except P.PlanError as exc:
        print(f"{exc}\nFix plan.json, or move it aside and run `3x3 init`.")
        return 1

    if action == "init":
        if PLAN_PATH.exists():
            print(f"{PLAN_PATH.name} already exists — nothing overwritten.")
        else:
            start = P.add_months(today.replace(day=1), 1)
            P.save(PLAN_PATH, dict(P.DEFAULT_PLAN, start=start.isoformat(), topics=[],
                                   weekly={"week_of": "", "changes": [], "history": []}))
            print(f"Wrote {PLAN_PATH.name}, first block starting {start:%d %b %Y}.")
        cfg = load_config()
        sections = list(cfg.get("sections") or [])
        if "threexthree" not in sections:
            # After calendar, which is where it renders.
            at = sections.index("calendar") + 1 if "calendar" in sections else len(sections)
            sections.insert(at, "threexthree")
            cfg["sections"] = sections
            CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), "utf-8")
            print("Enabled the 3x3 section in config.json.")
        print('\nNext: 3x3 topic 1 "Your topic" --video "Title|url" --text "..." --misc "..."')
        return 0

    if action == "status":
        if not PLAN_PATH.exists():
            print("No plan.json yet. Run `dailybrief.py 3x3 init`.")
            return 1
        st = P.status(plan, today)
        if st.get("block_state") == "active":
            # Costs one calendar fetch, so `3x3 status` answers the same
            # question the morning brief does rather than a staler version.
            import sources as S

            secs = {"threexthree": S.Section("threexthree", S.OK, data=st)}
            try:
                secs["calendar"] = S.calendar(load_config(), today, BASE, horizon_days=7)
            except Exception as exc:  # noqa: BLE001 - status must still print
                secs["calendar"] = S.Section("calendar", S.FAILED, reason=str(exc))
            attach_session(secs, today)
        for line in _plan_lines(st, today):
            print(line)
        return 0

    # --- mutating actions ---------------------------------------------------
    if action == "start":
        if not items:
            print("Give a date: 3x3 start 2026-09-01")
            return 1
        try:
            dt.date.fromisoformat(items[0])
        except ValueError:
            print(f"Not a YYYY-MM-DD date: {items[0]!r}")
            return 1
        plan["start"] = items[0]

    elif action == "topic":
        if len(items) < 1 or not items[0].isdigit():
            print('Give a topic number and name: 3x3 topic 1 "Psychoacoustics" --video "..."')
            return 1
        number, name = int(items[0]), (items[1] if len(items) > 1 else "")
        sources = {"video": args.video, "text": args.text, "misc": args.misc}
        try:
            P.set_topic(plan, number, name, {k: v for k, v in sources.items() if v})
        except P.PlanError as exc:
            print(str(exc))
            return 1

    elif action == "week":
        if len(items) != 3 or not all(i.strip() for i in items):
            got = f"got {len(items)}" if len(items) != 3 else "one of them was blank"
            print(f'Give exactly three non-empty changes; {got}.\n'
                  f'  3x3 week "app limit 20 min" "Tue/Thu 8-9pm blocked" "call a friend"')
            return 1
        P.set_week(plan, today, items)

    elif action == "score":
        current = (plan.get("weekly") or {}).get("changes") or []
        if not current:
            print("No changes on file to score.")
            return 1
        if len(items) != len(current):
            print(f"{len(current)} changes on file, {len(items)} verdicts given.\n"
                  f"  3x3 score {' '.join(P.VERDICTS[:1] * len(current))}")
            return 1
        try:
            P.score_week(plan, items)
        except P.PlanError as exc:
            print(str(exc))
            return 1

    elif action == "output":
        try:
            plan, name = P.mark_output(plan, today, " ".join(items))
        except P.PlanError as exc:
            print(str(exc))
            return 1
        print(f"Output marked produced for {name}.")

    P.save(PLAN_PATH, plan)
    print("")
    for line in _plan_lines(P.status(plan, today), today):
        print(line)
    return 0


def cmd_protocol(args) -> int:
    """Entry point for dailybrief:<verb> URLs (toast clicks, the Refresh button).

    refresh regenerates latest.html IN PLACE and opens no window: the page that
    fired it reloads itself to pick up the new content.
    """
    verb = (args.url or "").split(":", 1)[-1].strip().lower().rstrip("/")
    if verb in ("", "open"):
        open_brief(load_config())
        return 0
    if verb == "refresh":
        ns = argparse.Namespace(force=True, open=False)
        return cmd_run(ns)
    log(f"WARN: unknown protocol verb {verb!r}; opening instead")
    open_brief(load_config())
    return 0


def cmd_calendar(args) -> int:
    """Check the calendar setup and show what today (or a given date) holds."""
    import netlib
    import sources as S

    cfg = load_config()
    path = BASE / "calendars.txt"
    srcs = S.read_calendar_sources(path)
    if not srcs:
        print(f"No calendars configured.\n\nOpen {path} — it has the click-by-click\n"
              "instructions for getting the URL out of Google Calendar.")
        return 1

    when = dt.date.today()
    if args.date:
        try:
            when = dt.date.fromisoformat(args.date)
        except ValueError:
            print("--date must be YYYY-MM-DD")
            return 1

    # Identify calendars by label and shape, never by URL.
    print(f"{len(srcs)} calendar source(s) configured:")
    for s in srcs:
        t = s["target"]
        kind = "google ics" if "calendar.google.com" in t else ("url" if t.startswith("http") else "local file")
        print(f"  - {s['label'] or '(unlabelled)'}  [{kind}]")
        if t.startswith("http") and not re.match(
            r"^https://calendar\.google\.com/calendar/ical/.+/(private-[0-9a-f]{16,}|public)/(basic|full)\.ics$", t
        ):
            print("      NOTE: does not match the usual Google secret-ICS shape — "
                  "check you copied the whole line.")

    emails = cfg.get("calendar_emails") or []
    print(f"\nDeclined-invitation filtering: {', '.join(emails) if emails else 'OFF '
          '(set \"calendar_emails\" in config.json to enable)'}")

    sec = S.calendar(cfg, when, BASE)
    print(f"\n{when:%A %d %B} — {sec.status.upper()} in {sec.elapsed_ms}ms")
    if sec.reason:
        print(f"  {netlib.scrub(sec.reason)}")
    for ev in (sec.data or []):
        stamp = "all day" if ev["all_day"] else f"{ev['start']:%H:%M}"
        warn = f"   [{'; '.join(ev['warnings'])}]" if ev["warnings"] else ""
        print(f"  {stamp:>7}  {ev['summary'] or '(no title)'}{warn}")
    if sec.status == S.EMPTY and not sec.data:
        print("  (nothing scheduled)")
    return 0 if sec.status != S.FAILED else 1


def cmd_check(_args) -> int:
    """Hit every configured source and report what worked."""
    import sources as S

    cfg = load_config()
    print("Checking connectivity and every configured source...\n")
    secs, notices = collect_sections(cfg, dt.date.today())
    for note in notices:
        print(f"  !! {note}")
    width = max((len(k) for k in secs), default=10)
    for k, s in sorted(secs.items()):
        flag = {"ok": "OK   ", "empty": "EMPTY", "failed": "FAIL "}.get(s.status, s.status)
        n = len(s.data) if isinstance(s.data, list) else ""
        print(f"  {flag} {k:<{width}}  {s.elapsed_ms:>5}ms  {str(n):>3}  {s.reason[:60]}")
    bad = [k for k, s in secs.items() if s.status == S.FAILED]
    print(f"\n{len(secs) - len(bad)}/{len(secs)} sources usable.")
    return 1 if bad else 0


def cmd_status(_args) -> int:
    cfg = load_config()
    state = {}
    if STATE_PATH.exists():
        try:
            state = read_json(STATE_PATH)
        except (OSError, ValueError):
            pass
    engine = str(cfg.get("engine", "local")).lower()
    have, how = credential_available(cfg) if engine != "local" else (False, "not needed")
    effective = engine if engine != "auto" else ("claude" if have else "local")
    loc = cfg.get("location") or {}

    print(f"Program dir : {BASE}")
    print(f"engine      : {engine}" + (f" -> {effective}" if engine == "auto" else ""))
    print(f"credential  : {how}" + ("" if engine != "local" else " (local engine needs none)"))
    src = loc.get("postcode") or loc.get("city") or "fixed coordinates"
    coords = (
        f"{loc['latitude']}, {loc['longitude']}"
        if loc.get("latitude") is not None else "unresolved"
    )
    stale = has_coords_stale(loc)
    print(f"location    : {loc.get('label') or 'NOT SET -- run `dailybrief.py setup <city>`'}")
    print(f"  from      : {src} -> {coords}" + ("   (CHANGED — re-resolves next run)" if stale else ""))
    u = _S_units(cfg)
    print(f"units       : {u['temperature']}, {u['wind']}, {u['precipitation']}, {u['clock']}")
    print(f"holidays    : {cfg.get('bank_holiday_division', 'off')}")
    print(f"sections    : {', '.join(cfg.get('sections') or [])}")
    if effective == "claude":
        print(f"model       : {cfg.get('model')}")
        print(f"prompt      : {BASE / cfg.get('prompt_file', 'prompt.md')}")
    print(f"browser     : {find_browser(cfg) or 'system default'}")
    print(f"last run    : {state.get('last_run', 'never')} ({state.get('last_status', '-')})")
    if state.get("last_error"):
        print(f"last error  : {state['last_error']}")
    briefs = sorted(BRIEFS_DIR.glob("????-??-??.html"))
    print(f"briefs kept : {len(briefs)}" + (f" (newest {briefs[-1].stem})" if briefs else ""))
    return 0


def main() -> int:
    # A brief contains °C, en-dashes and non-Latin names. The console here is
    # cp1252, which raises UnicodeEncodeError on those. Under pythonw stdout is
    # None entirely, so every print() would throw AttributeError.
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    ap = argparse.ArgumentParser(prog="dailybrief", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    p_run = sub.add_parser("run", help="generate today's brief")
    p_run.add_argument("--force", action="store_true", help="regenerate even if today's brief exists")
    p_run.add_argument("--open", action="store_true", help="open the window when done")
    p_run.set_defaults(func=cmd_run)

    p_open = sub.add_parser("open", help="open the most recent brief")
    p_open.set_defaults(func=lambda a: (open_brief(), 0)[1])

    p_toast = sub.add_parser("toast-test", help="fire a sample toast")
    p_toast.set_defaults(
        func=lambda a: 0 if send_toast("Daily Brief", "Toast plumbing works. Click to open the viewer.",
                                       attribution="Test notification") else 1
    )

    p_rl = sub.add_parser("render-last", help="re-render the last markdown without calling Claude")
    p_rl.add_argument("--open", action="store_true")
    p_rl.set_defaults(func=cmd_render_last)

    p_setup = sub.add_parser("setup", help="set location and unit settings")
    p_setup.add_argument("city", nargs="?", default="", help="e.g. \"Newcastle upon Tyne\"")
    p_setup.add_argument("--postcode", default="", help="UK postcode instead of a city, e.g. \"EH1 1YZ\"")
    p_setup.add_argument("--country", default="", help="ISO code to disambiguate a city, e.g. GB")
    p_setup.add_argument("--pick", type=int, default=None, help="choose the Nth candidate")
    p_setup.add_argument("--temperature", choices=["celsius", "fahrenheit"], help="temperature unit")
    p_setup.add_argument("--wind", choices=["mph", "kmh", "ms", "kn"], help="wind speed unit")
    p_setup.add_argument("--clock", choices=["24h", "12h"], help="sunrise/sunset clock format")
    p_setup.set_defaults(func=cmd_setup)

    p_src = sub.add_parser("sources", help="list, add, edit, enable/disable, remove or test feeds")
    p_src.add_argument("action",
                       choices=["list", "add", "edit", "remove", "enable", "disable", "test"])
    p_src.add_argument("name", nargs="?", default="", help="feed name, e.g. \"Ars Technica\"")
    p_src.add_argument("url", nargs="?", default=None, help="feed URL (for add/edit/test)")
    p_src.add_argument("--section", default=None,
                       help="which heading it appears under, e.g. news, tech, audio")
    p_src.add_argument("--max-items", type=int, default=None, dest="max_items",
                       help="how many items to take from this feed (default 2)")
    p_src.add_argument("--max-age", type=int, default=None, dest="max_age",
                       help="ignore items older than N hours (default 24)")
    p_src.add_argument("--force", action="store_true",
                       help="save even if the feed fails validation")
    p_src.set_defaults(func=cmd_sources)

    p_3x3 = sub.add_parser("3x3", help="the 3x3 plan: this week's three changes and this month's topic")
    p_3x3.add_argument("action", nargs="?", default="status",
                       choices=["status", "init", "start", "topic", "week", "score", "output"])
    p_3x3.add_argument("items", nargs="*", default=[],
                       help='e.g. week "change one" "change two" "change three"')
    p_3x3.add_argument("--video", default="", help='lecture video, as "Title|url"')
    p_3x3.add_argument("--text", default="", help='article or book, as "Title|url"')
    p_3x3.add_argument("--misc", default="", help='course, series or other, as "Title|url"')
    p_3x3.set_defaults(func=cmd_threexthree)

    p_proto = sub.add_parser("protocol", help="handle a dailybrief: URL (used by toasts and the Refresh button)")
    p_proto.add_argument("url", nargs="?", default="dailybrief:open")
    p_proto.set_defaults(func=cmd_protocol)

    p_cal = sub.add_parser("calendar", help="check the calendar setup and show today's events")
    p_cal.add_argument("--date", default="", help="a specific day, YYYY-MM-DD")
    p_cal.set_defaults(func=cmd_calendar)

    sub.add_parser("check", help="hit every source and report what worked").set_defaults(func=cmd_check)

    sub.add_parser("icon", help="regenerate icon.png and icon.ico").set_defaults(
        func=lambda a: (write_icon(), write_ico(),
                        print(f"Wrote {ICON_PATH}\nWrote {ICO_PATH}"), 0)[3]
    )
    sub.add_parser("status", help="show configuration and last run").set_defaults(func=cmd_status)

    args = ap.parse_args()
    if not getattr(args, "func", None):
        ap.print_help()
        return 0
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
