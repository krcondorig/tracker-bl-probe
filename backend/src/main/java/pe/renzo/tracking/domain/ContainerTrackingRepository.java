package pe.renzo.tracking.domain;

import java.time.Instant;
import java.util.List;
import java.util.Optional;

/**
 * Puerto de persistencia. La implementación en memoria permite arrancar el
 * ejemplo sin base de datos; sustituirla por Spring Data JPA es un cambio de
 * una sola clase.
 */
public interface ContainerTrackingRepository {

    Optional<ContainerTracking> findByContainerNumber(String containerNumber);

    /** Guarda o reemplaza por número de contenedor. */
    ContainerTracking save(ContainerTracking tracking);

    /**
     * Contenedores que el scraper debe consultar ahora: pendientes, fallos
     * transitorios cuyo backoff ya venció y envíos activos cuyo último chequeo
     * superó el intervalo de refresco.
     */
    List<ContainerTracking> findDue(Instant now, int limit, java.time.Duration recheckInterval);

    /** Alta de contenedores si no existen. Devuelve cuántos se crearon. */
    int ensureExists(List<String> containerNumbers);

    List<ContainerTracking> findAll();
}
