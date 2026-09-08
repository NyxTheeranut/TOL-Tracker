/**
 * TOL Install Tracker -- Google Sheets backend (sign-in gate + live data).
 *
 * What this is: the API the hosted TOL Tracker page calls. It runs inside a
 * Google Sheet, as that Sheet's owner -- there is no separate server to host
 * or pay for, and the Sheet itself is the database. Modeled on the L2
 * Discount Map / Route Planner projects' Apps Script backends, with the same
 * "every allow-listed viewer sees the same full dataset" model as L2 (no
 * per-person scoping -- unlike Route Planner's ae/cm/admin roles).
 *
 * ── Data layout ────────────────────────────────────────────────────────────
 * "Data" -- the full aggregator output (everything aggregate_bb.py builds:
 *   months, district/channel/subChannel/dealerTerritory/supervisor/technology/
 *   villages for Connect, plus the whole Register mirror incl. its per-status
 *   breakdowns) as ONE JSON blob, chunked across rows because a Sheets cell
 *   caps out at 50,000 characters and this payload runs several MB. Row 1 is
 *   a header; every row after that is one chunk, column A, in order --
 *   myBbData_ just concatenates them back into one string and parses it.
 *   Fully overwritten (never appended) by update_bb_sheet.py.
 * "Users" -- who's allowed to view the tracker: just an email allow-list (no
 *   roles -- an allow-listed email gets the full dataset, both Connect and
 *   Register). Created automatically (with a sample row) the first time
 *   anyone signs in, if it doesn't exist yet.
 *
 * ── Auth ───────────────────────────────────────────────────────────────────
 * The page signs the user in with Google Identity Services and sends the
 * resulting ID token on every request. This script verifies that token
 * against Google directly (no session/cookie trust needed) and checks the
 * token's audience against OAUTH_CLIENT_ID, so it only accepts tokens issued
 * for THIS app -- not a token from some other Google sign-in.
 *
 * A verified token proves WHO is calling, not that they're allowed to. Every
 * action enforces that separately:
 *   myBbData  -> requires the email to be a row in Users. An email that
 *                isn't gets an explicit "not set up" response.
 *   syncBbData -> not a person signing in at all (it's update_bb_sheet.py on
 *                 your own machine), so it can't go through the Users tab --
 *                 gated by a shared SYNC_SECRET instead.
 *
 * ── SETUP (one-time) -- see this repo's README.md for the full walkthrough ─
 *  1. Create a new Google Sheet (or open one dedicated to this app).
 *  2. Extensions -> Apps Script. Delete any starter code, paste this whole file in.
 *  3. Project Settings (gear icon, left sidebar) -> Script Properties -> Add:
 *       OAUTH_CLIENT_ID = <the Client ID from Google Cloud Console>
 *       SYNC_SECRET = <any random string -- e.g. `openssl rand -hex 24` in a
 *       terminal. Also put this exact value into Config/bb_sync_secret.txt
 *       (see update_bb_sheet.py). Without this, anyone who found the
 *       deployment URL could overwrite the entire dataset with one request.
 *  4. Deploy -> New deployment -> Type: Web app.
 *       - Execute as: Me
 *       - Who has access: Anyone
 *     ("Anyone" is fine here -- real access control happens via the ID token +
 *     Users tab check above, not via this deployment setting.)
 *  5. Deploy, authorize when prompted, copy the Web app URL into index.html's
 *     DEFAULT_SYNC_URL and update_bb_sheet.py's SYNC_URL.
 *  6. Sign in once from the page with any Google account -- this creates the
 *     Users tab (with a sample row). Edit it (or add rows) for your team,
 *     delete the sample row.
 *  7. Run update_bb_sheet.py to populate "Data".
 */

// A Sheets cell caps out at 50,000 characters -- chunk comfortably under that
// so cell overhead/edge cases never trip it.
var CHUNK_SIZE = 45000;

function doPost(e) {
  try {
    var body = JSON.parse(e.postData.contents);

    if (body.action === "myBbData") {
      return jsonResponse_(myBbData_(body.idToken));
    }

    if (body.action === "syncBbData") {
      // Only ever called from your own machine via update_bb_sheet.py, but
      // unlike myBbData it can't go through the ID-token/Users-tab check --
      // it's not a person signing in, it's a script. This deployment's URL
      // sits in plain text in the public index.html, so without SOME check,
      // anyone who found it could wipe and replace the entire dataset with a
      // single unauthenticated request.
      requireSyncSecret_(body.secret);
      var result = syncBbData_(body.payload || "");
      return jsonResponse_({ ok: true, bytes: result.bytes, chunks: result.chunks });
    }

    return jsonResponse_({ ok: false, error: "unknown action" });
  } catch (err) {
    return jsonResponse_({ ok: false, error: String(err) });
  }
}

function doGet(e) {
  return jsonResponse_({ ok: true });
}

// ---------- auth ----------

function requireSyncSecret_(secret) {
  var expected = PropertiesService.getScriptProperties().getProperty("SYNC_SECRET");
  if (!expected) throw new Error("SYNC_SECRET script property is not set -- see setup notes at the top of this file");
  if (secret !== expected) throw new Error("forbidden: bad sync secret");
}

// Verifies the ID token directly against Google (not just trusting the client) and
// checks it was issued for THIS app specifically, via the audience claim.
function verifyIdToken_(idToken) {
  if (!idToken) return null;
  var resp = UrlFetchApp.fetch(
    "https://oauth2.googleapis.com/tokeninfo?id_token=" + encodeURIComponent(idToken),
    { muteHttpExceptions: true }
  );
  if (resp.getResponseCode() !== 200) return null;
  var data = JSON.parse(resp.getContentText());
  var expectedClientId = PropertiesService.getScriptProperties().getProperty("OAUTH_CLIENT_ID");
  if (!expectedClientId) throw new Error("OAUTH_CLIENT_ID script property is not set -- see setup notes at the top of this file");
  if (data.aud !== expectedClientId) return null;
  if (!data.email || data.email_verified !== "true") return null;
  return data.email;
}

// Looks up whether a verified email is allow-listed. Creates the Users tab
// (with a sample row) on first use if it doesn't exist yet, so there's
// something to edit.
function isAllowedUser_(email) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName("Users");
  if (!sheet) {
    sheet = ss.insertSheet("Users");
    sheet.appendRow(["email", "note"]);
    sheet.appendRow(["example@gmail.com", "sample row -- replace with your team, then delete this"]);
    sheet.setFrozenRows(1);
    return false; // just created -- nothing real to match against yet
  }
  var data = sheet.getDataRange().getValues();
  var header = data[0];
  var emailCol = header.indexOf("email");
  if (emailCol === -1) return false;
  for (var i = 1; i < data.length; i++) {
    if (String(data[i][emailCol]).trim().toLowerCase() === email.toLowerCase()) return true;
  }
  return false;
}

// ---------- myBbData: the whole point of the auth layer ----------
// A signed-in, allow-listed viewer gets the full dataset -- there's no
// per-person scoping here, every viewer of this tracker sees the same
// districts/dealers/Connect+Register numbers.
function myBbData_(idToken) {
  var email = verifyIdToken_(idToken);
  if (!email) return { ok: false, error: "not_signed_in" };
  if (!isAllowedUser_(email)) {
    return {
      ok: false,
      error: "no_access",
      message: "This Google account (" + email + ") isn't set up yet. Ask an admin to add it to the Users tab.",
    };
  }
  var payload = readDataSheet_();
  if (!payload) return { ok: false, error: "no_data", message: "No data has been synced yet -- run update_bb_sheet.py." };
  return { ok: true, email: email, payload: payload };
}

function readDataSheet_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName("Data");
  if (!sheet) return null;
  var lastRow = sheet.getLastRow();
  if (lastRow < 2) return null;
  var chunks = sheet.getRange(2, 1, lastRow - 1, 1).getValues().map(function (r) { return r[0]; });
  var json = chunks.join("");
  if (!json) return null;
  return JSON.parse(json); // parsed here, not just concatenated -- a bad/partial
                            // upload should fail loudly in the sync step, not
                            // serve broken JSON to every viewer.
}

// ---------- syncBbData: update_bb_sheet.py's write path ----------

function syncBbData_(payload) {
  var json = typeof payload === "string" ? payload : JSON.stringify(payload);
  // Round-trip-validate before writing -- a partial/corrupt upload should
  // fail the sync script loudly rather than silently break the live page for
  // every viewer until the next successful run.
  JSON.parse(json);
  writeDataSheet_(json);
  return { bytes: json.length, chunks: Math.ceil(json.length / CHUNK_SIZE) };
}

function writeDataSheet_(json) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName("Data");
  if (!sheet) sheet = ss.insertSheet("Data");
  sheet.clearContents();
  sheet.getRange(1, 1).setValue("chunk (JSON, split every " + CHUNK_SIZE + " chars -- concatenate column A from row 2 down)");

  var rows = [];
  for (var i = 0; i < json.length; i += CHUNK_SIZE) {
    rows.push([json.substring(i, i + CHUNK_SIZE)]);
  }
  if (rows.length) {
    var range = sheet.getRange(2, 1, rows.length, 1);
    // Plain-text format BEFORE writing -- Sheets "helpfully" auto-detects
    // some strings as numbers/dates otherwise, which would corrupt a JSON
    // chunk that happens to start looking numeric.
    range.setNumberFormat("@");
    range.setValues(rows);
  }
  sheet.setFrozenRows(1);
}

// ---------- shared ----------

function jsonResponse_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(
    ContentService.MimeType.JSON,
  );
}
