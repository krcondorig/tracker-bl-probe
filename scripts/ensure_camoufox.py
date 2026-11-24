"""Garantiza que el binario de Camoufox esté disponible para la sesión.

Existe por un fallo concreto en CI:

    Error: 403 Client Error: rate limit exceeded for url:
    https://api.github.com/repos/daijro/camoufox/releases/tags/v158.0.1-beta.41

`camoufox fetch` resuelve el tag consultando la API de GitHub, y los runners
comparten IP, así que ese límite se agota sin que el repo tenga nada que ver.
Cuando `actions/cache` ya dejó el binario en ~/.cache/camoufox, hacer fetch es
gastar una llamada de red que puede fallar por una razón ajena al proyecto.

Este script comprueba primero y solo descarga si falta. Con GITHUB_TOKEN en el
entorno, la descarga va autenticada y el rate limit deja de ser un problema.

Uso:
    python scripts/ensure_camoufox.py
    python scripts/ensure_camoufox.py --print-path   # imprime la ruta y sale
"""

from __future__ import annotations

import argparse
import sys
import time


def installed() -> dict[str, str] | None:
    """Devuelve la versión y ruta activas, o None si no hay binario usable."""
    try:
        from camoufox.multiversion import Version, get_active_path

        path = get_active_path()
        if not path:
            return None
        if not Version.from_path(path).is_supported():
            return None
    except Exception:
        return None

    version = "?"
    try:
        from camoufox.pkgman import installed_verstr

        version = installed_verstr()
    except Exception:
        pass
    return {"version": version, "path": str(path)}


def main() -> int:
    ap = argparse.ArgumentParser(description="Verifica (y si falta, descarga) Camoufox.")
    ap.add_argument("--print-path", action="store_true",
                    help="Solo imprime la ruta del binario activo.")
    args = ap.parse_args()

    found = installed()
    if found:
        if args.print_path:
            print(found["path"])
        else:
            print(f"Camoufox ya disponible: {found['version']} en {found['path']}")
            print("No se consulta la red.")
        return 0

    if args.print_path:
        return 1

    print("Camoufox no está instalado; ejecutando `camoufox fetch`.")
    # El rate limit de api.github.com es intermitente (los runners comparten
    # IP), así que un solo intento convierte un fallo pasajero en un job rojo.
    attempts = 3
    for attempt in range(1, attempts + 1):
        try:
            from camoufox.pkgman import CamoufoxFetcher

            CamoufoxFetcher().install()
            break
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            rate_limited = "rate limit" in detail.lower()
            print(f"Intento {attempt}/{attempts} falló: {detail}",
                  file=sys.stderr)
            if attempt == attempts:
                print("Si es un 403 de rate limit, exporta GITHUB_TOKEN para "
                      "autenticar la llamada a api.github.com.", file=sys.stderr)
                return 1
            if not rate_limited:
                # Un error que no es de rate limit no mejora reintentando.
                return 1
            wait = 20 * attempt
            print(f"Reintentando en {wait}s...", file=sys.stderr)
            time.sleep(wait)

    found = installed()
    if not found:
        print("La descarga terminó pero no se detecta un binario usable.",
              file=sys.stderr)
        return 1
    print(f"Instalado: {found['version']} en {found['path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
