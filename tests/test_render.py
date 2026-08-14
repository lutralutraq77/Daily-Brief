"""Torture-test the dependency-free markdown renderer."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import dailybrief as db

MD = """TLDR: Rain until noon, two things need a decision, and the build is green.

## Weather

**Manchester** — 12/6 C, *heavy rain* until ~13:00, 80% chance. Take a coat.

## News

- [Reuters](https://reuters.com/x) reports a thing with `--flags` in it
- Nested detail:
  - sub-bullet one
  - sub-bullet two with **bold** and a bare https://example.com/path?a=1
    1. numbered inner
    2. second
- Back to top level, with a continuation line
  that wraps onto the next source line.

1. Ordered top level
2. Second item

### Table

| Item | Status | Cost |
|:-----|:------:|-----:|
| Ingest | **green** | $0.02 |
| Upload | pending | $0 |

> A quote with `code` and **bold**.
> Second quote line.

---

```python
def hello(x):
    return f"<not escaped> & {x}"
```

Edge cases: 2 * 3 * 4 should not italicise. A snake_case_word stays intact.
~~struck~~ and ***both*** and <script>alert(1)</script> must be escaped.
"""

tldr, body = db.split_tldr(MD)
print("TLDR ->", repr(tldr))
print()
html = db.markdown_to_html(body)

page = db.render_page(
    heading="Renderer test",
    lede=tldr,
    body_html=html,
    meta_bits=["test", "no Claude call"],
    footer="renderer torture test",
)
out = Path(__file__).with_name("render_test.html")
out.write_text(page, "utf-8")
print(html)
print()
print("wrote", out)

# Structural sanity checks
checks = {
    "script escaped": "&lt;script&gt;" in html and "<script>" not in html,
    "table built": "<table>" in html and "text-align:right" in html,
    "nested ul": html.count("<ul>") >= 2,
    "ordered list": "<ol>" in html,
    "blockquote": "<blockquote>" in html,
    "code fence": '<pre><code class="lang-python">' in html,
    "fence content escaped": "&lt;not escaped&gt; &amp;" in html,
    "md link": '<a href="https://reuters.com/x"' in html,
    "bare url linked": '<a href="https://example.com/path?a=1"' in html,
    "no double-link": html.count('href="https://reuters.com/x"') == 1,
    "snake_case intact": "snake_case_word" in html,
    "no stray asterisk italics": "<em>3</em>" not in html,
    "strikethrough": "<del>struck</del>" in html,
    "bold+italic": "<strong><em>both</em></strong>" in html,
    "hr": "<hr>" in html,
    "no placeholder leak": "\x00" not in html,
    "lists closed": html.count("<ul>") == html.count("</ul>") and html.count("<ol>") == html.count("</ol>"),
    "li balanced": html.count("<li>") == html.count("</li>"),
}
print()
bad = 0
for k, v in checks.items():
    print(("  PASS " if v else "  FAIL ") + k)
    if not v:
        bad += 1
print(f"\n{len(checks)-bad}/{len(checks)} passed")
sys.exit(1 if bad else 0)
