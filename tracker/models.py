"""Modelos de dominio y contrato JSON compartido con Spring Boot."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utc_now_iso() -> str:
    """Timestamp ISO-8601 UTC con milisegundos, formato que espera java.time."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Carrier(str, Enum):
    MSC = "MSC"
    MAERSK = "MAERSK"
    HAPAG_LLOYD = "HAPAG_LLOYD"
    UNKNOWN = "UNKNOWN"


class ResultStatus(str, Enum):
    """Estado de negocio de una consulta.

    OK        -> payload capturado y normalizado
    NOT_FOUND -> la naviera respondió, pero no conoce el contenedor
    BLOCKED   -> challenge anti-bot (Akamai/Cloudflare) o 403
    TIMEOUT   -> no se capturó ningún payload dentro del presupuesto
    ERROR     -> fallo técnico (excepción, payload ilegible)
    INVALID   -> el string no es un número de contenedor ISO 6346 utilizable
    """

    OK = "OK"
    NOT_FOUND = "NOT_FOUND"
    BLOCKED = "BLOCKED"
    TIMEOUT = "TIMEOUT"
    ERROR = "ERROR"
    INVALID = "INVALID"

    @property
    def is_failure(self) -> bool:
        return self in (ResultStatus.BLOCKED, ResultStatus.TIMEOUT, ResultStatus.ERROR)

    @property
    def is_resolved(self) -> bool:
        """Un contenedor resuelto deja de estar pendiente en el backend."""
        return self in (ResultStatus.OK, ResultStatus.NOT_FOUND, ResultStatus.INVALID)


@dataclass
class TrackingEvent:
    timestamp: str | None = None
    status: str | None = None
    location: str | None = None
    vessel: str | None = None
    voyage: str | None = None

    def to_dict(self) -> dict[str, Any]:
        # Se omiten campos vacíos: el frontend distingue "sin dato" de "dato vacío".
        return {k: v for k, v in self.__dict__.items() if v not in (None, "")}


@dataclass
class TrackingData:
    current_status: str | None = None
    location: str | None = None
    vessel: str | None = None
    voyage: str | None = None
    eta: str | None = None
    last_update: str | None = None
    events: list[TrackingEvent] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any(
            (self.current_status, self.location, self.vessel, self.voyage, self.eta, self.events)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "currentStatus": self.current_status,
            "location": self.location,
            "vessel": self.vessel,
            "voyage": self.voyage,
            "eta": self.eta,
            "lastUpdate": self.last_update,
            "events": [e.to_dict() for e in self.events],
        }


@dataclass
class ContainerResult:
    container_number: str
    carrier: str
    status: str
    attempts: int = 1
    duration_ms: int = 0
    scraped_at: str = field(default_factory=utc_now_iso)
    error_code: str | None = None
    error_message: str | None = None
    data: TrackingData | None = None
    source_url: str | None = None
    check_digit_valid: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "containerNumber": self.container_number,
            "carrier": self.carrier,
            "status": self.status,
            "attempts": self.attempts,
            "durationMs": self.duration_ms,
            "scrapedAt": self.scraped_at,
            "errorCode": self.error_code,
            "errorMessage": self.error_message,
            "checkDigitValid": self.check_digit_valid,
            "sourceUrl": self.source_url,
            "data": self.data.to_dict() if self.data else None,
        }


@dataclass
class RunStats:
    requested: int = 0
    ok: int = 0
    not_found: int = 0
    failed: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "requested": self.requested,
            "ok": self.ok,
            "notFound": self.not_found,
            "failed": self.failed,
        }


@dataclass
class ResultsPayload:
    """Cuerpo del POST que consume Spring Boot."""

    run_id: str
    trigger: str
    started_at: str
    finished_at: str
    duration_ms: int
    stats: RunStats
    results: list[ContainerResult]
    carrier_filter: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "runId": self.run_id,
            "trigger": self.trigger,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "durationMs": self.duration_ms,
            "carrierFilter": self.carrier_filter,
            "stats": self.stats.to_dict(),
            "results": [r.to_dict() for r in self.results],
            "meta": self.meta,
        }

    def compute_stats(self) -> RunStats:
        stats = RunStats(requested=len(self.results))
        for result in self.results:
            status = ResultStatus(result.status)
            if status is ResultStatus.OK:
                stats.ok += 1
            elif status is ResultStatus.NOT_FOUND:
                stats.not_found += 1
            elif status.is_failure:
                stats.failed += 1
        return stats
