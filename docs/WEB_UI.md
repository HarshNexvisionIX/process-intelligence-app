# Web UI (ProcessLens v3)

## Run

    Start_Web_UI.bat            # Windows; or: python -m app.api
    http://127.0.0.1:8008

The API (`app/api/server.py`, FastAPI) serves the pre-built React UI from `web/dist`. To change the UI:
`cd web && npm install && npm run dev` (port 5173, proxies `/api` to 8008), then `npm run build`.

## Code map

| Path | What it is |
| --- | --- |
| `app/api/server.py` | All endpoints: jobs, import/convert, business context, AI packages, run, map, step panel, review, questions, report, PDF, workflow map |
| `app/api/ai_packages.py` | Approve-before-send: PreviewGateway (build + guard + keep), ApprovedGateway (send approved tasks, audit log), SHA-256 fingerprints |
| `app/api/insights.py` | Data insights (people, applications, hour of day, weekday, switching, copy/paste); people shown as P01… |
| `app/api/system_map.py` | Workflow map: every service / AI agent / human gate with inputs, outputs, files; live status per analysis |
| `app/api/pdf.py` | Stakeholder PDF from checked facts (no AI); AI readout PDF |
| `app/api/readout.py` | AI readout: evidence catalog (checked facts F… + derived signals X…), the request, the answer check (cites evidence, no numbers) |
| `app/core/workflow.py` | Steps of both workflows and when each is done (shared with the Streamlit app) |
| `app/nocase/questions.py` | SME questions and answers (shared with the Streamlit app) |
| `web/src/App.jsx` | Shell: sidebar with the workflow steps, current analysis, tools, workspace |
| `web/src/pages/*` | One file per page |
| `web/src/components/FlowMap.jsx` | Process map (React Flow + dagre): hours and applications per box, stage boxes, business order vs observed transitions |
| `web/src/components/StagePanel.jsx` | Selected step: applications and hours, Level 4 activities, run patterns, signals, screens |
| `web/src/components/AIPackages.jsx` | Purpose, contents, exact JSON, size, SHA-256, approve with name, audit log |

## Files written per analysis (new)

| File | Written by | Meaning |
| --- | --- | --- |
| `ai_packages.json` | run without AI, docs prepare, report | Every AI request built, guarded and kept (not sent) |
| `ai_approvals.json` | Approve button | Task, task SHA-256, request SHA-256s, who, when |
| `ai_audit.jsonl` | ApprovedGateway | One line per request actually sent: task, SHA-256, matched approval, provider, model, result |
| `nc_last_run.json` | each run | What every engine stage did; requests the guard stopped |
| `readout.json` | AI readout page | The seven-section readout, provider, model, fingerprint of the evidence it was written from |

## Rules kept

- No AI key or model field anywhere in the web UI or API (`tests/test_web_api.py` checks this).
- Client data only to private providers; every request passes `payloads.guard`; documents are masked first.
- Business context is required before the report; nothing comes from built-in templates.
- `data/` is never deleted: removing an analysis moves it to the backup folder.

The full workflow guide (how every step works, inputs/outputs, examples) is the "ProcessLens — How it
works" document.
