"""Sonda de Hapag-Lloyd: obtiene el JSON de tracking del endpoint real.

Historia de por qué existe este script (el camino hasta aquí):

1. Sin WARP, `hapag-lloyd.com` devuelve Managed Challenge: bloqueo por IP.
2. Con WARP la web carga, pero `?blno=` y el formulario no disparan datos:
   el formulario hace GET a `/solutions/tracking/#/` (SPA con hash routing).
3. Minando el bundle JS de la SPA se encontró la llamada real:

       GET https://tracking.api.hlag.cloud/api/tracking/v{version}/events
       params: reference=<B/L>            (más turnstileToken si aplica)
       header: X-Turnstile-Token         (solo si hay challenge)

   La versión sale de la config (`tracking_backend_version: 3`) y el flag
   `tracking_turnstile_challenge: false` dice que, por defecto, no hay
   challenge en la API.

Este script ataca ese endpoint directamente, desde dentro del navegador
(que es quien tiene el contexto y la IP buena de WARP), y guarda la respuesta.

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

SPA_URL = "https://www.hapag-lloyd.com/solutions/tracking/"
BOOKING_URL = ("https://www.hapag-lloyd.com/en/online-business/track/"
               "track-by-booking-solution.html")
API_BASE = "https://tracking.api.hlag.cloud/api/tracking"


async def _dismiss(page) -> None:
    for sel in ("#onetrust-accept-btn-handler",
                'button[id*="accept" i]',
                'button:has-text("Accept all")',
                'button:has-text("Aceptar todo")'):
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


async def _api_call(page, url: str, bl: str) -> dict[str, Any] | None:
    """Llama al endpoint desde dentro de la página (contexto del navegador).

    Llamar desde la página y no desde httpx importa: así la petición sale con
    la IP de WARP, el User-Agent del navegador y las cookies del dominio, que
    es exactamente lo que hace la SPA real.
    """
    try:
        result = await page.evaluate(
            """async ({url, bl}) => {
                const sep = url.includes('?') ? '&' : '?';
                const full = url + sep + 'reference=' + encodeURIComponent(bl);
                try {
                    const r = await fetch(full, {
                        method: 'GET',
                        credentials: 'include',
                        headers: {'Accept': 'application/json'}
                    });
                    const text = await r.text();
                    return {status: r.status, ctype: r.headers.get('content-type') || '',
                            len: text.length, body: text.slice(0, 200000)};
                } catch (e) {
                    return {error: String(e)};
                }
            }""",
            {"url": url, "bl": bl},
        )
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    return result


def _summarize(label: str, res: dict[str, Any]) -> dict[str, Any] | None:
    """Imprime el resultado de una llamada y devuelve el JSON si es válido."""
    if res.get("error"):
        print(f"     {label}: ERROR {res['error'][:130]}")
        return None
    status = res.get("status")
    body = res.get("body") or ""
    print(f"     {label}: HTTP {status}  {res.get('len', 0)}B  "
          f"{res.get('ctype', '')[:35]}")
    if not body:
        return None
    try:
        parsed = json.loads(body)
    except Exception:
        snippet = " ".join(body.split())[:160]
        print(f"        no-JSON: {snippet}")
        return None
    if isinstance(parsed, dict):
        print(f"        claves: {list(parsed)[:12]}")
        groups = parsed.get("groups")
        if isinstance(groups, list):
            print(f"        groups: {len(groups)}")
            for g in groups[:3]:
                if isinstance(g, dict):
                    print(f"           container={g.get('containerNumber')!r} "
                          f"events={len(g.get('events') or [])}")
                    for ev in (g.get("events") or [])[:2]:
                        print(f"              {ev}")
        if parsed.get("errorKey"):
            print(f"        errorKey: {parsed['errorKey']!r}")
    return parsed


async def main() -> int:
    ap = argparse.ArgumentParser(description="Consulta el endpoint de tracking de Hapag.")
    ap.add_argument("bl", help="B/L o booking de Hapag")
    ap.add_argument("--proxy", default="", help="Proxy SOCKS5, p. ej. socks5://127.0.0.1:40000")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--dump", default="probe-dumps/hapag", help="Carpeta de salida")
    args = ap.parse_args()

    bl = re.sub(r"[^A-Z0-9]", "", args.bl.upper())
    dump = Path(args.dump)
    dump.mkdir(parents=True, exist_ok=True)

    launch: dict[str, Any] = dict(headless=not args.headed, geoip=True,
                                  locale="es-PE", os="windows", humanize=True,
                                  window=(1440, 900))
    if args.proxy:
        launch["proxy"] = {"server": args.proxy}

    # Variantes: la version que declara la config es 3, pero conviene saber
    # si las demas responden igual (util si cambian de version sin avisar).
    variants = [
        ("v3", f"{API_BASE}/v3/events"),
        ("v2", f"{API_BASE}/v2/events"),
        ("v1", f"{API_BASE}/v1/events"),
        ("sin-version", f"{API_BASE}/events"),
    ]

    found: dict[str, Any] | None = None
    try:
        async with AsyncCamoufox(**launch) as browser:
            page = await browser.new_page()
            page.set_default_timeout(60_000)
            page.on("dialog", lambda d: asyncio.create_task(d.accept()))

            print("=" * 78)
            print("PASO 1: cargar la SPA (para tener contexto y pasar el challenge)")
            print("=" * 78)
            resp = await page.goto(SPA_URL, wait_until="domcontentloaded")
            print(f"  HTTP {resp.status if resp else '?'}")
            await page.wait_for_timeout(4000)
            await _dismiss(page)
            title = await page.title() or ""
            print(f"  title: {title[:70]!r}")
            if re.search(r"(just a moment|un momento|security check|access denied)",
                         title, re.I):
                print("  BLOQUEADO en la SPA: revisa el proxy/WARP.")
                await page.close()
                return 1

            print("\n" + "=" * 78)
            print("PASO 2: llamar al endpoint de tracking")
            print("=" * 78)
            for label, url in variants:
                print(f"\n  [{label}] {url}")
                res = await _api_call(page, url, bl)
                parsed = _summarize(label, res or {})
                if parsed and isinstance(parsed.get("groups"), list) and parsed["groups"]:
                    found = parsed
                    (dump / f"hapag-{bl}-{label}.json").write_text(
                        json.dumps(parsed, ensure_ascii=False, indent=1),
                        encoding="utf-8")
                    print(f"     -> DATOS: guardado hapag-{bl}-{label}.json")
                    break
                if parsed is not None:
                    (dump / f"hapag-{bl}-{label}.json").write_text(
                        json.dumps(parsed, ensure_ascii=False, indent=1),
                        encoding="utf-8")

            if not found:
                print("\n" + "=" * 78)
                print("PASO 3: el endpoint no devolvió grupos; probar vía formulario")
                print("=" * 78)
                await page.goto(BOOKING_URL, wait_until="domcontentloaded")
                await page.wait_for_timeout(2500)
                await _dismiss(page)
                try:
                    handle = await page.evaluate_handle(
                        """() => Array.from(document.querySelectorAll('input'))
                            .filter(el => el.offsetParent !== null &&
                                          !['hidden','checkbox','radio']
                                            .includes(el.type))[0] || null"""
                    )
                    el = handle.as_element()
                    if el is not None:
                        await el.click(timeout=5000)
                        await el.fill("")
                        await el.type(bl, delay=70)
                        await el.press("Enter")
                        await page.wait_for_timeout(6000)
                        print(f"  URL tras Enter: {page.url}")
                except Exception as exc:
                    print(f"  formulario falló: {type(exc).__name__}")

            await page.close()
    except Exception as exc:
        if args.proxy:
            print(f"\n  No se pudo abrir el navegador con proxy {args.proxy}: "
                  f"{type(exc).__name__}\n  ¿WARP conectado en modo proxy?")
            return 1
        raise

    print("\n" + "=" * 78)
    print("RESULTADO:", "datos obtenidos" if found else "sin datos")
    print("=" * 78)
    return 0 if found else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
