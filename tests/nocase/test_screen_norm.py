"""Prompt D: universal screen normaliser, verb + object fallback labels, evidence "n/a" without steps,
DayWindow only as a last resort.

Run with:  pytest -q tests/nocase/test_screen_norm.py
"""
import pandas as pd
import pytest

from app.nocase.clean import screen_label, screen_label_with_source


@pytest.mark.parametrize("title, app, want", [
    ("Cost_Model_AB01_CD02_Jul26.xlsx - Excel", "Excel", "Cost Model"),          # file name with codes + period
    ("XY12_export_Aug26.xlsx - Excel", "Excel", "XY12 export"),                   # a leading code names the screen
    ("Drivers_AB01_Jul26.xlsx - Excel", "Excel", "Drivers"),
    ("Account Matching - AB01 vs CD02 - Microsoft Edge", "Matcher", "Account Matching"),   # not the codes
    ("Cost approval AB01-CD02 Aug26", "Outlook", "Cost approval"),                # periods and code pairs masked
    ("RE: Headcount drivers Aug26 - AB01", "Outlook", "RE: Headcount drivers"),
    ("Document 1900012345 was posted in company code AB01", "Ledger", "Document was posted in company code"),
    ("Allocation Drivers Sep26 - SharePoint - Microsoft Edge", "SharePoint", "Allocation Drivers"),
    ("Close Pack 2026-07.xlsx - Excel", "Excel", "Close Pack"),
    ("Status report Q3 FY26", "Word", "Status report"),
    ("View Worker - Ayakha Ningiza - Workday", "Workday", "View Worker"),          # existing behaviour kept
    ("PHR0263894 - Case - ServiceNow", "ServiceNow", "Case"),
    ("Payment Run INV-500007 - Oracle", "Oracle", "Payment Run"),
])
def test_titles_become_stable_screens(title, app, want):
    assert screen_label(title, app) == want


def test_one_screen_per_template_not_per_month_or_code():
    titles = [f"Cost approval {a}-{b} {m}" for a in ("AB01", "CD02", "EF03") for b in ("GH04", "IJ05")
              for m in ("Jul26", "Aug26", "Sep26")]
    assert {screen_label(t, "Outlook") for t in titles} == {"Cost approval"}


@pytest.mark.parametrize("title", ["Chat | Finance Ops", "Intranet - Finance Policies - Google Chrome",
                                   "AB01_CD02_Jul26.xlsx - Excel", "12345678", "Untitled - Notepad", "x"])
def test_a_non_empty_title_never_gives_none(title):
    label, source = screen_label_with_source(title, "Some App")
    assert label and source in ("core", "normalised", "fallback")


def test_empty_title_stays_empty():
    assert screen_label_with_source(None, "App") == (None, None)
    assert screen_label_with_source("   ", "App") == (None, None)


def test_fallback_never_returns_a_person_name():
    label = screen_label("Chat | Ayakha Ningiza", "Teams")
    assert "Ayakha" not in (label or "")


def test_fallback_activity_labels_are_verb_and_object():
    from app.nocase.engine.activities import fallback_groups
    u = pd.DataFrame({"app": ["Excel", "Excel", "Matcher", "Mail", "Ledger"],
                      "screen_clean": ["Cost Model", "Cost Model", "Account Matching", "Cost approval",
                                       "AB12 Enter G/L Account Document"],
                      "action_clean": ["Type <typed text>", "Paste", "Click Match", "Click Send", "Click Post"],
                      "events": [50, 40, 30, 20, 10]})
    labels = [g["label"] for g in fallback_groups(u)]
    assert labels[0] == "Update cost model"
    assert "Send cost approval" in labels
    assert not any(lbl.startswith("Work in") or "AB12" in lbl for lbl in labels)


def test_evidence_scores_are_na_without_steps():
    from app.nocase.ai.evidence import score_signal
    ev = pd.DataFrame({"l3": [None] * 10, "screen_clean": ["A"] * 5 + ["B"] * 5, "action_clean": ["x"] * 10,
                       "episode_id": ["E1"] * 10, "ts": pd.date_range("2026-01-01", periods=10, freq="min"),
                       "eid": range(10)})
    for sig in ("screen", "action", "episode_sequence", "co_occurrence"):
        s = score_signal(sig, ev, {})
        assert s["score"] is None and s["reason"].startswith("n/a")
