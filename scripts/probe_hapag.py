"""Sonda de Hapag-Lloyd: descubre la ruta y el endpoint de tracking reales.

Contexto de por qué existe este script:

1. Sin WARP la web devuelve Managed Challenge (bloqueo por IP).
2. Con WARP la web carga (HTTP 200, title 'Tracking | Hapag-Lloyd').
3. Pero `?blno=` y el formulario no disparan ninguna petición de datos: el
   formulario hace GET a `/solutions/tracking/#/`, una SPA con rutas hash, y
   el B/L se pierde en el camino.

Así que lo que falta no es adivinar más URLs, es averiguar cómo la SPA llama a
su backend. Este script lo hace en tres fases:

  A. Interacción: rellena el campo y pulsa el botón real "Next" (no Enter),
     y reporta a qué URL termina navegando.
  B. Minería de bundles: descarga el JS de la app y busca en él rutas y
     endpoints (`/api/...`, `track...`). El endpoint está en el código que el
     navegador ya se descargó; no hace falta adivinarlo.
  C. Rutas candidatas: prueba variantes de deep link hash y anota cuál
     provoca peticiones de datos.

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

BOOKING_URL = ("https://www.hapag-lloyd.com/en/online-business/track/"
               "track-by-booking-solution.html")
SPA_URL = "https://www.hapag-lloyd.com/solutions/tracking/"

IGNORE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".woff",
              ".woff2", ".ttf", ".otf", ".css", ".map", ".mp4", ".webm")

NOISE = re.compile(
    r"(cookielaw|onetrust|googletagmanager|google-analytics|doubleclick"
    r"|facebook|hotjar|segment\.io|newrelic|sentry|optimizely"
    r"|challenge-platform|challenges\.cloudflare|turnstile"
    r"|/etc/designs/.*/i18n/|fonts\.googleapis|launchdarkly|usabilla"
    r"|/cf-services/geolocation|/navigation/)",
    re.I,
)

# Patrones para minar el bundle JS de la app.
API_PATTERNS = [
    # Rutas absolutas que huelen a datos de tracking.
    re.compile(r"""["'`](/(?:api|rest|service|services|solutions)[A-Za-z0-9/_.\-{}]{3,80})["'`]"""),
    # Endpoints construidos por concatenación de servicio + recurso.
    re.compile(r"""["'`]([A-Za-z0-9/_\-]*(?:track|shipment|billof|bill-of|blno|booking)[A-Za-z0-9/_\-]*)["'`]""", re.I),
    # Hosts de backend.
    re.compile(r"""["'`](https?://[A-Za-z0-9.\-]+/[A-Za-z0-9/_\-]{0,60})["'`]"""),
]
# Rutas de la SPA (hash routing) para construir el deep link.
HASH_PATTERN = re.compile(r"""["'`]#?/([a-z][A-Za-z0-9/_\-{}\$:]{2,60})["'`]""")


class Capture:
    """Registra respuestas JSON y un diario de todas las peticiones."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self.tasks: set[asyncio.Task] = set()
        self.netlog: list[tuple[str, str, str]] = []

    def attach(self, page) -> None:
        def schedule(response):
            task = asyncio.create_task(self._handle(response))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

        page.on("response", schedule)
        page.on("request", self._on_request)
        page.on("requestfailed", self._on_failed)

    def _on_request(self, request) -> None:
        try:
            url = request.url
            if request.method == "POST":
                body = request.post_data or ""
                url = f"{url}  [POST {body[:300]}]"
            self.netlog.append((request.resource_type, request.method, url))
        except Exception:
            pass

    def _on_failed(self, request) -> None:
        try:
            self.netlog.append(("FAILED", request.method,
                                f"{request.url}  <- {request.failure or '?'}"))
        except Exception:
            pass

    async def _handle(self, response) -> None:
        try:
            url = response.url
            base = url.lower().split("?")[0]
            if any(base.endswith(ext) for ext in IGNORE_EXT):
                return
            ctype = (response.headers or {}).get("content-type", "").lower()
            if "text/html" in ctype:
                return
            text = await response.text()
            if not text or text.lstrip()[:1] not in "[{":
                return
            body = json.loads(text)
            self.items.append({
                "url": url,
                "status": response.status,
                "size": len(text),
                "noise": bool(NOISE.search(url)),
                "keys": _top_keys(body),
                "body": body,
            })
        except Exception:
            pass

    async def settle(self) -> None:
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)

    def data_requests(self) -> list[tuple[str, str, str]]:
        """Peticiones que podrían traer datos (incluye POST, que es lo normal
        en APIs de tracking por número de documento)."""
        out = []
        for t, m, u in self.netlog:
            if t not in ("xhr", "fetch"):
                continue
            if NOISE.search(u):
                continue
            out.append((t, m, u))
        return out


def _top_keys(body: Any) -> list[str]:
    if isinstance(body, dict):
        return [str(k) for k in list(body)[:12]]
    if isinstance(body, list):
        first = body[0] if body else None
        return [f"[{len(body)}]"] + ([str(k) for k in list(first)[:10]]
                                     if isinstance(first, dict) else [])
    return []


GOOD_KEYS = ("containers", "container", "shipments", "shipment", "billoflading",
             "blnumber", "voyage", "vessel", "transportevents", "events",
             "pol", "pod", "equipment", "carrierbooking")
BAD_KEYS = ("translation", "translations", "locale", "i18n", "messages", "labels")


def _score(item: dict[str, Any]) -> int:
    """Puntúa un JSON por probabilidad de ser el payload de tracking.

    Existe porque el extractor anterior aceptó un i18n con "tracking" en la
    URL, y un diccionario de textos no trae contenedores.
    """
    keys = " ".join(item["keys"]).lower()
    url = item["url"].lower()
    score = 3 * sum(1 for k in GOOD_KEYS if k in keys)
    if any(x in url for x in ("/api", "/rest", "/service")):
        score += 2
    if item["size"] > 3000:
        score += 1
    score -= 4 * sum(1 for k in BAD_KEYS if k in keys)
    if any(x in url for x in ("i18n", "translation", "locale")):
        score -= 6
    return score


async def _dismiss(page) -> None:
    for sel in ("#onetrust-accept-btn-handler",
                'button[id*="accept" i]',
                'button:has-text("Accept all")',
                'button:has-text("Aceptar todo")'):
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=2500)
                await page.wait_for_timeout(800)
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


async def _fill_input(page, bl: str) -> str:
    """Rellena el campo de búsqueda. Devuelve el id usado o ''."""
    try:
        handle = await page.evaluate_handle(
            """() => {
                const cands = Array.from(document.querySelectorAll('input'))
                    .filter(el => !['hidden','checkbox','radio','submit','button']
                                    .includes(el.type) && el.offsetParent !== null);
                return cands[0] || null;
            }"""
        )
        el = handle.as_element()
        if el is None:
            print("  formulario: no hay campo de texto visible")
            return ""
        ident = await page.evaluate(
            """(el) => el.id || el.name || el.placeholder || '?'""", el)
        try:
            await el.click(timeout=6000)
            await el.fill("")
            await el.type(bl, delay=70)
        except Exception:
            await el.evaluate(
                """(e, v) => { e.value = v;
                     e.dispatchEvent(new Event('input', {bubbles:true}));
                     e.dispatchEvent(new Event('change', {bubbles:true})); }""",
                bl)
        print(f"  formulario: B/L escrito en {ident!r}")
        return ident
    except Exception as exc:
        print(f"  formulario FALLÓ: {type(exc).__name__}: {str(exc)[:120]}")
        return ""


async def _click_next(page) -> None:
    """Pulsa el botón 'Next' real. Enter en un input type=search solo envía
    el formulario GET a `#/` y pierde el número; el botón hace lo mismo salvo
    que la SPA lo intercepte. Vale la pena comprobar cuál es cuál."""
    for sel in ('button:has-text("Next")', "button.next", 'button[type="submit"]'):
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=5000)
                print(f"  botón: click en {sel}")
                return
        except Exception:
            continue
    print("  botón: no se encontró 'Next' visible")


async def _mine_bundles(page, bl: str) -> None:
    """Descarga el JS de la app y busca endpoints y rutas dentro.

    El navegador ya bajó ese código; leerlo es más fiable que adivinar URLs.
    """
    print("\n  --- minería de bundles JS ---")
    try:
        srcs = await page.evaluate(
            """() => Array.from(document.querySelectorAll('script[src]'))
                        .map(s => s.src).filter(Boolean)"""
        )
    except Exception:
        srcs = []
    srcs = [s for s in srcs if s.endswith(".js") or ".js?" in s]
    print(f"  scripts en la página: {len(srcs)}")
    for s in srcs[:12]:
        print(f"     {s[:110]}")

    seen: set[str] = set()
    api_hits: list[str] = []
    hash_hits: list[str] = []
    for src in srcs[:12]:
        try:
            text = await page.evaluate(
                """async (u) => {
                    try {
                        const r = await fetch(u, {credentials: 'same-origin'});
                        if (!r.ok) return '';
                        return await r.text();
                    } catch (e) { return ''; }
                }""", src)
        except Exception:
            continue
        if not text:
            continue
        for pat in API_PATTERNS:
            for m in pat.finditer(text):
                val = m.group(1)
                if val in seen or len(val) < 5:
                    continue
                seen.add(val)
                api_hits.append(f"{val}   <- {src.split('/')[-1][:40]}")
        for m in HASH_PATTERN.finditer(text):
            val = "#/" + m.group(1)
            if val not in seen:
                seen.add(val)
                hash_hits.append(val)

    print(f"\n  endpoints/rutas candidatas encontrados: {len(api_hits)}")
    for h in api_hits[:40]:
        print(f"     {h}")
    if len(api_hits) > 40:
        print(f"     ... y {len(api_hits) - 40} más")

    print(f"\n  rutas hash de la SPA encontradas: {len(hash_hits)}")
    for h in hash_hits[:40]:
        print(f"     {h}")
    if len(hash_hits) > 40:
        print(f"     ... y {len(hash_hits) - 40} más")

    # Guardar para análisis posterior.
    dump = ROOT / "probe-dumps" / "hapag"
    dump.mkdir(parents=True, exist_ok=True)
    (dump / "mined-endpoints.txt").write_text(
        "\n".join(api_hits) + "\n\n=== rutas hash ===\n" + "\n".join(hash_hits),
        encoding="utf-8")
    print(f"\n  guardado en {dump / 'mined-endpoints.txt'}")


async def _probe_route(page, label: str, url: str, wait_ms: int) -> None:
    """Carga una URL y reporta si provocó peticiones de datos."""
    cap = Capture()
    cap.attach(page)
    print(f"\n  [{label}] {url}")
    try:
        resp = await page.goto(url, wait_until="domcontentloaded")
        print(f"     HTTP {resp.status if resp else '?'}")
        await page.wait_for_timeout(2500)
        await _dismiss(page)
        for _ in range(max(wait_ms // 1000, 1)):
            await asyncio.sleep(1)
            if cap.data_requests():
                break
        await cap.settle()
        reqs = cap.data_requests()
        print(f"     peticiones de datos: {len(reqs)}")
        for t, m, u in reqs[:12]:
            print(f"        {t:<6} {m:<4} {u[:105]}")
        if len(reqs) > 12:
            print(f"        ... y {len(reqs) - 12} más")
        if cap.items:
            best = max(cap.items, key=_score)
            print(f"     mejor JSON: score={_score(best)} {best['size']//1024}KB "
                  f"{best['url'][:80]}")
            print(f"        claves: {best['keys']}")
    except Exception as exc:
        print(f"     ERROR: {type(exc).__name__}: {str(exc)[:140]}")


async def main() -> int:
    ap = argparse.ArgumentParser(description="Descubre el endpoint de tracking de Hapag.")
    ap.add_argument("bl", help="B/L o booking de Hapag")
    ap.add_argument("--proxy", default="", help="Proxy SOCKS5, p. ej. socks5://127.0.0.1:40000")
    ap.add_argument("--wait", type=int, default=25, help="Segundos de espera por ruta")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--dump", default="probe-dumps/hapag", help="Carpeta de salida")
    args = ap.parse_args()

    bl = re.sub(r"[^A-Z0-9]", "", args.bl.upper())
    wait_ms = args.wait * 1000

    launch: dict[str, Any] = dict(headless=not args.headed, geoip=True,
                                  locale="es-PE", os="windows", humanize=True,
                                  window=(1440, 900))
    if args.proxy:
        launch["proxy"] = {"server": args.proxy}

    try:
        async with AsyncCamoufox(**launch) as browser:
            page = await browser.new_page()
            page.set_default_timeout(60_000)
            page.on("dialog", lambda d: asyncio.create_task(d.accept()))

            # ---------------------------------------------------------------
            # Fase A: interacción y observación de la navegación
            # ---------------------------------------------------------------
            cap = Capture()
            cap.attach(page)
            print("=" * 78)
            print("FASE A: formulario + botón Next")
            print("=" * 78)
            resp = await page.goto(BOOKING_URL, wait_until="domcontentloaded")
            print(f"  HTTP {resp.status if resp else '?'}")
            await page.wait_for_timeout(3000)
            await _dismiss(page)
            print(f"  URL inicial: {page.url}")
            await _fill_input(page, bl)
            await _click_next(page)
            await page.wait_for_timeout(5000)
            print(f"  URL tras Next: {page.url}")
            await page.wait_for_timeout(3000)
            await cap.settle()
            reqs = cap.data_requests()
            print(f"  peticiones de datos tras Next: {len(reqs)}")
            for t, m, u in reqs[:10]:
                print(f"     {t:<6} {m:<4} {u[:105]}")

            # ---------------------------------------------------------------
            # Fase B: minar el JS de la app
            # ---------------------------------------------------------------
            print("\n" + "=" * 78)
            print("FASE B: minería de bundles")
            print("=" * 78)
            await page.goto(SPA_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(3500)
            await _dismiss(page)
            await _mine_bundles(page, bl)

            # ---------------------------------------------------------------
            # Fase C: rutas hash candidatas
            # ---------------------------------------------------------------
            print("\n" + "=" * 78)
            print("FASE C: rutas hash candidatas")
            print("=" * 78)
            candidates = [
                ("spa-raiz", SPA_URL),
                ("hash-blno", f"{SPA_URL}#/tracking?blno={bl}"),
                ("hash-bl", f"{SPA_URL}#/bl/{bl}"),
                ("hash-booking", f"{SPA_URL}#/booking/{bl}"),
                ("hash-tracking-bl", f"{SPA_URL}#/tracking/{bl}"),
                ("hash-bill-of-lading", f"{SPA_URL}#/bill-of-lading/{bl}"),
            ]
            for label, url in candidates:
                await _probe_route(page, label, url, wait_ms)

            await page.close()

    except Exception as exc:
        if args.proxy:
            print(f"\n  No se pudo abrir el navegador con proxy {args.proxy}: "
                  f"{type(exc).__name__}\n  ¿WARP conectado en modo proxy?")
            return 1
        raise

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
