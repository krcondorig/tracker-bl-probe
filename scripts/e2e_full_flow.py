"""E2E del flujo completo: backend falso -> plan -> navegador -> POST webhook.

Simula el backend Spring Boot con dos endpoints y verifica que:
  1. Sin `--containers`, el script hace GET /pendientes con Bearer.
  2. Detecta naviera por prefijo y consulta (aquí con un adaptador local).
  3. Envía el POST al webhook con el JSON que espera `ScraperResultsRequest`.
  4. Un contenedor bloqueado se reporta como BLOCKED, no como OK.

Uso:  python scripts/e2e_full_flow.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tracker.carriers import CarrierAdapter  # noqa: E402
from tracker.models import Carrier  # noqa: E402

received: dict[str, object] = {}
TOKEN = "secreto-de-prueba"

TRACKING = {
    "data": {
        "containerNumber": "MSCU1234567",
        "events": [
            {"eventDate": "2026-11-01T10:00:00Z", "eventName": "Gate Out", "locationName": "Callao"},
            {"eventDate": "2026-11-12T03:00:00Z", "eventName": "Discharged", "locationName": "Shanghai"},
        ],
    }
}

SITE = """<!doctype html><html><head><meta charset="utf-8"><title>Fake carrier</title></head>
<body><script>
fetch('/api/tracking?c=1').then(r=>r.json()).then(j=>document.title='ok');
</script></body></html>"""

BLOCKED_SITE = """<!doctype html><html><head><meta charset="utf-8">
<title>Access Denied</title></head><body>Reference #18.abc</body></html>"""


class Backend(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, code: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/pendientes"):
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {TOKEN}":
                self._json(401, {"error": "unauthorized"})
                return
            self._json(200, {"containers": [
                {"containerNumber": "MSCU1234567", "carrier": "MSC", "attempts": 0},
                {"containerNumber": "MSCU9999999", "carrier": "MSC", "attempts": 1},
                {"containerNumber": "BAD123", "carrier": "UNKNOWN", "attempts": 0},
            ]})
            return
        if self.path.startswith("/site"):
            body = SITE.encode()
        elif self.path.startswith("/blocked"):
            body = BLOCKED_SITE.encode()
        elif self.path.startswith("/api/tracking"):
            self._json(200, TRACKING)
            return
        else:
            self._json(404, {})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._json(401, {"error": "unauthorized"})
            return
        received["payload"] = json.loads(body)
        self._json(200, {"updated": len(received["payload"]["results"])})


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    print("backend falso en", base)

    # Redirige el adaptador MSC (en el subproceso, vía runner) al sitio local.
    fake_adapter = CarrierAdapter(
        carrier=Carrier.MSC,
        display_name="FakeMSC",
        home_url=f"{base}/site",
        deep_link=f"{base}/site?c={{container}}",
        url_markers=("127.0.0.1", "localhost"),
        path_hints=("/api/tracking",),
        needs_form_interaction=False,
        post_load_wait_ms=500,
    )

    env = {
        **os.environ,
        "SPRING_WEBHOOK_URL": base,
        "SPRING_API_TOKEN": TOKEN,
        "SCRAPER_PROXY": "",
        "SCRAPER_HEADLESS": "true",
        "SCRAPER_WARMUP": "false",
        "SCRAPER_WAIT_SECONDS": "8",
        "SCRAPER_MAX_ATTEMPTS": "1",
        "SCRAPER_DELAY_MIN": "0",
        "SCRAPER_DELAY_MAX": "0.2",
        # Se antepone ROOT sin descartar el PYTHONPATH existente: así funciona
        # tanto en un venv normal como con dependencias instaladas con --target.
        "PYTHONPATH": os.pathsep.join(
            p for p in (str(ROOT), os.environ.get("PYTHONPATH", "")) if p
        ),
        "PYTHONIOENCODING": "utf-8",
        # El adaptador falso se define en el propio subproceso porque
        # los módulos importados en el padre no se heredan.
        "E2E_FAKE_ADAPTER_JSON": json.dumps({
            "home_url": fake_adapter.home_url,
            "deep_link": fake_adapter.deep_link,
            "url_markers": list(fake_adapter.url_markers),
            "path_hints": list(fake_adapter.path_hints),
            "post_load_wait_ms": fake_adapter.post_load_wait_ms,
        }),
    }

    # Runner: importa los módulos reales, aplica el adaptador falso, parchea la
    # captura del segundo contenedor para simular un challenge de Akamai y
    # llama a scraper.main() sin argumentos (fuerza el GET /pendientes).
    runner = f'''
import sys, json, os
sys.path.insert(0, r"{ROOT}")
from tracker import carriers
from tracker.carriers import CarrierAdapter
from tracker.models import Carrier, ResultStatus
from tracker.browser import CaptureResult
import scraper

cfg = json.loads(os.environ["E2E_FAKE_ADAPTER_JSON"])
carriers.ADAPTERS[Carrier.MSC] = CarrierAdapter(
    carrier=Carrier.MSC,
    display_name="FakeMSC",
    home_url=cfg["home_url"],
    deep_link=cfg["deep_link"],
    url_markers=tuple(cfg["url_markers"]),
    path_hints=tuple(cfg["path_hints"]),
    needs_form_interaction=False,
    post_load_wait_ms=cfg["post_load_wait_ms"],
)

real_capture = scraper.BrowserSession.capture_container

async def capture(self, carrier, container):
    if container.endswith("9999999"):
        page = await self._new_page()
        try:
            await page.goto("{base}/blocked", wait_until="domcontentloaded")
            blocked, reason = await self._detect_block(page)
            title = await page.title()
            seen = await self._safe_title(page)
        finally:
            await page.close()
        return CaptureResult(
            payload=None, blocked=blocked, block_reason=reason,
            page_title=title, responses_seen=0,
        )
    return await real_capture(self, carrier, container)

scraper.BrowserSession.capture_container = capture
sys.exit(scraper.main([]))
'''
    result = subprocess.run(
        [sys.executable, "-c", runner],
        env=env, cwd=str(ROOT), capture_output=True, text=True, timeout=300,
    )
    print(result.stdout[-6000:])
    if result.returncode != 0:
        print("STDERR:", result.stderr[-6000:])

    payload = received.get("payload")
    if not payload:
        print("FALLO: el backend no recibió ningún POST")
        return 1

    by_number = {r["containerNumber"]: r for r in payload["results"]}
    print("\nresultados recibidos:", json.dumps(
        {k: (v["status"], v.get("errorCode")) for k, v in by_number.items()}, indent=2))

    ok_result = by_number.get("MSCU1234567") or {}
    checks = {
        "exit code 0": result.returncode == 0,
        "3 resultados": len(payload["results"]) == 3,
        "MSCU1234567 OK": ok_result.get("status") == "OK",
        "eventos normalizados": len((ok_result.get("data") or {}).get("events") or []) == 2,
        "estado actual": (ok_result.get("data") or {}).get("currentStatus") == "Discharged",
        "MSCU9999999 BLOCKED": by_number.get("MSCU9999999", {}).get("status") == "BLOCKED",
        "BAD123 INVALID": by_number.get("BAD123", {}).get("status") == "INVALID",
        "stats coherentes": payload["stats"]["requested"] == 3 and payload["stats"]["ok"] == 1,
        "trigger=manual": payload["trigger"] == "manual",
    }
    for name, passed in checks.items():
        print(("OK    " if passed else "FALLA ") + name)
    server.shutdown()
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
