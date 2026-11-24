package pe.renzo.tracking.service;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import pe.renzo.tracking.api.dto.IngestResponse;
import pe.renzo.tracking.api.dto.ScraperResultsRequest;
import pe.renzo.tracking.domain.ContainerTracking;
import pe.renzo.tracking.domain.ContainerTrackingRepository;
import pe.renzo.tracking.domain.InMemoryContainerTrackingRepository;
import pe.renzo.tracking.domain.TrackingStatus;

import java.time.Duration;
import java.time.Instant;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

class TrackingIngestServiceTest {

    private final ContainerTrackingRepository repository = new InMemoryContainerTrackingRepository();
    private final TrackingIngestService service = new TrackingIngestService(repository);

    private static ScraperResultsRequest.ContainerResult ok(String number) {
        var data = new ScraperResultsRequest.TrackingData(
                "Discharged", "Callao", "MSC PERU", "123E",
                Instant.parse("2026-11-30T00:00:00Z"),
                Instant.parse("2026-11-12T03:00:00Z"),
                List.of(
                        new ScraperResultsRequest.TrackingEventDto(Instant.parse("2026-11-01T10:00:00Z"),
                                "Gate Out", "Callao", "MSC PERU", "123E"),
                        new ScraperResultsRequest.TrackingEventDto(Instant.parse("2026-11-12T03:00:00Z"),
                                "Discharged", "Shanghai", null, null)
                ));
        return new ScraperResultsRequest.ContainerResult(
                number, "MSC", "OK", 1, 8200L, Instant.now(), null, null, true,
                "https://www.msc.com/api/tracking/" + number, data);
    }

    private static ScraperResultsRequest.ContainerResult failed(String number, String status, String code) {
        return new ScraperResultsRequest.ContainerResult(
                number, "MAERSK", status, 2, 60000L, Instant.now(), code,
                "detalle del fallo", true, null, null);
    }

    private static ScraperResultsRequest request(String runId, ScraperResultsRequest.ContainerResult... results) {
        return new ScraperResultsRequest(runId, "schedule", Instant.now(), Instant.now(), 900L,
                List.of("MSC", "MAERSK"), null, List.of(results), null);
    }

    @Test
    @DisplayName("Un resultado OK persiste eventos, ETA y estado actual")
    void ingestsOkResult() {
        repository.ensureExists(List.of("MSCU1234567"));

        IngestResponse response = service.ingest(request("run-1", ok("MSCU1234567")));

        assertThat(response.updated()).isEqualTo(1);
        assertThat(response.unknown()).isZero();

        ContainerTracking saved = repository.findByContainerNumber("MSCU1234567").orElseThrow();
        assertThat(saved.status()).isEqualTo(TrackingStatus.OK);
        assertThat(saved.currentStatus()).isEqualTo("Discharged");
        assertThat(saved.events()).hasSize(2);
        assertThat(saved.eta()).isEqualTo(Instant.parse("2026-11-30T00:00:00Z"));
        assertThat(saved.lastEventAt()).isEqualTo(Instant.parse("2026-11-12T03:00:00Z"));
        assertThat(saved.attempts()).isEqualTo(1);
        assertThat(saved.nextRetryAt()).isNull();
        assertThat(saved.runId()).isEqualTo("run-1");
    }

    @Test
    @DisplayName("Un fallo transitorio programa backoff y no borra los datos previos")
    void keepsPreviousDataOnTransientFailure() {
        repository.ensureExists(List.of("MSCU1234567"));
        service.ingest(request("run-1", ok("MSCU1234567")));

        service.ingest(request("run-2", failed("MSCU1234567", "BLOCKED", "ANTIBOT_CHALLENGE")));

        ContainerTracking saved = repository.findByContainerNumber("MSCU1234567").orElseThrow();
        assertThat(saved.status()).isEqualTo(TrackingStatus.BLOCKED);
        assertThat(saved.errorCode()).isEqualTo("ANTIBOT_CHALLENGE");
        // Los datos buenos siguen ahí: el frontend no pierde el último estado conocido.
        assertThat(saved.currentStatus()).isEqualTo("Discharged");
        assertThat(saved.events()).hasSize(2);
        assertThat(saved.attempts()).isEqualTo(2);
        assertThat(saved.nextRetryAt()).isAfter(Instant.now());
    }

    @Test
    @DisplayName("NOT_FOUND es terminal: deja de estar pendiente")
    void notFoundIsTerminal() {
        repository.ensureExists(List.of("MRKU7654321"));
        service.ingest(request("run-1", failed("MRKU7654321", "NOT_FOUND", "NOT_FOUND")));

        ContainerTracking saved = repository.findByContainerNumber("MRKU7654321").orElseThrow();
        assertThat(saved.status()).isEqualTo(TrackingStatus.NOT_FOUND);
        assertThat(saved.isDue(Instant.now())).isFalse();
        assertThat(repository.findDue(Instant.now(), 10, Duration.ofHours(20))).isEmpty();
    }

    @Test
    @DisplayName("Un fallo sobre un contenedor no registrado se reporta como desconocido")
    void reportsUnknownContainers() {
        IngestResponse response = service.ingest(
                request("run-1", failed("HLCU1111111", "TIMEOUT", "NO_RESPONSE_CAPTURED")));

        assertThat(response.updated()).isZero();
        assertThat(response.unknownContainers()).containsExactly("HLCU1111111");
    }

    @Test
    @DisplayName("El backoff crece exponencialmente y se corta a los 7 días")
    void backoffGrowsAndIsCapped() {
        assertThat(TrackingIngestService.backoffFor(1)).isEqualTo(Duration.ofHours(6));
        assertThat(TrackingIngestService.backoffFor(2)).isEqualTo(Duration.ofHours(12));
        assertThat(TrackingIngestService.backoffFor(3)).isEqualTo(Duration.ofHours(24));
        assertThat(TrackingIngestService.backoffFor(9)).isEqualTo(Duration.ofDays(7));
    }

    @Test
    @DisplayName("Un estado desconocido en el wire se degrada a ERROR, no revienta")
    void unknownWireStatusDegradesToError() {
        assertThat(TrackingStatus.fromWire("WAT")).isEqualTo(TrackingStatus.ERROR);
        assertThat(TrackingStatus.fromWire(null)).isEqualTo(TrackingStatus.ERROR);
        assertThat(TrackingStatus.fromWire(" ok ")).isEqualTo(TrackingStatus.OK);
    }
}
