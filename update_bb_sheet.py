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
import socket
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
        # Apps Script's per-column setNumberFormat() loop in writeTab_ (fixed
        # to 2 batched getRangeList() calls instead of one per column, but
        # kept here as a safety margin) could make a single large-tab append
        # slow enough on a big sheet to brush against a 120s client timeout
        # even though the write itself would have finished fine given a bit
        # longer.
        with urllib.request.urlopen(req, timeout=180) as res:
            body = res.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{action} failed: HTTP {e.code}\n{e.read().decode('utf-8', 'replace')[:500]}")
    except urllib.error.URLError as e:
        raise SystemExit(f"{action} failed: {e.reason}")
    except socket.timeout:
        # A read timeout only means THIS client gave up waiting -- Apps
        # Script keeps running server-side regardless, so the write this
        # request was making may well have completed anyway. Just re-running
        # the whole command is always safe: every tab's first chunk clears it
        # before appending, so a fresh run naturally overwrites whatever a
        # half-finished previous run left behind rather than duplicating it.
        raise SystemExit(
            f"{action} timed out waiting for a response (Apps Script may still be "
            "finishing server-side). Safe to just run this command again -- every "
            "tab gets cleared before its data is rewritten, so a retry can't leave "
            "duplicate rows behind."
        )
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


PROGRESS_TOTAL_STEPS = 4


def progress(step, label, width=28):
    filled = int(width * step / PROGRESS_TOTAL_STEPS)
    bar = "█" * filled + "░" * (width - filled)
    pct = int(100 * step / PROGRESS_TOTAL_STEPS)
    print(f"\n[{bar}] {pct:3d}%  Step {step}/{PROGRESS_TOTAL_STEPS}: {label}")


def upload_bar(done_bytes, total_bytes, width=28):
    frac = min(done_bytes / total_bytes, 1) if total_bytes else 1
    filled = int(width * frac)
    bar = "█" * filled + "░" * (width - filled)
    pct = int(100 * frac)
    print(f"\r  [{bar}] {pct:3d}%  {done_bytes / 1e6:.2f}/{total_bytes / 1e6:.2f} MB uploaded",
          end="", flush=True)


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

    progress(1, "Aggregating local source files")
    print("Running the same aggregation as aggregate_bb.py...")
    new_out = aggregate_bb.build_output()
    total = sum(m["installs"] for m in new_out["months"])
    total_reg = sum(m["installsReg"] for m in new_out["months"])
    print(f"  local data covers: {[m['key'] for m in new_out['months']]}   "
          f"{total} installs   {total_reg} registrations")

    progress(2, "Fetching the Sheet's current state")
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

    progress(3, "Preparing tabs to upload")
    tabs = sheet_schema.flatten(out)
    total_rows = sum(len(t["rows"]) for t in tabs.values())
    print(f"Flattened into {len(tabs)} tabs, {total_rows} rows total:")
    for name, t in tabs.items():
        print(f"  {name:<20} {len(t['rows']):>6} rows")

    payload_preview = json.dumps(tabs, ensure_ascii=False)
    total_bytes = len(payload_preview.encode("utf-8"))
    print(f"Upload size: {total_bytes / 1e6:.2f} MB")

    progress(4, "Uploading to Google Sheet")
    total_tabs = 0
    total_rows = 0
    done_bytes = 0
    for i, chunk in enumerate(chunk_tabs(tabs), 1):
        chunk_json = json.dumps(chunk, ensure_ascii=False)
        chunk_mb = len(chunk_json) / 1e6
        print(f"  chunk {i}: {list(chunk.keys())} ({chunk_mb:.2f} MB)")
        result = post(sync_secret, "syncBbData", tabs=chunk)
        total_tabs += result.get("tabs", 0)
        total_rows += result.get("rows", 0)
        done_bytes += len(chunk_json.encode("utf-8"))
        upload_bar(done_bytes, total_bytes)
    print()
    print(f"Done -- {total_rows} rows synced across {total_tabs} tabs.")


def chunk_tabs(tabs, max_bytes=400_000):
    """Splits {tab_name: {header, rows}} into several smaller dicts, each
    under roughly max_bytes of JSON, instead of one big payload. A single
    ~3MB POST to the Apps Script Web App comes back as a bare Google error
    page (rejected before doPost even runs) rather than a JSON error from
    our own code -- whatever Google's actual limit is, staying well under it
    avoids the question entirely. That limit turned out to be closer than
    it looked: a 900KB chunk worked once, then started failing the moment
    Buildings picked up 4 more columns and crossed ~0.9MB -- so this cap is
    set with real headroom below wherever the true ceiling is, not right up
    against the last size that happened to work.

    Greedy bin-packing by whole tab where that fits (most tabs are small);
    a tab bigger than max_bytes on its own (BuildingsBreakdown runs ~1.7MB
    already, and only grows with more months) gets its OWN rows split
    across several requests instead, each marked "append" after the first
    so syncBbData_ knows not to re-clear the sheet and lose the earlier
    pieces."""
    chunk, chunk_size = {}, 0
    for name, t in tabs.items():
        header, rows = t["header"], t["rows"]
        t_size = len(json.dumps(t, ensure_ascii=False))
        if t_size <= max_bytes:
            if chunk and chunk_size + t_size > max_bytes:
                yield chunk
                chunk, chunk_size = {}, 0
            chunk[name] = t
            chunk_size += t_size
            continue

        if chunk:
            yield chunk
            chunk, chunk_size = {}, 0
        avg_row_size = t_size / max(len(rows), 1)
        rows_per_piece = max(int(max_bytes / avg_row_size), 1)
        for i in range(0, len(rows), rows_per_piece):
            piece = {"header": header, "rows": rows[i:i + rows_per_piece]}
            if i > 0:
                piece["append"] = True
            yield {name: piece}
    if chunk:
        yield chunk


if __name__ == "__main__":
    main()
