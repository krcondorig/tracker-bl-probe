package pe.renzo.tracking.api.dto;

import java.time.Instant;
import java.util.List;

public record IngestResponse(
        String runId,
        int received,
        int updated,
        int unknown,
        List<String> unknownContainers,
        Instant processedAt
) {
}
