"""Sonda de Shadow DOM: MSC y Hapag usan web components.

Uso: python scripts/probe_shadow.py msc
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
  const found = {custom: [], shadowInputs: [], deepInputs: []};
  const walk = (root, depth) => {
    if (depth > 12) return;
    let nodes;
    try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
    nodes.forEach(el => {
      if (el.tagName.includes('-')) {
        found.custom.push({tag: el.tagName, id: el.id,
                           hasShadow: !!el.shadowRoot,
                           shadowKids: el.shadowRoot ? el.shadowRoot.querySelectorAll('*').length : 0});
      }
      if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
      if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
        found.deepInputs.push({
          type: el.type, name: el.name, id: el.id,
          placeholder: (el.placeholder || '').slice(0, 50),
          inside: depth > 0 ? 'shadow' : 'light',
          visible: !!(el.offsetWidth || el.offsetHeight)
        });
      }
    });
  };
  walk(document, 0);
  return found;
}"""


async def main(key: str) -> None:
    async with AsyncCamoufox(headless=True, geoip=True, locale="es-PE", os="windows",
                             humanize=True, window=(1440, 900)) as browser:
        page = await browser.new_page()
        page.set_default_timeout(60_000)
        await page.goto(URLS[key], wait_until="domcontentloaded")
        await page.wait_for_timeout(14000)
        for sel in ("#onetrust-accept-btn-handler", 'button[id*="accept" i]'):
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=3000)
                    await page.wait_for_timeout(2000)
                    break
            except Exception:
                continue
        info = await page.evaluate(JS)
        print(f"\n=== {key} · {await page.title()}")
        print("-- custom elements --")
        seen = set()
        for c in info["custom"]:
            sig = (c["tag"], c["hasShadow"])
            if sig in seen:
                continue
            seen.add(sig)
            print("  ", json.dumps(c, ensure_ascii=False))
        print("-- inputs (deep) --")
        for i in info["deepInputs"]:
            print("  ", json.dumps(i, ensure_ascii=False))
        html = await page.content()
        print("-- html len:", len(html))
        for marker in ("track-a-shipment", "trackingNumber", "shadow", "mc-", "hl-", "blno"):
            print(f"   '{marker}':", html.count(marker))
        await page.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
