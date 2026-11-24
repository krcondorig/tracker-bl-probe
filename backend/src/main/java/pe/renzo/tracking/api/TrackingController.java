package pe.renzo.tracking.api;

import jakarta.validation.Valid;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import pe.renzo.tracking.api.dto.DispatchRequest;
import pe.renzo.tracking.api.dto.DispatchResponse;
import pe.renzo.tracking.api.dto.IngestResponse;
import pe.renzo.tracking.api.dto.PendingContainersResponse;
import pe.renzo.tracking.api.dto.ScraperResultsRequest;
import pe.renzo.tracking.api.dto.TrackingView;
import pe.renzo.tracking.domain.ContainerTrackingRepository;
import pe.renzo.tracking.service.GitHubActionsService;
import pe.renzo.tracking.service.PendingContainersService;
import pe.renzo.tracking.service.TrackingIngestService;

import java.time.Instant;
import java.util.List;

/**
 * Superficie HTTP del orquestador.
 *
 * <pre>
 * POST /api/v1/tracking/dispatch   -> dispara workflow_dispatch en GitHub
 * GET  /api/v1/scraper/pendientes  -> lista que consume scraper.py (Bearer)
 * POST /api/v1/scraper/results     -> webhook que recibe scraper.py (Bearer)
 * GET  /api/v1/tracking/{number}   -> consulta individual
 * GET  /api/v1/tracking            -> listado completo
 * </pre>
 *
 * <p>Las dos rutas bajo {@code /scraper/} son las que viajan en el workflow y
 * están protegidas por {@link pe.renzo.tracking.config.ScraperTokenFilter}.
 */
@RestController
@RequestMapping("/api/v1")
public class TrackingController {

    private final GitHubActionsService gitHubActionsService;
    private final PendingContainersService pendingContainersService;
    private final TrackingIngestService ingestService;
    private final ContainerTrackingRepository repository;

    public TrackingController(
            GitHubActionsService gitHubActionsService,
            PendingContainersService pendingContainersService,
            TrackingIngestService ingestService,
            ContainerTrackingRepository repository) {
        this.gitHubActionsService = gitHubActionsService;
        this.pendingContainersService = pendingContainersService;
        this.ingestService = ingestService;
        this.repository = repository;
    }

    // ---------------------------------------------------------------- dispatch
    @PostMapping("/tracking/dispatch")
    public ResponseEntity<DispatchResponse> dispatch(@Valid @RequestBody DispatchRequest request) {
        if (request.containers() != null && !request.containers().isEmpty()) {
            repository.ensureExists(request.containers());
        }

        var outcome = gitHubActionsService.dispatch(request);
        return ResponseEntity.status(HttpStatus.ACCEPTED).body(new DispatchResponse(
                true,
                "scraper.yml",
                outcome.ref(),
                "Ejecución encolada en GitHub Actions. Los resultados llegarán al webhook.",
                Instant.now(),
                outcome.containersQueued()
        ));
    }

    // -------------------------------------------------------------- pendientes
    /**
     * Contrato mínimo: {@code {"containers":[{"containerNumber":"...","carrier":"...","attempts":0}]}}.
     *
     * <p>Se devuelve 200 con lista vacía (y no 204) cuando no hay pendientes: el
     * script distingue "no hay nada" de "el endpoint falló" por el código HTTP,
     * y un 204 no lleva cuerpo que loguear.
     */
    @GetMapping("/scraper/pendientes")
    public PendingContainersResponse pendientes(@RequestParam(required = false) Integer limit) {
        return pendingContainersService.pending(limit);
    }

    // ----------------------------------------------------------------- webhook
    /**
     * Acepta las dos formas de URL que un operador puede configurar en
     * {@code SPRING_WEBHOOK_URL}:
     * <ul>
     *   <li>{@code .../api/v1/scraper}  → el script hace POST a la raíz</li>
     *   <li>{@code .../api/v1/scraper/results} → endpoint explícito</li>
     * </ul>
     * Sin esta doble ruta, configurar la base con prefijo producía un 405 difícil
     * de diagnosticar desde el log del workflow.
     */
    @PostMapping({"/scraper/results", "/scraper", "/scraper/"})
    public ResponseEntity<IngestResponse> results(@RequestBody ScraperResultsRequest request) {
        if (request.results().isEmpty()) {
            // Un lote vacío es válido (no había pendientes), pero no se reescribe nada.
            return ResponseEntity.ok(new IngestResponse(
                    request.runId(), 0, 0, 0, List.of(), Instant.now()));
        }
        return ResponseEntity.ok(ingestService.ingest(request));
    }

    // -------------------------------------------------------------- consultas
    @GetMapping("/tracking/{containerNumber}")
    public ResponseEntity<TrackingView> byContainer(@PathVariable String containerNumber) {
        return repository.findByContainerNumber(containerNumber)
                .map(TrackingView::from)
                .map(ResponseEntity::ok)
                .orElseGet(() -> ResponseEntity.notFound().build());
    }

    @GetMapping("/tracking")
    public List<TrackingView> all() {
        return TrackingView.from(repository.findAll());
    }

    /**
     * Alta de contenedores para que el próximo cron los incluya. Útil cuando el
     * envío se crea en otro microservicio y no se quiere esperar al despacho.
     */
    @PostMapping("/tracking/register")
    public ResponseEntity<List<TrackingView>> register(@RequestBody List<String> containerNumbers) {
        repository.ensureExists(containerNumbers);
        List<TrackingView> views = containerNumbers.stream()
                .map(value -> value == null ? "" : value.trim().toUpperCase())
                .map(repository::findByContainerNumber)
                .flatMap(java.util.Optional::stream)
                .map(TrackingView::from)
                .toList();
        return ResponseEntity.status(HttpStatus.CREATED).body(views);
    }
}
