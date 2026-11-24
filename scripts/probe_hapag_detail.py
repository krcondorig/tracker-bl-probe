"""Sonda 2: ¿la página con `view=S8510&container=` es el detalle real?

La sonda anterior vio estados de movimiento en esa página, pero podrían venir
del bundle JS de la SPA y no de una tabla. Aquí se comprueba con marcadores que
solo existen en un detalle de verdad: el buque del envío y fechas de movimiento.

Temporal: se borra al decidir.
"""

from __future__ import annotations

import asyncio
import json
import sys

from camoufox.async_api import AsyncCamoufox

TRACK = "https://www.hapag-lloyd.com/en/online-business/track/"
CONTAINER = "HLBU2959982"
BL = "HLCUNG12607EKZE0"
# Buque real del envío del B/L HLCUNG12607EKZE0 (visto en check_bl).
VESSEL = "IQUIQUE EXPRESS"

CASES = [
    ("blno", TRACK + "track-by-booking-solution-legacy.html", f"blno={BL}"),
    ("view+container_pad", TRACK + "track-by-booking-solution-legacy.html",
     f"view=S8510&container={CONTAINER[:4]}++{CONTAINER[4:]}"),
    ("view+container", TRACK + "track-by-booking-solution-legacy.html",
     f"view=S8510&container={CONTAINER}"),
]


async def inspect(page, url: str) -> dict:
    info = {"url": url}
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    except Exception as exc:  # noqa: BLE001
        info["error"] = f"goto: {exc}"
        return info

    await page.wait_for_timeout(8000)
    html = await page.content()
    try:
        text = await page.evaluate("() => document.body ? document.body.innerText : ''")
    except Exception:  # noqa: BLE001
        text = ""

    info["title"] = (await page.title() or "")[:90]
    info["has_vessel"] = VESSEL.lower() in html.lower()
    info["text_has_vessel"] = VESSEL.lower() in (text or "").lower()
    # Filas de movimiento en texto plano: estado + fecha YYYY-MM-DD.
    import re
    rows = re.findall(r"(?m)^\s*(Gate [a-z]+ \w+|Arrival in|Departure from|Loaded|Discharged|"
                      r"Vessel (?:arrived|departed))\s+.*?\d{4}-\d{2}-\d{2}", text or "")
    info["text_rows"] = len(rows)
    info["sample_rows"] = rows[:4]
    # ¿Sigue habiendo radios (listado) o es el detalle?
    info["has_radio"] = 'type="radio"' in html
    info["text_len"] = len(text or "")
    info["sample"] = (text or "")[:600]
    return info


async def main() -> int:
    proxy = sys.argv[1] if len(sys.argv) > 1 else None
    options: dict = {"headless": True, "locale": "es-PE", "humanize": True}
    if proxy:
        options["proxy"] = {"server": proxy}

    results = []
    async with AsyncCamoufox(**options) as browser:
        page = await browser.new_page()
        for name, base, query in CASES:
            url = f"{base}?{query}"
            print(f"── {name}", flush=True)
            info = await inspect(page, url)
            info["case"] = name
            results.append(info)
            print(f"   vessel={info.get('text_has_vessel')} rows={info.get('text_rows')} "
                  f"radio={info.get('has_radio')} len={info.get('text_len')}", flush=True)
            print(f"   muestra: {info.get('sample', '')[:220]!r}", flush=True)
        await page.close()

    with open("hapag-detail-probe.json", "w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=2, ensure_ascii=False)

    print("\nRESUMEN")
    for info in results:
        print(f"  {info['case']:<22} vessel={info.get('text_has_vessel')} "
              f"rows={info.get('text_rows')} radio={info.get('has_radio')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
