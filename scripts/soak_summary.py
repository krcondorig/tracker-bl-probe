"""Resumen del soak de WARP: estabilidad de la IP y resultado por naviera.

Lee `ip-monitor.log` (una muestra cada 30 s: epoch, ip, estado warp) y los
`soak-resultados-*.json` que deja el bucle del scraper.

Responde tres preguntas concretas:

1. ¿Cambia la IP de egreso durante un run largo? Si cambia, una sesión de
   navegador abierta puede morir a mitad de lote.
2. ¿Se cae el túnel en algún momento? (`unreachable`)
3. ¿Cuántos contenedores salieron OK por naviera, y con qué error los que no?

Temporal: se borra al decidir.
"""

from __future__ import annotations

import glob
import json
import os
from collections import Counter, defaultdict


def read_monitor(path: str) -> list[tuple[int, str, str]]:
    samples: list[tuple[int, str, str]] = []
    if not os.path.exists(path):
        return samples
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            parts = line.strip().split("\t")
            if len(parts) != 3:
                continue
            try:
                samples.append((int(parts[0]), parts[1], parts[2]))
            except ValueError:
                continue
    return samples


def summarize_ip(samples: list[tuple[int, str, str]]) -> None:
    print("== Estabilidad de la IP ==")
    if not samples:
        print("  sin muestras: el monitor no escribio nada")
        return

    first_ts, last_ts = samples[0][0], samples[-1][0]
    print(f"  muestras: {len(samples)} en {(last_ts - first_ts) / 60:.1f} min")

    ips = [ip for _, ip, _ in samples]
    distinct = Counter(ips)
    print(f"  IPs distintas: {len(distinct)}")
    for ip, count in distinct.most_common():
        pct = 100.0 * count / len(samples)
        print(f"    {ip:<20} {count:>4} muestras ({pct:.1f}%)")

    # Cambios de IP entre muestras consecutivas.
    switches = sum(1 for a, b in zip(ips, ips[1:]) if a != b)
    print(f"  cambios de IP: {switches}")

    # Huecos: si el monitor tarda mucho mas de 30s, el proceso se atascó.
    gaps = [(b[0] - a[0]) for a, b in zip(samples, samples[1:])]
    if gaps:
        print(f"  hueco maximo entre muestras: {max(gaps)} s")

    unreachable = sum(1 for _, ip, _ in samples if ip == "unreachable")
    print(f"  muestras con el tunel caido: {unreachable}")
    if unreachable:
        print("  AVISO: el tunel no estuvo disponible en algun momento")


def summarize_warp(samples: list[tuple[int, str, str]]) -> None:
    print()
    print("== Estado de warp-cli ==")
    states = Counter(state for _, _, state in samples)
    if not states:
        print("  sin datos")
        return
    for state, count in states.most_common():
        print(f"  {state:<40} {count:>4}")


def summarize_results(pattern: str) -> None:
    print()
    print("== Resultado por naviera (codigo real del tracker) ==")
    files = sorted(glob.glob(pattern))
    if not files:
        print("  sin archivos de resultados")
        return

    per_carrier: dict[str, Counter] = defaultdict(Counter)
    errors: dict[str, Counter] = defaultdict(Counter)
    duration_total = 0
    runs = 0

    for path in files:
        try:
            # BOM tolerado: un JSON editado a mano en Windows lo trae y no debe
            # romper el resumen.
            with open(path, encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  {os.path.basename(path)}: ilegible ({exc})")
            continue

        runs += 1
        duration_total += data.get("durationMs", 0)
        for item in data.get("results", []):
            carrier = item.get("carrier", "?")
            status = item.get("status", "?")
            per_carrier[carrier][status] += 1
            if status != "OK":
                code = item.get("errorCode") or "sin codigo"
                msg = (item.get("errorMessage") or "")[:70]
                errors[carrier][f"{code}: {msg}"] += 1

    print(f"  iteraciones: {runs}, tiempo medio {duration_total / max(runs, 1) / 1000:.1f}s")
    for carrier, counter in sorted(per_carrier.items()):
        total = sum(counter.values())
        ok = counter.get("OK", 0)
        pct = 100.0 * ok / total if total else 0
        detail = ", ".join(f"{k}={v}" for k, v in counter.most_common() if k != "OK")
        print(f"  {carrier:<14} OK {ok}/{total} ({pct:.0f}%){('  | ' + detail) if detail else ''}")

    if errors:
        print()
        print("  Errores vistos:")
        for carrier, counter in sorted(errors.items()):
            for msg, count in counter.most_common(3):
                print(f"    {carrier}: {msg} (x{count})")


def main() -> int:
    samples = read_monitor("ip-monitor.log")
    summarize_ip(samples)
    summarize_warp(samples)
    summarize_results("soak-resultados-*.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
