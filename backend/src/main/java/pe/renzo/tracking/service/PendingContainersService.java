package pe.renzo.tracking.service;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import pe.renzo.tracking.api.dto.PendingContainersResponse;
import pe.renzo.tracking.config.TrackingProperties;
import pe.renzo.tracking.domain.ContainerTracking;
import pe.renzo.tracking.domain.ContainerTrackingRepository;

import java.time.Instant;
import java.util.List;

/**
 * Selecciona qué contenedores debe consultar el scraper en esta ejecución.
 *
 * <p>La selección se hace en el backend (y no en el script) porque el backend es
 * quien conoce los reintentos, los backoffs y qué contenedores son relevantes
 * (por ejemplo, solo los de envíos activos). Así el workflow programado no
 * necesita parámetros: pide la lista y la procesa.
 */
@Service
public class PendingContainersService {

    private static final Logger log = LoggerFactory.getLogger(PendingContainersService.class);

    private final ContainerTrackingRepository repository;
    private final TrackingProperties properties;

    public PendingContainersService(ContainerTrackingRepository repository, TrackingProperties properties) {
        this.repository = repository;
        this.properties = properties;
    }

    public PendingContainersResponse pending(Integer limit) {
        int effective = (limit == null || limit <= 0)
                ? properties.scraper().maxBatch()
                : Math.min(limit, properties.scraper().maxBatch());

        Instant now = Instant.now();
        List<ContainerTracking> due = repository.findDue(now, effective, properties.scraper().recheckInterval());

        List<PendingContainersResponse.Item> items = due.stream()
                .map(t -> new PendingContainersResponse.Item(t.containerNumber(), t.carrier(), t.attempts()))
                .toList();

        log.debug("Pendientes solicitados limit={} -> {} contenedores", effective, items.size());
        return PendingContainersResponse.of(items);
    }
}
