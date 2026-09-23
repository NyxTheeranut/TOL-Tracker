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
 * The aggregator's output (aggregate_bb.py's build_output()) is one deeply
 * nested object -- months, district/channel/subChannel/dealerTerritory/
 * supervisor/technology/buildings breakdowns, for Connect and the whole
 * Register mirror including its per-status slices. Rather than one JSON blob,
 * it's stored NORMALIZED across real tabs with real columns -- readable and
 * auditable directly in Sheets, matching PakKret Territory Explorer / Route
 * Planner. update_bb_sheet.py's sheet_schema.flatten() does the decomposition
 * (Python, single source of truth for the shape); reconstructPayload_ below
 * is a hand-ported mirror of that same module's reconstruct() -- Apps Script
 * can't import Python, so the two must be kept in sync by hand if the schema
 * ever changes. TAB_NAMES lists every tab this round-trip touches:
 *   Meta               1 row of top-level metadata.
 *   Months             1 row per month -- every scalar metric (Connect
 *                      totals, Register totals, Register-by-status totals)
 *                      as its own column.
 *   Daily              1 row per (month, day, mode) with nonzero activity --
 *                      mode is "connect", "register", or "register:<Status>".
 *   District, Channel, SubChannel, DealerTerritory, Supervisor, Technology
 *                      1 row per (mode, name, month) breakdown fact. Same
 *                      columns in all six (gaImport/target blank where that
 *                      dimension doesn't carry one).
 *   Buildings          1 row per (mode, category, name, month) -- category is
 *                      "matched" (has an Active FTTH census match) or
 *                      "bbOnly".
 *   VillagesFtthOnly   1 row per (mode, name) -- villages with zero installs
 *                      in every month.
 *   BuildingsBreakdown 1 row per (mode, building, channel, specialChannel,
 *                      territory, partner, month) -- LEAF facts only; the
 *                      rollup subtotals at each level are recomputed here,
 *                      not stored (they're just sums of these leaves).
 * "Users" -- who's allowed to view the tracker: just an email allow-list (no
 *   roles). Created automatically (with a sample row) the first time anyone
 *   signs in, if it doesn't exist yet.
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
 *   getSyncData / syncBbData -> not a person signing in at all (it's
 *                 update_bb_sheet.py on your own machine, reading the
 *                 current state before merging in fresh months and then
 *                 writing the result back), so neither can go through the
 *                 Users tab -- both gated by a shared SYNC_SECRET instead.
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
 *  7. Run update_bb_sheet.py to populate the tabs listed above.
 */

var TAB_NAMES = [
  "Meta",
  "Months",
  "Daily",
  "District",
  "Channel",
  "SubChannel",
  "DealerTerritory",
  "Supervisor",
  "Technology",
  "Buildings",
  "VillagesFtthOnly",
  "BuildingsBreakdown",
  "BuildingsDaily",
  "BuildingsDailyBreakdown",
];
var STATUS_ORDER = ["Connect", "Pending", "Cancel", "Un-Complete", "Other"];
// Columns that must stay plain text -- Sheets otherwise auto-detects a
// numeric-looking string as a real number (stripping leading zeros), or a
// human-readable string as a DATE, silently corrupting either way. Text
// columns like district/channel names are never date-or-numeric-looking, so
// this only needs to cover ID-shaped and date-ish-looking values
// specifically. ("label", e.g. "March 2026", isn't stored at all -- see
// labelFromKey_ below -- specifically because setNumberFormat("@") did NOT
// reliably stop Sheets auto-converting it to a real date on write, unlike
// every other column here; deriving it from "key" sidesteps that entirely.)
var TEXT_COLUMNS = ["key", "month", "bid", "file", "short", "mtime"];

var MONTH_NAMES_ = [
  "",
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
];
function labelFromKey_(key) {
  var y = String(key).substring(0, 4),
    m = parseInt(String(key).substring(4, 6), 10);
  return MONTH_NAMES_[m] + " " + y;
}

function doPost(e) {
  try {
    var body = JSON.parse(e.postData.contents);

    if (body.action === "myBbData") {
      return jsonResponse_(myBbData_(body.idToken));
    }

    if (body.action === "getSyncData") {
      // Read-only counterpart of syncBbData, for update_bb_sheet.py to fetch
      // the Sheet's current state before merging in fresh local months --
      // gated by the same secret (not a person signing in, so no ID token).
      requireSyncSecret_(body.secret);
      var tabs = readAllTabs_();
      return jsonResponse_({
        ok: true,
        payload: tabs ? reconstructPayload_(tabs) : null,
      });
    }

    if (body.action === "syncBbData") {
      // Only ever called from your own machine via update_bb_sheet.py, but
      // unlike myBbData it can't go through the ID-token/Users-tab check --
      // it's not a person signing in, it's a script. This deployment's URL
      // sits in plain text in the public index.html, so without SOME check,
      // anyone who found it could wipe and replace the entire dataset with a
      // single unauthenticated request.
      requireSyncSecret_(body.secret);
      var result = syncBbData_(body.tabs || {});
      return jsonResponse_({ ok: true, tabs: result.tabs, rows: result.rows });
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
  var expected =
    PropertiesService.getScriptProperties().getProperty("SYNC_SECRET");
  if (!expected)
    throw new Error(
      "SYNC_SECRET script property is not set -- see setup notes at the top of this file",
    );
  if (secret !== expected) throw new Error("forbidden: bad sync secret");
}

// Verifies the ID token directly against Google (not just trusting the client) and
// checks it was issued for THIS app specifically, via the audience claim.
function verifyIdToken_(idToken) {
  if (!idToken) return null;
  var resp = UrlFetchApp.fetch(
    "https://oauth2.googleapis.com/tokeninfo?id_token=" +
      encodeURIComponent(idToken),
    { muteHttpExceptions: true },
  );
  if (resp.getResponseCode() !== 200) return null;
  var data = JSON.parse(resp.getContentText());
  var expectedClientId =
    PropertiesService.getScriptProperties().getProperty("OAUTH_CLIENT_ID");
  if (!expectedClientId)
    throw new Error(
      "OAUTH_CLIENT_ID script property is not set -- see setup notes at the top of this file",
    );
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
    sheet.appendRow([
      "example@gmail.com",
      "sample row -- replace with your team, then delete this",
    ]);
    sheet.setFrozenRows(1);
    return false; // just created -- nothing real to match against yet
  }
  var data = sheet.getDataRange().getValues();
  var header = data[0];
  var emailCol = header.indexOf("email");
  if (emailCol === -1) return false;
  for (var i = 1; i < data.length; i++) {
    if (String(data[i][emailCol]).trim().toLowerCase() === email.toLowerCase())
      return true;
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
      message:
        "This Google account (" +
        email +
        ") isn't set up yet. Ask an admin to add it to the Users tab.",
    };
  }
  var tabs = readAllTabs_();
  if (!tabs)
    return {
      ok: false,
      error: "no_data",
      message: "No data has been synced yet -- run update_bb_sheet.py.",
    };
  // Sends the raw tabs, NOT reconstructPayload_(tabs) -- rebuilding the full
  // nested shape (thousands of rows across 14 tabs, deep object/array
  // building) is real CPU work, and doing it here means every single
  // sign-in pays for it inside Apps Script's slower, quota-metered runtime
  // before the viewer sees anything at all. The browser does the identical
  // reconstruction (reconstructPayload_ ported verbatim into index.html)
  // in its own fast JS engine instead -- same output, much faster to a
  // signed-in viewer since Apps Script only ever does cheap sheet reads now.
  // getSyncData below still reconstructs server-side -- that path is
  // update_bb_sheet.py's local sync tool, not a viewer waiting on a page
  // load, so it was never the slow one.
  return { ok: true, email: email, tabs: tabs };
}

function readAllTabs_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var tabs = {};
  for (var i = 0; i < TAB_NAMES.length; i++) {
    var name = TAB_NAMES[i];
    var sheet = ss.getSheetByName(name);
    if (!sheet) return null; // no sync has ever run
    var values = sheet.getDataRange().getValues();
    tabs[name] = { header: values[0], rows: values.slice(1) };
  }
  return tabs;
}

// ---------- syncBbData: update_bb_sheet.py's write path ----------

function syncBbData_(tabs) {
  // One-time cleanup: an earlier version of this backend stored everything
  // as one chunked JSON blob in a "Data" tab. Remove it so the Sheet doesn't
  // end up with both that and the normalized tabs below -- harmless no-op
  // once it's gone (and on every later chunk, once it's already gone).
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var stale = ss.getSheetByName("Data");
  if (stale) ss.deleteSheet(stale);

  // Writes whatever tabs are IN this call, not all of TAB_NAMES -- the
  // client sends the full set across several smaller requests (one big
  // ~3MB POST came back as a bare Google error page, rejected before
  // doPost even ran, rather than a JSON error from our own code), so any
  // single call only ever carries a subset. Still validated against
  // TAB_NAMES so a typo'd tab name fails loudly instead of silently
  // writing a stray sheet.
  var names = Object.keys(tabs);
  var totalRows = 0;
  for (var i = 0; i < names.length; i++) {
    var name = names[i];
    if (TAB_NAMES.indexOf(name) === -1) throw new Error("unknown tab in payload: " + name);
    var t = tabs[name];
    writeTab_(name, t.header, t.rows, !!t.append);
    totalRows += t.rows.length;
  }
  return { tabs: names.length, rows: totalRows };
}

// append=true adds `rows` after whatever's already in the sheet instead of
// replacing it -- a tab too big for one request (BuildingsBreakdown, ~1.7MB
// already) arrives as several syncBbData_ calls in sequence; every call
// after the first for that tab must not re-clear it, or only the LAST
// piece would survive. See chunk_tabs() in update_bb_sheet.py, which is the
// only thing that ever sets append:true.
function writeTab_(name, header, rows, append) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName(name);
  if (!sheet) sheet = ss.insertSheet(name);

  if (append) {
    if (!rows.length) return;
    var startRow = sheet.getLastRow() + 1;
    var appendRange = sheet.getRange(startRow, 1, rows.length, header.length);
    header.forEach(function (colName, idx) {
      var fmt = TEXT_COLUMNS.indexOf(colName) !== -1 ? "@" : "0.####";
      sheet.getRange(startRow, idx + 1, rows.length, 1).setNumberFormat(fmt);
    });
    appendRange.setValues(rows);
    return;
  }

  sheet.clearContents();
  // clearContents() only wipes VALUES, not per-cell formatting -- if the
  // schema's column order ever changes (as it did when "label" was removed
  // from Months), a later column can silently inherit a stale "Date" format
  // left over from whatever column USED to occupy that position, and a
  // plain number written there gets reinterpreted as a date serial value on
  // read (this actually happened: "installs" landed in "mtime"'s old slot
  // and came back as a 1902 timestamp instead of 897). clearFormats() resets
  // every cell to "Automatic" first -- but Automatic still means "Sheets
  // guesses from context," and that guess demonstrably still misfired here
  // even post-reset. So every column below gets an EXPLICIT format instead
  // of being left to Automatic: "@" (plain text) for TEXT_COLUMNS, "0.####"
  // (plain number) for everything else -- no ambiguity left for Sheets to
  // guess wrong about, regardless of what any earlier schema version's
  // column layout happened to leave behind.
  sheet.clearFormats();

  var allRows = [header].concat(rows);
  if (!allRows.length || !header.length) {
    sheet.setFrozenRows(1);
    return;
  }
  var range = sheet.getRange(1, 1, allRows.length, header.length);
  header.forEach(function (colName, idx) {
    var fmt = TEXT_COLUMNS.indexOf(colName) !== -1 ? "@" : "0.####";
    sheet.getRange(1, idx + 1, allRows.length, 1).setNumberFormat(fmt);
  });
  range.setValues(allRows);
  sheet.setFrozenRows(1);
  sheet.autoResizeColumns(1, header.length);
}

// ---------- reconstructPayload_: hand-ported mirror of sheet_schema.py's
// reconstruct(). Rebuilds the exact nested shape aggregate_bb.py's
// build_output() produces, from the flat tabs above, so index.html never
// needs to know the storage is normalized tabs rather than one blob. ----------

function rowObjects_(tab) {
  return tab.rows.map(function (row) {
    var obj = {};
    tab.header.forEach(function (h, i) {
      obj[h] = row[i];
    });
    return obj;
  });
}

function reconstructPayload_(tabs) {
  var meta = rowObjects_(tabs.Meta)[0];
  // bd* columns are BuildingsDaily plumbing, not part of the real `meta`
  // object build_output() produces -- pull them out before `meta` becomes
  // out.meta below, mirroring sheet_schema.py's reconstruct().
  var bdMonthKey = meta.bdMonthKey,
    bdDays = meta.bdDays,
    bdElapsedDays = meta.bdElapsedDays,
    bdPartial = meta.bdPartial;
  delete meta.bdMonthKey;
  delete meta.bdDays;
  delete meta.bdElapsedDays;
  delete meta.bdPartial;

  var monthsByKey = {};
  var monthsOrder = [];
  rowObjects_(tabs.Months).forEach(function (r) {
    var m = {
      key: r.key,
      label: labelFromKey_(r.key),
      short: r.short,
      days: r.days,
      elapsedDays: r.elapsedDays,
      partial: r.partial,
      file: r.file,
      mtime: r.mtime,
      installs: r.installs,
      dupes: r.dupes,
      otherStatus: r.otherStatus,
      nonKpiConnect: r.nonKpiConnect,
      totals: { ga: r.ga, revenue: r.revenue, target: r.target },
      installsReg: r.installsReg,
      totalsReg: { ga: r.gaReg, revenue: r.revenueReg, target: 0 },
      daily: [],
      dailyReg: [],
      installsRegByStatus: {},
      totalsRegByStatus: {},
      dailyRegByStatus: {},
    };
    STATUS_ORDER.forEach(function (s) {
      var key = s.replace("-", "");
      m.installsRegByStatus[s] = r["reg_" + key];
      m.totalsRegByStatus[s] = {
        ga: r["reg_" + key],
        revenue: r["regRev_" + key],
        target: 0,
      };
      m.dailyRegByStatus[s] = [];
    });
    // installsRegByStatus (fixed, canonical labels) already carries the same
    // information registerByStatus (raw SUBS_STATUS strings) used to -- this
    // just renames it back for index.html's hero-note funnel text. The one
    // difference: an "Other" bucket here can't be un-lumped back into
    // whatever oddball raw status string(s) actually produced it.
    m.registerByStatus = {};
    STATUS_ORDER.forEach(function (s) {
      if (m.installsRegByStatus[s])
        m.registerByStatus[s] = m.installsRegByStatus[s];
    });
    monthsByKey[r.key] = m;
    monthsOrder.push(r.key);
  });

  rowObjects_(tabs.Daily).forEach(function (r) {
    var d = { day: r.day, ga: r.ga, revenue: r.revenue };
    if (r.mode === "connect") monthsByKey[r.month].daily.push(d);
    else if (r.mode === "register") monthsByKey[r.month].dailyReg.push(d);
    else monthsByKey[r.month].dailyRegByStatus[r.mode.split(":")[1]].push(d);
  });
  // Zero-activity days were skipped when flattening -- fill them back in so
  // every day 1..days is present, as the frontend expects.
  function padDays(entries, days) {
    var byDay = {};
    entries.forEach(function (d) {
      byDay[d.day] = d;
    });
    var out = [];
    for (var d = 1; d <= days; d++)
      out.push(byDay[d] || { day: d, ga: 0, revenue: 0 });
    return out;
  }
  monthsOrder.forEach(function (mk) {
    var m = monthsByKey[mk];
    m.daily = padDays(m.daily, m.days);
    m.dailyReg = padDays(m.dailyReg, m.days);
    STATUS_ORDER.forEach(function (s) {
      m.dailyRegByStatus[s] = padDays(m.dailyRegByStatus[s], m.days);
    });
  });
  var months = monthsOrder.map(function (mk) {
    return monthsByKey[mk];
  });

  // ---- breakdown dimensions (District/Channel/SubChannel/DealerTerritory/Supervisor) ----
  function rowsToListify(rows, withImport, withTarget) {
    var byName = {},
      order = [];
    rows.forEach(function (r) {
      if (!(r.name in byName)) {
        byName[r.name] = { name: r.name, m: {} };
        order.push(r.name);
      }
      // compact() (aggregate_bb.py) drops zero-ga months from "m" entirely --
      // a row that only exists to carry a target for an otherwise-zero month
      // must not resurrect a fake {g:0} entry there.
      if (r.ga) byName[r.name].m[r.month] = { g: r.ga, r: r.revenue };
      if (withImport && r.gaImport !== "") {
        if (!byName[r.name].mi) byName[r.name].mi = {};
        byName[r.name].mi[r.month] = { g: r.gaImport, r: r.revenueImport };
      }
      if (withTarget && r.target !== "") {
        if (!byName[r.name].t) byName[r.name].t = {};
        byName[r.name].t[r.month] = r.target;
      }
    });
    var result = order.map(function (n) {
      return byName[n];
    });
    result.sort(function (a, b) {
      var sa = 0,
        sb = 0;
      Object.keys(a.m).forEach(function (k) {
        sa += a.m[k].g;
      });
      Object.keys(b.m).forEach(function (k) {
        sb += b.m[k].g;
      });
      return sb - sa;
    });
    return result;
  }

  var DIMS = [
    ["district", "District", true, false],
    ["channel", "Channel", true, false],
    ["subChannel", "SubChannel", true, true],
    ["dealerTerritory", "DealerTerritory", false, false],
    ["supervisor", "Supervisor", false, false],
  ];

  function rowsForMode(tabName, mode, withImport, withTarget) {
    var rows = rowObjects_(tabs[tabName]).filter(function (r) {
      return r.mode === mode;
    });
    return rowsToListify(rows, withImport, withTarget);
  }

  function technologyForMode(mode) {
    var out = {};
    rowObjects_(tabs.Technology).forEach(function (r) {
      if (r.mode !== mode) return;
      if (!out[r.name]) out[r.name] = {};
      out[r.name][r.month] = r.ga;
    });
    return out;
  }

  // ---- buildings / villages ----
  function villagesForMode(mode) {
    var byKey = {},
      order = [];
    rowObjects_(tabs.Buildings).forEach(function (r) {
      if (r.mode !== mode) return;
      var key = r.category + "|" + r.name;
      if (!byKey[key]) {
        var entry = {
          name: r.name,
          m: {},
          district: r.district,
          bid: r.bid || null,
        };
        if (r.category === "matched") {
          entry.activeFtth = r.activeFtth;
          if (r.lat !== "" && r.lng !== "") {
            entry.lat = r.lat;
            entry.lng = r.lng;
          }
        }
        byKey[key] = entry;
        order.push({ key: key, category: r.category });
      }
      byKey[key].m[r.month] = { g: r.ga, r: r.revenue };
      if (r.gaImport !== "") {
        if (!byKey[key].mi) byKey[key].mi = {};
        byKey[key].mi[r.month] = { g: r.gaImport, r: r.revenueImport };
      }
    });

    var leavesByBuilding = {};
    rowObjects_(tabs.BuildingsBreakdown).forEach(function (r) {
      if (r.mode !== mode) return;
      if (!leavesByBuilding[r.building]) leavesByBuilding[r.building] = [];
      leavesByBuilding[r.building].push(r);
    });

    var matched = [],
      bbOnly = [];
    order.forEach(function (o) {
      var entry = byKey[o.key];
      entry.breakdown = rebuildBreakdownTree_(
        leavesByBuilding[entry.name] || [],
      );
      (o.category === "matched" ? matched : bbOnly).push(entry);
    });
    matched.sort(function (a, b) {
      return b.activeFtth - a.activeFtth;
    });
    bbOnly.sort(function (a, b) {
      var sa = 0,
        sb = 0;
      Object.keys(a.m).forEach(function (k) {
        sa += a.m[k].g;
      });
      Object.keys(b.m).forEach(function (k) {
        sb += b.m[k].g;
      });
      return sb - sa;
    });

    var ftthOnly = rowObjects_(tabs.VillagesFtthOnly)
      .filter(function (r) {
        return r.mode === mode;
      })
      .map(function (r) {
        return { name: r.name, activeFtth: r.activeFtth };
      });
    ftthOnly.sort(function (a, b) {
      return b.activeFtth - a.activeFtth;
    });

    return { matched: matched, ftthOnly: ftthOnly, bbOnly: bbOnly };
  }

  function rebuildBreakdownTree_(leafRows) {
    // Groups (channel, specialChannel, territory, partner, month) leaf facts
    // back into the 4-level {name, m, children:[...]} tree, summing rollups
    // at each level -- the exact inverse of the flattening, matching
    // aggregate_bb.py's build_breakdown().
    var root = {};
    leafRows.forEach(function (r) {
      root[r.channel] = root[r.channel] || {};
      root[r.channel][r.specialChannel] =
        root[r.channel][r.specialChannel] || {};
      root[r.channel][r.specialChannel][r.territory] =
        root[r.channel][r.specialChannel][r.territory] || {};
      root[r.channel][r.specialChannel][r.territory][r.partner] =
        root[r.channel][r.specialChannel][r.territory][r.partner] || {};
      var bucket = root[r.channel][r.specialChannel][r.territory][r.partner];
      bucket[r.month] = bucket[r.month] || { g: 0, r: 0 };
      bucket[r.month].g += r.ga;
      bucket[r.month].r += r.revenue;
    });

    function rollup(d, depth) {
      var items = [];
      Object.keys(d).forEach(function (name) {
        var val = d[name];
        if (depth === 3) {
          var m = {};
          var any = false;
          Object.keys(val).forEach(function (mk) {
            if (val[mk].g) {
              m[mk] = { g: val[mk].g, r: Math.round(val[mk].r * 100) / 100 };
              any = true;
            }
          });
          if (any) items.push({ name: name, m: m });
        } else {
          var kids = rollup(val, depth + 1);
          if (!kids.length) return;
          var agg = {};
          kids.forEach(function (k) {
            Object.keys(k.m).forEach(function (mk) {
              agg[mk] = agg[mk] || { g: 0, r: 0 };
              agg[mk].g += k.m[mk].g;
              agg[mk].r += k.m[mk].r;
            });
          });
          var m2 = {};
          Object.keys(agg).forEach(function (mk) {
            m2[mk] = { g: agg[mk].g, r: Math.round(agg[mk].r * 100) / 100 };
          });
          items.push({ name: name, m: m2, children: kids });
        }
      });
      items.sort(function (a, b) {
        var sa = 0,
          sb = 0;
        Object.keys(a.m).forEach(function (k) {
          sa += a.m[k].g;
        });
        Object.keys(b.m).forEach(function (k) {
          sb += b.m[k].g;
        });
        return sb - sa;
      });
      return items;
    }

    return rollup(root, 0);
  }

  function modeBlock(mode, withTargets) {
    var block = {};
    DIMS.forEach(function (d) {
      var key = d[0],
        tabName = d[1],
        withImport = d[2],
        withTarget = d[3];
      block[key] = rowsForMode(
        tabName,
        mode,
        withImport,
        withTarget && withTargets,
      );
    });
    block.supervisor = block.supervisor.slice(0, 20);
    block.technology = technologyForMode(mode);
    block.villages = villagesForMode(mode);
    return block;
  }

  var connectBlock = modeBlock("connect", true);
  var registerBlock = modeBlock("register", false);
  var presentStatuses = {};
  rowObjects_(tabs.District).forEach(function (r) {
    if (r.mode.indexOf("register:") === 0)
      presentStatuses[r.mode.split(":")[1]] = true;
  });
  var byStatus = {};
  Object.keys(presentStatuses)
    .sort()
    .forEach(function (status) {
      byStatus[status] = modeBlock("register:" + status, false);
    });
  registerBlock.byStatus = byStatus;

  // ---- buildings-by-day (current month, Connect only) ----
  function rebuildBreakdownTreeSimple_(leafRows) {
    // Same as rebuildBreakdownTree_, for BuildingsDailyBreakdown's leaner row
    // shape (no mode/building columns -- caller already grouped by building,
    // and there's only ever one mode: Connect) and day instead of month.
    var root = {};
    leafRows.forEach(function (r) {
      root[r.channel] = root[r.channel] || {};
      root[r.channel][r.specialChannel] =
        root[r.channel][r.specialChannel] || {};
      root[r.channel][r.specialChannel][r.territory] =
        root[r.channel][r.specialChannel][r.territory] || {};
      root[r.channel][r.specialChannel][r.territory][r.partner] =
        root[r.channel][r.specialChannel][r.territory][r.partner] || {};
      var bucket = root[r.channel][r.specialChannel][r.territory][r.partner];
      bucket[r.day] = bucket[r.day] || { g: 0, r: 0 };
      bucket[r.day].g += r.ga;
      bucket[r.day].r += r.revenue;
    });

    function rollup(d, depth) {
      var items = [];
      Object.keys(d).forEach(function (name) {
        var val = d[name];
        if (depth === 3) {
          var m = {};
          var any = false;
          Object.keys(val).forEach(function (dk) {
            if (val[dk].g) {
              m[dk] = { g: val[dk].g, r: Math.round(val[dk].r * 100) / 100 };
              any = true;
            }
          });
          if (any) items.push({ name: name, m: m });
        } else {
          var kids = rollup(val, depth + 1);
          if (!kids.length) return;
          var agg = {};
          kids.forEach(function (k) {
            Object.keys(k.m).forEach(function (dk) {
              agg[dk] = agg[dk] || { g: 0, r: 0 };
              agg[dk].g += k.m[dk].g;
              agg[dk].r += k.m[dk].r;
            });
          });
          var m2 = {};
          Object.keys(agg).forEach(function (dk) {
            m2[dk] = { g: agg[dk].g, r: Math.round(agg[dk].r * 100) / 100 };
          });
          items.push({ name: name, m: m2, children: kids });
        }
      });
      items.sort(function (a, b) {
        var sa = 0,
          sb = 0;
        Object.keys(a.m).forEach(function (k) {
          sa += a.m[k].g;
        });
        Object.keys(b.m).forEach(function (k) {
          sb += b.m[k].g;
        });
        return sb - sa;
      });
      return items;
    }

    return rollup(root, 0);
  }

  function buildingsDailyFromTabs_() {
    var byName = {},
      order = [];
    rowObjects_(tabs.BuildingsDaily).forEach(function (r) {
      if (!byName[r.building]) {
        byName[r.building] = {
          name: r.building,
          m: {},
          district: r.district,
          bid: r.bid || null,
        };
        order.push(r.building);
      }
      byName[r.building].m[r.day] = { g: r.ga, r: r.revenue };
    });
    var leavesByBuilding = {};
    rowObjects_(tabs.BuildingsDailyBreakdown).forEach(function (r) {
      if (!leavesByBuilding[r.building]) leavesByBuilding[r.building] = [];
      leavesByBuilding[r.building].push(r);
    });
    var rows = order.map(function (name) {
      var entry = byName[name];
      entry.breakdown = rebuildBreakdownTreeSimple_(
        leavesByBuilding[name] || [],
      );
      return entry;
    });
    rows.sort(function (a, b) {
      var sa = 0,
        sb = 0;
      Object.keys(a.m).forEach(function (k) {
        sa += a.m[k].g;
      });
      Object.keys(b.m).forEach(function (k) {
        sb += b.m[k].g;
      });
      return sb - sa;
    });
    return {
      monthKey: bdMonthKey,
      label: labelFromKey_(bdMonthKey),
      days: bdDays,
      elapsedDays: bdElapsedDays,
      partial: bdPartial,
      rows: rows,
    };
  }

  var out = { meta: meta, months: months, register: registerBlock };
  Object.keys(connectBlock).forEach(function (k) {
    out[k] = connectBlock[k];
  });
  out.buildingsDaily = buildingsDailyFromTabs_();
  return out;
}

// ---------- shared ----------

function jsonResponse_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(
    ContentService.MimeType.JSON,
  );
}
