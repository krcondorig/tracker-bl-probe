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


_NUMBER_KEYS = ("containerNumber", "container", "numero", "number", "code")


def parse_pending_entries(entries: Any) -> list[tuple[str, str | None]]:
    """Normaliza la lista de /pendientes a pares (numero, naviera_o_None).

    Antes se devolvian solo los numeros y el carrier se volvia a adivinar por
    el prefijo. Eso pierde informacion: con prefijos de arrendador (CAIU, DRYU,
    FCIU...) el prefijo no identifica naviera y el contenedor se descartaba
    como INVALID sin consultar. Si el backend manda una naviera usable, se
    conserva; si manda UNKNOWN o nada, queda None y decide el prefijo.
    """
    if not isinstance(entries, list):
        return []

    out: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for entry in entries:
        number: str | None = None
        carrier: str | None = None

        if isinstance(entry, str):
            number = entry
        elif isinstance(entry, dict):
            for key in _NUMBER_KEYS:
                value = entry.get(key)
                if isinstance(value, str) and value.strip():
                    number = value
                    break
            raw_carrier = entry.get("carrier")
            if isinstance(raw_carrier, str) and raw_carrier.strip():
                carrier = raw_carrier.strip().upper()
                if carrier in ("", "UNKNOWN", "NULL", "NONE"):
                    carrier = None

        if not number or not number.strip():
            continue
        # Se deduplica en mayusculas: el mismo contenedor con distinta
        # capitalizacion no debe consultarse dos veces.
        normalized = number.strip().upper()
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append((normalized, carrier))
    return out


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
    def fetch_pending(self, limit: int | None = None) -> list[tuple[str, str | None]]:
        """GET /pendientes -> pares (número, naviera opcional).

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

        pending = parse_pending_entries(entries)
        log.info("Backend devolvió %d contenedores pendientes", len(pending))
        return pending

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
