#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import base64
import calendar
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from filelock import FileLock as CrossFileLock, Timeout


DEFAULT_BASE_URL = "https://erp.developers.net"
DEFAULT_TIMEOUT = 30
DEFAULT_PAGE_LIMIT = 200
APP_NAME = "erp_timesheet"
APP_VERSION = "final"


class ERPError(Exception):
    pass


class AuthError(ERPError):
    pass


class TokenHelperError(AuthError):
    pass


class LoginRejectedError(AuthError):
    pass


class AuthExpiredError(AuthError):
    pass


class APIRequestError(ERPError):
    pass


class ValidationError(ERPError):
    pass


class UpsertError(ERPError):
    pass


class LockError(ERPError):
    pass


@dataclass
class ERPToken:
    token: str
    exp: int

    @property
    def expires_at_iso(self) -> str:
        return datetime.fromtimestamp(self.exp, tz=timezone.utc).isoformat()

    def is_valid(self, skew_seconds: int = 300) -> bool:
        return int(time.time()) < (self.exp - skew_seconds)


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stderr,
    )


def safe_json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def read_json_file(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ERPError(f"No se pudo leer JSON de {path}: {exc}") from exc


def ensure_secure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        logging.warning("No se pudo ajustar permisos 700 al directorio %s", path.parent)


def write_json_file_atomic(path: Path, data: Dict[str, Any]) -> None:
    ensure_secure_parent_dir(path)
    tmp_path = path.with_suffix(path.suffix + ".tmp")

    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())

    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        logging.warning("No se pudo ajustar permisos 600 al archivo temporal %s", tmp_path)

    tmp_path.replace(path)

    try:
        os.chmod(path, 0o600)
    except OSError:
        logging.warning("No se pudo ajustar permisos 600 al archivo %s", path)


def decode_jwt_exp(token: str) -> int:
    token = token.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()

    parts = token.split(".")
    if len(parts) != 3:
        raise AuthError("JWT inválido: formato incorrecto.")

    payload_b64 = parts[1]
    padding = "=" * (-len(payload_b64) % 4)

    try:
        payload_json = base64.urlsafe_b64decode(payload_b64 + padding).decode("utf-8")
        payload = json.loads(payload_json)
        exp = payload.get("exp")
        if not isinstance(exp, int):
            raise AuthError("JWT inválido: no contiene exp entero.")
        return exp
    except Exception as exc:
        raise AuthError(f"No se pudo decodificar exp del JWT: {exc}") from exc


def normalize_direct_token(token: str) -> str:
    token = token.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def first_day_of_month_utc(date_str: str) -> str:
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    first = datetime(dt.year, dt.month, 1, 6, 0, 0, tzinfo=timezone.utc)
    return first.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def last_day_of_month_utc(date_str: str) -> str:
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    last_day = calendar.monthrange(dt.year, dt.month)[1]
    last = datetime(dt.year, dt.month, last_day, 6, 0, 0, tzinfo=timezone.utc)
    return last.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def create_date_iso_for_add(date_str: str) -> str:
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    val = datetime(dt.year, dt.month, dt.day, 0, 0, 0, tzinfo=timezone.utc)
    return val.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def create_date_iso_for_update(date_str: str) -> str:
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    val = datetime(dt.year, dt.month, dt.day, 6, 0, 0, tzinfo=timezone.utc)
    return val.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def normalize_entry_date_to_ymd(value: str) -> Optional[str]:
    if not value:
        return None
    try:
        raw = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        return dt.date().isoformat()
    except Exception:
        if "T" in value and len(value) >= 10:
            return value[:10]
        return None


def mask_token(token: str, prefix: int = 10, suffix: int = 6) -> str:
    if not token or len(token) <= prefix + suffix:
        return "***"
    return f"{token[:prefix]}...{token[-suffix:]}"


class FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock: Optional[CrossFileLock] = None

    def __enter__(self):
        ensure_secure_parent_dir(self.path)
        self.lock = CrossFileLock(str(self.path), timeout=0)
        try:
            self.lock.acquire()
        except Timeout as exc:
            raise LockError(f"Ya existe otra ejecución activa. Lock: {self.path}") from exc
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.lock:
            self.lock.release()


class ERPClient:
    def __init__(
        self,
        base_url: str,
        timeout: int = DEFAULT_TIMEOUT,
        session: Optional[requests.Session] = None,
        max_retries: int = 2,
        retry_backoff_seconds: float = 1.5,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": f"{APP_NAME}/{APP_VERSION}",
            }
        )

    def _should_retry(self, status_code: int) -> bool:
        return status_code in (429, 502, 503, 504)

    def _request(
        self,
        method: str,
        path: str,
        *,
        token: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        expected_status: Tuple[int, ...] = (200,),
        retryable: bool = False,
        auth_context: str = "none",
    ) -> requests.Response:
        url = f"{self.base_url}{path}"
        headers: Dict[str, str] = {}

        if token:
            headers["Authorization"] = f"Bearer {token}"

        attempts = self.max_retries + 1 if retryable else 1
        last_exc: Optional[Exception] = None

        for attempt in range(1, attempts + 1):
            try:
                response = self.session.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=params,
                    json=json_body,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < attempts:
                    sleep_s = self.retry_backoff_seconds * attempt
                    logging.warning(
                        "Error de red %s %s. Reintento %s/%s en %.1fs",
                        method, url, attempt, attempts - 1, sleep_s
                    )
                    time.sleep(sleep_s)
                    continue
                raise APIRequestError(f"Error de red al llamar {url}: {exc}") from exc

            if response.status_code in expected_status:
                return response

            if response.status_code in (401, 403):
                if auth_context == "bearer":
                    raise AuthExpiredError(
                        f"El ERP rechazó el JWT actual en {method} {url} con HTTP {response.status_code}."
                    )
                if auth_context == "login":
                    body_preview = response.text[:1000]
                    raise LoginRejectedError(
                        f"El ERP rechazó el login en {method} {url} con HTTP {response.status_code}. "
                        f"Body: {body_preview}"
                    )

            if retryable and attempt < attempts and self._should_retry(response.status_code):
                sleep_s = self.retry_backoff_seconds * attempt
                logging.warning(
                    "HTTP %s en %s %s. Reintento %s/%s en %.1fs",
                    response.status_code, method, url, attempt, attempts - 1, sleep_s
                )
                time.sleep(sleep_s)
                continue

            body_preview = response.text[:1000]
            raise APIRequestError(
                f"Respuesta inesperada {response.status_code} en {method} {url}. "
                f"Body: {body_preview}"
            )

        if last_exc:
            raise APIRequestError(f"Error final al llamar {url}: {last_exc}") from last_exc
        raise APIRequestError(f"Fallo inesperado al llamar {url}")

    def login_with_google_id_token(self, email: str, id_token: str) -> ERPToken:
        payload = {
            "email": email,
            "idToken": id_token,
            "password": id_token,
        }

        resp = self._request(
            "POST",
            "/api/User/Login",
            json_body=payload,
            expected_status=(200,),
            retryable=True,
            auth_context="login",
        )

        data = resp.json()
        token = data.get("token")
        if not token or not isinstance(token, str):
            raise AuthError("La respuesta de /api/User/Login no contiene token válido.")

        exp = decode_jwt_exp(token)
        token = normalize_direct_token(token)
        logging.info(
            "Login ERP exitoso. Token ERP %s expira en %s",
            mask_token(token),
            datetime.fromtimestamp(exp, tz=timezone.utc).isoformat(),
        )
        return ERPToken(token=token, exp=exp)

    def get_timesheets_paged(
        self,
        token: str,
        user_id: int,
        target_date: str,
        client_id_filter: int = 0,
        timesheet_type_id: int = 0,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
        page: int = 1,
    ) -> Dict[str, Any]:
        request_data = {
            "limit": limit,
            "offset": offset,
            "page": page,
            "filters": [],
            "order": "asc",
            "orderBy": "",
            "filtersData": [
                {"key": "startDate", "value": first_day_of_month_utc(target_date)},
                {"key": "endDate", "value": last_day_of_month_utc(target_date)},
                {"key": "status", "value": 1},
                {"key": "clientId", "value": client_id_filter},
                {"key": "userId", "value": user_id},
                {"key": "timesheetTypeId", "value": timesheet_type_id},
            ],
        }

        resp = self._request(
            "GET",
            "/api/TimeSheet/GetTimeSheetsPaged",
            token=token,
            params={"requestData": safe_json_dumps(request_data)},
            expected_status=(200,),
            retryable=True,
            auth_context="bearer",
        )
        return resp.json()

    def create_timesheet(
        self,
        token: str,
        *,
        date_str: str,
        client_id: int,
        hours_product_development: float,
        description: str,
        hours_product_support: float = 0,
        hours_client_support: float = 0,
        is_overtime: bool = False,
        is_holiday: bool = False,
        is_other_not_paid: bool = False,
    ) -> Dict[str, Any]:
        payload = {
            "startDate": create_date_iso_for_add(date_str),
            "isOvertime": is_overtime,
            "clientId": client_id,
            "hoursProductDevelopment": hours_product_development,
            "description": description,
            "hoursProductSupport": hours_product_support,
            "hoursClientSupport": hours_client_support,
            "isHoliday": is_holiday,
            "isOtherNotPaid": is_other_not_paid,
        }

        resp = self._request(
            "POST",
            "/api/TimeSheet",
            token=token,
            json_body=payload,
            expected_status=(200,),
            retryable=False,
            auth_context="bearer",
        )
        return resp.json() if resp.content else {}

    def update_timesheet_from_existing(
        self,
        token: str,
        *,
        existing_entry: Dict[str, Any],
        date_str: str,
        client_id: int,
        description: str,
        hours_product_development: float,
        hours_product_support: float = 0,
        hours_client_support: float = 0,
        is_overtime: bool = False,
        is_holiday: bool = False,
        is_other_not_paid: bool = False,
    ) -> Dict[str, Any]:
        payload = dict(existing_entry)

        payload["startDate"] = create_date_iso_for_update(date_str)
        payload["clientId"] = client_id
        payload["description"] = description
        payload["isOvertime"] = is_overtime
        payload["isHoliday"] = is_holiday
        payload["isOtherNotPaid"] = is_other_not_paid
        payload["hoursProductDevelopment"] = hours_product_development
        payload["hoursProductSupport"] = hours_product_support
        payload["hoursClientSupport"] = hours_client_support
        payload["hours"] = (
            hours_product_development
            + hours_product_support
            + hours_client_support
        )

        resp = self._request(
            "POST",
            "/api/TimeSheet",
            token=token,
            json_body=payload,
            expected_status=(200,),
            retryable=False,
            auth_context="bearer",
        )
        return resp.json() if resp.content else {}


class ERPTokenManager:
    def __init__(
        self,
        client: ERPClient,
        cache_path: Path,
        email: str,
        google_token_helper: str,
        google_token_helper_args: List[str],
        direct_erp_token: str = "",
    ) -> None:
        self.client = client
        self.cache_path = cache_path
        self.email = email
        self.google_token_helper = google_token_helper
        self.google_token_helper_args = google_token_helper_args
        self.direct_erp_token = normalize_direct_token(direct_erp_token)

    def load_direct_token(self) -> Optional[ERPToken]:
        if not self.direct_erp_token:
            return None

        exp = decode_jwt_exp(self.direct_erp_token)
        erp_token = ERPToken(token=self.direct_erp_token, exp=exp)

        if erp_token.is_valid():
            logging.info(
                "Usando ERP token provisto directamente. Expira en %s",
                erp_token.expires_at_iso,
            )
            return erp_token

        logging.warning(
            "El ERP token provisto directamente ya expiró en %s",
            erp_token.expires_at_iso,
        )
        return None

    def load_cached_token(self) -> Optional[ERPToken]:
        raw = read_json_file(self.cache_path)
        if not raw:
            logging.info("No existe cache de token ERP.")
            return None

        token = raw.get("token")
        exp = raw.get("exp")
        if not isinstance(token, str) or not isinstance(exp, int):
            logging.warning("Cache de token inválido, será ignorado.")
            return None

        token = normalize_direct_token(token)
        erp_token = ERPToken(token=token, exp=exp)
        if erp_token.is_valid():
            logging.info("Reutilizando token ERP cacheado. Expira en %s", erp_token.expires_at_iso)
            return erp_token

        logging.info("Token ERP cacheado expirado. Expiró en %s", erp_token.expires_at_iso)
        return None

    def save_token(self, erp_token: ERPToken) -> None:
        write_json_file_atomic(
            self.cache_path,
            {
                "token": erp_token.token,
                "exp": erp_token.exp,
                "savedAt": datetime.now(tz=timezone.utc).isoformat(),
            },
        )

    def invalidate_cached_token(self) -> None:
        try:
            if self.cache_path.exists():
                self.cache_path.unlink()
                logging.info("Cache de token ERP invalidado.")
        except OSError as exc:
            logging.warning("No se pudo invalidar cache de token: %s", exc)

    def get_google_id_token_from_helper(self) -> str:
        if not self.google_token_helper.strip():
            raise TokenHelperError("No se configuró GOOGLE_TOKEN_HELPER.")

        cmd = [self.google_token_helper, *self.google_token_helper_args]
        logging.info("Ejecutando helper de Google: %s", cmd[0])

        try:
            proc = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except subprocess.TimeoutExpired as exc:
            raise TokenHelperError("El helper de Google excedió el timeout de 180s.") from exc
        except FileNotFoundError as exc:
            raise TokenHelperError(f"No se encontró el helper de Google: {cmd[0]}") from exc
        except Exception as exc:
            raise TokenHelperError(f"No se pudo ejecutar el helper de Google: {exc}") from exc

        if proc.returncode != 0:
            raise TokenHelperError(
                f"Helper de Google retornó código {proc.returncode}. stderr={proc.stderr[:1000]}"
            )

        stdout = proc.stdout.strip()
        if not stdout:
            raise TokenHelperError("El helper de Google no devolvió salida.")

        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise TokenHelperError(
                f"La salida del helper no es JSON válido. stdout={stdout[:500]}"
            ) from exc

        id_token = data.get("idToken")
        erp_bearer = data.get("erpBearerToken")

        if isinstance(id_token, str) and id_token.strip():
            logging.info("Google idToken obtenido correctamente.")
            return {"idToken": id_token}

        if isinstance(erp_bearer, str) and erp_bearer.strip():
            logging.info("ERP bearer obtenido directamente desde sesión activa.")
            return {"erpBearerToken": erp_bearer}

        raise TokenHelperError("El helper no devolvió idToken ni erpBearerToken válido.")

    def refresh_erp_token(self) -> ERPToken:
        helper_result = self.get_google_id_token_from_helper()

        if "erpBearerToken" in helper_result:
            # Sesión activa: Playwright capturó el bearer directamente
            token = helper_result["erpBearerToken"]
            exp = decode_jwt_exp(token)
            erp_token = ERPToken(token=token, exp=exp)
            if not erp_token.is_valid():
                raise AuthExpiredError(
                    f"El bearer capturado por Playwright ya expiró en {erp_token.expires_at_iso}. "
                    "Requiere re-login manual via VNC."
                )
            logging.info("Usando ERP bearer capturado por Playwright. Expira en %s", erp_token.expires_at_iso)
            self.save_token(erp_token)
            return erp_token

        # Flujo normal: login con Google ID token
        id_token = helper_result["idToken"]
        erp_token = self.client.login_with_google_id_token(self.email, id_token)
        self.save_token(erp_token)
        return erp_token

    def get_valid_erp_token(self) -> ERPToken:
        direct = self.load_direct_token()
        if direct:
            return direct

        cached = self.load_cached_token()
        if cached:
            return cached

        return self.refresh_erp_token()


def extract_items_from_paged_response(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    resource_model = data.get("resourceModel")
    if isinstance(resource_model, list):
        return resource_model

    raise APIRequestError(
        "No se pudo identificar la lista de timesheets en resourceModel. "
        f"Keys raíz: {list(data.keys())}"
    )


def get_entry_client_id(entry: Dict[str, Any]) -> Optional[int]:
    item_client_id = entry.get("clientId")
    if item_client_id is None and isinstance(entry.get("client"), dict):
        item_client_id = entry["client"].get("id")
    return item_client_id


def find_entries_for_date(
    items: List[Dict[str, Any]],
    target_date: str,
) -> List[Dict[str, Any]]:
    matches: List[Dict[str, Any]] = []

    for item in items:
        item_date = normalize_entry_date_to_ymd(str(item.get("startDate", "")))
        if item_date == target_date:
            matches.append(item)

    matches.sort(key=lambda x: int(x.get("id", 0) or 0))
    return matches


def validate_hours(
    hours_product_development: float,
    hours_product_support: float,
    hours_client_support: float,
) -> None:
    values = {
        "hoursProductDevelopment": hours_product_development,
        "hoursProductSupport": hours_product_support,
        "hoursClientSupport": hours_client_support,
    }

    for name, value in values.items():
        if value < 0:
            raise ValidationError(f"{name} no puede ser negativo.")
        if value > 24:
            raise ValidationError(f"{name} no puede ser mayor a 24.")

    total = sum(values.values())
    if total <= 0:
        raise ValidationError("La suma total de horas debe ser mayor a 0.")
    if total > 24:
        raise ValidationError("La suma total de horas no puede ser mayor a 24.")


_PROTECTED_DESCRIPTION_KEYWORDS: Tuple[str, ...] = (
    "holiday",
    "time off",
    "paid time off",
    "pto",
    "vacacion",
    "festivo",
    "descanso",
    "permiso",
    "ausencia",
    "day off",
)


def is_protected_entry(entry: Dict[str, Any]) -> Tuple[bool, str]:
    time_sheet_type_id = entry.get("timeSheetTypeId")
    if time_sheet_type_id != 1:
        return True, f"timeSheetTypeId={time_sheet_type_id} (solo se permite editar typeId=1)"

    if entry.get("isHoliday"):
        return True, "isHoliday=True en el entry existente"

    if entry.get("isOtherNotPaid"):
        return True, "isOtherNotPaid=True en el entry existente"

    description = str(entry.get("description") or "").lower()
    for keyword in _PROTECTED_DESCRIPTION_KEYWORDS:
        if keyword in description:
            return True, f"description contiene '{keyword}'"

    return False, ""


def validate_description(description: str) -> None:
    if not description.strip():
        raise ValidationError("La descripción no puede ir vacía.")
    if len(description) > 1000:
        raise ValidationError("La descripción excede 1000 caracteres.")


def select_entry_for_upsert(
    entries_for_day: List[Dict[str, Any]],
    expected_client_id: int,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not entries_for_day:
        return None, None

    for entry in entries_for_day:
        protected, reason = is_protected_entry(entry)
        if protected:
            return entry, reason

    for entry in entries_for_day:
        if entry.get("timeSheetTypeId") != 1:
            continue
        if get_entry_client_id(entry) == expected_client_id:
            return entry, None

    for entry in entries_for_day:
        if entry.get("timeSheetTypeId") == 1:
            other_client_id = get_entry_client_id(entry)
            return entry, f"Existe una entry editable ese día pero con clientId distinto ({other_client_id})"

    return None, None


def upsert_timesheet_once(
    client: ERPClient,
    token: str,
    *,
    user_id: int,
    date_str: str,
    client_id: int,
    description: str,
    hours_product_development: float,
    hours_product_support: float = 0,
    hours_client_support: float = 0,
    is_overtime: bool = False,
    is_holiday: bool = False,
    is_other_not_paid: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    validate_hours(
        hours_product_development=hours_product_development,
        hours_product_support=hours_product_support,
        hours_client_support=hours_client_support,
    )
    validate_description(description)

    paged = client.get_timesheets_paged(
        token=token,
        user_id=user_id,
        target_date=date_str,
    )
    items = extract_items_from_paged_response(paged)

    entries_for_day = find_entries_for_date(items, target_date=date_str)
    selected_entry, decision_reason = select_entry_for_upsert(entries_for_day, client_id)

    if selected_entry and decision_reason:
        entry_id = selected_entry.get("id")
        return {
            "dryRun": dry_run,
            "action": "skipped",
            "date": date_str,
            "entryId": entry_id,
            "skipReason": decision_reason,
        }

    if selected_entry and not decision_reason:
        entry_id = selected_entry.get("id")
        if not isinstance(entry_id, int):
            raise UpsertError(f"La entrada existente no trae id válido: {selected_entry}")

        request_preview = {
            "id": entry_id,
            "startDate": create_date_iso_for_update(date_str),
            "clientId": client_id,
            "description": description,
            "hoursProductDevelopment": hours_product_development,
            "hoursProductSupport": hours_product_support,
            "hoursClientSupport": hours_client_support,
            "hours": hours_product_development + hours_product_support + hours_client_support,
            "isOvertime": is_overtime,
            "isHoliday": is_holiday,
            "isOtherNotPaid": is_other_not_paid,
        }

        if dry_run:
            return {
                "dryRun": True,
                "action": "updated",
                "date": date_str,
                "entryId": entry_id,
                "requestPreview": request_preview,
            }

        response = client.update_timesheet_from_existing(
            token=token,
            existing_entry=selected_entry,
            date_str=date_str,
            client_id=client_id,
            description=description,
            hours_product_development=hours_product_development,
            hours_product_support=hours_product_support,
            hours_client_support=hours_client_support,
            is_overtime=is_overtime,
            is_holiday=is_holiday,
            is_other_not_paid=is_other_not_paid,
        )

        return {
            "dryRun": False,
            "action": "updated",
            "date": date_str,
            "entryId": entry_id,
            "response": response,
        }

    request_preview = {
        "startDate": create_date_iso_for_add(date_str),
        "clientId": client_id,
        "description": description,
        "hoursProductDevelopment": hours_product_development,
        "hoursProductSupport": hours_product_support,
        "hoursClientSupport": hours_client_support,
        "isOvertime": is_overtime,
        "isHoliday": is_holiday,
        "isOtherNotPaid": is_other_not_paid,
    }

    if dry_run:
        return {
            "dryRun": True,
            "action": "created",
            "date": date_str,
            "entryId": None,
            "requestPreview": request_preview,
        }

    response = client.create_timesheet(
        token=token,
        date_str=date_str,
        client_id=client_id,
        hours_product_development=hours_product_development,
        description=description,
        hours_product_support=hours_product_support,
        hours_client_support=hours_client_support,
        is_overtime=is_overtime,
        is_holiday=is_holiday,
        is_other_not_paid=is_other_not_paid,
    )

    created_id = response.get("id") if isinstance(response, dict) else None

    return {
        "dryRun": False,
        "action": "created",
        "date": date_str,
        "entryId": created_id,
        "response": response,
    }


def upsert_timesheet_with_auth_recovery(
    client: ERPClient,
    token_manager: ERPTokenManager,
    *,
    user_id: int,
    date_str: str,
    client_id: int,
    description: str,
    hours_product_development: float,
    hours_product_support: float = 0,
    hours_client_support: float = 0,
    is_overtime: bool = False,
    is_holiday: bool = False,
    is_other_not_paid: bool = False,
    dry_run: bool = False,
) -> Tuple[ERPToken, Dict[str, Any]]:
    erp_token = token_manager.get_valid_erp_token()

    try:
        result = upsert_timesheet_once(
            client=client,
            token=erp_token.token,
            user_id=user_id,
            date_str=date_str,
            client_id=client_id,
            description=description,
            hours_product_development=hours_product_development,
            hours_product_support=hours_product_support,
            hours_client_support=hours_client_support,
            is_overtime=is_overtime,
            is_holiday=is_holiday,
            is_other_not_paid=is_other_not_paid,
            dry_run=dry_run,
        )
        return erp_token, result

    except AuthExpiredError:
        logging.warning("El token ERP fue rechazado por el servidor. Se intentará relogin una vez.")
        token_manager.invalidate_cached_token()
        erp_token = token_manager.refresh_erp_token()

        result = upsert_timesheet_once(
            client=client,
            token=erp_token.token,
            user_id=user_id,
            date_str=date_str,
            client_id=client_id,
            description=description,
            hours_product_development=hours_product_development,
            hours_product_support=hours_product_support,
            hours_client_support=hours_client_support,
            is_overtime=is_overtime,
            is_holiday=is_holiday,
            is_other_not_paid=is_other_not_paid,
            dry_run=dry_run,
        )
        return erp_token, result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Upsert de timesheet en ERP usando login Google -> JWT ERP."
    )

    parser.add_argument("--base-url", default=os.getenv("ERP_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--email", default=os.getenv("ERP_EMAIL"))
    parser.add_argument("--user-id", type=int, default=int(os.getenv("ERP_USER_ID", "0") or "0"))
    parser.add_argument("--client-id", type=int, default=int(os.getenv("ERP_CLIENT_ID", "0") or "0"))
    parser.add_argument("--date", required=True, help="Fecha objetivo en formato YYYY-MM-DD")
    parser.add_argument("--description", required=True)

    parser.add_argument("--hours-product-development", type=float, default=0)
    parser.add_argument("--hours-product-support", type=float, default=0)
    parser.add_argument("--hours-client-support", type=float, default=0)

    parser.add_argument("--is-overtime", action="store_true")
    parser.add_argument("--is-holiday", action="store_true")
    parser.add_argument("--is-other-not-paid", action="store_true")

    parser.add_argument(
        "--erp-token",
        default=os.getenv("ERP_TOKEN", ""),
        help="JWT del ERP ya obtenido. Si se provee, se usa antes que cache/helper.",
    )
    parser.add_argument(
        "--cache-path",
        default=os.getenv("ERP_TOKEN_CACHE_PATH", str(Path.home() / ".cache" / APP_NAME / "erp_token.json")),
    )
    parser.add_argument(
        "--lock-path",
        default=os.getenv("ERP_LOCK_PATH", str(Path.home() / ".cache" / APP_NAME / "run.lock")),
    )
    parser.add_argument(
        "--google-token-helper",
        default=os.getenv("GOOGLE_TOKEN_HELPER", ""),
        help='Ruta ejecutable del helper que imprime JSON: {"idToken":"..."}',
    )
    parser.add_argument(
        "--google-token-helper-args",
        nargs="*",
        default=[],
        help="Argumentos adicionales para el helper de Google",
    )
    parser.add_argument("--timeout", type=int, default=int(os.getenv("ERP_TIMEOUT", str(DEFAULT_TIMEOUT))))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    setup_logging(args.verbose)

    try:
        if not args.email or not args.email.strip() or "@" not in args.email:
            raise ValidationError("Debes indicar un --email válido (ej. usuario@dominio.com).")
        if args.user_id <= 0:
            raise ValidationError("Debes indicar un --user-id válido (> 0).")
        if args.client_id <= 0:
            raise ValidationError("Debes indicar un --client-id válido (> 0).")

        datetime.strptime(args.date, "%Y-%m-%d")

        client = ERPClient(
            base_url=args.base_url,
            timeout=args.timeout,
        )

        token_manager = ERPTokenManager(
            client=client,
            cache_path=Path(args.cache_path),
            email=args.email,
            google_token_helper=args.google_token_helper,
            google_token_helper_args=args.google_token_helper_args,
            direct_erp_token=args.erp_token,
        )

        with FileLock(Path(args.lock_path)):
            erp_token, result = upsert_timesheet_with_auth_recovery(
                client=client,
                token_manager=token_manager,
                user_id=args.user_id,
                date_str=args.date,
                client_id=args.client_id,
                description=args.description,
                hours_product_development=args.hours_product_development,
                hours_product_support=args.hours_product_support,
                hours_client_support=args.hours_client_support,
                is_overtime=args.is_overtime,
                is_holiday=args.is_holiday,
                is_other_not_paid=args.is_other_not_paid,
                dry_run=args.dry_run,
            )

        output = {
            "status": "ok",
            "app": APP_NAME,
            "version": APP_VERSION,
            "tokenExpiresAt": erp_token.expires_at_iso,
            **result,
        }
        print(json.dumps(output, ensure_ascii=False))
        return 0

    except ValidationError as exc:
        logging.error("Error de validación: %s", exc)
        print(json.dumps({"status": "error", "errorType": "ValidationError", "message": str(exc)}, ensure_ascii=False))
        return 2

    except LockError as exc:
        logging.error("Error de lock: %s", exc)
        print(json.dumps({"status": "error", "errorType": "LockError", "message": str(exc)}, ensure_ascii=False))
        return 11

    except TokenHelperError as exc:
        logging.error("Error obteniendo idToken de Google: %s", exc)
        print(json.dumps({"status": "error", "errorType": "TokenHelperError", "message": str(exc)}, ensure_ascii=False))
        return 3

    except LoginRejectedError as exc:
        logging.error("El ERP rechazó el login: %s", exc)
        print(json.dumps({"status": "error", "errorType": "LoginRejectedError", "message": str(exc)}, ensure_ascii=False))
        return 4

    except AuthExpiredError as exc:
        logging.error("El ERP rechazó el JWT incluso tras recuperación: %s", exc)
        print(json.dumps({"status": "error", "errorType": "AuthExpiredError", "message": str(exc)}, ensure_ascii=False))
        return 12

    except AuthError as exc:
        logging.error("Error de autenticación: %s", exc)
        print(json.dumps({"status": "error", "errorType": "AuthError", "message": str(exc)}, ensure_ascii=False))
        return 13

    except APIRequestError as exc:
        logging.error("Error de API: %s", exc)
        print(json.dumps({"status": "error", "errorType": "APIRequestError", "message": str(exc)}, ensure_ascii=False))
        return 5

    except UpsertError as exc:
        logging.error("Error de upsert: %s", exc)
        print(json.dumps({"status": "error", "errorType": "UpsertError", "message": str(exc)}, ensure_ascii=False))
        return 6

    except Exception as exc:
        logging.exception("Error no controlado")
        print(json.dumps({"status": "error", "errorType": "UnhandledError", "message": str(exc)}, ensure_ascii=False))
        return 10


if __name__ == "__main__":
    raise SystemExit(main())