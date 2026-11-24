"""Cliente HTTP hacia Spring Boot: GET /pendientes y POST del webhook."""

from __future__ import annotations

import logging
import random
import time
from typing import Any

import httpx

from .config import Settings
from .models import ResultsPayload

log = logging.getLogger(__name__)


class BackendError(RuntimeError):
    """El backend no está disponible o rechazó la petición tras los reintentos."""


class SpringClient:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._base = (settings.spring_base_url or "").rstrip("/")
        limits = httpx.Limits(max_connections=4, max_keepalive_connections=2)
        self._client = httpx.Client(
            base_url=self._base or "http://localhost",
            timeout=httpx.Timeout(settings.http_timeout_seconds, connect=10.0),
            headers={
                "Accept": "application/json",
                "User-Agent": "maritime-tracker/1.0 (+github-actions)",
                **settings.auth_headers,
            },
            limits=limits,
        )

    def __enter__(self) -> "SpringClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Petición con reintentos exponenciales solo para fallos transitorios."""
        last_error: Exception | None = None
        for attempt in range(1, self._settings.http_retries + 1):
            try:
                response = self._client.request(method, path, **kwargs)
                if response.status_code < 500:
                    return response
                last_error = BackendError(f"HTTP {response.status_code}: {response.text[:300]}")
            except httpx.HTTPError as exc:
                last_error = exc
            if attempt < self._settings.http_retries:
                sleep_for = min(2 ** attempt, 20) + random.uniform(0, 1.5)
                log.warning(
                    "%s %s falló (intento %d/%d): %s. Reintento en %.1fs",
                    method, path, attempt, self._settings.http_retries, last_error, sleep_for,
                )
                time.sleep(sleep_for)
        raise BackendError(f"{method} {path} agotó los reintentos: {last_error}")

    # ---------------------------------------------------------------- lecturas
    def fetch_pending(self, limit: int | None = None) -> list[str]:
        """GET /pendientes -> lista de números de contenedor.

        Acepta las tres formas razonables de respuesta para no acoplar el
        contrato a una sola implementación del backend:
          * ["MSCU1234567", ...]
          * [{"containerNumber": "..."}, ...]
          * {"containers": [...]} / {"data": [...]}
        """
        params = {"limit": limit} if limit else None
        response = self._request("GET", "/pendientes", params=params)
        if response.status_code == 204:
            return []
        if response.status_code >= 400:
            raise BackendError(f"GET /pendientes -> HTTP {response.status_code}: {response.text[:300]}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise BackendError("GET /pendientes no devolvió JSON válido") from exc

        entries: Any
        if isinstance(payload, list):
            entries = payload
        elif isinstance(payload, dict):
            entries = (
                payload.get("containers")
                or payload.get("pendientes")
                or payload.get("data")
                or payload.get("content")
                or []
            )
        else:
            entries = []

        numbers: list[str] = []
        for entry in entries:
            if isinstance(entry, str):
                numbers.append(entry)
            elif isinstance(entry, dict):
                for key in ("containerNumber", "container", "numero", "number", "code"):
                    value = entry.get(key)
                    if isinstance(value, str) and value.strip():
                        numbers.append(value)
                        break
        log.info("Backend devolvió %d contenedores pendientes", len(numbers))
        return numbers

    # ---------------------------------------------------------------- escritura
    def post_results(self, payload: ResultsPayload) -> dict[str, Any]:
        body = payload.to_dict()
        response = self._request("POST", "", json=body)
        if response.status_code >= 400:
            raise BackendError(f"POST webhook -> HTTP {response.status_code}: {response.text[:500]}")
        try:
            return response.json()
        except ValueError:
            return {"status": response.status_code}
