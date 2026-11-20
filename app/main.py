"""App entry point. Run from the project root:

    streamlit run app/main.py
"""
import sys
from pathlib import Path

# Make `import app.core...` work when Streamlit runs this file directly.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402
from app import __version__  # noqa: E402

st.set_page_config(page_title="ProcessLens", page_icon=":material/account_tree:", layout="wide",
                   initial_sidebar_state="expanded")

# Startup checks stay read-only. A misconfigured AWS profile is a hard startup error.
from app.core.config import startup_status  # noqa: E402
try:
    deployment = startup_status()
except ValueError as exc:
    st.error(str(exc))
    st.stop()
if not st.session_state.get("deployment_status_logged"):
    private_count = sum(bool(p["private"]) for p in deployment["providers"])
    print(f"ProcessLens profile={deployment['profile']} data_dir={deployment['data_dir']} "
          f"private_ai_routes={private_count}/{len(deployment['providers'])}")
    for provider in deployment["providers"]:
        print(f"  AI route {provider['task']} -> {provider['provider']} "
              f"private={'✓' if provider['private'] else '✗'}")
    st.session_state["deployment_status_logged"] = True
st.session_state["deployment"] = deployment          # shown on Settings → System

# Privacy status: computed once per session from the settings, no network call.
if "privacy_line" not in st.session_state:
    from app.core.privacy import status_line  # noqa: E402
    st.session_state["privacy_line"] = status_line()

# The shell (app/ui/shell.py): one current analysis, the menu that fits its mode, the sidebar.
from app.ui import shell  # noqa: E402
job = shell.current_job()
nav = shell.navigation(job)
goto = st.session_state.pop("goto", None)       # a page asked to open another one after changing the analysis
if goto:
    st.switch_page(shell.st_page(goto))
footer = " · ".join([x for x in st.session_state["privacy_line"].split(" · ") if x.startswith("AI")]
                    + [f"v{__version__}"])
shell.sidebar(job, footer)
from app.core.privacy import external_for_client  # noqa: E402
if external_for_client():
    st.sidebar.warning("Client datasets may be sent to AI providers that are not private "
                       "(ai.allow_external_for_client is on).")

nav.run()
