"""Sonda: ¿qué URL/parámetro devuelve el listado legacy de Hapag?

Temporal. Responde con evidencia cuál de las variantes trae filas con radio,
porque el sitio devuelve 403 desde fuera y solo se puede medir en el runner
con WARP. Escribe un JSON con el resultado de cada combinación.
"""

from __future__ import annotations

import asyncio
import json
import sys

from camoufox.async_api import AsyncCamoufox

TRACK = "https://www.hapag-lloyd.com/en/online-business/track/"
CONTAINER = "HLBU2959982"
BL = "HLCUNG12607EKZE0"

# Combinaciones a probar: página legacy + forma del parámetro.
CASES = [
    ("booking_legacy+blno", TRACK + "track-by-booking-solution-legacy.html", f"blno={BL}"),
    ("booking_legacy+container", TRACK + "track-by-booking-solution-legacy.html",
     f"container={CONTAINER}"),
    ("booking_legacy+container+pad", TRACK + "track-by-booking-solution-legacy.html",
     f"container={CONTAINER[:4]}++{CONTAINER[4:]}"),
    ("container_legacy+container", TRACK + "track-by-container-solution-legacy.html",
     f"container={CONTAINER}"),
    ("container_legacy+container+pad", TRACK + "track-by-container-solution-legacy.html",
     f"container={CONTAINER[:4]}++{CONTAINER[4:]}"),
]

RADIO = "type=\"radio\""


async def inspect(page, url: str) -> dict:
    info = {"url": url}
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    except Exception as exc:  # noqa: BLE001
        info["error"] = f"goto: {exc}"
        return info

    # La tabla la pinta ExtJS después; se sondea en vez de esperar a ciegas.
    for _ in range(12):
        await page.wait_for_timeout(1000)
        html = await page.content()
        if RADIO in html:
            break

    html = await page.content()
    info["title"] = (await page.title())[:90] if await page.title() else ""
    info["bytes"] = len(html)
    info["has_radio"] = RADIO in html
    info["has_container_text"] = CONTAINER in html
    info["looks_blocked"] = any(
        marker in html.lower()
        for marker in ("checking your browser", "just a moment", "managed challenge")
    )
    # Cuántas filas seleccionables hay, que es lo que consume el scraper.
    info["rows"] = len([1 for chunk in html.split("<tr") if RADIO in chunk])
    # Primeros indicios de si el número aparece con espacios de relleno.
    info["has_padded_number"] = "HLBU  2959982" in html
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
            print(f"── {name}: {url}", flush=True)
            info = await inspect(page, url)
            info["case"] = name
            results.append(info)
            print(f"   radio={info.get('has_radio')} rows={info.get('rows')} "
                  f"blocked={info.get('looks_blocked')} bytes={info.get('bytes')}",
                  flush=True)
        await page.close()

    with open("hapag-probe.json", "w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=2, ensure_ascii=False)

    print("\nRESUMEN")
    for info in results:
        print(f"  {info['case']:<30} radio={info.get('has_radio')} "
              f"rows={info.get('rows')} padded={info.get('has_padded_number')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
