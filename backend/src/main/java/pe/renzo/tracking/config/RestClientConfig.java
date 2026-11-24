package pe.renzo.tracking.config;

import org.springframework.boot.web.client.ClientHttpRequestFactories;
import org.springframework.boot.web.client.ClientHttpRequestFactorySettings;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.HttpHeaders;
import org.springframework.http.client.JdkClientHttpRequestFactory;
import org.springframework.web.client.RestClient;
import pe.renzo.tracking.service.GitHubActionsService;

import java.net.http.HttpClient;
import java.time.Duration;

@Configuration
public class RestClientConfig {

    /**
     * Cliente dedicado a la API de GitHub. Se configura aparte del bean
     * {@code RestClient.Builder} por defecto para no arrastrar headers o
     * interceptores pensados para otras integraciones.
     */
    @Bean
    public RestClient gitHubRestClient(TrackingProperties properties) {
        TrackingProperties.Github github = properties.github();

        HttpClient httpClient = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(10))
                .followRedirects(HttpClient.Redirect.NORMAL)
                .build();

        JdkClientHttpRequestFactory requestFactory = new JdkClientHttpRequestFactory(httpClient);
        requestFactory.setReadTimeout(github.timeout());

        return RestClient.builder()
                .baseUrl(github.apiBaseUrl())
                .requestFactory(requestFactory)
                .defaultHeader(HttpHeaders.ACCEPT, "application/vnd.github+json")
                .defaultHeader("X-GitHub-Api-Version", GitHubActionsService.API_VERSION)
                .defaultHeader(HttpHeaders.USER_AGENT, "tracking-scraper-backend/1.0")
                .build();
    }
}
