"""Sonda de Hapag-Lloyd: reproduce el flujo real (listado -> detalle).

Camino recorrido hasta aquí (para no repetir el trabajo):

1. Sin WARP, `hapag-lloyd.com` devuelve Managed Challenge: bloqueo por IP,
   no por fingerprint. Con WARP la web carga.
2. La SPA nueva (`/solutions/tracking/#/`) NO sirve para esto: su API JSON
   (`tracking.api.hlag.cloud/api/tracking/v3/events`) falla con NetworkError
   por CORS desde el navegador, y las cuatro variantes de versión dan lo mismo.
3. El flujo que sí existe es el de toda la vida, HTML renderizado en servidor:

       listing : track-by-booking-solution[-legacy].html?blno=<B/L>
       detalle : track-by-booking-solution.html?view=S8510&container=<CONT>

   `view=S8510` es constante; el contenedor sale del listado. La forma del
   parámetro en el sitio es `HLBU++2959982` (el `+` es espacio codificado),
   así que aquí se prueba la forma normalizada y la cruda.

Este script prueba las dos vías de listado, extrae los contenedores del HTML
y visita el detalle. Guarda todo crudo para no tener que repetir la corrida.

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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from camoufox.async_api import AsyncCamoufox  # noqa: E402

TRACK = "https://www.hapag-lloyd.com/en/online-business/track/"
BOOKING = TRACK + "track-by-booking-solution.html"
LEGACY = TRACK + "track-by-booking-solution-legacy.html"
SPA = "https://www.hapag-lloyd.com/solutions/tracking/"
VIEW = "S8510"  # constante del enlace "Detalles"

CONTAINER_RE = re.compile(r"\b([A-Z]{4})[\s+]{0,2}(\d{6})[\s+]{0,2}(\d)\b")
BLOCK_RE = re.compile(r"(just a moment|un momento|security check|access denied"
                      r"|attention required|your browser was tested)", re.I)


def _norm_container(raw: str) -> str:
    """Quita separadores: `HLBU++2959982` -> `HLBU2959982`.

    El parámetro llega a veces URL-encoded (`HLBU%2B%2B2959982`), así que hay
    que decodificar antes: si no, el `B` de `%2B` se cuela en el número.
    """
    from urllib.parse import unquote

    text = unquote(raw.replace("+", " ")) if "%" in raw else raw
    return re.sub(r"[^A-Z0-9]", "", text.upper())


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
    if BLOCK_RE.search(title):
        return title.strip()[:90]
    try:
        text = await page.evaluate(
            "() => document.body ? document.body.innerText.slice(0,600) : ''")
    except Exception:
        text = ""
    if BLOCK_RE.search(text or ""):
        return " ".join(text.split())[:120]
    return ""


async def _html(page) -> tuple[str, str]:
    try:
        html = await page.content()
    except Exception:
        html = ""
    try:
        text = await page.evaluate(
            "() => document.body ? document.body.innerText : ''")
    except Exception:
        text = ""
    return html, text


async def _containers_from_page(page, html: str, text: str) -> list[dict[str, str]]:
    """Extrae los contenedores del listado, con su índice de fila.

    El `row` es lo que luego permite marcar el radio correcto y pulsar
    Details: sin él solo se tendría el número, no la forma de pedir su detalle.
    """
    found: list[dict[str, str]] = []
    seen: set[str] = set()

    # 1) filas del formulario JSF (la fuente buena: trae radio + datos)
    rows = await _rows_from_page(page)
    for idx, row in enumerate(rows):
        cells = row.get("cells") or []
        cont = ""
        for c in cells:
            m = CONTAINER_RE.search(c)
            if m:
                cont = _norm_container(m.group(0))
                break
        if not cont or cont in seen:
            continue
        seen.add(cont)
        raw = row.get("rawContainer") or ""
        found.append({
            "container": cont,
            "param": _container_param(raw) if raw else "",
            "raw": raw,
            "row": str(idx),
            "radio": row.get("radioId") or "",
            "cells": " | ".join(cells)[:120],
            "source": "row",
        })
    if found:
        return found

    # 2) enlaces con ?container= (versiones antiguas del listado)
    for m in re.finditer(
            r"""href=["']([^"']*[?&]container=[^"']+)["']""", html or "", re.I):
        href = m.group(1)
        cm = re.search(r"[?&]container=([^&\"']+)", href)
        if not cm:
            continue
        cont = _norm_container(cm.group(1))
        if not cont or cont in seen:
            continue
        seen.add(cont)
        found.append({"container": cont, "param": cm.group(1), "raw": cm.group(1),
                      "row": "", "radio": "", "href": href, "source": "href"})

    # 3) texto visible
    if not found:
        for m in CONTAINER_RE.finditer(text or ""):
            cont = _norm_container(m.group(0))
            if not cont or cont in seen:
                continue
            seen.add(cont)
            found.append({"container": cont, "param": _container_param(m.group(0)),
                          "raw": m.group(0), "row": "", "radio": "",
                          "source": "text"})
    return found


async def _fill_and_submit(page, bl: str) -> bool:
    """Escribe el B/L en el primer campo visible y envía."""
    try:
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
            print("     sin campo de texto visible")
            return False
        ident = await page.evaluate("(e) => e.id || e.placeholder || '?'", el)
        print(f"     campo: {ident!r}")
        try:
            await el.click(timeout=8000)
            await el.fill("")
            await el.type(bl, delay=60)
        except Exception:
            await el.evaluate(
                """(e, v) => { e.value = v;
                     e.dispatchEvent(new Event('input', {bubbles:true}));
                     e.dispatchEvent(new Event('change', {bubbles:true})); }""",
                bl)
        for sel in ('button:has-text("Next")', 'button:has-text("Siguiente")',
                    'button[type="submit"]', 'input[type="submit"]'):
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=6000)
                    print(f"     click {sel}")
                    return True
            except Exception:
                continue
        await el.press("Enter")
        print("     Enter")
        return True
    except Exception as exc:
        print(f"     formulario FALLÓ: {type(exc).__name__}: {str(exc)[:120]}")
        return False


async def _rows_from_page(page) -> list[dict[str, Any]]:
    """Lee las filas del listado JSF: radio, tipo, contenedor y última actividad.

    El botón "Details" no es un enlace: es un submit de formulario JSF que
    envía el radio seleccionado y el servidor responde con la URL
    `?view=S8510&container=<CONT>`. Por eso construir esa URL a mano devuelve
    la shell vacía: falta el estado del formulario.

    El contenedor se muestra rellenado con espacios (`HLBU  2959982`, dos
    espacios). Al enviar, esos espacios se codifican como `++`, y esa es la
    forma que espera el servidor: `...&container=HLBU++2959982`. Por eso se
    guarda el texto crudo de la celda, no solo el número normalizado.
    """
    try:
        rows = await page.evaluate(
            """() => {
                const out = [];
                const radios = Array.from(document.querySelectorAll(
                    'input[type=radio][name]'));
                for (const r of radios) {
                    const tr = r.closest('tr');
                    if (!tr) continue;
                    const tds = Array.from(tr.querySelectorAll('td'));
                    const cells = tds.map(td => (td.innerText || '').trim());
                    // Texto crudo de la celda que trae el contenedor.
                    // Se usa `textContent` y no `innerText`: `innerText`
                    // refleja el render y colapsa los espacios múltiples, y
                    // aquí los DOS espacios son significativos porque el
                    // submit los convierte en `++`, parte del parámetro.
                    let rawContainer = '';
                    for (const td of tds) {
                        const t = td.textContent || '';
                        if (/[A-Z]{4}\s*\d{6}\s*\d/.test(t)) {
                            rawContainer = t.replace(/[\r\n\t]/g, ' ')
                                             .replace(/^\s+|\s+$/g, '');
                            break;
                        }
                    }
                    out.push({radioId: r.id, radioName: r.name,
                              radioValue: r.value, cells: cells,
                              rawContainer: rawContainer});
                }
                return out;
            }"""
        )
        return rows or []
    except Exception:
        return []


def _container_param(raw: str) -> str:
    """Convierte el texto de la celda a la forma del parámetro del sitio.

    `HLBU  2959982` -> `HLBU++2959982`

    Cada espacio se vuelve `+`, que es exactamente lo que produce el submit del
    formulario. No se usa `quote()` porque este escaparía el `+` a `%2B`.
    """
    return raw.replace(" ", "+")


async def _container_in_row(cells: list[str]) -> str:
    for c in cells:
        m = CONTAINER_RE.search(c)
        if m:
            return _norm_container(m.group(0))
    return ""


async def _click_details(page, row_index: int) -> bool:
    """Selecciona el radio de la fila y pulsa Details (submit JSF real)."""
    try:
        info = await page.evaluate(
            """(idx) => {
                const radios = Array.from(document.querySelectorAll(
                    'input[type=radio][name]'));
                const r = radios[idx];
                if (!r) return null;
                r.click();
                const label = document.querySelector(`label[for="${r.id}"]`);
                if (label) label.click();
                return {id: r.id, checked: r.checked};
            }""", row_index)
        if not info:
            print(f"     no existe el radio {row_index}")
            return False
        print(f"     radio {info['id']!r} checked={info['checked']}")
    except Exception as exc:
        print(f"     no se pudo marcar el radio: {type(exc).__name__}")
        return False

    for sel in ('button[value="Details"]', 'button:has-text("Details")',
                'input[value="Details"]'):
        try:
            loc = page.locator(sel).first
            if await loc.count():
                await loc.click(timeout=15000)
                print(f"     click {sel}")
                return True
        except Exception:
            continue
    print("     no se encontró el botón Details")
    return False


async def _wait_containers(page, seconds: int = 25) -> tuple[list[dict[str, str]], str, str]:
    """Espera a que aparezca al menos un contenedor en el DOM."""
    last_html, last_text = "", ""
    for _ in range(max(seconds // 2, 1)):
        await page.wait_for_timeout(2000)
        last_html, last_text = await _html(page)
        found = await _containers_from_page(page, last_html, last_text)
        if found:
            return found, last_html, last_text
    return [], last_html, last_text


async def main() -> int:
    ap = argparse.ArgumentParser(description="Flujo listado + detalle de Hapag.")
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
    listing_used = ""
    detail_ok = False

    try:
        async with AsyncCamoufox(**launch) as browser:
            page = await browser.new_page()
            page.set_default_timeout(90_000)
            page.on("dialog", lambda d: asyncio.create_task(d.accept()))

            # ---------------------------------------------------------------
            # A. Listado: tres estrategias, en orden de probabilidad
            # ---------------------------------------------------------------
            print("=" * 78)
            print(f"PASO A: obtener el listado del B/L {bl}")
            print("=" * 78)

            strategies = [
                ("A1-legacy-deeplink", f"{LEGACY}?blno={bl}", False),
                ("A2-legacy-form", LEGACY, True),
                ("A3-spa-form", BOOKING, True),
            ]

            for label, url, fill in strategies:
                print(f"\n  [{label}] {url[:120]}")
                try:
                    r = await page.goto(url, wait_until="domcontentloaded",
                                        timeout=90_000)
                    print(f"     HTTP {r.status if r else '?'}")
                    await page.wait_for_timeout(3000)
                    await _dismiss(page)
                    b = await _blocked(page)
                    if b:
                        print(f"     BLOQUEADO: {b}")
                        continue
                    if fill:
                        if not await _fill_and_submit(page, bl):
                            continue
                        await page.wait_for_timeout(4000)

                    found, html, text = await _wait_containers(page, 25)
                    print(f"     URL: {page.url[:120]}")
                    print(f"     HTML {len(html) // 1024}KB, texto {len(text)}B")
                    print(f"     contenedores: {len(found)}")
                    for c in found[:12]:
                        print(f"        {c['container']:<16} raw={c['raw']!r:<22} "
                              f"{c['source']}")
                    if found:
                        containers = found
                        listing_used = label
                        (dump / f"listado-{bl}.html").write_text(html, encoding="utf-8")
                        (dump / f"listado-{bl}.txt").write_text(text, encoding="utf-8")
                        (dump / f"listado-{bl}.json").write_text(
                            json.dumps(found, ensure_ascii=False, indent=1),
                            encoding="utf-8")
                        print(f"     -> guardado listado-{bl}.txt/.html/.json")
                        break
                    # Guardar también el fallo: se necesita para diagnosticar.
                    safe = re.sub(r"[^A-Za-z0-9]+", "-", label)
                    (dump / f"listado-{bl}-{safe}.txt").write_text(
                        text or "", encoding="utf-8")
                except Exception as exc:
                    print(f"     ERROR: {type(exc).__name__}: {str(exc)[:150]}")

            # ---------------------------------------------------------------
            # B. Detalle: seleccionar fila y pulsar Details (flujo JSF real)
            # ---------------------------------------------------------------
            print("\n" + "=" * 78)
            print("PASO B: detalle por contenedor (radio + botón Details)")
            print("=" * 78)

            if not containers:
                print("  sin contenedores: no hay detalle que pedir.")
            else:
                for idx, c in enumerate(containers[:args.max_containers]):
                    param = c.get("param") or ""
                    print(f"\n  [{idx + 1}/{len(containers)}] {c['container']}")
                    print(f"     celdas: {c.get('cells', '')[:110]}")
                    print(f"     param : {param!r}")
                    if not param:
                        print("     sin parámetro: no se puede pedir el detalle")
                        continue
                    # URL exactamente como la genera el submit del formulario.
                    url = f"{BOOKING}?view={VIEW}&container={param}"
                    print(f"     {url}")
                    try:
                        r = await page.goto(url, wait_until="domcontentloaded",
                                            timeout=90_000)
                        print(f"     HTTP {r.status if r else '?'}")
                        await page.wait_for_timeout(6000)
                        await _dismiss(page)
                        b = await _blocked(page)
                        if b:
                            print(f"     BLOQUEADO: {b}")
                            continue
                        html, text = await _html(page)
                        flat = " ".join(text.split())
                        print(f"     HTML {len(html) // 1024}KB, texto {len(text)}B")
                        print(f"     texto: {flat[:400]}")
                        safe = re.sub(r"[^A-Za-z0-9]+", "-", c["container"])
                        (dump / f"detalle-{safe}.html").write_text(html, encoding="utf-8")
                        (dump / f"detalle-{safe}.txt").write_text(text, encoding="utf-8")
                        hits = [k for k in ("Vessel", "Buque", "Voyage", "Viaje",
                                            "ETA", "Arrival", "Llegada", "Discharge",
                                            "Descarga", "MANZANILLO")
                                if k.lower() in flat.lower()]
                        if hits:
                            print(f"     términos de tracking: {hits}")
                        # El detalle real lista eventos y menciona el contenedor.
                        if c["container"][:4].lower() in flat.lower() and len(text) > 1500:
                            detail_ok = True
                            print("     -> detalle con datos")
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
    print(f"RESULTADO: listado={listing_used or 'ninguno'}, "
          f"{len(containers)} contenedores, "
          f"detalle={'OK' if detail_ok else 'sin datos'}")
    print("=" * 78)
    return 0 if containers else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
