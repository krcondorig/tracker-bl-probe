"""Sesión de navegador Camoufox con intercepción de XHR/Fetch.

Estrategia anti-bloqueo en capas:

1. **IP**: WARP en modo proxy (SOCKS5 en 127.0.0.1:40000) ya está activo antes de
   arrancar el proceso. Camoufox no negocia TLS con la IP del runner de GitHub.
2. **Fingerprint**: Camoufox parchea Firefox a nivel de binario (no es un
   playwright-stealth en JS). `geoip=True` alinea zona horaria e idioma del
   navegador con la IP de salida; `locale` fija el Accept-Language.
3. **Comportamiento**: `humanize=True` introduce cursor y tecleo con jitter.
4. **Datos**: no se raspa el DOM salvo como respaldo. Se escucha
   `page.on("response")` y se guardan los cuerpos JSON del host de la naviera.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from camoufox.async_api import AsyncCamoufox
from playwright.async_api import Page, Response, TimeoutError as PlaywrightTimeout

from .carriers import (
    BLOCK_MARKERS,
    CarrierAdapter,
    adapter_for,
    build_deep_link,
    split_candidates,
)
from .config import Settings
from .hapag import (
    is_real_detail,
    listing_url,
    normalize_container,
    parse_movements,
    rows_from_html,
    to_payload,
)
from .models import Carrier

log = logging.getLogger(__name__)

# Respuestas mayores se ignoran: los payloads de tracking reales pesan KB.
MAX_BODY_BYTES = 4 * 1024 * 1024
# Extensiones que jamás contienen tracking.
IGNORED_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".woff", ".woff2",
    ".ttf", ".otf", ".eot", ".mp4", ".webm", ".css", ".map",
)

CONSENT_SELECTORS = (
    "#onetrust-accept-btn-handler",
    'button[id*="accept" i]',
    'button[class*="accept" i]',
    'button:has-text("Accept all")',
    'button:has-text("Aceptar todo")',
    'button:has-text("I agree")',
    'button:has-text("Acepto")',
    '[data-testid*="accept" i]',
)

CONTAINER_IN_PAYLOAD = re.compile(r"[A-Z]{4}\d{7}")


@dataclass
class CapturedResponse:
    url: str
    status: int
    content_type: str
    body: Any
    carrier: Carrier
    priority: int = 0
    has_events: bool = False
    matched_container: bool = False


@dataclass
class CaptureResult:
    payload: CapturedResponse | None
    blocked: bool = False
    block_reason: str | None = None
    page_title: str | None = None
    responses_seen: int = 0
    candidate_urls: list[str] = field(default_factory=list)


def _is_ignored_url(url: str) -> bool:
    lowered = url.lower()
    return any(lowered.split("?")[0].endswith(ext) for ext in IGNORED_EXTENSIONS)


def _count_event_shapes(node: Any, depth: int = 0) -> int:
    """Heurística rápida: cuántos dicts tienen a la vez algo tipo estado y fecha."""
    if depth > 10 or node is None:
        return 0
    hits = 0
    if isinstance(node, dict):
        keys = {re.sub(r"[^a-z0-9]", "", str(k).lower()) for k in node.keys()}
        has_status = any(
            k in keys for k in ("status", "statusname", "eventname", "eventtype", "movename", "description")
        )
        has_time = any(
            k in keys for k in ("date", "time", "timestamp", "eventtime", "eventdate", "eventdatetime", "datetime")
        )
        if has_status and has_time:
            hits += 1
        for value in node.values():
            hits += _count_event_shapes(value, depth + 1)
    elif isinstance(node, list):
        for item in node[:200]:
            hits += _count_event_shapes(item, depth + 1)
    return hits


class BrowserSession:
    """Envoltura de AsyncCamoufox con vida útil por ejecución completa.

    Un único navegador para todas las consultas: arrancar Firefox cuesta ~5 s y
    reutilizar cookies de sesión ayuda a no parecer un bot que abre una sesión
    nueva por contenedor. Cada consulta usa una pestaña nueva.

    Nota sobre identidad: en Camoufox 0.5.x el fingerprint (UA, screen, WebGL,
    fuentes, timezone, IP de WebRTC) se inyecta al lanzar el navegador, así que
    un solo lanzamiento + `browser.new_page()` es suficiente. Crear un
    `new_context()` a mano pisaría ese fingerprint y además entra en conflicto
    con la ventana spoofeada (Playwright fija un viewport distinto al que el
    navegador reporta, y Juggler se bloquea).
    """

    def __init__(self, settings: Settings, proxy_url: str | None = None):
        self._settings = settings
        # Proxy de esta sesion. Se pasa explicitamente porque el proxy se fija
        # al lanzar el navegador: no se puede cambiar por pestaña, asi que las
        # navieras que van directas y las que van por WARP necesitan sesiones
        # distintas. `None` fuerza salida directa; si se omite, se usa el global.
        self._proxy_url = proxy_url
        self._camoufox: AsyncCamoufox | None = None
        self._browser = None

    async def __aenter__(self) -> "BrowserSession":
        options: dict[str, Any] = {
            "headless": self._settings.headless,
            "humanize": self._settings.humanize,
            "locale": self._settings.locale,
            "geoip": True,
            # Perfil Windows: es el SO más común en Perú y evita el fingerprint
            # "Linux headless" que Akamai puntúa peor.
            "os": "windows",
            "window": (1440, 900),
            "firefox_user_prefs": {
                # Sin telemetría ni peticiones de fondo que ensucien la captura.
                "toolkit.telemetry.enabled": False,
                "app.shield.optoutstudies.enabled": False,
                "network.http.speculative-parallel-limit": 0,
                "browser.safebrowsing.malware.enabled": False,
                "browser.safebrowsing.phishing.enabled": False,
                # Sin precarga de DNS: evita resolver dominios que luego no se visitan.
                "network.dns.disablePrefetch": True,
                "network.prefetch-next": False,
            },
        }
        if self._proxy_url is not None:
            options["proxy"] = {"server": self._proxy_url}
            log.info("Navegador saldrá por el proxy %s", self._proxy_url)
        else:
            log.info("Navegador saldrá directo (sin proxy)")

        try:
            self._camoufox = AsyncCamoufox(**options)
            self._browser = await self._camoufox.__aenter__()
            log.info(
                "Camoufox listo (headless=%s, locale=%s, geoip=on)",
                self._settings.headless, self._settings.locale,
            )
        except Exception:
            await self.aclose()
            raise
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._camoufox is not None:
            try:
                await self._camoufox.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            self._camoufox = None
            self._browser = None

    # ------------------------------------------------------------------ utils
    async def _new_page(self) -> Page:
        if self._browser is None:
            raise RuntimeError("BrowserSession no está inicializada")
        page = await self._browser.new_page()
        page.set_default_timeout(self._settings.nav_timeout_ms)
        page.set_default_navigation_timeout(self._settings.nav_timeout_ms)
        return page

    async def _detect_block(self, page: Page) -> tuple[bool, str | None]:
        try:
            title = (await page.title()) or ""
        except Exception:  # noqa: BLE001 - la página pudo cerrarse
            title = ""
        if BLOCK_MARKERS.search(title):
            return True, f"Challenge en <title>: {title.strip()[:120]}"
        try:
            snippet = await page.evaluate(
                "() => (document.body ? document.body.innerText.slice(0, 3000) : '')"
            )
        except Exception:  # noqa: BLE001
            snippet = ""
        if snippet and BLOCK_MARKERS.search(snippet):
            return True, f"Challenge en el cuerpo: {snippet.strip()[:120]}"
        return False, None

    async def _dismiss_consent(self, page: Page) -> None:
        for selector in CONSENT_SELECTORS:
            try:
                locator = page.locator(selector).first
                if await locator.count() and await locator.is_visible():
                    await locator.click(timeout=2500)
                    await page.wait_for_timeout(600)
                    log.debug("Banner de consentimiento cerrado con %s", selector)
                    return
            except Exception:  # noqa: BLE001 - el banner es opcional
                continue

    async def warm_up(self, carrier: Carrier) -> None:
        """Visita la home para obtener cookies de sesión antes de pedir tracking."""
        adapter = adapter_for(carrier)
        if adapter is None:
            return
        page = await self._new_page()
        try:
            await page.goto(adapter.home_url, wait_until="domcontentloaded")
            await self._dismiss_consent(page)
            await page.wait_for_timeout(2500)
        except PlaywrightTimeout:
            log.debug("Warm-up de %s agotó el tiempo (se continúa)", adapter.display_name)
        except Exception as exc:  # noqa: BLE001
            log.debug("Warm-up de %s falló: %s", adapter.display_name, exc)
        finally:
            await page.close()

    # ------------------------------------------------------- captura principal
    async def capture_container(self, carrier: Carrier, container: str) -> CaptureResult:
        """Consulta el contenedor y devuelve el payload que entienda `extract`.

        MSC y Maersk exponen JSON: se navega al deep link y se intercepta la
        respuesta. Hapag no: su detalle es una tabla HTML detrás de un formulario
        JSF, así que va por su propio camino y se convierte al mismo esquema.
        """
        adapter = adapter_for(carrier)
        if adapter is None:
            return CaptureResult(payload=None, block_reason=f"Sin adaptador para {carrier}")

        if carrier is Carrier.HAPAG_LLOYD:
            return await self._capture_hapag(container)

        page = await self._new_page()
        captured: list[CapturedResponse] = []
        seen_urls: set[str] = set()
        pending: set[asyncio.Task] = set()

        async def handle_response(response: Response) -> None:
            try:
                url = response.url
                if _is_ignored_url(url) or not adapter.is_candidate(url):
                    return
                headers = response.headers or {}
                content_type = headers.get("content-type", "")
                if "text/html" in content_type.lower() and not any(
                    hint in url.lower() for hint in adapter.path_hints
                ):
                    return
                length = headers.get("content-length")
                if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
                    return

                text = await response.text()
                if not text or len(text) > MAX_BODY_BYTES:
                    return
                stripped = text.lstrip()
                if not stripped or stripped[0] not in "[{":
                    return
                try:
                    body = json.loads(text)
                except json.JSONDecodeError:
                    return

                _, path = split_candidates(url)
                priority = 0
                if any(hint in path for hint in adapter.path_hints):
                    priority += 2
                if container.lower() in url.lower() or container.lower() in text[:4000].lower():
                    priority += 3

                events = _count_event_shapes(body)
                record = CapturedResponse(
                    url=url,
                    status=response.status,
                    content_type=content_type,
                    body=body,
                    carrier=carrier,
                    priority=priority + min(events, 25),
                    has_events=events > 0,
                    matched_container=container.lower() in text[:20000].lower(),
                )
                if url in seen_urls and not record.has_events:
                    return
                seen_urls.add(url)
                captured.append(record)
                log.debug(
                    "[%s] JSON capturado %s (eventos=%d, prio=%d, %d bytes)",
                    container, url.split("?")[0][:110], events, record.priority, len(text),
                )
            except Exception as exc:  # noqa: BLE001 - una respuesta ilegible no aborta la consulta
                log.debug("Respuesta descartada (%s): %s", carrier, exc)

        def schedule_handler(response: Response) -> None:
            task = asyncio.create_task(handle_response(response))
            pending.add(task)
            task.add_done_callback(pending.discard)

        page.on("response", schedule_handler)

        blocked = False
        block_reason: str | None = None
        title: str | None = None

        try:
            try:
                await page.goto(build_deep_link(carrier, container), wait_until="domcontentloaded")
            except PlaywrightTimeout:
                log.warning("[%s] Timeout de navegación (%s)", container, carrier.value)
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] Fallo de navegación: %s", container, exc)

            await self._dismiss_consent(page)
            await page.wait_for_timeout(adapter.post_load_wait_ms)

            blocked, block_reason = await self._detect_block(page)
            if blocked:
                log.warning("[%s] Bloqueo detectado: %s", container, block_reason)
                return CaptureResult(
                    payload=None,
                    blocked=True,
                    block_reason=block_reason,
                    page_title=await self._safe_title(page),
                    responses_seen=len(captured),
                    candidate_urls=[c.url for c in captured][:5],
                )

            # Espera activa: en cuanto hay un payload con eventos y el contenedor
            # aparece en él, se puede cortar la espera.
            deadline = asyncio.get_running_loop().time() + self._settings.wait_seconds
            while asyncio.get_running_loop().time() < deadline:
                if await self._best_so_far(captured, container):
                    break
                await asyncio.sleep(0.5)

            if not captured:
                # Respaldo: rellenar el formulario cuando el deep link no dispara XHR.
                await self._fallback_form(page, adapter, container)
                deadline = asyncio.get_running_loop().time() + min(self._settings.wait_seconds, 15)
                while asyncio.get_running_loop().time() < deadline:
                    if await self._best_so_far(captured, container):
                        break
                    await asyncio.sleep(0.5)

            title = await self._safe_title(page)
            if not captured and not blocked:
                blocked, block_reason = await self._detect_block(page)
        finally:
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            page.remove_listener("response", schedule_handler)
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass

        best = await self._best_so_far(captured, container)
        return CaptureResult(
            payload=best,
            blocked=blocked,
            block_reason=block_reason,
            page_title=title,
            responses_seen=len(captured),
            candidate_urls=[c.url for c in captured][:5],
        )

    # ------------------------------------------------------------- Hapag (HTML)
    async def _capture_hapag(self, container: str) -> CaptureResult:
        """Listado legacy + clic real en `Details` + tabla de movimientos.

        No se compone la URL del detalle porque no existe: `Details` es un
        `type="button"` que la app legacy (ExtJS) maneja enviando un POST JSF
        con el `javax.faces.ViewState` de la sesión. Armar
        `?view=S8510&container=...` a mano devuelve la shell de la SPA nueva,
        que no tiene la tabla. Por eso se marca el radio y se pulsa el botón.

        El detalle es HTML, no JSON: se lee `innerText` y lo parsea
        `hapag.parse_movements`. El resultado se convierte al esquema anidado
        para que la detección de llegada siga viviendo en `extract`.
        """
        page = await self._new_page()
        wanted = normalize_container(container)
        try:
            listing = listing_url(container)
            try:
                await page.goto(listing, wait_until="domcontentloaded")
            except PlaywrightTimeout:
                return CaptureResult(payload=None, block_reason="Timeout en el listado de Hapag")
            except Exception as exc:  # noqa: BLE001
                return CaptureResult(payload=None, block_reason=f"Fallo al abrir el listado: {exc}")

            await self._dismiss_consent(page)

            blocked, reason = await self._detect_block(page)
            if blocked:
                return CaptureResult(
                    payload=None, blocked=True, block_reason=reason,
                    page_title=await self._safe_title(page),
                )

            # El listado lo pinta ExtJS: se espera a que haya radios.
            rows: list[dict[str, Any]] = []
            deadline = asyncio.get_running_loop().time() + self._settings.wait_seconds
            while asyncio.get_running_loop().time() < deadline:
                rows = rows_from_html(await page.content())
                if rows:
                    break
                await page.wait_for_timeout(1000)

            if not rows:
                blocked, reason = await self._detect_block(page)
                if blocked:
                    return CaptureResult(
                        payload=None, blocked=True, block_reason=reason,
                        page_title=await self._safe_title(page),
                    )
                # Sin filas y sin challenge: la naviera no conoce el contenedor,
                # o el listado por contenedor no es esta página.
                return CaptureResult(
                    payload=None,
                    block_reason="El listado de Hapag no trajo contenedores",
                    page_title=await self._safe_title(page),
                    candidate_urls=[page.url],
                    responses_seen=1,
                )

            # Preferir la fila del contenedor pedido; si no aparece, el listado
            # se consultó por B/L y hay que quedarse con la primera.
            index = next(
                (i for i, row in enumerate(rows) if row["container"] == wanted), 0
            )

            text = await self._open_hapag_detail(page, listing, index)
            if text is None:
                return CaptureResult(
                    payload=None,
                    block_reason="No se pudo abrir el detalle de Hapag",
                    page_title=await self._safe_title(page),
                    candidate_urls=[page.url],
                    responses_seen=1,
                )

            if not is_real_detail(text):
                return CaptureResult(
                    payload=None,
                    block_reason="El detalle de Hapag no tenía tabla de movimientos",
                    page_title=await self._safe_title(page),
                    candidate_urls=[page.url],
                    responses_seen=1,
                )

            events = parse_movements(text)
            body = to_payload(wanted, events)
            if body is None:
                return CaptureResult(
                    payload=None,
                    block_reason="Movimientos de Hapag ilegibles",
                    page_title=await self._safe_title(page),
                    responses_seen=1,
                )

            number = next((row["container"] for row in rows), wanted)
            record = CapturedResponse(
                url=page.url,
                status=200,
                content_type="text/html",
                body=body,
                carrier=Carrier.HAPAG_LLOYD,
                priority=100,
                has_events=bool(events),
                matched_container=True,
            )
            log.info("[%s] Hapag: %d movimientos del contenedor %s",
                     container, len(events), number)
            return CaptureResult(
                payload=record,
                page_title=await self._safe_title(page),
                responses_seen=1,
                candidate_urls=[page.url],
            )
        finally:
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass

    async def _open_hapag_detail(self, page: Page, listing: str, index: int) -> str | None:
        """Vuelve al listado, marca el radio `index` y pulsa `Details`.

        Se vuelve al listado en cada intento: tras abrir un detalle la URL sigue
        conteniendo el nombre legacy, así que comprobar la URL no sirve para
        saber si se está en el formulario, y sin los radios el clic falla.
        """
        try:
            await page.goto(listing, wait_until="domcontentloaded")
            await page.wait_for_timeout(2500)
            await self._dismiss_consent(page)

            picked = await page.evaluate(
                """(idx) => {
                    const radios = Array.from(document.querySelectorAll(
                        'input[type=radio][name]'));
                    const r = radios[idx];
                    if (!r) return null;
                    r.click();
                    const label = document.querySelector(`label[for="${r.id}"]`);
                    if (label) label.click();
                    return {id: r.id, checked: r.checked};
                }""",
                index,
            )
            if not picked:
                return None

            try:
                async with page.expect_navigation(timeout=45_000):
                    await page.evaluate(
                        """() => {
                            const b = document.querySelector('button[value="Details"]');
                            if (b) { b.click(); return true; }
                            return false;
                        }"""
                    )
            except Exception:  # noqa: BLE001 - puede pintar sin navegar
                await page.wait_for_timeout(6000)

            await page.wait_for_timeout(3500)
            blocked, _ = await self._detect_block(page)
            if blocked:
                return None
            return await page.evaluate(
                "() => (document.body ? document.body.innerText : '')"
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("No se pudo abrir el detalle de Hapag: %s", exc)
            return None

    @staticmethod
    async def _safe_title(page: Page) -> str | None:
        try:
            return (await page.title()) or None
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    async def _best_so_far(captured: list[CapturedResponse], container: str) -> CapturedResponse | None:
        if not captured:
            return None
        ranked = sorted(captured, key=lambda c: (c.has_events, c.matched_container, c.priority), reverse=True)
        best = ranked[0]
        if best.has_events:
            return best
        # Sin eventos, solo vale un payload que mencione el contenedor (p. ej. un 404 semántico).
        if best.matched_container:
            return best
        return None

    async def _fallback_form(self, page: Page, adapter: CarrierAdapter, container: str) -> None:
        """Relleno del formulario de búsqueda como segundo intento."""
        if not adapter.needs_form_interaction:
            return
        for selector in adapter.input_selectors:
            try:
                field = page.locator(selector).first
                if not await field.count() or not await field.is_visible():
                    continue
                await field.click(timeout=3000)
                await field.fill("")
                await field.type(container, delay=90)
                await page.wait_for_timeout(400)
                for submit in adapter.submit_selectors:
                    button = page.locator(submit).first
                    try:
                        if await button.count() and await button.is_visible():
                            await button.click(timeout=3000)
                            log.debug("Formulario enviado con %s", submit)
                            return
                    except Exception:  # noqa: BLE001
                        continue
                await field.press("Enter")
                log.debug("Formulario enviado con Enter en %s", selector)
                return
            except Exception:  # noqa: BLE001
                continue
        log.debug("No se encontró formulario usable para %s", adapter.display_name)


async def run_with_retry(
    session: BrowserSession,
    carrier: Carrier,
    container: str,
    attempts: int,
    on_retry: Callable[[int, CaptureResult], Any] | None = None,
) -> tuple[CaptureResult, int]:
    """Consulta con reintentos. Devuelve (último resultado, intentos usados)."""
    last = CaptureResult(payload=None, block_reason="Sin intentos ejecutados")
    for attempt in range(1, attempts + 1):
        try:
            last = await session.capture_container(carrier, container)
        except Exception as exc:  # noqa: BLE001 - nunca debe tumbar el lote
            log.warning("[%s] Error en intento %d/%d: %s", container, attempt, attempts, exc)
            last = CaptureResult(payload=None, block_reason=f"Excepción: {exc}")
        if last.payload is not None:
            return last, attempt
        if attempt < attempts:
            backoff = 5 * attempt
            log.info("[%s] Reintentando en %ss (%s)", container, backoff, last.block_reason or "sin payload")
            if on_retry is not None:
                maybe = on_retry(attempt, last)
                if asyncio.iscoroutine(maybe):
                    await maybe
            await asyncio.sleep(backoff)
    return last, attempts
