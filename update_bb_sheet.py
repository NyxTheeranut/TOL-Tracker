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

── Why this fetches before it writes ────────────────────────────────────────
Raw TOL_*.txt exports live only in TOL/Data/ on this one machine -- never
committed anywhere, never backed up elsewhere. If that folder ever has fewer
months than usual (a fresh machine, an accidentally-cleared folder, a laptop
swap), a naive "aggregate whatever's here and overwrite the Sheet" sync would
silently DELETE the missing months from the Sheet too -- and once gone,
they're gone for good; the Sheet is the only remaining copy past MAX_MONTHS
ago. So this script fetches the Sheet's current state first (getSyncData),
merges in whatever fresh months build_output() actually produced locally
(sheet_schema.merge_months -- local data always wins for months it covers,
older Sheet-only months are carried through untouched, and the whole thing
is still capped to aggregate_bb.MAX_MONTHS), and only then writes the union
back. A month can only ever be dropped by that cap once genuinely newer
months push it off the old end -- never by an incomplete local folder.
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


def post(sync_secret, action, **fields):
    payload = json.dumps({"action": action, "secret": sync_secret, **fields}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        SYNC_URL, data=payload, method="POST",
        headers={"Content-Type": "text/plain;charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as res:
            body = res.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{action} failed: HTTP {e.code}\n{e.read().decode('utf-8', 'replace')[:500]}")
    except urllib.error.URLError as e:
        raise SystemExit(f"{action} failed: {e.reason}")
    try:
        result = json.loads(body)
    except json.JSONDecodeError:
        raise SystemExit(
            f"{action} failed: response wasn't JSON (the Web App URL may need to be "
            "redeployed with \"Who has access: Anyone\").\n"
            f"First 300 chars of response:\n{body[:300]}"
        )
    if not result.get("ok"):
        raise SystemExit(f"{action} failed: {result.get('error')}")
    return result


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
    new_out = aggregate_bb.build_output()
    total = sum(m["installs"] for m in new_out["months"])
    total_reg = sum(m["installsReg"] for m in new_out["months"])
    print(f"  local data covers: {[m['key'] for m in new_out['months']]}   "
          f"{total} installs   {total_reg} registrations")

    print("Fetching the Sheet's current state (to merge into, not overwrite)...")
    existing = post(sync_secret, "getSyncData").get("payload")
    if existing is None:
        print("  nothing synced yet -- this will be the first push.")
        out = new_out
        # Still apply the same cap a fresh build_output() would already
        # satisfy in practice (discover_months() already caps it) -- explicit
        # here so this script's behavior doesn't depend on that detail.
        if len(out["months"]) > aggregate_bb.MAX_MONTHS:
            out["months"] = out["months"][-aggregate_bb.MAX_MONTHS:]
    else:
        print(f"  Sheet currently has: {[m['key'] for m in existing['months']]}")
        out = sheet_schema.merge_months(existing, new_out, aggregate_bb.MAX_MONTHS)
        print(f"  merged result covers: {[m['key'] for m in out['months']]}")

    tabs = sheet_schema.flatten(out)
    total_rows = sum(len(t["rows"]) for t in tabs.values())
    print(f"Flattened into {len(tabs)} tabs, {total_rows} rows total:")
    for name, t in tabs.items():
        print(f"  {name:<20} {len(t['rows']):>6} rows")

    payload_preview = json.dumps(tabs, ensure_ascii=False)
    print(f"Upload size: {len(payload_preview) / 1e6:.2f} MB")

    print("Uploading to Google Sheet...")
    result = post(sync_secret, "syncBbData", tabs=tabs)
    print(f"Done -- {result.get('rows')} rows synced across {result.get('tabs')} tabs.")


if __name__ == "__main__":
    main()
