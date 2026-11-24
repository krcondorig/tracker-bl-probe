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
from dataclasses import dataclass, field

from tracker.browser import BrowserSession, CaptureResult, run_with_retry
from tracker.carriers import adapter_for
from tracker.config import ConfigError, Settings, load_settings
from tracker.containers import (
    ParsedContainer,
    carrier_candidates,
    parse,
    split_csv,
)
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
    # Navieras a probar si la principal no reconoce el contenedor. Solo se
    # rellena cuando el prefijo es ambiguo (arrendador) y no hay pista externa.
    alternatives: list[Carrier] = field(default_factory=list)


def build_plan(
    raw_numbers: list[str | tuple[str, str | None]],
    carriers: tuple[Carrier, ...],
    limit: int,
) -> tuple[list[Job], list[ContainerResult]]:
    """Valida y ordena. Devuelve (trabajos a ejecutar, resultados ya decididos).

    Acepta números sueltos o pares (número, naviera). El par lo usa el camino
    del backend: allí ya se sabe de qué naviera es el contenedor cuando viene
    de un B/L. Sin ese dato, los prefijos de arrendador (CAIU, DRYU, FCIU...)
    se descartaban como INVALID sin llegar a consultarse.

    Los contenedores mal formados se resuelven sin red: no tiene sentido gastar
    una sesión de navegador en algo que la naviera va a rechazar.
    """
    pre_resolved: list[ContainerResult] = []
    jobs: list[Job] = []
    allowed = set(carriers)

    for entry in raw_numbers:
        if isinstance(entry, tuple):
            raw, hint = entry
        else:
            raw, hint = entry, None
        parsed = parse(raw, carrier_hint=hint)
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
            # Prefijo de arrendador (CAIU, DRYU, FCIU...). El prefijo no dice de
            # qué naviera es, así que se prueban las habilitadas en vez de
            # descartar el contenedor sin consultar. Antes se marcaba INVALID y
            # 6 de los 8 contenedores del B/L de MSC no se consultaban nunca.
            candidates = carrier_candidates(raw, carriers)
            if not candidates:
                pre_resolved.append(ContainerResult(
                    container_number=parsed.number,
                    carrier=Carrier.UNKNOWN.value,
                    status=ResultStatus.INVALID.value,
                    attempts=0,
                    check_digit_valid=parsed.check_digit_ok,
                    error_code="UNKNOWN_CARRIER",
                    error_message=f"Prefijo {parsed.number[:4]} sin naviera configurada "
                                  f"y no hay navieras habilitadas donde probarlo.",
                ))
                continue
            parsed = parse(raw, carrier_hint=candidates[0])
            jobs.append(Job(parsed=parsed, index=len(jobs),
                            alternatives=candidates[1:]))
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


def order_results(
    results: list[ContainerResult],
    jobs: list[Job],
    pre_resolved: list[ContainerResult],
) -> list[ContainerResult]:
    """Devuelve el reporte en el orden del plan, sin duplicados.

    Agrupar por proxy cambia el orden de ejecución (primero los directos, luego
    los de WARP), pero el JSON de salida no debe cambiar por eso: el backend
    compara por contenedor y un orden distinto ensucia los diffs.

    Los `pre_resolved` van primero: son los descartados sin red, y ya venían
    resueltos desde `build_plan`. Se deduplica por número porque un contenedor
    puede haberse pre-resuelto y además consultado.
    """
    order = {job.parsed.number: position for position, job in enumerate(jobs)}

    seen: set[str] = set()
    ordered: list[ContainerResult] = []
    for result in list(pre_resolved):
        if result.container_number in seen:
            continue
        seen.add(result.container_number)
        ordered.append(result)

    planned = [r for r in results if r.container_number in order]
    planned.sort(key=lambda r: order[r.container_number])
    for result in planned:
        if result.container_number in seen:
            continue
        seen.add(result.container_number)
        ordered.append(result)

    for result in results:
        if result.container_number in seen:
            continue
        seen.add(result.container_number)
        ordered.append(result)

    return ordered


def carrier_attempts(job: Job, settings: Settings) -> list[Carrier]:
    """Navieras a consultar para este trabajo, en orden, sin salir del proxy.

    El proxy se fija al lanzar el navegador, así que un trabajo solo puede usar
    las navieras que comparten el proxy de su naviera principal. Si la principal
    va directa y una alternativa necesita WARP (Hapag por defecto), esa
    alternativa se descarta: probarla desde la sesión directa daría un 403 que
    no dice nada sobre el contenedor y gastaría un intento.

    Las alternativas descartadas no se pierden del todo: si el contenedor
    tampoco está en las demás, el resultado queda NOT_FOUND y la próxima
    ejecución lo reintenta. Consultar bloqueado es peor que no consultar.
    """
    primary = job.parsed.carrier
    proxy = settings.proxy_for(primary)
    plan = [primary]
    for candidate in job.alternatives:
        if settings.proxy_for(candidate) != proxy:
            log.debug("[%s] Se omite %s: usa otro proxy que %s",
                      job.parsed.number, candidate.value, primary.value)
            continue
        plan.append(candidate)
    return plan


def group_by_proxy(jobs: list[Job], settings: Settings) -> dict[str, list[Carrier]]:
    """Agrupa las navieras de los trabajos por el proxy que les toca.

    Hace falta porque el proxy se fija al **lanzar** el navegador: no se puede
    cambiar por pestaña. Sin agrupar habría que elegir un proxy para todos, y
    cualquiera de las dos opciones rompe algo: con WARP global, MSC falla de
    forma intermitente; sin WARP, Hapag devuelve Managed Challenge.

    La clave es el proxy ("" = directo) y el valor las navieras de ese grupo,
    sin repetir y en el orden en que aparecen en el plan.
    """
    groups: dict[str, list[Carrier]] = {}
    for job in jobs:
        # Se registran también las alternativas: un contenedor ambiguo puede
        # acabar consultándose en otra naviera, y ese grupo necesita su sesión
        # (y su warm-up) aunque ningún trabajo empiece por ella.
        for carrier in carrier_attempts(job, settings):
            key = settings.proxy_for(carrier) or ""
            carriers = groups.setdefault(key, [])
            if carrier not in carriers:
                carriers.append(carrier)
    return groups


# --------------------------------------------------------------------------- #
# Ejecución de un contenedor
# --------------------------------------------------------------------------- #
def should_try_alternative(status: str, error_code: str | None) -> bool:
    """¿Merece la pena probar otra naviera con este contenedor?

    Solo cuando la respuesta dice claramente que el contenedor no es de esa
    naviera. Un BLOCKED no se reintenta con otra: la IP ya está señalada y
    desde ella solo empeora. Un TIMEOUT no dice nada sobre la naviera.
    """
    if status != ResultStatus.NOT_FOUND.value:
        return False
    return error_code in (None, "", "NOT_FOUND")


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
        # Prefijo de arrendador sin pista: no se sabe de que naviera es, asi que
        # se prueban las habilitadas. Solo las que comparten el proxy de la
        # sesion: probar Hapag sin WARP devolveria un 403 inutil.
        plan: list[Carrier] = carrier_attempts(job, settings)
        total_attempts = 0

        for position, carrier in enumerate(plan):
            capture, attempts = await run_with_retry(
                session, carrier, parsed.number, settings.max_attempts
            )
            total_attempts += attempts
            result.carrier = carrier.value
            result.attempts = total_attempts

            if capture.payload is None:
                status, code = _failure_reason(capture)
                result.status = status
                result.error_code = code
                result.error_message = capture.block_reason or (
                    f"Sin payload JSON tras {attempts} intento(s). "
                    f"Título: {capture.page_title or 'n/d'}. "
                    f"URLs vistas: {', '.join(u.split('?')[0][:80] for u in capture.candidate_urls) or 'ninguna'}"
                )
                log.warning("[%s] %s en %s — %s", parsed.number, status,
                            carrier.value, result.error_message[:180])
            else:
                outcome = extract(capture.payload.body, parsed.number,
                                  port=settings.discharge_port)
                if outcome.data is not None:
                    result.status = ResultStatus.OK.value
                    result.data = outcome.data
                    result.source_url = capture.payload.url
                    result.error_code = None
                    result.error_message = None
                    log.info(
                        "[%s] OK en %s (%d eventos) — %s",
                        parsed.number, carrier.value, outcome.event_count,
                        outcome.data.current_status or "sin estado",
                    )
                    return result
                if outcome.not_found:
                    result.status = ResultStatus.NOT_FOUND.value
                    result.error_code = "NOT_FOUND"
                    result.error_message = outcome.message or "La naviera no conoce el contenedor."
                    result.source_url = capture.payload.url
                    log.info("[%s] No encontrado en %s", parsed.number, carrier.value)
                elif outcome.blocked:
                    result.status = ResultStatus.BLOCKED.value
                    result.error_code = "ANTIBOT_CHALLENGE"
                    result.error_message = outcome.message
                else:
                    result.status = ResultStatus.ERROR.value
                    result.error_code = "UNRECOGNIZED_SCHEMA"
                    result.error_message = outcome.message or "Payload JSON sin esquema de tracking reconocible."
                    result.source_url = capture.payload.url
                    log.warning("[%s] Payload no reconocido (%s)",
                                parsed.number, capture.payload.url[:120])

            # Solo se pasa a la siguiente naviera si la actual dijo claramente
            # que el contenedor no es suyo. Bloqueos y timeouts no se escalan.
            has_more = position < len(plan) - 1
            if not (has_more and should_try_alternative(result.status, result.error_code)):
                return result
            log.info("[%s] Se prueba la siguiente naviera: %s",
                     parsed.number, plan[position + 1].value)
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

    # Un navegador por proxy. El proxy se fija al lanzarlo, así que las navieras
    # que van directas y las que van por WARP no pueden compartir sesión. Se
    # agrupa en vez de lanzar un navegador por contenedor: arrancar Firefox
    # cuesta ~5 s y reutilizar cookies ayuda a no parecer un bot.
    groups = group_by_proxy(jobs, settings)
    for proxy, carriers in groups.items():
        label = proxy or "directo"
        log.info("Grupo %s: %s", label, ", ".join(c.value for c in carriers))
        subset = [
            job for job in jobs
            if any(settings.proxy_for(carrier) == (proxy or None)
                   for carrier in carrier_attempts(job, settings))
        ]

        async with BrowserSession(settings, proxy_url=proxy or None) as session:
            if settings.warmup:
                for carrier in carriers:
                    log.info("Calentando sesión en %s (%s)", carrier.value, label)
                    try:
                        await session.warm_up(carrier)
                    except Exception as exc:  # noqa: BLE001 - el warm-up nunca bloquea el lote
                        session_error = f"warmup {carrier.value}: {exc}"
                        log.warning("Warm-up de %s falló: %s", carrier.value, exc)
                    await asyncio.sleep(random.uniform(2.0, 4.0))

            for position, job in enumerate(subset, start=1):
                log.info("── %s %d/%d · %s (%s)", label, position, len(subset),
                         job.parsed.number, job.parsed.carrier.value)
                result = await check_container(session, job, settings)
                results.append(result)

                if position < len(subset):
                    delay = random.uniform(settings.delay_min, settings.delay_max)
                    log.debug("Pausa de %.1fs antes del siguiente contenedor", delay)
                    await asyncio.sleep(delay)

    results = order_results(results, jobs, pre_resolved)

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


def resolve_containers(
    settings: Settings,
) -> tuple[list[str | tuple[str, str | None]], str]:
    """Contenedores desde CLI/entrada o desde el backend. Devuelve (lista, origen).

    Del backend se conserva la naviera que mande: es el único sitio donde llega
    el dato del B/L. Aplanarla a strings obligaba a re-adivinar por el prefijo
    y perdía los contenedores de arrendador.
    """
    explicit = split_csv(settings.containers_raw)
    if explicit:
        log.info("%d contenedores recibidos por parámetro", len(explicit))
        return explicit, "input"

    settings.validate_for_spring()
    log.info("Consultando pendientes al backend %s", settings.spring_base_url)
    with SpringClient(settings) as client:
        pending = client.fetch_pending(limit=settings.max_containers)
    return list(pending), "backend"


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
