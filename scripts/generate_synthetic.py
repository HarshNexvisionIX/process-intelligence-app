"""Generate synthetic event logs with a KNOWN process and KNOWN defects.

Why: real logs never tell you the right answer. Here we know exactly which
process created the data and which defects we injected, so we can measure
whether the app finds the right map and whether the quality gate catches
every defect.

Examples (run from the project root):
    python scripts/generate_synthetic.py --cases 20000 --out data/samples/clean_small.parquet
    python scripts/generate_synthetic.py --cases 1400000 --out data/samples/clean_10m.parquet
    python scripts/generate_synthetic.py --cases 20000 --missing-ts 0.03 --ties 0.15 \
        --duplicates 0.02 --typos 0.02 --date-only 0.4 --out data/samples/dirty_small.csv \
        --ts-format "%d/%m/%Y %H:%M:%S"

    python scripts/generate_synthetic.py --cases 20000 --split-by day --format parquet \
        --out data/samples/daily_dataset

A ground-truth JSON is written next to every file (same name + .truth.json).
With --split-by day/month, --out is a FOLDER with one file per day/month and
the ground truth is written into it as truth.json.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa

# ---- The "true" process: variants and how often they happen ----------------
VARIANTS = [
    (0.45, ["Create Request", "Approve Request", "Create Order", "Receive Goods", "Receive Invoice", "Pay Invoice"]),
    (0.15, ["Create Request", "Approve Request", "Create Order", "Receive Invoice", "Receive Goods", "Pay Invoice"]),
    (0.10, ["Create Request", "Approve Request", "Create Order", "Change Price", "Approve Request", "Receive Goods", "Receive Invoice", "Pay Invoice"]),
    (0.12, ["Create Request", "Approve Request", "Create Order", "Receive Goods", "Receive Invoice", "Block Invoice", "Unblock Invoice", "Pay Invoice"]),
    (0.08, ["Create Request", "Reject Request"]),
    (0.05, ["Create Request", "Approve Request", "Create Order", "Cancel Order"]),
    (0.05, ["Create Request", "Approve Request", "Create Order", "Receive Goods", "Receive Invoice", "Change Vendor Bank", "Pay Invoice"]),
]

# Typical waiting time BEFORE each activity, in hours (log-normal around this).
WAIT_HOURS = {
    "Create Request": 0, "Approve Request": 20, "Reject Request": 30, "Create Order": 10,
    "Change Price": 48, "Receive Goods": 120, "Receive Invoice": 72, "Block Invoice": 6,
    "Unblock Invoice": 160, "Pay Invoice": 240, "Cancel Order": 96, "Change Vendor Bank": 24,
}

RESOURCES = {
    "Create Request": [f"requester_{i}" for i in range(60)],
    "Approve Request": [f"manager_{i}" for i in range(12)],
    "Reject Request": [f"manager_{i}" for i in range(12)],
    "Create Order": [f"buyer_{i}" for i in range(15)],
    "Change Price": [f"buyer_{i}" for i in range(15)],
    "Cancel Order": [f"buyer_{i}" for i in range(15)],
    "Receive Goods": [f"warehouse_{i}" for i in range(20)],
    "Receive Invoice": [f"ap_clerk_{i}" for i in range(10)],
    "Block Invoice": ["system"],
    "Unblock Invoice": [f"ap_clerk_{i}" for i in range(10)],
    "Change Vendor Bank": [f"vendor_admin_{i}" for i in range(3)],
    "Pay Invoice": [f"treasury_{i}" for i in range(4)],
}


def generate(cases: int, seed: int, start: str, months: int) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    probs = np.array([p for p, _ in VARIANTS])
    variant_of_case = rng.choice(len(VARIANTS), size=cases, p=probs / probs.sum())
    t0 = np.datetime64(start, "s").astype(np.int64)
    span = int(months * 30.4 * 86400)

    vendors = np.array([f"V{i:04d}" for i in range(400)])
    companies = np.array(["IN01", "IN02", "UK01", "US01"])

    cols: dict[str, list[np.ndarray]] = {k: [] for k in
        ["case_id", "activity", "timestamp", "resource", "vendor", "company_code", "amount"]}
    case_ids = np.char.add("C", np.char.zfill(np.arange(1, cases + 1).astype(str), 8))
    case_vendor = vendors[rng.integers(0, len(vendors), cases)]
    case_company = companies[rng.choice(4, cases, p=[0.4, 0.3, 0.2, 0.1])]
    case_amount = np.round(rng.lognormal(8, 1.2, cases), 2)

    for v, (_, acts) in enumerate(VARIANTS):
        idx = np.where(variant_of_case == v)[0]
        n, k = len(idx), len(acts)
        if n == 0:
            continue
        waits = np.stack([
            rng.lognormal(np.log(max(WAIT_HOURS[a], 0.01)), 0.6, n) if WAIT_HOURS[a] > 0 else np.zeros(n)
            for a in acts
        ], axis=1) * 3600
        start_s = t0 + rng.integers(0, span, n)
        ts = (start_s[:, None] + np.cumsum(waits, axis=1)).astype(np.int64)  # shape (n, k)
        cols["case_id"].append(np.repeat(case_ids[idx], k))
        cols["activity"].append(np.tile(np.array(acts), n))
        cols["timestamp"].append(ts.reshape(-1))
        cols["resource"].append(np.concatenate([
            np.array(RESOURCES[a])[rng.integers(0, len(RESOURCES[a]), n)] for a in acts
        ]).reshape(k, n).T.reshape(-1))
        cols["vendor"].append(np.repeat(case_vendor[idx], k))
        cols["company_code"].append(np.repeat(case_company[idx], k))
        cols["amount"].append(np.repeat(case_amount[idx], k))
    return {k: np.concatenate(v) for k, v in cols.items()}


# --apps: the application each step is done in. Some exports mask application names
# (MASKED_APPS are written as MD5 hashes); window titles still hint at the real app.
STEP_APPS = {
    "Create Request": "ServiceNow", "Approve Request": "Workday", "Reject Request": "Workday",
    "Create Order": "SAP Logon", "Change Price": "SAP Logon", "Cancel Order": "SAP Logon",
    "Receive Goods": "Coupa", "Receive Invoice": "Outlook", "Block Invoice": "SAP Logon",
    "Unblock Invoice": "Excel", "Change Vendor Bank": "Coupa", "Pay Invoice": "Excel",
}
MASKED_APPS = {"Workday", "Coupa", "Outlook"}
APP_TITLES = {
    "ServiceNow": "Incident INC{n} - ServiceNow",
    "Workday": "Workday - Approvals #{h}# - {mail}",
    "SAP Logon": "SAP Easy Access - Order {n}",
    "Coupa": "Coupa | Receipts - Supplier {n}",
    "Outlook": "Inbox - {mail} - Outlook",
    "Excel": "Payments_Q{q}.xlsx - Excel",
}


# --stages: the business stage of each step (written as a "phase" column).
STEP_STAGES = {
    "Create Request": "Request", "Approve Request": "Request", "Reject Request": "Request",
    "Create Order": "Ordering", "Change Price": "Ordering", "Cancel Order": "Ordering",
    "Receive Goods": "Receiving", "Receive Invoice": "Invoicing", "Block Invoice": "Invoicing",
    "Unblock Invoice": "Invoicing", "Change Vendor Bank": "Invoicing", "Pay Invoice": "Invoicing",
}


def add_ids_to_titles(data: dict) -> None:
    """Every case's ticket number (INC + case number) in its window titles; a masked #hex# token in ~40%.

    Used to test deriving the case from text when the export has no case column.
    """
    import hashlib
    cases = np.asarray(data["case_id"]).astype(str)
    tickets = np.char.add("INC", np.char.zfill(np.char.lstrip(cases, "C"), 7))
    masked = np.array(["#" + hashlib.sha1(c.encode()).hexdigest() + "#" for c in cases], dtype=object)
    keep_masked = (np.arange(len(cases)) % 5) < 2
    base = np.asarray(data["simplified_title"], dtype=object) if "simplified_title" in data else         np.char.add(np.asarray(data["activity"]).astype(str), " form").astype(object)
    # The case's own ticket comes first: other numbers already in the title come after it.
    titles = [f"{k} - {t}" + (f" {m}" if mk else "") for t, k, m, mk in zip(base, tickets, masked, keep_masked)]
    data["simplified_title" if "simplified_title" in data else "window_title"] = np.array(titles, dtype=object)


def add_batch(data: dict, days: int) -> None:
    """A 'batch' column: all cases that start in the same `days`-day window share one value.

    Mapped as the case, it mixes several requests whose events overlap in time, so the
    order of steps looks scrambled (used to test the search for a better reading).
    """
    _, inverse = np.unique(data["case_id"], return_inverse=True)
    starts = np.full(inverse.max() + 1, np.iinfo(np.int64).max, dtype=np.int64)
    np.minimum.at(starts, inverse, data["timestamp"].astype(np.int64))
    data["batch"] = np.char.add("B", (starts[inverse] // (days * 86400)).astype(str))


def shuffle_order(data: dict, rng) -> None:
    """Shuffle the steps inside every case (times stay in order): a log with no fixed sequence.

    Each case keeps its own set of steps, but in a random order, so for any two steps
    A -> B and B -> A happen about equally often.
    """
    case, ts = data["case_id"], data["timestamp"]
    in_time_order = np.lexsort((ts, case))            # rows grouped by case, oldest first
    random_order = np.lexsort((rng.random(len(case)), case))   # same groups, random inside
    for key in ("activity", "resource"):
        values = data[key].copy()
        values[in_time_order] = data[key][random_order]
        data[key] = values


def app_hash(name: str) -> str:
    import hashlib
    return hashlib.md5(name.encode()).hexdigest()


def add_apps(data: dict, rng) -> dict[str, str]:
    """Add Application_name (some values hashed) and simplified_title. Returns {hash: real name}."""
    acts = data["activity"]
    apps = np.array([STEP_APPS[a] for a in acts], dtype=object)
    recorded = np.array([app_hash(a) if a in MASKED_APPS else a for a in apps], dtype=object)
    titles = [APP_TITLES[a].format(n=int(rng.integers(1_000_000, 9_999_999)), h=app_hash(str(i))[:12],
                                   mail=f"user{int(rng.integers(1, 40))}@example.com", q=int(rng.integers(1, 5)))
              for i, a in enumerate(apps)]
    data["Application_name"] = recorded
    data["simplified_title"] = np.array(titles, dtype=object)
    add_switching_step(data, rng)
    return {app_hash(a): a for a in MASKED_APPS}


# A known switching pattern: inside SWITCH_STEP the same user goes Workday -> Excel ->
# Workday, a minute apart (e.g. checking a tracker spreadsheet while approving).
SWITCH_STEP = "Approve Request"


def add_switching_step(data: dict, rng) -> None:
    """Add two events to every SWITCH_STEP: +1 min in Excel, +2 min back in its own app."""
    idx = np.where(data["activity"] == SWITCH_STEP)[0]
    if not len(idx):
        return
    extra = {k: np.concatenate([np.asarray(v)[idx], np.asarray(v)[idx]]) for k, v in data.items()}
    extra["timestamp"] = np.concatenate([data["timestamp"][idx] + 60, data["timestamp"][idx] + 120])
    own_app = np.asarray(data["Application_name"])[idx]
    extra["Application_name"] = np.concatenate([np.full(len(idx), "Excel", dtype=object), own_app])
    extra["simplified_title"] = np.concatenate([
        np.array([f"Approval_tracker_{int(rng.integers(1, 9))}.xlsx - Excel" for _ in idx], dtype=object),
        np.asarray(data["simplified_title"])[idx]])
    for k in data:
        data[k] = np.concatenate([np.asarray(data[k]), extra[k]])


def inject_defects(data: dict, args, rng) -> dict:
    """Damage the clean log in controlled, countable ways."""
    n = len(data["timestamp"])
    counts = {}
    ts = data["timestamp"].astype("datetime64[s]").astype(object)  # python datetimes, None allowed
    ts = np.array(ts, dtype=object)

    if args.ties:  # copy the previous event's time inside the same case
        same_case_as_prev = np.r_[False, data["case_id"][1:] == data["case_id"][:-1]]
        pick = same_case_as_prev & (rng.random(n) < args.ties)
        ts[1:][pick[1:]] = ts[:-1][pick[1:]]
        counts["ties"] = int(pick.sum())
    if args.date_only:  # lose the time of day
        pick = rng.random(n) < args.date_only
        ts[pick] = [t.replace(hour=0, minute=0, second=0) for t in ts[pick]]
        counts["date_only"] = int(pick.sum())
    if args.placeholder:  # system default dates
        pick = rng.random(n) < args.placeholder
        ts[pick] = np.datetime64("1970-01-01T00:00:00").astype(object)
        counts["placeholder_dates"] = int(pick.sum())
    if args.missing_ts:
        pick = rng.random(n) < args.missing_ts
        ts[pick] = None
        counts["missing_timestamps"] = int(pick.sum())
    data["timestamp"] = ts

    if args.typos:  # same step written differently
        pick = rng.random(n) < args.typos
        acts = data["activity"].astype(object)
        acts[pick] = [a.lower() + " " for a in acts[pick]]
        data["activity"] = acts
        counts["label_typos"] = int(pick.sum())
    if args.missing_case:
        pick = rng.random(n) < args.missing_case
        cid = data["case_id"].astype(object)
        cid[pick] = None
        data["case_id"] = cid
        counts["missing_case_ids"] = int(pick.sum())
    if args.duplicates:
        dup_idx = np.where(rng.random(n) < args.duplicates)[0]
        for k in data:
            data[k] = np.concatenate([np.asarray(data[k], dtype=object), np.asarray(data[k], dtype=object)[dup_idx]])
        counts["duplicate_rows"] = int(len(dup_idx))
    return counts


def add_actions(data: dict, rng) -> None:
    """Plant a strictly timed chain in forty percent of cases, amid shared UI noise."""
    n = len(data["case_id"])
    data["activity_alias_name"] = np.array([f"View field {x}" for x in rng.integers(0, 4000, n)], dtype=object)
    cases, first = np.unique(data["case_id"], return_index=True)
    selected = rng.choice(first, size=int(len(cases) * 0.4), replace=False)
    indices = np.repeat(selected, 3)
    extra = {k: v[indices].copy() for k, v in data.items()}
    extra["activity"] = np.tile(np.array(["Create Request", "Approve Request", "Approve Request"], dtype=object), len(selected))
    extra["activity_alias_name"] = np.tile(np.array(["Create Tax Data", "Create Benefits", "Click Submit"], dtype=object), len(selected))
    # Before existing case events: works even when original step labels were shuffled.
    extra["timestamp"] = data["timestamp"][indices] - np.tile(np.array([300, 200, 100]), len(selected))
    extra["resource"] = np.array([f"clerk_{i % 8}" for i in range(len(indices))], dtype=object)
    for key in data:
        data[key] = np.concatenate([data[key], extra[key]])
    # A completion action repeated by one user must never qualify.
    rare = np.flatnonzero(data["resource"] == data["resource"][0])[:400]
    data["activity_alias_name"][rare] = "Save Rare Form"


# --step-actions: every step run becomes a few UI actions. Per step a planted standard path
# is followed by ~60% of runs; the rest are planted variations. "Pay Invoice" is shuffled.
STEP_LEVEL3 = {
    "Create Request": "Requisition", "Approve Request": "Requisition", "Reject Request": "Requisition",
    "Create Order": "Purchase order", "Change Price": "Purchase order", "Cancel Order": "Purchase order",
    "Receive Goods": "Goods receipt", "Receive Invoice": "Invoice check", "Block Invoice": "Invoice check",
    "Unblock Invoice": "Invoice check", "Change Vendor Bank": "Invoice check", "Pay Invoice": "Payment",
}
SHUFFLED_STEP = "Pay Invoice"
VARIATIONS = (("Standard", 0.60), ("Standard + repeats", 0.10), ("Extra actions", 0.10),
              ("Missing steps", 0.10), ("Different order", 0.10))


def standard_path(activity: str) -> list[str]:
    return [f"Open {activity}", "Enter details", "Check data", "Click Submit"]


def planted_path(activity: str, kind: str, rng) -> list[str]:
    o, e, c, s = standard_path(activity)
    if activity == SHUFFLED_STEP:
        return list(rng.permutation([o, e, c, s]))
    return {"Standard": [o, e, c, s], "Standard + repeats": [o, e, c, e, c, s],
            "Extra actions": [o, e, "Attach file", c, s], "Missing steps": [o, e, c],
            "Different order": [o, c, e, s]}[kind]


def add_step_actions(data: dict, rng) -> None:
    """Each event becomes one step run of UI actions (activity_alias_name), 30 s apart, with a
    run ID (abstraction_instance_id), process levels 1-3 and active_time (seconds). Noise that
    the app must clean: '#hex#' tokens, a person name as an action, a rare action."""
    n = len(data["case_id"])
    kinds = rng.choice(len(VARIATIONS), size=n, p=[p for _, p in VARIATIONS])
    rows, actions, offsets, kind_of = [], [], [], []
    for i in range(n):
        path = planted_path(str(data["activity"][i]), VARIATIONS[kinds[i]][0], rng)
        if rng.random() < 0.05:
            path = [a + " #9f3c2a1b#" if a == "Check data" else a for a in path]
        if rng.random() < 0.01:
            path = path + ["Priya Sharma"]
        if rng.random() < 0.002:
            path = path + ["Print preview"]
        for j, a in enumerate(path):
            rows.append(i)
            actions.append(a)
            offsets.append(30 * j)
            kind_of.append(VARIATIONS[kinds[i]][0])
    idx = np.array(rows)
    for key in list(data):
        data[key] = np.asarray(data[key])[idx]
    data["timestamp"] = data["timestamp"].astype(np.int64) + np.array(offsets, dtype=np.int64)
    data["activity_alias_name"] = np.array(actions, dtype=object)
    data["abstraction_instance_id"] = np.array([f"AI{i:08d}" for i in idx], dtype=object)
    data["level1_process_name"] = np.full(len(idx), "Purchase to pay", dtype=object)
    data["level2_process_name"] = np.array([STEP_STAGES[a] for a in data["activity"]], dtype=object)
    data["level3_process_name"] = np.array([STEP_LEVEL3[a] for a in data["activity"]], dtype=object)
    data["active_time"] = rng.integers(5, 60, len(idx)).astype(np.int64)


# --level4: inside every Level 3 step, clicks from 5 planted families (12 click names each, 60 per L3)
# visited in a fixed family order; "Payment" visits them in random order. Plus noise.
L4_FAMILIES = (("Tax Data", "SAP"), ("Bank Details", "SAP"), ("Absence Records", "Workday"),
               ("Worker Profile", "Workday"), ("Ticket Notes", "ServiceNow"))
# The window title (screen) of each family: with a #hex# token, a long number, a person name and the app suffix.
L4_SCREENS = ("Maintain Tax Data #9a8b7c6d# - SAP", "Bank Details 4471234 - SAP", "Absence Overview - Workday",
              "Worker Profile - Priya Sharma - Workday", "Compose Email | ServiceNow")
L4_TYPED = ("Hello Team", "Kind Regards, see attached")
L4_VERBS = ("View Create", "View Copy", "View Display", "View Change", "Select", "Open", "Search", "Edit",
            "Check", "Save", "Print", "Review")
L4_SHUFFLED = "Payment"


def add_level4_clicks(data: dict, rng) -> None:
    """Each event becomes 3-10 clicks (activity_alias_name, application, 20 s apart) from the planted
    families of its Level 3 step (level3_process_name); noise: "Hello", a person name inside a click
    ("Select Priya Sharma Profile") and rare one-off clicks."""
    n = len(data["case_id"])
    idx, actions, apps, offsets, titles = [], [], [], [], []
    weights = np.array([8, 6, 5, 4, 3, 3, 2, 2, 1, 1, 1, 1], dtype=float)
    weights /= weights.sum()
    for i in range(n):
        l3 = STEP_LEVEL3[str(data["activity"][i])]
        k = int(rng.integers(3, 6))
        fams = sorted(rng.choice(5, size=k, replace=False))
        if l3 == L4_SHUFFLED:
            fams = list(rng.permutation(fams))
        clicks = []
        for f in fams:
            noun, app = L4_FAMILIES[f]
            for _ in range(int(rng.integers(1, 3))):
                clicks.append((f"{L4_VERBS[rng.choice(12, p=weights)]} {noun}", app, L4_SCREENS[f]))
            if f == 4 and rng.random() < 0.5:                     # typed text in the e-mail: no window title
                clicks.append((L4_TYPED[int(rng.integers(0, 2))], app, ""))
        if rng.random() < 0.05:
            clicks.append(("Hello", "ServiceNow", ""))
        if rng.random() < 0.03:
            clicks.append(("Select Priya Sharma Profile", "Workday", L4_SCREENS[3]))
        if rng.random() < 0.01:
            clicks.append((f"Misc Click {int(rng.integers(1, 99))}", "Excel", "Book1 - Excel"))
        for j, (a, app, title) in enumerate(clicks):
            idx.append(i)
            actions.append(a)
            apps.append(app)
            titles.append(title or None)
            offsets.append(20 * j)
    ix = np.array(idx)
    for key in list(data):
        data[key] = np.asarray(data[key])[ix]
    data["timestamp"] = data["timestamp"].astype(np.int64) + np.array(offsets, dtype=np.int64)
    data["activity_alias_name"] = np.array(actions, dtype=object)
    data["application"] = np.array(apps, dtype=object)
    data["simplified_title"] = np.array(titles, dtype=object)
    data["level1_process_name"] = np.full(len(ix), "Purchase to pay", dtype=object)
    data["level2_process_name"] = np.array([STEP_STAGES[a] for a in data["activity"]], dtype=object)
    data["level3_process_name"] = np.array([STEP_LEVEL3[a] for a in data["activity"]], dtype=object)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cases", type=int, default=20000)
    p.add_argument("--out", type=Path, required=True, help=".parquet or .csv")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--start", default="2025-01-01")
    p.add_argument("--months", type=int, default=18)
    p.add_argument("--ts-format", default=None,
                   help="Write timestamps as text in this format (CSV/Parquet), e.g. '%%d/%%m/%%Y %%H:%%M'")
    for name, helptext in [("missing-ts", "share of events without timestamp"),
                           ("ties", "share of events copying the previous event's time"),
                           ("date-only", "share of events with time set to 00:00:00"),
                           ("placeholder", "share of events dated 1970-01-01"),
                           ("duplicates", "share of rows duplicated"),
                           ("typos", "share of activity labels written differently"),
                           ("missing-case", "share of events without case ID")]:
        p.add_argument(f"--{name}", type=float, default=0.0, help=helptext)
    p.add_argument("--split-by", choices=["none", "day", "month"], default="none",
                   help="Write one file per day or month into the FOLDER --out (a multi-file dataset)")
    p.add_argument("--format", choices=["parquet", "csv"], default="parquet",
                   help="File format of the split files (only used with --split-by)")
    p.add_argument("--apps", action="store_true",
                   help="Add Application_name (some values masked as hashes) and simplified_title columns")
    p.add_argument("--shuffle-order", action="store_true",
                   help="Shuffle the steps inside every case: a log without a fixed order of steps")
    p.add_argument("--stages", action="store_true", help="Add a 'phase' column with the stage of each step")
    p.add_argument("--actions", action="store_true", help="Add UI noise and a completion milestone chain in forty percent of cases")
    p.add_argument("--step-actions", action="store_true",
                   help="Turn every step into a run of UI actions with a planted standard path per step, a run ID, "
                        "process levels 1-3 and an active-time column (sub-process tests)")
    p.add_argument("--level4", action="store_true",
                   help="Turn every step into clicks from 5 planted families per Level 3 step, with an application "
                        "column and process levels 1-3 (Level 4 tests)")
    p.add_argument("--case-column-name", default="case_id",
                   help="Name of the case column (e.g. request_id); default case_id")
    p.add_argument("--ids-in-title", action="store_true",
                   help="Put each case's ticket number (INC0000123) in the window titles, and a masked #hex# "
                        "token in about 40%% of them (creates a window_title column when --apps is not used)")
    p.add_argument("--drop-columns", default="",
                   help="Comma-separated columns to leave out of the file: case, stage, levels, app, action, activity")
    p.add_argument("--batch-days", type=int, default=0,
                   help="Add a 'batch' column grouping the cases that START in the same N-day window: "
                        "a wrong case column whose cases' events interleave")
    args = p.parse_args(argv)

    t = time.perf_counter()
    rng = np.random.default_rng(args.seed + 1)
    data = generate(args.cases, args.seed, args.start, args.months)
    clean_events = len(data["case_id"])
    if args.batch_days:
        add_batch(data, args.batch_days)
    if args.shuffle_order:
        shuffle_order(data, np.random.default_rng(args.seed + 3))
    applications = add_apps(data, np.random.default_rng(args.seed + 2)) if args.apps else {}
    if args.actions:
        add_actions(data, np.random.default_rng(args.seed + 4))
    if args.step_actions:
        add_step_actions(data, np.random.default_rng(args.seed + 5))
    if args.level4:
        add_level4_clicks(data, np.random.default_rng(args.seed + 6))
    if args.ids_in_title:
        add_ids_to_titles(data)
    if args.stages:
        data["phase"] = np.array([STEP_STAGES[a] for a in data["activity"]], dtype=object)
    if args.split_by != "none":
        # Which file each row goes to: decided by the CLEAN time, so rows whose
        # time is damaged or removed below still land in a file. Duplicated rows
        # get a copy of this value too (inject_defects copies every column).
        data["__file_ts"] = data["timestamp"].copy()
    defects = inject_defects(data, args, rng) if any(
        getattr(args, a) for a in ["missing_ts", "ties", "date_only", "placeholder", "duplicates", "typos", "missing_case"]
    ) else {}
    if not defects:
        data["timestamp"] = data["timestamp"].astype("datetime64[s]")
    file_ts = data.pop("__file_ts", None)

    arrays = {}
    for k, v in data.items():
        if k == "timestamp":
            arr = pa.array(list(v) if v.dtype == object else v, type=pa.timestamp("s"))
            if args.ts_format:
                import pyarrow.compute as pc
                arr = pc.strftime(arr, format=args.ts_format)
            arrays["timestamp"] = arr
        elif k == "amount":
            arrays[k] = pa.array(np.asarray(v, dtype=float))
        else:
            arrays[k] = pa.array(list(v) if v.dtype == object else v.astype(str)).dictionary_encode()
    table = pa.table(arrays)
    drops = {d.strip() for d in args.drop_columns.split(",") if d.strip()}
    if drops:
        names = {"case": {"case_id"}, "stage": {"phase", "stage"}, "app": {"Application_name"},
                 "action": {"action", "activity_alias_name"}, "activity": {"activity"},
                 "levels": {c for c in table.column_names if c.lower().startswith("level")}}
        gone = set().union(*(names.get(d, set()) for d in drops))
        table = table.drop_columns([c for c in table.column_names if c in gone])
    if args.case_column_name != "case_id" and "case_id" in table.column_names:
        table = table.rename_columns([args.case_column_name if c == "case_id" else c for c in table.column_names])

    con = duckdb.connect()
    if args.split_by == "none":
        args.out.parent.mkdir(parents=True, exist_ok=True)
        con.register("t", table)
        out = args.out.as_posix().replace("'", "''")
        fmt = "csv" if args.out.suffix.lower() == ".csv" else "parquet"
        con.execute(f"COPY (SELECT * FROM t) TO '{out}' (FORMAT {fmt})")
        files_written = 1
        truth_path = Path(str(args.out) + ".truth.json")
    else:
        files_written = write_split(con, table, file_ts, args.out, args.split_by, args.format)
        truth_path = args.out / "truth.json"

    truth = {
        "cases": args.cases,
        "clean_events": clean_events,
        "rows_written": table.num_rows,
        "variants": [{"probability": p_, "path": acts} for p_, acts in VARIANTS],
        "defects_injected": defects,
        "timestamp_format": args.ts_format or "native",
        "seed": args.seed,
        "split_by": args.split_by,
        "applications": applications,   # masked value -> real application (with --apps)
        "app_switch_step": SWITCH_STEP if args.apps else None,   # where users switch Workday <-> Excel
        "files_written": files_written,
    }
    with open(truth_path, "w", encoding="utf-8") as f:
        json.dump(truth, f, indent=2)
    print(f"Wrote {table.num_rows:,} rows ({args.cases:,} cases) to {args.out} "
          f"({files_written:,} file{'s' if files_written != 1 else ''}) "
          f"in {time.perf_counter() - t:.1f}s. Defects: {defects or 'none'}")
    return 0


def write_split(con, table: pa.Table, file_ts, folder: Path, split_by: str, fmt: str) -> int:
    """Write one file per day or month (events_2025_01_15.parquet / events_2025_01.csv).

    `file_ts` holds each row's clean time in epoch seconds and decides its file.
    Returns the number of files written.
    """
    unit = "D" if split_by == "day" else "M"
    keys = (np.asarray(file_ts, dtype="int64").astype("datetime64[s]")
            .astype(f"datetime64[{unit}]").astype(str))
    keys = np.char.replace(keys, "-", "_")
    con.register("t", table.append_column("__file", pa.array(keys)))
    folder.mkdir(parents=True, exist_ok=True)
    buckets = sorted(set(keys.tolist()))
    for key in buckets:
        target = (folder / f"events_{key}.{fmt}").as_posix().replace("'", "''")
        con.execute(f"COPY (SELECT * EXCLUDE (__file) FROM t WHERE __file = '{key}') "
                    f"TO '{target}' (FORMAT {fmt})")
    return len(buckets)


if __name__ == "__main__":
    sys.exit(main())
