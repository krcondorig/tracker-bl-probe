package pe.renzo.tracking.api;

import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import pe.renzo.tracking.service.GitHubDispatchException;

import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.Map;

@RestControllerAdvice
public class ApiExceptionHandler {

    @ExceptionHandler(MethodArgumentNotValidException.class)
    public ResponseEntity<Map<String, Object>> onValidation(MethodArgumentNotValidException ex) {
        Map<String, String> fields = new LinkedHashMap<>();
        ex.getBindingResult().getFieldErrors()
                .forEach(error -> fields.putIfAbsent(error.getField(), error.getDefaultMessage()));
        return ResponseEntity.badRequest().body(body("VALIDATION_ERROR", "Petición inválida", fields));
    }

    @ExceptionHandler(GitHubDispatchException.class)
    public ResponseEntity<Map<String, Object>> onDispatch(GitHubDispatchException ex) {
        HttpStatus status = ex.isConfigurationError() ? HttpStatus.SERVICE_UNAVAILABLE : HttpStatus.BAD_GATEWAY;
        return ResponseEntity.status(status).body(body("DISPATCH_FAILED", ex.getMessage(), null));
    }

    @ExceptionHandler(IllegalArgumentException.class)
    public ResponseEntity<Map<String, Object>> onIllegalArgument(IllegalArgumentException ex) {
        return ResponseEntity.badRequest().body(body("BAD_REQUEST", ex.getMessage(), null));
    }

    @ExceptionHandler(Exception.class)
    public ResponseEntity<Map<String, Object>> onUnexpected(Exception ex) {
        // No se filtra el stack al cliente, pero sí se registra en el log del servidor.
        return ResponseEntity.internalServerError()
                .body(body("INTERNAL_ERROR", "Error interno: " + ex.getClass().getSimpleName(), null));
    }

    private static Map<String, Object> body(String code, String message, Map<String, String> fields) {
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("timestamp", Instant.now());
        payload.put("code", code);
        payload.put("message", message);
        if (fields != null && !fields.isEmpty()) {
            payload.put("fields", fields);
        }
        return payload;
    }
}
