from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import subprocess
import json
import os
from typing import Any, Dict


app = FastAPI(title="ERP Automation Runner")


class TimesheetRequest(BaseModel):
    erp_bearer_token: str
    email: str
    user_id: int
    client_id: int
    date: str
    description: str
    hours_product_development: float = 0
    hours_product_support: float = 0
    hours_client_support: float = 0
    is_overtime: bool = False
    is_holiday: bool = False
    is_other_not_paid: bool = False
    dry_run: bool = False
    verbose: bool = False


def _tail(text: str | None, limit: int = 4000) -> str:
    return (text or "")[-limit:]


def _run_python_script(
    cmd: list[str],
    extra_env: Dict[str, str] | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    # Use xvfb-run to provide a virtual display for FedCM/headed browser features
    xvfb_cmd = ["xvfb-run", "--auto-servernum", "--server-args=-screen 0 1280x960x24"] + cmd

    return subprocess.run(
        xvfb_cmd,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def _parse_json_stdout(result: subprocess.CompletedProcess, context: str) -> Dict[str, Any]:
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()

    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail={
                "error": f"{context} failed",
                "returncode": result.returncode,
                "stdout": _tail(stdout),
                "stderr": _tail(stderr),
            },
        )

    try:
        return json.loads(stdout)
    except Exception:
        raise HTTPException(
            status_code=500,
            detail={
                "error": f"{context} did not return valid JSON on stdout",
                "stdout": _tail(stdout),
                "stderr": _tail(stderr),
            },
        )


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/get-erp-token")
def get_erp_token():
    script_path = os.environ.get(
        "PLAYWRIGHT_TOKEN_SCRIPT",
        "/app/scripts/playwright_get_erp_token_v3.py",
    )

    extra_env = {
        "APP_URL": os.environ.get("APP_URL", "https://erp.developers.net/"),
        "LOGIN_API_URL": os.environ.get("LOGIN_API_URL", "https://erp.developers.net/api/User/Login"),
        "USER_DATA_DIR": os.environ.get("USER_DATA_DIR", "/app/playwright-profile"),
        "OUTPUT_JSON": os.environ.get("OUTPUT_JSON", "/app/playwright-profile/playwright_token_output.json"),
        "STORAGE_STATE_PATH": os.environ.get("STORAGE_STATE_PATH", "/app/playwright-profile/playwright_storage_state.json"),
        "GOOGLE_ACCOUNT_EMAIL": os.environ.get("GOOGLE_ACCOUNT_EMAIL", "sebastian.mendez@developers.net"),
        "HEADLESS": os.environ.get("HEADLESS", "true"),
        "MAX_WAIT_SEC": os.environ.get("MAX_WAIT_SEC", "240"),
        "NAV_TIMEOUT_MS": os.environ.get("NAV_TIMEOUT_MS", "45000"),
        "MASK_OUTPUT": os.environ.get("MASK_OUTPUT", "false"),
        "TELEGRAM_BOT_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        "TELEGRAM_CHAT_ID": os.environ.get("TELEGRAM_CHAT_ID", ""),
    }

    try:
        result = _run_python_script(
            cmd=["python", script_path],
            extra_env=extra_env,
            timeout=int(os.environ.get("GET_TOKEN_TIMEOUT_SEC", "300")),
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Playwright token script timed out (MFA may be pending)")

    data = _parse_json_stdout(result, "playwright_get_erp_token")

    if not data.get("erp_bearer_token"):
        raise HTTPException(
            status_code=500,
            detail={
                "error": "No erp_bearer_token returned",
                "data": data,
                "stderr": _tail(result.stderr),
            },
        )

    return data


@app.post("/run-timesheet")
def run_timesheet(payload: TimesheetRequest):
    script_path = os.environ.get(
        "ERP_TIMESHEET_SCRIPT",
        "/app/scripts/erp_timesheet.py",
    )

    cmd = [
        "python",
        script_path,
        "--erp-token", payload.erp_bearer_token,
        "--email", payload.email,
        "--user-id", str(payload.user_id),
        "--client-id", str(payload.client_id),
        "--date", payload.date,
        "--description", payload.description,
        "--hours-product-development", str(payload.hours_product_development),
        "--hours-product-support", str(payload.hours_product_support),
        "--hours-client-support", str(payload.hours_client_support),
        "--google-token-helper", "python",
        "--google-token-helper-args", "/app/scripts/google_id_token_helper.py",
    ]

    if payload.is_overtime:
        cmd.append("--is-overtime")
    if payload.is_holiday:
        cmd.append("--is-holiday")
    if payload.is_other_not_paid:
        cmd.append("--is-other-not-paid")
    if payload.dry_run:
        cmd.append("--dry-run")
    if payload.verbose:
        cmd.append("--verbose")

    extra_env = {
        "APP_URL": os.environ.get("APP_URL", "https://erp.developers.net/"),
        "LOGIN_API_URL": os.environ.get("LOGIN_API_URL", "https://erp.developers.net/api/User/Login"),
        "USER_DATA_DIR": os.environ.get("USER_DATA_DIR", "/app/playwright-profile"),
        "OUTPUT_JSON": os.environ.get("OUTPUT_JSON", "/app/playwright-profile/playwright_token_output.json"),
        "STORAGE_STATE_PATH": os.environ.get("STORAGE_STATE_PATH", "/app/playwright-profile/playwright_storage_state.json"),
        "GOOGLE_ACCOUNT_EMAIL": os.environ.get("GOOGLE_ACCOUNT_EMAIL", "sebastian.mendez@developers.net"),
        "HEADLESS": os.environ.get("HEADLESS", "true"),
        "MAX_WAIT_SEC": os.environ.get("MAX_WAIT_SEC", "240"),
        "NAV_TIMEOUT_MS": os.environ.get("NAV_TIMEOUT_MS", "45000"),
        "MASK_OUTPUT": os.environ.get("MASK_OUTPUT", "false"),
        "PLAYWRIGHT_TOKEN_SCRIPT": os.environ.get(
            "PLAYWRIGHT_TOKEN_SCRIPT",
            "/app/scripts/playwright_get_erp_token_v3.py",
        ),
        "GET_TOKEN_TIMEOUT_SEC": os.environ.get("GET_TOKEN_TIMEOUT_SEC", "135"),
    }

    try:
        result = _run_python_script(
            cmd=cmd,
            extra_env=extra_env,
            timeout=int(os.environ.get("RUN_TIMESHEET_TIMEOUT_SEC", "180")),
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="erp_timesheet.py timed out")

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()

    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "erp_timesheet.py failed",
                "returncode": result.returncode,
                "stdout": _tail(stdout),
                "stderr": _tail(stderr),
            },
        )

    try:
        return json.loads(stdout)
    except Exception:
        return {
            "ok": True,
            "stdout": stdout,
            "stderr": _tail(stderr),
        }
