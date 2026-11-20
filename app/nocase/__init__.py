"""No case ID mode ("clickstream mapping"): work with desktop / clickstream events that have no usable case ID.

Never invents a case: event order, the user or closeness in time is not a case. Case analysis only comes
from a real ID that passes the case-link tests. Code computes every number; the AI (later prompts) only
proposes labels and rules from masked summaries.

Files written in the job folder:
    nc_events.parquet         the standard event table (raw text / screen kept only here)  ingest.py
    nc_scope.json             rows in file -> left out -> unreadable time -> in scope         ingest.py
    nc_capabilities.json      what the data supports, measured                                profile.py
    nc_events_clean.parquet   cleaned screen / action, is_generic, unit_id                    clean.py, units.py
    nc_units.parquet          one row per (app, screen_clean, action_clean)                   units.py
"""
