"""Sonda 5: estructura del formulario de contenedor de Hapag.

La sonda anterior encontró el campo pero el clic expiró (banner encima) y en la
otra página no lo encontró. Aquí solo se REPORTA la estructura, sin clics:
campos, botones, si están tapados y qué elemento hay en el centro del campo.

Temporal: se borra al decidir.
"""

from __future__ import annotations

import asyncio
import json
import sys

from camoufox.async_api import AsyncCamoufox

TRACK = "https://www.hapag-lloyd.com/en/online-business/track/"
PAGES = {
    "container_page": TRACK + "track-by-container-solution-legacy.html",
    "booking_page": TRACK + "track-by-booking-solution-legacy.html",
}


async def report(page) -> dict:
    return await page.evaluate(
        """() => {
            const out = {inputs: [], buttons: [], forms: [], cookie: []};

            document.querySelectorAll('input').forEach(el => {
                const r = el.getBoundingClientRect();
                let covered = null;
                if (r.width && r.height) {
                    const top = document.elementFromPoint(
                        r.left + r.width / 2, r.top + r.height / 2);
                    if (top && top !== el) {
                        covered = (top.id ? '#' + top.id : top.tagName) +
                                  (top.className ? '.' + String(top.className).split(' ')[0] : '');
                    }
                }
                out.inputs.push({
                    type: el.type, name: el.name, id: el.id,
                    placeholder: el.placeholder || '',
                    visible: !!(el.offsetParent),
                    rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
                    covered_by: covered,
                });
            });

            document.querySelectorAll('button, input[type=submit], a[role=button]').forEach(el => {
                out.buttons.push({
                    tag: el.tagName, type: el.type || '', id: el.id, name: el.name,
                    value: el.value || '', text: (el.innerText || '').trim().slice(0, 40),
                    visible: !!(el.offsetParent),
                });
            });

            document.querySelectorAll('form').forEach(el => out.forms.push({
                id: el.id, name: el.name, action: (el.action || '').slice(0, 120),
                method: el.method,
            }));

            ['#onetrust-accept-btn-handler', '#onetrust-consent-sdk', '.onetrust-pc-dark-filter']
                .forEach(sel => {
                    const el = document.querySelector(sel);
                    if (el) out.cookie.push({sel, visible: !!(el.offsetParent)});
                });

            return out;
        }"""
    )


async def main() -> int:
    proxy = sys.argv[1] if len(sys.argv) > 1 else None
    options: dict = {"headless": True, "locale": "es-PE", "humanize": True}
    if proxy:
        options["proxy"] = {"server": proxy}

    results = {}
    async with AsyncCamoufox(**options) as browser:
        for name, url in PAGES.items():
            print(f"── {name}", flush=True)
            context = await browser.new_context(locale="es-PE")
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                await page.wait_for_timeout(9000)
                results[name] = await report(page)
                info = results[name]
                print(f"   inputs visibles: {[i for i in info['inputs'] if i['visible']]}", flush=True)
                print(f"   botones visibles: "
                      f"{[b['text'] or b['value'] for b in info['buttons'] if b['visible']]}", flush=True)
                print(f"   forms: {info['forms']}", flush=True)
                print(f"   cookie: {info['cookie']}", flush=True)
            except Exception as exc:  # noqa: BLE001
                results[name] = {"error": f"{type(exc).__name__}: {exc}"}
                print(f"   ERROR: {exc}", flush=True)
            finally:
                try:
                    await page.close()
                    await context.close()
                except Exception:  # noqa: BLE001
                    pass

    with open("hapag-structure.json", "w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=2, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
