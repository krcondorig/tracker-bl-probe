package pe.renzo.tracking.api.dto;

import java.time.Instant;
import java.util.List;

/**
 * Espejo exacto del JSON que emite {@code tracker/models.py}.
 * Jackson mapea por nombre de propiedad; no se necesitan anotaciones.
 */
public record ScraperResultsRequest(
        String runId,
        String trigger,
        Instant startedAt,
        Instant finishedAt,
        Long durationMs,
        List<String> carrierFilter,
        RunStats stats,
        List<ContainerResult> results,
        Object meta
) {

    public ScraperResultsRequest {
        results = (results == null) ? List.of() : List.copyOf(results);
        carrierFilter = (carrierFilter == null) ? List.of() : List.copyOf(carrierFilter);
    }

    public record RunStats(Integer requested, Integer ok, Integer notFound, Integer failed) {
    }

    public record ContainerResult(
            String containerNumber,
            String carrier,
            String status,
            Integer attempts,
            Long durationMs,
            Instant scrapedAt,
            String errorCode,
            String errorMessage,
            Boolean checkDigitValid,
            String sourceUrl,
            TrackingData data
    ) {
    }

    /**
     * Datos normalizados de un contenedor.
     *
     * <p>`currentStatus` y `location` son el <em>último</em> evento. Tras la
     * descarga ese último evento suele ser un movimiento terrestre (Maersk
     * reporta {@code "CX"} en MEXICO CITY), así que no sirven para responder si
     * el buque llegó al puerto: para eso están {@code arrived},
     * {@code arrivalDate} y {@code dischargePort}.
     *
     * <p>`eta` es la llegada estimada <em>al puerto</em>. La entrega final al
     * consignatario va en `finalDeliveryEta` porque puede ser semanas posterior
     * (entregar en MEXICO CITY tras descargar en Manzanillo) y confundirlas
     * hacía que se reportara como ETA una fecha que ya había pasado.
     */
    public record TrackingData(
            String currentStatus,
            String location,
            String vessel,
            String voyage,
            Instant eta,
            Instant lastUpdate,
            Boolean arrived,
            Instant arrivalDate,
            String dischargePort,
            Instant finalDeliveryEta,
            List<TrackingEventDto> events
    ) {
        public TrackingData {
            events = (events == null) ? List.of() : List.copyOf(events);
            arrived = Boolean.TRUE.equals(arrived);
        }
    }

    public record TrackingEventDto(
            Instant timestamp,
            String status,
            String location,
            String vessel,
            String voyage
    ) {
    }
}
