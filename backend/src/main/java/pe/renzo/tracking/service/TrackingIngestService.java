package pe.renzo.tracking.service;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import pe.renzo.tracking.api.dto.IngestResponse;
import pe.renzo.tracking.api.dto.ScraperResultsRequest;
import pe.renzo.tracking.domain.ContainerTracking;
import pe.renzo.tracking.domain.ContainerTrackingRepository;
import pe.renzo.tracking.domain.TrackingEvent;
import pe.renzo.tracking.domain.TrackingStatus;

import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;

/**
 * Aplica el resultado de una ejecución del scraper al estado persistido.
 *
 * <p>Reglas de negocio (y el porqué):
 * <ul>
 *   <li><b>OK</b> sustituye los eventos completos: la naviera es la fuente de
 *       verdad y sus payloads son acumulativos.</li>
 *   <li><b>NOT_FOUND / INVALID</b> son terminales: dejar de reintentar evita
 *       quemar consultas en números equivocados.</li>
 *   <li><b>BLOCKED / TIMEOUT / ERROR</b> programan el siguiente intento con
 *       backoff exponencial. Sin backoff, un bloqueo persistente reintentaría
 *       cada día con la misma IP y agravaría el bloqueo.</li>
 *   <li>Un fallo <i>nunca</i> borra datos ya buenos: si el contenedor estaba OK
 *       y ahora viene BLOCKED, se conserva el último estado conocido y solo se
 *       anota el error.</li>
 * </ul>
 */
@Service
public class TrackingIngestService {

    private static final Logger log = LoggerFactory.getLogger(TrackingIngestService.class);
    private static final Duration BASE_BACKOFF = Duration.ofHours(6);
    private static final Duration MAX_BACKOFF = Duration.ofDays(7);

    private final ContainerTrackingRepository repository;

    public TrackingIngestService(ContainerTrackingRepository repository) {
        this.repository = repository;
    }

    public IngestResponse ingest(ScraperResultsRequest request) {
        Instant now = Instant.now();
        int updated = 0;
        List<String> unknown = new ArrayList<>();

        for (ScraperResultsRequest.ContainerResult result : request.results()) {
            String number = normalise(result.containerNumber());
            if (number == null) {
                continue;
            }
            ContainerTracking existing = repository.findByContainerNumber(number).orElse(null);
            if (existing == null && result.data() == null) {
                // Un fallo sobre un contenedor que no tenemos registrado no aporta nada.
                unknown.add(number);
                continue;
            }

            ContainerTracking base = (existing != null)
                    ? existing
                    : ContainerTracking.pending(number, result.carrier(), now);

            repository.save(apply(base, result, request.runId(), now));
            updated++;
        }

        log.info("Ingesta run={} trigger={}: {} resultados, {} actualizados, {} desconocidos",
                request.runId(), request.trigger(), request.results().size(), updated, unknown.size());

        return new IngestResponse(
                request.runId(),
                request.results().size(),
                updated,
                unknown.size(),
                List.copyOf(unknown),
                now
        );
    }

    private ContainerTracking apply(
            ContainerTracking base,
            ScraperResultsRequest.ContainerResult result,
            String runId,
            Instant now) {

        TrackingStatus status = TrackingStatus.fromWire(result.status());
        int attempts = base.attempts() + 1;
        boolean hasFreshData = result.data() != null && status == TrackingStatus.OK;

        List<TrackingEvent> events = hasFreshData
                ? result.data().events().stream()
                        .map(e -> new TrackingEvent(e.timestamp(), e.status(), e.location(), e.vessel(), e.voyage()))
                        .toList()
                : base.events();

        // El estado previo solo se pisa cuando la ejecución trae datos.
        String currentStatus = hasFreshData ? result.data().currentStatus() : base.currentStatus();
        String location = hasFreshData ? result.data().location() : base.location();
        String vessel = hasFreshData ? result.data().vessel() : base.vessel();
        String voyage = hasFreshData ? result.data().voyage() : base.voyage();
        Instant eta = hasFreshData ? result.data().eta() : base.eta();

        // La llegada es un hecho pasado e irreversible: cuando el buque atracó,
        // ya no puede "desatracar". Por eso, a diferencia del resto de campos, se
        // conserva aunque la consulta de hoy falle o aunque el evento de arribo ya
        // no venga en el payload (las navieras solo devuelven una ventana reciente).
        // Sin esto, `arrived` volvía a false en la siguiente ejecución y el
        // backend volvía a decir "en tránsito" para un barco que ya estaba aquí.
        boolean freshArrived = hasFreshData && result.data().arrived();
        boolean arrived = base.arrived() || freshArrived;
        Instant arrivalAt = firstNonNull(
                hasFreshData ? result.data().arrivalDate() : null, base.arrivalAt());
        String dischargePort = firstNonBlank(
                hasFreshData ? result.data().dischargePort() : null, base.dischargePort());
        Instant finalDeliveryEta = firstNonNull(
                hasFreshData ? result.data().finalDeliveryEta() : null, base.finalDeliveryEta());

        Instant lastEventAt = hasFreshData ? lastEventTimestamp(result) : base.lastEventAt();
        String sourceUrl = hasFreshData ? result.sourceUrl() : base.sourceUrl();

        // Si ya había eventos y el estado nuevo es igual, se conserva la fecha
        // original del evento: así el frontend puede mostrar "sin cambios desde".
        Instant lastAttemptAt = now;
        Instant nextRetryAt = status.isRetryable()
                ? now.plus(backoffFor(attempts))
                : null;

        return new ContainerTracking(
                base.containerNumber(),
                isBlank(result.carrier()) ? base.carrier() : result.carrier(),
                status,
                currentStatus,
                location,
                vessel,
                voyage,
                eta,
                arrived,
                arrivalAt,
                dischargePort,
                finalDeliveryEta,
                lastEventAt,
                events,
                attempts,
                lastAttemptAt,
                nextRetryAt,
                status == TrackingStatus.OK ? null : result.errorCode(),
                status == TrackingStatus.OK ? null : truncate(result.errorMessage(), 900),
                runId,
                sourceUrl,
                Boolean.TRUE.equals(result.checkDigitValid()),
                base.createdAt() == null ? now : base.createdAt(),
                now
        );
    }

    private static Instant lastEventTimestamp(ScraperResultsRequest.ContainerResult result) {
        Instant fromData = result.data() != null ? result.data().lastUpdate() : null;
        if (fromData != null) {
            return fromData;
        }
        return result.data() == null ? null : result.data().events().stream()
                .map(ScraperResultsRequest.TrackingEventDto::timestamp)
                .filter(java.util.Objects::nonNull)
                .max(Instant::compareTo)
                .orElse(null);
    }

    static Duration backoffFor(int attempts) {
        // 6h, 12h, 24h, 48h... con techo de 7 días.
        long multiplier = 1L << Math.min(Math.max(attempts - 1, 0), 5);
        Duration candidate = BASE_BACKOFF.multipliedBy(multiplier);
        return candidate.compareTo(MAX_BACKOFF) > 0 ? MAX_BACKOFF : candidate;
    }

    /** Conserva el valor ya conocido cuando el payload nuevo no lo trae. */
    private static Instant firstNonNull(Instant candidate, Instant fallback) {
        return candidate != null ? candidate : fallback;
    }

    /** Igual que {@link #firstNonNull} para textos: un campo vacío no borra el previo. */
    private static String firstNonBlank(String candidate, String fallback) {
        return isBlank(candidate) ? fallback : candidate;
    }

    private static String normalise(String value) {
        if (value == null || value.isBlank()) {
            return null;
        }
        return value.trim().toUpperCase();
    }

    private static boolean isBlank(String value) {
        return value == null || value.isBlank();
    }

    private static String truncate(String value, int max) {
        if (value == null) {
            return null;
        }
        return value.length() <= max ? value : value.substring(0, max);
    }
}
