#!/usr/bin/env python3
"""Punto de entrada del extractor de tracking marítimo.

Flujo:
  1. Carga configuración (entorno + CLI).
  2. Si no vienen contenedores por CLI, los pide al backend (GET /pendientes).
  3. Agrupa por naviera (prefijo ISO 6346) y consulta cada uno con Camoufox,
     interceptando las respuestas JSON de red.
  4. Normaliza todo a un único esquema y hace POST al webhook de Spring Boot.

Códigos de salida:
  0  -> al menos una consulta OK (o todo resuelto sin fallos técnicos)
  1  -> fallo de configuración / backend inaccesible / todas las consultas fallaron
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import random
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass

from tracker.browser import BrowserSession, CaptureResult, run_with_retry
from tracker.carriers import adapter_for
from tracker.config import ConfigError, Settings, load_settings
from tracker.containers import ParsedContainer, parse, split_csv
from tracker.extract import extract
from tracker.httpclient import BackendError, SpringClient
from tracker.models import (
    Carrier,
    ContainerResult,
    ResultStatus,
    ResultsPayload,
    RunStats,
    utc_now_iso,
)
from tracker.report import print_summary, write_json, write_step_summary

log = logging.getLogger("tracker")


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
class _ColorFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG: "\033[38;5;244m",
        logging.INFO: "\033[0m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[1;31m",
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if not sys.stderr.isatty():
            return text
        return f"{self.COLORS.get(record.levelno, '')}{text}{self.RESET}"


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_ColorFormatter("%(asctime)s %(levelname)-7s %(name)-16s %(message)s", "%H:%M:%S"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # Playwright y httpx son muy verbosos en DEBUG.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


# --------------------------------------------------------------------------- #
# Planificación
# --------------------------------------------------------------------------- #
@dataclass
class Job:
    parsed: ParsedContainer
    index: int


def build_plan(raw_numbers: list[str], carriers: tuple[Carrier, ...], limit: int) -> tuple[list[Job], list[ContainerResult]]:
    """Valida y ordena. Devuelve (trabajos a ejecutar, resultados ya decididos).

    Los contenedores mal formados se resuelven sin red: no tiene sentido gastar
    una sesión de navegador en algo que la naviera va a rechazar.
    """
    pre_resolved: list[ContainerResult] = []
    jobs: list[Job] = []
    allowed = set(carriers)

    for raw in raw_numbers:
        parsed = parse(raw)
        if not parsed.well_formed:
            pre_resolved.append(ContainerResult(
                container_number=parsed.number or raw.strip().upper()[:11],
                carrier=parsed.carrier.value,
                status=ResultStatus.INVALID.value,
                attempts=0,
                check_digit_valid=False,
                error_code="INVALID_FORMAT",
                error_message=f"'{raw}' no cumple el patrón ISO 6346 (4 letras + 7 dígitos).",
            ))
            continue
        if parsed.carrier is Carrier.UNKNOWN:
            pre_resolved.append(ContainerResult(
                container_number=parsed.number,
                carrier=Carrier.UNKNOWN.value,
                status=ResultStatus.INVALID.value,
                attempts=0,
                check_digit_valid=parsed.check_digit_ok,
                error_code="UNKNOWN_CARRIER",
                error_message=f"Prefijo {parsed.number[:4]} sin naviera configurada.",
            ))
            continue
        if allowed and parsed.carrier not in allowed:
            log.info("[%s] Omitido: %s no está en el filtro de navieras", parsed.number, parsed.carrier.value)
            continue
        jobs.append(Job(parsed=parsed, index=len(jobs)))

    # Mezcla estable por naviera: se consultan todos los MSC, luego Maersk, etc.
    # Así se reutilizan cookies/warm-up y se evita saltar entre dominios en cada
    # iteración (menos peticiones a los sistemas anti-bot por cambio de contexto).
    order = {carrier: position for position, carrier in enumerate(carriers)}
    jobs.sort(key=lambda job: (order.get(job.parsed.carrier, 99), job.index))
    return jobs[:limit], pre_resolved


# --------------------------------------------------------------------------- #
# Ejecución de un contenedor
# --------------------------------------------------------------------------- #
def _failure_reason(capture: CaptureResult) -> tuple[str, str]:
    """Mapea un CaptureResult fallido a (status, error_code) del contrato."""
    if capture.blocked:
        return ResultStatus.BLOCKED.value, "ANTIBOT_CHALLENGE"
    if capture.responses_seen == 0:
        return ResultStatus.TIMEOUT.value, "NO_RESPONSE_CAPTURED"
    return ResultStatus.TIMEOUT.value, "NO_TRACKING_DATA"


async def check_container(session: BrowserSession, job: Job, settings: Settings) -> ContainerResult:
    parsed = job.parsed
    adapter = adapter_for(parsed.carrier)
    started = time.perf_counter()

    result = ContainerResult(
        container_number=parsed.number,
        carrier=parsed.carrier.value,
        status=ResultStatus.ERROR.value,
        check_digit_valid=parsed.check_digit_ok,
        source_url=adapter.deep_link.format(container=parsed.number) if adapter else None,
    )
    if not parsed.check_digit_ok:
        log.warning("[%s] Dígito verificador inválido; se consulta igualmente", parsed.number)

    try:
        capture, attempts = await run_with_retry(
            session, parsed.carrier, parsed.number, settings.max_attempts
        )
        result.attempts = attempts

        if capture.payload is None:
            status, code = _failure_reason(capture)
            result.status = status
            result.error_code = code
            result.error_message = capture.block_reason or (
                f"Sin payload JSON tras {attempts} intento(s). "
                f"Título: {capture.page_title or 'n/d'}. "
                f"URLs vistas: {', '.join(u.split('?')[0][:80] for u in capture.candidate_urls) or 'ninguna'}"
            )
            log.warning("[%s] %s — %s", parsed.number, status, result.error_message[:180])
            return result

        outcome = extract(capture.payload.body, parsed.number)
        if outcome.data is not None:
            result.status = ResultStatus.OK.value
            result.data = outcome.data
            result.source_url = capture.payload.url
            log.info(
                "[%s] OK (%d eventos) — %s",
                parsed.number, outcome.event_count, outcome.data.current_status or "sin estado",
            )
        elif outcome.not_found:
            result.status = ResultStatus.NOT_FOUND.value
            result.error_code = "NOT_FOUND"
            result.error_message = outcome.message or "La naviera no conoce el contenedor."
            result.source_url = capture.payload.url
            log.info("[%s] No encontrado en %s", parsed.number, parsed.carrier.value)
        elif outcome.blocked:
            result.status = ResultStatus.BLOCKED.value
            result.error_code = "ANTIBOT_CHALLENGE"
            result.error_message = outcome.message
        else:
            result.status = ResultStatus.ERROR.value
            result.error_code = "UNRECOGNIZED_SCHEMA"
            result.error_message = outcome.message or "Payload JSON sin esquema de tracking reconocible."
            result.source_url = capture.payload.url
            log.warning("[%s] Payload no reconocido (%s)", parsed.number, capture.payload.url[:120])
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - un contenedor no debe tumbar el lote
        result.status = ResultStatus.ERROR.value
        result.error_code = type(exc).__name__.upper()
        result.error_message = str(exc)[:400]
        log.exception("[%s] Error inesperado", parsed.number)
    finally:
        result.duration_ms = int((time.perf_counter() - started) * 1000)
    return result


# --------------------------------------------------------------------------- #
# Orquestación
# --------------------------------------------------------------------------- #
async def run_async(settings: Settings, containers: list[str]) -> ResultsPayload:
    run_id = uuid.uuid4().hex[:12]
    started_at = utc_now_iso()
    t0 = time.perf_counter()

    jobs, pre_resolved = build_plan(containers, settings.carriers, settings.max_containers)
    log.info(
        "run=%s trigger=%s navieras=%s a_consultar=%d pre_resueltos=%d",
        run_id, settings.trigger, ",".join(c.value for c in settings.carriers), len(jobs), len(pre_resolved),
    )
    if not jobs:
        log.warning("No hay contenedores válidos que consultar.")
        payload = ResultsPayload(
            run_id=run_id, trigger=settings.trigger, started_at=started_at,
            finished_at=utc_now_iso(), duration_ms=0, stats=RunStats(), results=pre_resolved,
        )
        payload.stats = payload.compute_stats()
        return payload

    results: list[ContainerResult] = list(pre_resolved)
    by_carrier = Counter(job.parsed.carrier for job in jobs)
    log.info("Distribución: %s", dict(by_carrier.most_common()))

    session_error: str | None = None
    async with BrowserSession(settings) as session:
        if settings.warmup:
            for carrier in dict.fromkeys(job.parsed.carrier for job in jobs):
                log.info("Calentando sesión en %s", carrier.value)
                try:
                    await session.warm_up(carrier)
                except Exception as exc:  # noqa: BLE001 - el warm-up nunca bloquea el lote
                    session_error = f"warmup {carrier.value}: {exc}"
                    log.warning("Warm-up de %s falló: %s", carrier.value, exc)
                await asyncio.sleep(random.uniform(2.0, 4.0))

        for position, job in enumerate(jobs, start=1):
            log.info("── %d/%d · %s (%s)", position, len(jobs), job.parsed.number, job.parsed.carrier.value)
            result = await check_container(session, job, settings)
            results.append(result)

            if position < len(jobs):
                delay = random.uniform(settings.delay_min, settings.delay_max)
                log.debug("Pausa de %.1fs antes del siguiente contenedor", delay)
                await asyncio.sleep(delay)

    duration_ms = int((time.perf_counter() - t0) * 1000)
    payload = ResultsPayload(
        run_id=run_id,
        trigger=settings.trigger,
        started_at=started_at,
        finished_at=utc_now_iso(),
        duration_ms=duration_ms,
        stats=RunStats(),
        results=results,
        carrier_filter=[c.value for c in settings.carriers],
        meta={
            "python": platform.python_version(),
            "platform": platform.platform(),
            "headless": settings.headless,
            "proxy": bool(settings.proxy_url),
            "warmup": settings.warmup,
            "maxAttempts": settings.max_attempts,
            "delayRange": [settings.delay_min, settings.delay_max],
            "sessionError": session_error,
            "githubRunId": os.getenv("GITHUB_RUN_ID"),
            "githubActor": os.getenv("GITHUB_ACTOR"),
        },
    )
    payload.stats = payload.compute_stats()
    return payload


def resolve_containers(settings: Settings) -> tuple[list[str], str]:
    """Contenedores desde CLI/entrada o desde el backend. Devuelve (lista, origen)."""
    explicit = split_csv(settings.containers_raw)
    if explicit:
        log.info("%d contenedores recibidos por parámetro", len(explicit))
        return explicit, "input"

    settings.validate_for_spring()
    log.info("Consultando pendientes al backend %s", settings.spring_base_url)
    with SpringClient(settings) as client:
        pending = client.fetch_pending(limit=settings.max_containers)
    return split_csv(",".join(pending)), "backend"


def main(argv: list[str] | None = None) -> int:
    configure_logging(os.getenv("SCRAPER_LOG_LEVEL", "INFO"))
    try:
        settings = load_settings(argv)
    except ConfigError as exc:
        log.error("Configuración inválida: %s", exc)
        return 1

    try:
        containers, source = resolve_containers(settings)
    except (ConfigError, BackendError) as exc:
        log.error("No se pudo obtener la lista de contenedores: %s", exc)
        return 1

    if not containers:
        log.warning("La lista de contenedores está vacía. Nada que hacer.")
        if settings.spring_base_url and source == "backend":
            payload = ResultsPayload(
                run_id=uuid.uuid4().hex[:12],
                trigger=settings.trigger,
                started_at=utc_now_iso(),
                finished_at=utc_now_iso(),
                duration_ms=0,
                stats=RunStats(),
                results=[],
            )
            try:
                with SpringClient(settings) as client:
                    client.post_results(payload)
            except BackendError as exc:
                log.error("No se pudo notificar la ejecución vacía: %s", exc)
        return 0

    try:
        payload = asyncio.run(run_async(settings, containers))
    except KeyboardInterrupt:
        log.warning("Interrumpido por el usuario.")
        return 130
    except Exception as exc:  # noqa: BLE001 - siempre intentamos reportar al backend
        log.exception("Fallo global de la ejecución: %s", exc)
        return 1

    print_summary(payload.results, payload.stats, payload.duration_ms)
    write_step_summary(payload.results, payload.stats, payload.duration_ms, payload.run_id)
    write_json(settings.output_path, payload.to_dict())

    if settings.dry_run:
        log.info("--dry-run activo: no se envía nada al backend.")
    elif settings.spring_base_url:
        try:
            with SpringClient(settings) as client:
                response = client.post_results(payload)
            log.info("Webhook entregado: %s", response)
        except BackendError as exc:
            log.error("No se pudo entregar el webhook: %s", exc)
            return 1
    else:
        log.warning("SPRING_WEBHOOK_URL no definida: resultados solo en %s", settings.output_path)
        return 0

    if settings.fail_on_total_failure and payload.stats.ok == 0 and payload.stats.requested > 0:
        resolvable = payload.stats.ok + payload.stats.not_found
        if resolvable == 0:
            log.error("Ningún contenedor se pudo resolver. Se marca el job como fallido.")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
