package pe.renzo.tracking.api.dto;

import java.time.Instant;

public record DispatchResponse(
        boolean dispatched,
        String workflow,
        String ref,
        String message,
        Instant requestedAt,
        int containersQueued
) {
}
