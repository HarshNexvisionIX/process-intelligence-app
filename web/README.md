# Web UI (React + Vite)

The built UI is in `dist/` and is served by the Python API: `python -m app.api` → http://127.0.0.1:8008.
Node.js is only needed to change the UI:

    cd web
    npm install
    npm run dev      # http://127.0.0.1:5173, /api is proxied to the API on :8008
    npm run build    # writes dist/ (commit it, so the app runs without Node)

Pages: Import → Business context → Overview and analysis → Process map → Review steps → Report (no case ID), or
Import → Business context → Data quality → Process analysis → Process definition (classic app) → Report (case ID);
plus Data insights, Workflow map, Process library, Analyses, Settings (read-only).
No page asks for an AI key or a model: those stay in Settings / .env.
