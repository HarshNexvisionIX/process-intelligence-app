"""The "Without case ID" menu section (app/main.py adds it after the existing pages, which are unchanged)."""
import streamlit as st

SECTION = "Without case ID"


def nocase_pages() -> list:
    return [
        st.Page("ui/nocase_overview.py", title="Clickstream mapping", icon="🖱️", url_path="clickstream"),
        st.Page("ui/nocase_map.py", title="Clickstream map", icon="🗺️", url_path="clickstream-map"),
        st.Page("ui/nocase_report.py", title="Clickstream report", icon="📄", url_path="clickstream-report"),
        st.Page("ui/nocase_review_steps.py", title="Review steps", icon="🧩", url_path="review-steps"),
        st.Page("ui/nocase_review_activities.py", title="Review activities", icon="🗂️", url_path="review-activities"),
        st.Page("ui/nocase_review_order.py", title="Review order", icon="↕️", url_path="review-order"),
        st.Page("ui/nocase_questions.py", title="SME questions", icon="💬", url_path="sme-questions"),
        st.Page("ui/nocase_pack.py", title="Process pack", icon="📚", url_path="process-pack"),
    ]
