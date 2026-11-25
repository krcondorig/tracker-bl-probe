package probe;

import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import org.openqa.selenium.By;
import org.openqa.selenium.JavascriptExecutor;
import org.openqa.selenium.WebDriver;
import org.openqa.selenium.WebElement;
import org.openqa.selenium.chrome.ChromeDriver;
import org.openqa.selenium.chrome.ChromeOptions;

/**
 * Sonda: ¿puede Chrome (Selenium) pasar Cloudflare en Hapag-Lloyd a través de WARP?
 *
 * <p>La pregunta nace de una asimetría: la prueba de referencia usó Camoufox
 * (Firefox parcheado, Playwright), que deriva timezone/locale/WebRTC de la IP de
 * salida. El proyecto real usa Selenium + Chrome. La IP de confianza viaja con
 * WARP; la coherencia de huella no. Esto mide si con la IP basta.
 *
 * <p>Se ejecutan cuatro escenarios en un solo proceso para compararlos con la
 * misma IP de egreso:
 *
 * <ol>
 *   <li>directo + headless  — control: debería fallar (IP del runner)</li>
 *   <li>WARP + headless     — el escenario que importa</li>
 *   <li>WARP + headful      — headless es más detectable; xvfb da pantalla</li>
 *   <li>WARP + headful + stealth CDP — oculta navigator.webdriver</li>
 * </ol>
 *
 * <p>No decide nada por sí sola: imprime el veredicto por escenario con la
 * evidencia (título, estados encontrados, filas parseadas).
 */
public final class Probe {

    private static final String CONTAINER = "HLBU2959982";

    /** Forma medida del parámetro: `HLBU  2959982` -> `HLBU++2959982`. */
    private static final String CONTAINER_PARAM = "HLBU++2959982";

    private static final String DETAIL_URL =
            "https://www.hapag-lloyd.com/en/online-business/track/"
            + "track-by-container-solution-legacy.html"
            + "?view=S8510&container=" + CONTAINER_PARAM;

    /** Estados exactos que emite la tabla de movimientos del sitio. */
    private static final String[] HAPAG_STATES = {
        "Gate out empty", "Gate in empty", "Gate out full", "Gate in full",
        "Arrival in", "Departure from", "Loaded", "Discharged",
        "Vessel departed", "Vessel arrived",
    };

    /** Señales de que Cloudflare/Akamai frenó la petición. */
    private static final String[] BLOCK_MARKERS = {
        "just a moment", "managed challenge", "verifying you are human",
        "enable javascript and cookies", "checking your browser",
        "attention required", "access denied", "challenge-platform",
    };

    private static final String STEALTH_JS = """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = window.chrome || {runtime: {}};
            Object.defineProperty(navigator, 'languages',
                {get: () => ['es-PE', 'es', 'en']});
            Object.defineProperty(navigator, 'plugins',
                {get: () => [1, 2, 3, 4, 5]});
            """;

    public static void main(String[] args) {
        String proxy = System.getenv().getOrDefault("PROBE_PROXY", "");
        List<Map<String, Object>> results = new ArrayList<>();

        results.add(run("directo+headless", "", true, false));
        results.add(run("warp+headless", proxy, true, false));
        results.add(run("warp+headful", proxy, false, false));
        results.add(run("warp+headful+stealth", proxy, false, true));

        System.out.println();
        System.out.println("================ VEREDICTO ================");
        for (Map<String, Object> r : results) {
            System.out.printf("%-24s detalle=%-5s estados=%-2d filas=%-2d bloqueo=%-5s %s%n",
                    r.get("scenario"), r.get("isDetail"), r.get("states"),
                    r.get("rows"), r.get("blocked"), r.get("title"));
        }
        System.out.println("===========================================");
    }

    private static Map<String, Object> run(String scenario, String proxy,
                                           boolean headless, boolean stealth) {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("scenario", scenario);
        out.put("isDetail", false);
        out.put("states", 0);
        out.put("rows", 0);
        out.put("blocked", "?");
        out.put("title", "");

        ChromeOptions options = new ChromeOptions();
        options.addArguments("--no-sandbox", "--disable-dev-shm-usage",
                "--disable-gpu", "--window-size=1440,900",
                "--lang=es-PE");
        if (headless) {
            options.addArguments("--headless=new");
        }
        if (proxy != null && !proxy.isBlank()) {
            options.addArguments("--proxy-server=" + proxy);
        }
        if (!stealth) {
            // Por defecto Selenium anuncia la automatización; se deja así en los
            // tres primeros escenarios para ver si eso es lo que delata.
            options.addArguments("--enable-automation");
        }
        options.setExperimentalOption("excludeSwitches",
                stealth ? List.of("enable-automation") : List.of());
        options.setExperimentalOption("useAutomationExtension", !stealth);

        WebDriver driver = null;
        long started = System.currentTimeMillis();
        try {
            driver = new ChromeDriver(options);
            driver.manage().timeouts().pageLoadTimeout(Duration.ofSeconds(60));

            if (stealth) {
                ((ChromeDriver) driver).executeCdpCommand(
                        "Page.addScriptToEvaluateOnNewDocument",
                        Map.of("source", STEALTH_JS));
            }

            driver.get(DETAIL_URL);

            // La tabla la pinta ExtJS después del HTML: se sondea, no se lee una vez.
            String text = "";
            long deadline = System.currentTimeMillis() + 25_000;
            while (System.currentTimeMillis() < deadline) {
                try {
                    text = bodyText(driver);
                } catch (Exception e) {
                    text = "";
                }
                if (countStates(text) >= 2) {
                    break;
                }
                sleep(1000);
            }
            if (countStates(text) < 2) {
                try {
                    text = bodyText(driver);
                } catch (Exception ignored) {
                    // se conserva el último texto disponible
                }
            }

            String title = "";
            try {
                title = driver.getTitle();
            } catch (Exception ignored) {
                // título no disponible: se reporta vacío
            }

            int states = countStates(text);
            out.put("title", oneLine(title));
            out.put("states", states);
            out.put("isDetail", states >= 2);
            out.put("rows", countRows(text));
            out.put("blocked", isBlocked(title, text));
            out.put("elapsedMs", System.currentTimeMillis() - started);
            out.put("snippet", oneLine(text).substring(0, Math.min(160, oneLine(text).length())));
        } catch (Exception e) {
            out.put("error", e.getClass().getSimpleName() + ": " + oneLine(e.getMessage()));
            out.put("elapsedMs", System.currentTimeMillis() - started);
        } finally {
            if (driver != null) {
                try {
                    driver.quit();
                } catch (Exception ignored) {
                    // el cierre no debe tapar el resultado
                }
            }
        }

        System.out.println(out);
        return out;
    }

    private static String bodyText(WebDriver driver) {
        try {
            WebElement body = driver.findElement(By.tagName("body"));
            String inner = body.getText();
            if (inner != null && !inner.isBlank()) {
                return inner;
            }
        } catch (Exception ignored) {
            // se intenta el fallback por JS
        }
        Object raw = ((JavascriptExecutor) driver).executeScript(
                "return document.body ? document.body.innerText : '';");
        return raw == null ? "" : raw.toString();
    }

    /** Equivalente a `is_real_detail` del proyecto: cuenta estados, no bytes. */
    private static int countStates(String text) {
        String flat = text == null ? "" : text.toLowerCase().replaceAll("\\s+", " ");
        int found = 0;
        for (String state : HAPAG_STATES) {
            if (flat.contains(state.toLowerCase())) {
                found++;
            }
        }
        return found;
    }

    /** Filas que empiezan por un estado conocido: cuántos movimientos hay. */
    private static int countRows(String text) {
        int rows = 0;
        for (String line : (text == null ? "" : text).split("\\R")) {
            String trimmed = line.strip();
            for (String state : HAPAG_STATES) {
                if (trimmed.startsWith(state)) {
                    rows++;
                    break;
                }
            }
        }
        return rows;
    }

    private static String isBlocked(String title, String text) {
        String flat = ((title == null ? "" : title) + " "
                + (text == null ? "" : text)).toLowerCase();
        for (String marker : BLOCK_MARKERS) {
            if (flat.contains(marker)) {
                return "si:" + marker;
            }
        }
        return "no";
    }

    private static String oneLine(String value) {
        return value == null ? "" : value.replaceAll("\\s+", " ").strip();
    }

    private static void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    private Probe() {
    }
}
