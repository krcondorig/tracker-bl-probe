"""Sonda 8: elegir la URL definitiva del detalle de Hapag.

Ya está probado que `?view=S8510&container=` devuelve el detalle del contenedor
pedido (dos contenedores dan tablas distintas). Falta decidir dos cosas con
evidencia, porque asumir aquí ya costó dos diagnósticos equivocados:

1. ¿Qué página usar, la de contenedor o la de booking? Ambas pueden renderizar
   el detalle, pero solo una debe ignorar el parámetro.
2. ¿`view=S8510` es obligatorio, o la página de contenedor lo deduce sola?

La prueba de que la URL manda es que el primer movimiento cambie entre los dos
contenedores. Si no cambia, la página está sirviendo estado de sesión.

Temporal: se borra al decidir.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys

from camoufox.async_api import AsyncCamoufox

TRACK = "https://www.hapag-lloyd.com/en/online-business/track/"
CONTAINER_PAGE = TRACK + "track-by-container-solution-legacy.html"
BOOKING_PAGE = TRACK + "track-by-booking-solution-legacy.html"

TARGET = "HLBU2959982"
CONTROL = "FTAU1546275"

STATES = ("Gate out empty", "Gate in empty", "Gate out full", "Gate in full",
          "Arrival in", "Departure from", "Loaded", "Discharged",
          "Vessel departed", "Vessel arrived")

CASES = [
    ("container_view_target", CONTAINER_PAGE, f"view=S8510&container={TARGET[:4]}++{TARGET[4:]}"),
    ("container_view_control", CONTAINER_PAGE, f"view=S8510&container={CONTROL[:4]}++{CONTROL[4:]}"),
    ("container_noview_target", CONTAINER_PAGE, f"container={TARGET[:4]}++{TARGET[4:]}"),
    ("booking_view_target", BOOKING_PAGE, f"view=S8510&container={TARGET[:4]}++{TARGET[4:]}"),
    ("booking_view_control", BOOKING_PAGE, f"view=S8510&container={CONTROL[:4]}++{CONTROL[4:]}"),
]


def movement_rows(text: str) -> list[str]:
    rows = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        head = next((s for s in STATES if stripped.startswith(s)), None)
        if head and re.search(r"\d{4}-\d{2}-\d{2}", stripped):
            rows.append(stripped)
    return rows


async def inspect(browser, url: str) -> dict:
    info = {"url": url}
    context = await browser.new_context(locale="es-PE")
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        rows: list[str] = []
        text = ""
        for _ in range(15):
            await page.wait_for_timeout(1000)
            text = await page.evaluate("() => document.body ? document.body.innerText : ''")
            rows = movement_rows(text or "")
            if len(rows) >= 3:
                break
        info["rows"] = len(rows)
        info["first"] = rows[0] if rows else None
        info["last"] = rows[-1] if rows else None
        info["manzanillo"] = sum(1 for r in rows if "MANZANILLO" in r)
        info["url_after"] = page.url
        info["title"] = (await page.title() or "")[:70]
        # El tipo y las medidas del contenedor distinguen envíos: dos
        # contenedores de 20GP pueden coincidir, así que se guarda el texto.
        with open(f"hapag-url2-{abs(hash(url)) % 100000}.txt", "w", encoding="utf-8") as handle:
            handle.write(text or "")
    except Exception as exc:  # noqa: BLE001
        info["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            await page.close()
            await context.close()
        except Exception:  # noqa: BLE001
            pass
    return info


async def main() -> int:
    proxy = sys.argv[1] if len(sys.argv) > 1 else None
    options: dict = {"headless": True, "locale": "es-PE", "humanize": True, "geoip": True}
    if proxy:
        options["proxy"] = {"server": proxy}

    results = []
    async with AsyncCamoufox(**options) as browser:
        for name, base, query in CASES:
            url = f"{base}?{query}"
            print(f"── {name}", flush=True)
            info = await inspect(browser, url)
            info["case"] = name
            results.append(info)
            print(f"   rows={info.get('rows')} manzanillo={info.get('manzanillo')} "
                  f"err={info.get('error')}", flush=True)
            print(f"   first={info.get('first')!r}", flush=True)

    with open("hapag-url2-probe.json", "w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=2, ensure_ascii=False)

    print("\nRESUMEN")
    for info in results:
        print(f"  {info['case']:<26} rows={info.get('rows')} "
              f"first={info.get('first')!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
