"""Deterministic no-case test datasets (2-5k rows each) and their mappings (tests/nocase/mappings/*.yaml).

    hr_like   step labels on ~85% of rows, active / wait times, run IDs; the true case only under eval.case_id;
              PHR9999999 tickets in ~30% of titles, all in the ServiceNow-like app; person names in titles.
    icc_like  no labels; a global sequence counter (mapped as case_id, as a user might); cross-skilled actors
              switching between two processes in a day; many generic clicks; month-end concentration;
              eval.l3 and eval.case_id hold the truth.
    ap_like   INV-999999 invoice numbers in ~70% of titles; eval.case_id.
    web_sessions  page views only (no screens, no clicks); the session is truth.
    rpa_ui        one user copying between a sheet and a web form; the routine is truth.
    erp_docs      document numbers in a text attribute, no actor column; the purchase order is truth.

Usage:  path = make("hr_like", tmp_path); mapping = load_mapping("hr_like")
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from app.nocase.canonical import NcMapping

MAPPINGS = Path(__file__).parent / "mappings"
PEOPLE = ["Ayakha Ningiza", "Priya Sharma", "John Smith", "Maria Garcia"]


def load_mapping(name: str) -> NcMapping:
    return NcMapping.from_yaml(MAPPINGS / f"{name}.yaml")


# --------------------------------------------------------------------------- hr_like
HR_STEPS = {
    "Offer Processing": [("Workday", "View Worker", ["View Worker", "Update Address", "Select Search"]),
                         ("Excel", "Offers_Team_Web_Tool_Testing-v1.xlsx", ["Update Offer", "Copy", "Paste"]),
                         ("ServiceNow", "Case", ["Open Case", "Work Notes", "Core Activity"])],
    "Payroll Change": [("Workday", "Worker Profile", ["View Worker Profile", "Edit Bank Details"]),
                       ("ServiceNow", "Incident", ["Open Incident", "Resolve", "Click"])],
    "Onboarding": [("Workday", "Hire", ["Hire Employee", "Assign Manager"]),
                   ("Outlook", "Inbox", ["Compose Email", "Send", "Hello team, please see below."])],
}


def hr_like(rnd: random.Random) -> pd.DataFrame:
    rows, t0, ticket_no = [], datetime(2026, 3, 2, 8, 0), 263894
    run = 0
    for c in range(220):
        case_id = f"HRC{c:05d}"
        l3 = rnd.choice(list(HR_STEPS))
        actor = f"P{rnd.randint(1, 8)}"
        t = t0 + timedelta(hours=c * 3 + rnd.randint(0, 2))
        ticket = f"PHR0{ticket_no + c:06d}"
        for app, screen, actions in HR_STEPS[l3]:
            run += 1
            for k in range(rnd.randint(3, 6)):
                person = rnd.choice(PEOPLE)
                if app == "ServiceNow":
                    title = f"{ticket} - {screen} - ServiceNow"
                elif app == "Workday":
                    title = f"{screen} - {person} - Workday"
                elif app == "Excel":
                    title = f"{screen} - Excel"
                else:
                    title = f"{screen} - Outlook"
                act = rnd.choice(actions)
                rows.append({
                    "date_time": t.strftime("%Y-%m-%d %H:%M:%S"), "participant_id": actor,
                    "Application_name": app, "simplified_title": title.rsplit(" - ", 1)[0] if app != "ServiceNow"
                    else f"{screen}", "title": title, "activity_alias_name": act,
                    "level2_process_name": "HR Services",
                    "level3_process_name": l3 if rnd.random() < 0.85 else None,
                    "active_time": float(rnd.randint(5, 120)), "wait_time": float(rnd.randint(0, 60)),
                    "abstraction_instance_id": f"R{run:06d}", "is_process": "1" if rnd.random() < 0.8 else "0",
                    "case_id": case_id})
                t += timedelta(seconds=rnd.randint(10, 90))
    rows.append({**rows[0], "date_time": "not a time", "is_process": "1"})       # one unreadable time
    rows.append({**rows[1], "date_time": "", "is_process": "1"})                 # and an empty one
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- icc_like
ICC = {"Invoice Check": [("SAP", "Invoice Display", ["Display Invoice", "Check Amount"]),
                         ("SAP", "Vendor Master", ["Display Vendor", "Click"])],
       "Credit Note": [("Outlook", "Inbox", ["Open Mail", "Reply", "Click"]),
                       ("SAP", "Credit Memo", ["Create Credit Memo", "Save"])]}
GENERIC = ["Click", "Select", "OK", "Back", "Scroll", "Next"]


def icc_like(rnd: random.Random) -> pd.DataFrame:
    rows, case = [], 0
    days = [datetime(2026, 1, d) for d in range(5, 31) if datetime(2026, 1, d).weekday() < 5]
    for day in days:
        busy = 3 if day.day >= 26 else 1                       # month-end concentration
        for actor in ("A1", "A2", "A3", "A4", "A5"):
            t = day.replace(hour=8, minute=rnd.randint(0, 30))
            for _ in range(busy * 3):
                case += 1
                l3 = rnd.choice(list(ICC))                       # cross-skilled: both processes in a day
                for app, screen, actions in ICC[l3]:
                    for _k in range(rnd.randint(2, 4)):
                        act = rnd.choice(GENERIC) if rnd.random() < 0.45 else rnd.choice(actions)
                        rows.append({"time": t, "user": actor, "application": app, "window": f"{screen} - {app}",
                                     "click": act, "truth_l3": l3, "truth_case": f"K{case:05d}"})
                        t += timedelta(seconds=rnd.randint(5, 60))
    df = pd.DataFrame(rows).sort_values(["time", "user"], kind="stable").reset_index(drop=True)
    df["seq"] = range(100001, 100001 + len(df))                  # a global event counter
    df["time"] = df["time"].dt.strftime("%d/%m/%Y %H:%M:%S")
    return df


# --------------------------------------------------------------------------- ap_like
def ap_like(rnd: random.Random) -> pd.DataFrame:
    rows, t = [], datetime(2026, 2, 2, 9, 0)
    for c in range(400):
        inv = f"INV-{500000 + c * 7:06d}"
        actor = f"U{rnd.randint(1, 5)}"
        for screen, act in (("Invoice Entry", "Enter Invoice"), ("Approval", "Approve Invoice"),
                            ("Payment Run", "Post Payment")):
            for _k in range(rnd.randint(1, 3)):
                title = f"{screen} {inv} - Oracle" if rnd.random() < 0.7 else f"{screen} - Oracle"
                rows.append({"timestamp": t.isoformat(), "user": actor, "app": "Oracle", "title": title,
                             "action": act, "invoice_truth": inv})
                t += timedelta(seconds=rnd.randint(20, 300))
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- web_sessions
def web_sessions(rnd: random.Random) -> pd.DataFrame:
    """Page views only: no screens, no clicks. The session is truth (never an input)."""
    pages = ["/home", "/search", "/product", "/basket", "/checkout", "/help", "/account"]
    rows, t = [], datetime(2026, 4, 1, 7, 0)
    for s in range(450):
        visitor = f"V{rnd.randint(1, 120):04d}"
        for _k in range(rnd.randint(2, 9)):
            rows.append({"viewed_at": t.isoformat(), "visitor": visitor, "page": rnd.choice(pages),
                         "session_truth": f"S{s:05d}"})
            t += timedelta(seconds=rnd.randint(5, 120))
        t += timedelta(minutes=rnd.randint(1, 30))
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- rpa_ui
def rpa_ui(rnd: random.Random) -> pd.DataFrame:
    """One user copying values from a spreadsheet into a web form, again and again; routine label is truth."""
    rows, t = [], datetime(2026, 5, 4, 9, 0)
    for r in range(260):
        for app, target, action in (("Excel", "Students.xlsx - Sheet1", "Select Cell"), ("Excel", "Students.xlsx - Sheet1", "Copy"),
                                    ("Chrome", "Enrolment form - Chrome", "Click Field"),
                                    ("Chrome", "Enrolment form - Chrome", "Paste"),
                                    ("Chrome", "Enrolment form - Chrome", "Click Submit" if rnd.random() < 0.9 else "Click Cancel")):
            rows.append({"timestamp": t.strftime("%Y-%m-%d %H:%M:%S"), "user": "operator", "app": app,
                         "target": target, "event": action, "routine_truth": f"R{r:04d}"})
            t += timedelta(seconds=rnd.randint(1, 8))
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- erp_docs
def erp_docs(rnd: random.Random) -> pd.DataFrame:
    """ERP document events without an actor column: document numbers in a text attribute; the order is truth."""
    rows, t = [], datetime(2026, 6, 1, 6, 0)
    for o in range(500):
        po = f"45{1000000 + o * 13:08d}"
        for act in ("Create Purchase Order", "Record Goods Receipt", "Record Invoice Receipt", "Clear Invoice")[: rnd.randint(2, 4)]:
            rows.append({"event_time": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "activity": act,
                         "document_text": f"PO {po} item 00010", "order_truth": po})
            t += timedelta(minutes=rnd.randint(5, 600))
    return pd.DataFrame(rows)


BUILDERS = {"hr_like": (hr_like, "parquet", 1), "icc_like": (icc_like, "csv", 2), "ap_like": (ap_like, "csv", 3),
            "web_sessions": (web_sessions, "csv", 4), "rpa_ui": (rpa_ui, "parquet", 5), "erp_docs": (erp_docs, "csv", 6)}
SHAPES = tuple(BUILDERS)


def make(name: str, folder: Path) -> Path:
    """Write the dataset into `folder`; returns the file path (the same rows on every call)."""
    build, ext, seed = BUILDERS[name]
    df = build(random.Random(seed))
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.{ext}"
    if ext == "csv":
        df.to_csv(path, index=False)
    else:
        df.to_parquet(path, index=False)
    return path


def icc_like_paused(folder: Path) -> Path:
    """icc_like with a 12-25 minute pause between pieces of work (and none inside one): ActorGap (30 min) does not
    split there, ActorAppContext (a new app / screen after over 10 minutes) does. Same mapping as icc_like."""
    rnd = random.Random(12)
    rows, case = [], 0
    for day in [datetime(2026, 1, d) for d in range(5, 10)]:
        for actor in ("A1", "A2", "A3"):
            t = day.replace(hour=8)
            for _ in range(6):
                case += 1
                l3 = rnd.choice(list(ICC))
                for app, screen, actions in ICC[l3]:
                    for _k in range(rnd.randint(2, 4)):
                        rows.append({"time": t, "user": actor, "application": app, "window": f"{screen} - {app}",
                                     "click": rnd.choice(actions), "truth_l3": l3, "truth_case": f"K{case:05d}"})
                        t += timedelta(seconds=rnd.randint(5, 60))
                t += timedelta(minutes=rnd.randint(12, 25))
    df = pd.DataFrame(rows).sort_values(["time", "user"], kind="stable").reset_index(drop=True)
    df["seq"] = range(1, len(df) + 1)
    df["time"] = df["time"].dt.strftime("%d/%m/%Y %H:%M:%S")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "icc_like_paused.csv"
    df.to_csv(path, index=False)
    return path
