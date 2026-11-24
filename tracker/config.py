"""Configuración centralizada: entorno, CLI y validación temprana."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from typing import Sequence

from .models import Carrier


class ConfigError(RuntimeError):
    """Falta una variable obligatoria o tiene un formato inválido."""


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "y", "si", "sí", "on")


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} debe ser un entero, recibido {raw!r}") from exc
    return max(minimum, value)


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} debe ser numérico, recibido {raw!r}") from exc
    return max(minimum, value)


def _parse_carriers(raw: str) -> tuple[Carrier, ...]:
    """'MSC,HAPAG_LLOYD' -> (Carrier.MSC, Carrier.HAPAG_LLOYD).

    Acepta los alias que usa el usuario: HAPAG, HAPAG-LLOYD, HL, MSK.
    """
    aliases = {
        "MSC": Carrier.MSC,
        "MAERSK": Carrier.MAERSK,
        "MSK": Carrier.MAERSK,
        "HAPAG": Carrier.HAPAG_LLOYD,
        "HAPAGLLOYD": Carrier.HAPAG_LLOYD,
        "HAPAG_LLOYD": Carrier.HAPAG_LLOYD,
        "HAPAG-LLOYD": Carrier.HAPAG_LLOYD,
        "HL": Carrier.HAPAG_LLOYD,
        "ALL": Carrier.UNKNOWN,
    }
    if not raw:
        return (Carrier.MSC, Carrier.MAERSK, Carrier.HAPAG_LLOYD)
    selected: list[Carrier] = []
    for chunk in raw.replace(";", ",").split(","):
        token = chunk.strip().upper()
        if not token:
            continue
        carrier = aliases.get(token)
        if carrier is None:
            raise ConfigError(
                f"Naviera desconocida {token!r}. Valores válidos: MSC, MAERSK, HAPAG_LLOYD"
            )
        if carrier is Carrier.UNKNOWN:
            return (Carrier.MSC, Carrier.MAERSK, Carrier.HAPAG_LLOYD)
        if carrier not in selected:
            selected.append(carrier)
    return tuple(selected) or (Carrier.MSC, Carrier.MAERSK, Carrier.HAPAG_LLOYD)


def _parse_proxy_carriers(raw: str) -> tuple[Carrier, ...]:
    """Navieras que salen por el proxy. Vacío = ninguna (todas directas).

    Es una lista de conveniencia, no un contrato: a diferencia de `--carriers`,
    un nombre desconocido no puede tumbar la corrida, solo deja esa naviera sin
    proxy. Se aceptan los mismos alias.
    """
    aliases = {
        "MSC": Carrier.MSC,
        "MAERSK": Carrier.MAERSK,
        "MSK": Carrier.MAERSK,
        "HAPAG": Carrier.HAPAG_LLOYD,
        "HAPAGLLOYD": Carrier.HAPAG_LLOYD,
        "HAPAG_LLOYD": Carrier.HAPAG_LLOYD,
        "HAPAG-LLOYD": Carrier.HAPAG_LLOYD,
        "HL": Carrier.HAPAG_LLOYD,
        "ALL": Carrier.UNKNOWN,
    }
    if not (raw or "").strip():
        return ()
    selected: list[Carrier] = []
    for chunk in raw.replace(";", ",").split(","):
        carrier = aliases.get(chunk.strip().upper())
        if carrier is None:
            continue
        if carrier is Carrier.UNKNOWN:
            return (Carrier.MSC, Carrier.MAERSK, Carrier.HAPAG_LLOYD)
        if carrier not in selected:
            selected.append(carrier)
    return tuple(selected)


@dataclass(frozen=True)
class Settings:
    # Spring Boot
    spring_base_url: str | None
    spring_api_token: str | None
    http_timeout_seconds: float
    http_retries: int

    # Navegador
    proxy_url: str
    # Navieras que salen por el proxy. El proxy se fija al lanzar el navegador y
    # no se puede cambiar por pestaña, así que se elige a quién aplicarlo:
    # Hapag bloquea por reputación de IP y necesita WARP; MSC y Maersk responden
    # bien desde la IP del runner y meterlos por WARP los degrada.
    proxy_carriers: tuple[Carrier, ...]
    # Puerto de descarga que interesa para responder "¿ya llegó?". Se compara
    # contra el Location de los eventos. None desactiva la comprobación.
    discharge_port: str | None
    headless: bool
    locale: str
    humanize: bool
    nav_timeout_ms: int
    wait_seconds: int
    max_attempts: int
    warmup: bool

    # Orquestación
    trigger: str
    containers_raw: str
    carriers: tuple[Carrier, ...]
    delay_min: float
    delay_max: float
    max_containers: int
    dry_run: bool
    output_path: str
    fail_on_total_failure: bool = True

    @property
    def auth_headers(self) -> dict[str, str]:
        if self.spring_api_token:
            return {"Authorization": f"Bearer {self.spring_api_token}"}
        return {}

    def proxy_for(self, carrier: Carrier) -> str | None:
        """Proxy a usar para esta naviera, o None para salir directo.

        Devuelve None si no hay proxy configurado (`SCRAPER_PROXY=""`, que es
        como el workflow desactiva WARP para depurar) o si la naviera no está
        en `SCRAPER_PROXY_CARRIERS`.
        """
        if not self.proxy_url:
            return None
        return self.proxy_url if carrier in self.proxy_carriers else None

    def validate_for_spring(self) -> None:
        if not self.spring_base_url:
            raise ConfigError(
                "SPRING_WEBHOOK_URL no está definida. Sin backend solo puedes usar "
                "--containers para una ejecución manual."
            )
        if not self.spring_api_token:
            raise ConfigError("SPRING_API_TOKEN no está definida (el backend exige Bearer).")


def load_settings(argv: Sequence[str] | None = None) -> Settings:
    parser = argparse.ArgumentParser(
        prog="scraper",
        description="Extractor de tracking marítimo (MSC / Maersk / Hapag-Lloyd).",
    )
    parser.add_argument(
        "--containers",
        default=_env("SCRAPER_CONTAINERS"),
        help="Lista separada por comas. Si se omite, se piden al backend.",
    )
    parser.add_argument("--carriers", default=_env("SCRAPER_CARRIERS", ""), help="MSC,MAERSK,HAPAG_LLOYD")
    parser.add_argument("--trigger", default=_env("SCRAPER_TRIGGER", "manual"))
    parser.add_argument("--max-containers", type=int, default=_env_int("SCRAPER_MAX_CONTAINERS", 40))
    parser.add_argument("--output", default=_env("SCRAPER_OUTPUT", "resultados.json"))
    parser.add_argument("--dry-run", action="store_true", default=_env_bool("SCRAPER_DRY_RUN", False))
    args = parser.parse_args(argv)

    delay_min = _env_float("SCRAPER_DELAY_MIN", 6.0)
    delay_max = _env_float("SCRAPER_DELAY_MAX", 12.0)
    if delay_max < delay_min:
        delay_max = delay_min

    settings = Settings(
        spring_base_url=_env("SPRING_WEBHOOK_URL") or None,
        spring_api_token=_env("SPRING_API_TOKEN") or None,
        http_timeout_seconds=_env_float("SCRAPER_HTTP_TIMEOUT", 30.0),
        http_retries=_env_int("SCRAPER_HTTP_RETRIES", 3),
        proxy_url=_env("SCRAPER_PROXY", "socks5://127.0.0.1:40000"),
        proxy_carriers=_parse_proxy_carriers(
            _env("SCRAPER_PROXY_CARRIERS", "HAPAG_LLOYD")
        ),
        discharge_port=(_env("SCRAPER_PORT", "MANZANILLO").strip().upper() or None),
        headless=_env_bool("SCRAPER_HEADLESS", False),
        locale=_env("SCRAPER_LOCALE", "es-PE"),
        humanize=_env_bool("SCRAPER_HUMANIZE", True),
        nav_timeout_ms=_env_int("SCRAPER_NAV_TIMEOUT_MS", 60_000, minimum=5_000),
        wait_seconds=_env_int("SCRAPER_WAIT_SECONDS", 25, minimum=5),
        max_attempts=_env_int("SCRAPER_MAX_ATTEMPTS", 2, minimum=1),
        warmup=_env_bool("SCRAPER_WARMUP", True),
        trigger=args.trigger,
        containers_raw=args.containers or "",
        carriers=_parse_carriers(args.carriers),
        delay_min=delay_min,
        delay_max=delay_max,
        max_containers=args.max_containers,
        dry_run=args.dry_run,
        output_path=args.output,
        fail_on_total_failure=_env_bool("SCRAPER_FAIL_ON_TOTAL_FAILURE", True),
    )
    return settings
