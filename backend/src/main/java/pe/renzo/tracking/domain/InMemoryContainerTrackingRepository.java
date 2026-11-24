package pe.renzo.tracking.domain;

import org.springframework.stereotype.Repository;

import java.time.Instant;
import java.util.Comparator;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.ConcurrentHashMap;

@Repository
public class InMemoryContainerTrackingRepository implements ContainerTrackingRepository {

    private final Map<String, ContainerTracking> store = new ConcurrentHashMap<>();

    @Override
    public Optional<ContainerTracking> findByContainerNumber(String containerNumber) {
        if (containerNumber == null || containerNumber.isBlank()) {
            return Optional.empty();
        }
        return Optional.ofNullable(store.get(containerNumber.trim().toUpperCase()));
    }

    @Override
    public ContainerTracking save(ContainerTracking tracking) {
        store.put(tracking.containerNumber(), tracking);
        return tracking;
    }

    @Override
    public List<ContainerTracking> findDue(Instant now, int limit, java.time.Duration recheckInterval) {
        return store.values().stream()
                .filter(tracking -> tracking.needsTracking(now, recheckInterval))
                // Nunca consultados primero; luego los que llevan más tiempo
                // esperando. Evita que un contenedor con bloqueos repetidos
                // acapare todas las ejecuciones.
                .sorted(Comparator
                        .comparing((ContainerTracking t) -> t.lastAttemptAt() == null ? Instant.EPOCH : t.lastAttemptAt())
                        .thenComparing(ContainerTracking::containerNumber))
                .limit(Math.max(0, limit))
                .toList();
    }

    @Override
    public int ensureExists(List<String> containerNumbers) {
        int created = 0;
        Instant now = Instant.now();
        for (String raw : containerNumbers) {
            if (raw == null || raw.isBlank()) {
                continue;
            }
            String number = raw.trim().toUpperCase();
            if (store.putIfAbsent(number, ContainerTracking.pending(number, carrierOf(number), now)) == null) {
                created++;
            }
        }
        return created;
    }

    @Override
    public List<ContainerTracking> findAll() {
        return store.values().stream()
                .sorted(Comparator.comparing(ContainerTracking::containerNumber))
                .toList();
    }

    /** Mismo mapa de prefijos que el scraper (ver tracker/containers.py). */
    static String carrierOf(String containerNumber) {
        if (containerNumber.length() < 4) {
            return "UNKNOWN";
        }
        return switch (containerNumber.substring(0, 4)) {
            case "MSCU", "MEDU", "MSDU", "MSCF", "MSWU", "MSCA" -> "MSC";
            case "MAEU", "MRKU", "MRSU", "MSKU", "MIEU", "APZU", "MSAU" -> "MAERSK";
            case "HLCU", "HLXU", "HLBU", "HLHU", "SUDU", "UACU", "HLRU" -> "HAPAG_LLOYD";
            default -> "UNKNOWN";
        };
    }
}
