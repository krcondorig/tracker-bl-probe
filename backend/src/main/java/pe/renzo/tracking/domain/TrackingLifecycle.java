package pe.renzo.tracking.domain;

import java.util.Locale;
import java.util.regex.Pattern;

/**
 * Decide cuándo un envío deja de necesitar consultas.
 *
 * <p>Sin esta política, un contenedor en estado {@code OK} se vuelve a consultar
 * en cada ejecución del cron para siempre. Eso tiene dos costos reales: minutos
 * de GitHub Actions y, más grave, un patrón de tráfico repetido contra la misma
 * naviera que acaba en bloqueo de IP.
 */
public final class TrackingLifecycle {

    /**
     * Estados que las navieras usan para cerrar el ciclo del contenedor.
     * Se comparan sobre el texto ya normalizado (minúsculas, sin acentos).
     */
    private static final Pattern CLOSED_STATUS = Pattern.compile(
            "(delivered|empty\\s+returned|returned\\s+to\\s+(?:shipper|origin)"
            + "|container\\s+released|gate\\s*-?\\s*in\\s+empty"
            + "|empty\\s+(?:container\\s+)?(?:returned|received|pick\\s*-?\\s*up)"
            + "|entregado|devuelto\\s+vacio|vacio\\s+devuelto)"
    );

    private TrackingLifecycle() {
    }

    /** ¿El estado reportado indica que el contenedor ya terminó su ciclo? */
    public static boolean isShipmentClosed(String currentStatus) {
        if (currentStatus == null || currentStatus.isBlank()) {
            return false;
        }
        String normalized = java.text.Normalizer.normalize(currentStatus, java.text.Normalizer.Form.NFD)
                .replaceAll("\\p{M}", "")
                .toLowerCase(Locale.ROOT)
                .replaceAll("\\s+", " ")
                .trim();
        return CLOSED_STATUS.matcher(normalized).find();
    }
}
