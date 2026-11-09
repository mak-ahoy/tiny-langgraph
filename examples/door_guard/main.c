/*
 * Door guard on a PC: runs scenarios through the agent with the trained model.
 *
 *   door_guard                   readable output
 *   door_guard --trace           @tlg events for the viewer:  door_guard --trace > run.log
 *   door_guard --eval val.txt    model accuracy on held-out examples
 *   door_guard --model path.bin  use another model file
 *   door_guard --corrupt 200     damage 200 model weights on purpose: the agent must stay safe
 */
#include "agent.h"
#include "scenarios.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static uint32_t clock_ms(void)
{
    return (uint32_t)((double)clock() * 1000.0 / CLOCKS_PER_SEC);
}

static bool g_human = true;

static void log_line(const char *line, void *ctx)
{
    (void)ctx;
    if (g_human) printf("    %s\n", line);
}

static bool run_tool(int action, void *ctx)
{
    (void)action; (void)ctx;
    return true;    /* on a device: drive the LED, send MQTT, ... */
}

static void write_line(const char *line, void *ctx)
{
    (void)ctx;
    fputs(line, stdout);
}

static int run_scenarios(dg_agent_t *a, tlg_tracer_t *tr)
{
    tlg_graph_t g;
    dg_graph(&g, a);

    char err[96];
    if (tlg_validate(&g, err, sizeof err) < 0) {
        fprintf(stderr, "invalid graph: %s\n", err);
        return 1;
    }
    if (a->tracer) tlg_trace_graph(tr, &g, "door_guard");

    for (int i = 0; i < dg_n_scenarios; i++) {
        const dg_scenario_t *sc = &dg_scenarios[i];
        a->in = sc->in;
        dg_begin(a);
        if (a->tracer) tlg_trace_run(tr, sc->label);
        if (g_human) printf("\n[%d] %s\n", i + 1, sc->label);

        tlg_run_t run;
        tlg_run_init(&run, &g);
        tlg_status_t st = tlg_invoke(&g, &run, a);
        const char *ans = sc->answers;
        while (st == TLG_INTERRUPTED) {
            if (a->tracer) tlg_trace_end(tr, &run);
            char c = *ans ? *ans++ : 'n';
            if (g_human) printf("    >>> human answers: %s\n", c == 'y' ? "yes" : "no");
            a->answer = c == 'y' ? DG_ANSWER_YES : DG_ANSWER_NO;
            st = tlg_invoke(&g, &run, a);
        }
        if (a->tracer) tlg_trace_end(tr, &run);
        if (g_human) printf("  -> %s after %lu steps\n", tlg_status_str(st), (unsigned long)run.step);
    }
    return 0;
}

/* Fault injection: overwrite n weights with large random values (deterministic). */
static void corrupt(tlg_lm_t *lm, long n)
{
    const tlg_lm_config_t *c = &lm->cfg;
    float *w = (float *)((char *)lm->blob + 4 + sizeof(tlg_lm_config_t));
    size_t d = (size_t)c->dim, h = (size_t)c->hidden;
    size_t count = (size_t)c->vocab * d + (size_t)c->seq_len * d
                 + (size_t)c->layers * (d + 3 * d * d + d * d + d + h * d + d * h) + d;
    uint32_t x = 12345u;
    for (long i = 0; i < n; i++) {
        x ^= x << 13; x ^= x >> 17; x ^= x << 5;
        w[x % count] = (float)((int32_t)(x >> 8) % 300) / 100.0f;    /* -3 .. +3 */
    }
}

static int eval(tlg_lm_t *lm, const char *path)
{
    FILE *f = fopen(path, "r");
    if (!f) { perror(path); return 1; }
    char line[512];
    int total = 0, exact = 0;
    clock_t t0 = clock();
    while (fgets(line, sizeof line, f)) {
        uint8_t seq[DOOR_GUARD_SEQ_LEN];
        int len = 0, think = -1;
        for (char *tok = strtok(line, " \r\n"); tok && len < DOOR_GUARD_SEQ_LEN; tok = strtok(NULL, " \r\n")) {
            int id = -1;
            for (int i = 0; i < DOOR_GUARD_N_TOKENS; i++)
                if (strcmp(tok, door_guard_token_names[i]) == 0) { id = i; break; }
            if (id < 0) { fprintf(stderr, "unknown token %s\n", tok); fclose(f); return 1; }
            if (id == DOOR_GUARD_TOK_THINK && think < 0) think = len;
            seq[len++] = (uint8_t)id;
        }
        if (think < 0) continue;
        uint8_t out[DOOR_GUARD_SEQ_LEN];
        int n = tlg_lm_generate(lm, seq, think + 1, DOOR_GUARD_TOK_EOS, out, DOOR_GUARD_SEQ_LEN - think - 1, NULL);
        exact += n == len - think - 1 && memcmp(out, seq + think + 1, (size_t)n) == 0;
        total++;
    }
    fclose(f);
    double ms = 1000.0 * (double)(clock() - t0) / CLOCKS_PER_SEC;
    printf("held-out examples: %d, exact answers: %d (%.2f%%), %.2f ms per decision on this PC\n",
           total, exact, total ? 100.0 * exact / total : 0.0, total ? ms / total : 0.0);
    return exact == total ? 0 : 2;
}

int main(int argc, char **argv)
{
    const char *model_path = "examples/door_guard/model/door_guard.bin";
    const char *eval_path = NULL;
    bool trace = false;
    long damage = 0;
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--trace") == 0) trace = true;
        else if (strcmp(argv[i], "--model") == 0 && i + 1 < argc) model_path = argv[++i];
        else if (strcmp(argv[i], "--eval") == 0 && i + 1 < argc) eval_path = argv[++i];
        else if (strcmp(argv[i], "--corrupt") == 0 && i + 1 < argc) damage = atol(argv[++i]);
        else {
            fprintf(stderr, "usage: %s [--trace] [--model file] [--eval val.txt] [--corrupt N]\n", argv[0]);
            return 1;
        }
    }

    static tlg_lm_t lm;
    bool have_model = tlg_lm_load_file(&lm, model_path);
    if (have_model && lm.cfg.vocab != DOOR_GUARD_N_TOKENS) {
        fprintf(stderr, "model vocabulary %d != schema %d: retrain\n", lm.cfg.vocab, DOOR_GUARD_N_TOKENS);
        return 1;
    }
    if (have_model && damage > 0) corrupt(&lm, damage);
    if (eval_path) {
        if (!have_model) return 1;
        int rc = eval(&lm, eval_path);
        tlg_lm_free(&lm);
        return rc;
    }

    static tlg_tracer_t tr;
    tr.write = write_line;
    tr.clock = clock_ms;
    g_human = !trace;

    dg_agent_t agent = { 0 };
    agent.lm = have_model ? &lm : NULL;
    agent.tracer = trace ? &tr : NULL;
    agent.tool = run_tool;
    agent.log = log_line;

    if (g_human) {
        printf("door guard: %s\n", have_model ? "model loaded" : "no model, planner only");
        if (damage > 0) printf("fault injection: %ld weights damaged\n", damage);
    }
    int rc = run_scenarios(&agent, &tr);
    if (have_model) tlg_lm_free(&lm);
    return rc;
}
