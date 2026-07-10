# Web-service version — scope

Goal: let institute staff (who don't run Claude Code) upload a sheet or paste a
Google Sheet URL in a browser and get the HTML report — the same pipeline, no
CLI. Also the bridge to using it from the claude.ai app (via an MCP connector).

The pipeline itself does **not** change. The service is a thin layer over the
existing `OrchestratorAgent.run(...)`, which already exposes an `on_stage`
callback built for live progress streaming.

## Architecture

```
Browser (upload CSV/XLSX  or  paste Sheet URL + question)
    │  HTTPS
    ▼
Web service (FastAPI)
    ├─ POST /api/analyze     → save input, start run in background, return run_id
    ├─ GET  /api/runs/{id}/events  → SSE stream of stage progress (on_stage)
    ├─ GET  /api/runs/{id}/report  → the HTML report
    ├─ GET  /api/runs/{id}         → status + final_report JSON
    └─ GET  /                      → upload page (static HTML)
    │
    ▼
agents/ingestion.py  →  OrchestratorAgent.run()  →  runs/{id}/report.html
```

## Endpoints (MVP)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Upload page: drop CSV/XLSX or paste a Sheet URL + question |
| `POST` | `/api/analyze` | multipart file **or** JSON `{sheet_url, question}` → `{run_id}` |
| `GET` | `/api/runs/{id}/events` | Server-Sent Events: `stage`, `status`, `summary` per agent |
| `GET` | `/api/runs/{id}/report` | The finished `report.html` |
| `GET` | `/api/runs/{id}` | Run status + compact `final_report` |

## Run model

- A run is slow-ish; start it in a background task and return `run_id`
  immediately. The browser subscribes to `/events` and renders the 8-stage rail
  from the `on_stage` callback (already wired in the orchestrator).
- Artifacts per run live under `runs/{id}/`: the input snapshot, canonical
  parquet, `report.html`, `run.json`.

## Tech choice

**FastAPI + uvicorn** for the service layer (clean multipart upload, SSE, async).
This is a *separate deployable* — the pipeline stays dependency-light; only the
web front adds deps. (A stdlib `http.server` version is possible with zero deps
but means hand-rolling multipart + SSE — more code, not worth it for production.)

New deps (service only): `fastapi`, `uvicorn`, `python-multipart`.

## Security / PII (must-have, not optional)

- **Raw uploads + Sheet snapshots contain un-masked PII.** Store them OUTSIDE any
  static/served directory. Never expose a download route for them.
- **Only the masked `report.html` is servable.** The pipeline already asserts no
  10-digit mobile survives into the report — keep that gate.
- **Retention/purge:** auto-delete `runs/{id}/` inputs after N hours (report may
  persist longer). A daily purge job.
- **Access control:** every `/api` route behind auth. MVP = one shared access
  token (env var) for institute staff; later Google OAuth restricted to the
  institute domain.
- **HTTPS only.** No plaintext — PII in transit.

## Multi-tenant

MVP = single institute (one `monitoring_registry.json`). Later: a `tenant_id`
partitions `runs/`, registries, and reports so multiple institutes share one
deployment.

## Phases + effort

| Phase | Deliverable | Rough effort |
|---|---|---|
| 1 | MVP: upload page + `POST /analyze` (file & URL) + synchronous run → returns report | ~1 day |
| 2 | Background runs + SSE progress rail + run-history list | ~1 day |
| 3 | Auth (shared token) + HTTPS + PII retention/purge job | ~1 day |
| 4 | Dockerfile + deploy (Render / Railway / Fly.io / VPS) | ~0.5 day |
| 5 | *(optional)* MCP connector wrapping the same API → usable from the **claude.ai app** | ~1 day |

Total for a solid internal tool: **~3–4 days**; +1 day for the claude.ai bridge.

## Deployment

- **Docker container**: Python + repo + `uvicorn`. One image.
- **Host**: Render / Railway / Fly.io (managed, HTTPS out of the box) or a small
  VPS behind nginx.
- **Config**: `ANTHROPIC_API_KEY` optional (sharper prose; numbers identical
  without it), `ACCESS_TOKEN`, `RUN_RETENTION_HOURS`.

## Bridge to the claude.ai app (Phase 5)

Wrap the same service as an **MCP server** exposing two tools:
`analyze_sheet(sheet_url | file, question)` and `get_report(run_id)`. Add it as a
custom connector in claude.ai → staff run reports from the consumer app chat.
This is the only way the claude.ai app can reach the pipeline (it has no local
Bash/Python).

## What already exists (so this is a wrapper, not a rewrite)

- `OrchestratorAgent.run(..., on_stage=...)` — full pipeline + progress callback.
- `agents/ingestion.py` — Sheet URL → snapshot, upload snapshot.
- `agents/report_agent.py` — the shareable, PII-safe HTML.
- `scripts/run_pipeline.py` — the CLI already assembles the exact call the API
  will make.

The web service is mostly: HTTP routing + a background task + an upload page.
