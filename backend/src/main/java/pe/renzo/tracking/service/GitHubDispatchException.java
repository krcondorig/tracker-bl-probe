package pe.renzo.tracking.service;

/** Fallo al invocar la API de GitHub Actions. */
public class GitHubDispatchException extends RuntimeException {

    private final boolean configurationError;

    private GitHubDispatchException(String message, boolean configurationError, Throwable cause) {
        super(message, cause);
        this.configurationError = configurationError;
    }

    /** 503: faltan owner/repo/token en la configuración. */
    public static GitHubDispatchException configuration(String message) {
        return new GitHubDispatchException(message, true, null);
    }

    /** 502: GitHub rechazó la petición o no respondió. */
    public static GitHubDispatchException upstream(String message, Throwable cause) {
        return new GitHubDispatchException(message, false, cause);
    }

    public boolean isConfigurationError() {
        return configurationError;
    }
}
