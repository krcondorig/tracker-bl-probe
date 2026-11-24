"""Normalización y validación de números de contenedor (ISO 6346 + prefijos)."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from .models import Carrier

# ISO 6346: 4 letras (BIC owner code + categoría) + 6 dígitos + dígito verificador.
CONTAINER_PATTERN = re.compile(r"^[A-Z]{4}\d{7}$")

# Tabla oficial de valores para el dígito verificador. Los múltiplos de 11
# (11, 22, 33) se omiten, de ahí los huecos entre J=20 y L=23.
_LETTER_VALUES: dict[str, int] = {
    "A": 10, "B": 12, "C": 13, "D": 14, "E": 15, "F": 16, "G": 17,
    "H": 18, "I": 19, "J": 20, "K": 21, "L": 23, "M": 24, "N": 25,
    "O": 26, "P": 27, "Q": 28, "R": 29, "S": 30, "T": 31, "U": 32,
    "V": 34, "W": 35, "X": 36, "Y": 37, "Z": 38,
}

# Prefijos BIC que usa cada naviera. Un mismo armador opera varios códigos
# (equipos propios + arrendados); se puede ampliar sin tocar código con
# SCRAPER_PREFIX_MAP='{"ABCD":"MSC"}'.
DEFAULT_PREFIXES: dict[str, Carrier] = {
    # MSC
    "MSCU": Carrier.MSC, "MEDU": Carrier.MSC, "MSDU": Carrier.MSC,
    "MSCF": Carrier.MSC, "MSWU": Carrier.MSC, "MSCA": Carrier.MSC,
    # Maersk (incluye A.P. Moller y Maersk Line Limited)
    "MAEU": Carrier.MAERSK, "MRKU": Carrier.MAERSK, "MRSU": Carrier.MAERSK,
    "MSKU": Carrier.MAERSK, "MIEU": Carrier.MAERSK, "APZU": Carrier.MAERSK,
    "MSAU": Carrier.MAERSK,
    # Hapag-Lloyd (SUDU/UACU vinieron de la compra de CP Ships y Süd)
    "HLCU": Carrier.HAPAG_LLOYD, "HLXU": Carrier.HAPAG_LLOYD,
    "HLBU": Carrier.HAPAG_LLOYD, "HLHU": Carrier.HAPAG_LLOYD,
    "SUDU": Carrier.HAPAG_LLOYD, "UACU": Carrier.HAPAG_LLOYD,
    "HLRU": Carrier.HAPAG_LLOYD,
}


def _load_prefix_overrides() -> dict[str, Carrier]:
    raw = os.getenv("SCRAPER_PREFIX_MAP", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    overrides: dict[str, Carrier] = {}
    for prefix, carrier in parsed.items():
        try:
            overrides[str(prefix).upper()[:4]] = Carrier(str(carrier).upper())
        except ValueError:
            continue
    return overrides


PREFIXES: dict[str, Carrier] = {**DEFAULT_PREFIXES, **_load_prefix_overrides()}


def normalize(raw: str) -> str:
    """'mscu 123 456 7' -> 'MSCU1234567'. No valida, solo limpia."""
    return re.sub(r"[^A-Z0-9]", "", (raw or "").upper())


def is_well_formed(container: str) -> bool:
    return bool(CONTAINER_PATTERN.match(container))


def compute_check_digit(container: str) -> int:
    """Calcula el 11.º carácter según ISO 6346.

    Suma valor(carácter) * 2^posición para los 10 primeros caracteres.
    """
    if len(container) < 10:
        raise ValueError(f"Se requieren 10 caracteres, recibidos {len(container)}")
    total = 0
    for index, char in enumerate(container[:10]):
        if char.isdigit():
            value = int(char)
        else:
            value = _LETTER_VALUES.get(char)
            if value is None:
                raise ValueError(f"Carácter no válido en ISO 6346: {char!r}")
        total += value * (2 ** index)
    return total % 11 % 10


def check_digit_valid(container: str) -> bool:
    if not is_well_formed(container):
        return False
    try:
        return compute_check_digit(container) == int(container[10])
    except (ValueError, IndexError):
        return False


def resolve_carrier(container: str) -> Carrier:
    if len(container) < 4:
        return Carrier.UNKNOWN
    return PREFIXES.get(container[:4], Carrier.UNKNOWN)


@dataclass(frozen=True)
class ParsedContainer:
    number: str
    carrier: Carrier
    well_formed: bool
    check_digit_ok: bool


def _coerce_carrier(value: Carrier | str | None) -> Carrier | None:
    """Acepta 'MSC', 'msc' o Carrier.MSC. Devuelve None si no es válido."""
    if value is None:
        return None
    if isinstance(value, Carrier):
        return None if value is Carrier.UNKNOWN else value
    try:
        resolved = Carrier(str(value).strip().upper())
    except ValueError:
        return None
    return None if resolved is Carrier.UNKNOWN else resolved


def parse(raw: str, carrier_hint: Carrier | str | None = None) -> ParsedContainer:
    """Normaliza y clasifica.

    `carrier_hint` es la naviera que se conoce por fuera del número (el B/L del
    que salió, o el backend). Hace falta porque los prefijos de arrendador
    (CAIU, DRYU, FCIU, TRHU...) son ambiguos: el mismo CAIU puede viajar en un
    barco de MSC o de otra naviera, así que el prefijo no identifica nada.

    El prefijo propio sigue mandando. Si el número ya dice MRKU=Maersk, una
    pista equivocada no lo rompe; la pista solo rellena el hueco de UNKNOWN.
    """
    number = normalize(raw)
    by_prefix = resolve_carrier(number)
    hinted = _coerce_carrier(carrier_hint)
    return ParsedContainer(
        number=number,
        carrier=by_prefix if by_prefix is not Carrier.UNKNOWN else (hinted or Carrier.UNKNOWN),
        well_formed=is_well_formed(number),
        check_digit_ok=check_digit_valid(number),
    )


def carrier_candidates(
    raw: str,
    allowed: tuple[Carrier, ...],
    carrier_hint: Carrier | str | None = None,
) -> list[Carrier]:
    """Navieras por las que merece la pena preguntar, en orden de preferencia.

    Con prefijo conocido devuelve solo esa (no se gasta una consulta de más).
    Con prefijo ambiguo y una pista devuelve la pista. Sin ninguna de las dos,
    se prueban las navieras habilitadas: mejor una consulta de más que perder
    el contenedor.
    """
    number = normalize(raw)
    by_prefix = resolve_carrier(number)
    if by_prefix is not Carrier.UNKNOWN:
        return [by_prefix]
    hinted = _coerce_carrier(carrier_hint)
    if hinted is not None:
        return [hinted]
    return list(allowed)


def split_csv(raw: str | None) -> list[str]:
    """Divide la entrada en números de contenedor sin duplicar.

    Acepta comas, punto y coma y saltos de línea como separadores, y tolera
    espacios dentro del número ('MAEU 765 432 1'), algo habitual al copiar de
    un Excel o de un correo.
    """
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for chunk in re.split(r"[,;\r\n]+", raw):
        chunk = chunk.strip()
        if not chunk:
            continue
        # Un chunk puede contener varios contenedores separados solo por espacios.
        found = re.findall(r"[A-Za-z]{4}[\s-]*\d(?:[\s-]*\d){6}", chunk)
        candidates = found or [chunk]
        for candidate in candidates:
            number = normalize(candidate)
            if number and number not in seen:
                seen.add(number)
                out.append(number)
    return out
