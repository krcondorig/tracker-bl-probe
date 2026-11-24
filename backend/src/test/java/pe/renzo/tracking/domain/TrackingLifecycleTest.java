package pe.renzo.tracking.domain;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.time.Instant;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * La política de refresco es la que evita consultar el mismo contenedor todos
 * los días para siempre. Si esto se rompe, el cron gasta minutos y quema la IP.
 */
class TrackingLifecycleTest {

    private static final Duration RECHECK = Duration.ofHours(20);

    private static ContainerTracking with(Instant lastAttemptAt, TrackingStatus status, String currentStatus) {
        Instant created = Instant.parse("2026-11-01T00:00:00Z");
        return new ContainerTracking(
                "MSCU1234567", "MSC", status, currentStatus, "Callao", null, null,
                null, false, null, null, null, null, List.of(), 1, lastAttemptAt, null, null,
                null, "run-1", null, true, created, created);
    }

    @Test
    @DisplayName("Un envío entregado sale de la cola definitivamente")
    void deliveredShipmentStopsBeingPolled() {
        ContainerTracking tracking = with(
                Instant.now().minus(Duration.ofDays(30)),
                TrackingStatus.OK,
                "Empty container returned to shipper");

        assertThat(tracking.needsTracking(Instant.now(), RECHECK)).isFalse();
    }

    @Test
    @DisplayName("Entregado en español también cierra el ciclo")
    void spanishDeliveredStatusIsRecognised() {
        assertThat(TrackingLifecycle.isShipmentClosed("Contenedor entregado")).isTrue();
        assertThat(TrackingLifecycle.isShipmentClosed("Vacío devuelto")).isTrue();
        assertThat(TrackingLifecycle.isShipmentClosed("DISCHARGED")).isFalse();
        assertThat(TrackingLifecycle.isShipmentClosed(null)).isFalse();
    }

    @Test
    @DisplayName("Un contenedor OK no se repite dentro del intervalo de refresco")
    void okContainerIsNotPolledTwiceInTheSameWindow() {
        ContainerTracking justChecked = with(Instant.now().minus(Duration.ofHours(2)),
                TrackingStatus.OK, "Discharged");
        assertThat(justChecked.needsTracking(Instant.now(), RECHECK)).isFalse();
    }

    @Test
    @DisplayName("Un contenedor OK se refresca cuando vence el intervalo")
    void okContainerIsPolledAgainAfterInterval() {
        ContainerTracking stale = with(Instant.now().minus(Duration.ofHours(21)),
                TrackingStatus.OK, "Discharged");
        assertThat(stale.needsTracking(Instant.now(), RECHECK)).isTrue();
    }

    @Test
    @DisplayName("Un fallo con backoff activo no se reintenta antes de tiempo")
    void transientFailureRespectsBackoff() {
        Instant now = Instant.now();
        ContainerTracking blocked = new ContainerTracking(
                "MSCU1234567", "MSC", TrackingStatus.BLOCKED, "Discharged", "Callao", null, null,
                null, false, null, null, null, null, List.of(), 2, now,
                now.plus(Duration.ofHours(6)), "ANTIBOT_CHALLENGE", null, "run-1", null, true,
                now, now);

        assertThat(blocked.needsTracking(now, RECHECK)).isFalse();
        assertThat(blocked.needsTracking(now.plus(Duration.ofHours(7)), RECHECK)).isTrue();
    }

    @Test
    @DisplayName("Un contenedor nunca consultado siempre entra en la cola")
    void neverAttemptedIsAlwaysDue() {
        ContainerTracking fresh = ContainerTracking.pending("MSCU1234567", "MSC", Instant.now());
        assertThat(fresh.needsTracking(Instant.now(), RECHECK)).isTrue();
    }

    @Test
    @DisplayName("El repositorio aplica la política y respeta el límite")
    void repositoryAppliesPolicy() {
        InMemoryContainerTrackingRepository repo = new InMemoryContainerTrackingRepository();
        repo.ensureExists(List.of("MSCU1234567", "MAEU7654321"));
        Instant now = Instant.now();

        repo.save(new ContainerTracking("MSCU1234567", "MSC", TrackingStatus.OK, "Discharged",
                null, null, null, null, false, null, null, null, null, List.of(), 1, now,
                null, null, null, "r", null, true, now, now));
        repo.save(new ContainerTracking("MAEU7654321", "MAERSK", TrackingStatus.OK, "Delivered",
                null, null, null, null, false, null, null, null, null, List.of(), 1,
                now.minus(Duration.ofDays(9)), null, null, null, "r", null, true, now, now));

        assertThat(repo.findDue(now, 10, RECHECK)).isEmpty();
        assertThat(repo.findDue(now.plus(Duration.ofHours(21)), 10, RECHECK))
                .extracting(ContainerTracking::containerNumber)
                .containsExactly("MSCU1234567");
    }
}
