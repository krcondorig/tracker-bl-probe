# Maritime Tracking Scraper

Microservicio de extracción de tracking marítimo para **MSC**, **Maersk** y
**Hapag-Lloyd**, pensado para correr gratis en GitHub Actions y enviar los
resultados a un backend Spring Boot.

## Por qué está montado así

| Problema real | Cómo se resuelve aquí |
| --- | --- |
| La IP del runner de GitHub está en listas negativas | Cloudflare WARP en modo `proxy` (SOCKS5 `127.0.0.1:40000`) delante de todo el tráfico |
| Akamai/Cloudflare detectan Firefox headless | Camoufox con `headless=False` bajo `xvfb-run`, `geoip=True`, `locale="es-PE"`, perfil `os="windows"`, `humanize=True` |
| Los selectores del DOM cambian cada semana | Se intercepta el JSON con `page.on("response", ...)`; el DOM solo es respaldo |
| Cada naviera devuelve un esquema distinto | Normalizador genérico por puntuación de colecciones, no mapeos rígidos |
| Un contenedor bloqueado no debe tumbar el lote | Cada consulta se aísla; los fallos se reportan como estado, no como excepción |
| Reintentar un bloqueo con la misma IP lo empeora | El backend aplica backoff exponencial (6 h → 7 días) por contenedor |

## Estructura

```
.
├── .github/workflows/
│   ├── scraper.yml           # WARP + Camoufox + ejecución (cron 13:00 UTC / dispatch)
│   └── ci.yml                # tests de Python y de Spring Boot
├── scraper.py                # orquestador (CLI)
├── requirements.txt
├── tracker/
│   ├── config.py             # entorno + argparse, validación temprana
│   ├── models.py             # enums, dataclasses, contrato JSON
│   ├── containers.py         # ISO 6346, prefijo → naviera
│   ├── extract.py            # normalizador de payloads heterogéneos
│   ├── carriers.py           # adaptadores por naviera
│   ├── browser.py            # Camoufox + intercepción XHR/Fetch
│   ├── httpclient.py         # GET /pendientes y POST webhook
│   └── report.py             # resumen, JSON y GitHub Step Summary
├── tests/test_tracker.py     # 16 tests del núcleo
├── scripts/
│   ├── smoke_extract.py      # normalizador contra payloads de las 3 navieras
│   ├── e2e_intercept.py      # Camoufox real capturando un XHR local
│   └── e2e_full_flow.py      # backend falso → navegador → webhook
└── backend/                  # Spring Boot 3.3 + Java 21
```

## Contrato entre el scraper y Spring Boot

El script espera tres cosas y nada más:

1. `GET {SPRING_WEBHOOK_URL}/pendientes` con `Authorization: Bearer <token>`.
   Acepta `["MSCU1234567", ...]`, `[{"containerNumber": "..."}, ...]` o
   `{"containers": [...]}`.
2. `POST {SPRING_WEBHOOK_URL}` con el JSON de resultados.
3. `420`/`5xx` reintentables: el cliente reintenta con backoff exponencial.

Los estados posibles son los mismos en ambos lados:

| Estado | Significado | ¿Reintentar? |
| --- | --- | --- |
| `OK` | Payload capturado y normalizado | Sí, cada 20 h |
| `NOT_FOUND` | La naviera no conoce el contenedor | No (terminal) |
| `INVALID` | No es un contenedor ISO 6346 utilizable | No (terminal) |
| `BLOCKED` | Challenge anti-bot o 403 | Sí, con backoff |
| `TIMEOUT` | Sin payload dentro del presupuesto | Sí, con backoff |
| `ERROR` | Fallo técnico o esquema desconocido | Sí, con backoff |

### Política de refresco

Un contenedor `OK` **no** se vuelve a consultar en cada ejecución. El backend
aplica dos reglas:

1. Si la naviera reporta el envío como cerrado (`Delivered`, `Empty returned`,
   `Entregado`, `Vacío devuelto`…), sale de la cola para siempre.
2. Si sigue activo, se refresca como mucho cada `tracking.scraper.recheck-interval`
   (20 h por defecto, alineado con el cron diario).

Sin esto el cron consultaría eternamente contenedores entregados hace meses:
minutos de Actions desperdiciados y, peor, un patrón repetido contra la misma
naviera que termina en bloqueo de IP.

## Configuración

### Secretos del repositorio (Settings → Secrets → Actions)

| Secreto | Uso |
| --- | --- |
| `SPRING_WEBHOOK_URL` | URL base del backend, p. ej. `https://api.midominio.pe/api/v1/scraper` |
| `SPRING_API_TOKEN` | Token compartido; el backend lo valida en `/scraper/*` |

### Variables de entorno del scraper

Todas son opcionales salvo las dos credenciales.

| Variable | Default | Notas |
| --- | --- | --- |
| `SCRAPER_PROXY` | `socks5://127.0.0.1:40000` | Vacío = salida directa (solo para depurar) |
| `SCRAPER_HEADLESS` | `false` | En el workflow se ejecuta bajo `xvfb-run` |
| `SCRAPER_LOCALE` | `es-PE` | `geoip=True` ajusta timezone/IP de WebRTC |
| `SCRAPER_WAIT_SECONDS` | `25` | Espera máxima por payload tras cargar la página |
| `SCRAPER_MAX_ATTEMPTS` | `2` | Reintentos por contenedor |
| `SCRAPER_DELAY_MIN` / `MAX` | `6` / `12` | Pausa aleatoria entre contenedores |
| `SCRAPER_MAX_CONTAINERS` | `40` | Tope duro por ejecución |
| `SCRAPER_DRY_RUN` | `false` | Extrae y escribe JSON, no envía el POST |
| `SCRAPER_LOG_LEVEL` | `INFO` | `DEBUG` muestra cada URL capturada |

## Uso local

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m camoufox fetch

# Sin backend: contenedores por CLI, salida a JSON
python scraper.py --containers MSCU1234567,MAEU7654321 --dry-run

# Con backend (lee /pendientes y publica el webhook)
export SPRING_WEBHOOK_URL=https://api.midominio.pe/api/v1/scraper
export SPRING_API_TOKEN=...
python scraper.py
```

Pruebas:

```bash
python -m unittest discover -s tests -v   # 16 tests, sin navegador
python scripts/e2e_intercept.py           # Camoufox real contra servidor local
python scripts/e2e_full_flow.py           # flujo completo con backend falso
cd backend && mvn verify                  # 12 tests de Spring Boot
```

## Backend Spring Boot

```bash
export GITHUB_OWNER=tu-usuario
export GITHUB_REPO=scraper
export GITHUB_DISPATCH_TOKEN=ghp_...      # scope: actions:write sobre ese repo
export SCRAPER_SHARED_SECRET=...          # el mismo valor que SPRING_API_TOKEN
cd backend && mvn spring-boot:run
```

Endpoints:

```bash
# Disparar el workflow a mano
curl -X POST http://localhost:8080/api/v1/tracking/dispatch \
  -H 'Content-Type: application/json' \
  -d '{"containers":["MSCU1234567","MAEU7654321"],"carriers":["MSC","MAERSK"]}'

# Registrar contenedores para el próximo cron
curl -X POST http://localhost:8080/api/v1/tracking/register \
  -H 'Content-Type: application/json' -d '["HLCU1234567"]'

# Consultar estado
curl http://localhost:8080/api/v1/tracking/MSCU1234567
```

## Notas de operación

- **Cron**: `0 13 * * *` = 08:00 en Lima (UTC-5 todo el año). GitHub puede
  retrasar el disparo hasta ~15 min en horas punta; si la puntualidad es
  crítica, usa `workflow_dispatch` desde un cron externo.
- **WARP**: si el paso de verificación de IP falla, el job se detiene antes de
  scraping. Es intencional: sin WARP las tres navieras devuelven challenge y el
  backend solo acumularía backoffs.
- **Rate limit**: 40 contenedores × ~20 s ≈ 15 min de navegador. El límite del
  plan gratuito de Actions (2000 min/mes en repos privados) da de sobra para un
  cron diario. En repos públicos es ilimitado.
- **Persistencia**: `InMemoryContainerTrackingRepository` es para arrancar sin
  base de datos. Para producción, implementa `ContainerTrackingRepository` con
  Spring Data JPA; es la única clase que hay que cambiar.
- **Mantenimiento**: si una naviera cambia su endpoint, la intercepción sigue
  capturando cualquier JSON del host que parezca tracking. Solo hay que ajustar
  `path_hints`/`url_markers` en `tracker/carriers.py`, no el normalizador.
