package pe.renzo.tracking.service;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatusCode;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Service;
import org.springframework.web.client.ResourceAccessException;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientResponseException;
import pe.renzo.tracking.api.dto.DispatchRequest;
import pe.renzo.tracking.config.TrackingProperties;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Dispara el workflow de GitHub Actions por la API REST ({@code workflow_dispatch}).
 *
 * <p>La API v3 exige: endpoint
 * {@code POST /repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches},
 * header {@code Accept: application/vnd.github+json}, {@code ref} en el body y un
 * token con permiso {@code actions:write}. El {@code workflow_id} puede ser el
 * nombre del archivo ({@code scraper.yml}) o su id numérico.
 *
 * <p>Respuesta correcta: {@code 204 No Content} sin cuerpo. Cualquier otro
 * código se traduce a {@link GitHubDispatchException} con un mensaje que
 * distingue "token sin permiso" de "workflow inexistente", porque son los dos
 * errores que se cometen al configurar esto.
 */
@Service
public class GitHubActionsService {

    public static final String API_VERSION = "2022-11-28";
    private static final Logger log = LoggerFactory.getLogger(GitHubActionsService.class);

    private final RestClient gitHubRestClient;
    private final TrackingProperties properties;

    public GitHubActionsService(RestClient gitHubRestClient, TrackingProperties properties) {
        this.gitHubRestClient = gitHubRestClient;
        this.properties = properties;
    }

    public boolean isConfigured() {
        return properties.github().isConfigured();
    }

    /** Lanza el workflow. Devuelve el id de ejecución si GitHub lo expone en el header. */
    public DispatchOutcome dispatch(DispatchRequest request) {
        TrackingProperties.Github github = properties.github();
        if (!github.isConfigured()) {
            throw GitHubDispatchException.configuration(
                    "Faltan tracking.github.owner/repo/token. Configura GITHUB_DISPATCH_TOKEN "
                            + "con permiso 'actions:write' sobre el repositorio del scraper.");
        }

        List<String> containers = request.containers() == null ? List.of() : request.containers().stream()
                .map(String::trim)
                .map(String::toUpperCase)
                .filter(value -> !value.isEmpty())
                .distinct()
                .toList();

        if (containers.size() > properties.scraper().maxBatch()) {
            throw new IllegalArgumentException(
                    "Se pidieron %d contenedores; el máximo por ejecución es %d"
                            .formatted(containers.size(), properties.scraper().maxBatch()));
        }

        Map<String, Object> inputs = new LinkedHashMap<>();
        // La API de GitHub solo transporta strings en 'inputs' de tipo string
        // (y booleanos nativos para type: boolean). Se envían en el tipo declarado
        // en scraper.yml para no recibir 422.
        inputs.put("containers", String.join(",", containers));
        inputs.put("carriers", String.join(",", normaliseCarriers(request.carriers())));
        inputs.put("max_containers", String.valueOf(effectiveLimit(request.maxContainers())));
        inputs.put("dry_run", Boolean.TRUE.equals(request.dryRun()));
        inputs.put("use_warp", Boolean.TRUE);

        Map<String, Object> body = new LinkedHashMap<>();
        body.put("ref", (request.ref() == null || request.ref().isBlank()) ? github.ref() : request.ref().trim());
        body.put("inputs", inputs);

        String path = "/repos/{owner}/{repo}/actions/workflows/{workflow}/dispatches";

        try {
            var response = gitHubRestClient.post()
                    .uri(path, github.owner(), github.repo(), github.workflowFile())
                    .contentType(MediaType.APPLICATION_JSON)
                    .header("Authorization", "Bearer " + github.token())
                    .body(body)
                    .retrieve()
                    // 204 en éxito; 200/201 también se aceptan por si GitHub cambia.
                    .onStatus(HttpStatusCode::isError, (req, res) -> {
                        throw GitHubDispatchException.upstream(
                                explain(res.getStatusCode().value()), null);
                    })
                    .toBodilessEntity();

            String runId = response.getHeaders().getFirst("X-GitHub-Request-Id");
            log.info("workflow_dispatch aceptado: repo={}/{}, workflow={}, ref={}, contenedores={}, requestId={}",
                    github.owner(), github.repo(), github.workflowFile(), body.get("ref"),
                    containers.size(), runId);
            return new DispatchOutcome(containers.size(), String.valueOf(body.get("ref")), runId);

        } catch (RestClientResponseException ex) {
            throw GitHubDispatchException.upstream(explain(ex.getStatusCode().value()), ex);
        } catch (ResourceAccessException ex) {
            throw GitHubDispatchException.upstream(
                    "No se pudo contactar con api.github.com (timeout o DNS): " + ex.getMessage(), ex);
        }
    }

    private int effectiveLimit(Integer requested) {
        int configured = properties.scraper().maxBatch();
        if (requested == null || requested <= 0) {
            return configured;
        }
        return Math.min(requested, configured);
    }

    private List<String> normaliseCarriers(List<String> carriers) {
        List<String> whitelist = properties.scraper().carrierWhitelist();
        List<String> requested = (carriers == null || carriers.isEmpty()) ? whitelist : carriers;
        List<String> cleaned = requested.stream()
                .map(value -> value == null ? "" : value.trim().toUpperCase())
                .filter(whitelist::contains)
                .distinct()
                .toList();
        if (cleaned.isEmpty()) {
            throw new IllegalArgumentException(
                    "Ninguna naviera es válida. Permitidas: " + String.join(", ", whitelist));
        }
        return cleaned;
    }

    private static String explain(int status) {
        return switch (status) {
            case 401 -> "GitHub devolvió 401: el token es inválido, está caducado o no se envió.";
            case 403 -> "GitHub devolvió 403: al token le falta el permiso 'actions:write' o la "
                    + "organización lo bloquea por SSO.";
            case 404 -> "GitHub devolvió 404: owner/repo/workflow no existen, o el token no tiene "
                    + "visibilidad sobre el repositorio privado.";
            case 422 -> "GitHub devolvió 422: los inputs no coinciden con los declarados en scraper.yml "
                    + "(revisa nombres y tipos) o la rama 'ref' no existe.";
            default -> "GitHub respondió HTTP " + status + " al solicitar workflow_dispatch.";
        };
    }

    public record DispatchOutcome(int containersQueued, String ref, String gitHubRequestId) {
    }
}
