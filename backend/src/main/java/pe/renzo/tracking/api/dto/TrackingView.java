package pe.renzo.tracking.api.dto;

import pe.renzo.tracking.domain.ContainerTracking;

import java.time.Instant;
import java.util.List;

public record TrackingView(
        String containerNumber,
        String carrier,
        String status,
        String currentStatus,
        String location,
        String vessel,
        String voyage,
        Instant eta,
        boolean arrived,
        Instant arrivalDate,
        String dischargePort,
        Instant finalDeliveryEta,
        Instant lastEventAt,
        int eventCount,
        int attempts,
        Instant lastAttemptAt,
        Instant nextRetryAt,
        String errorCode,
        String errorMessage,
        String runId,
        String sourceUrl,
        boolean checkDigitValid,
        Instant updatedAt
) {
    public static TrackingView from(ContainerTracking tracking) {
        return new TrackingView(
                tracking.containerNumber(),
                tracking.carrier(),
                tracking.status() == null ? "PENDING" : tracking.status().name(),
                tracking.currentStatus(),
                tracking.location(),
                tracking.vessel(),
                tracking.voyage(),
                tracking.eta(),
                tracking.arrived(),
                tracking.arrivalAt(),
                tracking.dischargePort(),
                tracking.finalDeliveryEta(),
                tracking.lastEventAt(),
                tracking.events().size(),
                tracking.attempts(),
                tracking.lastAttemptAt(),
                tracking.nextRetryAt(),
                tracking.errorCode(),
                tracking.errorMessage(),
                tracking.runId(),
                tracking.sourceUrl(),
                tracking.checkDigitValid(),
                tracking.updatedAt()
        );
    }

    public static List<TrackingView> from(List<ContainerTracking> trackings) {
        return trackings.stream().map(TrackingView::from).toList();
    }
}
