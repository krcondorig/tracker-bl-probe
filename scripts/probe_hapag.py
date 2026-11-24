"""Sonda de Hapag-Lloyd: descubre el endpoint real de tracking para un B/L.

Existe porque el bloqueo de Hapag era de IP, no de navegador: con WARP la
página carga. Una vez que carga, lo que falta es saber **qué petición** trae
los datos de tracking y con qué forma.

Este script no asume nada: registra TODAS las respuestas JSON/XHR de la
sesión (URL, estado, tamaño, primeras claves) y las guarda enteras en disco.
También prueba el formulario, porque varias navieras no disparan el XHR desde
un deep link.

Uso:
    python scripts/probe_hapag.py HLCUNG12607EKZE0
    python scripts/probe_hapag.py HLCUNG12607EKZE0 --proxy socks5://127.0.0.1:40000 --headed
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
CONTAINER_URL = ("https://www.hapag-lloyd.com/en/online-business/track/"
                 "track-by-container-solution.html")

IGNORE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".woff",
              ".woff2", ".ttf", ".otf", ".css", ".map", ".mp4", ".webm")

# Ruido conocido: no son datos de tracking.
NOISE = re.compile(
    r"(cookielaw|onetrust|googletagmanager|google-analytics|doubleclick"
    r"|facebook|hotjar|segment\.io|newrelic|sentry|optimizely"
    r"|challenge-platform|challenges\.cloudflare|turnstile"
    r"|/etc/designs/.*/i18n/|fonts\.googleapis)",
    re.I,
)


class Capture:
    """Registra toda respuesta no-HTML con su URL y su cuerpo.

    Además guarda un diario de TODAS las peticiones (tipo, URL, estado o error).
    Sin eso no se puede distinguir "la página no pidió datos" de "los pidió y
    se los bloquearon", que es justo la duda cuando el challenge ya pasó.
    """

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
            failure = request.failure or "?"
            self.netlog.append(("FAILED", request.method, f"{request.url}  <- {failure}"))
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
                "ctype": ctype,
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


def _top_keys(body: Any) -> list[str]:
    if isinstance(body, dict):
        return [str(k) for k in list(body)[:12]]
    if isinstance(body, list):
        first = body[0] if body else None
        return [f"[{len(body)}]" ] + ([str(k) for k in list(first)[:10]]
                                      if isinstance(first, dict) else [])
    return []


# Claves que delatan un payload de tracking real de Hapag.
GOOD_KEYS = ("containers", "container", "shipments", "shipment", "billOfLading",
             "billoflading", "blNumber", "blnumber", "voyage", "vessel",
             "transportEvents", "events", "pol", "pod", "equipment")
# Señales de que es un diccionario de textos y no datos.
BAD_KEYS = ("translation", "translations", "locale", "i18n", "messages", "labels")


def _score(item: dict[str, Any]) -> int:
    """Puntúa un JSON por probabilidad de ser el payload de tracking.

    Existe porque el extractor anterior aceptó un fichero de traducciones con
    la palabra "tracking" en la URL. Puntuar en vez de aceptar el primero
    evita repetir ese error aquí.
    """
    keys = " ".join(item["keys"]).lower()
    url = item["url"].lower()
    score = 0
    score += 3 * sum(1 for k in GOOD_KEYS if k.lower() in keys)
    if "api" in url or "/service" in url or "/rest" in url:
        score += 2
    if "track" in url or "shipment" in url:
        score += 2
    if item["size"] > 3000:
        score += 1
    score -= 4 * sum(1 for k in BAD_KEYS if k.lower() in keys)
    if any(k in url for k in ("i18n", "translation", "locale")):
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


async def _inventory(page) -> None:
    """Qué formularios y campos tiene la página, para saber cómo consultarla."""
    try:
        info = await page.evaluate(
            """() => ({
                inputs: Array.from(document.querySelectorAll('input,select'))
                    .map(el => ({tag: el.tagName, id: el.id, name: el.name,
                                 type: el.type, ph: el.placeholder,
                                 label: (el.labels && el.labels[0] && el.labels[0].innerText || '').trim().slice(0,40)}))
                    .filter(el => el.type !== 'hidden')
                    .slice(0, 20),
                buttons: Array.from(document.querySelectorAll('button'))
                    .map(b => ({id: b.id, text: (b.innerText||'').trim().slice(0,30),
                                type: b.type, form: b.form && b.form.id}))
                    .slice(0, 20),
                forms: Array.from(document.querySelectorAll('form'))
                    .map(f => ({id: f.id, action: f.action, method: f.method})).slice(0, 10)
            })"""
        )
        print("\n  --- inventario del DOM ---")
        print("  inputs:")
        for i in info.get("inputs", []):
            print(f"     {i}")
        print("  buttons:")
        for b in info.get("buttons", []):
            if b.get("text") or b.get("id"):
                print(f"     {b}")
        print("  forms:")
        for f in info.get("forms", []):
            print(f"     {f}")
    except Exception as exc:
        print(f"  inventario FALLÓ: {type(exc).__name__}")


async def _try_fill(page, bl: str) -> None:
    """Rellena el primer campo de texto visible y pulsa Enter."""
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
            return
        label = await page.evaluate(
            """(el) => (el.id || el.name || el.placeholder || el.type || '?')""", el)
        print(f"  formulario: usando campo {label!r}")
        try:
            await el.click(timeout=6000)
            await el.fill("")
            await el.type(bl, delay=80)
        except Exception:
            await el.evaluate(
                """(e, v) => { e.value = v;
                     e.dispatchEvent(new Event('input', {bubbles:true}));
                     e.dispatchEvent(new Event('change', {bubbles:true})); }""",
                bl)
        await el.press("Enter")
        print("  formulario: B/L escrito + Enter")
    except Exception as exc:
        print(f"  formulario FALLÓ: {type(exc).__name__}: {str(exc)[:120]}")


async def run(browser, label: str, url: str, bl: str, wait_ms: int,
              fill: bool, dump: Path) -> None:
    print("\n" + "=" * 78)
    print(f"RUTA: {label}")
    print(f"URL : {url}")
    print("=" * 78)

    capture = Capture()
    page = await browser.new_page()
    page.set_default_timeout(60_000)
    try:
        capture.attach(page)
        page.on("dialog", lambda d: asyncio.create_task(d.accept()))

        resp = await page.goto(url, wait_until="domcontentloaded")
        print(f"  HTTP {resp.status if resp else '?'}")
        await page.wait_for_timeout(4000)
        await _dismiss(page)

        title = await page.title() or ""
        print(f"  title: {title[:70]!r}")

        blocked = bool(re.search(
            r"(security check|just a moment|un momento|attention required"
            r"|access denied)", title, re.I))
        if blocked:
            body = await page.evaluate(
                "() => document.body ? document.body.innerText.slice(0,300) : ''")
            print(f"  BLOQUEADO: {' '.join(body.split())[:180]}")
            await page.close()
            return

        await _inventory(page)

        if fill:
            await _try_fill(page, bl)
            await page.wait_for_timeout(2000)

        # Espera activa: en cuanto aparece una respuesta grande y con pinta de
        # datos, deja de esperar.
        for _ in range(max(wait_ms // 1000, 1)):
            await asyncio.sleep(1)
            if any(not it["noise"] and it["size"] > 1500 and
                   re.search(r"(container|shipment|track|bl|booking)", it["url"], re.I)
                   for it in capture.items):
                break

        await capture.settle()
        await page.wait_for_timeout(1500)

        print(f"\n  respuestas JSON capturadas: {len(capture.items)}")
        print(f"  {'EST':<4} {'KB':>6}  {'RUIDO':<5} URL")
        print(f"  {'-'*4} {'-'*6}  {'-'*5} {'-'*60}")
        for it in capture.items:
            print(f"  {it['status']:<4} {it['size']/1024:6.1f}  "
                  f"{'si' if it['noise'] else 'no':<5} {it['url'][:110]}")
            if it["keys"]:
                print(f"            claves: {it['keys']}")

        # Diario de red: distingue "no pidió datos" de "se los bloquearon".
        relevant = [(t, m, u) for (t, m, u) in capture.netlog
                    if t in ("document", "xhr", "fetch", "FAILED")]
        print(f"\n  peticiones relevantes (document/xhr/fetch/fallidas): {len(relevant)}")
        for t, m, u in relevant[:40]:
            mark = "  " if u.startswith("https://www.hapag-lloyd.com") else "→ "
            print(f"   {mark}{t:<9} {m:<4} {u[:100]}")
        if len(relevant) > 40:
            print(f"   ... y {len(relevant) - 40} más")
        # Deja el diario completo en disco: el recorte a 40 oculta justo las
        # peticiones que suelen importar (las últimas).
        netlog_file = dump / f"{label}-netlog.txt"
        dump.mkdir(parents=True, exist_ok=True)
        netlog_file.write_text(
            "\n".join(f"{t}\t{m}\t{u}" for (t, m, u) in capture.netlog),
            encoding="utf-8")
        print(f"  diario de red: {netlog_file}")

        # Guardar solo lo que no es ruido, y además lo que parece tracking.
        dump.mkdir(parents=True, exist_ok=True)
        saved = 0
        for idx, it in enumerate(capture.items):
            if it["noise"]:
                continue
            slug = re.sub(r"[^a-zA-Z0-9]+", "-", it["url"].split("?")[0].split("/")[-1])[:40]
            target = dump / f"{label}-{idx:02d}-{slug or 'resp'}.json"
            target.write_text(json.dumps(it["body"], ensure_ascii=False, indent=1),
                              encoding="utf-8")
            saved += 1
        print(f"\n  guardados {saved} JSON (sin ruido) en {dump}")

        # Candidatos: ordena por probabilidad de ser el payload de tracking.
        # Evita tener que abrir 30 dumps a mano para encontrar el bueno.
        cands = sorted(
            (it for it in capture.items if not it["noise"]),
            key=lambda it: _score(it), reverse=True)
        print("\n  --- candidatos a payload de tracking ---")
        for it in cands[:8]:
            s = _score(it)
            if s <= 0:
                continue
            print(f"   score {s:2d}  {it['size']/1024:6.1f} KB  {it['url'][:95]}")
            print(f"            claves: {it['keys']}")

    except Exception as exc:
        print(f"  ERROR: {type(exc).__name__}: {str(exc)[:160]}")
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def main() -> int:
    ap = argparse.ArgumentParser(description="Descubre el endpoint de tracking de Hapag.")
    ap.add_argument("bl", help="B/L o booking de Hapag")
    ap.add_argument("--proxy", default="", help="Proxy SOCKS5, p. ej. socks5://127.0.0.1:40000")
    ap.add_argument("--wait", type=int, default=30, help="Segundos de espera por ruta")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--dump", default="probe-dumps/hapag", help="Carpeta de salida")
    args = ap.parse_args()

    bl = re.sub(r"[^A-Z0-9]", "", args.bl.upper())
    dump = Path(args.dump)
    wait_ms = args.wait * 1000

    launch: dict[str, Any] = dict(headless=not args.headed, geoip=True, locale="es-PE",
                                  os="windows", humanize=True, window=(1440, 900))
    if args.proxy:
        launch["proxy"] = {"server": args.proxy}

    routes = [
        ("deep-booking", f"{BOOKING_URL}?blno={bl}", False),
        ("deep-container", f"{CONTAINER_URL}?blno={bl}", False),
        ("form-booking", BOOKING_URL, True),
    ]

    try:
        async with AsyncCamoufox(**launch) as browser:
            for label, url, fill in routes:
                await run(browser, label, url, bl, wait_ms, fill, dump)
                await asyncio.sleep(3)
    except Exception as exc:
        if args.proxy:
            print(f"\n  No se pudo abrir el navegador con proxy {args.proxy}: "
                  f"{type(exc).__name__}\n  ¿WARP conectado en modo proxy?")
            return 1
        raise

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
