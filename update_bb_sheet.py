#!/usr/bin/env python3
"""
Pushes the TOL Install Tracker's aggregated data into the Google Sheet this
project's Apps Script reads from -- normalized across real tabs with real
columns (District, Channel, Buildings, ...), readable and auditable directly
in Sheets, matching PakKret Territory Explorer / Route Planner -- so the
hosted page never needs a local rebuild, just this sync.

Run this any time a fresh TOL_*.txt / BB_CURRENT_MTH.txt export lands in
TOL/Data/ -- same trigger as running aggregate_bb.py locally, just pushed to
the Sheet instead of (or as well as) written to a local HTML file.

This reuses aggregate_bb.py's build_output() directly -- the exact same
aggregation the local "BB Current Month Tracker.html" is built from -- so the
two never drift into computing installs/registrations differently.
sheet_schema.flatten() then decomposes that nested output into the flat
tables actually written to the Sheet; see that module's docstring for the
full schema and why (the Apps Script side reconstructs the nested shape back
from those tables -- see its reconstructPayload_, a hand-ported mirror of
sheet_schema.reconstruct(), which this repo's own test proves lossless
against real data).
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DASHBOARD_DIR = HERE.parent
sys.path.insert(0, str(DASHBOARD_DIR / "TOL"))
import aggregate_bb  # noqa: E402  (path must be set up first)
import sheet_schema  # noqa: E402

# Keep this in sync with DEFAULT_SYNC_URL in index.html -- if you change one,
# change the other.
SYNC_URL = "https://script.google.com/macros/s/AKfycbxKW_yaYY4B2O3nsX_12Wgzpg_QWOUWS5HOiMZROUMSGrVqUWTwiHsRjNPcyNapelj-/exec"

SYNC_SECRET_FILE = DASHBOARD_DIR / "Config" / "bb_sync_secret.txt"


def main():
    if not SYNC_SECRET_FILE.exists():
        raise SystemExit(
            f"Not found: {SYNC_SECRET_FILE}\n"
            "Create it containing the same value as the SYNC_SECRET Script "
            "Property in the Apps Script project, with no extra whitespace."
        )
    sync_secret = SYNC_SECRET_FILE.read_text(encoding="utf-8").strip()

    if SYNC_URL.startswith("REPLACE_WITH"):
        raise SystemExit(
            "SYNC_URL at the top of this script still needs to be set to "
            "your Apps Script Web App URL -- see this repo's README."
        )

    print("Running the same aggregation as aggregate_bb.py...")
    out = aggregate_bb.build_output()
    total = sum(m["installs"] for m in out["months"])
    total_reg = sum(m["installsReg"] for m in out["months"])
    print(f"  {len(out['months'])} months   {total} installs   {total_reg} registrations   "
          f"{out['meta']['totalBuildings']} buildings")

    tabs = sheet_schema.flatten(out)
    total_rows = sum(len(t["rows"]) for t in tabs.values())
    print(f"Flattened into {len(tabs)} tabs, {total_rows} rows total:")
    for name, t in tabs.items():
        print(f"  {name:<20} {len(t['rows']):>6} rows")

    payload = json.dumps({
        "action": "syncBbData", "tabs": tabs, "secret": sync_secret,
    }, ensure_ascii=False).encode("utf-8")
    print(f"Upload size: {len(payload) / 1e6:.2f} MB")
    req = urllib.request.Request(
        SYNC_URL, data=payload, method="POST",
        headers={"Content-Type": "text/plain;charset=utf-8"},
    )
    print("Uploading to Google Sheet...")
    try:
        with urllib.request.urlopen(req, timeout=120) as res:
            body = res.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Upload failed: HTTP {e.code}\n{e.read().decode('utf-8', 'replace')[:500]}")
    except urllib.error.URLError as e:
        raise SystemExit(f"Upload failed: {e.reason}")

    try:
        result = json.loads(body)
    except json.JSONDecodeError:
        raise SystemExit(
            "Upload failed: response wasn't JSON (the Web App URL may need to be "
            "redeployed with \"Who has access: Anyone\").\n"
            f"First 300 chars of response:\n{body[:300]}"
        )

    if result.get("ok"):
        print(f"Done -- {result.get('rows')} rows synced across {result.get('tabs')} tabs.")
    else:
        raise SystemExit(f"Upload failed: {result.get('error')}")


if __name__ == "__main__":
    main()
