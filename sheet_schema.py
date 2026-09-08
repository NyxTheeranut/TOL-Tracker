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
import calendar
from collections import defaultdict

STATUS_ORDER = ["Connect", "Pending", "Cancel", "Un-Complete", "Other"]


def label_from_key(key):
    """"202603" -> "March 2026". Not stored in the Months tab -- it's 100%
    derivable from key, and storing it hit a real Sheets gotcha: a
    human-readable string like "March 2026" gets silently auto-detected and
    converted to an actual date value on write (setNumberFormat("@") should
    prevent this and reliably did for every OTHER at-risk column, but not
    this one in practice), so reading it back handed a JS Date object
    instead of the string. Deriving it fresh on both sides sidesteps that
    entirely rather than continuing to fight Sheets' auto-formatting."""
    y, m = int(key[:4]), int(key[4:6])
    return f"{calendar.month_name[m]} {y}"


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

    months_header = ["key", "short", "days", "elapsedDays", "partial", "file", "mtime",
                      "installs", "dupes", "otherStatus", "nonKpiConnect", "ga", "revenue", "target",
                      "installsReg", "gaReg", "revenueReg"]
    for s in STATUS_ORDER:
        months_header += ["reg_" + s.replace("-", ""), "regRev_" + s.replace("-", "")]
    months_rows = []
    daily_rows = []
    for m in out["months"]:
        row = [m["key"], m["short"], m["days"], m["elapsedDays"], m["partial"], m["file"], m["mtime"],
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
            "key": r["key"], "label": label_from_key(r["key"]), "short": r["short"], "days": r["days"],
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


# ── merge_months ─────────────────────────────────────────────────────────────
# The Sheet is the only durable copy of history beyond MAX_MONTHS ago (raw
# TOL_*.txt exports live only on the one local machine, in TOL/Data/, never
# committed anywhere). A naive "flatten local build_output() and overwrite"
# sync would silently DELETE history whenever the local Data/ folder happens
# to have fewer months than usual (a fresh machine, an accidentally-cleared
# folder, ...) -- exactly the scenario that must never destroy data. So the
# sync script fetches what's already in the Sheet, merges in whatever fresh
# months it has locally, and pushes the union (capped to max_months) --
# months present locally always win (they're a fresh, presumably-better
# recomputation); months ONLY known to the Sheet are carried through
# untouched. A month can only ever be dropped by the max_months cap pushing
# it off the OLD end after genuinely newer months arrive, never by a
# temporarily-incomplete local folder.

def _merge_month_map(existing_m, new_m, new_months, final_months):
    """One entity's {month: value} map (value is {g,r} or a plain number --
    doesn't matter, this never inspects it). For each month in the final
    window: if local recomputed that month at all, its answer wins outright
    (including "absent" -- compact() already means zero, not "unknown");
    otherwise fall back to whatever the Sheet already had."""
    merged = {}
    for mk in final_months:
        if mk in new_months:
            if mk in new_m:
                merged[mk] = new_m[mk]
        elif mk in existing_m:
            merged[mk] = existing_m[mk]
    return merged


def _merge_entity_list(existing_list, new_list, new_months, final_months, with_import, with_target):
    existing_by_name = {r["name"]: r for r in existing_list}
    new_by_name = {r["name"]: r for r in new_list}
    names = set(existing_by_name) | set(new_by_name)

    result = []
    for name in names:
        e = existing_by_name.get(name, {})
        n = new_by_name.get(name, {})
        merged_m = _merge_month_map(e.get("m", {}), n.get("m", {}), new_months, final_months)
        if not merged_m:
            continue  # zero activity anywhere in the merged window -- drop, matching compact()
        row = {"name": name, "m": merged_m}
        if with_import:
            mi = _merge_month_map(e.get("mi", {}), n.get("mi", {}), new_months, final_months)
            if mi:
                row["mi"] = mi
        if with_target:
            t = _merge_month_map(e.get("t", {}), n.get("t", {}), new_months, final_months)
            if t:
                row["t"] = t
        result.append(row)
    result.sort(key=lambda r: -sum(v["g"] for v in r["m"].values()))
    return result


def _merge_technology(existing_tech, new_tech, new_months, final_months):
    names = set(existing_tech) | set(new_tech)
    result = {}
    for name in names:
        merged = _merge_month_map(existing_tech.get(name, {}), new_tech.get(name, {}), new_months, final_months)
        if merged:
            result[name] = merged
    return result


def _merge_villages(existing_v, new_v, new_months, final_months):
    def by_name(rows):
        return {r["name"]: r for r in rows}

    e_matched, n_matched = by_name(existing_v["matched"]), by_name(new_v["matched"])
    e_bbonly, n_bbonly = by_name(existing_v["bbOnly"]), by_name(new_v["bbOnly"])
    e_ftth, n_ftth = by_name(existing_v["ftthOnly"]), by_name(new_v["ftthOnly"])

    # "matched" (has an FTTH census entry) vs "bbOnly" is a static identity
    # fact about the building, not month-dependent -- if either run ever saw
    # it as matched, it's matched. Every census village that's been seen
    # (matched OR ftthOnly, in either run) forms the full census set; anyone
    # not left with installs after merging falls back to ftthOnly.
    census_names = set(e_matched) | set(n_matched) | set(e_ftth) | set(n_ftth)
    building_names = set(e_matched) | set(n_matched) | set(e_bbonly) | set(n_bbonly)

    def leaf_rows_for(entry):
        rows = []
        if entry:
            _flatten_leaf_breakdown(entry.get("breakdown", []), "x", entry["name"], rows)
        return rows

    def merged_entity(name, is_matched):
        e = (e_matched if is_matched else e_bbonly).get(name, {})
        n = (n_matched if is_matched else n_bbonly).get(name, {})
        merged_m = _merge_month_map(e.get("m", {}), n.get("m", {}), new_months, final_months)
        if not merged_m:
            return None
        entry = {
            "name": name, "m": merged_m,
            "district": n.get("district") or e.get("district") or "(no district)",
            "bid": n.get("bid") or e.get("bid"),
        }
        if is_matched:
            entry["activeFtth"] = n.get("activeFtth", e.get("activeFtth", 0))

        # Merge the breakdown trees at the leaf level (channel/special/
        # territory/partner/month), the same month-preference rule as
        # everywhere else, then rebuild the rollup tree from those leaves.
        e_leaves = leaf_rows_for(e)
        n_leaves = leaf_rows_for(n)
        leaf_map = {}  # (channel, special, territory, partner) -> {month: {g,r}}
        for mode, building, channel, special, territory, partner, mk, ga, revenue in e_leaves:
            key = (channel, special, territory, partner)
            leaf_map.setdefault(key, {})[mk] = {"g": ga, "r": revenue}
        new_leaf_map = {}
        for mode, building, channel, special, territory, partner, mk, ga, revenue in n_leaves:
            key = (channel, special, territory, partner)
            new_leaf_map.setdefault(key, {})[mk] = {"g": ga, "r": revenue}
        all_keys = set(leaf_map) | set(new_leaf_map)
        merged_leaf_rows = []
        for key in all_keys:
            merged_mm = _merge_month_map(leaf_map.get(key, {}), new_leaf_map.get(key, {}), new_months, final_months)
            channel, special, territory, partner = key
            for mk, v in merged_mm.items():
                merged_leaf_rows.append(("x", name, channel, special, territory, partner, mk, v["g"], v["r"]))
        entry["breakdown"] = _rebuild_breakdown_tree(merged_leaf_rows)
        return entry

    matched, bb_only = [], []
    for name in building_names:
        is_matched = name in census_names
        entry = merged_entity(name, is_matched)
        if entry:
            (matched if is_matched else bb_only).append(entry)
    matched.sort(key=lambda r: -r["activeFtth"])
    bb_only.sort(key=lambda r: -sum(v["g"] for v in r["m"].values()))

    matched_names_final = {r["name"] for r in matched}
    ftth_only = []
    for name in census_names - matched_names_final:
        active = (n_ftth.get(name) or e_ftth.get(name) or n_matched.get(name) or e_matched.get(name) or {}).get("activeFtth", 0)
        ftth_only.append({"name": name, "activeFtth": active})
    ftth_only.sort(key=lambda r: -r["activeFtth"])

    return {"matched": matched, "ftthOnly": ftth_only, "bbOnly": bb_only}


def _merge_mode_block(existing_block, new_block, new_months, final_months, with_targets):
    dims = [
        ("district", True, False), ("channel", True, False), ("subChannel", True, True),
        ("dealerTerritory", False, False), ("supervisor", False, False),
    ]
    block = {}
    for key, with_import, with_target in dims:
        block[key] = _merge_entity_list(
            existing_block.get(key, []), new_block.get(key, []),
            new_months, final_months, with_import, with_target and with_targets,
        )
    block["supervisor"] = block["supervisor"][:20]
    block["technology"] = _merge_technology(
        existing_block.get("technology", {}), new_block.get("technology", {}), new_months, final_months
    )
    block["villages"] = _merge_villages(
        existing_block.get("villages", {"matched": [], "bbOnly": [], "ftthOnly": []}),
        new_block.get("villages", {"matched": [], "bbOnly": [], "ftthOnly": []}),
        new_months, final_months,
    )
    return block


def merge_months(existing_out, new_out, max_months):
    """Merges a freshly-computed build_output() into whatever's already been
    synced to the Sheet (also in build_output()'s shape, via reconstruct()),
    keeping the max_months most recent months by key. existing_out may be
    None (nothing synced yet -- first run), in which case this just caps
    new_out to its own most recent max_months and returns it unchanged
    otherwise."""
    new_months_set = {m["key"] for m in new_out["months"]}

    if existing_out is None:
        final_keys = sorted(new_months_set)[-max_months:]
    else:
        existing_keys = {m["key"] for m in existing_out["months"]}
        final_keys = sorted(existing_keys | new_months_set)[-max_months:]
    final_keys_set = set(final_keys)

    existing_months_by_key = {m["key"]: m for m in (existing_out["months"] if existing_out else [])}
    new_months_by_key = {m["key"]: m for m in new_out["months"]}
    merged_months = [
        new_months_by_key[k] if k in new_months_by_key else existing_months_by_key[k]
        for k in final_keys
    ]

    existing_register = existing_out["register"] if existing_out else {"byStatus": {}}
    new_register = new_out["register"]

    connect_block = _merge_mode_block(
        existing_out if existing_out else {}, new_out, new_months_set, final_keys_set, with_targets=True
    )
    register_block = _merge_mode_block(
        existing_register, new_register, new_months_set, final_keys_set, with_targets=False
    )
    statuses = set(existing_register.get("byStatus", {})) | set(new_register.get("byStatus", {}))
    by_status = {}
    for status in statuses:
        merged = _merge_mode_block(
            existing_register.get("byStatus", {}).get(status, {}),
            new_register.get("byStatus", {}).get(status, {}),
            new_months_set, final_keys_set, with_targets=False,
        )
        if merged["district"]:  # drop a status entirely if it has no activity left in the merged window
            by_status[status] = merged
    register_block["byStatus"] = by_status

    # Meta is whole-window summary info -- recomputed from the merged
    # buildings list (source of truth after merging) rather than taken
    # wholesale from either side, so totals stay accurate to what's actually
    # in the merged output.
    village_names = {r["name"] for r in connect_block["villages"]["matched"] + connect_block["villages"]["bbOnly"]}
    matched_names = {r["name"] for r in connect_block["villages"]["matched"]}
    census_total = len(village_names | {r["name"] for r in connect_block["villages"]["ftthOnly"]} | matched_names)
    meta = dict(new_out["meta"])  # freshest source-file/generatedAt info wins
    meta["totalBuildings"] = len(village_names)
    meta["matchedCount"] = len(matched_names)
    meta["totalVillages"] = len(connect_block["villages"]["ftthOnly"]) + len(matched_names)
    meta["totalActiveFtth"] = sum(r.get("activeFtth", 0) for r in connect_block["villages"]["matched"]) + \
        sum(r["activeFtth"] for r in connect_block["villages"]["ftthOnly"])
    meta["matchRateVillages"] = round(len(matched_names) / meta["totalVillages"] * 100, 1) if meta["totalVillages"] else 0

    out = {"meta": meta, "months": merged_months, "register": register_block}
    out.update(connect_block)
    return out
