"""Location + unit settings: do they actually take effect when edited?"""
import datetime as dt
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import dailybrief as db
import sources as S

CFG = ROOT / "config.json"
BACKUP = CFG.with_suffix(".json.testbak")
shutil.copyfile(CFG, BACKUP)

checks = {}
def check(n, c): checks[n] = bool(c)

try:
    # --- fingerprint / staleness --------------------------------------------
    fresh = {"city": "Edinburgh", "postcode": "", "country": "GB",
             "latitude": 55.9521, "longitude": -3.1965, "label": "Edinburgh"}
    fresh["resolved_from"] = db._location_fingerprint(fresh)
    check("fresh cache is not stale", not db.has_coords_stale(fresh))

    edited = dict(fresh, city="Glasgow")
    check("hand-edited city is detected as stale", db.has_coords_stale(edited))

    pinned = {"city": "", "postcode": "", "latitude": 51.5, "longitude": -0.12, "label": "pinned"}
    check("pinned coords never stale", not db.has_coords_stale(pinned))

    # --- editing the city re-resolves ---------------------------------------
    cfg = json.loads(CFG.read_text("utf-8-sig"))
    cfg["location"] = dict(cfg["location"], city="Glasgow")   # hand-edit, stale cache
    before = (cfg["location"]["latitude"], cfg["location"]["longitude"])
    loc, problem = db.resolve_location(cfg, save=False)
    check("edited city re-resolved", problem is None and loc["label"].startswith("Glasgow"))
    check("coordinates actually moved", (loc["latitude"], loc["longitude"]) != before)
    check("division follows the new place", cfg["bank_holiday_division"] == "scotland")
    print(f"    Glasgow -> {loc['label']} ({loc['latitude']}, {loc['longitude']})")

    # --- unchanged config makes no network call -----------------------------
    calls = []
    orig_geo, orig_pc = S.geocode, S.postcode_lookup
    S.geocode = lambda *a, **k: (calls.append("geo"), orig_geo(*a, **k))[1]
    S.postcode_lookup = lambda *a, **k: (calls.append("pc"), orig_pc(*a, **k))[1]
    settled = json.loads(CFG.read_text("utf-8-sig"))
    settled["location"]["resolved_from"] = db._location_fingerprint(settled["location"])
    db.resolve_location(settled, save=False)
    check("no lookup when nothing changed", not calls)

    # --- postcode path ------------------------------------------------------
    pc_cfg = {"location": {"postcode": "EH1 1YZ", "city": "", "country": "GB",
                           "latitude": None, "longitude": None}}
    loc2, prob2 = db.resolve_location(pc_cfg, save=False)
    check("postcode resolves", prob2 is None and loc2["latitude"] is not None)
    check("postcode picks Scottish holidays", pc_cfg.get("bank_holiday_division") == "scotland")
    print(f"    EH1 1YZ -> {loc2['label']} ({loc2['latitude']}, {loc2['longitude']})")
    S.geocode, S.postcode_lookup = orig_geo, orig_pc

    # --- bad postcode degrades to a stated problem, not a crash -------------
    bad = {"location": {"postcode": "ZZ99 9ZZ", "city": "", "latitude": None, "longitude": None}}
    _l, prob3 = db.resolve_location(bad, save=False)
    check("bad postcode reports a reason", prob3 is not None and "ZZ99" in prob3)

    # --- unresolvable but previously cached keeps working, loudly ------------
    stalecfg = {"location": {"postcode": "ZZ99 9ZZ", "city": "", "latitude": 55.9, "longitude": -3.1,
                             "label": "old", "resolved_from": "x"}}
    l4, prob4 = db.resolve_location(stalecfg, save=False)
    check("keeps stale coords but warns", l4["latitude"] == 55.9 and prob4 and "previous" in prob4)

    # --- BOM: Notepad and PowerShell both write one when you edit by hand ----
    original = CFG.read_bytes()
    CFG.write_bytes(b"\xef\xbb\xbf" + original.lstrip(b"\xef\xbb\xbf"))
    bom_cfg = db.load_config()
    check("BOM config still parses", bom_cfg.get("location", {}).get("city") == "Edinburgh")
    check("BOM config not silently defaulted", bom_cfg.get("bank_holiday_division") == "scotland")
    CFG.write_bytes(original)

    # --- partial edits keep untouched nested keys ---------------------------
    CFG.write_text(json.dumps({"units": {"temperature": "fahrenheit"}}), "utf-8")
    part = db.load_config()
    check("partial nested edit merges", part["units"]["temperature"] == "fahrenheit"
          and part["units"]["wind"] == "mph")
    check("untouched top-level keys survive", part["engine"] == "local")
    CFG.write_bytes(original)

    # --- corrupt config is loud, not silent ---------------------------------
    CFG.write_text("{ this is not json", "utf-8")
    broken = db.load_config()
    check("corrupt config falls back to defaults", broken["engine"] == "local")
    CFG.write_bytes(original)

    # --- units --------------------------------------------------------------
    metric = S.units_from({"temperature": "celsius", "wind": "mph", "clock": "24h"})
    us = S.units_from({"temperature": "fahrenheit", "wind": "kmh", "precipitation": "inch", "clock": "12h"})
    check("metric symbols", metric["temperature_symbol"] == "\u00b0C" and metric["wind_symbol"] == " mph")
    check("imperial symbols", us["temperature_symbol"] == "\u00b0F" and us["wind_symbol"] == " km/h")
    check("garbage unit falls back", S.units_from({"temperature": "kelvin"})["temperature"] == "celsius")
    check("12h clock", S._clock("2026-08-13T05:40", "12h") == "5:40am")
    check("12h clock pm", S._clock("2026-08-13T20:54", "12h") == "8:54pm")
    check("12h midnight", S._clock("2026-08-13T00:10", "12h") == "12:10am")
    check("12h noon", S._clock("2026-08-13T12:00", "12h") == "12:00pm")
    check("24h clock", S._clock("2026-08-13T05:40", "24h") == "05:40")
    check("clock handles None", S._clock(None, "24h") is None)

    # --- units reach the rendered brief -------------------------------------
    wdata = dict(condition="Overcast", high=82.0, low=61.0, precip_prob=39, precip_mm=0.0,
                 wind_mph=13.0, now_temp=79.0, now_feels=78.0, now_condition="Clear sky",
                 sunrise="5:40am", sunset="8:54pm", for_date="2026-08-13", code=3,
                 timezone="Europe/London", units=us)
    md = db.compose_markdown({"units": {"temperature": "fahrenheit", "wind": "kmh"}, "tech_items": 2},
                             dt.date(2026, 8, 13), {"weather": S.Section("weather", S.OK, data=wdata)}, [])
    check("fahrenheit rendered", "82\u00b0F" in md and "\u00b0C" not in md)
    check("kmh rendered", "km/h" in md)
    check("12h times rendered", "5:40am" in md and "8:54pm" in md)

finally:
    shutil.copyfile(BACKUP, CFG)
    BACKUP.unlink()

bad_n = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(("  PASS " if v else "  FAIL ") + k)
print(f"\n{len(checks) - len(bad_n)}/{len(checks)} passed  (config.json restored)")
sys.exit(1 if bad_n else 0)
