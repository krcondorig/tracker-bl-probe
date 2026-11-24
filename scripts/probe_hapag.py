"""Sonda de Hapag-Lloyd: obtiene el JSON de tracking del endpoint real.

Camino recorrido hasta aquí (para no repetir el trabajo):

1. Sin WARP, `hapag-lloyd.com` devuelve Managed Challenge: bloqueo por IP.
2. Con WARP la web carga, pero `?blno=` y el formulario no disparan datos:
   el formulario hace GET a `/solutions/tracking/#/` (SPA con hash routing).
3. Minando el bundle JS de la SPA se encontró la llamada real:

       GET https://tracking.api.hlag.cloud/api/tracking/v{version}/events
           ?reference=<B/L>
       header: X-Turnstile-Token   (solo si tracking_turnstile_challenge)

   Con `tracking_backend_version: 3` y `tracking_turnstile_challenge: false`
   en la config por defecto.

4. `fetch()` desde la página a ese host dio "NetworkError": eso es CORS o
   DNS, no un 403. Por eso aquí se prueban tres vías distintas y se reporta
   qué falla en cada una, en vez de asumir.

Vías:
   A. fetch desde la página          -> sujeto a CORS
   B. navegación directa (page.goto) -> sin CORS, es un documento
   C. request del contexto           -> sin CORS, sale por el proxy

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
API_BASE = "https://tracking.api.hlag.cloud/api/tracking"
API_VERSION = "3"  # tracking_backend_version en la config de la SPA


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


def _json_or_text(body: str) -> Any:
    try:
        return json.loads(body)
    except Exception:
        return None


def _report(label: str, status: Any, ctype: str, body: str) -> bool:
    """Imprime una respuesta y dice si trae datos de tracking."""
    print(f"     {label}: HTTP {status}  {len(body)}B  {ctype[:35]}")
    if not body:
        return False
    parsed = _json_or_text(body)
    if parsed is None:
        print(f"        no-JSON: {' '.join(body.split())[:170]}")
        return False
    if isinstance(parsed, dict):
        print(f"        claves: {list(parsed)[:12]}")
        groups = parsed.get("groups")
        if isinstance(groups, list):
            print(f"        groups: {len(groups)}")
            for g in groups[:3]:
                if isinstance(g, dict):
                    evs = g.get("events") or []
                    print(f"           container={g.get('containerNumber')!r} "
                          f"events={len(evs)}")
                    if evs:
                        print(f"              primero: {json.dumps(evs[0])[:180]}")
            if groups:
                return True
        if parsed.get("errorKey"):
            print(f"        errorKey: {parsed['errorKey']!r}")
    return False


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

    api_url = f"{API_BASE}/v{API_VERSION}/events?reference={bl}"
    success = False

    try:
        async with AsyncCamoufox(**launch) as browser:
            page = await browser.new_page()
            page.set_default_timeout(90_000)
            page.on("dialog", lambda d: asyncio.create_task(d.accept()))

            print("=" * 78)
            print("PASO 0: cargar la SPA (contexto + pasar el challenge)")
            print("=" * 78)
            resp = await page.goto(SPA_URL, wait_until="domcontentloaded")
            print(f"  HTTP {resp.status if resp else '?'}")
            await page.wait_for_timeout(4000)
            await _dismiss(page)
            title = await page.title() or ""
            print(f"  title: {title[:70]!r}")
            if re.search(r"(just a moment|un momento|security check|access denied)",
                         title, re.I):
                print("  BLOQUEADO: revisa el proxy/WARP.")
                await page.close()
                return 1

            # -- Diagnóstico de red: distinguir CORS de falta de salida ------
            print("\n" + "=" * 78)
            print("PASO 1: diagnóstico (¿hay red? ¿es CORS?)")
            print("=" * 78)
            diag = await page.evaluate(
                """async () => {
                    const probe = async (u) => {
                        try {
                            const r = await fetch(u, {mode: 'no-cors'});
                            return 'opaque-ok status=' + r.status;
                        } catch (e) { return 'FAIL ' + String(e).slice(0, 60); }
                    };
                    const out = {};
                    out.ipify_no_cors = await probe('https://api.ipify.org');
                    out.hlag_no_cors = await probe('https://tracking.api.hlag.cloud/api/tracking/v3/events');
                    return out;
                }"""
            )
            print(f"  fetch no-cors a api.ipify.org  -> {diag.get('ipify_no_cors')}")
            print(f"  fetch no-cors a hlag.cloud     -> {diag.get('hlag_no_cors')}")

            # -- Vía B: navegación directa (sin CORS) -----------------------
            print("\n" + "=" * 78)
            print("PASO 2: vía B — navegar a la URL de la API")
            print("=" * 78)
            print(f"  {api_url}")
            try:
                r = await page.goto(api_url, wait_until="domcontentloaded",
                                    timeout=60_000)
                status = r.status if r else "?"
                ctype = ""
                body = ""
                if r is not None:
                    try:
                        ctype = (r.headers or {}).get("content-type", "")
                    except Exception:
                        pass
                    try:
                        body = await r.text()
                    except Exception:
                        body = await page.evaluate(
                            "() => document.body ? document.body.innerText : ''")
                if not body:
                    body = await page.evaluate(
                        "() => document.body ? document.body.innerText : ''")
                success = _report("via-B", status, ctype, body)
                (dump / f"viaB-{bl}.txt").write_text(
                    f"status={status}\nctype={ctype}\n\n{body}", encoding="utf-8")
            except Exception as exc:
                print(f"     via-B ERROR: {type(exc).__name__}: {str(exc)[:150]}")

            # -- Vía C: request del contexto (sin CORS, por el proxy) --------
            if not success:
                print("\n" + "=" * 78)
                print("PASO 3: vía C — request del contexto del navegador")
                print("=" * 78)
                for label, url in (
                    ("C-v3", api_url),
                    ("C-v3-token", api_url),
                ):
                    headers = {}
                    if label.endswith("token"):
                        headers["X-Turnstile-Token"] = ""
                    try:
                        r = await page.request.get(url, headers=headers, timeout=60_000)
                        body = await r.text()
                        ok = _report(label, r.status, r.headers.get("content-type", ""), body)
                        (dump / f"viaC-{bl}-{label}.txt").write_text(
                            f"status={r.status}\n\n{body}", encoding="utf-8")
                        if ok:
                            success = True
                            break
                    except Exception as exc:
                        print(f"     {label} ERROR: {type(exc).__name__}: {str(exc)[:150]}")

            # -- Vía A: fetch desde la página, ya sabiendo si es CORS --------
            if not success:
                print("\n" + "=" * 78)
                print("PASO 4: vía A — fetch desde la página")
                print("=" * 78)
                try:
                    res = await page.goto(SPA_URL, wait_until="domcontentloaded")
                    await page.wait_for_timeout(2500)
                    out = await page.evaluate(
                        """async (u) => {
                            try {
                                const r = await fetch(u, {headers: {'Accept': 'application/json'}});
                                const t = await r.text();
                                return {status: r.status, len: t.length, body: t.slice(0, 100000)};
                            } catch (e) { return {error: String(e)}; }
                        }""", api_url)
                    if out.get("error"):
                        print(f"     via-A ERROR: {out['error'][:140]}")
                    else:
                        success = _report("via-A", out.get("status"), "",
                                          out.get("body") or "")
                except Exception as exc:
                    print(f"     via-A ERROR: {type(exc).__name__}: {str(exc)[:150]}")

            await page.close()
    except Exception as exc:
        if args.proxy:
            print(f"\n  No se pudo abrir el navegador con proxy {args.proxy}: "
                  f"{type(exc).__name__}\n  ¿WARP conectado en modo proxy?")
            return 1
        raise

    print("\n" + "=" * 78)
    print("RESULTADO:", "DATOS OBTENIDOS" if success else "sin datos")
    print("=" * 78)
    return 0 if success else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
