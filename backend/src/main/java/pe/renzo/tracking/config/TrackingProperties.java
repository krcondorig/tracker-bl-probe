package pe.renzo.tracking.config;

import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.util.Assert;

import java.time.Duration;
import java.util.List;

/**
 * Propiedades del orquestador.
 *
 * <pre>
 * tracking:
 *   github:
 *     owner: renzo
 *     repo: scraper
 *     workflow-file: scraper.yml
 *     ref: main
 *     token: ${GITHUB_DISPATCH_TOKEN}
 *   scraper:
 *     shared-secret: ${SCRAPER_SHARED_SECRET}
 *     max-batch: 40
 *     carrier-whitelist: [MSC, MAERSK, HAPAG_LLOYD]
 *     pending-max-age: PT6H
 * </pre>
 */
@ConfigurationProperties(prefix = "tracking")
public record TrackingProperties(Github github, Scraper scraper) {

    public TrackingProperties {
        Assert.notNull(github, "tracking.github es obligatorio");
        Assert.notNull(scraper, "tracking.scraper es obligatorio");
    }

    public record Github(
            String owner,
            String repo,
            String workflowFile,
            String ref,
            String token,
            String apiBaseUrl,
            Duration timeout
    ) {
        public Github {
            apiBaseUrl = (apiBaseUrl == null || apiBaseUrl.isBlank())
                    ? "https://api.github.com" : apiBaseUrl;
            ref = (ref == null || ref.isBlank()) ? "main" : ref;
            workflowFile = (workflowFile == null || workflowFile.isBlank()) ? "scraper.yml" : workflowFile;
            timeout = (timeout == null) ? Duration.ofSeconds(20) : timeout;
        }

        public boolean isConfigured() {
            return notBlank(owner) && notBlank(repo) && notBlank(token);
        }

        private static boolean notBlank(String value) {
            return value != null && !value.isBlank();
        }
    }

    public record Scraper(
            String sharedSecret,
            int maxBatch,
            List<String> carrierWhitelist,
            Duration pendingMaxAge,
            Duration recheckInterval
    ) {
        public Scraper {
            maxBatch = maxBatch <= 0 ? 40 : maxBatch;
            carrierWhitelist = (carrierWhitelist == null || carrierWhitelist.isEmpty())
                    ? List.of("MSC", "MAERSK", "HAPAG_LLOYD")
                    : List.copyOf(carrierWhitelist);
            pendingMaxAge = (pendingMaxAge == null) ? Duration.ofHours(6) : pendingMaxAge;
            // 20 h: el cron es diario, así cada contenedor activo se refresca una
            // vez al día sin repetirse dentro de la misma jornada.
            recheckInterval = (recheckInterval == null) ? Duration.ofHours(20) : recheckInterval;
        }
    }
}
