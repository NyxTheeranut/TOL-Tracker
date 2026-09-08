"""
The normalized, human-readable Sheet schema for the TOL Install Tracker --
real columns instead of a chunked JSON blob, matching how PakKret Territory
Explorer / Route Planner store their data.

flatten(out) turns aggregate_bb.build_output()'s nested dict into a set of
flat tables (one row per fact), suitable for writing to Sheet tabs.
reconstruct(tabs) does the exact inverse, rebuilding the same nested shape
index.html already expects -- so the frontend never needs to know the
storage changed. This module is the single source of truth for that
round-trip; both update_bb_sheet.py (flatten, for writing) and this file's
own self-test (both directions, for verification) use it. The Apps Script
side is a manual JS port of reconstruct() -- see Sheets Sync - Apps Script
Code.gs's readAllTabs_ -- because Apps Script can't import Python.

── Tabs ─────────────────────────────────────────────────────────────────────
Meta              one row of top-level metadata.
Months            one row per month, every scalar metric as its own column
                  (Connect totals, Register totals, Register-by-status
                  totals). Daily figures live in Daily instead -- a month
                  row would otherwise need 30+ day columns.
Daily             one row per (month, day, mode) -- mode is "connect",
                  "register", or "register:<Status>".
District, Channel, SubChannel, DealerTerritory, Supervisor, Technology
                  one row per (mode, name, month) breakdown fact. Same six
                  columns in every one of these tabs (gaImport/target blank
                  where that dimension doesn't carry one) so the read side
                  can share one function across all six.
Buildings         one row per (mode, category, name, month) -- category is
                  "matched" (has an Active FTTH census match) or "bbOnly".
VillagesFtthOnly  one row per (mode, name) -- villages with zero installs
                  in every month, so no month dimension to break out.
BuildingsBreakdown  one row per (mode, building, channel, specialChannel,
                  territory, partner, month) -- the LEAF facts of each
                  building's drill-down tree. Deliberately leaves out the
                  rollup subtotals at each level (channel-only,
                  channel+special, ...) since those are just sums of the
                  leaves under them -- reconstruct() rebuilds them the same
                  way build_breakdown() does in aggregate_bb.py, so storing
                  them too would be redundant, bigger, and a second place
                  they could drift out of sync with the leaves.
"""
from collections import defaultdict

STATUS_ORDER = ["Connect", "Pending", "Cancel", "Un-Complete", "Other"]


def _mode_key(suffix_label):
    """"" -> "connect", "_reg" -> "register", a status label -> "register:<label>"."""
    if suffix_label == "":
        return "connect"
    if suffix_label == "_reg":
        return "register"
    return "register:" + suffix_label


def _flatten_breakdown_rows(rows, mode, out_rows, with_import=False, with_target=False):
    for r in rows:
        name = r["name"]
        mi = r.get("mi") or {}
        t = r.get("t") or {}
        # A sub-channel can carry a target for a month with zero installs --
        # that month then has no entry in "m" at all, so the month set to
        # visit is the union, not just r["m"].keys().
        months = set(r["m"].keys()) | (set(t.keys()) if with_target else set())
        for mk in months:
            v = r["m"].get(mk, {"g": 0, "r": 0})
            imp = mi.get(mk) or {}
            out_rows.append([
                mode, name, mk, v["g"], v["r"],
                imp.get("g", "") if with_import else "",
                imp.get("r", "") if with_import else "",
                t.get(mk, "") if with_target else "",
            ])


def _flatten_leaf_breakdown(tree, mode, building, out_rows):
    """Walks one building's 4-level channel->special->territory->partner tree,
    emitting only the depth-3 leaves (which carry real per-month facts, not
    rollup sums)."""
    for channel_node in tree:
        for special_node in channel_node.get("children", []):
            for territory_node in special_node.get("children", []):
                for partner_node in territory_node.get("children", []):
                    for mk, v in partner_node["m"].items():
                        out_rows.append([
                            mode, building, channel_node["name"], special_node["name"],
                            territory_node["name"], partner_node["name"], mk, v["g"], v["r"],
                        ])


def _flatten_villages(village_block, mode, out_rows, ftth_rows):
    for category in ("matched", "bbOnly"):
        for r in village_block[category]:
            for mk, v in r["m"].items():
                out_rows.append([
                    mode, category, r["name"], mk, v["g"], v["r"],
                    r.get("activeFtth", ""), r.get("district", ""), r.get("bid") or "",
                ])
    for r in village_block["ftthOnly"]:
        ftth_rows.append([mode, r["name"], r["activeFtth"]])


def flatten(out):
    tabs = {}

    meta = out["meta"]
    tabs["Meta"] = {
        "header": ["province", "ftthSourceFile", "ftthSourceMTime", "ftthRowCount", "generatedAt",
                   "totalActiveFtth", "totalVillages", "totalBuildings", "matchedCount", "matchRateVillages"],
        "rows": [[meta["province"], meta["ftthSourceFile"], meta["ftthSourceMTime"], meta["ftthRowCount"],
                  meta["generatedAt"], meta["totalActiveFtth"], meta["totalVillages"], meta["totalBuildings"],
                  meta["matchedCount"], meta["matchRateVillages"]]],
    }

    months_header = ["key", "label", "short", "days", "elapsedDays", "partial", "file", "mtime",
                      "installs", "dupes", "otherStatus", "nonKpiConnect", "ga", "revenue", "target",
                      "installsReg", "gaReg", "revenueReg"]
    for s in STATUS_ORDER:
        months_header += ["reg_" + s.replace("-", ""), "regRev_" + s.replace("-", "")]
    months_rows = []
    daily_rows = []
    for m in out["months"]:
        row = [m["key"], m["label"], m["short"], m["days"], m["elapsedDays"], m["partial"], m["file"], m["mtime"],
               m["installs"], m["dupes"], m["otherStatus"], m["nonKpiConnect"],
               m["totals"]["ga"], m["totals"]["revenue"], m["totals"]["target"],
               m["installsReg"], m["totalsReg"]["ga"], m["totalsReg"]["revenue"]]
        for s in STATUS_ORDER:
            t = m["totalsRegByStatus"].get(s, {"ga": 0, "revenue": 0})
            row += [t["ga"], t["revenue"]]
        months_rows.append(row)

        # Zero-activity days are skipped here (a fact table shouldn't carry rows
        # for facts that didn't happen) -- reconstruct() pads every day of the
        # month back in using out["months"][i]["days"], since the frontend
        # expects one entry per calendar day whether or not anything happened.
        for d in m["daily"]:
            if d["ga"]:
                daily_rows.append([m["key"], d["day"], "connect", d["ga"], d["revenue"]])
        for d in m["dailyReg"]:
            if d["ga"]:
                daily_rows.append([m["key"], d["day"], "register", d["ga"], d["revenue"]])
        for s in STATUS_ORDER:
            for d in m["dailyRegByStatus"].get(s, []):
                if d["ga"]:
                    daily_rows.append([m["key"], d["day"], "register:" + s, d["ga"], d["revenue"]])
    tabs["Months"] = {"header": months_header, "rows": months_rows}
    tabs["Daily"] = {"header": ["month", "day", "mode", "ga", "revenue"], "rows": daily_rows}

    breakdown_header = ["mode", "name", "month", "ga", "revenue", "gaImport", "revenueImport", "target"]
    # with_import/with_target must match exactly what build_mode_block() in
    # aggregate_bb.py passes to listify() for each key -- district/channel/
    # subChannel carry an import overlay, dealerTerritory/supervisor don't
    # (territory IS the import dimension; supervisor's import store is
    # accumulated but never surfaced in the JSON output either).
    dims = [
        ("district", "District", True, False),
        ("channel", "Channel", True, False),
        ("subChannel", "SubChannel", True, True),
        ("dealerTerritory", "DealerTerritory", False, False),
        ("supervisor", "Supervisor", False, False),
        ("technology", None, False, False),  # handled separately -- flat count dict, not listify() rows
    ]
    for key, tab_name, with_import, with_target in dims:
        if tab_name is None:
            continue
        rows = []
        _flatten_breakdown_rows(out[key], "connect", rows, with_import, with_target)
        _flatten_breakdown_rows(out["register"][key], "register", rows, with_import, False)
        for status, block in out["register"]["byStatus"].items():
            _flatten_breakdown_rows(block[key], "register:" + status, rows, with_import, False)
        tabs[tab_name] = {"header": breakdown_header, "rows": rows}

    tech_rows = []
    for mode, tech_dict in (("connect", out["technology"]), ("register", out["register"]["technology"])):
        for tech, by_month in tech_dict.items():
            for mk, count in by_month.items():
                tech_rows.append([mode, tech, mk, count, "", "", "", ""])
    for status, block in out["register"]["byStatus"].items():
        for tech, by_month in block["technology"].items():
            for mk, count in by_month.items():
                tech_rows.append(["register:" + status, tech, mk, count, "", "", "", ""])
    tabs["Technology"] = {"header": breakdown_header, "rows": tech_rows}

    bld_rows, ftth_rows, leaf_rows = [], [], []
    _flatten_villages(out["villages"], "connect", bld_rows, ftth_rows)
    for name in out["villages"]["matched"] + out["villages"]["bbOnly"]:
        _flatten_leaf_breakdown(name["breakdown"], "connect", name["name"], leaf_rows)
    _flatten_villages(out["register"]["villages"], "register", bld_rows, ftth_rows)
    for name in out["register"]["villages"]["matched"] + out["register"]["villages"]["bbOnly"]:
        _flatten_leaf_breakdown(name["breakdown"], "register", name["name"], leaf_rows)
    for status, block in out["register"]["byStatus"].items():
        _flatten_villages(block["villages"], "register:" + status, bld_rows, ftth_rows)
        for name in block["villages"]["matched"] + block["villages"]["bbOnly"]:
            _flatten_leaf_breakdown(name["breakdown"], "register:" + status, name["name"], leaf_rows)

    tabs["Buildings"] = {
        "header": ["mode", "category", "name", "month", "ga", "revenue", "activeFtth", "district", "bid"],
        "rows": bld_rows,
    }
    tabs["VillagesFtthOnly"] = {"header": ["mode", "name", "activeFtth"], "rows": ftth_rows}
    tabs["BuildingsBreakdown"] = {
        "header": ["mode", "building", "channel", "specialChannel", "territory", "partner", "month", "ga", "revenue"],
        "rows": leaf_rows,
    }
    return tabs


def _rows_to_listify(rows, with_import, with_target):
    """Inverse of _flatten_breakdown_rows for one mode's slice of rows."""
    by_name = {}
    order = []
    for mode, name, mk, ga, revenue, ga_import, revenue_import, target in rows:
        if name not in by_name:
            by_name[name] = {"name": name, "m": {}}
            order.append(name)
        # compact() (aggregate_bb.py) drops zero-ga months from "m" entirely --
        # a row that only exists to carry a target for an otherwise-zero month
        # must not resurrect a fake {g:0} entry there.
        if ga:
            by_name[name]["m"][mk] = {"g": ga, "r": revenue}
        if with_import and ga_import != "":
            by_name[name].setdefault("mi", {})[mk] = {"g": ga_import, "r": revenue_import}
        if with_target and target != "":
            by_name[name].setdefault("t", {})[mk] = target
    result = [by_name[n] for n in order]
    result.sort(key=lambda r: -sum(v["g"] for v in r["m"].values()))
    return result


def _rebuild_breakdown_tree(leaf_rows):
    """Inverse of _flatten_leaf_breakdown -- groups leaf facts back into the
    4-level {name, m, children:[...]} tree, summing rollups at each level
    exactly the way aggregate_bb.py's build_breakdown() does."""
    root = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(
        lambda: {"g": 0, "r": 0.0}
    )))))
    for _mode, _building, channel, special, territory, partner, mk, ga, revenue in leaf_rows:
        b = root[channel][special][territory][partner][mk]
        b["g"] += ga
        b["r"] += revenue

    def rollup(d, depth):
        items = []
        for name, val in d.items():
            if depth == 3:
                m = {mk: {"g": b["g"], "r": round(b["r"], 2)} for mk, b in val.items() if b["g"]}
                if m:
                    items.append({"name": name, "m": m})
            else:
                kids = rollup(val, depth + 1)
                if not kids:
                    continue
                agg = defaultdict(lambda: {"g": 0, "r": 0.0})
                for k in kids:
                    for mk, e in k["m"].items():
                        agg[mk]["g"] += e["g"]
                        agg[mk]["r"] += e["r"]
                items.append({"name": name, "m": {mk: {"g": v["g"], "r": round(v["r"], 2)} for mk, v in agg.items()}, "children": kids})
        items.sort(key=lambda x: -sum(v["g"] for v in x["m"].values()))
        return items

    return rollup(root, 0)


def reconstruct(tabs):
    """Rebuilds aggregate_bb.build_output()'s exact nested shape from flat
    tabs -- the inverse of flatten(). Used here to self-test the schema
    losslessly captures everything; ported to JS in the Apps Script for the
    real read path (Apps Script can't import this module directly)."""
    meta_row = dict(zip(tabs["Meta"]["header"], tabs["Meta"]["rows"][0]))

    months_by_key = {}
    months_order = []
    for row in tabs["Months"]["rows"]:
        r = dict(zip(tabs["Months"]["header"], row))
        m = {
            "key": r["key"], "label": r["label"], "short": r["short"], "days": r["days"],
            "elapsedDays": r["elapsedDays"], "partial": r["partial"], "file": r["file"], "mtime": r["mtime"],
            "installs": r["installs"], "dupes": r["dupes"], "otherStatus": r["otherStatus"], "nonKpiConnect": r["nonKpiConnect"],
            "totals": {"ga": r["ga"], "revenue": r["revenue"], "target": r["target"]},
            "installsReg": r["installsReg"],
            "totalsReg": {"ga": r["gaReg"], "revenue": r["revenueReg"], "target": 0},
            "daily": [], "dailyReg": [],
            "installsRegByStatus": {}, "totalsRegByStatus": {}, "dailyRegByStatus": {s: [] for s in STATUS_ORDER},
        }
        for s in STATUS_ORDER:
            key = s.replace("-", "")
            m["installsRegByStatus"][s] = r["reg_" + key]
            m["totalsRegByStatus"][s] = {"ga": r["reg_" + key], "revenue": r["regRev_" + key], "target": 0}
        # registerByStatus isn't stored separately -- installsRegByStatus (fixed,
        # canonical labels) already carries the same information; this just
        # renames it back for index.html's hero-note funnel text. The one
        # difference: an "Other" bucket here can't be un-lumped back into
        # whatever oddball raw SUBS_STATUS string(s) actually produced it.
        m["registerByStatus"] = {s: n for s, n in m["installsRegByStatus"].items() if n}
        months_by_key[r["key"]] = m
        months_order.append(r["key"])

    for row in tabs["Daily"]["rows"]:
        mk, day, mode, ga, revenue = row
        d = {"day": day, "ga": ga, "revenue": revenue}
        if mode == "connect":
            months_by_key[mk]["daily"].append(d)
        elif mode == "register":
            months_by_key[mk]["dailyReg"].append(d)
        else:
            status = mode.split(":", 1)[1]
            months_by_key[mk]["dailyRegByStatus"][status].append(d)
    def pad_days(entries, days):
        """Zero-activity days were skipped when flattening (see flatten()) --
        fill them back in so every day 1..days is present, as the frontend
        expects (m.daily.forEach etc. assume a complete day range)."""
        by_day = {d["day"]: d for d in entries}
        return [by_day.get(d, {"day": d, "ga": 0, "revenue": 0.0}) for d in range(1, days + 1)]

    for mk in months_order:
        m = months_by_key[mk]
        m["daily"] = pad_days(m["daily"], m["days"])
        m["dailyReg"] = pad_days(m["dailyReg"], m["days"])
        for s in STATUS_ORDER:
            m["dailyRegByStatus"][s] = pad_days(m["dailyRegByStatus"][s], m["days"])
    months = [months_by_key[mk] for mk in months_order]

    def rows_for_mode(tab_name, mode, with_import, with_target):
        rows = [r for r in tabs[tab_name]["rows"] if r[0] == mode]
        return _rows_to_listify([r for r in rows], with_import, with_target)

    dims = [
        ("district", "District", True, False),
        ("channel", "Channel", True, False),
        ("subChannel", "SubChannel", True, True),
        ("dealerTerritory", "DealerTerritory", False, False),
        ("supervisor", "Supervisor", False, False),
    ]

    def technology_for_mode(mode):
        out = defaultdict(dict)
        for row in tabs["Technology"]["rows"]:
            if row[0] != mode:
                continue
            _, tech, mk, count, *_ = row
            out[tech][mk] = count
        return dict(out)

    def villages_for_mode(mode):
        matched, bb_only = [], []
        by_key = {}
        order = []
        for row in tabs["Buildings"]["rows"]:
            m, category, name, mk, ga, revenue, active_ftth, district, bid = row
            if m != mode:
                continue
            key = (category, name)
            if key not in by_key:
                entry = {"name": name, "m": {}, "district": district, "bid": bid or None}
                if category == "matched":
                    entry["activeFtth"] = active_ftth
                by_key[key] = entry
                order.append(key)
            by_key[key]["m"][mk] = {"g": ga, "r": revenue}
        leaves_by_building = defaultdict(list)
        for row in tabs["BuildingsBreakdown"]["rows"]:
            if row[0] != mode:
                continue
            leaves_by_building[row[1]].append(row)
        for key in order:
            category, name = key
            entry = by_key[key]
            entry["breakdown"] = _rebuild_breakdown_tree(leaves_by_building.get(name, []))
            (matched if category == "matched" else bb_only).append(entry)
        matched.sort(key=lambda r: -r["activeFtth"])
        bb_only.sort(key=lambda r: -sum(v["g"] for v in r["m"].values()))
        ftth_only = [
            {"name": row[1], "activeFtth": row[2]}
            for row in tabs["VillagesFtthOnly"]["rows"] if row[0] == mode
        ]
        ftth_only.sort(key=lambda r: -r["activeFtth"])
        return {"matched": matched, "ftthOnly": ftth_only, "bbOnly": bb_only}

    def mode_block(mode, with_targets):
        block = {}
        for key, tab_name, with_import, with_target in dims:
            block[key] = rows_for_mode(tab_name, mode, with_import, with_target and with_targets)
        if mode != "connect":
            block["supervisor"] = block["supervisor"][:20]
        else:
            block["supervisor"] = block["supervisor"][:20]
        block["technology"] = technology_for_mode(mode)
        block["villages"] = villages_for_mode(mode)
        return block

    connect_block = mode_block("connect", with_targets=True)
    register_block = mode_block("register", with_targets=False)
    by_status = {}
    for row in tabs["Months"]["rows"]:
        pass  # status set is fixed (STATUS_ORDER); presence detected via Daily/Buildings/breakdown rows below
    present_statuses = sorted({
        row[0].split(":", 1)[1] for row in tabs["District"]["rows"] if row[0].startswith("register:")
    })
    for status in present_statuses:
        by_status[status] = mode_block("register:" + status, with_targets=False)
    register_block["byStatus"] = by_status

    out = {
        "meta": meta_row,
        "months": months,
        "register": register_block,
    }
    out.update(connect_block)
    return out
