"""E2E: Camoufox + intercepción de XHR contra un servidor local que emula una naviera.

No sustituye una prueba contra MSC/Maersk/Hapag (eso requiere WARP y salir a
Internet), pero verifica la pieza frágil: que `page.on("response")` capture el
JSON que la SPA pide por fetch, lo puntúe y lo entregue al normalizador.

Uso:  python scripts/e2e_intercept.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracker.carriers import ADAPTERS, CarrierAdapter  # noqa: E402
from tracker.config import load_settings  # noqa: E402
from tracker.extract import extract  # noqa: E402
from tracker.models import Carrier  # noqa: E402
from tracker.browser import BrowserSession  # noqa: E402

TRACKING_JSON = {
    "data": {
        "containerNumber": "MSCU1234567",
        "shipmentEvents": [
            {"eventDate": "2026-11-01T10:00:00Z", "eventName": "Gate Out",
             "locationName": "Callao", "vesselName": "MSC PERU", "voyageNumber": "123E"},
            {"eventDate": "2026-11-12T03:00:00Z", "eventName": "Discharged",
             "locationName": "Shanghai", "vesselName": "MSC PERU", "voyageNumber": "123E"},
        ],
        "eta": "2026-11-30T00:00:00Z",
    }
}

PAGE_HTML = """<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>Track a shipment</title></head>
<body>
  <h1>Rastreo</h1>
  <form onsubmit="event.preventDefault();doTrack();">
    <input name="trackingNumber" id="trackingNumber" type="text" placeholder="Container">
    <button type="submit">Track</button>
  </form>
  <script>
    async function doTrack() {
      const number = document.getElementById('trackingNumber').value || 'MSCU1234567';
      const res = await fetch('/api/tracking/v1/shipments?container=' + encodeURIComponent(number));
      const body = await res.json();
      document.body.insertAdjacentHTML('beforeend', '<pre class="result">' + JSON.stringify(body) + '</pre>');
    }
    document.addEventListener('DOMContentLoaded', () => {
      doTrack();
      setTimeout(doTrack, 900);
    });
  </script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silenciar el log del servidor
        pass

    def do_GET(self):
        if self.path.startswith("/api/tracking"):
            payload = json.dumps(TRACKING_JSON).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        body = PAGE_HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_server() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}"


def patch_adapter(base: str) -> None:
    """Apunta el adaptador MSC al host local para el test."""
    original = ADAPTERS[Carrier.MSC]
    ADAPTERS[Carrier.MSC] = CarrierAdapter(
        carrier=Carrier.MSC,
        display_name="FakeMSC",
        home_url=f"{base}/",
        deep_link=f"{base}/track?a={{container}}",
        url_markers=("127.0.0.1", "localhost"),
        path_hints=("/api/tracking",),
        input_selectors=('#trackingNumber', 'input[name="trackingNumber"]'),
        submit_selectors=('button[type="submit"]',),
        result_selectors=(".result",),
        status_hint_keys=("eventname",),
        post_load_wait_ms=600,
        needs_form_interaction=True,
    )
    return original


async def main() -> int:
    server, base = start_server()
    patch_adapter(base)
    # Se reutiliza la configuración real, solo se neutralizan proxy y warm-up.
    load_settings([
        "--containers", "MSCU1234567",
        "--carriers", "MSC",
        "--dry-run",
        "--output", "resultados-e2e.json",
    ])
    import os
    os.environ.update({
        "SCRAPER_PROXY": "",
        "SCRAPER_HEADLESS": "true",
        "SCRAPER_WARMUP": "false",
        "SCRAPER_WAIT_SECONDS": "15",
        "SCRAPER_MAX_ATTEMPTS": "1",
    })
    settings = load_settings([
        "--containers", "MSCU1234567",
        "--carriers", "MSC",
        "--dry-run",
        "--output", "resultados-e2e.json",
    ])
    print("settings:", settings.proxy_url or "<sin proxy>", "headless:", settings.headless)

    try:
        async with BrowserSession(settings) as session:
            capture = await session.capture_container(Carrier.MSC, "MSCU1234567")

        print("capturado:", capture.payload is not None, "| respuestas vistas:", capture.responses_seen)
        if capture.payload is None:
            print("FALLO: no se capturó ningún JSON")
            return 1
        print("url:", capture.payload.url)
        print("eventos detectados:", capture.payload.has_events, "| prio:", capture.payload.priority)

        outcome = extract(capture.payload.body, "MSCU1234567")
        assert outcome.data is not None, "el normalizador no produjo datos"
        print(json.dumps(outcome.data.to_dict(), indent=2, ensure_ascii=False))

        checks = {
            "estado actual": outcome.data.current_status == "Discharged",
            "eventos = 2": len(outcome.data.events) == 2,
            "ETA": outcome.data.eta == "2026-11-30T00:00:00Z",
            "ubicación": outcome.data.location == "Shanghai",
            "buque": outcome.data.vessel == "MSC PERU",
        }
        for name, passed in checks.items():
            print(("OK   " if passed else "FALLA") + " " + name)
        return 0 if all(checks.values()) else 1
    finally:
        server.shutdown()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
