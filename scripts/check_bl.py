"""Consulta un B/L y dice si el buque ya llegó a Manzanillo.

Script independiente, sin depender del paquete `tracker`: sirve para probar de
verdad si el acceso funciona contra las tres navieras, que es el problema real
(challenge de Cloudflare/Akamai).

Uso:
    python scripts/check_bl.py MEDUADP85690
    python scripts/check_bl.py 275792562 HLCUNG12607EKZE0
    python scripts/check_bl.py MEDUADP85690 --headed --port MANZANILLO

Estrategia por naviera (verificada contra los sitios):
    MSC     -> hay que rellenar el formulario #trackingNumber y pulsar Enter.
               Los deep links ?trackingNumber=/&trackingMode=1 NO disparan el XHR.
    Maersk  -> el deep link /tracking/{bl} sí dispara el XHR.
    Hapag   -> ?blno={bl}. Sin WARP suele devolver challenge de Cloudflare.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from camoufox.async_api import AsyncCamoufox  # noqa: E402

DEFAULT_PORT = "MANZANILLO"

# --------------------------------------------------------------------------- #
# Clasificación del B/L
# --------------------------------------------------------------------------- #
BL_PATTERNS: tuple[tuple[str, str], ...] = (
    ("MSC", r"^(?:MEDU|MSCU)[A-Z0-9]{8}$"),
    ("MAERSK", r"^\d{9}$"),
    ("HAPAG_LLOYD", r"^HLCU[A-Z0-9]{12}$"),
)


def classify(raw: str) -> tuple[str, str]:
    """Devuelve (naviera, número normalizado) o ('UNKNOWN', número)."""
    number = re.sub(r"[^A-Z0-9]", "", (raw or "").upper())
    for carrier, pattern in BL_PATTERNS:
        if re.match(pattern, number):
            return carrier, number
    return "UNKNOWN", number


# --------------------------------------------------------------------------- #
# Fechas
# --------------------------------------------------------------------------- #
_DATE_FORMATS = ("%d/%m/%Y", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                 "%Y-%m-%d %H:%M:%S", "%Y-%m-%d")


def parse_date(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def fmt(value: datetime | None) -> str:
    return value.strftime("%d/%m/%Y") if value else "—"


# --------------------------------------------------------------------------- #
# Modelo de salida
# --------------------------------------------------------------------------- #
@dataclass
class ContainerReport:
    number: str
    arrived: bool = False
    arrival_date: datetime | None = None
    eta: datetime | None = None
    eta_note: str = ""
    last_event: str = ""
    last_event_at: datetime | None = None
    vessel: str = ""
    voyage: str = ""


@dataclass
class Report:
    reference: str
    carrier: str
    ok: bool = False
    blocked: bool = False
    message: str = ""
    source_url: str = ""
    port: str = ""
    containers: list[ContainerReport] = field(default_factory=list)
    raw: dict | None = None


ARRIVAL_WORDS = re.compile(r"(discharg|arriv|descarg|atracad)", re.I)
OCEAN_WORDS = re.compile(r"(load|departure|discharg|arriv)", re.I)


def _port_matches(text: str, port: str) -> bool:
    return bool(text) and port.upper() in text.upper()


# --------------------------------------------------------------------------- #
# Extractores
# --------------------------------------------------------------------------- #
def extract_msc(payload: dict, port: str) -> Report | None:
    try:
        bl = payload["Data"]["BillOfLadings"][0]
    except (KeyError, IndexError, TypeError):
        return None

    report = Report(reference=bl.get("BillOfLadingNumber", ""), carrier="MSC", ok=True)
    report.port = (bl.get("GeneralTrackingInfo") or {}).get("PortOfDischarge") or port

    for info in bl.get("ContainersInfo") or []:
        item = ContainerReport(number=info.get("ContainerNumber") or "?")
        item.eta = parse_date(info.get("PodEtaDate"))

        best_arrival: datetime | None = None
        arrival_detail: list | None = None
        latest: datetime | None = None
        for event in info.get("Events") or []:
            when = parse_date(event.get("Date"))
            desc = event.get("Description") or ""
            place = event.get("Location") or ""

            if when and (latest is None or when > latest):
                latest = when
                item.last_event = desc
                item.last_event_at = when

            if when and ARRIVAL_WORDS.search(desc) and _port_matches(place, report.port):
                if best_arrival is None or when < best_arrival:
                    best_arrival = when
                    arrival_detail = event.get("Detail") or []

            # Buque/viaje del tramo oceánico. Detail = ["MSC LAUSANNE VI", "QM639R"].
            detail = event.get("Detail") or []
            if (len(detail) >= 1 and isinstance(detail[0], str)
                    and not detail[0].isdigit() and OCEAN_WORDS.search(desc)):
                item.vessel = detail[0]
                if len(detail) >= 2 and isinstance(detail[1], str):
                    item.voyage = detail[1]

        # El evento de llegada manda: mismo buque y el viaje correcto.
        if arrival_detail:
            if isinstance(arrival_detail[0], str) and not arrival_detail[0].isdigit():
                item.vessel = arrival_detail[0]
            if len(arrival_detail) >= 2 and isinstance(arrival_detail[1], str):
                item.voyage = arrival_detail[1]

        if best_arrival:
            item.arrived = True
            item.arrival_date = best_arrival
        elif item.eta:
            item.eta_note = "ETA del puerto"
        else:
            item.eta_note = "sin ETA publicada"

        report.containers.append(item)
    return report


def extract_maersk(payload: dict, port: str) -> Report | None:
    containers = payload.get("containers")
    if not isinstance(containers, list) or not containers:
        return None

    report = Report(reference=str(payload.get("tpdoc_num") or ""), carrier="MAERSK", ok=True)
    # Maersk no expone el puerto de descarga como campo: el `destination` es la
    # entrega final (MEXICO CITY en esta guía). Se deriva del evento de descarga.
    matched_port = ""

    for raw in containers:
        item = ContainerReport(number=raw.get("container_num") or "?")
        item.eta_note = "Maersk no publica ETA de puerto (solo entrega final)"

        best_arrival: datetime | None = None
        latest: datetime | None = None
        for loc in raw.get("locations") or []:
            city = loc.get("city") or ""
            code = loc.get("location_code") or ""
            for event in loc.get("events") or []:
                when = parse_date(event.get("event_time"))
                activity = event.get("activity") or ""
                if when and (latest is None or when > latest):
                    latest = when
                    item.last_event = f"{activity} · {city}"
                    item.last_event_at = when

                if when and ARRIVAL_WORDS.search(activity) and (
                    _port_matches(city, port) or _port_matches(code, "MXZLO")
                ):
                    if best_arrival is None or when < best_arrival:
                        best_arrival = when
                        matched_port = f"{city} ({code})" if code else city

                if event.get("vessel_name"):
                    item.vessel = event["vessel_name"]
                    item.voyage = event.get("voyage_num") or ""

        if best_arrival:
            item.arrived = True
            item.arrival_date = best_arrival
        report.containers.append(item)

    report.port = matched_port or port
    return report


# --------------------------------------------------------------------------- #
# Navegación
# --------------------------------------------------------------------------- #
IGNORE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".woff",
              ".woff2", ".ttf", ".otf", ".css", ".map", ".mp4", ".webm")


async def _dismiss_consent(page) -> None:
    """Cierra banners de cookies/overlays que tapan el campo de búsqueda."""
    selectors = (
        "#onetrust-accept-btn-handler",
        'button[id*="accept" i]',
        'button:has-text("Accept all")',
        'button:has-text("Aceptar todo")',
        'button:has-text("I accept")',
        "#onetrust-close-btn-container button",
    )
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=2500)
                await page.wait_for_timeout(900)
                break
        except Exception:
            continue
    # Red de seguridad: quitar el contenedor de OneTrust del DOM.
    try:
        await page.evaluate(
            """() => {
                for (const id of ['onetrust-consent-sdk', 'onetrust-banner-sdk',
                                  'onetrust-pc-sdk']) {
                    const el = document.getElementById(id);
                    if (el) el.remove();
                }
            }"""
        )
    except Exception:
        pass


async def _show_banner(page) -> None:
    """Quita un `window.confirm` que bloquea el hilo de la página."""
    try:
        page.on("dialog", lambda d: asyncio.create_task(d.accept()))
    except Exception:
        pass


class Collector:
    """Guarda los JSON de la naviera y decide si ya tiene el de tracking."""

    def __init__(self, carrier: str):
        self.carrier = carrier
        self.hits: list[tuple[str, dict]] = []
        self.others: list[tuple[str, dict]] = []
        self.tasks: set[asyncio.Task] = set()

    def attach(self, page) -> None:
        def schedule(response):
            task = asyncio.create_task(self._handle(response))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

        page.on("response", schedule)

    async def _handle(self, response) -> None:
        try:
            url = response.url
            base = url.lower().split("?")[0]
            if any(base.endswith(ext) for ext in IGNORE_EXT):
                return
            ctype = (response.headers or {}).get("content-type", "")
            if "text/html" in ctype.lower():
                return
            text = await response.text()
            if not text or text.lstrip()[:1] not in "[{":
                return
            body = json.loads(text)
            if self._is_tracking(url, body):
                self.hits.append((url, body))
            else:
                self.others.append((url, body))
        except Exception:
            pass

    def _is_tracking(self, url: str, body: Any) -> bool:
        """Solo cuenta un payload que el extractor sepa interpretar.

        Aceptar "cualquier JSON de la URL de tracking" hacía que una respuesta
        de error de MSC (`IsSuccess: false`, sin `BillOfLadings`) contara como
        éxito y luego fallara al extraer, perdiendo el reintento.
        """
        if not isinstance(body, dict):
            return False
        if self.carrier == "MSC":
            try:
                return bool(body["Data"]["BillOfLadings"][0]["ContainersInfo"])
            except (KeyError, IndexError, TypeError):
                return False
        if self.carrier == "MAERSK":
            return isinstance(body.get("containers"), list) and bool(body.get("containers"))
        if self.carrier == "HAPAG_LLOYD":
            text = json.dumps(body)[:4000].lower()
            return any(key in text for key in ("container", "shipment", "bill of lading"))
        return False

    def best(self) -> tuple[str, dict] | None:
        return self.hits[0] if self.hits else None

    def failure_hint(self) -> str:
        """Si la naviera respondió con un error explícito, lo describe."""
        for url, body in self.others:
            if self.carrier != "MSC" or "TrackingInfo" not in url:
                continue
            msg = body.get("Message") or body.get("message")
            success = body.get("IsSuccess")
            if msg or success is False:
                return f"MSC respondió IsSuccess={success} Message={msg!r}"
        return ""

    async def settle(self) -> None:
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)


BLOCK_MARKERS = re.compile(
    r"(access denied|just a moment|verify you are human|unusual traffic"
    r"|checking your browser|security check|interactive challenge|attention required"
    r"|your browser was tested against security)",
    re.I,
)


async def _check_blocked(page) -> str | None:
    try:
        title = await page.title() or ""
    except Exception:
        title = ""
    if BLOCK_MARKERS.search(title):
        return title.strip()[:100]
    try:
        text = await page.evaluate(
            "() => document.body ? document.body.innerText.slice(0,1500) : ''")
    except Exception:
        text = ""
    if text and BLOCK_MARKERS.search(text):
        flat = " ".join(text.split())
        # El detalle del challenge trae la IP: sirve para distinguir un bloqueo
        # por reputación de IP (cambia con WARP) de uno por fingerprint.
        ip = re.search(r"IP:\s*([\d.]+)", flat)
        ray = re.search(r"RayID:\s*(\w+)", flat)
        extra = ""
        if ip:
            extra += f" [IP {ip.group(1)}"
            extra += f", Ray {ray.group(1)}]" if ray else "]"
        return flat[:140] + extra
    return None


@asynccontextmanager
async def _browser(launch: dict):
    """Abre Camoufox traduciendo un fallo de proxy a un mensaje accionable."""
    try:
        async with AsyncCamoufox(**launch) as browser:
            yield browser
    except Exception as exc:
        if launch.get("proxy"):
            raise SystemExit(
                f"\n  No se pudo abrir el navegador con proxy "
                f"{launch['proxy'].get('server')}: {type(exc).__name__}\n"
                f"  Comprueba que WARP esté conectado y escuchando en ese puerto.\n"
                f"  (warp-cli --accept-tos mode proxy && warp-cli --accept-tos connect)\n"
            ) from exc
        raise


async def _fill_msc_form(page, reference: str) -> None:
    """Rellena el buscador de MSC y dispara el XHR.

    El campo está a veces tapado por el banner de cookies, así que el `click`
    normal expira. Se intenta click, luego foco por JS, y por último `fill`.
    """
    field = page.locator("#trackingNumber").first
    await field.wait_for(state="visible", timeout=30_000)

    for attempt in range(3):
        try:
            await field.click(timeout=8_000)
            break
        except Exception:
            if attempt == 0:
                await _dismiss_consent(page)
            elif attempt == 1:
                try:
                    await field.scroll_into_view_if_needed(timeout=5_000)
                except Exception:
                    pass
            await page.wait_for_timeout(1000)
    else:
        # Último recurso: foco directo por JS, que no pasa por hit-testing.
        await field.evaluate("el => el.focus()")

    try:
        await field.fill("")
        await field.type(reference, delay=90)
    except Exception:
        await field.evaluate(
            """(el, v) => {
                el.value = v;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            reference,
        )
    await field.press("Enter")


async def query(browser, reference: str, carrier: str, port: str,
                wait_ms: int, headed_note: str) -> Report:
    report = Report(reference=reference, carrier=carrier)
    collector = Collector(carrier)
    page = await browser.new_page()
    page.set_default_timeout(60_000)

    try:
        collector.attach(page)
        await _show_banner(page)

        if carrier == "MSC":
            await page.goto("https://www.msc.com/en/track-a-shipment",
                            wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
            await _dismiss_consent(page)
            await _fill_msc_form(page, reference)
        elif carrier == "MAERSK":
            await page.goto(f"https://www.maersk.com/tracking/{reference}",
                            wait_until="domcontentloaded")
            await _dismiss_consent(page)
        elif carrier == "HAPAG_LLOYD":
            await page.goto(
                "https://www.hapag-lloyd.com/en/online-business/track/"
                f"track-by-booking-solution.html?blno={reference}",
                wait_until="domcontentloaded")
            await _dismiss_consent(page)
        else:
            report.message = "Prefijo de B/L no reconocido"
            return report

        # Espera activa hasta que aparezca el payload de tracking.
        for _ in range(max(wait_ms // 500, 1)):
            await asyncio.sleep(0.5)
            if collector.best():
                break

        await collector.settle()

        hit = collector.best()
        if hit:
            url, body = hit
            extractor = {"MSC": extract_msc, "MAERSK": extract_maersk}.get(carrier)
            if extractor is None:
                report.message = "Payload capturado pero sin extractor para esta naviera"
                report.source_url = url
            else:
                parsed = extractor(body, port)
                if parsed is None:
                    report.message = "El JSON no tiene la forma esperada"
                    report.source_url = url
                else:
                    parsed.source_url = url
                    parsed.reference = parsed.reference or reference
                    parsed.raw = body
                    report = parsed
        else:
            reason = await _check_blocked(page)
            if reason:
                report.blocked = True
                report.message = f"BLOQUEADO: {reason}"
            else:
                hint = collector.failure_hint()
                if hint:
                    report.message = f"Sin tracking: {hint}"
                else:
                    report.message = ("Sin payload de tracking. "
                                      f"{headed_note}")
    except Exception as exc:
        report.message = f"{type(exc).__name__}: {str(exc)[:160]}"
    finally:
        try:
            await page.close()
        except Exception:
            pass
    return report


# --------------------------------------------------------------------------- #
# Salida
# --------------------------------------------------------------------------- #
def print_report(report: Report) -> None:
    print("\n" + "=" * 74)
    print(f"B/L {report.reference}  ·  {report.carrier}")
    print("=" * 74)

    if report.blocked:
        print(f"  RESULTADO: {report.message}")
        print("  -> Sin WARP el sitio devuelve challenge. Es el bloqueo que sufres.")
        return
    if not report.ok:
        print(f"  RESULTADO: {report.message or 'sin datos'}")
        if report.source_url:
            print(f"  fuente: {report.source_url}")
        return

    print(f"  puerto de descarga: {report.port or '—'}")
    print(f"  contenedores: {len(report.containers)}")
    print(f"  fuente: {report.source_url}")
    print()
    print(f"  {'CONTENEDOR':<13} {'¿LLEGÓ?':<9} {'LLEGADA':<11} {'ETA':<11} BUQUE")
    print(f"  {'-'*13} {'-'*9} {'-'*11} {'-'*11} {'-'*22}")

    arrived = 0
    for item in report.containers:
        if item.arrived:
            arrived += 1
        mark = "SÍ" if item.arrived else "NO"
        eta = fmt(item.eta) if item.eta else "—"
        vessel = item.vessel or "—"
        if item.voyage:
            vessel = f"{vessel} {item.voyage}"
        print(f"  {item.number:<13} {mark:<9} {fmt(item.arrival_date):<11} "
              f"{eta:<11} {vessel[:30]}")

    print()
    print(f"  RESUMEN: {arrived}/{len(report.containers)} contenedores llegaron a "
          f"{report.port or DEFAULT_PORT}")
    first = next((c for c in report.containers if c.arrived), None)
    if first:
        print(f"  El primero: {first.number} llegó el {fmt(first.arrival_date)}")
    else:
        print("  Ninguno ha llegado todavía.")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Consulta B/L y detecta llegada a puerto.")
    parser.add_argument("references", nargs="+", help="Uno o más B/L")
    parser.add_argument("--port", default=DEFAULT_PORT, help="Puerto a comprobar")
    parser.add_argument("--wait", type=int, default=25, help="Segundos máximos de espera")
    parser.add_argument("--headed", action="store_true", help="Navegador visible")
    parser.add_argument("--dump", default="", help="Carpeta donde guardar el JSON crudo")
    parser.add_argument(
        "--proxy", default="",
        help="Proxy SOCKS5/HTTP para la sesión, p. ej. socks5://127.0.0.1:40000 (WARP). "
             "Necesario para Hapag-Lloyd: bloquea por reputación de IP, no por fingerprint.")
    args = parser.parse_args()

    if args.dump:
        Path(args.dump).mkdir(parents=True, exist_ok=True)

    # Asignación previa para reportar B/L desconocidos sin abrir navegador.
    pending: list[tuple[str, str]] = []
    for raw in args.references:
        carrier, number = classify(raw)
        if carrier == "UNKNOWN":
            print(f"\n  {raw}: no parece un B/L de MSC, Maersk ni Hapag-Lloyd")
        else:
            pending.append((carrier, number))

    if not pending:
        return 2

    note = "Revisa con --headed para ver qué carga la página."

    # Camoufox 0.5.x inyecta el fingerprint en el lanzamiento: el proxy debe ir
    # en las opciones del navegador, no por contexto.
    launch: dict[str, Any] = dict(headless=not args.headed, geoip=True, locale="es-PE",
                                  os="windows", humanize=True, window=(1440, 900))
    if args.proxy:
        launch["proxy"] = {"server": args.proxy}
        launch["geoip"] = True

    async with _browser(launch) as browser:
        for carrier, number in pending:
            report = await query(browser, number, carrier, args.port, args.wait * 1000, note)
            print_report(report)
            if args.dump and report.raw:
                target = Path(args.dump) / f"{carrier.lower()}-{number}.json"
                target.write_text(json.dumps(report.raw, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
                print(f"  payload crudo: {target}")
            await asyncio.sleep(2)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
