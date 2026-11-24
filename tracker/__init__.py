"""Extractor de tracking marítimo (MSC / Maersk / Hapag-Lloyd).

Paquete modular:

    config      -> carga y validación de configuración desde entorno/CLI
    models      -> enums, dataclasses y contrato JSON con Spring Boot
    containers  -> normalización de números de contenedor + ISO 6346
    extract     -> normalizador genérico de payloads JSON heterogéneos
    carriers    -> adaptadores por naviera (deep links, hints, selectores)
    browser     -> sesión Camoufox + intercepción de XHR/Fetch
    httpclient  -> cliente de Spring Boot (GET /pendientes, POST webhook)
    report      -> resumen de ejecución para consola y GitHub Step Summary
"""

__version__ = "1.0.0"
