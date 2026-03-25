import json
import os
import subprocess
import sys

PLAYWRIGHT_TOKEN_SCRIPT = os.environ.get(
    "PLAYWRIGHT_TOKEN_SCRIPT",
    "/app/scripts/playwright_get_erp_token_v3.py",
)

def main() -> int:
    try:
        result = subprocess.run(
            ["python", PLAYWRIGHT_TOKEN_SCRIPT],
            capture_output=True,
            text=True,
            env=os.environ.copy(),
            timeout=int(os.environ.get("GET_TOKEN_TIMEOUT_SEC", "135")),
        )
    except subprocess.TimeoutExpired:
        print("Playwright helper timed out", file=sys.stderr)
        return 124

    if result.returncode != 0:
        print(result.stderr or result.stdout, file=sys.stderr)
        return result.returncode

    stdout = (result.stdout or "").strip()
    try:
        data = json.loads(stdout)
    except Exception:
        print(f"Salida no JSON del helper Playwright: {stdout[:1000]}", file=sys.stderr)
        return 2

    id_token = data.get("google_id_token")
    erp_bearer = data.get("erp_bearer_token")

    if isinstance(id_token, str) and id_token.strip():
        print(json.dumps({"idToken": id_token}, ensure_ascii=False))
        return 0

    # Sesión activa: no hubo flujo OAuth pero Playwright capturó el bearer directamente
    if isinstance(erp_bearer, str) and erp_bearer.strip():
        print(json.dumps({"erpBearerToken": erp_bearer}, ensure_ascii=False))
        return 0

    print(f"No se encontró google_id_token ni erp_bearer_token en la salida: {stdout[:1000]}", file=sys.stderr)
    return 3

if __name__ == "__main__":
    raise SystemExit(main())
