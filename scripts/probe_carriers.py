"""Sonda exploratoria: descubre los payloads reales de las 3 navieras.

No forma parte del producto. Sirve para obtener evidencia de qué endpoints
JSON usa cada naviera y con qué forma, para ajustar `tracker/carriers.py` y
`tracker/extract.py`. Deja el JSON crudo en `probe-dumps/`.

Uso:  python scripts/probe_carriers.py msc
      python scripts/probe_carriers.py maersk hl
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from camoufox.async_api import AsyncCamoufox  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "probe-dumps"

TARGETS = {
    "msc": [
        ("container", "https://www.msc.com/en/track-a-shipment?trackingNumber=MEDUADP85690"),
        ("bl", "https://www.msc.com/en/track-a-shipment?trackingMode=1&trackingNumber=MEDUADP85690"),
    ],
    "maersk": [
        ("bl", "https://www.maersk.com/tracking/275792562"),
    ],
    "hl": [
        ("bl", "https://www.hapag-lloyd.com/en/online-business/track/track-by-booking-solution.html?blno=HLCUNG12607EKZE0"),
    ],
}

IGNORE = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".woff", ".woff2",
          ".ttf", ".otf", ".css", ".map", ".mp4")


async def probe(key: str) -> None:
    OUT.mkdir(exist_ok=True)
    async with AsyncCamoufox(
        headless=True, geoip=True, locale="es-PE", os="windows",
        humanize=True, window=(1440, 900),
    ) as browser:
        for label, url in TARGETS[key]:
            page = await browser.new_page()
            page.set_default_timeout(60_000)
            hits: list[dict] = []
            pending: set[asyncio.Task] = set()

            async def on_response(resp, _page=page):
                try:
                    u = resp.url
                    if any(u.lower().split("?")[0].endswith(e) for e in IGNORE):
                        return
                    if "text/html" in (resp.headers.get("content-type") or ""):
                        return
                    body = await resp.text()
                    if not body or body.lstrip()[:1] not in ("[", "{"):
                        return
                    try:
                        parsed = json.loads(body)
                    except json.JSONDecodeError:
                        return
                    hits.append({"url": u, "status": resp.status,
                                 "ctype": resp.headers.get("content-type"),
                                 "body": parsed})
                except Exception:
                    pass

            def schedule(resp):
                t = asyncio.create_task(on_response(resp))
                pending.add(t)
                t.add_done_callback(pending.discard)

            page.on("response", schedule)
            print(f"\n=== [{key}/{label}] {url}")
            try:
                await page.goto(url, wait_until="domcontentloaded")
            except Exception as e:
                print("  navegación:", type(e).__name__, str(e)[:100])
            await page.wait_for_timeout(22000)
            title = await page.title()
            snippet = ""
            try:
                snippet = (await page.evaluate(
                    "() => document.body ? document.body.innerText.slice(0,1200) : ''")) or ""
            except Exception:
                pass
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

            print("  title:", title[:90])
            print("  json capturados:", len(hits))
            for h in hits:
                body = h["body"]
                size = len(json.dumps(body))
                print(f"    - {h['status']} {size:>7}B {h['url'].split('?')[0][:100]}")
                dump = OUT / f"{key}-{label}-{abs(hash(h['url'])) % 10**6}.json"
                dump.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
            print("  texto visible:", " ".join(snippet.split())[:280])
            await page.close()
            await asyncio.sleep(3)


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "msc"
    asyncio.run(probe(target))
