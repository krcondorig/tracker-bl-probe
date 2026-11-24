"""Normalizador genérico de payloads JSON de tracking.

Las tres navieras devuelven esquemas distintos y los cambian sin avisar. En vez
de escribir un mapeo rígido por endpoint, este módulo:

1. Localiza la colección de eventos dentro del JSON (lista de dicts con pinta de
   evento: tienen algo tipo *status* y algo tipo *fecha*), a cualquier profundidad.
2. Extrae campos por prioridad de nombres de clave, normalizando la clave
   (minúsculas, sin separadores) para tolerar `eventTime`, `event_time`,
   `EventTime` o `eventDate`.
3. Si no hay eventos pero el payload tiene mensaje de "no encontrado", lo reporta
   como tal en vez de como payload vacío.

Los adaptadores por naviera solo aportan *pistas* (qué claves buscar primero);
nada se rompe si esas claves dejan de existir.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

from .models import TrackingData, TrackingEvent

STATUS_KEYS = (
    "statusname", "eventname", "statusdescription", "eventdescription",
    "movename", "movetype", "eventtype", "transportevent", "activityname",
    "milestone", "status", "state", "description", "eventstatus",
)
TIME_KEYS = (
    "eventdatetime", "eventtime", "timestamp", "occurredat", "datetime",
    "actualtime", "actualdatetime", "estimatedtime", "date", "time",
)
LOCATION_KEYS = (
    "locationname", "placename", "portname", "facilityname", "locationcity",
    "cityname", "city", "terminalname", "location", "place", "port", "countryname",
)
VESSEL_KEYS = ("vesselname", "vessel", "shipname", "transportname", "vehiclename")
VOYAGE_KEYS = ("voyagenumber", "voyagerno", "carriervoyage", "voyageid", "voyage")
ETA_KEYS = (
    "eta", "estimatedarrival", "estimateddateofarrival", "estimatedtimeofarrival",
    "arrivaldate", "finaleta", "podeta",
)

NOT_FOUND_RE = re.compile(
    r"(not\s*found|no\s+(?:data|results?|records?|shipments?)|invalid\s+(?:container|number|bl)"
    r"|not\s*registered|unable\s+to\s+locate|sin\s+resultados|no\s+(?:se\s+)?encontr)",
    re.IGNORECASE,
)
BLOCKED_RE = re.compile(
    r"(access\s*denied|request\s+unsuccessful|incapsula|reference\s*#|captcha"
    r"|verify\s+you\s+are\s+human|just\s+a\s+moment|unusual\s+traffic)",
    re.IGNORECASE,
)

_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
    "%d-%m-%Y %H:%M",
    "%d-%m-%Y",
    "%d %b %Y %H:%M",
    "%d %b %Y",
    "%b %d, %Y %H:%M",
    "%b %d, %Y",
)


def normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def parse_timestamp(value: Any) -> datetime | None:
    """Acepta epoch (s/ms), ISO-8601 y los formatos textuales habituales."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000.0 if value > 1e11 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.isdigit() and len(text) in (10, 13):
        return parse_timestamp(int(text))
    candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        parsed = None
        for fmt in _DATE_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_timestamp(value: Any) -> str | None:
    """Devuelve ISO-8601 UTC cuando se puede parsear; si no, el texto original."""
    if value in (None, ""):
        return None
    parsed = parse_timestamp(value)
    if parsed is not None:
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    text = str(value).strip()
    return text or None


def iter_dicts(node: Any, depth: int = 0, max_depth: int = 18) -> Iterator[dict]:
    if depth > max_depth or node is None:
        return
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from iter_dicts(value, depth + 1, max_depth)
    elif isinstance(node, list):
        for item in node:
            yield from iter_dicts(item, depth + 1, max_depth)


def iter_dict_lists(node: Any, depth: int = 0, max_depth: int = 18) -> Iterator[list]:
    if depth > max_depth or node is None:
        return
    if isinstance(node, dict):
        for value in node.values():
            yield from iter_dict_lists(value, depth + 1, max_depth)
    elif isinstance(node, list):
        if 2 <= len(node) <= 500 and all(isinstance(i, dict) for i in node[:5]):
            yield node
        for item in node:
            yield from iter_dict_lists(item, depth + 1, max_depth)


def flatten_scalars(node: Any, prefix: str = "", depth: int = 0) -> dict[str, str]:
    """Aplana un dict a {clave_normalizada: texto}.

    Propaga el nombre del padre para que `{"vessel": {"name": "X"}}` produzca
    `vesselname` (y también `name` como respaldo), que es como las tres navieras
    anidan sus objetos. Los niveles superiores ganan sobre los inferiores.
    """
    out: dict[str, str] = {}
    if depth > 6 or not isinstance(node, dict):
        return out
    for key, value in node.items():
        norm = normalize_key(str(key))
        if not norm:
            continue
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            text = str(value).strip()
            if text:
                out.setdefault(norm, text)
                if prefix:
                    out.setdefault(prefix + norm, text)
        elif isinstance(value, dict):
            nested_prefix = (prefix or "") + norm
            for sub_key, sub_value in flatten_scalars(value, nested_prefix, depth + 1).items():
                out.setdefault(sub_key, sub_value)
        elif isinstance(value, list) and 1 <= len(value) <= 4:
            for item in value:
                if isinstance(item, dict):
                    nested_prefix = (prefix or "") + norm
                    for sub_key, sub_value in flatten_scalars(item, nested_prefix, depth + 1).items():
                        out.setdefault(sub_key, sub_value)
    return out


def pick(flat: dict[str, str], priorities: tuple[str, ...]) -> str | None:
    for key in priorities:
        if key in flat:
            return flat[key]
    for key in priorities:
        for flat_key, value in flat.items():
            if key in flat_key:
                return value
    return None


def _looks_like_event(entry: dict) -> tuple[bool, bool]:
    flat = flatten_scalars(entry)
    has_status = pick(flat, STATUS_KEYS) is not None
    has_time = pick(flat, TIME_KEYS) is not None
    return has_status, has_time


def score_collection(entries: list[dict]) -> float:
    """Puntúa una lista como candidata a colección de eventos."""
    if not entries:
        return 0.0
    sample = entries[:25]
    status_hits = 0
    time_hits = 0
    for entry in sample:
        has_status, has_time = _looks_like_event(entry)
        status_hits += int(has_status)
        time_hits += int(has_time)
    ratio = len(sample)
    return (status_hits / ratio) * 2.0 + (time_hits / ratio) * 1.5 + min(len(entries), 40) * 0.05


def build_event(entry: dict) -> TrackingEvent | None:
    flat = flatten_scalars(entry)
    status = pick(flat, STATUS_KEYS)
    timestamp = format_timestamp(pick(flat, TIME_KEYS))
    if not status and not timestamp:
        return None
    return TrackingEvent(
        timestamp=timestamp,
        status=status,
        location=pick(flat, LOCATION_KEYS),
        vessel=pick(flat, VESSEL_KEYS),
        voyage=pick(flat, VOYAGE_KEYS),
    )


def _dedupe(events: list[TrackingEvent]) -> list[TrackingEvent]:
    seen: set[tuple] = set()
    out: list[TrackingEvent] = []
    for event in events:
        key = (event.timestamp, event.status, event.location, event.vessel)
        if key in seen:
            continue
        seen.add(key)
        out.append(event)
    return out


@dataclass
class ExtractionOutcome:
    data: TrackingData | None
    not_found: bool = False
    blocked: bool = False
    event_count: int = 0
    message: str | None = None


def extract(payload: Any, container: str) -> ExtractionOutcome:
    """Convierte un payload JSON arbitrario en TrackingData normalizado."""
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)

    # 1) Buscar la mejor colección de eventos, en cualquier profundidad.
    best: list[dict] = []
    best_score = 0.0
    for collection in iter_dict_lists(payload):
        score = score_collection(collection)
        if score > best_score:
            best, best_score = collection, score

    events: list[TrackingEvent] = []
    if best_score >= 1.5:
        for entry in best:
            event = build_event(entry)
            if event:
                events.append(event)
    else:
        # Payload de un solo evento (algunos endpoints devuelven un objeto plano).
        for entry in iter_dicts(payload):
            event = build_event(entry)
            if event:
                events.append(event)

    events = _dedupe(events)[:120]

    if not events:
        if BLOCKED_RE.search(text):
            return ExtractionOutcome(None, blocked=True, message="Payload con señales de bloqueo")
        if NOT_FOUND_RE.search(text):
            return ExtractionOutcome(None, not_found=True, message="La naviera no devolvió eventos")
        return ExtractionOutcome(None, message="Payload sin eventos reconocibles")

    # 2) Cabecera: el dict "más rico" que no sea un evento suelto.
    header_flat: dict[str, str] = {}
    for node in iter_dicts(payload):
        flat = flatten_scalars(node)
        if len(flat) > len(header_flat):
            header_flat = flat

    ordered = sorted(events, key=lambda e: (parse_timestamp(e.timestamp) or datetime.min.replace(tzinfo=timezone.utc)))
    if all(parse_timestamp(e.timestamp) is None for e in ordered):
        ordered = events  # sin fechas fiables se respeta el orden de la naviera

    latest = ordered[-1] if parse_timestamp(ordered[-1].timestamp) else ordered[0]

    data = TrackingData(
        current_status=latest.status or pick(header_flat, STATUS_KEYS),
        location=latest.location or pick(header_flat, LOCATION_KEYS),
        vessel=latest.vessel or pick(header_flat, VESSEL_KEYS),
        voyage=latest.voyage or pick(header_flat, VOYAGE_KEYS),
        eta=format_timestamp(pick(header_flat, ETA_KEYS)),
        last_update=latest.timestamp or format_timestamp(pick(header_flat, TIME_KEYS)),
        events=ordered,
    )
    if data.is_empty():
        return ExtractionOutcome(None, message="Eventos detectados pero sin campos útiles")
    return ExtractionOutcome(data=data, event_count=len(ordered))
