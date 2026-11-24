package pe.renzo.tracking.domain;

import java.time.Instant;

public record TrackingEvent(
        Instant timestamp,
        String status,
        String location,
        String vessel,
        String voyage
) {
}
