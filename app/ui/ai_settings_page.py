"""AI settings: which providers have a key, which model each uses, routes, today's usage.

Keys are read from .env by the app and are never shown here, not even in part. Choices
(models, limit overrides) are written to config/ai_providers.local.yaml.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from app.ui import shell

from app.core import ai_client, privacy
from app.core.config import settings

shell.header("Settings", "AI providers, which provider each task uses, and how this installation is set up. "
             "Keys stay in .env and are never shown.")
tab_ai, tab_routes, tab_system = st.tabs(["AI providers", "Routes and usage", "System"])
with tab_ai:
    if not privacy._flag("AI_ENABLED"):
        st.info("AI is switched off (AI_ENABLED is not true in .env). You can still prepare providers here.")
    if not privacy._flag("ALLOW_EXTERNAL_AI"):
        st.warning("ALLOW_EXTERNAL_AI is not true: cloud providers are never called.")

    known = ai_client.providers()
    for key, p in known.items():
        with st.container(border=True):
            has = ai_client.has_key(p) and not p.get("key_optional")
            ok, why = ai_client.usable(p)
            key_status = "not required" if p.get("key_optional") else ("✓" if has else "✗")
            st.markdown(f"**{p.get('name', key)}** · key {key_status} "
                        f"(`{p.get('key_env')}`) · {privacy.classify_endpoint(p.get('base_url') or '')} endpoint")
            if p.get("free_tier_note"):
                st.caption(p["free_tier_note"])
            st.write(f"Model: **{p.get('model') or 'not chosen'}** · " + ("ready" if ok else f"not used: {why}"))
            lim = p.get("limits") or {}
            if any(lim.values()):
                st.caption("Limits: " + ", ".join(f"{k} {v:,}" for k, v in lim.items() if v))
            if key == "custom":
                st.caption("Set by LLM_BASE_URL / LLM_MODEL / LLM_API_KEY in .env; used first for every task.")
                continue
            c1, c2, c3 = st.columns([2, 3, 1])
            if c1.button("List models", key=f"ai_list_{key}", disabled=not has):
                try:
                    st.session_state[f"ai_models_{key}"] = ai_client.list_models(key)
                except Exception as err:  # noqa: BLE001 - show the reason, never the key
                    st.error(f"Could not list models: {type(err).__name__}")
            models = st.session_state.get(f"ai_models_{key}") or []
            if models:
                pick = c2.selectbox("Model", models, key=f"ai_pick_{key}",
                                    index=models.index(p["model"]) if p.get("model") in models else 0)
                if c3.button("Save", key=f"ai_save_{key}"):
                    ai_client.save_local(key, model=pick)
                    st.success(f"Saved {pick} for {key}.")
                    st.rerun()
            if st.button("Test", key=f"ai_test_{key}", disabled=not ok):
                r = ai_client.test_provider(key)
                if r["ok"]:
                    st.success(f"OK · {r['model']} · {r['latency_s']:.2f} s")
                else:
                    st.error(f"Not OK: {r['reason']}")


with tab_routes:
    st.subheader("Routes")
    rows = []
    for task in ai_client.TASKS:
        names = ai_client.route(task)
        now = next((n for n in names if n in known and ai_client.usable(known[n])[0]), None)
        rows.append({"Task": task, "Providers in order": " → ".join(names), "Would use now": now or "none (templates)"})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.caption(f"Routes are set in config/settings.yaml (ai.routes). Waiting for a rate limit: at most "
               f"{settings()['ai']['max_wait_seconds']} s, then the next provider.")

    st.subheader("Today's usage")
    usage = []
    for key, p in known.items():
        t = ai_client.today(p)
        lim = p.get("limits") or {}
        usage.append({"Provider": key, "Day": t["day"], "Requests": t["requests"],
                      "Requests per day": f"{lim['rpd']:,}" if lim.get("rpd") else "unknown", "Tokens": t["tokens"],
                      "Tokens per day": f"{lim['tpd']:,}" if lim.get("tpd") else "unknown"})
    st.dataframe(pd.DataFrame(usage), hide_index=True, width="stretch")
    st.caption("Counted on this computer in data/ai_usage.json (requests and estimated tokens, never contents).")

with tab_system:
    dep = st.session_state.get("deployment") or {}
    st.markdown(f"**Profile:** {dep.get('profile', 'default')}  \n**Data folder:** `{dep.get('data_dir', '')}`")
    st.caption(st.session_state.get("privacy_line", ""))
    provs = dep.get("providers") or []
    if provs:
        st.markdown("**AI route privacy by task**")
        st.dataframe(pd.DataFrame([{"Task": p["task"], "Provider": p["provider"],
                                    "Private provider": "yes" if p["private"] else "no"} for p in provs]),
                     hide_index=True, width="stretch")
