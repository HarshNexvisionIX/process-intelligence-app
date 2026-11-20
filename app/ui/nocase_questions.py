"""SME questions and evidence decisions for no-case jobs."""
from __future__ import annotations

from datetime import datetime, timezone

import streamlit as st

from app.nocase.ai.evidence import PLAN_FILE
from app.ui.nocase_review_common import page_context

ctx = page_context("Open questions")
if ctx:
    import json
    from app.nocase.knowledge.library import _log, process_dir
    from app.ui.nocase_review_common import all_questions
    by = st.text_input("Your name", key="SME questions_by")
    qs = all_questions(ctx["folder"], ctx["process"])
    open_ = [q for q in qs if not q["answer"]]
    st.caption(f"{len(open_)} open · {len(qs) - len(open_)} answered. Answers are saved to the process library and "
               "used by the next analysis of this process.")
    if not qs:
        st.info("No questions for this analysis.")
    for i, q in enumerate(qs):
        with st.container(border=True):
            st.markdown(f"**{q['question']}**")
            st.caption(q["kind"].replace("_", " ").capitalize()
                       + (f" · answered by {q['by']}: {q['answer']}" if q["answer"] else " · open"))
            if not q["answer"]:
                answer = st.text_area("Answer", key=f"nc_question_{i}", label_visibility="collapsed",
                                      placeholder="Your answer")
                if st.button("Save answer", key=f"nc_question_save_{i}"):
                    if not by.strip() or not answer.strip():
                        st.error("Enter your name above and an answer.")
                    else:
                        _log(process_dir(ctx["process"]), {"who": by.strip(),
                             "when": datetime.now(timezone.utc).isoformat(), "item": f"question:{q['key']}",
                             "action": "sme_answer", "before": q["question"], "after": answer.strip(), "comment": ""})
                        st.rerun()
    with st.expander("Add a question"):
        new_question = st.text_input("Question", key="nc_question_new")
        if st.button("Add question", key="nc_question_add") and new_question.strip():
            plan_path = ctx["folder"] / PLAN_FILE
            plan = json.loads(plan_path.read_text(encoding="utf-8")) if plan_path.exists() else {}
            plan.setdefault("questions_for_sme", []).append(new_question.strip())
            plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
            st.rerun()
    st.subheader("Recent review decisions")
    rows = ctx["knowledge"]["decisions"][-100:]
    if rows:
        st.dataframe([{"When": str(item.get("when") or "")[:16].replace("T", " "), "Who": item.get("who"),
                       "Item": item.get("item"), "Decision": item.get("action"), "Note": item.get("comment")}
                      for item in reversed(rows)], hide_index=True, width="stretch")
