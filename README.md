# TOL Install Tracker

Installs + registrations for the NTB : Pak Kret, Bang Bua Thong, Sai Noi
cluster, read live from a Google Sheet -- district/channel/sub-channel/
dealer-territory/buildings breakdowns, a Connect/Register toggle, and a
per-status filter (Connect/Pending/Cancel/Un-Complete) for Register mode.

Anyone signed in with an allow-listed Google account sees the same full
dataset -- the page itself ships with no install data at all; it's fetched
after sign-in.

## How it fits together

```
Browser (this page, hosted on GitHub Pages)
   │  Google Sign-In (Google Identity Services)
   ▼
Apps Script Web App  ──executes as the Sheet owner──▶  Google Sheet
   │   verifies the ID token against Google directly        "Users" tab (who's allowed in)
   │   checks the signed-in email is in the "Users" tab      "Data" tab (the aggregated JSON, chunked)
   │   returns the aggregated data as one JSON payload
   ▼
this page renders every table/chart from that payload, exactly like the
local "BB Current Month Tracker.html" does from its embedded copy
```

`update_bb_sheet.py` is a separate, local-only tool -- it re-runs the same
aggregation as `TOL/aggregate_bb.py` (importing `build_output()` directly, so
the two never compute installs/registrations differently) and pushes the
result into the Sheet. It's never called from the hosted page.

Unlike L2 Discount Map's data (flat rows -- one per splitter), this
dashboard's aggregated output is one deeply nested JSON object (months,
per-district/channel/sub-channel/dealer-territory/supervisor/technology/
buildings breakdowns, for Connect AND the whole Register-mode mirror
including its per-status slices). Rather than flattening that into many
Sheet tabs, it's stored as one JSON blob, chunked across rows in a single
"Data" tab (a Sheets cell caps out at 50,000 characters; the current payload
runs a few MB) -- `myBbData` just concatenates the chunks back into one
string and parses it.

## One-time setup

### 1. Google Cloud Console — OAuth Client ID

1. Go to [console.cloud.google.com](https://console.cloud.google.com/) and
   create a new project (or pick an existing one) dedicated to this app --
   keep it separate from L2's or Route Planner's OAuth client, so the three
   stay independent.
2. **APIs & Services → OAuth consent screen** -- configure it (External or
   Internal depending on your Google Workspace situation), add yourself as a
   test user if it stays in "Testing" publish status.
3. **APIs & Services → Credentials → Create Credentials → OAuth client ID**.
   - Application type: **Web application**
   - Authorized JavaScript origins -- add both:
     - `https://<your-github-username>.github.io`
     - `http://localhost:8093` (for local testing via
       `Start TOL Tracker.command`)
   - Leave "Authorized redirect URIs" empty -- Google Identity Services'
     sign-in button doesn't use a redirect flow.
4. Copy the **Client ID** (looks like `123...-abc....apps.googleusercontent.com`).
   You do **not** need the client secret for this -- the page only ever uses
   the Client ID, client-side.

### 2. Google Sheet + Apps Script backend

1. Create a new Google Sheet, dedicated to this app (don't reuse L2's or
   Route Planner's Sheet -- this project's Apps Script deployment and secret
   are its own, independent set).
2. **Extensions → Apps Script**, delete the starter code, paste in the full
   contents of `Sheets Sync - Apps Script Code.gs` from this repo.
3. **Project Settings** (gear icon, left sidebar) → **Script Properties** →
   add two:
   - `OAUTH_CLIENT_ID` = the Client ID from step 1.
   - `SYNC_SECRET` = any random string, e.g. from `openssl rand -hex 24` in a
     terminal. Gates the `syncBbData` action (used only by
     `update_bb_sheet.py`, see step 5) -- without it, anyone who finds the
     deployment URL could overwrite the entire dataset with one request.
4. **Deploy → New deployment**
   - Type: **Web app**
   - Execute as: **Me**
   - Who has access: **Anyone**
     (real access control happens via the ID token + Users tab check inside
     the script, not via this deployment setting)
5. Deploy, authorize when prompted, copy the **Web app URL**.

### 3. Wire the two together

1. In `index.html`, set `GOOGLE_CLIENT_ID` (near the bottom of the
   `<script>` block) to the Client ID from step 1.
2. Set `DEFAULT_SYNC_URL` to the Web app URL from step 2.
3. In `update_bb_sheet.py`, set `SYNC_URL` to that same Web app URL.

### 4. Add your team to the Users tab

1. Open the page and sign in once -- this auto-creates a "Users" tab in the
   Sheet with a sample row (and, until you've done this, `myBbData` correctly
   returns "not set up yet" for everyone, including you).
2. In the Sheet, edit that row (or add a new one) for yourself: `email |
   note` -- the `note` column is just for your own reference, it isn't read
   by the script. Add a row per teammate. Delete the sample row.

### 5. Push the data

Create a file named `bb_sync_secret.txt` outside this repo, in the
`Dashboard` folder's `Config/` directory -- containing exactly the
`SYNC_SECRET` value from step 2, no extra whitespace (this is a separate
file from L2's and Route Planner's secrets -- all three projects' secrets
are independent). **Never commit this file**; it lives outside the repo
specifically so it can't be.

Then run:

```
python3 update_bb_sheet.py
```

(or double-click `Update TOL Tracker.command` in the Dashboard folder's
`Launchers/`) to populate the "Data" tab. Re-run it any time a fresh
`TOL_*.txt` / `BB_CURRENT_MTH.txt` export lands in `TOL/Data/` -- same
trigger as re-running `aggregate_bb.py` for the local version.

### 6. Deploy to GitHub Pages

Push this repo to GitHub, then **Settings → Pages → Source: Deploy from a
branch → `main` / `(root)`**. The page will be live at
`https://<your-username>.github.io/<repo-name>/`.

## Local testing

`Start TOL Tracker.command` (in the Dashboard folder's `Launchers/`) serves
this page over `http://localhost:8093` instead of `file://` -- Google
Sign-In only works from an origin that's on the OAuth client's allow-list,
and `file://` isn't one you can add.

## Relationship to the local version

`TOL/aggregate_bb.py` and "BB Current Month Tracker.html" still work exactly
as before -- opening that file locally needs no sign-in, no internet, no
Sheet. This project is an alternative distribution of the same numbers for
sharing with people who shouldn't need file access to your machine, not a
replacement. Both read from the same `TOL/Data/` exports and the same
aggregation code (`aggregate_bb.py`'s `build_output()`), so they never
disagree.

## Security notes

- The Apps Script deployment uses "Anyone" access, but that's not the real
  gate -- every `myBbData` request carries a Google ID token, which the
  script verifies directly against Google (checking both the signature and
  that it was issued for *this* app's Client ID) before trusting the email
  in it, then checks that email is a row in the Users tab.
- Every allow-listed viewer gets the same full dataset (all districts,
  dealers, Connect and Register numbers) -- there's no per-person scoping to
  get wrong, unlike Route Planner's ae/cm/admin roles.
- `syncBbData` can't go through the sign-in check at all -- it's not a
  person signing in, it's `update_bb_sheet.py` running on your own machine --
  so it's gated by `SYNC_SECRET` instead (see step 2 and step 5 above). This
  deployment's URL is not actually secret; it's embedded directly in the
  public `index.html`, so without this, "Anyone" access would mean anyone on
  the internet could overwrite the dataset with one request.
- `syncBbData_` round-trip-validates the JSON (`JSON.parse`s it) before
  writing, so a truncated or corrupt upload fails the sync script loudly
  instead of silently breaking the live page for every viewer.
