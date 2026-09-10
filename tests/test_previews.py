"""Regression coverage for shared previews, persisted editions and notifications."""
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import ExitStack
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import android_entry
import dailybrief as db
import sources as S
from netlib import Item
from previews import load_previews, notification_body, reading_previews, safe_link


class PreviewTests(unittest.TestCase):
    def sections(self):
        return {
            "paper": S.Section("paper", S.OK, data={"title": "A paper", "abstract": "An abstract",
                "link": "https://arxiv.org/abs/1234", "published": dt.datetime(2026, 9, 8),
                "authors": [], "id": "1234", "category": "cs.SD", "read_minutes": 1},
                detail={"stale": "Cached from yesterday"}),
            "feed:climate": S.Section("feed:climate", S.OK, data=[
                Item("First story", "https://example.com/first", None, "Climate desk", "1", "A standfirst"),
                Item("Second story", "https://example.com/second", None, "Other desk", "2")]),
        }

    def test_current_selection_and_metadata(self):
        value = reading_previews(self.sections(), "2026-09-09")
        self.assertEqual(value["paper"]["summary"], "An abstract")
        self.assertEqual(value["paper"]["note"], "Cached from yesterday")
        self.assertEqual(value["climate"]["title"], "First story")
        self.assertEqual(value["climate"]["source"], "Climate desk")
        self.assertIn("Paper: A paper\nClimate: First story", notification_body(value))
        json.dumps(value)  # Dates must survive the Python/Kotlin JSON bridge.

    def test_unavailable_disabled_and_empty(self):
        value = reading_previews({"paper": S.Section("paper", S.FAILED, reason="Offline"),
                                 "feed:climate": S.Section("feed:climate", S.EMPTY)}, "2026-09-09")
        self.assertEqual(value["paper"]["note"], "Offline")
        self.assertEqual(value["climate"]["title"], "")
        self.assertEqual(reading_previews({}, "2026-09-09")["paper"]["status"], "disabled")

    def test_only_web_links(self):
        for url in ("javascript:alert(1)", "file:///etc/passwd", "https://[", "https:///missing"):
            self.assertEqual(safe_link(url), "")
        self.assertEqual(safe_link("https://example.com/a"), "https://example.com/a")

    def test_persisted_edition_and_android_status(self):
        with tempfile.TemporaryDirectory() as home:
            root = Path(home) / "briefs"
            root.mkdir()
            self.assertEqual(load_previews(home), {})
            html = root / "latest.html"
            html.write_text("A brief")
            value = reading_previews(self.sections(), "2026-09-09")
            value["html_mtime_ns"] = html.stat().st_mtime_ns
            sidecar = root / "latest-preview.json"
            sidecar.write_text(json.dumps(value), encoding="utf-8")
            self.assertEqual(load_previews(home)["paper"]["title"], "A paper")
            with patch.object(android_entry, "configure"):
                self.assertEqual(json.loads(android_entry.status(home))["previews"]["date"], "2026-09-09")
            value["html_mtime_ns"] -= 1
            sidecar.write_text(json.dumps(value))
            self.assertEqual(load_previews(home), {})  # A different/failed edition cannot reuse these.
            sidecar.write_text("broken json")
            self.assertEqual(load_previews(home), {})

    def test_html_has_daily_readings_even_without_plan(self):
        secs = self.sections()
        secs["threexthree"] = S.Section("threexthree", S.EMPTY, reason="No plan yet")
        secs["feed:climate"].data[0].title = '<script>alert("x")</script>Headline'
        html = db.compose_page(db.DEFAULT_CONFIG, dt.date(2026, 9, 9), secs, [], {}, "")
        self.assertIn("Daily · Read &amp; explore", html)
        self.assertIn("Top climate article", html)
        self.assertIn("An abstract", html)
        self.assertIn("No plan yet", html)
        self.assertNotIn('<script>alert("x")</script>', html)

    def test_run_publishes_previews_and_failure_invalidates_them(self):
        with tempfile.TemporaryDirectory() as home, ExitStack() as stack:
            root = Path(home)
            briefs = root / "briefs"
            for name, value in {"BASE": root, "BRIEFS_DIR": briefs, "LATEST_HTML": briefs / "latest.html",
                                "STATE_PATH": root / "state.json"}.items():
                stack.enter_context(patch.object(db, name, value))
            stack.enter_context(patch.object(db, "log"))
            stack.enter_context(patch.object(db, "load_config", return_value=dict(db.DEFAULT_CONFIG, toast=False)))
            generate = stack.enter_context(patch.object(db, "generate", return_value=(
                "TLDR: A reading brief.", {"engine": "local"}, self.sections(), [])))
            args = SimpleNamespace(force=True, open=False)
            self.assertEqual(db.cmd_run(args), 0)
            self.assertEqual(load_previews(home)["paper"]["title"], "A paper")
            generate.side_effect = RuntimeError("Offline")
            self.assertEqual(db.cmd_run(args), 1)
            self.assertEqual(load_previews(home), {})


if __name__ == "__main__":
    unittest.main(testRunner=unittest.TextTestRunner(stream=sys.stdout))
