#!/usr/bin/env python3
"""Aggregates the monthly True broadband order exports in TOL Tracker/Data/ plus
TOL Tracker/Data/Active FTTH In Village_BMA-West.TXT (existing active-subscriber census) into a
single JSON summary for one province cluster, embedded in a tracking dashboard.

This dashboard measures INSTALLS ONLY -- successful, KPI-qualifying connects.
Pending / Cancel / Un-Complete orders are deliberately not counted or displayed
anywhere. (Worth knowing: ~28% of pending orders carry no channel attribution at
all, while 100% of installs do, so the install-only view is also the better-
attributed one.)

Months are DISCOVERED from the folder, not hand-listed: any TOL_*.txt plus
BB_CURRENT_MTH.txt is scanned, its month read from the data itself, and its
calendar derived. Drop a new export in and re-run -- no code edit needed.
"""
import calendar
import json
import os
import re
from collections import defaultdict
from datetime import datetime
from glob import glob

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(HERE)
# Raw exports and the census live in Data/ (gitignored -- this repo is published
# to GitHub Pages, and these files must never reach it). Generated local
# outputs go to Local/, also gitignored, for the same reason.
DATA_DIR = os.path.join(REPO_DIR, "Data")
LOCAL_DIR = os.path.join(REPO_DIR, "Local")
FTTH_SRC = os.path.join(DATA_DIR, "Active FTTH In Village_BMA-West.TXT")
OUT = os.path.join(LOCAL_DIR, "bb_current_mth_summary.json")
TEMPLATE = os.path.join(HERE, "bb_tracker_template.html")
DASHBOARD_OUT = os.path.join(LOCAL_DIR, "BB Current Month Tracker.html")

# Scope: which customers this tracker covers. TDS_PROVINCE_CUST is the CUSTOMER's
# own province -- where the building actually is -- which is what a village-level
# tracker should filter on. Deliberately NOT TDS_PROVINCE_PARTNER (the selling
# dealer's assigned territory): a local customer can be sold to by a dealer
# registered anywhere, and that mismatch is the "import sale" signal shown below.
PROVINCE_CUST = "NTB : Pak Kret, Bang Bua Thong, Sai Noi"
# Matching group on the FTTH side (Vill_HOZ groups several BB provinces together,
# but this cluster happens to map 1:1 to its own group).
FTTH_HOZ = "NTB : ปากเกร็ด_บางบัวทอง_ไทรน้อย"

# Rolling window: only ever process the N most recent months, however many
# exports actually sit in Data/ -- older files are left on disk (nothing
# is deleted there) but simply never make it into `specs`, so both the local
# dashboard and the Sheet stay capped without deleting your source data.
MAX_MONTHS = 6


def normalize_name(s):
    """Strip common village-name prefixes/punctuation so the same place spelled
    slightly differently in the two source systems has a shot at matching exactly.
    Deliberately exact-match only (no fuzzy/substring matching) -- a wrong pairing
    would misreport real numbers."""
    s = (s or "").strip()
    s = re.sub(r"^(หมู่บ้าน|ม\.|บ้าน)\s*", "", s)
    s = re.sub(r"[\s\-\._()]+", "", s)
    return s.lower()


# The known SUBS_STATUS values a TOL-REGISTER row can carry. Anything else
# (data has shown a stray "Disconnect(Y)" here and there) lumps into "other"
# rather than growing this list -- these four are the real lifecycle stages.
STATUS_LABELS = {"connect": "Connect", "pending": "Pending", "cancel": "Cancel", "uncomplete": "Un-Complete", "other": "Other"}


def status_slug(status):
    s = (status or "").strip().lower()
    if s == "connect":
        return "connect"
    if s == "pending":
        return "pending"
    if s == "cancel":
        return "cancel"
    if s == "un-complete":
        return "uncomplete"
    return "other"


def district_of(org_level_aa_name):
    """ORG_LEVEL_AA_NAME_CUST looks like 'นนทบุรี_ปากเกร็ด_A1' -- province_district_zone.
    The middle segment is the amphoe/district, the only sub-province geography in
    the export that is human-readable."""
    parts = (org_level_aa_name or "").split("_")
    return parts[1].strip() if len(parts) >= 2 and parts[1].strip() else "(no district)"


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def read_header(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        header = f.readline().rstrip("\r\n").split("|")
    return {name.strip(): i for i, name in enumerate(header)}, len(header)


def detect_month(path):
    """Read the month straight out of the data (TM_KEY_DAY = YYYYMMDD) rather than
    trusting the filename -- BB_CURRENT_MTH.txt carries no date in its name at all,
    and a mislabelled file would otherwise be silently filed under the wrong month."""
    idx, ncol = read_header(path)
    i = idx.get("TM_KEY_DAY")
    if i is None:
        return None
    counts = defaultdict(int)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        f.readline()
        for n, line in enumerate(f):
            if n > 20000:
                break
            parts = line.rstrip("\r\n").split("|")
            if len(parts) < ncol or i >= len(parts):
                continue
            d = parts[i]
            if len(d) == 8 and d.isdigit():
                counts[d[:6]] += 1
    return max(counts.items(), key=lambda kv: kv[1])[0] if counts else None


def discover_months():
    """Any TOL_*.txt plus BB_CURRENT_MTH.txt. If two files resolve to the same
    month, the more recently modified one wins. Only the MAX_MONTHS most
    recent months (by month key, not file mtime) are kept -- older exports
    can keep piling up in Data/ without the dashboard or the Sheet
    growing without bound."""
    cands = sorted(glob(os.path.join(DATA_DIR, "TOL_*.txt")))
    bb = os.path.join(DATA_DIR, "BB_CURRENT_MTH.txt")
    if os.path.exists(bb):
        cands.append(bb)

    chosen = {}
    for path in cands:
        mk = detect_month(path)
        if not mk:
            print(f"  ! {os.path.basename(path)}: no readable TM_KEY_DAY, skipped")
            continue
        if mk in chosen and os.path.getmtime(chosen[mk]) >= os.path.getmtime(path):
            continue
        chosen[mk] = path

    specs = []
    for mk in sorted(chosen)[-MAX_MONTHS:]:
        y, m = int(mk[:4]), int(mk[4:6])
        specs.append(
            {
                "key": mk,
                "label": f"{calendar.month_name[m]} {y}",
                "short": calendar.month_abbr[m].upper(),
                "days": calendar.monthrange(y, m)[1],  # derived, never hand-typed
                "file": os.path.basename(chosen[mk]),
                "path": chosen[mk],
            }
        )
    return specs


def monthly_store():
    """{key: {month: {ga, revenue}}} -- installs only."""
    return defaultdict(lambda: defaultdict(lambda: {"ga": 0, "revenue": 0.0}))


def add(store, key, month, revenue):
    b = store[key][month]
    b["ga"] += 1
    b["revenue"] += revenue


def bump(acc, suffix, mk, district, group_channel, sub_ch, territory, supervisor, technology,
         partner, special_channel, building, building_id_val, net_rc):
    """Records one qualifying row (an install, suffix='', or a registration,
    suffix='_reg') into every breakdown that dimension gets sliced by. Building
    identity (name/district/id) is shared across both -- it's the same building
    either way, only the counts sliced from it differ."""
    add(acc["district" + suffix], district, mk, net_rc)
    add(acc["channel" + suffix], group_channel, mk, net_rc)
    add(acc["sub_channel" + suffix], sub_ch, mk, net_rc)
    add(acc["territory" + suffix], territory, mk, net_rc)
    add(acc["supervisor" + suffix], supervisor, mk, net_rc)
    if territory != PROVINCE_CUST:
        add(acc["district" + suffix + "_imp"], district, mk, net_rc)
        add(acc["channel" + suffix + "_imp"], group_channel, mk, net_rc)
        add(acc["sub_channel" + suffix + "_imp"], sub_ch, mk, net_rc)
        add(acc["supervisor" + suffix + "_imp"], supervisor, mk, net_rc)
    acc["technology" + suffix][technology][mk] += 1
    if building and building != "-":
        key = normalize_name(building)
        if key:
            acc["building_name"].setdefault(key, building)
            add(acc["building" + suffix], key, mk, net_rc)
            if territory != PROVINCE_CUST:
                add(acc["building" + suffix + "_imp"], key, mk, net_rc)
            acc["building_district"][key][district] += 1
            if building_id_val and building_id_val != "0":
                acc["building_id"][key][building_id_val] += 1
            leaf = acc["breakdown" + suffix][key][group_channel][special_channel][territory][partner][mk]
            leaf["ga"] += 1
            leaf["revenue"] += net_rc


def parse_month(spec, acc, is_current=False):
    mk = spec["key"]
    idx, ncol = read_header(spec["path"])

    def col(parts, name):
        i = idx.get(name)
        return parts[i] if i is not None and i < len(parts) else ""

    daily = {d: {"ga": 0, "revenue": 0.0} for d in range(1, spec["days"] + 1)}
    daily_reg = {d: {"ga": 0, "revenue": 0.0} for d in range(1, spec["days"] + 1)}
    daily_reg_status = {slug: {d: {"ga": 0, "revenue": 0.0} for d in range(1, spec["days"] + 1)} for slug in STATUS_LABELS}
    installs = 0
    revenue = 0.0
    registrations = 0     # SOURCES=TOL-REGISTER, KPI-flagged, ANY subs status --
    reg_revenue = 0.0     # bookings volume, not just the ones that went on to connect.
    reg_by_status = defaultdict(int)          # raw SUBS_STATUS string -> count, for display text
    reg_count_by_slug = defaultdict(int)      # status_slug() -> count, keys always match STATUS_LABELS
    reg_revenue_by_status = defaultdict(float)
    target = 0.0
    dupes = 0
    non_kpi_connect = 0   # in TOL-CONNECT, but FLAG_KPI != "KPI"
    other_status = 0      # not a connect at all (pending / cancel / un-complete)
    last_day = 0
    target_by_sub = defaultdict(float)

    with open(spec["path"], "r", encoding="utf-8", errors="replace") as f:
        f.readline()
        for line in f:
            parts = line.rstrip("\r\n").split("|")
            if len(parts) < ncol:
                continue

            actual_type = col(parts, "ACTUAL_TYPE")
            group_channel = col(parts, "GROUP_CHANNEL") or "(unspecified)"
            sub_ch = col(parts, "TDS_SUB_CHANNEL") or "(unspecified)"

            if actual_type == "TARGET":
                # TARGET rows carry no customer (TDS_PROVINCE_CUST is blank) -- targets
                # are set per dealer territory, so scope them by TDS_PROVINCE_PARTNER.
                # That measures the local team against its own quota, a related but
                # distinct question from the customer-scoped install totals.
                if col(parts, "TDS_PROVINCE_PARTNER") != PROVINCE_CUST:
                    continue
                t = to_float(col(parts, "TARGET"))
                target += t
                target_by_sub[sub_ch] += t
                continue
            if actual_type != "ACTUAL":
                continue
            if col(parts, "TDS_PROVINCE_CUST") != PROVINCE_CUST:
                continue

            status = col(parts, "SUBS_STATUS")
            sources = col(parts, "SOURCES")
            flag_kpi = col(parts, "FLAG_KPI")
            day_s = col(parts, "TM_KEY_DAY")
            day = int(day_s[6:8]) if len(day_s) == 8 and day_s.isdigit() else 0

            territory = col(parts, "TDS_PROVINCE_PARTNER") or "(unspecified)"
            district = district_of(col(parts, "ORG_LEVEL_AA_NAME_CUST"))
            partner = col(parts, "PARTNER_NAME").strip() or "(unspecified)"
            technology = col(parts, "TECHNOLOGY") or "(unspecified)"
            supervisor = col(parts, "SUPERVISOR_NAME") or "(unspecified)"
            special_channel = col(parts, "TDS_SPECIAL_CHANNEL") or "(unspecified)"
            building = col(parts, "BUILDING_CODE").strip()
            bid = col(parts, "BUILDING_ID").strip()
            net_rc = to_float(col(parts, "NET_RC"))

            # Registrations: every order submitted through the booking feed, whatever
            # it later becomes (connected / still pending / cancelled / un-complete),
            # dated by when it was REGISTERED -- a different question from "did it
            # connect." Independent of the Connect dedup below: SOURCES already
            # separates the two feeds, so a row only ever qualifies for one block.
            if sources == "TOL-REGISTER" and flag_kpi == "KPI":
                slug = status_slug(status)
                registrations += 1
                reg_revenue += net_rc
                reg_by_status[status or "(unspecified)"] += 1
                reg_count_by_slug[slug] += 1
                reg_revenue_by_status[slug] += net_rc
                if day in daily_reg:
                    daily_reg[day]["ga"] += 1
                    daily_reg[day]["revenue"] += net_rc
                    daily_reg_status[slug][day]["ga"] += 1
                    daily_reg_status[slug][day]["revenue"] += net_rc
                bump(acc, "_reg", mk, district, group_channel, sub_ch, territory, supervisor,
                     technology, partner, special_channel, building, bid, net_rc)
                bump(acc, "_reg_" + slug, mk, district, group_channel, sub_ch, territory, supervisor,
                     technology, partner, special_channel, building, bid, net_rc)

            # Every Connect order appears under BOTH SOURCES=TOL-REGISTER and
            # SOURCES=TOL-CONNECT (same SUI_ORDER_NUM, two feeds). TOL-CONNECT is
            # authoritative; counting both doubles every figure.
            if status == "Connect" and sources != "TOL-CONNECT":
                dupes += 1
                continue

            # Track the real end of data so a mid-month export isn't averaged or
            # target-compared as though the whole month had elapsed.
            if day:
                last_day = max(last_day, day)

            if status != "Connect":
                other_status += 1
                continue
            if flag_kpi != "KPI":
                # A real connection, but not one that counts toward the sales KPI
                # (migrations, internal, data-only). Excluded so installs stay
                # like-for-like with the KPI target they are scored against.
                non_kpi_connect += 1
                continue

            installs += 1
            revenue += net_rc
            if day in daily:
                daily[day]["ga"] += 1
                daily[day]["revenue"] += net_rc
            bump(acc, "", mk, district, group_channel, sub_ch, territory, supervisor,
                 technology, partner, special_channel, building, bid, net_rc)

            # Day-of-month building breakdown, current month only -- every other
            # breakdown in `acc` is keyed by calendar month, which is what the
            # multi-month Buildings table wants; this mirrors just the building
            # slice of bump() with `day` standing in for `mk`, so the current
            # month's Buildings-by-day table can drill channel -> sub-channel ->
            # territory -> dealer exactly like the monthly one does.
            if is_current and building and building != "-":
                key = normalize_name(building)
                if key:
                    add(acc["building_daily"], key, day, net_rc)
                    leaf = acc["breakdown_daily"][key][group_channel][special_channel][territory][partner][day]
                    leaf["ga"] += 1
                    leaf["revenue"] += net_rc

    for sub, t in target_by_sub.items():
        acc["sub_target"][sub][mk] += t

    elapsed = last_day or spec["days"]
    partial = elapsed < spec["days"]
    return {
        "key": mk,
        "label": spec["label"],
        "short": spec["short"],
        "days": spec["days"],
        "elapsedDays": elapsed,
        "partial": partial,
        "file": spec["file"],
        "mtime": datetime.fromtimestamp(os.path.getmtime(spec["path"])).isoformat(),
        "installs": installs,
        "dupes": dupes,
        "otherStatus": other_status,
        "nonKpiConnect": non_kpi_connect,
        "totals": {"ga": installs, "revenue": round(revenue, 2), "target": round(target, 2)},
        "daily": [{"day": d, "ga": v["ga"], "revenue": round(v["revenue"], 2)} for d, v in sorted(daily.items())],
        # Register-side counterparts -- same month, no target concept (registering
        # isn't scored against a quota the way a connect is), elapsedDays/partial
        # reused from the Connect side since it's the same file/timeframe either way.
        "installsReg": registrations,
        "totalsReg": {"ga": registrations, "revenue": round(reg_revenue, 2), "target": 0},
        "dailyReg": [{"day": d, "ga": v["ga"], "revenue": round(v["revenue"], 2)} for d, v in sorted(daily_reg.items())],
        "registerByStatus": dict(reg_by_status),
        # Per-status counterparts of installsReg/totalsReg/dailyReg above, so the
        # Register status filter can swap the hero/sparkline/month-cards too, not
        # just the breakdown tables -- keyed by the same labels as out.register.byStatus.
        "installsRegByStatus": {STATUS_LABELS[slug]: reg_count_by_slug.get(slug, 0) for slug in STATUS_LABELS},
        "totalsRegByStatus": {
            STATUS_LABELS[slug]: {"ga": reg_count_by_slug.get(slug, 0), "revenue": round(reg_revenue_by_status[slug], 2), "target": 0}
            for slug in STATUS_LABELS
        },
        "dailyRegByStatus": {
            STATUS_LABELS[slug]: [{"day": d, "ga": v["ga"], "revenue": round(v["revenue"], 2)} for d, v in sorted(daily_reg_status[slug].items())]
            for slug in STATUS_LABELS
        },
    }


def parse_ftth():
    with open(FTTH_SRC, "r", encoding="utf-8-sig", errors="replace") as f:
        header = f.readline().rstrip("\r\n").split("^")
        idx = {n.strip(): i for i, n in enumerate(header)}

        def col(parts, name):
            i = idx.get(name)
            return parts[i] if i is not None and i < len(parts) else ""

        villages = defaultdict(lambda: {"active": 0, "raw_name": None, "lat": None, "lng": None})
        scoped = 0
        for line in f:
            parts = line.rstrip("\r\n").split("^")
            if len(parts) < len(header):
                continue
            if col(parts, "Vill_HOZ") != FTTH_HOZ:
                continue
            scoped += 1
            name = col(parts, "GIS_VILL_NAME").strip()
            if not name:
                continue
            key = normalize_name(name)
            if not key:
                continue
            v = villages[key]
            if v["raw_name"] is None:
                v["raw_name"] = name
            v["active"] += 1
            # First port's coordinates stand in for the whole village -- ports
            # within one village cluster tightly enough for a map marker;
            # exact per-port precision isn't the point here.
            if v["lat"] is None:
                lat, lng = to_float(col(parts, "Vill_ADDRESS_LAT")), to_float(col(parts, "Vill_ADDRESS_LONG"))
                if lat and lng:
                    v["lat"], v["lng"] = lat, lng
    return {"villages": villages, "scoped": scoped}


def compact(month_map):
    """{month: bucket} -> sparse {month: {g, r}}, dropping empty months."""
    out = {}
    for mk, b in month_map.items():
        if b["ga"]:
            out[mk] = {"g": b["ga"], "r": round(b["revenue"], 2)}
    return out


def rollup_breakdown(d, depth):
    """Collapses one channel->sub_channel->territory->partner tree (leaves keyed
    by period -- calendar month or day-of-month, doesn't matter which) into the
    nested {name, m, children} shape the dashboard's drill-down rows read."""
    items = []
    for name, val in d.items():
        if depth == 3:
            m = {pk: {"g": b["ga"], "r": round(b["revenue"], 2)} for pk, b in val.items() if b["ga"]}
            if m:
                items.append({"name": name, "m": m})
        else:
            kids = rollup_breakdown(val, depth + 1)
            if not kids:
                continue
            agg = defaultdict(lambda: {"g": 0, "r": 0.0})
            for k in kids:
                for pk, e in k["m"].items():
                    agg[pk]["g"] += e["g"]
                    agg[pk]["r"] += e["r"]
            items.append({"name": name, "m": {pk: {"g": v["g"], "r": round(v["r"], 2)} for pk, v in agg.items()}, "children": kids})
    items.sort(key=lambda x: -sum(v["g"] for v in x["m"].values()))
    return items


def listify(store, extra=None, imp=None):
    rows = []
    for key, months in store.items():
        m = compact(months)
        if not m:
            continue
        row = {"name": key, "m": m}
        if imp is not None:
            mi = compact(imp.get(key, {}))
            if mi:
                row["mi"] = mi
        if extra:
            extra(key, row)
        rows.append(row)
    rows.sort(key=lambda r: -sum(v["g"] for v in r["m"].values()))
    return rows


def build_output():
    """Runs the full ETL and returns the `out` dict -- everything downstream
    (writing local files, or pushing to a Google Sheet for the hosted version)
    just serializes this same dict, so the two never drift apart."""
    specs = discover_months()
    if not specs:
        raise SystemExit("No monthly exports found in " + DATA_DIR)

    def new_breakdown():
        return defaultdict(
            lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(
                lambda: {"ga": 0, "revenue": 0.0}
            )))))
        )

    acc = {
        # Building identity (name/district/id) and sub-channel targets are shared --
        # same building, same target, regardless of which feed (Connect or Register)
        # is being counted. Everything else below comes in Connect ("") / Register
        # ("_reg") pairs, written by bump() above.
        "building_name": {},
        "building_district": defaultdict(lambda: defaultdict(int)),
        "building_id": defaultdict(lambda: defaultdict(int)),
        "sub_target": defaultdict(lambda: defaultdict(float)),
        # Current-month-only, day-keyed counterparts of "building"/"breakdown"
        # (Connect installs only -- see parse_month's is_current block) for the
        # Buildings-by-day table. Everything else in `acc` is keyed by calendar
        # month; these two are keyed by day-of-month instead.
        "building_daily": monthly_store(),
        "breakdown_daily": new_breakdown(),
    }
    all_suffixes = ("", "_reg") + tuple("_reg_" + slug for slug in STATUS_LABELS)
    for suffix in all_suffixes:
        acc["district" + suffix] = monthly_store()
        acc["channel" + suffix] = monthly_store()
        acc["sub_channel" + suffix] = monthly_store()
        acc["territory" + suffix] = monthly_store()
        acc["supervisor" + suffix] = monthly_store()
        acc["district" + suffix + "_imp"] = monthly_store()
        acc["channel" + suffix + "_imp"] = monthly_store()
        acc["sub_channel" + suffix + "_imp"] = monthly_store()
        acc["supervisor" + suffix + "_imp"] = monthly_store()
        acc["building" + suffix] = monthly_store()
        acc["building" + suffix + "_imp"] = monthly_store()
        acc["technology" + suffix] = defaultdict(lambda: defaultdict(int))
        acc["breakdown" + suffix] = new_breakdown()

    months = []
    for spec in specs:
        m = parse_month(spec, acc, is_current=(spec is specs[-1]))
        months.append(m)
        flag = f"  PARTIAL through day {m['elapsedDays']}/{m['days']}" if m["partial"] else ""
        print(f"  {spec['label']:<16} installs {m['installs']:>5}   (dropped {m['dupes']} dupes, {m['otherStatus']} non-install){flag}")

    ftth = parse_ftth()

    def build_breakdown(suffix, key):
        return rollup_breakdown(acc["breakdown" + suffix].get(key, {}), 0)

    def build_breakdown_daily(key):
        return rollup_breakdown(acc["breakdown_daily"].get(key, {}), 0)

    def build_buildings_daily():
        """Same shape as one villages.matched/bbOnly row, but `m` is keyed by
        day-of-month instead of calendar month, and scoped to the current
        (most recent) month's Connect installs only -- see is_current in
        parse_month(). Building identity is the same shared lookup the
        monthly Buildings table uses; only the counts are day-sliced."""
        rows = []
        for key in acc["building_daily"].keys():
            m = compact(acc["building_daily"][key])
            if not m:
                continue
            rows.append({
                "name": acc["building_name"][key],
                "m": m,
                "breakdown": build_breakdown_daily(key),
                "district": top_key(acc["building_district"].get(key)) or "(no district)",
                "bid": top_key(acc["building_id"].get(key)),
            })
        rows.sort(key=lambda r: -sum(v["g"] for v in r["m"].values()))
        return rows

    def top_key(counter):
        return max(counter.items(), key=lambda kv: kv[1])[0] if counter else None

    village_keys = set(ftth["villages"].keys())

    def build_villages(suffix):
        """Villages x buildings, from either the Connect ("") or Register ("_reg")
        building counts. Building identity (name/district/id) is shared regardless
        of suffix -- only which buildings actually have counts differs."""
        building_keys = set(acc["building" + suffix].keys())
        matched_keys = village_keys & building_keys

        matched, bb_only = [], []
        for key in building_keys:
            m = compact(acc["building" + suffix][key])
            if not m:
                continue
            row = {
                "name": acc["building_name"][key],
                "m": m,
                "breakdown": build_breakdown(suffix, key),
                "district": top_key(acc["building_district"].get(key)) or "(no district)",
                "bid": top_key(acc["building_id"].get(key)),
            }
            mi = compact(acc["building" + suffix + "_imp"].get(key, {}))
            if mi:
                row["mi"] = mi
            if key in matched_keys:
                row["name"] = ftth["villages"][key]["raw_name"] or row["name"]
                row["activeFtth"] = ftth["villages"][key]["active"]
                # Only matched villages carry census coordinates -- bbOnly
                # buildings (no FTTH census match) have nowhere to plot.
                if ftth["villages"][key]["lat"] is not None:
                    row["lat"] = ftth["villages"][key]["lat"]
                    row["lng"] = ftth["villages"][key]["lng"]
                matched.append(row)
            else:
                bb_only.append(row)
        matched.sort(key=lambda r: -r["activeFtth"])
        bb_only.sort(key=lambda r: -sum(v["g"] for v in r["m"].values()))

        ftth_only = sorted(
            ({"name": ftth["villages"][k]["raw_name"], "activeFtth": ftth["villages"][k]["active"]}
             for k in village_keys - building_keys),
            key=lambda r: -r["activeFtth"],
        )
        return {"matched": matched, "ftthOnly": ftth_only, "bbOnly": bb_only}, building_keys, matched_keys

    def with_target(key, row):
        t = {mk: round(v, 2) for mk, v in acc["sub_target"].get(key, {}).items() if v}
        if t:
            row["t"] = t

    def build_mode_block(suffix, with_targets=False):
        """Every breakdown table/chart the dashboard has, sourced from one suffix's
        slice of acc -- Connect (""), all Registrations ("_reg"), or one specific
        registration status ("_reg_" + slug). Same shape every time, so the
        frontend can swap the whole block in wholesale regardless of which."""
        villages_x, keys_x, matched_x = build_villages(suffix)
        return {
            "district": listify(acc["district" + suffix], imp=acc["district" + suffix + "_imp"]),
            "channel": listify(acc["channel" + suffix], imp=acc["channel" + suffix + "_imp"]),
            "subChannel": listify(acc["sub_channel" + suffix], extra=with_target if with_targets else None,
                                   imp=acc["sub_channel" + suffix + "_imp"]),
            "dealerTerritory": listify(acc["territory" + suffix]),
            "supervisor": listify(acc["supervisor" + suffix])[:20],
            "technology": {k: dict(v) for k, v in acc["technology" + suffix].items()},
            "villages": villages_x,
        }, keys_x, matched_x

    connect_block, building_keys, matched_keys = build_mode_block("", with_targets=True)
    register_block, _, _ = build_mode_block("_reg")
    # Every-table status filter (All/Connect/Pending/Cancel/Un-Complete/Other) for
    # Register mode -- e.g. "which districts are the cancellations concentrated
    # in." Skips a status entirely if this cluster never saw it (village_keys is
    # shared/static so "district" alone is enough to detect that).
    by_status = {}
    for slug, label in STATUS_LABELS.items():
        block, _, _ = build_mode_block("_reg_" + slug)
        if block["district"]:
            by_status[label] = block
    register_block["byStatus"] = by_status

    out = {
        "meta": {
            "province": PROVINCE_CUST,
            "ftthSourceFile": os.path.basename(FTTH_SRC),
            "ftthSourceMTime": datetime.fromtimestamp(os.path.getmtime(FTTH_SRC)).isoformat(),
            "ftthRowCount": ftth["scoped"],
            "generatedAt": datetime.now().isoformat(),
            "totalActiveFtth": sum(v["active"] for v in ftth["villages"].values()),
            "totalVillages": len(village_keys),
            "totalBuildings": len(building_keys),
            "matchedCount": len(matched_keys),
            "matchRateVillages": round(len(matched_keys) / len(village_keys) * 100, 1) if village_keys else 0,
        },
        "months": months,
        # Register mirror of everything above, same shape, sourced from SOURCES=
        # TOL-REGISTER rows (any subs status) instead of completed Connects -- lets
        # the dashboard's Connect/Register toggle swap this whole block in wholesale.
        "register": register_block,
        **connect_block,
        # Buildings-by-day for the current (most recent) month only, Connect
        # installs only -- deliberately outside the Connect/Register toggle
        # above; a second, narrower Buildings view, not a mode of the first.
        "buildingsDaily": {
            "monthKey": months[-1]["key"],
            "label": months[-1]["label"],
            "days": months[-1]["days"],
            "elapsedDays": months[-1]["elapsedDays"],
            "partial": months[-1]["partial"],
            "rows": build_buildings_daily(),
        },
    }
    return out


def main():
    out = build_output()
    months, building_keys, village_keys, matched_keys = (
        out["months"],
        out["meta"]["totalBuildings"],
        out["meta"]["totalVillages"],
        out["meta"]["matchedCount"],
    )

    os.makedirs(LOCAL_DIR, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    with open(TEMPLATE, "r", encoding="utf-8") as f:
        template = f.read()
    dash = os.path.normpath(DASHBOARD_OUT)
    with open(dash, "w", encoding="utf-8") as f:
        f.write(template.replace("__BB_DATA_JSON__", json.dumps(out, ensure_ascii=False, separators=(",", ":"))))

    total = sum(m["installs"] for m in months)
    print(f"\n  {len(months)} months   {total} installs   {len(out['district'])} districts")
    print(f"  villages matched {matched_keys} / {village_keys}   buildings {building_keys}")
    print(f"  wrote {os.path.basename(OUT)} ({os.path.getsize(OUT)/1e6:.2f} MB)  and  {os.path.basename(dash)} ({os.path.getsize(dash)/1e6:.2f} MB)")


if __name__ == "__main__":
    main()
