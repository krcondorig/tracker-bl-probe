package pe.renzo.tracking.config;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

/**
 * Exige {@code Authorization: Bearer <shared-secret>} en las rutas que usa el
 * scraper.
 *
 * <p>El webhook llega desde un runner de GitHub, es decir, desde una IP pública
 * cualquiera: un token compartido es la única barrera práctica. La comparación
 * es en tiempo constante para no filtrar el secreto por temporización.
 */
@Component
public class ScraperTokenFilter extends OncePerRequestFilter {

    private static final String PREFIX = "/api/v1/scraper/";

    private final TrackingProperties properties;

    public ScraperTokenFilter(TrackingProperties properties) {
        this.properties = properties;
    }

    @Override
    protected boolean shouldNotFilter(HttpServletRequest request) {
        // Solo se protege el prefijo del scraper; el resto de la API usa su
        // propia seguridad (Spring Security / gateway).
        // La comparación cubre también la ruta sin barra final: si no, un
        // POST a ".../api/v1/scraper" se saltaría el filtro.
        String uri = request.getRequestURI();
        String prefix = PREFIX.substring(0, PREFIX.length() - 1); // "/api/v1/scraper"
        return !(uri.equals(prefix) || uri.startsWith(PREFIX));
    }

    @Override
    protected void doFilterInternal(
            HttpServletRequest request,
            HttpServletResponse response,
            FilterChain chain) throws ServletException, IOException {

        String configured = properties.scraper().sharedSecret();
        if (configured == null || configured.isBlank()) {
            // Sin secreto configurado se falla cerrado: es preferible un 503
            // visible en el workflow a un webhook abierto al mundo.
            reject(response, HttpStatus.SERVICE_UNAVAILABLE,
                    "SCRAPER_SHARED_SECRET no configurado en el backend");
            return;
        }

        String header = request.getHeader("Authorization");
        if (header == null || !header.startsWith("Bearer ")) {
            reject(response, HttpStatus.UNAUTHORIZED, "Falta el header Authorization: Bearer");
            return;
        }

        String presented = header.substring("Bearer ".length()).trim();
        if (!constantTimeEquals(configured, presented)) {
            reject(response, HttpStatus.UNAUTHORIZED, "Token inválido");
            return;
        }

        chain.doFilter(request, response);
    }

    private static boolean constantTimeEquals(String expected, String actual) {
        return MessageDigest.isEqual(
                expected.getBytes(StandardCharsets.UTF_8),
                actual.getBytes(StandardCharsets.UTF_8));
    }

    private static void reject(HttpServletResponse response, HttpStatus status, String message)
            throws IOException {
        response.setStatus(status.value());
        response.setContentType(MediaType.APPLICATION_JSON_VALUE);
        response.setCharacterEncoding(StandardCharsets.UTF_8.name());
        response.getWriter().write("{\"code\":\"UNAUTHORIZED\",\"message\":\"%s\"}".formatted(message));
    }
}
