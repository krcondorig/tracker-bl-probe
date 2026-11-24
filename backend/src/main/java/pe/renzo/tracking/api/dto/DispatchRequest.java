package pe.renzo.tracking.api.dto;

import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;

import java.util.List;

/** Body de POST /api/v1/tracking/dispatch */
public record DispatchRequest(
        @Size(max = 200, message = "Máximo 200 contenedores por despacho")
        List<@Pattern(
                regexp = "^[A-Za-z]{4}\\d{7}$",
                message = "Debe ser un contenedor ISO 6346 (4 letras + 7 dígitos), p. ej. MSCU1234567"
        ) String> containers,

        @Size(max = 3)
        List<String> carriers,

        @Min(value = 1, message = "maxContainers debe ser >= 1")
        @Max(value = 200, message = "maxContainers debe ser <= 200")
        Integer maxContainers,

        Boolean dryRun,

        String ref
) {
    public DispatchRequest {
        carriers = (carriers == null || carriers.isEmpty())
                ? List.of("MSC", "MAERSK", "HAPAG_LLOYD")
                : List.copyOf(carriers);
    }
}
