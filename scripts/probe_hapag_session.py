"""Sonda 3 (decisiva): ¿el detalle por `view+container` depende de la URL?

Las sondas anteriores navegaban varias URLs en la misma página, así que el
detalle podía venir del estado de sesión de la búsqueda anterior. Eso explicaría
que dos URLs distintas dieran exactamente el mismo texto (len=3070).

Aquí cada caso usa un CONTEXTO NUEVO (sin cookies ni sesión previa) y además se
comprueba un contenedor de control: si la página muestra el contenedor de la
URL y no el de otro envío, la URL manda. Si no, el detalle viene de la sesión.

Temporal: se borra al decidir.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys

from camoufox.async_api import AsyncCamoufox

TRACK = "https://www.hapag-lloyd.com/en/online-business/track/"
BOOKING = TRACK + "track-by-booking-solution-legacy.html"
CONTAINER_PAGE = TRACK + "track-by-container-solution-legacy.html"

# Dos contenedores del mismo B/L, para el control.
TARGET = "HLBU2959982"
CONTROL = "FTAU1546275"
BL = "HLCUNG12607EKZE0"

CASES = [
    ("booking_view_target", BOOKING, f"view=S8510&container={TARGET[:4]}++{TARGET[4:]}", TARGET, CONTROL),
    ("booking_view_control", BOOKING, f"view=S8510&container={CONTROL[:4]}++{CONTROL[4:]}", CONTROL, TARGET),
    ("container_view_target", CONTAINER_PAGE, f"view=S8510&container={TARGET[:4]}++{TARGET[4:]}", TARGET, CONTROL),
]


def movement_rows(text: str) -> list[str]:
    states = ("Gate out empty", "Gate in empty", "Gate out full", "Gate in full",
              "Arrival in", "Departure from", "Loaded", "Discharged",
              "Vessel departed", "Vessel arrived")
    rows = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        head = next((s for s in states if stripped.startswith(s)), None)
        if head and re.search(r"\d{4}-\d{2}-\d{2}", stripped):
            rows.append(stripped)
    return rows


async def inspect(browser, url: str, target: str, control: str) -> dict:
    info = {"url": url}
    # Contexto nuevo: sin cookies ni estado de búsquedas anteriores.
    context = await browser.new_context(locale="es-PE")
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(9000)
        text = await page.evaluate("() => document.body ? document.body.innerText : ''")
        html = await page.content()

        info["title"] = (await page.title() or "")[:80]
        info["shows_target"] = target in (text or "")
        info["shows_control"] = control in (text or "")
        info["rows"] = len(movement_rows(text or ""))
        info["sample_rows"] = movement_rows(text or "")[:3]
        info["text_len"] = len(text or "")
        info["blocked"] = any(m in html.lower() for m in
                             ("checking your browser", "just a moment", "managed challenge"))
        # Texto alrededor del número, para ver en qué contexto aparece.
        if target in (text or ""):
            pos = text.index(target)
            info["context"] = text[max(0, pos - 120):pos + 80]
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
    options: dict = {"headless": True, "locale": "es-PE", "humanize": True}
    if proxy:
        options["proxy"] = {"server": proxy}

    results = []
    async with AsyncCamoufox(**options) as browser:
        for name, base, query, target, control in CASES:
            url = f"{base}?{query}"
            print(f"── {name}: {url}", flush=True)
            info = await inspect(browser, url, target, control)
            info["case"] = name
            results.append(info)
            print(f"   target={info.get('shows_target')} control={info.get('shows_control')} "
                  f"rows={info.get('rows')} len={info.get('text_len')} "
                  f"blocked={info.get('blocked')}", flush=True)
            if info.get("context"):
                print(f"   ctx: {info['context']!r}", flush=True)
            for row in info.get("sample_rows", []):
                print(f"   fila: {row}", flush=True)

    with open("hapag-session-probe.json", "w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=2, ensure_ascii=False)

    print("\nRESUMEN")
    for info in results:
        print(f"  {info['case']:<24} target={info.get('shows_target')} "
              f"control={info.get('shows_control')} rows={info.get('rows')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
