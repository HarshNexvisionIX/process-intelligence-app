"""The app shell: one current analysis for the whole app, the menu that fits it, a consistent page header with the
workflow steps, and the sidebar.

Every page used to have its own "Job" dropdown, the menu showed the case-ID and the clickstream pages side by
side, and the sidebar listed technical details. Now:

    * the current analysis is chosen once, in the sidebar (or by opening / converting one), and every page uses it
    * the menu shows only the workflow that fits that analysis: Process mining (with a case ID) or Clickstream
      (no case ID); the shared pages (Import data, Analyses, Process library, Settings) are always there
    * each workflow page starts with the same header: title, one line on what the page is for, the workflow
      steps with what is done, and the next step as a link
    * technical details (profile, data folder, AI routes) moved to Settings → System
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import streamlit as st

from app.core import jobs

# --------------------------------------------------------------------------- #
# Pages (one registry, used by the menu, the steps and the "next" links)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PageDef:
    key: str
    file: str
    title: str
    icon: str
    url: str


PAGES = {
    # shared
    "import": PageDef("import", "ui/upload_page.py", "Import data", ":material/upload_file:", "import"),
    "analyses": PageDef("analyses", "ui/jobs_page.py", "Analyses", ":material/folder_open:", "analyses"),
    "library": PageDef("library", "ui/nocase_pack.py", "Process library", ":material/library_books:", "library"),
    "settings": PageDef("settings", "ui/ai_settings_page.py", "Settings", ":material/settings:", "settings"),
    "context": PageDef("context", "ui/context_page.py", "Business context", ":material/menu_book:",
                       "business-context"),
    # process mining (case ID)
    "quality": PageDef("quality", "ui/quality_page.py", "Data quality", ":material/fact_check:", "quality"),
    "analysis": PageDef("analysis", "ui/map_page.py", "Process analysis", ":material/account_tree:", "process-map"),
    "definition": PageDef("definition", "ui/business_page.py", "Process definition", ":material/rule:",
                          "business-process"),
    "report": PageDef("report", "ui/report_page.py", "Report", ":material/description:", "report"),
    # clickstream (no case ID)
    "nc_overview": PageDef("nc_overview", "ui/nocase_overview.py", "Overview and analysis", ":material/insights:",
                           "clickstream"),
    "nc_map": PageDef("nc_map", "ui/nocase_map.py", "Process map", ":material/account_tree:", "clickstream-map"),
    "nc_steps": PageDef("nc_steps", "ui/nocase_review_steps.py", "Review steps", ":material/checklist:",
                        "review-steps"),
    "nc_activities": PageDef("nc_activities", "ui/nocase_review_activities.py", "Review activities",
                             ":material/category:", "review-activities"),
    "nc_order": PageDef("nc_order", "ui/nocase_review_order.py", "Review step order", ":material/swap_vert:",
                        "review-order"),
    "nc_questions": PageDef("nc_questions", "ui/nocase_questions.py", "Open questions", ":material/help:",
                            "sme-questions"),
    "nc_report": PageDef("nc_report", "ui/nocase_report.py", "Report", ":material/description:",
                         "clickstream-report"),
}

# The workflow steps per mode: (step label, page key). Review in clickstream mode is one step with four pages.
WORKFLOWS = {
    "case": [("Import", "import"), ("Business context", "context"), ("Check data", "quality"), ("Analyse", "analysis"),
             ("Define process", "definition"), ("Report", "report")],
    "nocase": [("Import", "import"), ("Business context", "context"), ("Analyse", "nc_overview"), ("Process map", "nc_map"),
               ("Review", "nc_steps"), ("Report", "nc_report")],
}
REVIEW_PAGES = ["nc_steps", "nc_activities", "nc_order", "nc_questions"]
MODE_LABEL = {"case": "Process mining · case ID", "nocase": "Clickstream · no case ID"}


_st_pages: dict[str, object] = {}


def st_page(key: str, default: bool = False):
    """One st.Page object per page (the same object must be used in st.navigation and st.page_link)."""
    if key not in _st_pages:
        p = PAGES[key]
        _st_pages[key] = st.Page(p.file, title=p.title, icon=p.icon, url_path=p.url, default=default)
    return _st_pages[key]


# --------------------------------------------------------------------------- #
# The current analysis
# --------------------------------------------------------------------------- #


def mode_of(job: dict | None) -> str:
    return "nocase" if (job or {}).get("mode") == "nocase" else "case"


def _label(job: dict) -> str:
    name = job.get("source_name") or "upload"
    when = (job.get("created_at") or job["job_id"])[:16].replace("T", " ")
    return f"{name} · {when}"


def all_jobs() -> list[dict]:
    try:
        return jobs.list_jobs()
    except FileNotFoundError:
        return []


def current_job() -> dict | None:
    """The analysis the app is working on (session state), or the most recent one."""
    job = st.session_state.get("job")
    if job and (jobs.job_folder(job["job_id"]) / "job.json").exists():
        return job
    found = all_jobs()
    if found:
        st.session_state["job"] = jobs.load_job(found[0]["job_id"])
        return st.session_state["job"]
    return None


def set_current(job_id: str) -> None:
    st.session_state["job"] = jobs.load_job(job_id)


def pick_job(candidates: list[dict], key: str, need: str) -> str | None:
    """The page's analysis: the current one when it fits this page. Otherwise one short message and a choice
    among the analyses that fit (picking one makes it current for the whole app). None when none fits."""
    cur = current_job()
    ids = [j["job_id"] for j in candidates]
    if cur and cur["job_id"] in ids:
        return cur["job_id"]
    if not candidates:
        return None
    st.info(f"The current analysis {('(' + _label(cur) + ') ') if cur else ''}is not ready for this page: {need}. "
            "Choose one that is, or finish the earlier step first.")
    choice = st.selectbox("Analysis", ids, key=f"pick_{key}", index=None, placeholder="Choose an analysis",
                          format_func=lambda i: _label(next(j for j in candidates if j["job_id"] == i)))
    if choice:
        set_current(choice)
        st.rerun()
    return None


# --------------------------------------------------------------------------- #
# What is done for an analysis
# --------------------------------------------------------------------------- #


def _exists(folder: Path, *names: str) -> bool:
    return any((folder / n).exists() for n in names)


def step_status(job: dict | None) -> dict[str, bool]:
    """page key -> done, for the workflow of the job's mode (rules in app/core/workflow.py, shared with the web UI)."""
    from app.core.workflow import step_status as _status
    return _status(job)


def context_done(job: dict | None) -> bool:
    """Business context is complete (rules in app/core/workflow.py)."""
    from app.core.workflow import context_done as _done
    return _done(job)


def require_context(job: dict | None, what: str) -> bool:
    """Mandatory step: stop `what` (e.g. "the report") until Business context is complete. True when complete."""
    if context_done(job):
        return True
    st.warning(f"**Business context is required before {what}.** Name the process and add its documents "
               "(SOPs, transcripts, decks), or record that there are none.", icon=":material/menu_book:")
    st.page_link(st_page("context"), label="Go to Business context", icon=":material/arrow_forward:")
    return False


def next_step(job: dict | None) -> tuple[str, str] | None:
    """(step label, page key) of the first step not done."""
    if not job:
        return ("Import", "import")
    done = step_status(job)
    for label, key in WORKFLOWS[mode_of(job)]:
        if not done.get(key):
            return label, key
    return None


# --------------------------------------------------------------------------- #
# Page header and the workflow steps
# --------------------------------------------------------------------------- #


def _stepper_html(job: dict | None, here: str) -> str:
    mode = mode_of(job)
    done = step_status(job)
    here_step = "nc_steps" if here in REVIEW_PAGES else here
    parts = []
    for n, (label, key) in enumerate(WORKFLOWS[mode], start=1):
        if key == here_step:
            cls, mark = "pm-step pm-here", str(n)
        elif done.get(key):
            cls, mark = "pm-step pm-done", "✓"
        else:
            cls, mark = "pm-step", str(n)
        parts.append(f'<span class="{cls}"><span class="pm-dot">{mark}</span>{label}</span>')
    return '<div class="pm-steps">' + '<span class="pm-line"></span>'.join(parts) + "</div>"


def header(title: str, purpose: str, here: str | None = None) -> dict | None:
    """The page header. `here` is the page key when the page is a workflow step. Returns the current job."""
    style()
    job = current_job()
    st.markdown(f'<div class="pm-title">{title}</div><div class="pm-purpose">{purpose}</div>',
                unsafe_allow_html=True)
    if here and (job or here == "import"):
        st.markdown(_stepper_html(job, here), unsafe_allow_html=True)
    return job


def next_link(job: dict | None, after: str | None = None) -> None:
    """A "Next step" link to the first workflow step that is not done (shown at the end of a page)."""
    nxt = next_step(job)
    if not nxt or nxt[1] == after:
        return
    label, key = nxt
    st.page_link(st_page(key), label=f"Next step: {label}", icon=":material/arrow_forward:")


# --------------------------------------------------------------------------- #
# Look and feel
# --------------------------------------------------------------------------- #

_CSS = """
<style>
:root { --pm-ink:#14323f; --pm-muted:#5b6b75; --pm-line:#d7dee3; --pm-accent:#0f5f7a; --pm-done:#2c7a4b; }
[data-testid="stMainBlockContainer"] { padding-top: 2.2rem; max-width: 1280px; }
.pm-title { font-size: 1.75rem; font-weight: 650; color: var(--pm-ink); letter-spacing: -0.01em; line-height: 1.2; }
.pm-purpose { color: var(--pm-muted); font-size: 0.98rem; margin: 0.25rem 0 0.9rem 0; max-width: 72ch; }
.pm-steps { display:flex; align-items:center; flex-wrap:wrap; gap:0; margin: 0.2rem 0 1.4rem 0;
            padding: 0.55rem 0.8rem; background:#f5f8fa; border:1px solid var(--pm-line); border-radius:10px; }
.pm-step { display:inline-flex; align-items:center; gap:0.45rem; font-size:0.88rem; color:var(--pm-muted);
           white-space:nowrap; }
.pm-dot { display:inline-flex; align-items:center; justify-content:center; width:1.45rem; height:1.45rem;
          border-radius:50%; border:1.5px solid var(--pm-line); font-size:0.75rem; font-weight:600; background:#fff; }
.pm-done { color: var(--pm-ink); }
.pm-done .pm-dot { background: var(--pm-done); border-color: var(--pm-done); color:#fff; }
.pm-here { color: var(--pm-accent); font-weight:600; }
.pm-here .pm-dot { background: var(--pm-accent); border-color: var(--pm-accent); color:#fff; }
.pm-line { flex: 1 1 1.2rem; min-width: 1.2rem; height:1.5px; background: var(--pm-line); margin: 0 0.6rem; }
.pm-side-title { font-weight:650; font-size:1.02rem; color:var(--pm-ink); margin-bottom:0.1rem; }
.pm-side-mode { display:inline-block; font-size:0.74rem; padding:0.1rem 0.5rem; border-radius:999px;
                background:#e3eef3; color:var(--pm-accent); margin: 0.15rem 0 0.4rem 0; }
.pm-side-status { font-size:0.78rem; color:var(--pm-muted); margin-top:1.2rem; }
.pm-side-head { font-size:0.72rem; text-transform:uppercase; letter-spacing:0.07em; color:var(--pm-muted);
                margin: 0.9rem 0 0.2rem 0.2rem; font-weight:600; }
[data-testid="stSidebar"] [data-testid="stPageLink"] a { padding-top: 0.15rem; padding-bottom: 0.15rem; }
.st-key-pm_sub { padding-left: 1.3rem; gap: 0 !important; }
.st-key-pm_sub [data-testid="stPageLink"] p { font-size: 0.86rem; }
[data-testid="stSidebarUserContent"] { padding-top: 0.6rem; }
[data-testid="stMetricValue"] { font-size: 1.7rem; }
h1, h2, h3 { color: var(--pm-ink); letter-spacing: -0.01em; }
</style>
"""


def style() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# Sidebar and menu (called from app/main.py)
# --------------------------------------------------------------------------- #


def navigation(job: dict | None):
    """Every page of the current analysis' mode plus the workspace pages. The menu itself is drawn by sidebar()."""
    mode = mode_of(job)
    keys = [k for _, k in WORKFLOWS[mode]] + (REVIEW_PAGES[1:] if mode == "nocase" else [])
    keys += ["analyses", "library", "settings"]
    return st.navigation([st_page(k, default=(k == "import")) for k in keys], position="hidden")


def _picked() -> None:
    set_current(st.session_state["shell_job"])


def sidebar(job: dict | None, footer: str) -> None:
    """Title, current analysis, the numbered workflow (✓ when done), the workspace pages, a status line."""
    style()
    found = all_jobs()
    mode = mode_of(job)
    done = step_status(job)
    with st.sidebar:
        st.markdown('<div class="pm-side-title">ProcessLens</div>', unsafe_allow_html=True)
        if found:
            ids = [j["job_id"] for j in found]
            cur = job["job_id"] if job and job["job_id"] in ids else ids[0]
            # The box always shows the current analysis: a page may have changed it (a new import, Open on
            # Analyses). Only a real pick by the user (on_change) changes the current analysis; comparing the
            # box's remembered value with the current one would switch back to the old analysis on the next click.
            st.session_state["shell_job"] = cur
            st.selectbox("Current analysis", ids, key="shell_job", on_change=_picked,
                         format_func=lambda i: _label(next(j for j in found if j["job_id"] == i)))
            st.markdown(f'<span class="pm-side-mode">{MODE_LABEL[mode]}</span>', unsafe_allow_html=True)
        else:
            st.caption("No analysis yet: start with Import data.")
        st.markdown('<div class="pm-side-head">Workflow</div>', unsafe_allow_html=True)
        for n, (label, key) in enumerate(WORKFLOWS[mode], start=1):
            tick = "  ✓" if done.get(key) else ""
            st.page_link(st_page(key), label=f"{n}. {PAGES[key].title}{tick}", icon=PAGES[key].icon)
            if key == "nc_steps":
                with st.container(key="pm_sub"):          # indented under "Review steps" (CSS .st-key-pm_sub)
                    for sub in REVIEW_PAGES[1:]:
                        st.page_link(st_page(sub), label=PAGES[sub].title, icon=PAGES[sub].icon)
        st.markdown('<div class="pm-side-head">Workspace</div>', unsafe_allow_html=True)
        for key in ("analyses", "library", "settings"):
            st.page_link(st_page(key), label=PAGES[key].title, icon=PAGES[key].icon)
        st.markdown(f'<div class="pm-side-status">{footer}</div>', unsafe_allow_html=True)
