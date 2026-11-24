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
    # `activity` es la clave de Maersk ("DISCHARG", "CONTAINER ARRIVAL").
    # Va primero a propósito: en su payload el evento cuelga de un registro de
    # ubicación con {"city": "MANZANILLO", "state": "CL"}, donde `state` es el
    # estado mexicano (Colima), no un estado de envío. Si `state` se mira antes,
    # el status queda en "CL" y la descarga nunca se detecta.
    "activity", "activityname", "statusname", "eventname", "statusdescription",
    "eventdescription", "movename", "movetype", "eventtype", "transportevent",
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
# "vessel" va al final como ultimo recurso: en MSC es un objeto {"IMO": "9320398"}
# y quedarse con el daba el IMO como nombre de buque. _clean_vessel rechaza los
# valores puramente numericos, y el nombre real viene en Detail o en vesselName.
VESSEL_KEYS = ("vesselname", "shipname", "transportname", "vehiclename", "vessel")
VOYAGE_KEYS = ("voyagenumber", "voyagerno", "carriervoyage", "voyageid", "voyage")
# ETA de llegada al PUERTO. "etafinaldelivery" queda fuera a proposito: es la
# entrega al consignatario, que en Maersk llega ~4 semanas despues de descargar.
ETA_KEYS = (
    "podetadate", "podeta", "eta", "estimatedarrival", "estimateddateofarrival",
    "estimatedtimeofarrival", "arrivaldate",
)
FINAL_DELIVERY_KEYS = ("etafinaldelivery", "finaldeliveryeta", "finaleta")

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


def _first_scalar(node: dict, keys: tuple[str, ...]) -> str | None:
    """Primer valor de texto en `node` para alguna de `keys` (normalizadas)."""
    if not isinstance(node, dict):
        return None
    flat = {normalize_key(str(k)): v for k, v in node.items()}
    for key in keys:
        value = flat.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            text = str(value).strip()
            if text:
                return text
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


_NUMERIC_RE = re.compile(r"^[\d\s.,-]+$")


def _clean_vessel(value: str | None) -> str | None:
    """Descarta valores que no son un nombre de buque.

    MSC manda `Vessel: {"IMO": "9320398"}` junto al nombre real en `Detail`.
    Un IMO, un código de 7 cifras o un numero de viaje no identifican al buque
    de forma util, asi que se descartan en vez de propagarlos.
    """
    if not value:
        return None
    text = value.strip()
    if not text or _NUMERIC_RE.match(text):
        return None
    return text


def build_event(entry: dict) -> TrackingEvent | None:
    flat = flatten_scalars(entry)
    status = pick(flat, STATUS_KEYS)
    timestamp = format_timestamp(pick(flat, TIME_KEYS))
    if not status and not timestamp:
        return None

    # MSC guarda ["MSC LAUSANNE VI", "QM639R"] en `detail`. Es la fuente mas
    # fiable del nombre y del viaje cuando el campo vessel es un objeto.
    detail = entry.get("Detail") or entry.get("detail")
    detail_vessel = detail_voyage = None
    if isinstance(detail, list):
        for item in detail:
            if isinstance(item, str) and item.strip() and _clean_vessel(item):
                if detail_vessel is None:
                    detail_vessel = item.strip()
                elif detail_voyage is None:
                    detail_voyage = item.strip()
                    break

    vessel = _clean_vessel(pick(flat, VESSEL_KEYS)) or detail_vessel
    voyage = pick(flat, VOYAGE_KEYS) or detail_voyage

    return TrackingEvent(
        timestamp=timestamp,
        status=status,
        location=pick(flat, LOCATION_KEYS),
        vessel=vessel,
        voyage=voyage,
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


# Estados con los que una naviera marca que el contenedor bajó del buque en
# ese puerto. Se busca solo entre los eventos cuyo Location coincide con el
# puerto de descarga, así que un "Discharged" en otro puerto no cuenta.
# Listas que, por nombre, contienen eventos y no sitios.
_EVENT_LIST_KEYS = frozenset({
    "events", "eventos", "movements", "movimientos", "milestones", "hitos",
    "activities", "actividades", "trackingevents", "eventlist",
})

# `arriv` suelto cubre "CONTAINER ARRIVAL", "Vessel arrived", "Arrival in".
# Es la señal principal: el buque ya está en el puerto.
_ARRIVAL_RE = re.compile(
    r"(arriv|discharg|disembark|descarg|arrib|atrac)",
    re.IGNORECASE,
)
_DEPARTURE_ONLY_RE = re.compile(r"(departure|departed|sail|salida|zarpa)", re.IGNORECASE)


def _port_matches(text: str | None, port: str | None) -> bool:
    if not text or not port:
        return False
    return port.strip().upper() in text.upper()


def find_arrival(
    events: list[TrackingEvent],
    port: str | None,
) -> tuple[bool, str | None, TrackingEvent | None]:
    """¿Hubo llegada en `port`? Devuelve (llegó, fecha, evento).

    En Maersk el mismo día hay dos hitos: "CONTAINER ARRIVAL" (el buque atraca,
    12:28) y "DISCHARG" (el contenedor baja, 23:14). La pregunta es cuándo
    llegó, así que gana el de llegada: el buque ya estaba en puerto antes de
    descargar, y usar la descarga retrasaría la respuesta casi un día.

    Dentro del mismo tipo gana la fecha más temprana: si el contenedor se
    descargó, salió y volvió, la respuesta sigue siendo la primera llegada.
    """
    if not port:
        return False, None, None
    best: TrackingEvent | None = None
    best_rank: tuple[int, datetime] | None = None
    for event in events:
        if not event.status or not _port_matches(event.location, port):
            continue
        if _DEPARTURE_ONLY_RE.search(event.status):
            continue
        if not _ARRIVAL_RE.search(event.status):
            continue
        stamp = parse_timestamp(event.timestamp) if event.timestamp else None
        if stamp is None:
            continue
        rank = (_arrival_priority(event.status), stamp)
        if best_rank is None or rank < best_rank:
            best, best_rank = event, rank
    if best is None:
        return False, None, None
    return True, best.timestamp, best


def _arrival_priority(status: str) -> int:
    """Menor gana: "llegó al puerto" antes que "lo descargaron"."""
    lowered = status.lower()
    if ("arriv" in lowered or "arrib" in lowered or "atrac" in lowered):
        return 0
    if "discharg" in lowered or "descarg" in lowered:
        return 1
    return 2


def extract(payload: Any, container: str, port: str | None = None) -> ExtractionOutcome:
    """Convierte un payload JSON arbitrario en TrackingData normalizado.

    `port` es el puerto de descarga que interesa (p. ej. MANZANILLO). Sin él no
    se puede afirmar si el contenedor llegó: `arrived` queda en False.
    """
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)

    # 1) Buscar la mejor colección de eventos, en cualquier profundidad.
    #    Se prefiere la lista cuyos elementos PARECEN eventos de verdad. Con la
    #    forma de Maersk (locations[].events[]) la lista de ubicaciones puntúa
    #    alto por tener fechas, pero sus elementos son sitios, no eventos.
    best: list[dict] = []
    best_score = 0.0
    for collection in iter_dict_lists(payload):
        score = score_collection(collection)
        if score > best_score:
            best, best_score = collection, score

    # Los eventos anidados se recogen aparte: en Maersk cada ubicación trae los
    # suyos y quedarse con la lista de ubicaciones pierde la descarga.
    nested: list[dict] = []
    for node in iter_dicts(payload):
        for key, value in node.items():
            if normalize_key(str(key)) not in _EVENT_LIST_KEYS or not isinstance(value, list):
                continue
            # El padre describe el sitio ({"city": "MANZANILLO", ...}); el hijo
            # solo trae su código. Sin heredar, el evento queda con "MXZLOSM" y
            # no hay forma de compararlo con el puerto que interesa.
            parent_city = _first_scalar(node, LOCATION_KEYS)
            parent_code = _first_scalar(node, ("locationcode", "code", "unlocode", "geoidcity"))
            for item in value:
                if not isinstance(item, dict):
                    continue
                merged = dict(item)
                if not _first_scalar(merged, LOCATION_KEYS) and parent_city:
                    merged["city"] = parent_city
                if not _first_scalar(merged, ("locationcode", "code", "unlocode")) and parent_code:
                    merged["location_code"] = parent_code
                nested.append(merged)

    events: list[TrackingEvent] = []
    if nested and score_collection(nested) >= best_score:
        for entry in nested:
            event = build_event(entry)
            if event:
                events.append(event)
    elif best_score >= 1.5:
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

    arrived, arrival_date, arrival_event = find_arrival(ordered, port)

    # El buque que descargó es el del evento de llegada, no el del último
    # evento: tras la descarga suele haber camión o tren, sin buque.
    # El respaldo de la cabecera tambien pasa por el filtro: si el payload trae
    # "vessel": "9320398" en la cabecera, seguiria colandose el IMO.
    vessel = (arrival_event.vessel if arrival_event else None) or latest.vessel \
        or _clean_vessel(pick(header_flat, VESSEL_KEYS))
    voyage = (arrival_event.voyage if arrival_event else None) or latest.voyage \
        or pick(header_flat, VOYAGE_KEYS)

    # Si ya llegó, la ETA sobra: se conoce la fecha real de llegada y publicar
    # una estimación junto a ella solo genera contradicciones.
    eta = None if arrived else format_timestamp(pick(header_flat, ETA_KEYS))

    data = TrackingData(
        current_status=latest.status or pick(header_flat, STATUS_KEYS),
        location=latest.location or pick(header_flat, LOCATION_KEYS),
        vessel=vessel,
        voyage=voyage,
        eta=eta,
        last_update=latest.timestamp or format_timestamp(pick(header_flat, TIME_KEYS)),
        events=ordered,
        arrived=arrived,
        arrival_date=arrival_date,
        discharge_port=(port.strip().upper() if port else None),
        final_delivery_eta=format_timestamp(pick(header_flat, FINAL_DELIVERY_KEYS)),
    )
    if data.is_empty():
        return ExtractionOutcome(None, message="Eventos detectados pero sin campos útiles")
    return ExtractionOutcome(data=data, event_count=len(ordered))
