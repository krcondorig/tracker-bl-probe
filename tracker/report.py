"""Resumen legible: consola, JSON local y GitHub Step Summary."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .models import ContainerResult, ResultStatus, RunStats

log = logging.getLogger(__name__)

STATUS_ICON = {
    ResultStatus.OK: "OK ",
    ResultStatus.NOT_FOUND: "404",
    ResultStatus.BLOCKED: "BLQ",
    ResultStatus.TIMEOUT: "TMO",
    ResultStatus.ERROR: "ERR",
    ResultStatus.INVALID: "INV",
}


def print_summary(results: list[ContainerResult], stats: RunStats, duration_ms: int) -> None:
    log.info("=" * 78)
    log.info("RESUMEN  ok=%d  no_encontrado=%d  fallidos=%d  total=%d  en %.1fs",
             stats.ok, stats.not_found, stats.failed, stats.requested, duration_ms / 1000)
    log.info("-" * 78)
    for result in results:
        icon = STATUS_ICON.get(ResultStatus(result.status), "???")
        detail = result.data.current_status if result.data else (result.error_code or result.error_message or "")
        log.info(
            "%-3s %-12s %-10s %6dms  %s",
            icon, result.container_number, result.carrier, result.duration_ms, (detail or "")[:60],
        )
    if stats.failed:
        log.warning("%d consulta(s) no se pudieron resolver; el backend las reintentará.", stats.failed)
    log.info("=" * 78)


def write_json(path: str, payload: dict) -> None:
    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Resultados escritos en %s", target.resolve())


def write_step_summary(results: list[ContainerResult], stats: RunStats, duration_ms: int, run_id: str) -> None:
    """Escribe en $GITHUB_STEP_SUMMARY; en local no hace nada."""
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    lines = [
        f"## Tracking marítimo — run `{run_id}`",
        "",
        f"Duración: **{duration_ms / 1000:.1f}s** · Consultados: **{stats.requested}** · "
        f"OK: **{stats.ok}** · No encontrados: **{stats.not_found}** · Fallidos: **{stats.failed}**",
        "",
        "| Estado | Contenedor | Naviera | ms | Detalle |",
        "| --- | --- | --- | ---: | --- |",
    ]
    for result in results:
        detail = result.data.current_status if result.data else (result.error_code or "")
        lines.append(
            f"| {result.status} | `{result.container_number}` | {result.carrier} | "
            f"{result.duration_ms} | {(detail or '').replace('|', '/')[:70]} |"
        )
    try:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as exc:
        log.debug("No se pudo escribir el step summary: %s", exc)
