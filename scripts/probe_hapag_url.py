"""Sonda 7 (decisiva): ¿`?view=S8510&container=` consulta el contenedor pedido?

Las sondas anteriores dejaron una duda: el número no aparece en `innerText`, así
que comprobar "shows_target" siempre daba False aunque la tabla fuera la buena.
La prueba válida es que dos contenedores distintos devuelvan tablas distintas y
que el contenedor pedido aparezca en el último movimiento esperado.

Cada caso usa un CONTEXTO NUEVO para descartar estado de sesión.

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

# Dos contenedores del B/L HLCUNG12607EKZE0.
TARGET = "HLBU2959982"      # debe llegar a MANZANILLO
CONTROL = "FTAU1546275"     # otro envío, no debe traer MANZANILLO

STATES = ("Gate out empty", "Gate in empty", "Gate out full", "Gate in full",
          "Arrival in", "Departure from", "Loaded", "Discharged",
          "Vessel departed", "Vessel arrived")


def movement_rows(text: str) -> list[str]:
    rows = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        head = next((s for s in STATES if stripped.startswith(s)), None)
        if head and re.search(r"\d{4}-\d{2}-\d{2}", stripped):
            rows.append(stripped)
    return rows


async def inspect(browser, container: str) -> dict:
    url = f"{BOOKING}?view=S8510&container={container[:4]}++{container[4:]}"
    info = {"container": container, "url": url}
    context = await browser.new_context(locale="es-PE")
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        # La tabla la pinta ExtJS; se sondea hasta que aparezca.
        rows: list[str] = []
        for _ in range(15):
            await page.wait_for_timeout(1000)
            text = await page.evaluate("() => document.body ? document.body.innerText : ''")
            rows = movement_rows(text or "")
            if len(rows) >= 3:
                break
        info["rows"] = len(rows)
        info["first"] = rows[0] if rows else None
        info["last"] = rows[-1] if rows else None
        info["has_manzanillo"] = any("MANZANILLO" in r for r in rows)
        info["has_iquique"] = "IQUIQUE EXPRESS" in (text or "")
        info["url_after"] = page.url
        info["text_len"] = len(text or "")
        with open(f"hapag-url-{container}.txt", "w", encoding="utf-8") as handle:
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
    options: dict = {"headless": True, "locale": "es-PE", "humanize": True}
    if proxy:
        options["proxy"] = {"server": proxy}

    results = []
    async with AsyncCamoufox(**options) as browser:
        for container in (TARGET, CONTROL):
            print(f"── {container}", flush=True)
            info = await inspect(browser, container)
            results.append(info)
            print(f"   rows={info.get('rows')} manzanillo={info.get('has_manzanillo')} "
                  f"iquique={info.get('has_iquique')} err={info.get('error')}", flush=True)
            print(f"   first={info.get('first')!r}", flush=True)
            print(f"   last ={info.get('last')!r}", flush=True)

    with open("hapag-url-probe.json", "w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=2, ensure_ascii=False)

    print("\nRESUMEN")
    for info in results:
        print(f"  {info['container']:<12} rows={info.get('rows')} "
              f"manzanillo={info.get('has_manzanillo')} last={info.get('last')!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
