"""Sonda: ¿el deep link de MSC dispara el XHR con un B/L?

Compara tres rutas para el mismo B/L:
  1. ?trackingNumber=...            (modo contenedor, 0)
  2. ?trackingMode=1&trackingNumber=...  (modo booking/BL, 1)
  3. formulario (#trackingNumber + Enter)

Solo observa. Sirve para decidir la estrategia de navegación del adaptador.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from camoufox.async_api import AsyncCamoufox  # noqa: E402

BL = "MEDUADP85690"

ROUTES = {
    "modo0_deeplink": f"https://www.msc.com/en/track-a-shipment?trackingNumber={BL}",
    "modo1_deeplink": f"https://www.msc.com/en/track-a-shipment?trackingMode=1&trackingNumber={BL}",
}


async def probe_route(browser, name: str, url: str | None) -> None:
    page = await browser.new_page()
    page.set_default_timeout(60_000)
    hits: list[dict] = []
    pending: set[asyncio.Task] = set()

    async def on_response(resp):
        try:
            u = resp.url
            if "TrackingInfo" not in u:
                return
            body = await resp.text()
            hits.append({"url": u, "status": resp.status,
                         "len": len(body), "body": json.loads(body)})
        except Exception:
            pass

    def sched(r):
        t = asyncio.create_task(on_response(r))
        pending.add(t)
        t.add_done_callback(pending.discard)

    page.on("response", sched)

    print(f"\n=== {name}")
    if url:
        await page.goto(url, wait_until="domcontentloaded")
    else:
        await page.goto("https://www.msc.com/en/track-a-shipment", wait_until="domcontentloaded")

    await page.wait_for_timeout(5000)
    for sel in ("#onetrust-accept-btn-handler", 'button[id*="accept" i]'):
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=3000)
                await page.wait_for_timeout(1200)
                break
        except Exception:
            continue

    if url is None:
        # Ruta 3: rellenar el campo y pulsar Enter.
        field = page.locator("#trackingNumber").first
        try:
            await field.wait_for(state="visible", timeout=20000)
            await field.click()
            await field.type(BL, delay=90)
            await field.press("Enter")
            print("  formulario: B/L escrito + Enter")
        except Exception as exc:
            print("  formulario FALLÓ:", type(exc).__name__)

    await page.wait_for_timeout(18000)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    print(f"  TrackingInfo capturados: {len(hits)}")
    for h in hits:
        body = h["body"]
        ok = body.get("IsSuccess") if isinstance(body, dict) else None
        conts = 0
        try:
            conts = len(body["Data"]["BillOfLadings"][0]["ContainersInfo"])
        except Exception:
            pass
        print(f"    HTTP {h['status']} {h['len']}B  IsSuccess={ok}  contenedores={conts}")

    text = await page.evaluate("() => document.body ? document.body.innerText.slice(0,300) : ''")
    print("  texto:", " ".join((text or "").split())[:180])
    await page.close()


async def main() -> None:
    async with AsyncCamoufox(headless=True, geoip=True, locale="es-PE", os="windows",
                             humanize=True, window=(1440, 900)) as browser:
        for name, url in ROUTES.items():
            try:
                await probe_route(browser, name, url)
            except Exception as exc:
                print(f"  {name} ERROR: {type(exc).__name__}: {exc}")
            await asyncio.sleep(3)
        try:
            await probe_route(browser, "modo_formulario", None)
        except Exception as exc:
            print(f"  formulario ERROR: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
