"""Adaptadores por naviera.

Cada adaptador declara:
  * la URL de entrada y el deep link con el contenedor,
  * qué respuestas de red merecen ser interceptadas (host + patrón de path),
  * los selectores para el camino de respaldo (rellenar el formulario a mano),
  * las pistas de claves JSON propias de esa naviera.

Nada de esto es un contrato: si la naviera renombra un endpoint, la
intercepción sigue capturando cualquier JSON del host que parezca de tracking.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote

from .models import Carrier


@dataclass(frozen=True)
class CarrierAdapter:
    carrier: Carrier
    display_name: str
    home_url: str
    deep_link: str
    # Fragmentos que deben aparecer en la URL de la respuesta.
    url_markers: tuple[str, ...]
    # Si el path contiene alguno de estos, es candidato prioritario.
    path_hints: tuple[str, ...] = ()
    input_selectors: tuple[str, ...] = ()
    submit_selectors: tuple[str, ...] = ()
    result_selectors: tuple[str, ...] = ()
    # Claves normalizadas que esta naviera usa para el estado actual.
    status_hint_keys: tuple[str, ...] = ()
    # Ajustes de navegación propios (algunas SPAs necesitan más espera).
    post_load_wait_ms: int = 1500
    needs_form_interaction: bool = False

    def is_candidate(self, url: str, content_type: str = "") -> bool:
        lowered = url.lower()
        if not any(marker in lowered for marker in self.url_markers):
            return False
        if "application/json" in content_type.lower() or "+json" in content_type.lower():
            return True
        if any(hint in lowered for hint in self.path_hints):
            return True
        # Muchas APIs devuelven text/plain o sin content-type declarado.
        return any(ext in lowered for ext in ("/api/", "graphql", "track", "shipment", "container"))


MSC = CarrierAdapter(
    carrier=Carrier.MSC,
    display_name="MSC",
    home_url="https://www.msc.com/en",
    deep_link="https://www.msc.com/en/track-a-shipment?trackingNumber={container}",
    url_markers=("msc.com",),
    path_hints=("/api/", "tracking", "shipment", "track-a-shipment"),
    input_selectors=(
        'input[name="trackingNumber"]',
        'input[id*="tracking" i]',
        'input[placeholder*="container" i]',
        'input[placeholder*="contenedor" i]',
        'input[type="search"]',
        'input[type="text"]',
    ),
    submit_selectors=(
        'button[type="submit"]',
        'button[data-test*="track" i]',
        'button:has-text("Track")',
        'button:has-text("Rastrear")',
        'button:has-text("Search")',
    ),
    result_selectors=(
        "table tbody tr",
        '[class*="tracking-result" i] table tr',
        '[data-test*="event" i]',
    ),
    status_hint_keys=("statusname", "eventname", "movename", "trackingstatus"),
    needs_form_interaction=True,
)

MAERSK = CarrierAdapter(
    carrier=Carrier.MAERSK,
    display_name="Maersk",
    home_url="https://www.maersk.com/",
    deep_link="https://www.maersk.com/tracking/{container}",
    url_markers=("maersk.com",),
    path_hints=("track-and-trace", "/track", "shipment", "events"),
    input_selectors=(
        'input[name="trackingNumber"]',
        'input[data-test*="tracking" i]',
        'input[id*="track" i]',
        'input[placeholder*="container" i]',
        'input[placeholder*="bill" i]',
        'input[type="search"]',
        'input[type="text"]',
    ),
    submit_selectors=(
        'button[type="submit"]',
        'button[data-test*="track" i]',
        'button:has-text("Track")',
        'button:has-text("Rastrear")',
    ),
    result_selectors=(
        '[data-test*="tracking-result" i]',
        '[data-test*="event" i]',
        "table tbody tr",
    ),
    status_hint_keys=("status", "eventtype", "transportevent", "milestone"),
    needs_form_interaction=True,
)

HAPAG_LLOYD = CarrierAdapter(
    carrier=Carrier.HAPAG_LLOYD,
    display_name="Hapag-Lloyd",
    home_url="https://www.hapag-lloyd.com/en/home.html",
    deep_link="https://www.hapag-lloyd.com/en/online-business/track/track-by-container-solution.html?container={container}",
    url_markers=("hapag-lloyd.com", "hlag.com"),
    path_hints=("track", "container", "bl", "shipment", "/api/"),
    input_selectors=(
        'input[name*="container" i]',
        'input[id*="container" i]',
        'input[placeholder*="container" i]',
        'input[placeholder*="contenedor" i]',
        'input[type="search"]',
        'input[type="text"]',
    ),
    submit_selectors=(
        'button[type="submit"]',
        'button[data-test*="track" i]',
        'button:has-text("Track")',
        'button:has-text("Search")',
        'button:has-text("Rastrear")',
    ),
    result_selectors=(
        "table tbody tr",
        '[class*="tracking" i] table tr',
        '[class*="result" i] li',
    ),
    status_hint_keys=("statusname", "eventname", "description", "movename"),
    post_load_wait_ms=4000,
    needs_form_interaction=True,
)

ADAPTERS: dict[Carrier, CarrierAdapter] = {
    Carrier.MSC: MSC,
    Carrier.MAERSK: MAERSK,
    Carrier.HAPAG_LLOYD: HAPAG_LLOYD,
}

# Marcadores de challenge en el documento. Se evalúan sobre el <title> y el
# principio del body, no sobre todo el HTML (los SPAs enormes dan falsos positivos).
BLOCK_MARKERS = re.compile(
    r"(access denied|request unsuccessful|incapsula|reference\s*#|just a moment"
    r"|verify you are human|unusual traffic|attention required|checking your browser"
    r"|cf-chl|px-captcha|datadome|please wait while we verify)",
    re.IGNORECASE,
)


def adapter_for(carrier: Carrier) -> CarrierAdapter | None:
    return ADAPTERS.get(carrier)


def build_deep_link(carrier: Carrier, container: str) -> str:
    adapter = ADAPTERS[carrier]
    return adapter.deep_link.format(container=quote(container), container_lower=quote(container.lower()))


def split_candidates(url: str) -> tuple[str, str]:
    """Extrae (host, path) sin dependencias externas."""
    match = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://([^/?#]+)([^?#]*)", url)
    if not match:
        return "", ""
    return match.group(1).lower(), match.group(2).lower()
