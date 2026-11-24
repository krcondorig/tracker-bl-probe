package pe.renzo.tracking.domain;

import java.time.Instant;
import java.util.List;

/**
 * Estado consolidado de un contenedor. Es la fila que el scraper actualiza en
 * cada ejecución y la que consume el frontend.
 *
 * <p>Se modela como clase inmutable con {@code with...} para que el servicio de
 * ingesta no mute estado compartido. En producción, esta clase suele ser una
 * entidad JPA; el repositorio es una interfaz precisamente para permitir ese
 * cambio sin tocar los servicios.
 */
public record ContainerTracking(
        String containerNumber,
        String carrier,
        TrackingStatus status,
        String currentStatus,
        String location,
        String vessel,
        String voyage,
        Instant eta,
        Instant lastEventAt,
        List<TrackingEvent> events,
        int attempts,
        Instant lastAttemptAt,
        Instant nextRetryAt,
        String errorCode,
        String errorMessage,
        String runId,
        String sourceUrl,
        boolean checkDigitValid,
        Instant createdAt,
        Instant updatedAt
) {

    public ContainerTracking {
        events = (events == null) ? List.of() : List.copyOf(events);
    }

    public static ContainerTracking pending(String containerNumber, String carrier, Instant now) {
        return new ContainerTracking(
                containerNumber, carrier, TrackingStatus.PENDING,
                null, null, null, null, null, null,
                List.of(), 0, null, null, null, null, null, null, false,
                now, now);
    }

    /** ¿Debe el scraper volver a intentarlo en esta ejecución? */
    public boolean isDue(Instant now) {
        if (status != null && status.isTerminal()) {
            return false;
        }
        return nextRetryAt == null || !nextRetryAt.isAfter(now);
    }

    /**
     * ¿Sigue este contenedor necesitando consultas?
     *
     * <p>Un envío con estado terminal (NOT_FOUND/INVALID) o ya cerrado por la
     * naviera (entregado / vacío devuelto) sale de la cola para siempre. El resto
     * se consulta cada {@code recheckInterval} aunque el último intento haya
     * sido OK: el tracking cambia, pero sin volver a pedirlo cada 24 h.
     */
    public boolean needsTracking(Instant now, java.time.Duration recheckInterval) {
        if (status == null || status.isTerminal()) {
            return false;
        }
        if (TrackingLifecycle.isShipmentClosed(currentStatus)) {
            return false;
        }
        if (nextRetryAt != null && nextRetryAt.isAfter(now)) {
            return false;
        }
        // Un fallo transitorio se reintenta en cuanto vence su backoff: el
        // intervalo de refresco solo espacia las consultas que salieron bien.
        if (status.isRetryable()) {
            return true;
        }
        // Recién consultado con éxito: no se repite hasta cumplir el intervalo.
        return lastAttemptAt == null || !lastAttemptAt.plus(recheckInterval).isAfter(now);
    }
}
