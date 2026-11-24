"""Hapag-Lloyd: detalle por contenedor en HTML legacy.

Por qué Hapag necesita un módulo aparte y no un `deep_link` como MSC y Maersk:

1. La página de detalle es **HTML con una tabla de movimientos**, no JSON, así
   que la intercepción de respuestas de `browser.py` no la ve: hay que leer
   `innerText` y parsear filas.
2. El detalle se abre con
   `track-by-container-solution-legacy.html?view=S8510&container=...` y esa URL
   **sí consulta el contenedor pedido**. Medido en el runner con dos
   contenedores del mismo B/L: cada uno devuelve su propia tabla
   (`2026-08-22 14:30` frente a `2026-08-14 06:44`). No hay que rellenar ningún
   formulario.
3. El número puede venir con **dos espacios** de relleno (`HLBU  2959982`) en la
   variante por B/L; el sitio los codifica como `++`
   (`container=HLBU++2959982`), que es la forma del parámetro.

Este módulo se ocupa de lo que se puede probar sin navegador: la URL, el
parámetro, el parseo de la tabla y la conversión al esquema común. La navegación
(abrir, cerrar consentimiento, esperar la tabla) vive en `browser.py`.
"""

from __future__ import annotations

import re
from typing import Any

HAPAG_TRACK = "https://www.hapag-lloyd.com/en/online-business/track/"
CONTAINER_PAGE = HAPAG_TRACK + "track-by-container-solution-legacy.html"

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
    """Traduce el texto de la celda a la forma que envía el sitio.

    `HLBU  2959982` -> `HLBU++2959982`

    Cada espacio se vuelve `+`, que es exactamente lo que produce el sitio. No
    se usa `quote()` porque escaparía el `+` a `%2B` y el servidor no
    encontraría el contenedor.
    """
    return (raw or "").strip().replace(" ", "+")


def padded_container(raw: str) -> str:
    """Devuelve el número con el relleno que emite el sitio: `HLBU  2959982`.

    El listado y el propio formulario usan cuatro letras, dos espacios y siete
    dígitos. Se rellena para pedir la misma forma que se midió funcionando, en
    vez de una variante compacta que no se comprobó.
    """
    number = normalize_container(raw)
    if len(number) != 11:
        return number
    return f"{number[:4]}  {number[4:]}"


def container_page() -> str:
    """Página de entrada del detalle por contenedor.

    Abrirla con `?view=S8510&container=...` ya devuelve el detalle: medido en el
    runner con dos contenedores del mismo B/L, cada uno trae su propia tabla
    (`2026-08-22 14:30` frente a `2026-08-14 06:44`). No hace falta formulario
    ni pulsar `Find`.
    """
    return CONTAINER_PAGE


def detail_url(container: str) -> str:
    """URL navegable del detalle de un contenedor.

    `view=S8510` es la forma exacta que produce el sitio al abrir un detalle, y
    la que se midió funcionando. Se envía además el número con el relleno del
    sitio (`HLBU++2959982`), que es la forma medida; la compacta no se comprobó.

    Sin `view` el sitio también responde, pero no se usa una variante no medida.
    """
    return f"{CONTAINER_PAGE}?view=S8510&container={container_param(padded_container(container))}"


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
