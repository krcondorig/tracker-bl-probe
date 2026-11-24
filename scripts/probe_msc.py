"""Sonda dirigida: rellena #trackingNumber en MSC y captura el XHR de tracking.

Uso: python scripts/probe_msc.py MEDUADP85690
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from camoufox.async_api import AsyncCamoufox  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "probe-dumps"


async def main(number: str) -> None:
    OUT.mkdir(exist_ok=True)
    async with AsyncCamoufox(headless=True, geoip=True, locale="es-PE", os="windows",
                             humanize=True, window=(1440, 900)) as browser:
        page = await browser.new_page()
        page.set_default_timeout(60_000)

        hits: list[dict] = []
        posts: list[str] = []
        pending: set[asyncio.Task] = set()

        async def on_response(resp):
            try:
                u = resp.url
                ct = resp.headers.get("content-type") or ""
                if "text/html" in ct:
                    return
                if any(u.lower().split("?")[0].endswith(e) for e in
                       (".png", ".jpg", ".svg", ".css", ".woff", ".woff2", ".map", ".ico")):
                    return
                txt = await resp.text()
                if not txt or txt.lstrip()[:1] not in ("[", "{"):
                    return
                hits.append({"url": u, "status": resp.status, "ct": ct,
                             "body": json.loads(txt)})
            except Exception:
                pass

        def sched(r):
            t = asyncio.create_task(on_response(r))
            pending.add(t)
            t.add_done_callback(pending.discard)

        page.on("response", sched)
        page.on("request", lambda r: (posts.append(f"{r.method} {r.url}")
                                      if r.method in ("POST", "PUT") else None))

        await page.goto("https://www.msc.com/en/track-a-shipment", wait_until="domcontentloaded")
        await page.wait_for_timeout(6000)
        for sel in ("#onetrust-accept-btn-handler", 'button[id*="accept" i]'):
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=3000)
                    await page.wait_for_timeout(1500)
                    break
            except Exception:
                continue

        field = page.locator("#trackingNumber").first
        await field.wait_for(state="visible", timeout=20000)
        await field.click()
        await field.type(number, delay=90)
        val = await field.input_value()
        print("valor en el campo:", val)

        await page.wait_for_timeout(600)
        # Enter dentro del campo es lo que dispara el submit del componente.
        await field.press("Enter")
        print("Enter enviado")
        await page.wait_for_timeout(20000)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        print("POST/PUT:", len(posts))
        for p in posts[:8]:
            print("  >", p[:140])
        print("JSON:", len(hits))
        for h in hits:
            size = len(json.dumps(h["body"]))
            key = any(k in h["url"].lower() for k in ("track", "shipment", "container", "bl"))
            print(f"  {'**' if key else '  '} {h['status']} {size:>7}B {h['url'].split('?')[0][:110]}")
            if key:
                (OUT / f"msc-{abs(hash(h['url'])) % 10**6}.json").write_text(
                    json.dumps(h["body"], ensure_ascii=False, indent=2), encoding="utf-8")

        text = await page.evaluate("() => document.body.innerText.slice(0,2500)")
        print("texto:", " ".join(text.split())[:700])
        await page.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "MEDUADP85690"))
