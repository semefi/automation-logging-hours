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
2. **`POST /get-erp-token`** — launches Playwright to capture ERP bearer token from active browser session
3. **`POST /run-timesheet`** — submits timesheet entries to the ERP API

Flow: n8n workflow triggers → calls playwright-runner API → runner spawns Python scripts as subprocesses via `xvfb-run` (virtual display) → scripts interact with ERP via browser automation or direct API calls.

## Key Files

- `docker-compose.yml` — service definitions; env vars come from `.env`
- `runner_api.py` — FastAPI app, entry point for the playwright-runner container. Wraps all subprocess calls with `xvfb-run` for virtual display support
- `scripts/playwright_get_erp_token_v3.py` — Playwright browser automation to capture auth tokens. Intercepts Authorization headers and login API calls. Has auto-click logic for the Google Sign-In button on the ERP login page
- `scripts/erp_timesheet.py` — core timesheet logic (~1200 lines); handles date math, client ID mapping, holiday/overtime, dry-run mode, file locking for concurrency
- `scripts/google_id_token_helper.py` — bridge script: returns `google_id_token` if available, or falls back to returning `erp_bearer_token` directly when the browser session is active (no Google OAuth flow triggered)
- `scripts/manual_google_login.py` — helper for one-time manual Google login within the container via VNC
- `Dockerfile.playwright` — builds the runner image (Python 3.12 + Playwright Chromium + xvfb + xauth)
- `n8n-workflow-timesheet.json` — importable n8n workflow for batch timesheet submission

## Authentication Flow

The ERP uses Google Sign-In (GSI) with FedCM. The authentication chain:

1. **Browser session active**: Playwright navigates to ERP → goes to `/home` → captures bearer from Authorization headers. This is the happy path.
2. **Browser session expired**: Playwright navigates to ERP → lands on `/user/login` → auto-clicks the Google Sign-In iframe button → if Google session cookies exist, FedCM auto-authenticates → ERP login completes → bearer captured.
3. **Google session expired** (rare, cookies last months): requires manual re-login via VNC (see Troubleshooting).

The `google_id_token_helper.py` handles two response types:
- `{"idToken": "..."}` — full OAuth flow completed, `erp_timesheet.py` calls the login API
- `{"erpBearerToken": "..."}` — session reuse, `erp_timesheet.py` uses the bearer directly

**Important**: FedCM does NOT work in headless Chromium. All Playwright scripts run under `xvfb-run` to provide a virtual display. The Dockerfile installs `xvfb` and `xauth` for this.

## n8n Workflow

The workflow (`n8n-workflow-timesheet.json`) accepts timesheet entries via webhook:

```bash
curl -X POST "https://openclaw-vps.tail956176.ts.net:8444/webhook/timesheet" \
  -H "Content-Type: application/json" \
  -d '{"entries": [
    {"date": "2026-03-24", "description": "Worked on feature X", "hours": 8},
    {"date": "2026-03-25", "description": "Bug fixes", "hours": 6}
  ]}'
```

Each entry needs: `date`, `description`, `hours` (maps to `hours_product_development`). Optional: `hours_product_support`, `hours_client_support`, `is_overtime`, `is_holiday`, `dry_run`.

Fixed values: `email=sebastian.mendez@developers.net`, `user_id=3189`, `client_id=56`.

**Concurrency**: `erp_timesheet.py` uses file locking — entries must be submitted sequentially. The n8n workflow uses SplitInBatches with batch size 1 and a 2-second wait between submissions.

## Common Commands

```bash
# Build and start the full stack
docker-compose build
docker-compose up -d

# Rebuild only the playwright-runner after code changes
docker-compose build playwright-runner
docker-compose rm -sf playwright-runner
docker-compose up -d playwright-runner

# View logs
docker-compose logs -f playwright-runner
docker-compose logs -f n8n

# Test the runner API
curl http://localhost:8000/health
curl -X POST http://localhost:8000/get-erp-token
curl -X POST http://localhost:8000/run-timesheet -H "Content-Type: application/json" -d '{ ... }'

# Test Playwright token capture directly in container
docker-compose exec playwright-runner xvfb-run --auth-file=/tmp/xvfb.auth env USER_DATA_DIR=/app/playwright-profile python3 /app/scripts/playwright_get_erp_token_v3.py

# Check Google session cookies inside container
docker-compose exec playwright-runner python3 -c "
from playwright.sync_api import sync_playwright
pw = sync_playwright().start()
ctx = pw.chromium.launch_persistent_context('/app/playwright-profile', headless=True)
cookies = ctx.cookies('https://accounts.google.com')
names = [c['name'] for c in cookies]
has_session = any(c in names for c in ['SID', 'SSID', 'HSID'])
print(f'Google cookies: {names}')
print(f'Has Google session: {has_session}')
ctx.close(); pw.stop()
"
```

## Troubleshooting

### Token capture returns null / timeout

**Symptom**: `playwright_get_erp_token_v3.py` times out with no bearer token, browser lands on `/user/login`.

**Cause**: Google session cookies expired or were lost. FedCM can't auto-authenticate.

**Fix**: Manual Google login inside the container via VNC:

```bash
# 1. Install VNC tools (lost on container rebuild)
docker-compose exec playwright-runner apt-get update && \
docker-compose exec playwright-runner apt-get install -y x11vnc xauth

# 2. Expose port 5900 in docker-compose.yml under playwright-runner:
#    ports:
#      - "8000:8000"
#      - "5900:5900"
# Then: docker-compose up -d playwright-runner

# 3. Start xvfb + VNC server
docker-compose exec playwright-runner bash -c \
  "xvfb-run --server-num=99 --auth-file=/tmp/xvfb.auth x11vnc -auth /tmp/xvfb.auth -display :99 -nopw -listen 0.0.0.0 -forever"

# 4. Connect VNC viewer to openclaw-vps.tail956176.ts.net:5900

# 5. In another SSH session, open Google login in the browser:
docker-compose exec playwright-runner rm -f /app/playwright-profile/SingletonLock /app/playwright-profile/SingletonCookie /app/playwright-profile/SingletonSocket
docker-compose exec playwright-runner env DISPLAY=:99 XAUTHORITY=/tmp/xvfb.auth python3 /app/scripts/manual_google_login.py

# 6. Log into Google in the VNC viewer, then press Enter in SSH

# 7. Kill VNC and test:
docker-compose exec playwright-runner pkill -f x11vnc
docker-compose exec playwright-runner pkill -f Xvfb
docker-compose exec playwright-runner xvfb-run --auth-file=/tmp/xvfb.auth env USER_DATA_DIR=/app/playwright-profile python3 /app/scripts/playwright_get_erp_token_v3.py
```

**Important**: The Google login MUST happen inside the container's Chromium. Logging in from a different Playwright/Chromium (e.g., a local venv) stores cookies in an incompatible format.

### Profile lock error

**Symptom**: `The profile appears to be in use by another Chromium process`

**Fix**: Remove lock files:
```bash
docker-compose exec playwright-runner rm -f /app/playwright-profile/SingletonLock /app/playwright-profile/SingletonCookie /app/playwright-profile/SingletonSocket
```

### LockError on timesheet submission

**Symptom**: `Ya existe otra ejecución activa. Lock: /root/.cache/erp_timesheet/run.lock`

**Cause**: Multiple timesheet submissions running concurrently. `erp_timesheet.py` uses file locking.

**Fix**: Ensure n8n workflow submits entries sequentially (SplitInBatches with batch size 1 + Wait node). If the lock is stale:
```bash
docker-compose exec playwright-runner rm -f /root/.cache/erp_timesheet/run.lock
```

### ContainerConfig error on docker-compose up

**Symptom**: `KeyError: 'ContainerConfig'`

**Cause**: Bug in docker-compose v1.29.2 with newer Docker engines.

**Fix**: Remove the old container first:
```bash
docker-compose rm -sf playwright-runner
docker-compose build playwright-runner
docker-compose up -d playwright-runner
```

### xvfb-run: error: xauth command not found

**Cause**: `xauth` not installed (lost after container rebuild).

**Fix**: Already included in Dockerfile. If missing:
```bash
docker-compose exec playwright-runner apt-get update && apt-get install -y xvfb xauth
```

## Development Notes

- **Python dependencies**: listed in `requirements-runner.txt` (fastapi, uvicorn, pydantic, playwright, requests, filelock)
- **No test suite exists** — test changes via `dry_run: true` in timesheet requests or by hitting the `/health` endpoint
- **Environment config**: all secrets and URLs live in `.env` (not committed). The runner also reads env vars at runtime for script paths and timeouts
- **Browser state**: Playwright uses a persistent Chromium profile at `playwright-profile/` for session reuse. This directory is mounted as a volume — do NOT delete it or run `docker-compose down -v`
- **Google session cookies** last ~2 years unless revoked. Manual re-login should be very rare
- **Concurrency**: `erp_timesheet.py` uses `filelock` to prevent overlapping timesheet submissions — always submit sequentially
- **Timeouts**: token script defaults to 135s, timesheet script to 180s — configurable via `GET_TOKEN_TIMEOUT_SEC` / `RUN_TIMESHEET_TIMEOUT_SEC` env vars
- **xvfb-run**: all Playwright subprocesses run under `xvfb-run` for FedCM compatibility. This is handled automatically in `runner_api.py`
- **Language**: code comments and some error messages are in Spanish
- **Git remote**: https://github.com/semefi/automation-logging-hours.git (branch: main)
- **VPS access**: openclaw-vps.tail956176.ts.net via Tailscale
