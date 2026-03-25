# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A custom n8n deployment for ERP automation — specifically timesheet entry and token management for an ERP system (erp.developers.net) with Google OAuth. This is NOT the n8n open-source repo; it's a Docker Compose stack with three services that orchestrate browser-automated ERP workflows.

## Architecture

Three Docker Compose services:

- **PostgreSQL 16** — n8n's persistence backend
- **n8n** (official `n8nio/n8n:latest` image) — workflow engine, port 5678 (localhost only)
- **playwright-runner** — custom Python 3.12 FastAPI service, port 8000, handles browser automation

The playwright-runner exposes a REST API (`runner_api.py`) that n8n workflows call to:
1. **`GET /health`** — liveness check
2. **`POST /get-erp-token`** — launches Playwright to intercept Google ID token and ERP bearer token during browser login flow
3. **`POST /run-timesheet`** — submits timesheet entries to the ERP API

Flow: n8n workflow triggers -> calls playwright-runner API -> runner spawns Python scripts as subprocesses -> scripts interact with ERP via browser automation or direct API calls.

## Key Files

- `docker-compose.yml` — service definitions; env vars come from `.env`
- `runner_api.py` — FastAPI app, entry point for the playwright-runner container
- `scripts/playwright_get_erp_token_v3.py` — Playwright browser automation to capture auth tokens (intercepts login API calls)
- `scripts/erp_timesheet.py` — core timesheet logic (~1200 lines); handles date math, client ID mapping, holiday/overtime, dry-run mode, file locking for concurrency
- `scripts/google_id_token_helper.py` — bridge script that extracts `google_id_token` from the Playwright token script output
- `Dockerfile.playwright` — builds the runner image (Python 3.12 + Playwright Chromium)

## Common Commands

```bash
# Build and start the full stack
docker-compose build
docker-compose up -d

# Rebuild only the playwright-runner after code changes
docker-compose build playwright-runner
docker-compose up -d playwright-runner

# View logs
docker-compose logs -f playwright-runner
docker-compose logs -f n8n

# Test the runner API
curl http://localhost:8000/health
curl -X POST http://localhost:8000/get-erp-token
curl -X POST http://localhost:8000/run-timesheet -H "Content-Type: application/json" -d '{ ... }'
```

## Development Notes

- **Python dependencies**: listed in `requirements-runner.txt` (fastapi, uvicorn, pydantic, playwright, requests, filelock)
- **No test suite exists** — test changes via `dry_run: true` in timesheet requests or by hitting the `/health` endpoint
- **Environment config**: all secrets and URLs live in `.env` (not committed). The runner also reads env vars at runtime for script paths and timeouts
- **Browser state**: Playwright uses a persistent Chromium profile at `playwright-profile/` for session reuse; storage state is saved to avoid re-authenticating every call
- **Concurrency**: `erp_timesheet.py` uses `filelock` to prevent overlapping timesheet submissions
- **Timeouts**: token script defaults to 135s, timesheet script to 180s — configurable via `GET_TOKEN_TIMEOUT_SEC` / `RUN_TIMESHEET_TIMEOUT_SEC` env vars
- **Language**: code comments and some error messages are in Spanish
