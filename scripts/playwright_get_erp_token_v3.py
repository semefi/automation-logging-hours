#!/usr/bin/env python3
"""
Captura el Google ID token y el Bearer token del ERP interceptando las
llamadas al endpoint de login vía Playwright (contexto persistente).

Variables de entorno:
  APP_URL            URL inicial de la app          (default: https://erp.developers.net/)
  LOGIN_API_URL      Endpoint de login del ERP      (default: https://erp.developers.net/api/User/Login)
  USER_DATA_DIR      Directorio de perfil Chromium  (default: ./playwright_user_data)
  OUTPUT_JSON        Ruta del JSON de salida         (default: ./playwright_token_output.json)
  STORAGE_STATE_PATH Ruta para guardar storage state (default: ./playwright_storage_state.json)
  GOOGLE_ACCOUNT_EMAIL  Email para autoseleccionar cuenta Google
  HEADLESS           true/false                     (default: false)
  MAX_WAIT_SEC       Segundos máximos de espera     (default: 120)
  NAV_TIMEOUT_MS     Timeout de navegación (ms)     (default: 45000)
"""
from __future__ import annotations

import base64
import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, Playwright, Request, Response, sync_playwright

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DEFAULT_APP_URL = "https://erp.developers.net/"
DEFAULT_LOGIN_API_URL = "https://erp.developers.net/api/User/Login"


def _env_str(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("Valor inválido para %s='%s', usando default=%d", name, raw, default)
        return default


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------
def _decode_jwt_payload(token: str | None) -> dict[str, Any]:
    """Decodifica el payload de un JWT sin verificar firma."""
    if not token:
        return {}
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1]
        padding = "=" * (-len(payload) % 4)
        raw = base64.urlsafe_b64decode(payload + padding).decode("utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        log.debug("No se pudo decodificar JWT payload: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# Validación de token ERP
# ---------------------------------------------------------------------------
def _looks_like_erp_token(token: str | None) -> bool:
    """Distingue el Bearer del ERP de otros JWTs (Google ID token, tokens de terceros)."""
    payload = _decode_jwt_payload(token)
    if not payload:
        return False
    # Descartar Google ID tokens explícitamente
    if payload.get("iss") == "https://accounts.google.com":
        return False
    # El Bearer del ERP debe tener al menos una claim de identidad interna
    return any(k in payload for k in ("UserId", "Username", "Group", "Email", "role", "sub"))


# ---------------------------------------------------------------------------
# Estado de captura
# ---------------------------------------------------------------------------
class CaptureState:
    def __init__(self) -> None:
        self.google_id_token: str | None = None
        self.google_id_token_payload: dict[str, Any] = {}
        self.google_email: str | None = None

        self.erp_bearer_token: str | None = None
        self.erp_bearer_payload: dict[str, Any] = {}

        self.login_request_seen = False
        self.login_response_seen = False
        self.login_request_summary: dict[str, Any] | None = None
        self.login_response_summary: dict[str, Any] | None = None

        self.request_url: str | None = None
        self.response_status: int | None = None

        # Bounded: guardamos solo las últimas N URLs vistas
        self._page_urls: list[str] = []
        self._max_urls = 20

        self.notes: list[str] = []

        # Event para despertar el loop principal cuando ya tenemos el bearer
        self._token_ready = threading.Event()

    # ------------------------------------------------------------------
    def set_google_id_token(self, token: str | None, email: str | None = None) -> None:
        if not token or self.google_id_token:
            return
        self.google_id_token = token
        self.google_id_token_payload = _decode_jwt_payload(token)
        self.google_email = email or self.google_id_token_payload.get("email")  # type: ignore[assignment]
        log.info("✅ Google ID token capturado (email=%s)", self.google_email)

    def set_erp_bearer(self, token: str | None) -> None:
        if not token or self.erp_bearer_token:
            return
        if not _looks_like_erp_token(token):
            log.debug("Token descartado: no parece ser el Bearer del ERP.")
            return
        self.erp_bearer_token = token
        self.erp_bearer_payload = _decode_jwt_payload(token)
        log.info("✅ ERP Bearer token capturado")
        self._token_ready.set()  # <-- despierta el wait() del loop principal

    def add_url(self, url: str) -> None:
        if url and url not in self._page_urls:
            if len(self._page_urls) >= self._max_urls:
                self._page_urls.pop(0)
            self._page_urls.append(url)

    def wait_for_bearer(self, timeout: float) -> bool:
        """Bloquea hasta obtener el bearer o que expire el timeout. Devuelve True si lo obtuvo."""
        return self._token_ready.wait(timeout=timeout)

    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "obtained_at": _now_iso(),
            "google_id_token": self.google_id_token,
            "google_id_token_payload": self.google_id_token_payload,
            "google_email": self.google_email,
            "erp_bearer_token": self.erp_bearer_token,
            "erp_bearer_payload": self.erp_bearer_payload,
            "login_request_seen": self.login_request_seen,
            "login_response_seen": self.login_response_seen,
            "login_request_summary": self.login_request_summary,
            "login_response_summary": self.login_response_summary,
            "request_url": self.request_url,
            "response_status": self.response_status,
            "page_urls_seen": list(self._page_urls),
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# Helpers de página
# ---------------------------------------------------------------------------
def _try_click_account(page: Page, email: str, state: CaptureState) -> None:
    """Intenta hacer click en la cuenta de Google si aparece el hint de selector."""
    if not email:
        return
    selectors = [
        f'[data-identifier="{email}"]',
        f'[data-email="{email}"]',
        f'div:has-text("{email}")',
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.click(timeout=1_500)
                state.notes.append(f"Click en selector de cuenta Google: {sel}")
                log.debug("Click en cuenta Google con selector: %s", sel)
                return
        except Exception:  # noqa: BLE001
            continue


def _attach_tracking(page: Page, state: CaptureState, account_email: str) -> None:
    def on_load() -> None:
        try:
            state.add_url(page.url)
        except Exception:  # noqa: BLE001
            pass
        _try_click_account(page, account_email, state)

    page.on("load", lambda: on_load())
    page.on("domcontentloaded", lambda: state.add_url(page.url) if page.url else None)


# ---------------------------------------------------------------------------
# Handlers de red
# ---------------------------------------------------------------------------
def _make_request_handler(login_api_url: str, state: CaptureState):
    norm_url = login_api_url.rstrip("/")

    def on_request(request: Request) -> None:
        if request.url.rstrip("/") != norm_url:
            return

        state.login_request_seen = True
        state.request_url = request.url
        log.info("🌐 Request al endpoint de login detectado")

        payload: dict | None = None
        try:
            payload = request.post_data_json
        except Exception:  # noqa: BLE001
            pass

        if not isinstance(payload, dict):
            try:
                raw = request.post_data or ""
                payload = json.loads(raw) if raw else None
            except Exception:  # noqa: BLE001
                pass

        if isinstance(payload, dict):
            state.login_request_summary = {
                "email": payload.get("email"),
                "has_idToken": bool(payload.get("idToken")),
                "has_password": bool(payload.get("password")),
            }
            state.set_google_id_token(payload.get("idToken"), payload.get("email"))

            if payload.get("password") and payload.get("password") != payload.get("idToken"):
                state.notes.append("El request incluye un campo 'password' distinto del idToken.")
        else:
            state.notes.append("Request de login detectado pero no se pudo parsear el body JSON.")
            log.warning("No se pudo parsear el body del request de login.")

    return on_request


def _make_response_handler(login_api_url: str, state: CaptureState):
    norm_url = login_api_url.rstrip("/")

    def on_response(response: Response) -> None:
        if response.url.rstrip("/") != norm_url:
            return

        state.login_response_seen = True
        state.response_status = response.status
        log.info("🌐 Response del endpoint de login: HTTP %d", response.status)

        if response.status >= 400:
            state.notes.append(f"Login respondió con error HTTP {response.status}.")
            log.warning("El endpoint de login devolvió HTTP %d.", response.status)

        data: dict | None = None
        raw_bytes = _read_response_body(response)
        if raw_bytes:
            try:
                data = json.loads(raw_bytes.decode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                state.notes.append(f"Body recibido pero no es JSON válido: {exc}")
                log.warning("Body de login no es JSON válido: %s", exc)
        else:
            state.notes.append("No se pudo leer el body de la response de login.")
            log.warning("No se pudo leer el body de la response de login.")

        if isinstance(data, dict):
            token = data.get("token")
            groups = data.get("groups")
            state.login_response_summary = {
                "has_token": bool(token),
                "group_count": len(groups) if isinstance(groups, list) else None,
            }
            state.set_erp_bearer(token)
            if not token:
                state.notes.append("La response de login no incluye campo 'token'.")
                log.warning("La response de login no tiene campo 'token'.")

    return on_response


def _read_response_body(response: Response) -> bytes | None:
    """Intenta leer el body con múltiples métodos, de más a menos confiable."""
    # Método 1: .body() — más confiable, usa buffer interno de Playwright
    try:
        return response.body()
    except Exception:
        pass
    # Método 2: .text() — puede funcionar si el body ya fue cacheado
    try:
        return response.text().encode("utf-8")
    except Exception:
        pass
    return None


def _mask_token(token: str | None) -> str | None:
    """Muestra solo los primeros 10 y últimos 6 caracteres del token."""
    if not token or len(token) < 20:
        return token
    return f"{token[:10]}...{token[-6:]}"


# ---------------------------------------------------------------------------
# Extracción de token desde sesión activa
# ---------------------------------------------------------------------------
# Claves comunes donde los SPA guardan el Bearer en storage
_STORAGE_TOKEN_KEYS = [
    "token", "access_token", "accessToken", "bearer", "bearerToken",
    "jwt", "authToken", "auth_token", "erp_token",
]

_JS_FIND_TOKEN = """
(keys) => {
    for (const store of [localStorage, sessionStorage]) {
        for (const key of keys) {
            const val = store.getItem(key);
            if (val && val.split('.').length === 3 && val.length > 100) return val;
        }
        for (let i = 0; i < store.length; i++) {
            const k = store.key(i);
            const v = store.getItem(k);
            if (v && v.split('.').length === 3 && v.length > 100) return v;
        }
    }
    return null;
}
"""

def _try_extract_from_storage(page: Page, state: CaptureState) -> bool:
    """Intenta extraer el Bearer desde localStorage/sessionStorage. Devuelve True si lo encontró."""
    try:
        token = page.evaluate(_JS_FIND_TOKEN, _STORAGE_TOKEN_KEYS)
        if token:
            log.info("✅ Bearer extraído desde storage (sesión activa reutilizada)")
            state.notes.append("Bearer extraído desde localStorage/sessionStorage (sesión activa).")
            state.set_erp_bearer(token)
            return True
    except Exception as exc:  # noqa: BLE001
        log.debug("No se pudo leer storage: %s", exc)
    return False


def _try_click_google_login(page: Page, state: CaptureState) -> None:
    """Si la página es el login del ERP, clickea el botón de Google Sign-In para iniciar OAuth."""
    try:
        if "/user/login" not in page.url.lower():
            return

        page.wait_for_timeout(2_000)  # Esperar a que el iframe de GSI cargue

        # El botón de Google Sign-In es un iframe de accounts.google.com/gsi
        gsi_frame = None
        for f in page.frames:
            if "accounts.google.com/gsi" in f.url:
                gsi_frame = f
                break

        if gsi_frame:
            # Dentro del iframe hay un div[role=button] que es el botón real
            btn = gsi_frame.locator("div[role=button]")
            if btn.count() > 0:
                btn.first.click(timeout=5_000)
                log.info("🔑 Click en botón GSI iframe en página de login")
                state.notes.append("Auto-click en iframe de Google Sign-In.")
                page.wait_for_timeout(5_000)
                return

        # Fallback: intentar click en el iframe element directamente
        iframe_loc = page.locator("iframe[id^='gsi_']")
        if iframe_loc.count() > 0:
            iframe_loc.first.click(timeout=5_000)
            log.info("🔑 Click directo en iframe GSI")
            state.notes.append("Auto-click directo en iframe GSI.")
            page.wait_for_timeout(5_000)
            return

        # Fallback: cualquier botón con texto Google
        btn = page.locator("button:has-text('Google')")
        if btn.count() > 0:
            btn.first.click(timeout=5_000)
            log.info("🔑 Click en botón 'Google' en página de login")
            state.notes.append("Auto-click en botón de Google login.")
            page.wait_for_timeout(3_000)

    except Exception as exc:  # noqa: BLE001
        log.debug("No se pudo clickear botón de Google login: %s", exc)


def _attach_auth_header_capture(context, state: CaptureState) -> None:
    """Intercepta cualquier request con Authorization header para capturar el Bearer.
    Útil cuando el token no está en storage sino solo en memoria del SPA."""
    def on_any_request(request: Request) -> None:
        if state.erp_bearer_token:
            return
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()
            if token:
                log.info("✅ Bearer capturado desde Authorization header (%s)", request.url[:80])
                state.notes.append(f"Bearer capturado desde header en: {request.url[:80]}")
                state.set_erp_bearer(token)
    context.on("request", on_any_request)


# ---------------------------------------------------------------------------
# Runner principal
# ---------------------------------------------------------------------------
def run(playwright: Playwright) -> dict[str, Any]:
    app_url = _env_str("APP_URL", DEFAULT_APP_URL)
    login_api_url = _env_str("LOGIN_API_URL", DEFAULT_LOGIN_API_URL)
    user_data_dir = Path(_env_str("USER_DATA_DIR", "./playwright_user_data")).resolve()
    output_json = Path(_env_str("OUTPUT_JSON", "./playwright_token_output.json")).resolve()
    storage_state_path = Path(_env_str("STORAGE_STATE_PATH", "./playwright_storage_state.json")).resolve()
    account_email = _env_str("GOOGLE_ACCOUNT_EMAIL", "")
    headless = _env_bool("HEADLESS", False)
    max_wait_sec = _env_int("MAX_WAIT_SEC", 120)
    nav_timeout_ms = _env_int("NAV_TIMEOUT_MS", 45_000)

    state = CaptureState()

    context = playwright.chromium.launch_persistent_context(
        user_data_dir=str(user_data_dir),
        headless=headless,
        args=["--disable-blink-features=AutomationControlled"],
        viewport={"width": 1440, "height": 960},
    )
    context.set_default_navigation_timeout(nav_timeout_ms)

    # Handlers de red:
    # 1) Captura el Bearer cuando pasa por /api/User/Login (flujo normal)
    context.on("request", _make_request_handler(login_api_url, state))
    context.on("response", _make_response_handler(login_api_url, state))
    # 2) Captura el Bearer desde cualquier request autenticado (sesión activa)
    _attach_auth_header_capture(context, state)
    context.on("page", lambda page: _attach_tracking(page, state, account_email))

    # Ctrl+C limpio
    def _sigint_handler(sig, frame):  # noqa: ANN001
        log.warning("Interrupción recibida. Esperando 2s para terminar requests en vuelo...")
        state.notes.append("Proceso interrumpido por el usuario (SIGINT).")
        time.sleep(2)
        state._token_ready.set()

    signal.signal(signal.SIGINT, _sigint_handler)

    try:
        page = context.pages[0] if context.pages else context.new_page()
        _attach_tracking(page, state, account_email)

        # ── Navegar al app ────────────────────────────────────────────────
        try:
            page.goto(app_url, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            log.warning("Advertencia en navegación inicial: %s", exc)
            state.notes.append(f"Advertencia en navegación inicial: {exc}")

        # ── Autoclick: si estamos en la página de login, clickear "Google credentials" ──
        _try_click_google_login(page, state)

        # ── Estrategia 1: sesión activa → polling de storage (hasta 8s) ────
        # El SPA puede escribir el token 1-3s después de cargar. Una sola
        # lectura inmediata puede llegar antes de que esté disponible.
        if not state.erp_bearer_token:
            storage_deadline = time.time() + 8
            while not state.erp_bearer_token and time.time() < storage_deadline:
                if _try_extract_from_storage(page, state):
                    break
                page.wait_for_timeout(500)

        # ── Estrategia 2: sesión activa → esperar primer request auth ─────
        # Si el token no está en storage (solo en memoria del SPA),
        # _attach_auth_header_capture lo capturará en el próximo request.
        if not state.erp_bearer_token:
            log.info("Token no encontrado en storage. Esperando request autenticado (timeout: %ds)...", max_wait_sec)

        interactive = sys.stdin.isatty()
        if interactive and not headless:
            log.info(
                "Browser abierto. Completa el login de Google si es necesario. "
                "El script termina en cuanto capture el Bearer (timeout: %ds).",
                max_wait_sec,
            )

        # ── Espera ────────────────────────────────────────────────────────
        got_token = state.wait_for_bearer(timeout=float(max_wait_sec))

        # ── Validar expiración del bearer capturado ──────────────────────
        # Si el bearer viene de una sesión vieja (headers/storage), puede
        # estar expirado. En ese caso limpiar la sesión y re-navegar para
        # forzar un login fresco via Google OAuth.
        if state.erp_bearer_token and not state.google_id_token:
            payload = _decode_jwt_payload(state.erp_bearer_token)
            exp = payload.get("exp", 0)
            if isinstance(exp, (int, float)) and exp > 0 and time.time() >= exp:
                log.warning(
                    "Bearer capturado está expirado (exp=%s). Limpiando sesión para forzar re-login...",
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(exp)),
                )
                state.notes.append("Bearer expirado detectado. Forzando re-login.")

                # Resetear estado
                state.erp_bearer_token = None
                state.erp_bearer_payload = {}
                state._token_ready.clear()

                # Limpiar solo cookies del ERP (preservar cookies de Google para OAuth)
                all_cookies = context.cookies()
                erp_cookies = [c for c in all_cookies if "erp.developers.net" in c.get("domain", "")]
                if erp_cookies:
                    context.clear_cookies()
                    # Re-agregar las cookies que NO son del ERP
                    non_erp = [c for c in all_cookies if "erp.developers.net" not in c.get("domain", "")]
                    if non_erp:
                        context.add_cookies(non_erp)
                try:
                    page.evaluate("try { localStorage.clear(); sessionStorage.clear(); } catch(e) {}")
                except Exception:  # noqa: BLE001
                    pass

                # Re-navegar — debería ir a /user/login y disparar Google OAuth
                try:
                    page.goto(app_url, wait_until="domcontentloaded")
                except Exception as exc:  # noqa: BLE001
                    log.warning("Advertencia en re-navegación: %s", exc)

                _try_click_google_login(page, state)
                got_token = state.wait_for_bearer(timeout=float(max_wait_sec))

        if not got_token:
            log.error(
                "Timeout de %ds alcanzado sin capturar el Bearer token. "
                "login_request_seen=%s, login_response_seen=%s",
                max_wait_sec,
                state.login_request_seen,
                state.login_response_seen,
            )
            state.notes.append(f"Timeout de {max_wait_sec}s alcanzado sin obtener el Bearer token.")

        try:
            context.storage_state(path=str(storage_state_path))
        except Exception as exc:  # noqa: BLE001
            log.warning("No se pudo guardar storage state: %s", exc)
            state.notes.append(f"No se pudo guardar storage state: {exc}")

    finally:
        try:
            context.close()
        except Exception as exc:  # noqa: BLE001
            log.debug("Error al cerrar context: %s", exc)

    # Serializar y guardar resultado
    result = {
        **state.to_dict(),
        "app_url": app_url,
        "login_api_url": login_api_url,
        "user_data_dir": str(user_data_dir),
        "storage_state_path": str(storage_state_path),
        "headless": headless,
    }

    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Resultado guardado en: %s", output_json)

    # stdout emite el JSON; con MASK_OUTPUT=true los tokens se enmascaran
    # (útil en n8n / CI donde los logs quedan visibles)
    mask = _env_bool("MASK_OUTPUT", False)
    if mask:
        display = {
            **result,
            "google_id_token": _mask_token(result.get("google_id_token")),
            "erp_bearer_token": _mask_token(result.get("erp_bearer_token")),
        }
        print(json.dumps(display, ensure_ascii=False))
    else:
        print(json.dumps(result, ensure_ascii=False))
    return result


# ---------------------------------------------------------------------------
def main() -> None:
    with sync_playwright() as pw:
        run(pw)


if __name__ == "__main__":
    main()
