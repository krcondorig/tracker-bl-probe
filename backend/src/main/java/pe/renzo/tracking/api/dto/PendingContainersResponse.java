package pe.renzo.tracking.api.dto;

import java.util.List;

/** Respuesta de GET /pendientes: contrato mínimo que consume scraper.py. */
public record PendingContainersResponse(List<Item> containers) {

    public record Item(String containerNumber, String carrier, int attempts) {
    }

    public static PendingContainersResponse of(List<Item> items) {
        return new PendingContainersResponse(items == null ? List.of() : List.copyOf(items));
    }
}
