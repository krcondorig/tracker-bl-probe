package pe.renzo.tracking.api;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.test.context.TestPropertySource;
import org.springframework.test.web.servlet.MockMvc;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

/**
 * Verifica el contrato exacto que consume scraper.py y que el filtro Bearer
 * falla cerrado.
 */
@SpringBootTest
@AutoConfigureMockMvc
@TestPropertySource(properties = {
        "tracking.scraper.shared-secret=test-secret",
        "tracking.github.token=",
})
class ScraperEndpointsTest {

    private static final String AUTH = "Bearer test-secret";

    @Autowired
    private MockMvc mockMvc;

    @Autowired
    private ObjectMapper objectMapper;

    @Test
    @DisplayName("GET /scraper/pendientes exige Bearer")
    void pendientesRequiresBearer() throws Exception {
        mockMvc.perform(get("/api/v1/scraper/pendientes"))
                .andExpect(status().isUnauthorized());
        mockMvc.perform(get("/api/v1/scraper/pendientes").header("Authorization", "Bearer wrong"))
                .andExpect(status().isUnauthorized());
    }

    @Test
    @DisplayName("GET /scraper/pendientes devuelve la forma {containers:[{containerNumber,...}]}")
    void pendientesReturnsExpectedShape() throws Exception {
        mockMvc.perform(post("/api/v1/tracking/register")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("[\"MSCU1234567\",\"MRKU7654321\"]"))
                .andExpect(status().isCreated());

        mockMvc.perform(get("/api/v1/scraper/pendientes").header("Authorization", AUTH))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.containers").isArray())
                .andExpect(jsonPath("$.containers[0].containerNumber").value("MRKU7654321"))
                .andExpect(jsonPath("$.containers[0].carrier").value("MAERSK"))
                .andExpect(jsonPath("$.containers[0].attempts").value(0));
    }

    @Test
    @DisplayName("POST /scraper/results ingiere el JSON del scraper y actualiza el estado")
    void webhookIngestsResults() throws Exception {
        mockMvc.perform(post("/api/v1/tracking/register")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("[\"MSCU1234567\"]"))
                .andExpect(status().isCreated());

        String payload = """
                {
                  "runId": "abc123",
                  "trigger": "schedule",
                  "startedAt": "2026-11-23T13:00:00.000Z",
                  "finishedAt": "2026-11-23T13:02:30.000Z",
                  "durationMs": 150000,
                  "carrierFilter": ["MSC"],
                  "stats": {"requested": 1, "ok": 1, "notFound": 0, "failed": 0},
                  "results": [
                    {
                      "containerNumber": "MSCU1234567",
                      "carrier": "MSC",
                      "status": "OK",
                      "attempts": 1,
                      "durationMs": 9000,
                      "scrapedAt": "2026-11-23T13:01:00.000Z",
                      "checkDigitValid": true,
                      "sourceUrl": "https://www.msc.com/api/tracking",
                      "data": {
                        "currentStatus": "Discharged",
                        "location": "Callao",
                        "vessel": "MSC PERU",
                        "voyage": "123E",
                        "eta": "2026-11-30T00:00:00Z",
                        "lastUpdate": "2026-11-12T03:00:00Z",
                        "events": [
                          {"timestamp": "2026-11-12T03:00:00Z", "status": "Discharged", "location": "Shanghai"}
                        ]
                      }
                    }
                  ],
                  "meta": {"headless": false}
                }
                """;

        mockMvc.perform(post("/api/v1/scraper/results")
                        .header("Authorization", AUTH)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(payload))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.updated").value(1))
                .andExpect(jsonPath("$.unknown").value(0));

        mockMvc.perform(get("/api/v1/tracking/MSCU1234567"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("OK"))
                .andExpect(jsonPath("$.currentStatus").value("Discharged"))
                .andExpect(jsonPath("$.eventCount").value(1))
                .andExpect(jsonPath("$.runId").value("abc123"));
    }

    @Test
    @DisplayName("El webhook expone si el buque ya llegó al puerto (payload real de Maersk)")
    void webhookExposesArrival() throws Exception {
        mockMvc.perform(post("/api/v1/tracking/register")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("[\"MRKU7779494\"]"))
                .andExpect(status().isCreated());

        // Forma exacta que envía tracker/models.py para el B/L 275792562.
        String payload = """
                {
                  "runId": "arrival-1",
                  "results": [
                    {
                      "containerNumber": "MRKU7779494",
                      "carrier": "MAERSK",
                      "status": "OK",
                      "attempts": 1,
                      "durationMs": 9000,
                      "scrapedAt": "2026-10-27T07:19:00Z",
                      "checkDigitValid": true,
                      "data": {
                        "currentStatus": "CX",
                        "location": "MEXICO CITY",
                        "vessel": "MAERSK EL ALTO",
                        "voyage": "636E",
                        "lastUpdate": "2026-10-27T07:19:00Z",
                        "arrived": true,
                        "arrivalDate": "2026-10-06T12:28:00Z",
                        "dischargePort": "MANZANILLO",
                        "finalDeliveryEta": "2026-10-31T13:57:00Z",
                        "events": []
                      }
                    }
                  ]
                }
                """;

        mockMvc.perform(post("/api/v1/scraper/results")
                        .header("Authorization", AUTH)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(payload))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.updated").value(1));

        mockMvc.perform(get("/api/v1/tracking/MRKU7779494"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.arrived").value(true))
                .andExpect(jsonPath("$.arrivalDate").value("2026-10-06T12:28:00Z"))
                .andExpect(jsonPath("$.dischargePort").value("MANZANILLO"))
                .andExpect(jsonPath("$.finalDeliveryEta").value("2026-10-31T13:57:00Z"))
                // No se inventa una ETA de puerto cuando ya llegó.
                .andExpect(jsonPath("$.eta").doesNotExist())
                // El último evento terrestre no se confunde con la llegada.
                .andExpect(jsonPath("$.currentStatus").value("CX"))
                .andExpect(jsonPath("$.location").value("MEXICO CITY"));
    }

    @Test
    @DisplayName("El webhook rechaza peticiones sin token")
    void webhookRequiresBearer() throws Exception {
        mockMvc.perform(post("/api/v1/scraper/results")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"runId\":\"x\",\"results\":[]}"))
                .andExpect(status().isUnauthorized());
    }

    @Test
    @DisplayName("La ruta base sin /results también acepta el webhook y sigue protegida")
    void webhookAcceptsBasePath() throws Exception {
        mockMvc.perform(post("/api/v1/scraper")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"runId\":\"x\",\"results\":[]}"))
                .andExpect(status().isUnauthorized());

        mockMvc.perform(post("/api/v1/scraper")
                        .header("Authorization", AUTH)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"runId\":\"base-path\",\"results\":[]}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.runId").value("base-path"));
    }

    @Test
    @DisplayName("dispatch sin token de GitHub devuelve 503 con explicación")
    void dispatchWithoutGitHubTokenReturns503() throws Exception {
        mockMvc.perform(post("/api/v1/tracking/dispatch")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"containers\":[\"MSCU1234567\"]}"))
                .andExpect(status().isServiceUnavailable())
                .andExpect(jsonPath("$.code").value("DISPATCH_FAILED"));
    }

    @Test
    @DisplayName("dispatch valida el formato ISO 6346 antes de llamar a GitHub")
    void dispatchValidatesContainerFormat() throws Exception {
        mockMvc.perform(post("/api/v1/tracking/dispatch")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"containers\":[\"NO-ES-CONTENEDOR\"]}"))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("VALIDATION_ERROR"));
    }
}
