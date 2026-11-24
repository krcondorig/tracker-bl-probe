"""Sonda 6 (definitiva): consulta por contenedor rellenando el formulario JSF.

Estructura real obtenida de la página:
  form  : #tracing_by_container_f  (POST a ...?_a=tracing_by_container)
  campo : #tracing_by_container_f:hl12
  botón : button[value="Find"]  ->  #tracing_by_container_f:hl25
  banner: #onetrust-accept-btn-handler (tapa el campo; hay que cerrarlo)

El clic normal expiraba porque un `div.category-menu-switch-handler` queda
encima del campo. Aquí se cierra el consentimiento, se escribe por JS sin
hit-testing y se pulsa `Find`. Se comprueba que el detalle sea el del
contenedor pedido y no el de otro envío.

Temporal: se borra al decidir.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys

from camoufox.async_api import AsyncCamoufox

TRACK = "https://www.hapag-lloyd.com/en/online-business/track/"
CONTAINER_PAGE = TRACK + "track-by-container-solution-legacy.html"

TARGET = "HLBU2959982"
OTHER = "FTAU1546275"

STATES = ("Gate out empty", "Gate in empty", "Gate out full", "Gate in full",
          "Arrival in", "Departure from", "Loaded", "Discharged",
          "Vessel departed", "Vessel arrived")


def movement_rows(text: str) -> list[str]:
    rows = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        head = next((s for s in STATES if stripped.startswith(s)), None)
        if head and re.search(r"\d{4}-\d{2}-\d{2}", stripped):
            rows.append(stripped)
    return rows


async def dismiss_consent(page) -> bool:
    for selector in ("#onetrust-accept-btn-handler", "#onetrust-reject-all-handler"):
        try:
            btn = page.locator(selector).first
            if await btn.count() and await btn.is_visible():
                await btn.click(timeout=5000)
                await page.wait_for_timeout(1500)
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


async def main() -> int:
    proxy = sys.argv[1] if len(sys.argv) > 1 else None
    options: dict = {"headless": True, "locale": "es-PE", "humanize": True}
    if proxy:
        options["proxy"] = {"server": proxy}

    info: dict = {"field_value_after_fill": None}
    async with AsyncCamoufox(**options) as browser:
        context = await browser.new_context(locale="es-PE")
        page = await context.new_page()
        try:
            await page.goto(CONTAINER_PAGE, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(6000)
            info["consent_closed"] = await dismiss_consent(page)
            await page.wait_for_timeout(1500)

            field_sel = "#tracing_by_container_f\\:hl12"
            await page.wait_for_selector(field_sel, state="attached", timeout=30_000)

            # Escribir por JS: el campo está tapado y el clic real expira.
            # No se pasa el valor por argumento: dentro de una arrow function
            # `arguments` no existe, así que se usa una función normal con
            # parámetro explícito.
            filled = await page.evaluate(
                """(value) => {
                    const el = document.querySelector('#tracing_by_container_f\\\\:hl12');
                    if (!el) return null;
                    el.value = '';
                    el.focus();
                    el.value = value;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    el.dispatchEvent(new Event('blur', {bubbles: true}));
                    return el.value;
                }""",
                TARGET,
            )
            info["field_value_after_fill"] = filled
            await page.wait_for_timeout(800)

            # Pulsar Find: es un type=button manejado por ExtJS/JSF.
            pressed = False
            try:
                async with page.expect_navigation(timeout=45_000):
                    await page.evaluate(
                        """() => {
                            const b = document.querySelector('button[value="Find"]');
                            if (!b) return false;
                            b.click();
                            return true;
                        }"""
                    )
                pressed = True
            except Exception:  # noqa: BLE001 - puede pintar sin navegar
                await page.wait_for_timeout(10_000)
                pressed = True
            info["find_pressed"] = pressed

            await page.wait_for_timeout(10_000)
            text = await page.evaluate("() => document.body ? document.body.innerText : ''")
            info["url_after"] = page.url
            info["shows_target"] = TARGET in (text or "")
            info["shows_other"] = OTHER in (text or "")
            info["rows"] = len(movement_rows(text or ""))
            info["sample_rows"] = movement_rows(text or "")[:6]
            info["text_len"] = len(text or "")
            if TARGET in (text or ""):
                pos = text.index(TARGET)
                info["context"] = text[max(0, pos - 150):pos + 120]
            # Guardar el texto para verificar el parser con datos reales.
            with open("hapag-detail.txt", "w", encoding="utf-8") as handle:
                handle.write(text or "")
        except Exception as exc:  # noqa: BLE001
            info["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                await page.close()
                await context.close()
            except Exception:  # noqa: BLE001
                pass

    print(json.dumps({k: v for k, v in info.items() if k != "context"}, indent=2,
                     ensure_ascii=False), flush=True)
    for row in info.get("sample_rows", []):
        print(f"  fila: {row}", flush=True)
    with open("hapag-form-probe.json", "w", encoding="utf-8") as handle:
        json.dump(info, handle, indent=2, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
