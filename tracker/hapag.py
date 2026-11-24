"""Hapag-Lloyd: listado legacy + tabla de movimientos.

Por qué Hapag necesita un módulo aparte y no un `deep_link` como MSC y Maersk:

1. No hay una URL de detalle que se pueda componer. El listado es un formulario
   JSF y el botón `Details` es un `type="button"` manejado por ExtJS que envía un
   POST con el `javax.faces.ViewState` de la sesión. Armar
   `?view=S8510&container=...` devuelve la shell de la SPA nueva, no el detalle.
2. La página de detalle es **HTML con una tabla de movimientos**, no JSON, así
   que la intercepción de respuestas de `browser.py` no la ve.
3. El número del contenedor viene relleno con **dos espacios** en el HTML
   servido (`HLBU  2959982`) y el submit los codifica como `++`
   (`container=HLBU++2959982`). El DOM los colapsa, por eso se lee el HTML.

Este módulo se ocupa de lo que se puede probar sin navegador: leer el listado,
traducir el parámetro, parsear los movimientos y convertirlos al esquema común.
La navegación (marcar el radio, pulsar `Details`) vive en `browser.py`.
"""

from __future__ import annotations

import html as html_lib
import re
from typing import Any

HAPAG_TRACK = "https://www.hapag-lloyd.com/en/online-business/track/"
CONTAINER_LISTING = HAPAG_TRACK + "track-by-container-solution-legacy.html"
BOOKING_LISTING = HAPAG_TRACK + "track-by-booking-solution-legacy.html"

# Estados de la tabla de movimientos, con el texto exacto que emite el sitio.
HAPAG_STATES: tuple[str, ...] = (
    "Gate out empty", "Gate in empty", "Gate out full", "Gate in full",
    "Arrival in", "Departure from", "Loaded", "Discharged",
    "Vessel departed", "Vessel arrived",
)

# El contenedor puede venir con espacios de relleno (`HLBU  2959982`) o ya
# compacto, y el separador puede ser espacio, `+` o `%2B`.
CONTAINER_TOKEN = re.compile(r"\b([A-Z]{4})[\s+]{0,2}(\d{6})[\s+]{0,2}(\d)\b")
_TIME_TOKEN = re.compile(r"^\d{1,2}:\d{2}$")
_DATE_TOKEN = re.compile(r"\d{4}-\d{2}-\d{2}")


def normalize_container(raw: str) -> str:
    """Deja solo letras y dígitos: `HLBU++2959982` -> `HLBU2959982`."""
    return re.sub(r"[^A-Z0-9]", "", (raw or "").upper())


def container_param(raw: str) -> str:
    """Traduce el texto de la celda a la forma que envía el formulario.

    `HLBU  2959982` -> `HLBU++2959982`

    Cada espacio se vuelve `+`, que es exactamente lo que produce el submit del
    formulario. No se usa `quote()` porque escaparía el `+` a `%2B` y el servidor
    no encontraría el contenedor.
    """
    return (raw or "").strip().replace(" ", "+")


def listing_url(container: str, bl: str | None = None) -> str:
    """URL del listado legacy.

    Por defecto se busca **por contenedor**, que es lo que recibe el scraper.
    `bl` existe porque la variante por B/L es la única verificada de punta a
    punta contra el sitio real (`track-by-booking-solution-legacy.html?blno=`),
    y conviene poder usarla sin reescribir el flujo.
    """
    if bl:
        return f"{BOOKING_LISTING}?blno={normalize_container(bl)}"
    return f"{CONTAINER_LISTING}?container={container_param(container)}"


def rows_from_html(page_html: str) -> list[dict[str, Any]]:
    """Filas seleccionables del listado, leídas del HTML servido.

    Se parsea el HTML y no el DOM a propósito: el servidor emite
    `<span class="nonEditableContent">HLBU  2959982</span>` con los dos espacios
    intactos, y el `innerText` del DOM los colapsa a uno. Esos dos espacios son
    significativos porque el submit los convierte en `++`, parte del parámetro.

    `param` es la forma exacta que hay que enviar; `container` es el número
    normalizado, para comparar y para reportar.
    """
    rows: list[dict[str, Any]] = []
    if not page_html:
        return rows

    for tr in re.findall(r"<tr\b[^>]*>(.*?)</tr>", page_html, re.S | re.I):
        radio = re.search(r"<input[^>]*type=[\"']radio[\"'][^>]*>", tr, re.I)
        if not radio:
            continue
        tag = radio.group(0)
        radio_id = re.search(r"id=[\"']([^\"']+)[\"']", tag)
        radio_name = re.search(r"name=[\"']([^\"']+)[\"']", tag)

        cell_html = re.findall(r"<td\b[^>]*>(.*?)</td>", tr, re.S | re.I)
        cells = [
            html_lib.unescape(re.sub(r"<[^>]+>", "", cell)).strip()
            for cell in cell_html
        ]

        raw_container = ""
        for cell in cell_html:
            plain = html_lib.unescape(re.sub(r"<[^>]+>", "", cell))
            if CONTAINER_TOKEN.search(plain):
                raw_container = plain.replace("\r", " ").replace("\n", " ").strip()
                break

        number = normalize_container(raw_container) if raw_container else ""
        if not number:
            continue
        rows.append({
            "container": number,
            "param": container_param(raw_container),
            "radio_id": radio_id.group(1) if radio_id else "",
            "radio_name": radio_name.group(1) if radio_name else "",
            "cells": cells,
            "raw": raw_container,
        })
    return rows


def _cells_from_line(line: str) -> list[str] | None:
    """Celdas de una fila de la tabla de movimientos, o None si no es una."""
    stripped = line.strip()
    if not stripped:
        return None

    if "\t" in line:
        cells = [c.strip() for c in line.split("\t")]
    else:
        # Sin tabuladores se exige que empiece por un estado conocido y se corta
        # por la fecha, que es el ancla fiable de la fila.
        head = next((st for st in HAPAG_STATES if stripped.startswith(st)), None)
        if not head:
            return None
        rest = stripped[len(head):].strip()
        match = _DATE_TOKEN.search(rest)
        if not match:
            return None
        cells = [head, rest[:match.start()].strip(), match.group(0)]
        cells.extend(rest[match.end():].strip().split())

    if not cells or cells[0] not in HAPAG_STATES:
        return None
    return cells


def parse_movements(text: str) -> list[dict[str, str]]:
    """Lee la tabla de movimientos del detalle.

    En texto plano cada fila queda así (celdas separadas por tabulador):

        Discharged\\tMANZANILLO, COL (MX)\\t2026-10-22\\t20:38\\tIQUIQUE EXPRESS\\t2636E

    Se aceptan también filas separadas por espacios: el estado conocido más una
    fecha `YYYY-MM-DD` identifican la fila sin ambigüedad.
    """
    events: list[dict[str, str]] = []
    for raw_line in (text or "").splitlines():
        cells = _cells_from_line(raw_line)
        if cells is None:
            continue

        activity = cells[0].strip()
        city = cells[1].strip() if len(cells) > 1 else ""
        date = cells[2].strip() if len(cells) > 2 else ""

        # La hora puede venir en su propia celda (tabuladores) o pegada al
        # buque (separación por espacios).
        time = ""
        tail = cells[3:]
        if tail and _TIME_TOKEN.match(tail[0].strip()):
            time = tail[0].strip()
            tail = tail[1:]
        else:
            for index, cell in enumerate(tail):
                if _TIME_TOKEN.match(cell.strip()):
                    time = cell.strip()
                    tail = tail[:index] + tail[index + 1:]
                    break

        vessel = tail[0].strip() if tail else ""
        voyage = tail[1].strip() if len(tail) > 1 else ""

        events.append({
            "activity": activity,
            "city": city,
            "date": date,
            "time": time,
            "event_time": f"{date} {time}".strip(),
            "vessel_name": vessel,
            "voyage_num": voyage,
        })
    return events


def to_payload(container: str, events: list[dict[str, str]]) -> dict[str, Any] | None:
    """Convierte los movimientos al esquema anidado que ya entiende `extract`.

    Se reutiliza el esquema de Maersk (`locations[].events[]`) en vez de tener
    una detección de llegada propia: así "¿llegó al puerto?" se responde en un
    solo sitio para las tres navieras, y la prioridad "llegada antes que
    descarga" se aplica igual en Hapag.

    Una ubicación por evento: el evento de Hapag no trae el puerto, lo trae la
    fila, así que agrupar por ciudad no aporta nada y complicaría el mapeo.
    """
    if not events:
        return None

    locations: list[dict[str, Any]] = []
    for event in events:
        payload_event: dict[str, Any] = {"activity": event["activity"]}
        if event.get("event_time"):
            payload_event["event_time"] = event["event_time"]
        if event.get("vessel_name"):
            payload_event["vessel_name"] = event["vessel_name"]
        if event.get("voyage_num"):
            payload_event["voyage_num"] = event["voyage_num"]

        location: dict[str, Any] = {"events": [payload_event]}
        if event.get("city"):
            location["city"] = event["city"]
        locations.append(location)

    return {"containers": [{"container_num": container, "locations": locations}]}


def is_real_detail(text: str) -> bool:
    """¿La página es un detalle real y no la shell de la SPA nueva?

    Se cuentan estados de la tabla en vez de medir el largo del texto: la shell
    de la SPA y el detalle legacy pesan igual en bytes y se distinguen solo por
    el contenido. Un detalle real trae al menos dos estados.
    """
    flat = " ".join((text or "").split()).lower()
    return sum(1 for state in HAPAG_STATES if state.lower() in flat) >= 2


def deep_link(container: str) -> str:
    """URL de entrada al flujo de Hapag."""
    return listing_url(container)
