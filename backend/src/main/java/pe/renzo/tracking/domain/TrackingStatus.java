package pe.renzo.tracking.domain;

/**
 * Estados del contrato con el scraper (los mismos valores que devuelve Python).
 */
public enum TrackingStatus {
    /** Aún no se ha consultado, o toca reintentar. */
    PENDING,
    /** Payload capturado y normalizado. */
    OK,
    /** La naviera respondió que no conoce el contenedor. */
    NOT_FOUND,
    /** Challenge anti-bot (Akamai/Cloudflare) o 403. */
    BLOCKED,
    /** No se capturó ningún payload dentro del presupuesto. */
    TIMEOUT,
    /** Fallo técnico: excepción, payload ilegible, esquema desconocido. */
    ERROR,
    /** El número no es un contenedor ISO 6346 utilizable. */
    INVALID;

    /** Terminal = no merece la pena reintentar automáticamente. */
    public boolean isTerminal() {
        return this == NOT_FOUND || this == INVALID;
    }

    /** Fallo transitorio: se reintenta con backoff. */
    public boolean isRetryable() {
        return this == BLOCKED || this == TIMEOUT || this == ERROR;
    }

    public static TrackingStatus fromWire(String raw) {
        if (raw == null || raw.isBlank()) {
            return ERROR;
        }
        try {
            return valueOf(raw.trim().toUpperCase());
        } catch (IllegalArgumentException ex) {
            return ERROR;
        }
    }
}
