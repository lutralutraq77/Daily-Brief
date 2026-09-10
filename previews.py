"""Small, shared reading previews made from the current run's collected data."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

from netlib import clean_text, scrub


def safe_link(value: str) -> str:
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme in ("http", "https") and parsed.netloc else ""
    except ValueError:
        return ""


def reading_previews(sections: dict, date: str) -> dict:
    result = {"date": date}
    for name, key in (("paper", "paper"), ("climate", "feed:climate")):
        sec = sections.get(key)
        card = {"title": "", "summary": "", "url": "", "source": "",
                "published": "", "note": "", "status": "disabled"}
        if sec is None:
            card["note"] = "This source is not enabled."
        elif not sec.usable or not sec.data:
            card.update(status=sec.status, note=clean_text(scrub(sec.reason or "No article available."), 240))
        else:
            if name == "paper":
                d = sec.data
                title, summary, url = d.get("title", ""), d.get("abstract", ""), d.get("link", "")
                source, published = "arXiv", d.get("published")
            else:
                # Keep the briefing's editorial/feed order; do not invent a ranking.
                item = sec.data[0]
                title, summary, url = item.title, item.summary, item.link
                source, published = item.source, item.when
            card.update(status="ok", title=clean_text(title, 220), summary=clean_text(summary, 460),
                        url=safe_link(url), source=clean_text(source, 100),
                        published=published.isoformat() if published else "",
                        note=clean_text(scrub(sec.detail.get("stale") or sec.reason or ""), 240))
        result[name] = card
    return result


def load_previews(home: str) -> dict:
    """Ignore missing/corrupt sidecars and metadata for a different HTML edition."""
    root = Path(home) / "briefs"
    try:
        value = json.loads((root / "latest-preview.json").read_text("utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("date"), str):
            return {}
        if value.get("html_mtime_ns") != (root / "latest.html").stat().st_mtime_ns:
            return {}
        if not all(isinstance(value.get(k), dict) for k in ("paper", "climate")):
            return {}
        return value
    except (OSError, ValueError):
        return {}


def notification_body(previews: dict, fallback: str = "Tap to read your brief") -> str:
    lines = []
    for key, label in (("paper", "Paper"), ("climate", "Climate")):
        card = previews.get(key, {})
        title = card.get("title")
        lines.append(f"{label}: {title}" if title else f"{label}: no preview available")
    return "\n".join(lines) if previews else fallback
