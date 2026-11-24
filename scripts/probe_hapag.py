"""Sonda de Hapag-Lloyd: reproduce el flujo real del detalle por contenedor.

Cómo se llegó aquí (no repetir):

1. Sin WARP, `hapag-lloyd.com` devuelve Managed Challenge: es bloqueo por IP.
2. Con WARP la web carga, pero `?blno=` y el formulario no disparan el JSON de
   tracking: la SPA usa rutas hash y llama a una API aparte.
3. Esa API (`tracking.api.hlag.cloud/api/tracking/v3/events`) falla con
   "NetworkError" desde `fetch`: CORS.
4. La vía que sí usa la web es **HTML renderizado en servidor**:

       track-by-booking-solution.html?view=S8510&container=<CONTENEDOR>

   `view=S8510` es una constante; `container` sale del listado que aparece al
   consultar el B/L. Ese es el camino de este probe.

Flujo:
   A. Consultar el B/L y extraer el listado de contenedores (con sus enlaces
      "Detalles", que ya traen el contenedor exacto).
   B. Navegar a la URL de detalle de cada contenedor y volcar el HTML.
   C. Resumir lo que se ve: buque, viaje, puerto, fechas, llegada.

Uso:
    python scripts/probe_hapag.py HLCUNG12607EKZE0 --proxy socks5://127.0.0.1:40000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from camoufox.async_api import AsyncCamoufox  # noqa: E402

BOOKING_URL = ("https://www.hapag-lloyd.com/en/online-business/track/"
               "track-by-booking-solution.html")
VIEW = "S8510"  # constante del enlace "Detalles"

CONTAINER_RE = re.compile(r"\b([A-Z]{4})[\s+]{0,2}(\d{6})[\s+]{0,2}(\d)\b")


def _normalize_container(raw: str) -> str:
    """Quita separadores de la forma cruda.

    El sitio entrega el contenedor como `HLBU++2959982`: el `+` es un espacio
    codificado, no parte del número.
    """
    return re.sub(r"[^A-Z0-9]", "", raw.upper())


async def _dismiss(page) -> None:
    for sel in ("#onetrust-accept-btn-handler",
                'button[id*="accept" i]',
                'button:has-text("Accept all")',
                'button:has-text("Aceptar todo")',
                'button:has-text("Aceptar")'):
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=2500)
                await page.wait_for_timeout(700)
                break
        except Exception:
            continue
    try:
        await page.evaluate(
            """() => { for (const id of ['onetrust-consent-sdk',
                                        'onetrust-banner-sdk',
                                        'onetrust-pc-sdk']) {
                           const el = document.getElementById(id);
                           if (el) el.remove();
                       } }"""
        )
    except Exception:
        pass


async def _blocked(page) -> str:
    try:
        title = await page.title() or ""
    except Exception:
        title = ""
    if re.search(r"(just a moment|un momento|security check|access denied"
                 r"|attention required)", title, re.I):
        return title.strip()[:90]
    return ""


async def _submit_booking(page, bl: str) -> None:
    """Escribe el B/L y envía. El botón 'Next' es el camino real."""
    handle = await page.evaluate_handle(
        """() => {
            const cands = Array.from(document.querySelectorAll('input'))
                .filter(el => el.offsetParent !== null &&
                              !['hidden','checkbox','radio','submit','button']
                                .includes(el.type));
            return cands[0] || null;
        }"""
    )
    el = handle.as_element()
    if el is None:
        print("  AVISO: no hay campo de texto visible")
        return
    ident = await page.evaluate("(e) => e.id || e.placeholder || '?'", el)
    print(f"  campo: {ident!r}")
    try:
        await el.click(timeout=8000)
        await el.fill("")
        await el.type(bl, delay=70)
    except Exception:
        await el.evaluate(
            """(e, v) => { e.value = v;
                 e.dispatchEvent(new Event('input', {bubbles:true}));
                 e.dispatchEvent(new Event('change', {bubbles:true})); }""",
            bl)
    print(f"  B/L escrito: {bl}")
    clicked = False
    for sel in ('button:has-text("Next")', 'button:has-text("Siguiente")',
                'button[type="submit"]'):
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=6000)
                print(f"  click en {sel}")
                clicked = True
                break
        except Exception:
            continue
    if not clicked:
        await el.press("Enter")
        print("  Enter (no había botón visible)")


async def _extract_containers(page) -> list[dict[str, str]]:
    """Saca los contenedores del listado, con su enlace de detalle real.

    Se prefieren los enlaces porque ya traen el contenedor exactamente como lo
    espera el servidor (incluida la codificación de espacios).
    """
    try:
        raw = await page.evaluate(
            """() => Array.from(document.querySelectorAll('a[href]'))
                    .map(a => ({href: a.href, text: (a.innerText || '').trim()}))
                    .filter(a => /[?&]container=/.test(a.href))"""
        )
    except Exception:
        raw = []

    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw or []:
        href = item.get("href") or ""
        m = re.search(r"[?&]container=([^&]+)", href)
        if not m:
            continue
        cont = m.group(1)
        if cont in seen:
            continue
        seen.add(cont)
        out.append({"container_param": cont, "href": href,
                    "label": (item.get("text") or "")[:40]})

    if out:
        return out

    # Sin enlaces: buscar números de contenedor en el texto de la página.
    try:
        text = await page.evaluate(
            "() => document.body ? document.body.innerText : ''")
    except Exception:
        text = ""
    for m in CONTAINER_RE.finditer(text or ""):
        cont = _normalize_container(m.group(0))
        if cont in seen:
            continue
        seen.add(cont)
        out.append({"container_param": cont, "href": "", "label": "(del texto)"})
    return out


async def main() -> int:
    ap = argparse.ArgumentParser(description="Flujo de detalle por contenedor de Hapag.")
    ap.add_argument("bl", help="B/L o booking de Hapag")
    ap.add_argument("--proxy", default="", help="Proxy SOCKS5, p. ej. socks5://127.0.0.1:40000")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--dump", default="probe-dumps/hapag", help="Carpeta de salida")
    ap.add_argument("--max-containers", type=int, default=2,
                    help="Cuántos contenedores volcar con detalle.")
    args = ap.parse_args()

    bl = re.sub(r"[^A-Z0-9]", "", args.bl.upper())
    dump = Path(args.dump)
    dump.mkdir(parents=True, exist_ok=True)

    launch: dict[str, Any] = dict(headless=not args.headed, geoip=True,
                                  locale="es-PE", os="windows", humanize=True,
                                  window=(1440, 1000))
    if args.proxy:
        launch["proxy"] = {"server": args.proxy}

    containers: list[dict[str, str]] = []
    detail_ok = False

    try:
        async with AsyncCamoufox(**launch) as browser:
            page = await browser.new_page()
            page.set_default_timeout(90_000)
            page.on("dialog", lambda d: asyncio.create_task(d.accept()))

            # ---------------------------------------------------------------
            # A. Listado del B/L
            # ---------------------------------------------------------------
            print("=" * 78)
            print(f"PASO A: consultar B/L {bl}")
            print("=" * 78)
            resp = await page.goto(BOOKING_URL, wait_until="domcontentloaded")
            print(f"  HTTP {resp.status if resp else '?'}")
            await page.wait_for_timeout(3500)
            await _dismiss(page)
            bloqueo = await _blocked(page)
            if bloqueo:
                print(f"  BLOQUEADO: {bloqueo}")
                print("  revisa --proxy / WARP")
                await page.close()
                return 1

            await _submit_booking(page, bl)
            await page.wait_for_timeout(6000)

            # La SPA puede tardar; se espera a que aparezca algún contenedor.
            containers = []
            for i in range(20):
                containers = await _extract_containers(page)
                if containers:
                    print(f"  listado listo tras {(i + 1) * 2}s")
                    break
                await page.wait_for_timeout(2000)

            print(f"  URL tras consultar: {page.url}")
            print(f"  contenedores encontrados: {len(containers)}")
            for c in containers[:15]:
                print(f"     {c['container_param']:<20} label={c['label']!r}")
                if c["href"]:
                    print(f"        {c['href'][:130]}")

            (dump / f"listado-{bl}.json").write_text(
                json.dumps(containers, ensure_ascii=False, indent=1),
                encoding="utf-8")
            try:
                (dump / f"listado-{bl}.txt").write_text(
                    await page.evaluate(
                        "() => document.body ? document.body.innerText : ''"),
                    encoding="utf-8")
            except Exception:
                pass

            # ---------------------------------------------------------------
            # B. Detalle por contenedor
            # ---------------------------------------------------------------
            print("\n" + "=" * 78)
            print("PASO B: detalle por contenedor (view=S8510)")
            print("=" * 78)

            if not containers:
                print("  sin contenedores: se prueba la URL de detalle con el B/L")
                targets = [{"container_param": bl, "href": "", "label": "(B/L)"}]
            else:
                targets = containers[:args.max_containers]

            for idx, c in enumerate(targets):
                param = c["container_param"]
                url = (c["href"] or
                       f"{BOOKING_URL}?view={VIEW}&container={quote(param)}")
                print(f"\n  [{idx + 1}/{len(targets)}] {param}")
                print(f"     {url[:140]}")
                try:
                    r = await page.goto(url, wait_until="domcontentloaded",
                                        timeout=90_000)
                    print(f"     HTTP {r.status if r else '?'}")
                    await page.wait_for_timeout(5000)
                    await _dismiss(page)
                    b = await _blocked(page)
                    if b:
                        print(f"     BLOQUEADO: {b}")
                        continue
                    await page.wait_for_timeout(3000)

                    html = await page.content()
                    text = await page.evaluate(
                        "() => document.body ? document.body.innerText : ''")
                    safe = re.sub(r"[^A-Za-z0-9]+", "-", param)[:28]
                    (dump / f"detalle-{safe}.html").write_text(html, encoding="utf-8")
                    (dump / f"detalle-{safe}.txt").write_text(text, encoding="utf-8")
                    print(f"     HTML {len(html) // 1024}KB, texto {len(text)}B "
                          f"-> detalle-{safe}.txt")

                    # Resumen legible de lo que hay en la página.
                    flat = " ".join(text.split())
                    print(f"     texto: {flat[:280]}")
                    for kw in ("Vessel", "Buque", "Voyage", "Viaje", "ETA",
                               "Arrival", "Llegada", "Discharge", "Descarga",
                               "MANZANILLO", "Manzanillo"):
                        if kw.lower() in flat.lower():
                            print(f"     contiene {kw!r}")
                    if "HLBU" in flat or param[:4] in flat:
                        detail_ok = True
                except Exception as exc:
                    print(f"     ERROR: {type(exc).__name__}: {str(exc)[:150]}")

            await page.close()
    except Exception as exc:
        if args.proxy:
            print(f"\n  No se pudo abrir el navegador con proxy {args.proxy}: "
                  f"{type(exc).__name__}\n  ¿WARP conectado en modo proxy?")
            return 1
        raise

    print("\n" + "=" * 78)
    print(f"RESULTADO: {len(containers)} contenedores, "
          f"detalle {'OK' if detail_ok else 'sin datos'}")
    print("=" * 78)
    return 0 if containers else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
