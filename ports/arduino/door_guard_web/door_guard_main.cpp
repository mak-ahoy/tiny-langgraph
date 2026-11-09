/*
 * tiny-langgraph door guard, with a web page that shows the agent thinking.
 *
 * The code lives in this .cpp file instead of the .ino, so the Arduino IDE
 * compiles it as plain C++ without rewriting it.
 *
 *   1. Edit config.h (Wi-Fi, PIN).
 *   2. Tools menu: Board "ESP32S3 Dev Module", Flash Size 16MB, PSRAM "OPI PSRAM",
 *      Partition Scheme "16M Flash (3MB APP/9.9MB FATFS)".
 *   3. Upload, open the Serial Monitor at 115200, and open the address it prints.
 *
 * Web API (same origin only, no CORS on purpose: other websites you visit
 * can't read the board through your browser):
 *   GET  /                 the trace viewer
 *   GET  /tlg/info         {"board":..., "boot":...}
 *   GET  /tlg/events       ?since=N[&graph=1] -> trace lines; headers X-Last, X-More, X-Boot
 *   GET  /tlg/pending      {"pending":true,"run":7,"question":"approve alarm?"}
 *   POST /tlg/answer       pin, run, answer=yes|no
 */
#include <Arduino.h>
#include <ESPmDNS.h>
#include <WebServer.h>
#include <WiFi.h>

#include "config.h"
#include "door_guard_model.h"
#include "web_viewer.h"

extern "C" {
#include "agent.h"
#include "scenarios.h"
#include "tlg_hub.h"
}

/* ---------------- shared state ---------------- */
static WebServer       server(80);
static tlg_hub_t       hub;
static SemaphoreHandle_t hub_lock;
static tlg_tracer_t    tracer;
static tlg_lm_t        lm;
static dg_agent_t      agent;
static QueueHandle_t   requests;          /* scenario numbers to run */
static uint32_t        boot_id;

static portMUX_TYPE    mux = portMUX_INITIALIZER_UNLOCKED;
static uint32_t        pending_run;       /* 0 = nothing waiting */
static char            pending_question[48];
static int             answer_given;      /* 0 none, 1 yes, 2 no */
static const char     *answer_from = "";

static int             pin_failures;
static uint32_t        pin_locked_until;

/* ---------------- tracing ---------------- */
static uint32_t clock_ms(void) { return millis(); }

static void trace_write(const char *line, void *ctx)
{
    (void)ctx;
    xSemaphoreTake(hub_lock, portMAX_DELAY);
    tlg_hub_put(&hub, line);
    xSemaphoreGive(hub_lock);
    if (SERIAL_TRACE) Serial.print(line);
}

static void human_log(const char *line, void *ctx)
{
    (void)ctx;
    if (!SERIAL_TRACE) Serial.printf("    %s\n", line);
}

static void blink(int times, int ms)
{
#if LED_PIN >= 0
    for (int i = 0; i < times; i++) {
        digitalWrite(LED_PIN, HIGH); vTaskDelay(pdMS_TO_TICKS(ms));
        digitalWrite(LED_PIN, LOW);  vTaskDelay(pdMS_TO_TICKS(ms));
    }
#else
    (void)times; (void)ms;
#endif
}

/* The real actions. Here they only print and blink; wire them to your hardware. */
static bool run_tool(int action, void *ctx)
{
    (void)ctx;
    switch (action) {
    case DOOR_GUARD_ACT_LED_FLASH: blink(5, 100); break;
    case DOOR_GUARD_ACT_ALARM:     blink(20, 50); break;
    default: break;
    }
    return true;
}

/* ---------------- approvals ---------------- */
static dg_answer_t wait_for_answer(uint32_t run_id, const char *question)
{
    portENTER_CRITICAL(&mux);
    pending_run = run_id;
    strncpy(pending_question, question, sizeof pending_question - 1);
    pending_question[sizeof pending_question - 1] = '\0';
    answer_given = 0;
    portEXIT_CRITICAL(&mux);

    Serial.printf("\n  >>> %s  (web page with PIN, BOOT button = yes, or type y/n here)\n", question);

    dg_answer_t a = DG_ANSWER_NO;
    const char *from = "timeout";
    uint32_t t0 = millis();
    while (millis() - t0 < APPROVAL_TIMEOUT_MS) {
        int given;
        portENTER_CRITICAL(&mux);
        given = answer_given;
        portEXIT_CRITICAL(&mux);
        if (given) { a = given == 1 ? DG_ANSWER_YES : DG_ANSWER_NO; from = answer_from; break; }
        if (digitalRead(BOOT_PIN) == LOW) { a = DG_ANSWER_YES; from = "BOOT button"; break; }
        vTaskDelay(pdMS_TO_TICKS(50));
    }

    portENTER_CRITICAL(&mux);
    pending_run = 0;
    portEXIT_CRITICAL(&mux);

    tlg_trace_note(&tracer, "answer: %s (from %s)", a == DG_ANSWER_YES ? "yes" : "no", from);
    if (!SERIAL_TRACE) Serial.printf("  >>> answer: %s (from %s)\n", a == DG_ANSWER_YES ? "yes" : "no", from);
    return a;
}

static bool give_answer(uint32_t run, bool yes, const char *from)
{
    bool ok = false;
    portENTER_CRITICAL(&mux);
    if (pending_run != 0 && (run == 0 || run == pending_run) && answer_given == 0) {
        answer_given = yes ? 1 : 2;
        answer_from = from;
        ok = true;
    }
    portEXIT_CRITICAL(&mux);
    return ok;
}

/* ---------------- the agent task ---------------- */
static void run_scenario(tlg_graph_t *g, int i)
{
    const dg_scenario_t *sc = &dg_scenarios[i];
    agent.in = sc->in;
    dg_begin(&agent);
    tlg_trace_run(&tracer, sc->label);
    if (!SERIAL_TRACE) Serial.printf("\n[%d] %s\n", i + 1, sc->label);

    tlg_run_t run;
    tlg_run_init(&run, g);
    uint32_t t0 = millis();
    tlg_status_t st = tlg_invoke(g, &run, &agent);
    while (st == TLG_INTERRUPTED) {
        tlg_trace_end(&tracer, &run);
        agent.answer = wait_for_answer(tracer.run_id, run.interrupt_reason);
        st = tlg_invoke(g, &run, &agent);
    }
    tlg_trace_end(&tracer, &run);
    if (!SERIAL_TRACE)
        Serial.printf("  -> %s after %lu steps (model %lu ms, run %lu ms, free heap %lu)\n",
                      tlg_status_str(st), (unsigned long)run.step, (unsigned long)agent.lm_ms,
                      (unsigned long)(millis() - t0), (unsigned long)ESP.getFreeHeap());
}

static void agent_task(void *arg)
{
    (void)arg;
    tlg_graph_t g;
    dg_graph(&g, &agent);
    char err[96];
    if (tlg_validate(&g, err, sizeof err) < 0) {
        Serial.printf("invalid graph: %s\n", err);
        vTaskDelete(NULL);
    }
    tlg_trace_graph(&tracer, &g, "door_guard");

    int next = 0;
    uint32_t last_run = millis();
    for (;;) {
        int pick;
        if (xQueueReceive(requests, &pick, pdMS_TO_TICKS(100)) != pdTRUE) {
            if (DEMO_INTERVAL_MS == 0 || millis() - last_run < DEMO_INTERVAL_MS) continue;
            pick = next;
            next = (next + 1) % dg_n_scenarios;
        }
        run_scenario(&g, pick);
        last_run = millis();
    }
}

/* ---------------- web server ---------------- */
static void no_cache() { server.sendHeader("Cache-Control", "no-store"); }

static void handle_root()
{
    server.sendHeader("Content-Encoding", "gzip");
    server.send_P(200, "text/html", (PGM_P)WEB_VIEWER_GZ, WEB_VIEWER_GZ_LEN);
}

static void handle_info()
{
    char body[128];
    snprintf(body, sizeof body, "{\"board\":\"door_guard\",\"boot\":\"%08lx\",\"approval\":true}",
             (unsigned long)boot_id);
    no_cache();
    server.send(200, "application/json", body);
}

static void handle_events()
{
    static char out[12 * 1024];
    uint32_t since = (uint32_t)strtoul(server.arg("since").c_str(), NULL, 10);
    int with_graph = server.hasArg("graph");
    uint32_t last;
    int more;
    xSemaphoreTake(hub_lock, portMAX_DELAY);
    size_t n = tlg_hub_get(&hub, since, with_graph, out, sizeof out, &last, &more);
    xSemaphoreGive(hub_lock);

    char buf[16];
    snprintf(buf, sizeof buf, "%lu", (unsigned long)last);
    server.sendHeader("X-Last", buf);
    server.sendHeader("X-More", more ? "1" : "0");
    snprintf(buf, sizeof buf, "%08lx", (unsigned long)boot_id);
    server.sendHeader("X-Boot", buf);
    no_cache();
    server.send_P(200, "text/plain", out, n);
}

static void handle_pending()
{
    char q[48], esc[96], body[160];
    uint32_t run;
    portENTER_CRITICAL(&mux);
    run = pending_run;
    memcpy(q, pending_question, sizeof q);
    portEXIT_CRITICAL(&mux);
    if (run) {
        tlg_json_escape(q, esc, sizeof esc);
        snprintf(body, sizeof body, "{\"pending\":true,\"run\":%lu,\"question\":\"%s\"}", (unsigned long)run, esc);
    } else {
        snprintf(body, sizeof body, "{\"pending\":false}");
    }
    no_cache();
    server.send(200, "application/json", body);
}

static void handle_answer()
{
    char body[96];
    if (millis() < pin_locked_until) {
        snprintf(body, sizeof body, "{\"error\":\"locked\",\"retry\":%lu}",
                 (unsigned long)((pin_locked_until - millis()) / 1000 + 1));
        server.send(429, "application/json", body);
        return;
    }
    if (server.arg("pin") != APPROVAL_PIN) {
        if (++pin_failures >= 5) { pin_locked_until = millis() + 60000; pin_failures = 0; }
        server.send(403, "application/json", "{\"error\":\"wrong pin\"}");
        return;
    }
    pin_failures = 0;
    String ans = server.arg("answer");
    if (ans != "yes" && ans != "no") {
        server.send(400, "application/json", "{\"error\":\"answer must be yes or no\"}");
        return;
    }
    uint32_t run = (uint32_t)strtoul(server.arg("run").c_str(), NULL, 10);
    if (!give_answer(run, ans == "yes", "web page")) {
        server.send(409, "application/json", "{\"error\":\"nothing is waiting for an answer\"}");
        return;
    }
    server.send(200, "application/json", "{\"ok\":true}");
}

static void start_network()
{
    WiFi.setHostname(HOSTNAME);
    bool joined = false;
    if (strlen(WIFI_SSID) > 0) {
        WiFi.mode(WIFI_STA);
        WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
        Serial.printf("joining Wi-Fi \"%s\"", WIFI_SSID);
        for (int i = 0; i < 30 && WiFi.status() != WL_CONNECTED; i++) { delay(500); Serial.print("."); }
        Serial.println();
        joined = WiFi.status() == WL_CONNECTED;
    }
    IPAddress ip;
    if (joined) {
        ip = WiFi.localIP();
    } else {
        WiFi.mode(WIFI_AP);
        WiFi.softAP(HOSTNAME, AP_PASSWORD);
        ip = WiFi.softAPIP();
        Serial.printf("no Wi-Fi: started network \"%s\" (password \"%s\")\n", HOSTNAME, AP_PASSWORD);
    }
    if (MDNS.begin(HOSTNAME)) MDNS.addService("http", "tcp", 80);

    server.on("/", HTTP_GET, handle_root);
    server.on("/tlg/info", HTTP_GET, handle_info);
    server.on("/tlg/events", HTTP_GET, handle_events);
    server.on("/tlg/pending", HTTP_GET, handle_pending);
    server.on("/tlg/answer", HTTP_POST, handle_answer);
    server.onNotFound([]() { server.send(404, "text/plain", "not found"); });
    server.begin();

    Serial.printf("\nopen  http://%s/   or   http://%s.local/\n", ip.toString().c_str(), HOSTNAME);
    Serial.printf("approval PIN: set in config.h\n\n");
}

/* ---------------- setup / loop ---------------- */
void setup()
{
    Serial.begin(115200);
    delay(1500);
    Serial.println("\ntiny-langgraph door guard");
    pinMode(BOOT_PIN, INPUT_PULLUP);
#if LED_PIN >= 0
    pinMode(LED_PIN, OUTPUT);
#endif
    boot_id = esp_random();

    /* event memory: plenty in PSRAM if the board has it */
    size_t slots = psramFound() ? 256 : 48;
    size_t bytes = slots * sizeof(tlg_hub_slot_t);
    void *mem = psramFound() ? ps_malloc(bytes) : malloc(bytes);
    if (!mem) { bytes = 16 * sizeof(tlg_hub_slot_t); mem = malloc(bytes); }
    hub_lock = xSemaphoreCreateMutex();
    Serial.printf("event memory: %lu events\n", (unsigned long)tlg_hub_init(&hub, mem, mem ? bytes : 0));

    tracer.write = trace_write;
    tracer.clock = clock_ms;

    uint32_t heap = ESP.getFreeHeap();
    bool model_ok = tlg_lm_init(&lm, DOOR_GUARD_MODEL, DOOR_GUARD_MODEL_LEN) && lm.cfg.vocab == DOOR_GUARD_N_TOKENS;
    Serial.printf("model: %s (%lu bytes of weights in flash, %lu bytes of RAM)\n",
                  model_ok ? "loaded" : "NOT loaded, planner only", (unsigned long)DOOR_GUARD_MODEL_LEN,
                  (unsigned long)(heap - ESP.getFreeHeap()));

    agent.lm = model_ok ? &lm : NULL;
    agent.tracer = &tracer;
    agent.tool = run_tool;
    agent.log = human_log;

    start_network();

    requests = xQueueCreate(8, sizeof(int));
    xTaskCreatePinnedToCore(agent_task, "agent", 12288, NULL, 1, NULL, 1);
}

void loop()
{
    server.handleClient();

    /* Serial Monitor: 1-8 runs a scenario, y/n answers a pending question */
    while (Serial.available()) {
        char c = (char)Serial.read();
        if (c == 'y' || c == 'n') {
            if (!give_answer(0, c == 'y', "serial monitor")) Serial.println("nothing is waiting for an answer");
        } else if (c >= '1' && c <= '0' + dg_n_scenarios) {
            int pick = c - '1';
            xQueueSend(requests, &pick, 0);
        }
    }
    delay(2);
}
