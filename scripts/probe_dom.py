"""Inspección de DOM: lista inputs/botones reales de una página.

Uso: python scripts/probe_dom.py msc
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from camoufox.async_api import AsyncCamoufox  # noqa: E402

URLS = {
    "msc": "https://www.msc.com/en/track-a-shipment",
    "hl": "https://www.hapag-lloyd.com/en/online-business/track/track-by-booking-solution.html",
    "maersk": "https://www.maersk.com/tracking/",
}

JS = """() => {
  const out = {inputs: [], buttons: [], forms: [], iframes: []};
  document.querySelectorAll('input, textarea').forEach(el => {
    out.inputs.push({
      tag: el.tagName, type: el.type, name: el.name, id: el.id,
      placeholder: el.placeholder, aria: el.getAttribute('aria-label'),
      visible: !!(el.offsetWidth || el.offsetHeight)
    });
  });
  document.querySelectorAll('button, input[type=submit], a[role=button]').forEach(el => {
    const t = (el.innerText || el.value || '').trim().slice(0, 40);
    if (!t) return;
    out.buttons.push({tag: el.tagName, type: el.type, id: el.id, text: t,
                      visible: !!(el.offsetWidth || el.offsetHeight)});
  });
  document.querySelectorAll('form').forEach(f => out.forms.push({
    action: f.action, method: f.method,
    inputs: Array.from(f.querySelectorAll('input')).map(i => i.name || i.id)
  }));
  document.querySelectorAll('iframe').forEach(f => {
    out.iframes.push({src: (f.src || '').slice(0, 120), id: f.id});
  });
  return out;
}"""


async def main(key: str) -> None:
    async with AsyncCamoufox(headless=True, geoip=True, locale="es-PE", os="windows",
                             humanize=True, window=(1440, 900)) as browser:
        page = await browser.new_page()
        page.set_default_timeout(60_000)
        await page.goto(URLS[key], wait_until="domcontentloaded")
        await page.wait_for_timeout(9000)
        for sel in ("#onetrust-accept-btn-handler", 'button[id*="accept" i]',
                    'button:has-text("Accept All")', 'button:has-text("Aceptar todo")'):
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=3000)
                    print("consent:", sel)
                    await page.wait_for_timeout(1500)
                    break
            except Exception:
                continue
        info = await page.evaluate(JS)
        print(f"\n=== {key} · {await page.title()}")
        print("-- INPUTS visibles --")
        for i in info["inputs"]:
            if i["visible"]:
                print("  ", json.dumps(i, ensure_ascii=False))
        print("-- BUTTONS visibles --")
        for b in info["buttons"][:25]:
            if b["visible"]:
                print("  ", json.dumps(b, ensure_ascii=False))
        print("-- FORMS --")
        for f in info["forms"]:
            print("  ", json.dumps(f, ensure_ascii=False)[:200])
        print("-- IFRAMES --")
        for f in info["iframes"]:
            print("  ", json.dumps(f, ensure_ascii=False)[:160])
        await page.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
