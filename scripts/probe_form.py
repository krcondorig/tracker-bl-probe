"""Sonda con interacción de formulario: revela el endpoint real de tracking.

Uso: python scripts/probe_form.py msc MEDUADP85690
     python scripts/probe_form.py hl  HLCUNG12607EKZE0
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from camoufox.async_api import AsyncCamoufox  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "probe-dumps"

CONFIG = {
    "msc": {
        "url": "https://www.msc.com/en/track-a-shipment",
        "inputs": ['input[name="trackingNumber"]', 'input[placeholder*="Container" i]',
                   'input[placeholder*="Bill" i]', 'input[type="text"]'],
        "buttons": ['button[type="submit"]', 'button:has-text("Track")',
                    'button:has-text("Search")', 'button:has-text("Rastrear")'],
    },
    "hl": {
        "url": "https://www.hapag-lloyd.com/en/online-business/track/track-by-booking-solution.html",
        "inputs": ['input[name*="bl" i]', 'input[id*="bl" i]',
                   'input[placeholder*="Bill" i]', 'input[type="text"]'],
        "buttons": ['button[type="submit"]', 'button:has-text("Find")',
                    'button:has-text("Search")', 'input[type="submit"]'],
    },
    "maersk": {
        "url": "https://www.maersk.com/tracking/",
        "inputs": ['input[name="trackingNumber"]', 'input[placeholder*="Bill" i]',
                   'input[placeholder*="container" i]', 'input[type="text"]'],
        "buttons": ['button[type="submit"]', 'button:has-text("Track")',
                    'button:has-text("Search")'],
    },
}

IGNORE = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".woff", ".woff2",
          ".ttf", ".otf", ".css", ".map", ".mp4")


async def main(key: str, number: str) -> None:
    cfg = CONFIG[key]
    OUT.mkdir(exist_ok=True)
    async with AsyncCamoufox(headless=True, geoip=True, locale="es-PE", os="windows",
                             humanize=True, window=(1440, 900)) as browser:
        page = await browser.new_page()
        page.set_default_timeout(60_000)
        hits: list[dict] = []
        pending: set[asyncio.Task] = set()
        posts: list[str] = []

        async def on_request(req):
            if req.method in ("POST", "PUT"):
                posts.append(f"{req.method} {req.url}")

        async def on_response(resp):
            try:
                u = resp.url
                if any(u.lower().split("?")[0].endswith(e) for e in IGNORE):
                    return
                ct = resp.headers.get("content-type") or ""
                if "text/html" in ct:
                    return
                body = await resp.text()
                if not body or body.lstrip()[:1] not in ("[", "{"):
                    return
                hits.append({"url": u, "status": resp.status, "ctype": ct,
                             "body": json.loads(body)})
            except Exception:
                pass

        def sched(r):
            t = asyncio.create_task(on_response(r))
            pending.add(t)
            t.add_done_callback(pending.discard)

        page.on("response", sched)
        page.on("request", lambda r: asyncio.create_task(on_request(r)))

        print(f"\n=== [{key}] {cfg['url']}  numero={number}")
        await page.goto(cfg["url"], wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)

        # Cerar banners de cookies que tapan el formulario.
        for sel in ("#onetrust-accept-btn-handler", 'button[id*="accept" i]',
                    'button:has-text("Accept")', 'button:has-text("Aceptar")'):
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=3000)
                    print("  consent cerrado:", sel)
                    await page.wait_for_timeout(1200)
                    break
            except Exception:
                continue

        filled = None
        for sel in cfg["inputs"]:
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=4000)
                    await loc.fill("")
                    await loc.type(number, delay=80)
                    filled = sel
                    print("  input usado:", sel)
                    break
            except Exception:
                continue
        if not filled:
            print("  FALLO: no se encontró input")
            body = await page.evaluate("() => document.body.innerText.slice(0,600)")
            print("  texto:", " ".join(body.split())[:300])
            await page.close()
            return

        await page.wait_for_timeout(500)
        clicked = None
        for sel in cfg["buttons"]:
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=4000)
                    clicked = sel
                    print("  botón usado:", sel)
                    break
            except Exception:
                continue
        if not clicked:
            await page.keyboard.press("Enter")
            print("  submit con Enter")

        await page.wait_for_timeout(22000)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        print("  POST/PUT vistos:", len(posts))
        for p in posts[:12]:
            print("    >", p[:130])
        print("  JSON capturados:", len(hits))
        for h in hits:
            size = len(json.dumps(h["body"]))
            interesting = any(k in h["url"].lower() for k in
                              ("track", "shipment", "container", "bl", "api"))
            mark = "**" if interesting else "  "
            print(f"  {mark} {h['status']} {size:>7}B {h['url'].split('?')[0][:105]}")
            if interesting:
                dump = OUT / f"form-{key}-{abs(hash(h['url'])) % 10**6}.json"
                dump.write_text(json.dumps(h["body"], ensure_ascii=False, indent=2), encoding="utf-8")

        text = await page.evaluate("() => document.body.innerText.slice(0,2000)")
        print("  texto visible:", " ".join(text.split())[:500])
        await page.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
