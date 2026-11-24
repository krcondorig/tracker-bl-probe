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

    public record TrackingData(
            String currentStatus,
            String location,
            String vessel,
            String voyage,
            Instant eta,
            Instant lastUpdate,
            List<TrackingEventDto> events
    ) {
        public TrackingData {
            events = (events == null) ? List.of() : List.copyOf(events);
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
