# Plan: cerrar los 4 huecos de producción

Orden acordado con el usuario: 2 → 3 → 1 → 4.

## 1. Carrier propagado desde el backend (hueco 2)

- `PendingContainersResponse.Item` ya trae `containerNumber` y `carrier`.
- `scraper.py` los tira: `split_csv(",".join(pending))` y re-adivina por prefijo.
- Necesario: `resolve_containers` devuelve pares (numero, carrier_del_backend).
- `parse()` acepta carrier sugerido: si el prefijo es UNKNOWN y hay carrier del
  backend, se usa ese.
- El prefijo sigue mandando cuando el backend no sabe.

## 2. "¿Llegó al puerto?" en TrackingData (hueco 3)

- `check_bl.py` tiene la lógica: evento de descarga/arribo en el puerto.
- `TrackingData` necesita `arrived: bool`, `arrival_date: str|None`,
  `discharge_port: str|None`.
- `ContainerTracking` (Java) y `TrackingView` también.
- El puerto se puede pedir por config (`SCRAPER_PORT`); por defecto el del
  evento de descarga.

## 3. Hapag-Lloyd en tracker/ (hueco 1)

- Portar de `check_bl.py`: listing legacy, radio, click Details, tabla HTML.
- `carriers.py` cambia el deep_link a la URL legacy.
- `browser.py` necesita un camino HTML, no solo JSON interceptado.

## 4. ETA correcta (hueco 4)

- Quitar `"eta"` de ETA_KEYS: es un campo trampa (Maersk lo usa para otra cosa).
- No reportar ETA cuando el contenedor ya llegó.

## Estado

| Hueco | Estado | Dónde |
| --- | --- | --- |
| 2. Carrier desde el backend | Hecho | `containers.parse(carrier_hint=)`, `carrier_candidates`, `Job.alternatives`, `httpclient.parse_pending_entries` |
| 3. ¿Llegó al puerto? | Hecho (Python + Java) | `extract.find_arrival`, `TrackingData.arrived`, `ContainerTracking.arrived` (monotónico), `TrackingView.arrived` |
| 1. Hapag-Lloyd en `tracker/` | Hecho (Python) | `tracker/hapag.py` + `browser._capture_hapag` / `_open_hapag_detail` |
| 4. ETA trampa | Hecho | `etafinaldelivery` fuera de `ETA_KEYS` → `finalDeliveryEta`; sin `eta` si `arrived` |

Descartado al implementar: ampliar `DEFAULT_PREFIXES`. El backend usa la misma
tabla (`carrierOf`), así que meter prefijos de arrendador ahí habría hecho que
el backend etiquetara mal el contenedor. La solución es pasar la naviera
conocida y no deducirla del prefijo.

## Verificación

- `python -m unittest discover -s tests` — 96 tests, verde
- `mvn -B -ntp verify` — 25 tests, verde
- `verify-bl.yml` verde en GitHub
