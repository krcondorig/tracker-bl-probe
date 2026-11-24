# Diseño: tracking por B/L con llegada a Manzanillo por contenedor

Fecha: 2026-11-24
Estado: propuesto

## Intención

El negocio consulta **embarques por Bill of Lading**. Un B/L agrupa varios
contenedores (3 en Maersk, 8 en MSC según las guías aportadas). Lo que se
necesita saber de cada contenedor es:

1. **Si el buque ya llegó a Manzanillo** (sí/no).
2. **Su ETA**, que las navieras publican como estimada y actualizan hasta
   convertirse en fecha real de atraque.

El sistema actual solo entiende números de contenedor ISO 6346. Con las tres
guías reales (`HLCUNG12607EKZE0`, `275792562`, `MEDUADP85690`) devuelve
`INVALID` en las tres, porque ninguna es un contenedor.

## Evidencia recogida

Payloads reales capturados con Camoufox (`probe-dumps/`):

| Guía | Naviera | Forma | Resultado real |
| --- | --- | --- | --- |
| `275792562` | Maersk | 9 dígitos | 3 contenedores, todos `COMPLETE` |
| `MEDUADP85690` | MSC | `MEDU` + 8 alfanum | 8 contenedores, Qingdao → Manzanillo → Guadalajara |
| `HLCUNG12607EKZE0` | Hapag-Lloyd | `HLCU` + 12 alfanum | Challenge Cloudflare sin WARP; **payload no observado** |

Hechos verificados que condicionan el diseño:

- **El prefijo no basta para clasificar.** `MEDUADP85690` es B/L MSC (12
  caracteres) y `MEDU3037855` es contenedor MSC (11 caracteres, dígito
  verificador válido). La detección debe combinar forma y longitud.
- **Los contenedores de un mismo B/L NO comparten historial.** En MSC,
  8 contenedores producen 4 secuencias de eventos distintas (fechas de
  `Import to consignee` diferentes). Mapear cada contenedor es información
  real, no duplicación.
- **Maersk `destination` no es el puerto de descarga.** En `275792562` el
  destino es `MEXICO CITY`; Manzanillo (`MXZLOSM`) solo aparece como una de
  las `locations` con eventos. El puerto hay que derivarlo de los eventos.
- **`eta_final_delivery` de Maersk no es la llegada al puerto.** Vale
  `2026-10-31` (entrega en Cuautitlán) mientras el buque atracó el
  `2026-10-06`. Usarla como ETA a Manzanillo daría 25 días de error.
- **`PodEtaDate` de MSC está vacío cuando el embarque ya llegó.** Todos los
  valores vienen `""`. No hay evidencia del formato del ETA en tránsito.

## Objetivos

- Aceptar B/L de las tres navieras como unidad de consulta.
- Guardar el embarque (B/L) y sus contenedores como entidades separadas.
- Exponer por contenedor: `arrived` (llegó a Manzanillo), `arrivalDate`
  (estimada o real), `arrivalIsActual`, `vessel`, `voyage`, `lastEvent`.
- Exponer por embarque: conteo `n/total arribados` y la llegada más temprana,
  para ver el estado "con el primer contenedor".
- Rediseñar `/pendientes` para devolver B/L, no contenedores.

## No objetivos

- Validación empírica del ETA en tránsito. Se implementa (ver decisión 3), pero
  no hay payload de referencia: las tres guías ya arribaron. Queda marcado como
  inferencia hasta que aparezca un B/L realmente en tránsito.
- Entrega final al cliente (`eta_final_delivery`). Solo interesa Manzanillo.
- Reintentos por contenedor. La unidad de reintento pasa a ser el B/L.
- Migración de datos existentes. El repositorio es en memoria.

## Decisiones de diseño

### 1. Detección de tipo por forma, no por prefijo

Nuevo módulo `tracker/references.py` con una función que clasifica una cadena
en `CONTAINER`, `BILL_OF_LADING` o `INVALID`, y en qué naviera:

| Patrón | Tipo | Naviera |
| --- | --- | --- |
| `^[A-Z]{4}\d{7}$` con dígito válido | CONTAINER | por prefijo BIC |
| `^\d{9}$` | BILL_OF_LADING | Maersk |
| `^MEDU[A-Z0-9]{8}$` o `^MSCU[A-Z0-9]{8}$` | BILL_OF_LADING | MSC |
| `^HLCU[A-Z0-9]{12}$` | BILL_OF_LADING | Hapag-Lloyd |
| cualquier otra cosa | INVALID | — |

La longitud desambigua `MEDU`: 8 caracteres tras el prefijo = B/L; 7 dígitos
= contenedor. Se documenta en el docstring porque es el punto frágil.

**Mi llamada:** un contenedor con prefijo conocido pero dígito verificador
inválido sigue consultándose (comportamiento actual), porque las navieras a
veces lo toleran y el usuario copia mal un carácter. Solo se rechaza si la
forma no es de contenedor ni de B/L.

`split_csv` de `tracker/containers.py` se reescribe sobre `classify`: hoy su
regex `[A-Za-z]{4}[\s-]*\d(?:[\s-]*\d){6}` solo reconoce contenedores, así que
una entrada manual como `"MEDUADP85690 MSCU1234567"` descartaría el B/L en
silencio. Los caminos automáticos (`dispatch`) unen con comas y no están
expuestos, pero el CLI sí. Se clasifica cada token y se conservan contenedores
y B/L, sin duplicados.

### 2. Modelo de datos en dos niveles

Python (`tracker/models.py`):

```
ShipmentResult            # un B/L o un contenedor suelto
  reference, reference_type, carrier
  status, attempts, duration_ms, scraped_at
  error_code, error_message, source_url
  discharge_port: str | None      # "MANZANILLO, MX" o derivado del DISCHARG
  carrier_closed: bool            # MSC Delivered / Maersk todos COMPLETE
  closed_reason: str | None       # "CARRIER_DELIVERED" | "ALL_ARRIVED" | None
  containers: list[ShipmentContainer]
  arrived_count, total_count      # derivados en lectura
  earliest_arrival, earliest_arrival_is_actual   # derivados en lectura

ShipmentContainer         # un contenedor dentro del B/L
  container_number, size_type
  arrived: bool
  arrival_date: str | None
  arrival_is_actual: bool
  vessel, voyage, last_event, last_event_at
  events: list[TrackingEvent]
```

`reference_type` es `BILL_OF_LADING` o `CONTAINER` y resuelve el caso del
contenedor suelto sin necesitar un campo nulo. `discharge_port` y
`carrier_closed` son **señales persistidas**: sin ellas el backend no puede
evaluar el cierre entre ejecuciones, porque solo ve el resultado de la
ejecución actual. `arrived_count`, `total_count` y `earliest_arrival` siguen
siendo derivados en lectura, como exige la decisión 9.

Se mantiene `TrackingEvent` tal cual (timestamp, status, location, vessel,
voyage) para no romper el contrato existente.

Backend Java: `ShipmentTracking` (nuevo) y `ContainerTracking` pasa a ser el
hijo. El repositorio tiene **un mapa por `reference`** y un índice secundario
`containerNumber -> reference` para resolver `GET /tracking/{number}`.

**Decisión sobre las filas hijas (hueco de la revisión):** los hijos **no**
entran en la cola ni tienen ciclo de vida propio. No tienen `status`,
`attempts` ni `nextRetryAt`: el reintento pertenece al embarque. El índice por
contenedor es solo de lectura (`GET /tracking/{number}` resuelve el embarque
padre). `GET /tracking` sigue listando **embarques**, no contenedores, para que
un frontend existente no empiece a ver filas `PENDING` por cada hijo.

Un contenedor suelto (decisión 5-ter) sí crea un `ShipmentTracking` con
`reference_type = CONTAINER` y un único hijo. La clave del mapa es siempre
`reference`, nunca un `null`.

#### 2.1. Absorción de un contenedor suelto que luego aparece en un B/L

Si `MEDU3037855` se registró suelto y después aparece como hijo de
`MEDUADP85690`, hay dos candidatos y no debe consultarse dos veces: es
exactamente el patrón repetido que el diseño evita.

Regla: **el B/L absorbe**. Al descubrir el contenedor dentro de un embarque, la
fila suelta `reference_type = CONTAINER` se **elimina** y su `firstSeenAt` se
traslada al hijo (para no perder la antigüedad del seguimiento). Los `attempts`
**no** se trasladan: el ciclo de reintento pertenece al embarque (decisión 6), y
moverlo al hijo reiniciaría el backoff en 6 h justo después de la absorción.
Precedencia de resolución de `GET /tracking/{number}`:

1. Clave `reference` en el mapa de embarques.
2. Índice `containerNumber -> reference` (devuelve el padre).
3. Nada → 404.

Un contenedor nunca queda con dos filas: o es suelto, o es hijo de un B/L.

#### 2.2. Forma de respuesta: `status` es el resultado, `lifecycle` es el negocio

`TrackingView` **conserva sus campos actuales** y añade los nuevos, para no
romper el contrato que el frontend ya consume ni el test
`ScraperEndpointsTest.webhookIngestsResults` (que exige `$.status = "OK"`,
`$.currentStatus`, `$.eventCount`).

La ambigüedad se resuelve separando dos conceptos que estaban fundidos:

| Campo | Qué es | Valores |
| --- | --- | --- |
| `status` | resultado del último scraping | `OK`, `NOT_FOUND`, `BLOCKED`, `TIMEOUT`, `ERROR`, `INVALID` |
| `lifecycle` | estado de negocio del embarque (nuevo) | `ARRIVED`, `CARRIER_CLOSED`, `IN_TRANSIT`, `NOT_PUBLISHED`, `STALE`, `INVALID_REFERENCE`, `FAILED` |

Los campos heredados (`currentStatus`, `location`, `vessel`, `voyage`, `eta`,
`eventCount`) se rellenan desde el **contenedor principal** (primer contenedor
con llegada, o el primero de la lista). Se añaden `reference`,
`referenceType`, `containers[]`, `arrivedCount`, `totalCount` y
`earliestArrival`.

Esto responde a la objeción "no puede ser tal cual": el DTO no cambia de forma,
solo gana campos, y `status` mantiene su significado anterior. `GET /tracking`
(lista) devuelve el mismo `TrackingView` por embarque.

### 3. Detección de llegada: evento real, no fecha

`arrivalDate` es **un solo campo con calidad**, como se describió en la
conversación: vale la estimada mientras el buque no atraca y pasa a ser la real
cuando atraca. `arrivalIsActual` indica cuál de las dos es.

Función pura `detect_arrival(container, discharge_port)` en
`tracker/arrivals.py`:

1. **Evento de arribo real**: evento cuyo texto de estado coincide con
   `arrival|arriv|discharg|atracad|descarg` **y** cuya ubicación contiene el
   puerto de descarga. Si existe → `arrived = true`,
   `arrival_date` = la fecha de ese evento, `arrival_is_actual = true`.
   Si hay varios (Maersk tiene `CONTAINER ARRIVAL` y `DISCHARG`), se toma el
   **más temprano**: es el momento en que el buque/equipo llegó al puerto.
2. **Sin evento real pero con ETA del carrier**: se usa el campo de ETA no
   vacío. Solo MSC lo tiene (`PodEtaDate` por contenedor). `eta_final_delivery`
   de Maersk **no se usa**: mide entrega al cliente, no llegada al puerto
   (25 días de diferencia en la guía real).
   **Maersk no muestra ETA antes de llegar**: en su payload no hay ningún campo
   de ETA portuaria —`event_time_type` es `ACTUAL` en todos los eventos— así
   que un contenedor Maersk en tránsito devuelve `arrival_date = null` y
   `arrival_is_actual = false`. Es un límite del carrier, no del diseño, y se
   documenta como tal en la respuesta (no se inventa una fecha).
3. **Sin nada**: `arrived = false`, `arrival_date = null`.

**Puerto de descarga**: se toma explícito del payload cuando existe
(`GeneralTrackingInfo.PortOfDischarge` en MSC). En Maersk no existe como campo
del embarque —el `destination` es la entrega final, `MEXICO CITY` en la guía
real— así que se deriva de la ubicación del primer evento de tipo `DISCHARG`.
Ese es el puerto donde el buque descargó, que es exactamente la pregunta de
negocio, y funciona igual si algún día el embarque va a otro puerto.

**Nota de honestidad:** el paso 1 está validado con datos reales (los dos B/L
arribados). Los pasos 2 y 3 son inferencia: todos los eventos observados son
`ACTUAL` y `PodEtaDate` viene vacío en MSC. El test de esos casos usa payload
sintético y así se marca en el propio test.

### 4. Normalizador: correcciones sobre el genérico actual

El normalizador genérico se conserva, con tres correcciones que la evidencia
demostró necesarias:

1. **`vessel` no debe ser el IMO.** En MSC el nombre real está en
   `Detail[0]` (`MSC LAUSANNE VI`) y `Vessel.IMO` es `9320398`. Se descartan
   valores puramente numéricos de 7 dígitos para `vessel`.
2. **`state` sale de `STATUS_KEYS` para Maersk.** Hoy `currentStatus` devuelve
   `"CX"` (provincia de destino de la location). El estado real es `activity`
   (`GATE-OUT`, `DISCHARG`), que se añade a las claves de prioridad.
3. **Los eventos anidados en `locations[].events[]` deben conservar la ciudad
   de su location padre.** Hoy la ubicación sale, pero el vínculo evento →
   puerto se hace por herencia del padre, no por clave propia.

Además, `vessel` y `voyage` necesitan una regla explícita, porque un mismo
contenedor pasa por varios tramos y la regla ingenua ("último evento") devuelve
`null`: ambos payloads terminan en un movimiento terrestre sin buque.

**Regla: el tramo oceánico que lo descargó.** Se toma el último evento (por
fecha) que tenga buque identificado y que sea marítimo —en MSC el evento cuyo
`Detail` trae nombre y viaje; en Maersk el evento con `vessel_name`
poblado— **anterior o igual** al evento de descarga en el puerto. Si no hay
tramo oceánico (contenedor solo terrestre), queda `null`. Esto es además lo que
hace comparable el dato entre navieras: MSC pone `MSC LAUSANNE VI` +
`QM639R` en el evento de descarga, Maersk pone `MAERSK EL ALTO` + `636E` en el
suyo.

En MSC el viaje (`QM639R`) viene en `Detail[1]` sin nombre de clave; se toma
por posición solo cuando `Detail[0]` es un nombre de buque plausible (no
numérico). Se documenta como dependencia frágil del esquema MSC.

Se añade un extractor específico por naviera (`extract_msc_shipment`,
`extract_maersk_shipment`) que aprovecha la estructura conocida y **cae al
genérico** si la forma no coincide. Esto evita que un cambio de esquema rompa
la extracción, pero da precisión mientras el esquema se mantenga.

### 5. Cambio de contrato con el backend

`GET /pendientes` pasa a devolver **referencias con su tipo**, para que la cola
pueda representar tanto un B/L como un contenedor suelto:

```json
{"references":[
  {"reference":"MEDUADP85690","type":"BILL_OF_LADING","carrier":"MSC","attempts":0},
  {"reference":"MEDU3037855","type":"CONTAINER","carrier":"MSC","attempts":0}
]}
```

`POST` del webhook cambia de `results: [ContainerResult]` a
`results: [ShipmentResult]` con contenedores anidados.

**Compatibilidad, en las dos direcciones:**

- *Lectura*: `httpclient.fetch_pending` acepta las tres formas anteriores
  (`["MSCU1234567"]`, `[{"containerNumber":...}]`, `{"containers":[...]}`)
  además de la nueva, detectando la forma de cada entrada.
- *Escritura*: `TrackingIngestService` detecta la forma de **cada entrada** de
  `results`. Una entrada con `containerNumber` y `data` es un `ContainerResult`
  heredado y se ingiere con la lógica actual (se convierte en un
  `ShipmentTracking` de un solo contenedor). Una entrada con `reference` es un
  `ShipmentResult` nuevo.

Esto no es cortesía: durante un despliegue, un job ya en vuelo con el commit
anterior hace POST con el formato viejo. Sin esta tolerancia, esa ejecución
perdería sus resultados en silencio — justo lo que el webhook existe para
evitar.

`results` es una unión tolerante, no dos endpoints: el campo
`reference`/`containerNumber` decide la rama.

### 5-bis. El intake también hay que cambiarlo (hueco detectado en revisión)

Sin esto la funcionalidad no es usable por el cron: el workflow pide
`/pendientes` y recibe B/L, pero hoy nada puede *crear* un B/L.

- `DispatchRequest` valida hoy `^[A-Za-z]{4}\d{7}$`, así que rechaza los tres
  B/L con 400. Se sustituye por una validación de forma que acepte contenedor
  **o** B/L (`ReferenceType`), reutilizando la misma tabla de la decisión 1.
- `InMemoryContainerTrackingRepository.ensureExists` clasifica hoy por
  `carrierOf(prefijo)`, que devuelve `UNKNOWN` para un B/L. Pasa a crear un
  `ShipmentTracking` cuando la forma es de B/L.
- `POST /tracking/register` acepta B/L de la misma manera.
- `GET /tracking/{number}` **conserva su forma de respuesta** (ver 2.2: mismos
  campos, más los nuevos; `status` mantiene su significado) y resuelve tanto un
  contenedor como un B/L mediante el índice. Se añade
  `GET /tracking/shipment/{reference}` como ruta explícita que devuelve el
  embarque con sus contenedores anidados.

### 5-ter. Contenedor suelto sigue existiendo

`MEDU3037855` sigue clasificando como `CONTAINER` y la ruta de consulta
individual no se elimina: sirve cuando alguien pasa un contenedor sin B/L.
En ese caso el resultado es un `ShipmentResult` con `reference` = el número de
contenedor y un único hijo, para que el webhook tenga **una sola forma** y no
dos. El `carrier` del hijo se deriva de su propio prefijo BIC (los hijos de un
B/L MSC incluyen `CAIU`, `DRYU`, `FCIU`, `TRHU`, que no son prefijos MSC),
cayendo al `carrier` del embarque si el prefijo es desconocido.

### 6. Ciclo de vida del embarque

**Corrección de un fallo de la versión anterior de este documento.** La regla
"cierra si el puerto de descarga nunca aparece en los eventos" habría cerrado
exactamente los embarques que la funcionalidad existe para seguir: todo
embarque en tránsito tiene el puerto conocido pero aún sin evento. Un B/L
recién registrado se habría consultado una vez y desaparecido para siempre.

El ciclo de vida es un **estado explícito por embarque**, no una heurística:

| Estado | Condición | ¿Se consulta? |
| --- | --- | --- |
| `ARRIVED` | todos los contenedores con `arrived = true` | No (terminal) |
| `CARRIER_CLOSED` | la naviera marca entregado/completo | No (terminal) |
| `IN_TRANSIT` | sin llegada, dentro de la ventana de seguimiento | Sí, cada `recheck-interval` |
| `NOT_PUBLISHED` | la naviera aún no conoce la referencia | Sí, dentro de la ventana |
| `STALE` | la ventana de seguimiento expiró | No (terminal) |
| `INVALID_REFERENCE` | no es contenedor ni B/L utilizable | No (terminal) |
| `FAILED` | `BLOCKED`/`TIMEOUT`/`ERROR` | Sí, con backoff |

La **ventana de seguimiento** (`tracking.scraper.follow-up-window`, nueva,
por defecto 90 días) se cuenta desde `firstSeenAt`, la primera vez que se vio
el embarque. Pasada la ventana, un embarque que aún no llegó pasa a `STALE`.
Es lo que evita la consulta eterna sin matar al embarque en tránsito: 90 días
cubre con holgura un tránsito Asia–Callao/Manzanillo, que ronda 30-45.

`pending-max-age` deja de usarse para cerrar embarques (hoy es configuración
muerta: nadie la lee). Se elimina del YAML en vez de dejarla como trampa.

Un B/L sin contenedores no puede llegar a `ARRIVED` (conjunto vacío): queda
`IN_TRANSIT` hasta agotar la ventana. Es deliberado: un payload de 0
contenedores suele significar "aún no publicado", no "embarque vacío".

**Cierre por señal de la naviera** (`carrier_closed`): MSC expone
`Delivered: true` en el B/L y por contenedor; Maersk solo por contenedor
(`status: "COMPLETE"`), sin estado a nivel de B/L. Regla: se considera cerrado
cuando **todos** los contenedores están entregados/completos, nunca con uno
solo, para no cerrar un embarque con un contenedor entregado y otro en el agua.

`TrackingLifecycle` se reescribe sobre estos estados; el criterio deja de ser
el texto del estado del último evento.

### 7. La navegación de MSC no puede usar deep link (hallazgo empírico)

Probado con `scripts/probe_msc_deeplink.py` contra el sitio real:

| Ruta | `TrackingInfo` capturados | Resultado |
| --- | --- | --- |
| `?trackingNumber=MEDUADP85690` (modo 0) | **0** | página renderizada, sin XHR |
| `?trackingMode=1&trackingNumber=...` (modo 1) | **0** | igual |
| Formulario `#trackingNumber` + Enter | **1** (24 KB, 8 contenedores) | ✅ |

Esto rompe el flujo actual de `capture_container`, que navega al deep link y
**solo prueba el formulario si no capturó nada**. Aquí no captura nada válido,
pero sí puede capturar JSON de OneTrust/geo (`geolocation.onetrust.com`,
`GetCountryAgencies`), con lo que el respaldo no se activaría y el B/L
quedaría mal clasificado. (Con la decisión 10 un `NOT_FOUND` ya no es terminal,
pero sigue siendo un resultado incorrecto: no es que la naviera no conozca el
B/L, es que nunca lo preguntamos.)

Cambios necesarios en `tracker/browser.py`:

- El adaptador declara **su estrategia de navegación**, no un deep link fijo:
  `navigation = "form"` para MSC, `"deeplink"` para Maersk y Hapag (a
  verificar en la primera ejecución real).
- Con `navigation = "form"`, se abre `home_url` y se rellena
  `#trackingNumber`. El deep link queda como respaldo, no al revés.
- El criterio de "ya tengo lo que busco" deja de ser "capturé algún JSON": pasa
  a ser "capturé un payload cuyo extractor reconoce el B/L". Un JSON de
  cookies no debe contar como éxito.

**Hapag queda en `navigation = "deeplink"` sin validar**: su sitio devolvió
challenge de Cloudflare sin WARP, así que no pude comprobar si `?blno=`
dispara el XHR. Es el primer ajuste a revisar con la salida real.

### 8. El formato de 9 dígitos no es exclusivo de Maersk (riesgo aceptado)

`^\d{9}$` se clasifica como Maersk porque `275792562` es un `tpdoc_num` de
Maersk. Un B/L numérico de otra naviera caería ahí.

**Mi llamada:** se acepta el riesgo, sin campo `carrier` por referencia. La
mitigación que proponía la versión anterior de este documento no era
implementable: `/tracking/register` recibe `List<String>` (no hay objeto donde
colgar un `carrier`) y `DispatchRequest.carriers` es un **filtro global del
lote**, no un override por referencia — con un contenedor MSC y un B/L Hapag
en el mismo lote, un solo valor global enruta mal uno de los dos.

Lo que sí se hace: un `NOT_FOUND` no es terminal (decisión 10), así que un B/L
mal enrutado queda en `NOT_PUBLISHED` y expira por la ventana en vez de
perderse. Si en la práctica aparecen B/L numéricos de otras navieras, la
solución correcta es un `POST /tracking/register` con forma de objeto
(`{"reference": "...", "carrier": "..."}`), que es un cambio de contrato
deliberado y no un añadido silencioso.

### 9. Contenedores que desaparecen entre consultas

Un payload posterior puede traer menos contenedores que el primero (Maersk
tiene `unassigned_containers`). Regla: **los contenedores son acumulativos**,
igual que los eventos. Un contenedor que ya vimos no se borra porque el
payload nuevo no lo mencione; se conserva su último estado conocido. Así el
conteo `n/total` nunca "mejora" falsamente por un payload incompleto.
`arrived_count`, `total_count` y `earliest_arrival` se **derivan en lectura**
del conjunto de contenedores, no se persisten, para que no puedan
desincronizarse de los hijos.

El ciclo de vida (cuándo sale de la cola) está definido **solo** en la decisión
6. La versión anterior de este documento proponía aquí cerrar por
`pending-max-age` cuando no aparecía el puerto; esa regla se eliminó por ser el
fallo que la decisión 6 corrige.

### 10. `NOT_FOUND` e `INVALID` en la máquina de estados

La máquina de la decisión 6 solo cubre el camino con payload. Faltan los dos
estados de negocio que el scraper ya produce:

| Estado scraper | Significado | Estado del embarque | ¿Se reconsulta? |
| --- | --- | --- | --- |
| `NOT_FOUND` | la naviera respondió que no lo conoce | `NOT_PUBLISHED` | Sí, dentro de la ventana |
| `INVALID` | la cadena no es contenedor ni B/L | `INVALID_REFERENCE` | No (terminal) |

**Por qué `NOT_FOUND` se reintenta y no es terminal:** un B/L suele registrarse
antes de que la naviera lo publique; durante días devuelve "no encontrado" y
luego aparece. Hacerlo terminal borraría en silencio el caso más común de uso.
La ventana de 90 días pone el límite: pasada la ventana sin publicarse, pasa a
`STALE`.

Esto **cambia el comportamiento actual** del backend, donde `NOT_FOUND` es
terminal. La decisión 8 heredaba ese supuesto y por eso lo describía como
peligroso; con `NOT_PUBLISHED` retentado, el riesgo real baja: un B/L de 9
dígitos mal enrutado a Maersk se queda en `NOT_PUBLISHED` y expira en 90 días
en vez de perderse, aunque sigue gastando una consulta por ciclo.

`INVALID` sí es terminal: no es un número utilizable y ninguna consulta lo
arreglará.

## Riesgos

| Riesgo | Mitigación |
| --- | --- |
| Hapag no validado (challenge) | Se implementa con `?blno=` y extractor genérico; se verifica en la primera ejecución real con WARP y se ajusta con el log `DEBUG` |
| ETA en tránsito sin evidencia | Solo MSC tiene campo de ETA portuaria; Maersk devuelve `null` por límite del carrier, documentado. Tests con payload sintético |
| Prefijo `MEDU` ambiguo | Resuelto por longitud, con test explícito para ambos casos |
| Cambio de esquema de naviera | Extractor específico con caída al genérico |
| MSC exige formulario, no deep link | Estrategia de navegación por adaptador (`form` para MSC), validada con `probe_msc_deeplink.py` |
| B/L de 9 dígitos de otra naviera | `NOT_FOUND` no es terminal: queda `NOT_PUBLISHED` y expira por la ventana en vez de perderse |
| Payload posterior con menos contenedores | Conjunto acumulativo; un contenedor visto no se borra |
| Un `NOT_FOUND` falso deja el embarque terminal | Ya no es terminal (decisión 10); el criterio de éxito exige payload reconocido por el extractor |
| Registrar un B/L antes de que la naviera lo publique | `NOT_PUBLISHED` retentado dentro de la ventana de 90 días |
| Cerrar un embarque en tránsito por error | Estado `IN_TRANSIT` explícito + ventana de 90 días desde `firstSeenAt`; nunca se cierra por "no hay evento aún" |
| Viaje de MSC por posición en `Detail` | Se valida que `Detail[0]` no sea numérico antes de tomar `Detail[1]`; queda como punto frágil documentado |
| Romper el frontend con la nueva respuesta | `TrackingView` conserva todos sus campos y `status` mantiene su significado; solo se añaden campos |
| Un contenedor medido dos veces (suelto + hijo) | El B/L absorbe la fila suelta; precedencia de resolución explícita |
| Un job en vuelo hace POST del formato viejo | El webhook detecta la forma de cada entrada y acepta ambas |

## Criterios de aceptación

1. `scripts/probe_offline.py` procesa los payloads reales de MSC y Maersk y
   produce un `ShipmentResult` con 8 y 3 contenedores respectivamente, todos
   con `arrived = true` y `vessel` con nombre real (no IMO).
2. `HLCUNG12607EKZE0`, `275792562` y `MEDUADP85690` se clasifican como
   `BILL_OF_LADING` de su naviera; `MEDU3037855` sigue clasificando como
   `CONTAINER`.
3. Tests unitarios cubren: detección de los tres formatos, desambiguación
   `MEDU`, detección de llegada (real / estimada / sin datos) y el normalizador
   con los payloads reales guardados como fixtures.
4. `mvn test` verde con los nuevos tests de `ShipmentTracking`.
5. Un embarque con todos los contenedores arribados no vuelve a `/pendientes`.
6. `POST /tracking/dispatch` y `POST /tracking/register` aceptan los tres B/L
   reales y crean un `ShipmentTracking`, no un contenedor `UNKNOWN`.
7. `POST /tracking/dispatch` sigue rechazando basura (`"BAD123"`,
   `"XXX"`) con 400.
8. **Un embarque en tránsito sobrevive.** Test: B/L con puerto de descarga
   conocido y sin evento de arribo, `firstSeenAt` de hace 3 días → sigue
   `IN_TRANSIT` y aparece en `/pendientes`. Con `firstSeenAt` de hace 120 días
   → `STALE` y no aparece.
9. **Un embarque con un contenedor entregado y otro en el agua no se cierra.**
   Test con 2 contenedores: uno `COMPLETE`, otro sin arribo → `IN_TRANSIT`.
10. Los hijos no aparecen en `/pendientes` ni como filas propias en
    `GET /tracking`.
11. `vessel`/`voyage` de cada contenedor corresponden al tramo oceánico de
    descarga, no al último evento terrestre (que no tiene buque).
12. **Un B/L no publicado sobrevive.** Test: `NOT_FOUND` en la primera consulta
    → estado `NOT_PUBLISHED`, sigue apareciendo en `/pendientes`; tras la
    ventana → `STALE` y sale.
13. **`INVALID` es terminal:** `"BAD123"` no vuelve a la cola.
14. La cola representa un contenedor suelto con
    `{"reference":"MEDU3037855","type":"CONTAINER"}` y el scraper lo procesa
    sin intentar expandirlo como B/L.
15. **Compatibilidad de `TrackingView`:** el test existente
    `webhookIngestsResults` sigue pasando sin cambios (`$.status = "OK"`,
    `$.currentStatus = "Discharged"`, `$.eventCount = 1`) y la respuesta añade
    `lifecycle`, `reference`, `containers`, `arrivedCount`, `totalCount`.
16. **Un contenedor suelto absorbido por un B/L no se consulta dos veces.** Test:
    registrar `MEDU3037855` suelto, luego ingerir el B/L `MEDUADP85690` que lo
    contiene → queda una sola fila, `reference = MEDUADP85690`, y
    `GET /tracking/MEDU3037855` devuelve el padre.
17. **El webhook acepta un POST con formato anterior.** Test: enviar
    `results:[{"containerNumber":...}]` (forma heredada) y comprobar que se
    ingiere sin error.
18. `split_csv("MEDUADP85690 MSCU1234567")` devuelve ambos, sin descartar el
    B/L.
