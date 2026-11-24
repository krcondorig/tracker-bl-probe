"""Sonda 4 (funcional): la vía por contenedor es el formulario, no la URL.

Hallazgo previo: `?view=S8510&container=X` devuelve una tabla de movimientos
pero de OTRO envío (el número pedido no aparece en la página). Es estado rancio
del servidor, no una consulta. Así que la URL no sirve.

La página de contenedor trae su propio formulario ("Tracing by Container",
campo `Container No.` + botón `Find`). Aquí se rellena de verdad y se comprueba
si el detalle que aparece es el del contenedor pedido.

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
OTHER = "FTAU1546275"

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


async def form_fields(page) -> list[dict]:
    """Campos y botones del formulario, para no adivinar selectores."""
    return await page.evaluate(
        """() => {
            const out = [];
            document.querySelectorAll('input').forEach(el => out.push({
                tag: 'input', type: el.type, name: el.name, id: el.id,
                placeholder: el.placeholder || '', visible: !!(el.offsetParent),
            }));
            document.querySelectorAll('button, input[type=submit]').forEach(el => out.push({
                tag: 'button', type: el.type || '', name: el.name, id: el.id,
                value: el.value || '', text: (el.innerText || '').trim().slice(0, 30),
                visible: !!(el.offsetParent),
            }));
            return out;
        }"""
    )


async def try_container_form(browser, page_url: str) -> dict:
    info = {"page": page_url}
    context = await browser.new_context(locale="es-PE")
    page = await context.new_page()
    try:
        await page.goto(page_url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(6000)

        fields = await form_fields(page)
        info["fields"] = [f for f in fields if f.get("visible")][:20]

        # Localizar el campo del contenedor y el botón Find por sus atributos,
        # sin depender de un selector concreto que pueda cambiar.
        locator = None
        for candidate in ("#containerNo", "input[name*=container i]", "input[id*=container i]",
                          "input[name*=Container i]"):
            try:
                loc = page.locator(candidate).first
                if await loc.count() and await loc.is_visible():
                    locator = loc
                    info["field_selector"] = candidate
                    break
            except Exception:  # noqa: BLE001
                continue
        if locator is None:
            info["error"] = "sin campo de contenedor visible"
            return info

        await locator.click(timeout=8000)
        await locator.fill("")
        await locator.type(TARGET, delay=90)
        await page.wait_for_timeout(500)

        clicked = False
        for candidate in ("button:has-text('Find')", "input[value='Find']",
                          "button[value='Find']", "#findBtn"):
            try:
                btn = page.locator(candidate).first
                if await btn.count() and await btn.is_visible():
                    await btn.click(timeout=8000)
                    info["find_selector"] = candidate
                    clicked = True
                    break
            except Exception:  # noqa: BLE001
                continue
        if not clicked:
            await locator.press("Enter")
            info["find_selector"] = "Enter"

        await page.wait_for_timeout(12_000)
        text = await page.evaluate("() => document.body ? document.body.innerText : ''")
        info["url_after"] = page.url
        info["shows_target"] = TARGET in (text or "")
        info["shows_other"] = OTHER in (text or "")
        info["rows"] = len(movement_rows(text or ""))
        info["sample_rows"] = movement_rows(text or "")[:4]
        info["text_len"] = len(text or "")
        if TARGET in (text or ""):
            pos = text.index(TARGET)
            info["context"] = text[max(0, pos - 150):pos + 100]
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
        for name, url in (("container_page", CONTAINER_PAGE), ("booking_page", BOOKING_PAGE)):
            print(f"── {name}: {url}", flush=True)
            info = await try_container_form(browser, url)
            info["case"] = name
            results.append(info)
            print(f"   target={info.get('shows_target')} other={info.get('shows_other')} "
                  f"rows={info.get('rows')} err={info.get('error')}", flush=True)
            print(f"   find={info.get('find_selector')} field={info.get('field_selector')} "
                  f"url_after={info.get('url_after')}", flush=True)
            for row in info.get("sample_rows", []):
                print(f"   fila: {row}", flush=True)
            if info.get("context"):
                print(f"   ctx: {info['context']!r}", flush=True)

    with open("hapag-form-probe.json", "w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=2, ensure_ascii=False)

    print("\nRESUMEN")
    for info in results:
        print(f"  {info['case']:<16} target={info.get('shows_target')} "
              f"rows={info.get('rows')} err={info.get('error')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
